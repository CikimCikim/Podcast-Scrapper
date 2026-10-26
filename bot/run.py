# bot/run.py - one run: for every site in feeds.json fetch the feed, send only NEW items into that site's topic.
# Audio is sent as a streamable audio post (hyperlinked date), then the extra files (PDF ...) found on the episode page.
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

from bot import state
from bot.extras import MAX_EXTRA_BYTES, caption, find_extras, page_candidates, title_matches
from bot.feeds import HEADERS, Entry, feed_urls, parse_feed
from bot.store import make_store

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = Path(os.environ.get("STATE_DIR", ROOT / "state"))
WORK = Path(os.environ.get("WORK_DIR", "/tmp/work"))
PREFETCH = 3


def _duration(path: Path) -> int:
    try:
        import mutagen
        f = mutagen.File(str(path))
        return int(f.info.length) if f and f.info else 0
    except Exception:  # noqa: BLE001
        return 0


async def _fetch_file(client: httpx.AsyncClient, url: str, dest: Path, limit: int = 0) -> Path | None:
    try:
        async with client.stream("GET", url, headers=HEADERS) as r:
            r.raise_for_status()
            if limit and int(r.headers.get("content-length") or 0) > limit:
                return None
            with dest.open("wb") as f:
                async for chunk in r.aiter_bytes(1 << 20):
                    f.write(chunk)
        return dest
    except Exception as exc:  # noqa: BLE001
        print(f"  download failed {url[:90]}: {type(exc).__name__}", file=sys.stderr)
        dest.unlink(missing_ok=True)
        return None


def _name(e: Entry, url: str, default_ext: str) -> str:
    return hashlib.sha1((e.uid + url).encode()).hexdigest()[:12] + (Path(url.split("?")[0]).suffix or default_ext)


async def _prepare(http: httpx.AsyncClient, site: dict, e: Entry, dest_dir: Path) -> dict:
    """Download the audio and the extra files of one entry (runs ahead of the upload)."""
    media = await _fetch_file(http, e.media_url, dest_dir / _name(e, e.media_url, ".mp3")) if e.media_url else None
    urls, files, page_url = [], [], ""
    if site.get("extras"):
        page_html, page_url = None, ""
        tmpl = site.get("page_template", "")
        for cand in page_candidates(tmpl, e.published):          # guessed lesson page (verified by title)
            try:
                pr = await http.get(cand, headers=HEADERS)
            except Exception:  # noqa: BLE001
                continue
            if pr.status_code == 200 and title_matches(e.title, pr.text):
                page_html, page_url = pr.text, str(pr.url)
                break
        if page_html is None and e.link:                          # fall back to the feed's own link
            try:
                pr = await http.get(e.link, headers=HEADERS)
                if pr.status_code == 200:
                    page_html, page_url = pr.text, ""
            except Exception as exc:  # noqa: BLE001
                print(f"  episode page failed {e.link}: {type(exc).__name__}", file=sys.stderr)
        if page_html:
            must = datetime.fromtimestamp(e.published, timezone.utc).strftime("%y%m%d") if site.get("extras_need_date") and e.published else ""
            urls = find_extras(page_html, page_url or e.link, must)
        for u in urls:
            p = await _fetch_file(http, u, dest_dir / _name(e, u, ".pdf"), MAX_EXTRA_BYTES)
            if p:
                files.append(p)
    return {"entry": e, "media": media, "extra_urls": urls, "extra_files": files, "page_url": page_url}


async def run_site(tg, site: dict, defaults: dict, store, http: httpx.AsyncClient) -> None:
    sid = site["id"]
    entries = None
    for u in feed_urls(site):
        try:
            r = await http.get(u, headers=HEADERS)
            r.raise_for_status()
            entries = parse_feed(r.content)
            break
        except Exception as exc:  # noqa: BLE001
            print(f"[{sid}] feed failed {u}: {type(exc).__name__}", file=sys.stderr)
    if entries is None:
        raise RuntimeError("all feed URLs failed")
    seen, first = await store.seen_uids(sid)
    send, seen_after = state.select_new(entries, seen, first, site.get("initial_send", defaults["initial_send"]),
                                        site.get("max_per_run", defaults["max_per_run"]))
    skipped = [e for e in entries if e.uid in set(seen_after) - set(seen)]
    if skipped:
        await store.mark_skipped(sid, skipped)
    print(f"[{sid}] feed={len(entries)} to_send={len(send)} first_run={first}")
    if not send:
        return
    topic = await store.topic(sid)
    if not topic:
        topic = site.get("topic_id") or await tg.create_topic(site["topic"])
        await store.set_topic(sid, topic, site["topic"])
        print(f"[{sid}] topic created id={topic}")
    q: asyncio.Queue = asyncio.Queue(maxsize=PREFETCH)
    dest_dir = WORK / sid
    dest_dir.mkdir(parents=True, exist_ok=True)

    async def producer() -> None:
        for e in send:
            await q.put(await _prepare(http, site, e, dest_dir))
        await q.put(None)

    prod = asyncio.create_task(producer())
    while (item := await q.get()) is not None:
        e, media, files = item["entry"], item["media"], item["extra_files"]
        try:
            if e.media_url and media is None:
                continue                      # download failed: retry on the next run
            if media is None:
                m = await tg.client.send_message(tg.chat, caption(e, item.get('page_url', '')), parse_mode="html", reply_to=topic, link_preview=False)
                msg_id, doc_id = int(m.id), 0
            else:
                msg_id, doc_id = await tg.send_media(topic, media, kind=e.media_kind, caption=caption(e, item.get('page_url', '')), title=e.title,
                                                     performer=site.get("performer", ""), duration=_duration(media), mime=e.mime)
            extra_ids = await tg.send_documents(topic, files, "📎") if files else []
            await store.record_sent(sid, e, item["extra_urls"], msg_id, doc_id, extra_ids, item.get("page_url", ""))
            print(f"  sent: {e.title[:60]} (+{len(files)} files)")
        finally:
            for p in [media, *files]:
                if p:
                    p.unlink(missing_ok=True)
    await prod


async def main() -> int:
    from bot.tg import Tg
    cfg = json.loads((ROOT / "feeds.json").read_text("utf-8"))
    store = make_store(STATE_DIR)
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
                    await run_site(tg, site, cfg["defaults"], store, http)
                except Exception as exc:  # noqa: BLE001  one broken site must not stop the others
                    failed += 1
                    print(f"[{site['id']}] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        await store.flush()
        await tg.close()
        shutil.rmtree(WORK, ignore_errors=True)
    return 1 if failed == len(cfg["sites"]) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
