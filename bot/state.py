# bot/state.py - seen.json / topics.json (committed to the repo by the workflow)
from __future__ import annotations

import json
from pathlib import Path

SEEN_CAP = 500


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), "utf-8")
    tmp.replace(path)


def select_new(entries, seen: list[str], first_run: bool, initial_send: int, max_per_run: int):
    """-> (to_send oldest-first, seen_after). On the very first run only the newest `initial_send`
    are sent and everything else is just marked seen (no archive dump)."""
    seen_set = set(seen)
    fresh = [e for e in entries if e.uid not in seen_set]
    if first_run:
        send = fresh[-initial_send:] if initial_send > 0 else []
        skipped = [e for e in fresh if e not in send]
        seen_after = seen + [e.uid for e in skipped]
        return send, seen_after[-SEEN_CAP:]
    return fresh[:max_per_run], seen[-SEEN_CAP:]
