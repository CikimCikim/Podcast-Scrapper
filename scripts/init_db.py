# scripts/init_db.py - creates the D1 tables from schema.sql (Actions -> init_db). Safe to run again.
import asyncio
import os
from pathlib import Path

from bot.store import D1Store


async def main() -> None:
    s = D1Store(os.environ["CF_ACCOUNT_ID"], os.environ["CF_D1_ID"], os.environ["CF_API_TOKEN"])
    sql = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()
    for part in sql.split(";"):
        stmt = "\n".join(l for l in part.splitlines() if not l.strip().startswith("--")).strip()
        if stmt:
            await s.q(stmt)
            print("ok:", stmt.split("(")[0].strip()[:60])
    try:                                   # tables created by an older version get the new column
        await s.q("ALTER TABLE episodes ADD COLUMN page_url TEXT")
        print("ok: added episodes.page_url")
    except RuntimeError as e:
        if "duplicate column" not in str(e).lower():
            raise
    print("tables:", [r["name"] for r in await s.q("SELECT name FROM sqlite_master WHERE type='table'")])
    await s.flush()

asyncio.run(main())
