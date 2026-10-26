# scripts/make_session.py  (4.0b: generate the Telethon StringSession once -> GitHub Secret TG_SESSION)
# Run on any machine with Python:  pip install telethon==1.45.0 && python scripts/make_session.py
# Values come from env (TG_API_ID, TG_API_HASH, TG_BOT_TOKEN) or are typed (hidden). The session is printed ONCE:
# copy it into the GitHub Secret TG_SESSION and never share or commit it (it is a login key).
from __future__ import annotations

import asyncio
import getpass
import os


def _ask(name: str, hidden: bool) -> str:
    v = (os.environ.get(name) or "").strip()
    return v or (getpass.getpass(f"{name}: ") if hidden else input(f"{name}: ")).strip()


async def main() -> None:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    api_id, api_hash, token = int(_ask("TG_API_ID", False)), _ask("TG_API_HASH", True), _ask("TG_BOT_TOKEN", True)
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start(bot_token=token)
    me = await client.get_me()
    print(f"Logged in as @{me.username} (bot={me.bot}).\nTG_SESSION (copy everything on the next line):")
    print(client.session.save())
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
