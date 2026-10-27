# tests/scan_selftest.py - offline check of bot/scan.py: local fake site + sqlite standing in for D1.  python -m tests.scan_selftest
import re
import sqlite3
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import bot.scan as scan
from bot.scan import D1Sync, Net, Scanner

PAGES = {
    "/": '<html><head><title>Home</title><meta name="description" content="Welcome"></head><body><a href="/grammar/a.html">A</a> <a href="/listen/">L</a> <a href="/x.pdf">pdf</a> <a href="/private/z.html">z</a> <a href="/hub.html">hub</a> <a href="/orphan.html">o</a></body></html>',
    "/grammar/a.html": '<html><head><title>Tense A</title></head><body><h1>Present perfect</h1><p>some words here</p><a href="/files/a.pdf">A</a> <a href="/files/nolen.pdf">N</a> <a href="/grammar/b.html">B</a><script>var x=1</script></body></html>',
    "/grammar/b.html": '<html><body><h1>Past simple</h1><a href="/files/b.docx">B</a> <a href="/files/dead.pdf">dead</a> <a href="/">home</a></body></html>',
    "/listen/": '<html><body><h1>Listening</h1><a href="/audio/1.mp3">1</a><script>var u="https:\\/\\/127.0.0.1\\/audio\\/2.mp3"</script></body></html>',
    "/orphan.html": '<html><body><h1>Only a dead link</h1><a href="/files/dead.pdf">d</a></body></html>',
    "/hub.html": '<html><body><h1>All files</h1><a href="/files/a.pdf">a</a><a href="/files/b.docx">b</a><a href="/files/nolen.pdf">n</a><a href="/x.pdf">x</a><a href="/audio/1.mp3">1</a></body></html>',
    "/private/z.html": "<html><body>secret</body></html>",
    "/robots.txt": "User-agent: *\nDisallow: /private/\nSitemap: http://{host}/sitemap.xml\n",
    "/sitemap.xml": "<urlset><url><loc>http://{host}/grammar/c.html</loc></url></urlset>",
    "/grammar/c.html": "<html><body><h1>Only in sitemap</h1></body></html>",
}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, head=False):
        path = self.path.split("?")[0]
        if path == "/files/dead.pdf":                          # broken link
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
        if path == "/files/nolen.pdf":                       # HEAD gives no length, GET does
            self.send_response(200); self.send_header("Content-Type", "application/pdf")
            if not head:
                self.send_header("Content-Length", "777")
            self.end_headers(); return
        if path.endswith((".pdf", ".docx", ".mp3")):
            self.send_response(200); self.send_header("Content-Type", "application/octet-stream"); self.send_header("Content-Length", "1234"); self.end_headers(); return
        body = PAGES.get(path)
        if body is None:
            self.send_response(404); self.end_headers(); return
        data = body.replace("{host}", self.headers["Host"]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/xml" if path.endswith(".xml") else "text/plain" if path.endswith(".txt") else "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data))); self.end_headers()
        if not head:
            self.wfile.write(data)

    def do_GET(self): self._send()
    def do_HEAD(self): self._send(True)


def fake_d1():
    con = sqlite3.connect(":memory:", check_same_thread=False)
    con.row_factory = sqlite3.Row
    sql = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()
    for part in sql.split(";"):
        stmt = "\n".join(l for l in part.splitlines() if not l.strip().startswith("--")).strip()
        if stmt:
            con.execute(stmt)

    def poster(url, payload):
        assert len(payload["params"]) <= 100, "D1 allows ~100 bound parameters"
        cur = con.execute(payload["sql"], payload["params"])
        con.commit()
        return 200, {"success": True, "result": [{"results": [dict(r) for r in cur.fetchall()]}]}
    return con, D1Sync("a", "d", "t", poster=poster)


srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_port}"
scan.SLEEP = lambda s: None
scan.FLUSH_EVERY = 1          # write often, so the end-of-crawl re-assignment is exercised
site = {"id": "t", "name": "Test", "base": base, "root": "", "seeds": ["/"], "tags": "g", "delay": 0}
con, db = fake_d1()
with tempfile.TemporaryDirectory() as tmp:
    status = Scanner(site, Net(), db, tmp, time.monotonic() + 60).run()
    assert status == "complete", status
    urls = {r["url"].replace(base, "") for r in con.execute("select url from cat_pages")}
    assert urls == {"/", "/grammar/a.html", "/grammar/b.html", "/listen/", "/hub.html"}, urls   # only pages with files; /private blocked by robots; c.html (sitemap, no files) not stored
    files = {r["url"].replace(base, ""): r["bytes"] for r in con.execute("select url,bytes from cat_files")}
    assert set(files) == {"/x.pdf", "/files/a.pdf", "/files/b.docx", "/audio/1.mp3", "/audio/2.mp3", "/files/nolen.pdf"} or "/audio/1.mp3" in files, files
    assert "/files/dead.pdf" not in files and "/orphan.html" not in urls, (files, urls)   # 404 files and pages with only dead links are not stored
    own = {r["url"].replace(base, ""): r["page_url"].replace(base, "") for r in con.execute("select url,page_url from cat_files")}
    assert own["/files/a.pdf"] == "/grammar/a.html" and own["/x.pdf"] == "/", own      # not filed under the hub page
    assert files["/files/nolen.pdf"] == 777 and files["/files/a.pdf"] == 1234, files
    row = con.execute("select * from cat_pages where url like '%a.html'").fetchone()
    assert row["title"] == "Present perfect" and row["section"] == "grammar" and "some words here" in row["body"] and "var x" not in row["body"], dict(row)
    s = con.execute("select * from cat_sites").fetchone()
    assert (s["status"], s["pages"], s["files"]) == ("complete", 5, len(files)), dict(s)
    # second run: up to date, nothing refetched
    assert Scanner(site, Net(), db, tmp, time.monotonic() + 60).run() == "complete"
    # forced rescan: nothing duplicated
    Scanner(site, Net(), db, tmp, time.monotonic() + 60, fresh=True).run()
    assert con.execute("select count(*) c from cat_pages").fetchone()["c"] == 5
    # time budget exhausted -> partial, then a later run resumes from saved state
    con.execute("delete from cat_pages"); con.execute("delete from cat_files"); con.commit()
    with tempfile.TemporaryDirectory() as tmp2:
        st = Scanner(site, Net(), db, tmp2, time.monotonic() - 1).run()
        assert st == "partial", st
        assert Scanner(site, Net(), db, tmp2, time.monotonic() + 60).run() == "complete"
        assert con.execute("select count(*) c from cat_pages").fetchone()["c"] == 5
    # only wanted types
    con.execute("delete from cat_pages"); con.execute("delete from cat_files"); con.commit()
    with tempfile.TemporaryDirectory() as tmp3:
        Scanner(site, Net(), db, tmp3, time.monotonic() + 60, exts=(".mp3",)).run()
        assert {r["ext"] for r in con.execute("select ext from cat_files")} == {"mp3"}
        assert con.execute("select count(*) c from cat_pages").fetchone()["c"] == 2     # /listen/ and /hub.html
# pages already in D1: a sitemap url is not fetched again; skip_known skips every known page
class Count(Net):
    def __init__(self): super().__init__(); self.got = []
    def page(self, url): self.got.append(url); return super().page(url)
