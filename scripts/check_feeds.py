# scripts/check_feeds.py - prints what each feed in feeds.json really returns (run it in Actions: check_feeds workflow)
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.feeds import parse_feed  # noqa: E402

cfg = json.loads((Path(__file__).resolve().parent.parent / "feeds.json").read_text("utf-8"))
for s in cfg["sites"]:
    print("=" * 60, s["id"], s["feed"])
    try:
        r = httpx.get(s["feed"], headers={"User-Agent": "Mozilla/5.0"}, timeout=30, follow_redirects=True)
        print("HTTP", r.status_code, r.headers.get("content-type"))
        es = parse_feed(r.content)
        print("entries:", len(es), "| with media:", sum(1 for e in es if e.media_url))
        for e in es[-3:]:
            print(" -", e.media_kind, e.title[:70], "|", (e.media_url or "")[:90])
    except Exception as exc:  # noqa: BLE001
        print("ERROR", type(exc).__name__, exc)
