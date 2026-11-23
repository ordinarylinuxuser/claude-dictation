"""Getting the text into the window you dictated for: paste (or type), with focus safety."""
from __future__ import annotations

import re
import shutil
import subprocess
import time

from . import clipboard, windows
from .config import FOCUS, PASTE_KEYS, PASTE_KEYS_FALLBACK, RESTORE_CLIPBOARD
from .notify import Notifier

KEYCODES = {"ctrl": 29, "shift": 42, "alt": 56, "super": 125, "meta": 125, "v": 47, "insert": 110, "enter": 28}


def ydotool_keys(combo: str) -> bool:
    if not shutil.which("ydotool"):
        return False
    codes = [KEYCODES[k] for k in combo.lower().split("+")]
    args = [f"{c}:1" for c in codes] + [f"{c}:0" for c in reversed(codes)]
    return subprocess.run(["ydotool", "key", *args], capture_output=True).returncode == 0


def keys_for(window_class: str | None) -> str:
    """Ctrl+V keeps the rich HTML in apps; terminals paste with Ctrl+Shift+V."""
    if PASTE_KEYS != "auto":
        return PASTE_KEYS
    if not window_class:
        return PASTE_KEYS_FALLBACK
    return "ctrl+shift+v" if windows.is_terminal(window_class) else "ctrl+v"


def _type(text: str) -> bool:
    one_line = re.sub(r"\s*\n\s*", " ", text)  # Enter would submit in Claude Code
    if shutil.which("kwin-type"):
        return subprocess.run(["kwin-type"], input=one_line.encode(), check=False).returncode == 0
    if shutil.which("ydotool"):
        return subprocess.run(["ydotool", "type", "--key-delay", "3", "--", one_line], check=False).returncode == 0
    return False


def deliver(text: str, how: str, notify: Notifier, target: str | None = None) -> str:
    """Puts text into the target window (the one focused when you stopped recording).

    If you've switched windows since, FOCUS=restore briefly switches back to paste and then returns
    you to where you are; FOCUS=warn leaves it on the clipboard instead.
    Returns "pasted", "copied" (you paste it) or "printed".
    """
    if how == "print":
        print(text)
        return "printed"
    time.sleep(0.25)  # let focus settle after a preview dialog closes
    current = windows.active_window()
    return_to = None
    if target and current and target != current and FOCUS != "off":
        if FOCUS == "restore" and windows.activate(target):
            return_to = current
            time.sleep(0.15)
        else:
            clipboard.set_clipboard(text)
            gone = not windows.exists(target)
            notify("copied", "The window you dictated for is closed. Paste it where you like."
                   if gone else "You switched windows, so it's on the clipboard. Paste it where you like.")
            return "copied"
    try:
        if how == "type" and _type(text):
            return "pasted"
        old = clipboard.get_clipboard() if RESTORE_CLIPBOARD else None
        clipboard.set_clipboard(text)
        keys = keys_for(windows.window_class(target or current))
        if how in ("paste", "type") and ydotool_keys(keys):
            if old is not None:
                time.sleep(1.0)
                clipboard.restore_clipboard(old)
            return "pasted"
        notify("copied", f"Paste with {keys.title()}.")
        return "copied"
    finally:
        if return_to:
            time.sleep(0.3)  # let the target read the clipboard before focus moves on
            windows.activate(return_to)
