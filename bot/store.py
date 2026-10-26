# bot/store.py - where "what was sent" lives. D1 (Cloudflare REST API) when CF_* env is set, else JSON files in state/.
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

from bot import state as jstate
from bot.feeds import Entry


class JsonStore:
    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.seen = jstate.load(directory / "seen.json")
        self.topics = jstate.load(directory / "topics.json")

    async def seen_uids(self, site: str):
        """-> (uids, first_run)"""
        return list(self.seen.get(site, [])), site not in self.seen

    async def mark_skipped(self, site: str, entries: list[Entry]) -> None:
        self.seen.setdefault(site, []).extend(e.uid for e in entries)

    async def topic(self, site: str):
        return self.topics.get(site)

    async def set_topic(self, site: str, topic_id: int, title: str) -> None:
        self.topics[site] = topic_id

    async def record_sent(self, site: str, e: Entry, extras: list[str], msg_id: int, doc_id: int, extra_ids: list[int], page_url: str = "") -> None:
        self.seen.setdefault(site, []).append(e.uid)

    async def flush(self) -> None:
        self.seen = {k: v[-jstate.SEEN_CAP:] for k, v in self.seen.items()}
        jstate.save(self.dir / "seen.json", self.seen)
        jstate.save(self.dir / "topics.json", self.topics)


class D1Store:
    """Cloudflare D1 through the REST API (works from Actions, no Worker needed). # VERIFY: response shape result[0].results"""
    CHUNK = 20      # D1 allows ~100 bound parameters per statement

    def __init__(self, account: str, db: str, token: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.url = f"https://api.cloudflare.com/client/v4/accounts/{account}/d1/database/{db}/query"
        self.http = httpx.AsyncClient(timeout=30, headers={"Authorization": f"Bearer {token}"}, transport=transport)

    async def q(self, sql: str, params: list | None = None) -> list[dict]:
        r = await self.http.post(self.url, json={"sql": sql, "params": params or []})
        data = r.json()
        if r.status_code != 200 or not data.get("success"):
            raise RuntimeError(f"D1 error {r.status_code}: {data.get('errors')}")
        return data["result"][0].get("results", [])

    async def seen_uids(self, site: str):
        rows = await self.q("SELECT uid FROM episodes WHERE site_id=?", [site])
        return [r["uid"] for r in rows], not rows

    async def mark_skipped(self, site: str, entries: list[Entry]) -> None:
        for i in range(0, len(entries), self.CHUNK):
            part = entries[i:i + self.CHUNK]
            sql = "INSERT OR IGNORE INTO episodes(site_id,uid,status,title,pub_ts) VALUES " + ",".join(["(?,?,'skipped',?,?)"] * len(part))
            params = [x for e in part for x in (site, e.uid, e.title, int(e.published))]
            await self.q(sql, params)

    async def topic(self, site: str):
        rows = await self.q("SELECT topic_id FROM topics WHERE site_id=?", [site])
        return rows[0]["topic_id"] if rows else None

    async def set_topic(self, site: str, topic_id: int, title: str) -> None:
        await self.q("INSERT OR REPLACE INTO topics(site_id,topic_id,title,created_at) VALUES (?,?,?,?)",
                     [site, topic_id, title, int(time.time())])

    async def record_sent(self, site: str, e: Entry, extras: list[str], msg_id: int, doc_id: int, extra_ids: list[int], page_url: str = "") -> None:
        await self.q("INSERT OR REPLACE INTO episodes(site_id,uid,status,title,link,pub_ts,media_url,extras,tg_msg_id,tg_doc_id,extra_msg_ids,sent_at,page_url)"
                     " VALUES (?,?,'sent',?,?,?,?,?,?,?,?,?,?)",
                     [site, e.uid, e.title, e.link, int(e.published), e.media_url, json.dumps(extras), msg_id, doc_id,
                      json.dumps(extra_ids), int(time.time()), page_url])

    async def flush(self) -> None:
        await self.http.aclose()


def make_store(directory: Path):
    acc, db, tok = (os.environ.get(k, "") for k in ("CF_ACCOUNT_ID", "CF_D1_ID", "CF_API_TOKEN"))
    if acc and db and tok:
        print("state: Cloudflare D1")
        return D1Store(acc, db, tok)
    print("state: JSON files (set CF_ACCOUNT_ID, CF_D1_ID, CF_API_TOKEN to use D1)")
    return JsonStore(directory)
