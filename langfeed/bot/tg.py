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
        self.chat = await self.client.get_entity(int(os.environ["TG_CHAT_ID"]))

    async def close(self) -> None:
        if self.client:
            await self.client.disconnect()

    async def create_topic(self, title: str) -> int:
        """Needs: group with Topics ON, bot is admin with 'Manage Topics'. Returns message_thread_id."""
        from telethon import functions, types
        import random
        res = await with_flood_wait(lambda: self.client(functions.channels.CreateForumTopicRequest(
            channel=self.chat, title=title[:128], random_id=random.randrange(1, 2**31))))
        for u in getattr(res, "updates", []):
            if isinstance(u, types.UpdateNewChannelMessage):
                return int(u.message.id)
        raise RuntimeError("topic created but its id was not found in the reply")

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
