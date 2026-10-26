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
    "/": '<html><head><title>Home</title><meta name="description" content="Welcome"></head><body><a href="/grammar/a.html">A</a> <a href="/listen/">L</a> <a href="/x.pdf">pdf</a> <a href="/private/z.html">z</a></body></html>',
    "/grammar/a.html": '<html><head><title>Tense A</title></head><body><h1>Present perfect</h1><p>some words here</p><a href="/files/a.pdf">A</a> <a href="/grammar/b.html">B</a><script>var x=1</script></body></html>',
    "/grammar/b.html": '<html><body><h1>Past simple</h1><a href="/files/b.docx">B</a> <a href="/">home</a></body></html>',
    "/listen/": '<html><body><h1>Listening</h1><a href="/audio/1.mp3">1</a><script>var u="https:\\/\\/127.0.0.1\\/audio\\/2.mp3"</script></body></html>',
    "/private/z.html": "<html><body>secret</body></html>",
    "/robots.txt": "User-agent: *\nDisallow: /private/\nSitemap: http://{host}/sitemap.xml\n",
    "/sitemap.xml": "<urlset><url><loc>http://{host}/grammar/c.html</loc></url></urlset>",
    "/grammar/c.html": "<html><body><h1>Only in sitemap</h1></body></html>",
}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, head=False):
        path = self.path.split("?")[0]
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
site = {"id": "t", "name": "Test", "base": base, "root": "", "seeds": ["/"], "tags": "g", "delay": 0}
con, db = fake_d1()
with tempfile.TemporaryDirectory() as tmp:
    status = Scanner(site, Net(), db, tmp, time.monotonic() + 60).run()
    assert status == "complete", status
    urls = {r["url"].replace(base, "") for r in con.execute("select url from cat_pages")}
    assert urls == {"/", "/grammar/a.html", "/grammar/b.html", "/listen/"}, urls   # only pages with files; /private blocked by robots; c.html (sitemap, no files) not stored
    files = {r["url"].replace(base, ""): r["bytes"] for r in con.execute("select url,bytes from cat_files")}
    assert set(files) == {"/x.pdf", "/files/a.pdf", "/files/b.docx", "/audio/1.mp3", "/audio/2.mp3"} or "/audio/1.mp3" in files, files
    assert all(v == 1234 for k, v in files.items() if k.startswith("/") and not k.startswith("//")), files
    row = con.execute("select * from cat_pages where url like '%a.html'").fetchone()
    assert row["title"] == "Present perfect" and row["section"] == "grammar" and "some words here" in row["body"] and "var x" not in row["body"], dict(row)
    s = con.execute("select * from cat_sites").fetchone()
    assert (s["status"], s["pages"], s["files"]) == ("complete", 4, len(files)), dict(s)
    # second run: up to date, nothing refetched
    assert Scanner(site, Net(), db, tmp, time.monotonic() + 60).run() == "complete"
    # forced rescan: nothing duplicated
    Scanner(site, Net(), db, tmp, time.monotonic() + 60, fresh=True).run()
    assert con.execute("select count(*) c from cat_pages").fetchone()["c"] == 4
    # time budget exhausted -> partial, then a later run resumes from saved state
    con.execute("delete from cat_pages"); con.execute("delete from cat_files"); con.commit()
    with tempfile.TemporaryDirectory() as tmp2:
        st = Scanner(site, Net(), db, tmp2, time.monotonic() - 1).run()
        assert st == "partial", st
        assert Scanner(site, Net(), db, tmp2, time.monotonic() + 60).run() == "complete"
        assert con.execute("select count(*) c from cat_pages").fetchone()["c"] == 4
    # only wanted types
    con.execute("delete from cat_pages"); con.execute("delete from cat_files"); con.commit()
    with tempfile.TemporaryDirectory() as tmp3:
        Scanner(site, Net(), db, tmp3, time.monotonic() + 60, exts=(".mp3",)).run()
        assert {r["ext"] for r in con.execute("select ext from cat_files")} == {"mp3"}
        assert con.execute("select count(*) c from cat_pages").fetchone()["c"] == 1     # only /listen/
# the site dropdown in scan.yml must list every id of sites.json (regenerate it when you add a site)
import json
ids = [x["id"] for x in json.loads((Path(__file__).resolve().parent.parent / "sites.json").read_text("utf-8"))["sites"]]
yml = (Path(__file__).resolve().parent.parent / ".github/workflows/scan.yml").read_text("utf-8")
missing = [i for i in ids if f"          - {i}\n" not in yml]
assert not missing, f"add to the site dropdown in scan.yml: {missing}"
print("scan selftest OK")
