"""Paths, DICTATE_* settings from the environment, and the settings you switch at runtime."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

APP = "dictate"
DESKTOP_ID = "claude-dictation"  # the installer's .desktop file; groups notifications in KDE
HOME = Path.home()
PACKAGE_DIR = Path(__file__).resolve().parent
REPO_DIR = PACKAGE_DIR.parent
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/{APP}-{os.getuid()}")
SOCK = RUNTIME / f"{APP}.sock"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / APP
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME") or HOME / ".local/state") / APP
PROJECT_FILE = STATE_DIR / "current-project.json"
HISTORY_FILE = STATE_DIR / "history.jsonl"
VENV_PYTHON = HOME / ".local/share/dictate/venv/bin/python"
PROMPT_FILE = PACKAGE_DIR / "prompts" / "cleanup.md"
MODES = ("clean", "structure", "raw")


def env(name: str, default: str) -> str:
    return os.environ.get(f"DICTATE_{name}", default)


# Speech to text
WHISPER_MODEL = env("WHISPER_MODEL", "small.en")       # or large-v3-turbo, distil-large-v3.5
WHISPER_DEVICE = env("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE = env("WHISPER_COMPUTE", "int8")
WHISPER_THREADS = int(env("WHISPER_THREADS", str(os.cpu_count() or 4)))
WHISPER_BEAM = int(env("BEAM", "1"))
LANGUAGE = env("LANGUAGE", "en")                       # "" = auto-detect (multilingual models)

# Recording
SOURCE = env("SOURCE", "")                             # PipeWire node name; "" = default source
MAX_SECONDS = float(env("MAX_SECONDS", "180"))
AUTOSTOP_SECONDS = float(env("AUTOSTOP_SECONDS", "1.5"))  # pause length that ends a recording
SPEECH_LEVEL = float(env("SPEECH_LEVEL", "0.012"))     # RMS above this counts as speech
SILENT_PEAK = 0.0005                                   # a whole recording below this is a dead mic

# Cleanup
CLAUDE_MODEL = env("CLAUDE_MODEL", "claude-haiku-5-5")
# Structure mode thinks a little before laying the text out; without thinking, Haiku drops details on
# long dictations. "off" = no thinking at low effort (faster, ~2.5 s instead of ~4.7 s).
STRUCTURE_EFFORT = env("STRUCTURE_EFFORT", "medium")

# Delivery
DELIVERY = env("DELIVERY", "paste")                    # paste | type | clipboard | print
PASTE_KEYS = env("PASTE_KEYS", "auto")                 # auto | ctrl+v | ctrl+shift+v
PASTE_KEYS_FALLBACK = env("PASTE_KEYS_FALLBACK", "ctrl+shift+v")  # auto without kdotool
RICH_PASTE = env("RICH_PASTE", "1") == "1"             # also offer text/html: real lists and bold in editors
RESTORE_CLIPBOARD = env("RESTORE_CLIPBOARD", "0") == "1"
FOCUS = env("FOCUS", "restore")                        # restore | warn | off: if you switched windows meanwhile
CONFIRM = env("CONFIRM", "1") != "0"                   # preview default if `dictate preview` was never set

# Feedback and records
NOTIFY_LEVEL = env("NOTIFY", "all")                    # all | results | errors | none
HISTORY = env("HISTORY", "1") == "1"
KEEP_AUDIO = env("KEEP_AUDIO", "0") == "1"
PROJECT_MAX_AGE_H = float(env("PROJECT_MAX_AGE_HOURS", "24"))


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------- settings you switch at runtime
# Each lives in its own small file under STATE_DIR, so the CLI, the tray and the daemon share them.

def _read(name: str) -> str | None:
    try:
        return (STATE_DIR / name).read_text().strip()
    except OSError:
        return None


def _write(name: str, value: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / name).write_text(value + "\n")


def _switch(name: str, value: str, current: bool) -> bool:
    on = (not current) if value == "toggle" else value == "on"
    _write(name, "on" if on else "off")
    return on


def preview_enabled() -> bool:
    v = _read("preview")
    return CONFIRM if v is None else v != "off"


def set_preview(value: str) -> bool:
    return _switch("preview", value, preview_enabled())


def autostop_enabled() -> bool:
    v = _read("autostop")
    return AUTOSTOP_SECONDS > 0 and (v is None or v != "off")


def set_autostop(value: str) -> bool:
    return _switch("autostop", value, autostop_enabled())


def default_mode() -> str:
    v = _read("mode")
    return v if v in MODES else "clean"


def set_mode(value: str) -> str:
    m = MODES[(MODES.index(default_mode()) + 1) % len(MODES)] if value == "next" else value
    _write("mode", m)
    return m