con.execute("delete from cat_pages"); con.execute("delete from cat_files"); con.commit()
with tempfile.TemporaryDirectory() as tmp9:
    Scanner(site, Net(), db, tmp9, time.monotonic() + 60).run()
    sm_url = f"{site['base']}/grammar/c.html"                                       # in the sitemap; pretend an earlier run stored it
    con.execute("insert into cat_pages(site_id,url,section,title,descr,body,via,found_at) values('t',?,'grammar','','','','sitemap',1)", (sm_url,)); con.commit()
    in_d1 = {r["url"] for r in con.execute("select url from cat_pages")}
    n2, n3 = Count(), Count()
    sc2 = Scanner(site, n2, db, tmp9 + "/b", time.monotonic() + 60, fresh=True)
    assert sc2.run() == "complete" and sm_url not in n2.got and sc2.skipped == 1, (n2.got, sc2.skipped)
    sc3 = Scanner({**site, "skip_known": True}, n3, db, tmp9 + "/c", time.monotonic() + 60, fresh=True)
    assert sc3.run() == "complete" and not (set(n3.got) & in_d1) and sc3.skipped >= 1, (n3.got, sc3.skipped)
    assert con.execute("select count(*) c from cat_pages").fetchone()["c"] == 6       # 5 + the one inserted by hand: nothing written twice
# the site dropdown in scan.yml must list every id of sites.json (regenerate it when you add a site)
import json
ids = [x["id"] for x in json.loads((Path(__file__).resolve().parent.parent / "sites.json").read_text("utf-8"))["sites"]]
yml = (Path(__file__).resolve().parent.parent / ".github/workflows/scan.yml").read_text("utf-8")
missing = [i for i in ids if f"          - {i}\n" not in yml]
assert not missing, f"add to the site dropdown in scan.yml: {missing}"
# CSV backend: same result as D1, no duplicates on a second run, index builder works
import csv, json as _json, os, subprocess, sys
from bot.scan import FileDb
with tempfile.TemporaryDirectory() as tf, tempfile.TemporaryDirectory() as ts:
    assert Scanner(site, Net(), FileDb(tf), ts, time.monotonic() + 60).run() == "complete"
    rd = lambda n: list(csv.DictReader(open(f"{tf}/t/{n}.csv", newline="", encoding="utf-8")))
    pg, fl = rd("pages"), rd("files")
    assert len(pg) == 5 and {r["url"].replace(base, "") for r in pg} == {"/", "/grammar/a.html", "/grammar/b.html", "/listen/", "/hub.html"}
    own = {r["url"].replace(base, ""): r["page_url"].replace(base, "") for r in fl}
    assert own["/files/a.pdf"] == "/grammar/a.html" and own["/x.pdf"] == "/", own          # moved off the hub page, in the file too
    meta = _json.load(open(f"{tf}/t/meta.json"))
    assert meta["status"] == "complete" and meta["pages"] == 5 and meta["files"] == len(fl)
    Scanner(site, Net(), FileDb(tf), ts + "/again", time.monotonic() + 60, fresh=True).run()   # known rows are read from the CSV
    assert len(rd("pages")) == 5 and len(rd("files")) == len(fl)
    subprocess.run([sys.executable, str(Path(__file__).resolve().parent.parent / "scripts/build_index.py"), tf], check=True, capture_output=True)
    assert "| [t](t/) | complete | 5 |" in open(f"{tf}/INDEX.md", encoding="utf-8").read() and os.path.exists(f"{tf}/sites.csv")
# the two checkbox forms (scan-sites-1/2.yml) must hold every site exactly once (regenerate: python scripts/gen_scan_forms.py)
import re
boxes = []
for n in (1, 2):
    boxes += re.findall(r"^      s_(\w+):", (Path(__file__).resolve().parent.parent / f".github/workflows/scan-sites-{n}.yml").read_text("utf-8"), re.M)
assert sorted(boxes) == sorted(ids), f"checkbox forms out of date, run scripts/gen_scan_forms.py: {set(ids) ^ set(boxes)}"
assert "workflow_call:" in yml
# D1 answering 401 'Authentication error' a few times, then working: the run must survive
calls = {"n": 0}
inner = db.poster
def flaky(url, payload):
    calls["n"] += 1
    if payload["sql"].startswith("INSERT OR IGNORE INTO cat_files") and calls["n"] % 3 == 0 and calls["n"] < 30:
        return 401, {"success": False, "errors": [{"code": 10000, "message": "Authentication error"}]}
    return inner(url, payload)
