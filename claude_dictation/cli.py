"""The `dictate` command.

  dictate toggle [--mode M] [--no-preview]   start/stop a dictation (bind to a hotkey)
  dictate start | stop | cancel | status
  dictate repaste                            paste the last dictation again into the focused window
  dictate mode [clean|structure|raw|next]    default mode for toggle without --mode
  dictate preview [on|off|toggle]            review each dictation before it's pasted
  dictate autostop [on|off|toggle]           end the recording after a pause
  dictate doctor [--mic]                     check the installation (--mic: test the level)
  dictate serve | tray                       the background service / the tray icon
  dictate hook                               Claude Code hook: remember the current project
  dictate context [-p DIR]                   show the project context sent to Haiku
  dictate clean [TEXT] [-p DIR] [-m MODE]    test the cleanup step on text (or stdin)
  dictate file X.wav [-p DIR] [-m MODE]      test transcription + cleanup on a 16 kHz WAV

Modes: clean (tidy prose), structure (at-a-glance layout), raw (no Haiku).
Settings: DICTATE_* variables in ~/.config/dictate/env (see claude_dictation/config.py).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import config, ipc
from .config import MODES


def _switch_command(ap, value, read, write, on_text: str, off_text: str) -> int:
    if value not in (None, "on", "off", "toggle"):
        ap.error("takes on, off or toggle")
    on = write(value) if value else read()
    msg = on_text if on else off_text
    print(msg)
    if value:
        ipc.notice("setting", msg)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="dictate", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["toggle", "start", "stop", "cancel", "status", "repaste", "mode", "preview",
                                        "autostop", "doctor", "serve", "tray", "hook", "context", "clean", "file"])
    ap.add_argument("arg", nargs="?", help="the value for mode/preview/autostop, text for clean, a WAV for file")
    ap.add_argument("-p", "--project", help="project directory (default: last Claude Code project)")
    ap.add_argument("-m", "--mode", default=None, choices=MODES,
                    help="mode for this dictation (default: the one set with `dictate mode`)")
    pv = ap.add_mutually_exclusive_group()
    pv.add_argument("--preview", dest="preview", action="store_true", default=None,
                    help="show the preview for this dictation")
    pv.add_argument("--no-preview", dest="preview", action="store_false", help="paste this dictation directly")
    ap.add_argument("--mic", action="store_true", help="doctor: also record 2 s and report the level")
    a = ap.parse_args(argv)

    if a.command == "hook":
        from .project import record_project_from_hook
        return record_project_from_hook()
    if a.command in ("toggle", "start", "stop", "cancel", "status", "repaste"):
        return ipc.send({"cmd": a.command, "mode": a.mode, "project": a.project, "preview": a.preview})
    if a.command == "mode":
        if a.arg not in (None, *MODES, "next"):
            ap.error("mode takes clean, structure, raw or next")
        m = config.set_mode(a.arg) if a.arg else config.default_mode()
        print(f"Default mode: {m}.")
        if a.arg:
            ipc.notice("setting", f"Default mode: {m}.")
        return 0
    if a.command == "preview":
        return _switch_command(ap, a.arg, config.preview_enabled, config.set_preview,
                               "Preview on: you review each dictation.", "Preview off: dictations paste directly.")
    if a.command == "autostop":
        return _switch_command(ap, a.arg, config.autostop_enabled, config.set_autostop,
                               f"Auto-stop on: recording ends after a {config.AUTOSTOP_SECONDS:g} s pause.",
                               "Auto-stop off: press the hotkey to stop.")
    if a.command == "doctor":
        from .doctor import run
        return run(a.mic)
    if a.command == "serve":
        from .daemon import Daemon
        Daemon().serve()
        return 0
    if a.command == "tray":
        from .tray import run
        return run()

    from .project import project_context, resolve_project
    project = resolve_project(a.project)
    context, terms = project_context(project)
    if a.command == "context":
        print(f"# project: {project}\n# whisper hints: {terms}\n\n{context}")
        return 0
    from .cleanup import Cleaner
    if a.command == "file":
        from .transcribe import Transcriber
        t0 = time.monotonic()
        raw = Transcriber()(Path(a.arg), terms)
        print(f"raw ({time.monotonic() - t0:.2f}s incl. model load): {raw}", file=sys.stderr)
    else:
        raw = a.arg if a.arg else sys.stdin.read()
    t0 = time.monotonic()
    cleaned, warning = Cleaner()(raw, context, a.mode or config.default_mode())
    print(f"cleanup {time.monotonic() - t0:.2f}s" + (f" — warning: {warning}" if warning else ""), file=sys.stderr)
    print(cleaned)
    return 0
