# Builds catalog/sites.csv and catalog/INDEX.md (a readable summary) from catalog/<site>/meta.json + files.csv.
#   python scripts/build_index.py [catalog_dir]
import csv
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "catalog"


def main() -> None:
    rows = []
    for meta in sorted(ROOT.glob("*/meta.json")):
        m = json.loads(meta.read_text("utf-8"))
        ext, size, unknown = Counter(), 0, 0
        fp = meta.parent / "files.csv"
        if fp.exists():
            with open(fp, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    ext[r["ext"]] += 1
                    b = int(float(r["bytes"] or 0))
                    size += b
                    unknown += b == 0
        m.update(types=" ".join(f"{e}:{n}" for e, n in ext.most_common()), gb=round(size / 1e9, 2), unknown=unknown)
        rows.append(m)
    cols = ["site_id", "name", "base", "tags", "status", "last_scan", "pages", "files", "gb", "unknown", "types"]
    with open(ROOT / "sites.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    out = ["# Catalog", "", f"Updated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}. "
           "Per site: `<site>/pages.csv` (pages that carry files) and `<site>/files.csv` (file links).", "",
           "| site | status | pages | files | GB | unknown size | types |", "|---|---|---:|---:|---:|---:|---|"]
    for m in rows:
        out.append(f"| [{m['site_id']}]({m['site_id']}/) | {m['status']} | {m['pages']:,} | {m['files']:,} | {m['gb']} | {m['unknown']:,} | {m['types']} |")
    out += ["", f"**Total:** {sum(m['pages'] for m in rows):,} pages, {sum(m['files'] for m in rows):,} files, "
            f"{round(sum(m['gb'] for m in rows), 2)} GB (files with unknown size count as 0)."]
    (ROOT / "INDEX.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"index: {len(rows)} sites")


if __name__ == "__main__":
    main()
