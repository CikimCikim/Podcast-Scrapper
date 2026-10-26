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
            if s.get("extras"):               # what would be attached to the newest episodes?
                from bot.extras import caption, find_extras, page_candidates, title_matches
                for e in es[-2:]:
                    print(" * link:", e.link)
                    for cand in page_candidates(s.get("page_template", ""), e.published):
                        try:
                            cr = httpx.get(cand, headers=HEADERS, timeout=30, follow_redirects=True)
                        except Exception as exc:  # noqa: BLE001
                            print("   cand", cand, type(exc).__name__)
                            continue
                        ok = cr.status_code == 200 and title_matches(e.title, cr.text)
                        print("   cand", cr.status_code, "title-match" if ok else "-", cand)
                        if ok:
                            print("   LESSON PAGE pdfs:", find_extras(cr.text, str(cr.url)))
                            break
                    print(" * caption:", caption(e).replace("\n", " | ")[:200])
                    try:
                        pr = httpx.get(e.link, headers=HEADERS, timeout=30, follow_redirects=True)
                        print("   page HTTP", pr.status_code, "| pdf links:", find_extras(pr.text, str(pr.url)))
                        print("   all hrefs with pdf/transcript/download:", sorted({m for m in __import__("re").findall(r'href="([^"]*(?:transcript|download|pdf)[^"]*)"', pr.text, flags=__import__("re").I)})[:10])
                    except Exception as exc:  # noqa: BLE001
                        print("   page ERROR", type(exc).__name__, exc)
        except Exception as exc:  # noqa: BLE001
            print("ERROR", type(exc).__name__, exc)
