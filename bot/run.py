# bot/run.py - one run: for every site in feeds.json fetch the feed, send only NEW items into that site's topic.
# Download of the next files overlaps with the upload of the current one (queue of 3).
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

import httpx

from bot import state
from bot.feeds import Entry, parse_feed

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = Path(os.environ.get("STATE_DIR", ROOT / "state"))
WORK = Path(os.environ.get("WORK_DIR", "/tmp/work"))
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) langfeed/0.1"}
PREFETCH = 3


def _duration(path: Path) -> int:
    try:
        import mutagen
        f = mutagen.File(str(path))
        return int(f.info.length) if f and f.info else 0
    except Exception:  # noqa: BLE001
        return 0


async def _download(client: httpx.AsyncClient, e: Entry, dest_dir: Path) -> Path | None:
    ext = Path(e.media_url.split("?")[0]).suffix or ".mp3"
    dest = dest_dir / (f"{abs(hash(e.uid)) % 10**10}{ext}")
    try:
        async with client.stream("GET", e.media_url, headers=UA) as r:
            r.raise_for_status()
            with dest.open("wb") as f:
                async for chunk in r.aiter_bytes(1 << 20):
                    f.write(chunk)
        return dest
    except Exception as exc:  # noqa: BLE001
        print(f"  download failed {e.media_url}: {type(exc).__name__}", file=sys.stderr)
        return None


def _caption(e: Entry) -> str:
    parts = [f"🎧 {e.title}"]
    if e.summary:
        parts.append(e.summary)
    if e.link:
        parts.append(e.link)
    return "\n\n".join(parts)


async def run_site(tg, site: dict, defaults: dict, seen_all: dict, topics: dict, http: httpx.AsyncClient) -> None:
    sid = site["id"]
    r = await http.get(site["feed"], headers=UA)
    r.raise_for_status()
    entries = parse_feed(r.content)
    first = sid not in seen_all
    send, seen_after = state.select_new(entries, seen_all.get(sid, []), first,
                                        site.get("initial_send", defaults["initial_send"]),
                                        site.get("max_per_run", defaults["max_per_run"]))
    seen_all[sid] = seen_after
    print(f"[{sid}] feed={len(entries)} to_send={len(send)} first_run={first}")
    if not send:
        return
    if sid not in topics:
        topics[sid] = await tg.create_topic(site["topic"])
        print(f"[{sid}] topic created id={topics[sid]}")
    q: asyncio.Queue = asyncio.Queue(maxsize=PREFETCH)
    dest_dir = WORK / sid
    dest_dir.mkdir(parents=True, exist_ok=True)

    async def producer() -> None:
        for e in send:
            await q.put((e, await _download(http, e, dest_dir) if e.media_url else None))
        await q.put(None)

    prod = asyncio.create_task(producer())
    while (item := await q.get()) is not None:
        e, path = item
        try:
            if path is None:    # no media or download failed: text-only post, still marked as seen only if no media expected
                if e.media_url:
                    continue    # retry on the next run
                await tg.client.send_message(tg.chat, _caption(e), reply_to=topics[sid], link_preview=False)
            else:
                await tg.send_media(topics[sid], path, kind=e.media_kind, caption=_caption(e), title=e.title,
                                    performer=site.get("performer", ""), duration=_duration(path), mime=e.mime)
            seen_all[sid].append(e.uid)
            print(f"  sent: {e.title[:60]}")
        finally:
            if path:
                path.unlink(missing_ok=True)
    await prod


async def main() -> int:
    from bot.tg import Tg
    cfg = json.loads((ROOT / "feeds.json").read_text("utf-8"))
    seen_all = state.load(STATE_DIR / "seen.json")
    topics = state.load(STATE_DIR / "topics.json")
    only = set(filter(None, os.environ.get("ONLY_SITES", "").split(",")))
    tg = Tg()
    await tg.connect()
    failed = 0
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as http:
            for site in cfg["sites"]:
                if only and site["id"] not in only:
                    continue
                try:
                    await run_site(tg, site, cfg["defaults"], seen_all, topics, http)
                except Exception as exc:  # noqa: BLE001  one broken site must not stop the others
                    failed += 1
                    print(f"[{site['id']}] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
                finally:
                    state.save(STATE_DIR / "seen.json", seen_all)
                    state.save(STATE_DIR / "topics.json", topics)
    finally:
        await tg.close()
        shutil.rmtree(WORK, ignore_errors=True)
    return 1 if failed == len(cfg["sites"]) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
