"""`dictate doctor`: checks what the installer sets up and says how to fix what isn't right."""
from __future__ import annotations

import shutil
import subprocess
import wave

from . import ipc, windows
from .audio import peak_level
from .cleanup import api_key
from .clipboard import clip_helper
from .config import (APP, CONFIG_DIR, HOME, NOTIFY_LEVEL, REPO_DIR, RUNTIME, SOURCE, VENV_PYTHON, autostop_enabled,
                     default_mode, preview_enabled)
from .project import resolve_project


def run(mic: bool) -> int:
    problems = 0

    def check(ok: bool, label: str, fix: str = "", optional: bool = False) -> None:
        nonlocal problems
        mark = "ok " if ok else (" - " if optional else "ERR")
        print(f"[{mark}] {label}" + ("" if ok or not fix else f"\n        fix: {fix}"))
        problems += 0 if ok or optional else 1

    for cmd, pkg in (("pw-record", "pipewire"), ("wl-copy", "wl-clipboard"), ("wl-paste", "wl-clipboard"),
                     ("notify-send", "libnotify"), ("kdialog", "kdialog"), ("ydotool", "ydotool")):
        check(bool(shutil.which(cmd)), f"{cmd} installed", f"sudo pacman -S --needed {pkg}")
    check(windows.available(), "kdotool installed (right paste keys, pasting into the right window)",
          "yay -S kdotool", optional=True)
    check(clip_helper() is not None, "rich-paste helper built (clip/wl-copy-multi)", f"make -C {REPO_DIR / 'clip'}")
    ok = VENV_PYTHON.exists() and subprocess.run([str(VENV_PYTHON), "-c", "import anthropic, faster_whisper"],
                                                 capture_output=True).returncode == 0
    check(ok, "Python environment with anthropic and faster-whisper", str(REPO_DIR / "install.sh"))
    check(api_key() is not None, "Anthropic API key configured",
          f"install -Dm600 /dev/stdin {CONFIG_DIR / 'anthropic_key'}  (paste the key, Enter, Ctrl+D)")
    for unit, optional in ((APP, False), ("dictate-tray", True), ("ydotool", False)):
        active = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True,
                                text=True).stdout.strip() == "active"
        check(active, f"service {unit} running", f"systemctl --user enable --now {unit}", optional=optional)
    check(ipc.status() is not None, "dictation service answers", f"journalctl --user -u {APP}")
    try:
        settings = (HOME / ".claude/settings.json").read_text()
    except OSError:
        settings = ""
    check("dictate hook" in settings, "Claude Code hooks track the current project",
          str(REPO_DIR / "install.sh"), optional=True)
    project = resolve_project()
    check(project is not None, f"current project: {project or 'none yet'}", "send a prompt in Claude Code",
          optional=True)
    print(f"      default mode: {default_mode()} · preview: {'on' if preview_enabled() else 'off'}"
          f" · auto-stop: {'on' if autostop_enabled() else 'off'} · notifications: {NOTIFY_LEVEL}")
    if mic:
        wav = RUNTIME / "dictate-mic-check.wav"
        cmd = ["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16"] + \
              (["--target", SOURCE] if SOURCE else [])
        print("      recording 2 s from the microphone, say something…", flush=True)
        try:
            subprocess.run(["timeout", "-s", "INT", "2", *cmd, str(wav)], capture_output=True)
            peak = peak_level(wav)
            check(peak > 0.02, f"microphone level (peak {peak:.3f})",
                  "pure silence: no audio reaches this machine (VM: route the viewer's recording stream to "
                  "your mic)" if peak == 0 else "very quiet: raise the input volume")
        except (OSError, wave.Error) as e:
            check(False, "microphone recording", str(e))
        finally:
            wav.unlink(missing_ok=True)
    print("\nAll good." if problems == 0 else f"\n{problems} problem(s) found.")
    return 0 if problems == 0 else 1
