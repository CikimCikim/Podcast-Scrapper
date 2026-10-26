# bot/extras.py - caption (hyperlinked release date) and extra files (PDF / transcript) found on the episode page
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

from bot.feeds import Entry

MAX_EXTRAS = 5
MAX_EXTRA_BYTES = 40 * 1024 * 1024


def fmt_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%-d %B %Y") if ts else ""


def caption(e: Entry, page_url: str = "") -> str:
    """HTML caption (parse_mode html, <= 1024 chars): bold title, the release date as a link to the episode page, a short summary."""
    esc = html.escape
    date = fmt_date(e.published)
    if e.link and date:
        line = f'📅 <a href="{esc(e.link, quote=True)}">{esc(date)}</a>'
    elif e.link:
        line = f'🔗 <a href="{esc(e.link, quote=True)}">{esc(e.link)}</a>'
    else:
        line = f"📅 {esc(date)}" if date else ""
    head = f"🎧 <b>{esc(e.title)}</b>" + (f"\n{line}" if line else "")
    if page_url:
        head += f'\n📖 <a href="{esc(page_url, quote=True)}">Lesson page</a>'
    room = 1000 - len(head)
    summary = e.summary if len(e.summary) <= room - 2 else e.summary[: max(room - 3, 0)].rstrip() + "…"
    return head + (f"\n\n{esc(summary)}" if summary and room > 20 else "")


_FILE_RE = re.compile(r"https?:(?:\\?/){2}[^\s\"'<>()]+?\.(?:pdf|docx?)(?![A-Za-z0-9])", re.I)
_HREF_RE = re.compile(r"""(?:href|src)\s*=\s*["']([^"'\s]+?\.(?:pdf|docx?)(?:\?[^"'\s]*)?)["']""", re.I)


def find_extras(page_html: str, base_url: str, must_contain: str = "") -> list[str]:
    """Absolute URLs of .pdf/.doc/.docx files in the page: normal links AND urls inside inline JSON (https:\\/\\/...),
    the way learning_sites_scanner.py finds them. must_contain (e.g. the episode date 260319) drops unrelated site-wide files."""
    found: list[str] = []
    for m in _HREF_RE.finditer(page_html):
        found.append(urljoin(base_url, html.unescape(m.group(1))))
    for m in _FILE_RE.finditer(page_html):
        found.append(m.group(0).replace("\\/", "/"))
    out: list[str] = []
    keys: set[str] = set()
    for u in found:
        k = u.split("?")[0]                       # same file with/without ?query counts once
        if k not in keys and (not must_contain or must_contain in u):
            keys.add(k)
            out.append(u)
    return out[:MAX_EXTRAS]


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2]


def title_matches(title: str, page_html: str, need: float = 0.6) -> bool:
    """True when most words of the entry title occur in the page (to confirm a guessed episode page)."""
    ws = _words(title)
    if not ws:
        return False
    body = " ".join(_words(re.sub(r"<[^>]+>", " ", html.unescape(page_html))))
    return sum(1 for w in set(ws) if w in body) / len(set(ws)) >= need


def page_candidates(template: str, published: float, offsets=(0, -1, 1, -2, 2, -3, 3)) -> list[str]:
    """Episode page urls guessed from the release date, e.g.
    https://www.bbc.co.uk/learningenglish/english/features/6-minute-english_{yyyy}/ep-{yymmdd}"""
    from datetime import timedelta
    if not published or not template:
        return []
    base = datetime.fromtimestamp(published, timezone.utc)
    out = []
    for o in offsets:
        d = base + timedelta(days=o)
        out.append(template.format(yyyy=d.strftime("%Y"), yymmdd=d.strftime("%y%m%d")))
    return out
