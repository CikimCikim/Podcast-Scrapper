# scripts/scan.py - scan learning sites and store the catalog in D1.
#   python scripts/scan.py                       all sites in sites.json
#   python scripts/scan.py --sites bbc,peg       some sites
#   python scripts/scan.py --url https://x.org/english --ia "english grammar" --ia-type audio
import argparse
import json
import os
import sys
import time
from pathlib import Path

from bot.scan import EXTS, D1Sync, Net, Scanner, custom_site, ia_site

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sites", default="", help="comma list of site ids from sites.json (empty = all)")
    ap.add_argument("--url", default="", help="extra site to scan, e.g. https://example.com/english")
    ap.add_argument("--ia", default="", help="Internet Archive search words")
    ap.add_argument("--ia-type", default="both", choices=["books", "audio", "both"])
    ap.add_argument("--ia-limit", type=int, default=100)
    ap.add_argument("--budget-min", type=float, default=float(os.environ.get("BUDGET_MIN", "320")), help="stop politely after N minutes")
    ap.add_argument("--types", default="", help="file types to keep, e.g. pdf,mp3 (default: all known: pdf, audio, video, office, epub, zip, srt...)")
    ap.add_argument("--no-measure", action="store_true", help="skip HEAD requests for file sizes")
    ap.add_argument("--fresh", action="store_true", help="ignore saved progress")
    ap.add_argument("--rescan-days", type=int, default=7)
    ap.add_argument("--state-dir", default=os.environ.get("SCAN_STATE_DIR", str(ROOT / "scan_state")))
    a = ap.parse_args()

    cfg = json.loads((ROOT / "sites.json").read_text("utf-8"))
    want = {s for s in a.sites.split(",") if s}
    if want:
        sites = [s for s in cfg["sites"] if s["id"] in want]
    else:
        sites = [] if (a.url or a.ia) else list(cfg["sites"])
    if want and not sites:
        print("no such site; known:", ", ".join(s["id"] for s in cfg["sites"]))
        return 2
    if a.url:
        sites.append(custom_site(a.url))
    if a.ia:
        sites.append(ia_site(a.ia, a.ia_type, a.ia_limit))

    exts = tuple("." + t.strip(" .") for t in a.types.split(",") if t.strip()) or EXTS
    db = D1Sync(os.environ["CF_ACCOUNT_ID"], os.environ["CF_D1_ID"], os.environ["CF_API_TOKEN"])
    net = Net()
    deadline = time.monotonic() + a.budget_min * 60
    failed = 0
    for s in sites:
        if time.monotonic() > deadline:
            print(f"[{s['id']}] skipped: time budget used up (next run continues)")
            continue
        try:
            res = Scanner(s, net, db, a.state_dir, deadline, measure=not a.no_measure, rescan_days=a.rescan_days, fresh=a.fresh, exts=exts).run()
            if res == "blocked":                      # 0 pages: make the job red / annotated instead of a silent green
                failed += 1
                print(f"::warning::[{s['id']}] scanned 0 pages - site blocks the bot or seeds are wrong", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001  one broken site must not stop the others
            failed += 1
            print(f"[{s['id']}] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 1 if sites and failed == len(sites) else 0


if __name__ == "__main__":
    raise SystemExit(main())
