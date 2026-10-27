# Only pages that carry at least one wanted file (pdf, audio ...) are stored (CSV files in catalog/, or Cloudflare D1).
# bot/scan.py - non-interactive site scanner (port of learning_sites_scanner.py): finds pages + downloadable files of
# learning sites and writes the catalog to Cloudflare D1 (tables cat_sites / cat_pages / cat_files). Nothing is downloaded.
from __future__ import annotations

import csv
import gzip
import html as htmllib
import json
import os
import re
import sys
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote, unquote, urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from requests.adapters import HTTPAdapter

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0",
      "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      "Accept-Language": "en-US,en;q=0.5"}
CHROME = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
          "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
          "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip, deflate", "Upgrade-Insecure-Requests": "1",
          "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
          "Sec-Ch-Ua": '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"', "Sec-Ch-Ua-Mobile": "?0",
          "Sec-Ch-Ua-Platform": '"Windows"'}
BLOCKED = (401, 403, 451)                                # "go away" answers: a failure, not a missing page
CONNECT_TIMEOUT = 10                                     # seconds to wait for a connection (read timeout stays per call)
DEAD_AFTER = 4                                           # connection failures in a row (and no success) = host unreachable
BLOCK_STOP = 10                                          # give up on a site that only ever answers BLOCKED
EXTS = (".pdf", ".mp3", ".m4a", ".wav", ".ogg", ".aac", ".mp4", ".webm", ".mov", ".doc", ".docx", ".rtf", ".txt",
        ".epub", ".ppt", ".pptx", ".xls", ".xlsx", ".zip", ".srt", ".vtt")
