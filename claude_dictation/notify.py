"""Desktop notifications. Every one comes from the NOTICES table, so titles, icons and lifetimes match."""
from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path

from .config import DESKTOP_ID, NOTIFY_LEVEL

# kind: progress = stays until the next state replaces it; done = closes after 3 s;
#       attention = stays 8 s; error = stays until dismissed.
NOTICES = {
    "recording":    ("Recording",             "media-record",           "progress"),
    "transcribing": ("Transcribing",          "audio-input-microphone", "progress"),
    "cleaning":     ("Tidying up",            "document-edit",          "progress"),
    "review":       ("Ready to review",       "document-preview",       "done"),
    "pasted":       ("Pasted",                "edit-paste",             "done"),
    "copied":       ("Copied",                "edit-copy",              "done"),
    "cancelled":    ("Cancelled",             "dialog-cancel",          "done"),
    "too-short":    ("Too short",             "dialog-cancel",          "done"),
    "setting":      ("Setting changed",       "configure",              "done"),
    "no-speech":    ("No speech heard",       "audio-input-microphone", "attention"),
    "silent":       ("Microphone is silent",  "dialog-warning",         "attention"),
    "check":        ("Check the result",      "dialog-warning",         "attention"),
    "nothing":      ("Nothing to paste",      "dialog-information",     "attention"),
    "error":        ("Dictation failed",      "dialog-error",           "error"),
    "unavailable":  ("Dictation unavailable", "dialog-error",           "error"),
}
_KIND = {  # urgency, timeout ms (0 = until replaced or dismissed), transient (kept out of history)
    "progress": ("low", 0, True), "done": ("low", 3000, True),
    "attention": ("normal", 8000, False), "error": ("critical", 0, False),
}
_LEVEL_SHOWS = {"all": {"progress", "done", "attention", "error"}, "results": {"done", "attention", "error"},
                "errors": {"attention", "error"}, "none": set()}


class Notifier:
    """One notification bubble, updated in place. Each dictation gets its own Notifier."""

    def __init__(self) -> None:
        self.nid: str | None = None
        self.lock = threading.Lock()

    def __call__(self, state: str, body: str = "") -> None:
        title, icon, kind = NOTICES[state]
        if kind not in _LEVEL_SHOWS.get(NOTIFY_LEVEL, _LEVEL_SHOWS["all"]) or not shutil.which("notify-send"):
            return
        urgency, timeout, transient = _KIND[kind]
        body = body if len(body) <= 240 else body[:239] + "…"
        with self.lock:
            cmd = ["notify-send", "-a", "Claude Dictation", "-i", icon, "-u", urgency, "-t", str(timeout), "-p",
                   "-h", f"string:desktop-entry:{DESKTOP_ID}"]
            if transient:
                cmd += ["-h", "boolean:transient:true"]
            if self.nid:
                cmd += ["-r", self.nid]
            try:
                out = subprocess.run([*cmd, title, body], capture_output=True, text=True, timeout=3).stdout.strip()
                self.nid = out or self.nid
            except (OSError, subprocess.SubprocessError):
                pass


def describe(mode: str, show_preview: bool, project: Path | None = None, autostop: bool = False) -> str:
    """The one-line context under in-progress notifications and in the tray."""
    parts = [f"{mode.capitalize()} mode", "preview" if show_preview else "pastes directly"]
    if autostop:
        parts.append("stops after a pause")
    if project:
        parts.append(project.name)
    return " · ".join(parts)
