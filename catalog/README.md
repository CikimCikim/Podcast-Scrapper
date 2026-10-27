# catalog/

Written by `scripts/scan.py --store files`. One folder per site:

- `pages.csv` - pages that carry at least one wanted file (site_id,url,section,title,descr,body,via,found_at)
- `files.csv` - file links (site_id,url,page_url,section,ext,name,bytes,found_at). `bytes` = 0 means the server did not tell the size.
- `meta.json` - status and counts of the last scan

`INDEX.md` and `sites.csv` are built by `scripts/build_index.py` after every scan.

Run it on a phone (Termux): `pip install requests`, then `python scripts/scan.py --sites voa --store files`, then `git add catalog && git commit && git push`.
