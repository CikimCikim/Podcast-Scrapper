# bot/tg.py - the only file importing Telethon. Bot-token session (StringSession), forum topics, streamable audio.
from __future__ import annotations

import asyncio
import os
from pathlib import Path

MAX_FLOOD_WAIT = 300
MAX_BYTES = 2000 * 1024 * 1024   # VERIFY: per-file limit for a bot account


def _is_flood(exc: BaseException) -> bool:
    return type(exc).__name__.startswith(("FloodWait", "SlowModeWait")) and isinstance(getattr(exc, "seconds", None), int)


async def with_flood_wait(make, tries: int = 5):
    for i in range(tries):
        try:
            return await make()
        except Exception as exc:  # noqa: BLE001
            if not _is_flood(exc) or i == tries - 1 or exc.seconds > MAX_FLOOD_WAIT:
                raise
            await asyncio.sleep(exc.seconds + 1)


def chat_id(raw: str) -> int:
    """Supergroup (forum) ids look like -100xxxxxxxxxx. A positive number gets the -100 prefix; a negative one is kept as is.
    A negative id WITHOUT -100 (e.g. -50xxxxxxxx) is an old basic group: it has no Topics (see README)."""
    v = int(str(raw).strip())
    return int("-100" + str(v)) if v > 0 else v


class Tg:
    def __init__(self) -> None:
        self.client = None
        self.chat = None

    async def connect(self) -> None:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        self.client = TelegramClient(StringSession(os.environ["TG_SESSION"]), int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise SystemExit("TG_SESSION is not authorized: run scripts/make_session.py again")
        cid = chat_id(os.environ["TG_CHAT_ID"])
        if cid < 0 and not str(cid).startswith("-100"):
            raise SystemExit(f"TG_CHAT_ID {cid} is a basic group, not a supergroup with Topics. Turn Topics on, then use the new -100... id.")
        try:
            self.chat = await self.client.get_entity(cid)
        except ValueError:
            # A fresh bot session has no cached access_hash and bots cannot list dialogs.
            # Ask Telegram directly for the channel (hash 0 is accepted for chats the bot is a member of). # VERIFY
            from telethon import functions, types, utils
            channel_id, _ = utils.resolve_id(cid)
            r = await self.client(functions.channels.GetChannelsRequest([types.InputChannel(channel_id, 0)]))
            if not r.chats:
                raise SystemExit(f"Bot cannot see chat {cid}: add the bot to the group as admin, then retry")
            self.chat = r.chats[0]

    async def close(self) -> None:
        if self.client:
            await self.client.disconnect()

    async def create_topic(self, title: str) -> int:
        """Telethon 1.45 has no CreateForumTopicRequest, so the Bot API is used for this one call.
        Needs TG_BOT_TOKEN (same bot as the session), Topics ON and the bot an admin with 'Manage Topics'."""
        import httpx
        token = os.environ.get("TG_BOT_TOKEN")
        if not token:
            raise SystemExit("TG_BOT_TOKEN secret is missing (needed once to create topics), or set topic_id in feeds.json")
        async with httpx.AsyncClient(timeout=30) as http:
            r = await with_flood_wait(lambda: http.post(f"https://api.telegram.org/bot{token}/createForumTopic",
                                                        json={"chat_id": chat_id(os.environ["TG_CHAT_ID"]), "name": title[:128]}))
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"createForumTopic failed: {data.get('description')}")
        return int(data["result"]["message_thread_id"])

    async def send_media(self, topic_id: int, path: Path, *, kind: str, caption: str, title: str = "",
                         performer: str = "", duration: int = 0, mime: str = "") -> int:
        """kind audio -> shown in Telegram's music player, plays while still loading (not 'document').
        kind video -> supports_streaming=True (plays before full download)."""
        from telethon.tl import types
        attrs, kw = [], {}
        if kind == "audio":
            attrs = [types.DocumentAttributeAudio(duration=duration, title=title[:60] or None, performer=performer[:60] or None, voice=False)]
            kw = dict(force_document=False, mime_type=mime or "audio/mpeg")
        elif kind == "video":
            attrs = [types.DocumentAttributeVideo(duration=duration, w=0, h=0, supports_streaming=True)]
            kw = dict(force_document=False, supports_streaming=True)
        else:
            kw = dict(force_document=True)
        msg = await with_flood_wait(lambda: self.client.send_file(
            self.chat, str(path), caption=caption[:1024], reply_to=topic_id, attributes=attrs or None,
            part_size_kb=512, **kw))
        return int(msg.id)
