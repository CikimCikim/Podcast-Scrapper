# scripts/check_feeds.py - prints what each feed in feeds.json really returns (run it in Actions: check_feeds workflow)
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.feeds import parse_feed  # noqa: E402

cfg = json.loads((Path(__file__).resolve().parent.parent / "feeds.json").read_text("utf-8"))
from bot.feeds import HEADERS, feed_urls  # noqa: E402

for s in cfg["sites"]:
    for u in feed_urls(s):
        print("=" * 60, s["id"], u)
        try:
            r = httpx.get(u, headers=HEADERS, timeout=30, follow_redirects=True)
            print("HTTP", r.status_code, r.headers.get("content-type"))
            r.raise_for_status()
            es = parse_feed(r.content)
            print("entries:", len(es), "| with media:", sum(1 for e in es if e.media_url))
            for e in es[-3:]:
                print(" -", e.media_kind, e.title[:70], "|", (e.media_url or "")[:90])
            if es and es[-1].media_url:       # can the runner download the media itself?
                h = httpx.head(es[-1].media_url, headers=HEADERS, timeout=30, follow_redirects=True)
                print("media HEAD:", h.status_code, h.headers.get("content-type"), h.headers.get("content-length"))
        except Exception as exc:  # noqa: BLE001
            print("ERROR", type(exc).__name__, exc)