PAGE_EXTS = (".html", ".shtml", ".htm", ".php")
EXT_ALT = "|".join(e[1:] for e in EXTS)
HREF_RE = re.compile(r"""(?:href|src)\s*=\s*["']([^"'\s]+)["']""", re.I)
FILE_RE = re.compile(r"https?:(?:\\?/){2}[^\s\"'<>()]+?\.(?:%s)(?![A-Za-z0-9])" % EXT_ALT, re.I)
EP_RE = re.compile(r"^(?P<prefix>.*/)6-minute-english_(?P<y>\d{4})/ep-(?P<id>\d{6})/?$")
MAX_PAGES = int(os.environ.get("MAX_PAGES", "20000"))   # safety limit per site
MAX_SITEMAPS = 100
FIRST_YEAR = 2007
FLUSH_EVERY = 100                                       # pages between D1 writes
BODY_CHARS = 1200                                       # text excerpt kept per page (for /search)
SLEEP = time.sleep


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- CSV files (no limits, lives in git)
class FileDb:
    """Same interface as D1Sync, but keeps the catalog in plain files: <root>/<site>/pages.csv, files.csv and meta.json.
    Columns are the same as the D1 tables cat_pages / cat_files, so the files can be imported into D1 later."""
    PAGE_COLS = ("site_id", "url", "section", "title", "descr", "body", "via", "found_at")
    FILE_COLS = ("site_id", "url", "page_url", "section", "ext", "name", "bytes", "found_at")

    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.pages: dict[str, dict] = {}
        self.files: dict[str, dict] = {}
        self.dirty: set[str] = set()                 # sites whose files.csv must be rewritten (a file moved to another page)

    def _load(self, site: str) -> None:
        if site in self.pages:
            return
        self.pages[site], self.files[site] = {}, {}
        for name, cols, store in (("pages.csv", self.PAGE_COLS, self.pages[site]), ("files.csv", self.FILE_COLS, self.files[site])):
            path = self.root / site / name
            if path.exists():
                with open(path, newline="", encoding="utf-8") as f:
                    for r in csv.DictReader(f):
                        store[r["url"]] = tuple(r[c] for c in cols)

    def _append(self, site: str, name: str, cols: tuple, rows: list) -> None:
        if not rows:
            return
        path = self.root / site / name
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(cols)
            w.writerows(rows)

    def known(self, site: str) -> tuple[set, set]:
        self._load(site)
        return set(self.pages[site]), set(self.files[site])

    def put_pages(self, rows: list[tuple]) -> None:
        if rows:
            site = rows[0][0]
            self._load(site)
            new = [r for r in rows if r[1] not in self.pages[site]]      # like INSERT OR IGNORE
            self.pages[site].update({r[1]: r for r in new})
            self._append(site, "pages.csv", self.PAGE_COLS, new)

    def put_files(self, rows: list[tuple]) -> None:
        if rows:
            site = rows[0][0]
            self._load(site)
            new = [r for r in rows if r[1] not in self.files[site]]
            self.files[site].update({r[1]: r for r in new})
            self._append(site, "files.csv", self.FILE_COLS, new)

    def move_file(self, site: str, url: str, page_url: str, section: str) -> None:
        self._load(site)
        if url in self.files[site]:
            r = list(self.files[site][url])
            r[2], r[3] = page_url, section
            self.files[site][url] = tuple(r)
            self.dirty.add(site)

    def site_row(self, site: dict, status: str) -> None:
        sid = site["id"]
        self._load(sid)
        d = self.root / sid
        d.mkdir(parents=True, exist_ok=True)
        if sid in self.dirty:                        # rewrite files.csv with the moved files, atomically
            tmp = d / "files.csv.tmp"
            with open(tmp, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(self.FILE_COLS)
                w.writerows(self.files[sid].values())
            os.replace(tmp, d / "files.csv")
            self.dirty.discard(sid)
        meta = {"site_id": sid, "name": site["name"], "base": site["base"], "tags": site.get("tags", ""), "status": status,
                "last_scan": int(time.time()), "pages": len(self.pages[sid]), "files": len(self.files[sid])}
        (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    def finish(self) -> None:
        pass


# ---------------------------------------------------------------- D1 (sync REST)
class D1Sync:
    """Cloudflare D1 REST. `poster(url, payload) -> (status, json)` can be replaced in tests."""
    PACE = 0.15                                  # pause after each request (be gentle with the Cloudflare API)
    CHUNK_PAGES, CHUNK_FILES = 12, 12            # D1: max ~100 bound parameters per statement

    def __init__(self, account: str, db: str, token: str, poster=None) -> None:
        self.url = f"https://api.cloudflare.com/client/v4/accounts/{account}/d1/database/{db}/query"
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"
        self.poster = poster

    def q(self, sql: str, params: list | None = None) -> list[dict]:
        payload = {"sql": sql, "params": params or []}
        err = ""
        for attempt in range(6):
            if self.poster:
                status, data = self.poster(self.url, payload)
            else:
                try:
                    r = self.s.post(self.url, json=payload, timeout=60)
                    status, data = r.status_code, r.json()
                except (requests.RequestException, ValueError) as exc:
                    status, data = 0, {"errors": [str(exc)]}
            if status == 200 and data.get("success"):
                SLEEP(self.PACE)
                return data["result"][0].get("results", [])
            err = f"D1 error {status}: {data.get('errors')}"
            if status in (401, 403):                 # Cloudflare sometimes answers 'Authentication error' for a moment under load: wait longer
                if attempt < 5:
                    log(f"  {err} - waiting, retry {attempt + 1}/5")
                    SLEEP(10 * (attempt + 1))
                continue
            if status in (0, 429, 500, 502, 503):
                SLEEP(2 * (attempt + 1))
                continue
            break
        raise RuntimeError(err)

    def known(self, site: str) -> tuple[set, set]:
        pages = {r["url"] for r in self.q("SELECT url FROM cat_pages WHERE site_id=?", [site])}
        files = {r["url"] for r in self.q("SELECT url FROM cat_files WHERE site_id=?", [site])}
        return pages, files

    def put_pages(self, rows: list[tuple]) -> None:
        for i in range(0, len(rows), self.CHUNK_PAGES):
            part = rows[i:i + self.CHUNK_PAGES]
            self.q("INSERT OR IGNORE INTO cat_pages(site_id,url,section,title,descr,body,via,found_at) VALUES "
                   + ",".join(["(?,?,?,?,?,?,?,?)"] * len(part)), [x for r in part for x in r])

    def put_files(self, rows: list[tuple]) -> None:
        for i in range(0, len(rows), self.CHUNK_FILES):
            part = rows[i:i + self.CHUNK_FILES]
            self.q("INSERT OR IGNORE INTO cat_files(site_id,url,page_url,section,ext,name,bytes,found_at) VALUES "
                   + ",".join(["(?,?,?,?,?,?,?,?)"] * len(part)), [x for r in part for x in r])

    def move_file(self, site: str, url: str, page_url: str, section: str) -> None:
        self.q("UPDATE cat_files SET page_url=?, section=? WHERE site_id=? AND url=?", [page_url, section, site, url])

    def finish(self) -> None:
        pass

    def site_row(self, site: dict, status: str) -> None:
        self.q("INSERT INTO cat_sites(site_id,name,base,tags,status,last_scan,pages,files) VALUES (?,?,?,?,?,?,"
               "(SELECT COUNT(*) FROM cat_pages WHERE site_id=?),(SELECT COUNT(*) FROM cat_files WHERE site_id=?)) "
               "ON CONFLICT(site_id) DO UPDATE SET name=excluded.name, base=excluded.base, tags=excluded.tags, "
               "status=excluded.status, last_scan=excluded.last_scan, pages=excluded.pages, files=excluded.files",
               [site["id"], site["name"], site["base"], site.get("tags", ""), status, int(time.time()), site["id"], site["id"]])


# ---------------------------------------------------------------- network
class Net:
    def __init__(self) -> None:
        self.s = requests.Session()
        self.s.headers.update(UA)
        ad = HTTPAdapter(pool_connections=16, pool_maxsize=32)
        self.s.mount("https://", ad)
        self.s.mount("http://", ad)
        self.fails: Counter = Counter()                  # host -> connection failures in a row
        self.codes: Counter = Counter()                  # HTTP status -> how many page requests got it (0 = no answer)
        self.mode: dict[str, str] = {}                   # host -> "chrome" | "impersonate": what got past its bot check last time
        self.chrome_s = None

    def _second_chance(self, host: str, url: str, timeout: int):
        """A 403 on a page: ask again like a real Chrome (full header set, cookies from the home page first), then, if the
        optional curl_cffi package is installed, with Chrome's TLS fingerprint (many bot walls look at that, not at headers).
        Returns a response that is not 403, or None. The mode that worked is remembered per host."""
        home = urlparse(url)._replace(path="/", query="").geturl()
        if self.chrome_s is None:
            self.chrome_s = requests.Session()
            self.chrome_s.headers.update(CHROME)
        try:
            self.chrome_s.get(home, timeout=(CONNECT_TIMEOUT, timeout))      # cookies / challenge tokens
            r = self.chrome_s.get(url, timeout=(CONNECT_TIMEOUT, timeout), headers={"Referer": home, "Sec-Fetch-Site": "same-origin"})
            if r.status_code != 403:
                self.mode[host] = "chrome"
                return r
            r.close()
        except requests.RequestException:
            pass
        try:
            from curl_cffi import requests as cr          # optional: pip install curl_cffi
        except Exception:  # noqa: BLE001
            return None
        try:
            r = cr.get(url, timeout=timeout, impersonate="chrome", headers={"Referer": home}, allow_redirects=True)
        except Exception:  # noqa: BLE001
            return None
        if r.status_code != 403:
            self.mode[host] = "impersonate"
            return r
        return None

    def _remembered(self, host: str, url: str, timeout: int):
        """Hosts that already needed the Chrome look keep getting it, without paying for a failed first try."""
        m = self.mode.get(host)
        try:
            if m == "chrome" and self.chrome_s is not None:
                return self.chrome_s.get(url, timeout=(CONNECT_TIMEOUT, timeout))
            if m == "impersonate":
                from curl_cffi import requests as cr
                return cr.get(url, timeout=timeout, impersonate="chrome", allow_redirects=True)
        except Exception:  # noqa: BLE001
            self.mode.pop(host, None)
        return None

    def request(self, method: str, url: str, timeout: int = 30, retries: int = 4, headers: dict | None = None, stream: bool = False):
        """Response, or None if every attempt failed. Waits and retries on 'slow down'."""
        host = urlparse(url).netloc.lower()
        if method == "GET" and not stream and host in self.mode:
            r = self._remembered(host, url, timeout)
            if r is not None and r.status_code != 403:
                return r
        for attempt in range(retries):
            if self.fails[host] >= DEAD_AFTER:           # host never answers (IP blocked / down): stop wasting minutes
                return None
            try:
                r = self.s.request(method, url, timeout=(min(CONNECT_TIMEOUT, timeout), timeout), allow_redirects=True,
                                   headers=headers, stream=stream)
            except requests.RequestException:
                self.fails[host] += 1
                SLEEP(1.5 * (attempt + 1))
                continue
            self.fails[host] = 0
            if r.status_code == 403 and attempt == 0 and method == "GET" and not stream and host not in self.mode:
                r.close()
                alt = self._second_chance(host, url, timeout)
                if alt is not None:
                    return alt
                SLEEP(1)
                continue
            if r.status_code == 403 and attempt < 2 and method == "GET":
                r.close()                                # often a one-off bot check: retry with a Referer, slowly
                headers = {**(headers or {}), "Referer": urlparse(url)._replace(path="/", query="").geturl()}
                SLEEP(2 * (attempt + 1))
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                ra = str(r.headers.get("Retry-After", ""))
                wait = int(ra) if ra.isdigit() else 2 * (attempt + 1)
                r.close()
                SLEEP(min(wait, 30))
                continue
            return r
        return None

    def page(self, url: str):
        """HTML text; '' if missing / not HTML; None if the request failed."""
        r = self.request("GET", url)
        if r is None:
            self.codes[0] += 1
            return None
        self.codes[r.status_code] += 1
        if r.status_code in BLOCKED:
            r.close()
            return None                                  # blocked: report it as failed instead of "page missing"
        if r.status_code != 200:
            return ""
        ct = r.headers.get("Content-Type", "").lower()
        if ct and not any(x in ct for x in ("html", "xml", "text/plain")):
            return ""
        r.encoding = r.encoding if (r.encoding and "charset" in ct) else "utf-8"
        return r.text

    def text(self, url: str) -> str:
        return self.page(url) or ""

    @staticmethod
    def _len(r) -> int:
        try:
            return int(r.headers.get("Content-Length") or 0) if r is not None and r.status_code == 200 else 0
        except ValueError:
            return 0

    def size(self, url: str) -> int:
        """File size in bytes; 0 = server did not say; -1 = the file does not exist (404/410).
        HEAD first (no compression, so the server can state the length); if that gives nothing, GET and read only the headers."""
        h = {"Accept-Encoding": "identity"}
        r = self.request("HEAD", url, timeout=15, retries=2, headers=h)
        n = self._len(r)
        if n:
            return n
        g = self.request("GET", url, timeout=20, retries=1, headers=h, stream=True)
        n = self._len(g)
        dead = g is not None and g.status_code in (404, 410)
        if g is not None:
            g.close()
        return n if n or not dead else -1

    def exists(self, url: str):
        r = self.request("HEAD", url, timeout=15, retries=2)
        if r is None:
            return None
        if r.status_code == 200:
            return True
        if r.status_code in (404, 410):
            return False
        r = self.request("GET", url, timeout=15, retries=2)
        return None if r is None else r.status_code == 200


# ---------------------------------------------------------------- html helpers
def file_ext(url: str) -> str:
    return os.path.splitext(urlparse(url).path)[1].lower()


def extract(html: str, base: str) -> tuple[set, list]:
    links = {urljoin(base, htmllib.unescape(h)).split("#")[0] for h in HREF_RE.findall(html)}
    files = {u for u in links if file_ext(u) in EXTS}
    for m in FILE_RE.findall(html):
        files.add(m.replace("\\/", "/"))
    return links, sorted(files)


def page_meta(html: str) -> tuple[str, str]:
    def text(rx):
        m = re.search(rx, html, re.S | re.I)
        return htmllib.unescape(re.sub(r"<[^>]+>", " ", m.group(1))).strip() if m else ""

    title = re.sub(r"\s+", " ", text(r"<h1[^>]*>(.*?)</h1>") or text(r"<title[^>]*>(.*?)</title>"))
    desc = ""
    for tag in re.findall(r"<meta[^>]+>", html, re.I):
        if re.search(r'(?:name|property)=["\'](?:og:)?description["\']', tag, re.I):
            m = re.search(r'content=["\']([^"\']*)["\']', tag, re.I)
            if m:
                desc = htmllib.unescape(m.group(1)).strip()
                break
    return title, desc


def body_excerpt(html: str, n: int = BODY_CHARS) -> str:
    t = re.sub(r"(?is)<(script|style|nav|header|footer|noscript|svg|form)\b.*?</\1>", " ", html)
    t = re.sub(r"(?s)<!--.*?-->", " ", t)
    t = htmllib.unescape(re.sub(r"<[^>]+>", " ", t))
    return re.sub(r"\s+", " ", t).strip()[:n]


# ---------------------------------------------------------------- scanner
class Scanner:
    def __init__(self, site: dict, net: Net, db, state_dir: str, deadline: float, measure: bool = True,
                 rescan_days: int = 7, fresh: bool = False, exts: tuple = EXTS) -> None:
        self.site, self.net, self.db = site, net, db
        self.id = site["id"]
        self.base = site["base"].rstrip("/")
        self.scheme = urlparse(self.base).scheme
        self.netloc = urlparse(self.base).netloc
        self.root_path = site.get("root", "").rstrip("/")
        self.root = self.base + self.root_path
        host = self.netloc.lower()
        bare = host[4:] if host.startswith("www.") else host
        self.hosts = {bare, "www." + bare}
        self.workers = site.get("workers", 6)
        self.delay = site.get("delay", 0.5)
        self.deadline, self.measure, self.rescan_days, self.fresh = deadline, measure, rescan_days, fresh
        self.exts = tuple(exts)
        self.state_path = os.path.join(state_dir, f"{self.id}.json.gz")
        self.robots = None
        self.max_pages = site.get("max_pages", MAX_PAGES)
        self.skip_known = bool(site.get("skip_known"))   # true: never re-fetch a page that is already in D1 (links in it are not followed)
        self.skipped = 0                                 # pages not fetched because D1 already has them
        self.st: dict = {}
        self.refused = False
        self.known_pages: set = set()
        self.known_files: set = set()

    # ---- state
    def new_state(self) -> dict:
        seeds = self.seeds()
        return {"pages": {}, "seen": set(seeds), "queue": deque(seeds), "failed": [], "via": {}, "sizes": {}, "done_at": 0, "owner": {}}

    def load_state(self):
        try:
            with gzip.open(self.state_path, "rt", encoding="utf-8") as f:
                s = json.load(f)
            s["seen"], s["queue"] = set(s["seen"]), deque(s["queue"])
            for k, v in (("failed", []), ("via", {}), ("sizes", {}), ("done_at", 0), ("owner", {})):
                s.setdefault(k, v)
            return s
        except Exception:  # noqa: BLE001
            return None

    def save_state(self) -> None:
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        s = self.st
        tmp = self.state_path + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump({"pages": s["pages"], "seen": sorted(s["seen"]), "queue": list(s["queue"]), "failed": s["failed"],
                       "via": s["via"], "sizes": s["sizes"], "done_at": s["done_at"], "owner": s["owner"]}, f)
        os.replace(tmp, self.state_path)

    # ---- url rules
    def seeds(self) -> list[str]:
        if self.site.get("ia"):
            return []
        if self.site.get("probe"):                       # BBC
            index = self.root + "/features/6-minute-english"
            return [self.root, self.root + "/features", self.root + "/course", index] + \
                   [f"{index}_{y}" for y in range(FIRST_YEAR, date.today().year + 1)]
        return [self.base + s for s in self.site.get("seeds", ["/"])]

    def load_robots(self) -> None:
        self.robots = None
        txt = self.net.text(self.base + "/robots.txt")
        if txt.strip() and "<html" not in txt[:300].lower():
            rp = RobotFileParser()
            rp.parse(txt.splitlines())
            self.robots = rp

    def allowed(self, url: str) -> bool:
        return self.robots is None or self.robots.can_fetch("*", url)

    def clean_page(self, u: str):
        u, _ = urldefrag(u)
        p = urlparse(u)
        if p.netloc.lower() not in self.hosts:
            return None
        if self.root_path and not (p.path == self.root_path or p.path.startswith(self.root_path + "/")):
            return None
        only = self.site.get("only")
        dpath = unquote(p.path)
        if only and not any(dpath == o.rstrip("/") or dpath.startswith(o.rstrip("/") + "/") for o in only):
            return None
        ext = os.path.splitext(p.path)[1].lower()
        if ext and ext not in PAGE_EXTS:
            return None
        q = p.query if re.fullmatch(r"(page|p)=\d+", p.query or "") else ""
        u = p._replace(scheme=self.scheme, netloc=self.netloc, query=q, fragment="").geturl()
        return u if self.allowed(u) else None

    def section_of(self, url: str) -> str:
        rel = urlparse(url).path[len(self.root_path):].strip("/")
        if not rel:
            return "(home)"
        first = unquote(rel.split("/")[0])
        return "(main)" if "." in first else first

    # ---- step 1a: sitemap
    def add_sitemap(self) -> None:
        robots = self.net.text(self.base + "/robots.txt")
        maps = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots) + [self.base + "/sitemap.xml", self.root + "/sitemap.xml"]
        hint = self.site.get("sitemap_hint", "")
        queue, seen_maps, added, n = deque(maps), set(), 0, 0
        while queue and n < MAX_SITEMAPS:
            sm = queue.popleft()
            if sm in seen_maps or sm.endswith(".gz"):
                continue
            seen_maps.add(sm)
            n += 1
            for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", self.net.text(sm)):
                loc = htmllib.unescape(loc)
                if loc.lower().endswith(".xml"):
                    if urlparse(loc).netloc.lower() in self.hosts and hint in loc.lower():
                        queue.append(loc)
                    continue
                u = self.clean_page(loc)
                if u and u not in self.st["seen"]:
                    self.st["seen"].add(u)
                    self.st["queue"].append(u)
                    self.st["via"][u] = "sitemap"
                    added += 1
        log(f"[{self.id}] sitemap: {added} pages added")

    # ---- step 1b: follow links
    def out_of_time(self) -> bool:
        return time.monotonic() > self.deadline

    def crawl(self) -> None:
        st = self.st
        pages, seen, queue = st["pages"], st["seen"], st["queue"]
        last_log = 0
        with ThreadPoolExecutor(self.workers) as ex:
            while queue and len(pages) < self.max_pages and not self.out_of_time():
                batch = [queue.popleft() for _ in range(min(self.workers * 3, len(queue)))]
                done = {u for u in batch if u in self.known_pages and (self.skip_known or st["via"].get(u) == "sitemap")}
                if done:                                 # already in D1: a sitemap url needs no link discovery, skip_known sites opt in
                    self.skipped += len(done)
                    batch = [u for u in batch if u not in done]
                    if not batch:
                        continue
                try:
                    htmls = list(ex.map(self.net.page, batch))
                except BaseException:                    # cancelled / SIGTERM: put the batch back so the next run still crawls it
                    queue.extendleft(reversed(batch))
                    raise
                if not pages and sum(self.net.codes[c] for c in BLOCKED) + self.net.codes[0] >= BLOCK_STOP:
                    st["failed"].extend(u for u, h in zip(batch, htmls) if h is None)
                    self.refused = True
                    log(f"[{self.id}] site refuses us (HTTP {dict(self.net.codes)}) - stopping early, nothing fetched")
                    break
                for url, html in zip(batch, htmls):
                    if html is None:
                        st["failed"].append(url)
                        continue
                    if not html:
                        continue
                    links, files = extract(html, url)
                    files = [f for f in files if file_ext(f) in self.exts]
                    title, desc = page_meta(html)
                    pages[url] = {"t": title[:200], "d": desc[:400], "b": body_excerpt(html), "f": files}
                    for lk in links:
                        u = self.clean_page(lk)
                        if u and u not in seen:
                            seen.add(u)
                            queue.append(u)
                if len(pages) - last_log >= 200:
                    last_log = len(pages)
                    log(f"[{self.id}] {len(pages)} pages · {len(queue)} queued")
                if sum(1 for p in pages.values() if not p.get("ok")) >= FLUSH_EVERY:
                    self.flush()
                    self.save_state()
                SLEEP(self.delay)

    # ---- step 1c: BBC date probe for 6 Minute English
    def probe_dates(self) -> int:
        st = self.st
        known = [m for m in (EP_RE.match(u) for u in st["pages"]) if m]
        if len(known) < 5:
            return 0
        prefix = known[0].group("prefix")
        wd = Counter()
        for m in known:
            s = m.group("id")
            try:
                wd[date(2000 + int(s[:2]), int(s[2:4]), int(s[4:])).weekday()] += 1
            except ValueError:
                pass
        total = sum(wd.values())
        days = {d for d, c in wd.items() if total and c / total >= 0.08}
        cands, d, end = [], date(FIRST_YEAR, 1, 1), date.today()
        while d <= end:
            if d.weekday() in days:
                u = f"{prefix}6-minute-english_{d.year}/ep-{d:%y%m%d}"
                if u not in st["seen"]:
                    cands.append(u)
            d += timedelta(days=1)
        hits = 0
        log(f"[{self.id}] date probe: {len(cands)} dates")
        with ThreadPoolExecutor(self.workers) as ex:
            for i in range(0, len(cands), 40):
                if self.out_of_time():
                    break
                chunk = cands[i:i + 40]
                for u, ok in zip(chunk, ex.map(self.net.exists, chunk)):
                    st["seen"].add(u)
                    if ok:
                        st["queue"].append(u)
                        st["via"][u] = "probe"
                        hits += 1
                SLEEP(self.delay)
        log(f"[{self.id}] date probe: {hits} new episodes")
        return hits

    # ---- Internet Archive
    def ia_fill(self) -> None:
        q, limit, got, page = self.site["q"], self.site["limit"], 0, 1
        while got < limit:
            rows = min(100, limit - got)
            url = ("https://archive.org/advancedsearch.php?q=" + quote(q) + "&fl%5B%5D=identifier&sort%5B%5D=downloads+desc"
                   + f"&rows={rows}&page={page}&output=json")
            try:
                docs = json.loads(self.net.text(url))["response"]["docs"]
            except (ValueError, KeyError, TypeError):
                break
            if not docs:
                break
            for d in docs:
                u = "https://archive.org/details/" + str(d.get("identifier", ""))
                if d.get("identifier") and u not in self.st["seen"]:
                    self.st["seen"].add(u)
                    self.st["queue"].append(u)
            got += len(docs)
            page += 1
            SLEEP(self.delay)
        log(f"[{self.id}] archive.org items: {len(self.st['queue'])}")

    def ia_item(self, url: str):
        r = self.net.request("GET", "https://archive.org/metadata/" + quote(url.rsplit("/", 1)[-1]))
        if r is None:
            return None
        try:
            return r.json() if r.status_code == 200 else {}
        except ValueError:
            return {}

    def ia_crawl(self) -> None:
        st = self.st
        with ThreadPoolExecutor(self.workers) as ex:
            while st["queue"] and not self.out_of_time():
                batch = [st["queue"].popleft() for _ in range(min(self.workers * 2, len(st["queue"])))]
                for url, meta in zip(batch, ex.map(self.ia_item, batch)):
                    if meta is None:
                        st["failed"].append(url)
                        continue
                    info = meta.get("metadata") or {}
                    if not meta or meta.get("is_dark") or str(info.get("access-restricted-item", "")).lower() == "true":
                        continue
                    ident, files = url.rsplit("/", 1)[-1], []
                    for f in meta.get("files") or []:
                        name = f.get("name") or ""
                        low = name.lower()
                        if os.path.splitext(low)[1] not in EXTS or low.endswith("_meta.txt"):
                            continue
                        if low.endswith(".zip") and any(x in low for x in ("_jp2", "_tif", "_jpg", "_raw")):
                            continue
                        fu = f"https://archive.org/download/{ident}/" + quote(name, safe="/")
                        files.append(fu)
                        try:
                            st["sizes"][fu] = int(f.get("size") or 0)
                        except (TypeError, ValueError):
                            st["sizes"][fu] = 0
                    title = info.get("title")
                    title = (title[0] if title else "") if isinstance(title, list) else (title or ident)
                    desc = info.get("description")
                    desc = " ".join(map(str, desc)) if isinstance(desc, list) else str(desc or "")
                    if files:
                        st["pages"][url] = {"t": str(title)[:200], "d": re.sub(r"<[^>]+>", " ", desc)[:400], "b": "", "f": files}
                if sum(1 for p in st["pages"].values() if not p.get("ok")) >= 20:
                    self.flush()
                    self.save_state()
                SLEEP(self.delay)

    # ---- write to D1
    def flush(self) -> None:
        st, now = self.st, int(time.time())
        new = [u for u, p in st["pages"].items() if not p.get("ok")]
        if not new:
            return
        owner: dict[str, str] = {}
        for u in new:
            for f in st["pages"][u]["f"]:
                if f not in self.known_files:
                    owner.setdefault(f, u)
        todo = [f for f in owner if f not in st["sizes"]]
        if self.measure and todo:
            with ThreadPoolExecutor(self.workers) as ex:
                for i in range(0, len(todo), 40):
                    if self.out_of_time():
                        break
                    chunk = todo[i:i + 40]
                    for f, n in zip(chunk, ex.map(self.net.size, chunk)):
                        st["sizes"][f] = n
        dead = {f for u in new for f in st["pages"][u]["f"] if st["sizes"].get(f) == -1}
        if dead:                                          # broken links (404): not stored, and a page left with no file is not stored either
            for u in new:
                st["pages"][u]["f"] = [f for f in st["pages"][u]["f"] if f not in dead]
            owner = {f: u for f, u in owner.items() if f not in dead}
            log(f"[{self.id}] dead links skipped: {len(dead)}")
        page_rows = [(self.id, u, self.section_of(u), st["pages"][u]["t"], st["pages"][u]["d"], st["pages"][u]["b"],
                      st["via"].get(u, "links"), now) for u in new if st["pages"][u]["f"] and u not in self.known_pages]
        file_rows = [(self.id, f, u, self.section_of(u), file_ext(f)[1:], os.path.basename(unquote(urlparse(f).path))[:150],
                      st["sizes"].get(f, 0), now) for f, u in owner.items()]
        self.db.put_pages(page_rows)
        self.db.put_files(file_rows)
        st["owner"].update({r[1]: r[2] for r in file_rows})
        for u in new:
            st["pages"][u]["ok"] = True
            if not st["pages"][u]["f"]:
                st["pages"][u]["b"] = ""             # pages without files are not stored in D1; keep state small
        self.known_pages.update(r[1] for r in page_rows)
        self.known_files.update(r[1] for r in file_rows)
        log(f"[{self.id}] saved +{len(page_rows)} pages +{len(file_rows)} files")

    def reassign_owners(self) -> None:
        """A file linked from several pages is stored once. Early on it may be filed under a hub page (e.g. 'all PDFs');
        when the crawl is complete, move each file to the most specific page that links it (fewest files)."""
        st, best = self.st, {}
        for u, p in st["pages"].items():
            n = len(p["f"])
            for f in p["f"]:
                if f not in best or (n, len(u)) < best[f][0]:
                    best[f] = ((n, len(u)), u)
        moved = 0
        for f, (_, u) in best.items():
            if f in st["owner"] and st["owner"][f] != u:
                self.db.move_file(self.id, f, u, self.section_of(u))
                st["owner"][f] = u
                moved += 1
        if moved:
            log(f"[{self.id}] files moved to their own page: {moved}")

    # ---- one site
    def run(self) -> str:
        self.load_robots()
        st = None if self.fresh else self.load_state()
        if st and not st["queue"] and time.time() - st["done_at"] < self.rescan_days * 86400:
            log(f"[{self.id}] up to date (scanned {int((time.time() - st['done_at']) / 3600)}h ago)")
            return "complete"
        if st and not st["queue"]:
            st = None                                              # finished earlier: rescan from the start
        if st is None:
            self.st = self.new_state()
            self.known_pages, self.known_files = self.db.known(self.id)
            if self.site.get("ia"):
                self.ia_fill()
            else:
                if self.robots and not any(self.allowed(u) for u in self.st["queue"]):
                    log(f"[{self.id}] robots.txt asks bots to stay out - skipped")
                    return "blocked"
                self.add_sitemap()
        else:
            self.st = st
            self.st["queue"].extend(self.st["failed"])
            self.st["failed"] = []
            self.known_pages = {u for u, p in st["pages"].items() if p.get("ok")}
            self.known_files = {f for u in self.known_pages for f in st["pages"][u]["f"]}
            kp, kf = self.db.known(self.id)                    # a resumed run must also know what earlier runs wrote to D1
            self.known_pages |= kp
            self.known_files |= kf
        try:                                                       # whatever happens, keep the progress made so far
            if self.site.get("ia"):
                self.ia_crawl()
            else:
                self.crawl()
                if self.site.get("probe") and not self.out_of_time() and self.probe_dates():
                    self.crawl()
            self.flush()
        finally:
            self.save_state()
        if not self.st["queue"] and not self.site.get("ia"):
            self.reassign_owners()
        empty = not self.site.get("ia") and not self.st["pages"] and not self.skipped and (not self.st["queue"] or self.refused)
        if not self.st["queue"] and not empty:
            self.st["done_at"] = int(time.time())          # an empty scan must be retried, never "up to date"
        self.save_state()
        if self.skipped:
            log(f"[{self.id}] skipped {self.skipped} pages already in D1")
        status = "blocked" if empty else "complete" if not self.st["queue"] else "partial"
        self.db.site_row(self.site, status)
        log(f"[{self.id}] {status}: {len(self.st['pages'])} pages, {len(self.st['queue'])} queued, {len(self.st['failed'])} failed")
        if empty:
            codes = ", ".join(f"{c or 'no answer'}: {n}" for c, n in sorted(self.net.codes.items())) or "no requests made"
            log(f"[{self.id}] WARNING: fetched 0 pages (HTTP answers - {codes}). The site probably blocks bots "
                f"(403 / challenge page) or the seeds are wrong. Will be retried on the next run.")
        return status


def custom_site(url: str) -> dict:
    if "://" not in url:
        url = "https://" + url
    p = urlparse(url)
    bare = re.sub(r"^www\.", "", p.netloc.lower())
    return {"id": "custom_" + re.sub(r"\W+", "_", bare), "name": bare, "base": f"{p.scheme}://{p.netloc}",
            "root": p.path.rstrip("/"), "seeds": [p.path or "/"], "tags": ""}


def ia_site(words: str, kind: str = "both", limit: int = 100) -> dict:
    types = {"books": "texts", "audio": "audio"}.get(kind, "texts OR audio")
    slug = re.sub(r"\W+", "_", words.lower()).strip("_")[:30] or "search"
    return {"id": "ia_" + slug, "name": "Internet Archive: " + words, "base": "https://archive.org", "root": "", "ia": True,
            "q": f"({words}) AND mediatype:({types})", "limit": limit, "workers": 4, "delay": 0.4, "tags": "o"}