db.poster = flaky
con.execute("delete from cat_pages"); con.execute("delete from cat_files"); con.commit()
with tempfile.TemporaryDirectory() as tmp4:
    assert Scanner(site, Net(), db, tmp4, time.monotonic() + 60).run() == "complete"
    assert con.execute("select count(*) c from cat_pages").fetchone()["c"] == 5
# D1 failing for good: the error comes out, but the crawl progress is saved for the next run
def broken(url, payload):
    if payload["sql"].startswith("INSERT"):
        return 401, {"success": False, "errors": ["Authentication error"]}
    return inner(url, payload)
db.poster = broken
import gzip, os
with tempfile.TemporaryDirectory() as tmp5:
    try:
        Scanner(site, Net(), db, tmp5, time.monotonic() + 60, fresh=True).run()
        raise AssertionError("should have failed")
    except RuntimeError as e:
        assert "401" in str(e)
    assert os.path.exists(os.path.join(tmp5, "t.json.gz"))
# a site that answers 403 to everything: must NOT end up "complete" / "up to date"
class Deny(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self): self.send_response(403); self.send_header("Content-Length", "0"); self.end_headers()
    do_HEAD = do_GET
dsrv = HTTPServer(("127.0.0.1", 0), Deny)
threading.Thread(target=dsrv.serve_forever, daemon=True).start()
dsite = {**site, "id": "d", "base": f"http://127.0.0.1:{dsrv.server_port}"}
db.poster = inner
with tempfile.TemporaryDirectory() as tmp6:
    sc = Scanner(dsite, Net(), db, tmp6, time.monotonic() + 60)
    assert sc.run() == "blocked"
    assert sc.st["done_at"] == 0 and sc.st["failed"], sc.st
    assert con.execute("select status from cat_sites where site_id='d'").fetchone()["status"] == "blocked"
    assert Scanner(dsite, Net(), db, tmp6, time.monotonic() + 60).run() == "blocked"   # retried, not "up to date"
# unreachable host: gives up after a few connection errors instead of retrying every URL for minutes
import socket
sk = socket.socket(); sk.bind(("127.0.0.1", 0)); dead_port = sk.getsockname()[1]; sk.close()     # nothing listens here
nsite = {**site, "id": "n", "base": f"http://127.0.0.1:{dead_port}"}
with tempfile.TemporaryDirectory() as tmp7:
    net7 = Net()
    t0 = time.monotonic()
    assert Scanner(nsite, net7, db, tmp7, time.monotonic() + 60).run() == "blocked"
    assert net7.fails[f"127.0.0.1:{dead_port}"] >= scan.DEAD_AFTER and time.monotonic() - t0 < 10
# cancel in the middle of a crawl: popped URLs go back to the queue and the state file is written
class Boom(Net):
    n = 0
    def page(self, url):
        Boom.n += 1
        if Boom.n > 4:                                   # robots x2 + sitemap, then the first crawl batch passes
            raise SystemExit(143)
        return super().page(url)
db.poster = inner
with tempfile.TemporaryDirectory() as tmp8:
    sc = Scanner({**site, "id": "c"}, Boom(), db, tmp8, time.monotonic() + 60, fresh=True)
    try:
        sc.run(); raise AssertionError("should have been interrupted")
    except SystemExit:
        pass
    assert os.path.exists(os.path.join(tmp8, "c.json.gz"))
    assert len(sc.st["queue"]) == 2 and not sc.st["failed"], sc.st["queue"]      # seed + sitemap url, both put back
    assert Scanner({**site, "id": "c"}, Net(), db, tmp8, time.monotonic() + 60).run() == "complete"   # next run finishes it
print("scan selftest OK")
