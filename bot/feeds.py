# bot/feeds.py - parse an RSS/podcast feed into plain Entry objects (no network inside parse_feed)
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

AUDIO_EXT = (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav")
VIDEO_EXT = (".mp4", ".mov", ".webm", ".m4v")


@dataclass
class Entry:
    uid: str
    title: str
    link: str
    summary: str
    media_url: Optional[str]
    media_kind: str          # "audio" | "video" | "none"
    mime: str = ""
    published: float = 0.0   # unix time, 0 = unknown


def _kind(url: str, mime: str) -> str:
    u = url.lower().split("?")[0]
    if mime.startswith("audio/") or u.endswith(AUDIO_EXT):
        return "audio"
    if mime.startswith("video/") or u.endswith(VIDEO_EXT):
        return "video"
    return "none"


def _clean(html: str, limit: int = 350) -> str:
    t = re.sub(r"<[^>]+>", " ", html or "")
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def _txt(el, tag):
    x = el.find(tag)
    return (x.text or "").strip() if x is not None and x.text else ""


def parse_feed(data: bytes | str) -> list[Entry]:
    """RSS 2.0 (podcast) via stdlib only. Returns entries oldest -> newest."""
    import calendar
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    if isinstance(data, str):
        data = data.encode("utf-8")
    root = ET.fromstring(data)
    out: list[Entry] = []
    for it in root.iter("item"):
        url, mime = None, ""
        for enc in it.findall("enclosure"):
            href, m = enc.get("url") or "", enc.get("type") or ""
            if href and _kind(href, m) != "none":
                url, mime = href, m
                break
        link = _txt(it, "link")
        uid = _txt(it, "guid") or link or url or _txt(it, "title")
        ts = 0.0
        try:
            ts = parsedate_to_datetime(_txt(it, "pubDate")).timestamp()
        except (TypeError, ValueError):
            pass
        out.append(Entry(uid=uid, title=_clean(_txt(it, "title"), 200) or "(untitled)", link=link,
                         summary=_clean(_txt(it, "description")), media_url=url,
                         media_kind=_kind(url, mime) if url else "none", mime=mime, published=ts))
    if out and all(x.published for x in out):
        out.sort(key=lambda x: x.published)
    else:
        out.reverse()          # feeds list newest first
    return out
