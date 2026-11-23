"""KWin windows through kdotool (AUR). Every function degrades to None/False without it."""
from __future__ import annotations

import shutil
import subprocess

TERMINAL_CLASSES = ("konsole", "yakuake", "alacritty", "kitty", "foot", "wezterm", "ghostty", "terminal", "xterm",
                    "tilix", "terminator", "warp")


def _kdotool(*args: str) -> str | None:
    if not shutil.which("kdotool"):
        return None
    try:
        r = subprocess.run(["kdotool", *args], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return out if r.returncode == 0 and out else None


def available() -> bool:
    return shutil.which("kdotool") is not None


def active_window() -> str | None:
    return _kdotool("getactivewindow")


def window_class(window: str | None = None) -> str | None:
    out = _kdotool("getwindowclassname", window) if window else _kdotool("getactivewindow", "getwindowclassname")
    return out.lower() if out else None


def exists(window: str) -> bool:
    return _kdotool("getwindowname", window) is not None


def activate(window: str) -> bool:
    """Brings the window to the front; True once it really is the active one."""
    if not shutil.which("kdotool"):
        return False
    subprocess.run(["kdotool", "windowactivate", window], capture_output=True, timeout=2)
    return active_window() == window


def is_terminal(cls: str | None) -> bool:
    return bool(cls) and any(t in cls for t in TERMINAL_CLASSES)
