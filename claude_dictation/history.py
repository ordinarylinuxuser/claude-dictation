"""The dictation log: what Whisper heard, what Haiku made of it, and what you sent."""
from __future__ import annotations

import json

from .config import HISTORY, HISTORY_FILE, STATE_DIR


def append(entry: dict) -> None:
    if not HISTORY:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with HISTORY_FILE.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def last_text() -> str | None:
    """The most recent text that was delivered (or would have been), for "paste again"."""
    try:
        lines = HISTORY_FILE.read_text().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        text = e.get("final") or e.get("cleaned")
        if text:
            return text
    return None
