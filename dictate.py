#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["anthropic>=2.12", "faster-whisper>=1.2", "numpy"]
# ///
"""dictate: push-to-talk dictation for Claude Code on KDE Wayland.

Pipeline: pw-record -> faster-whisper (local, CPU) -> Claude Haiku cleanup with
project context -> kdialog preview/edit -> paste or type into the focused window.

  dictate.py serve                 run the daemon (keeps the Whisper model loaded)
  dictate.py toggle [--mode M]     start/stop recording (bind this to a hotkey)
  dictate.py start|stop|cancel|status
  dictate.py preview [on|off|toggle] show or skip the preview window (persists)
  dictate.py toggle --no-preview    skip the preview for this one dictation
  dictate.py hook                  Claude Code hook: remember the active project
  dictate.py file X.wav [-p DIR]   offline test: transcribe + clean a 16 kHz WAV
  dictate.py clean [TEXT] [-p DIR] test the cleanup step on text (or stdin)
  dictate.py context [-p DIR]      print the project context sent to Haiku

Modes: clean (default), structure (also organise into a task spec), raw (skip Haiku).
Configuration is through DICTATE_* environment variables; see CONFIG below.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

APP = "dictate"
HOME = Path.home()
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/{APP}-{os.getuid()}")
SOCK = RUNTIME / f"{APP}.sock"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / APP
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME") or HOME / ".local/state") / APP
PROJECT_FILE = STATE_DIR / "current-project.json"
HISTORY_FILE = STATE_DIR / "history.jsonl"
PREVIEW_FILE = STATE_DIR / "preview"          # "on" / "off", set with `dictate preview`
SCRIPT_DIR = Path(__file__).resolve().parent


def cfg(name: str, default: str) -> str:
    return os.environ.get(f"DICTATE_{name}", default)


# CONFIG
CLAUDE_MODEL = cfg("CLAUDE_MODEL", "claude-haiku-5-5")
WHISPER_MODEL = cfg("WHISPER_MODEL", "small.en")      # or large-v3-turbo, distil-large-v3.5
WHISPER_DEVICE = cfg("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE = cfg("WHISPER_COMPUTE", "int8")
WHISPER_THREADS = int(cfg("WHISPER_THREADS", str(os.cpu_count() or 4)))
LANGUAGE = cfg("LANGUAGE", "en")                      # "" = auto-detect (multilingual models)
SOURCE = cfg("SOURCE", "")                            # PipeWire node name; "" = default source
DELIVERY = cfg("DELIVERY", "paste")                   # paste | type | clipboard | print
PASTE_KEYS = cfg("PASTE_KEYS", "ctrl+shift+v")        # works in Konsole and Electron/Chromium
CONFIRM = cfg("CONFIRM", "1") != "0"                  # preview default if `dictate preview` was never set
RESTORE_CLIPBOARD = cfg("RESTORE_CLIPBOARD", "0") == "1"
MAX_SECONDS = float(cfg("MAX_SECONDS", "180"))
# Structure mode thinks a little before laying the text out; without thinking, Haiku drops details and
# invents closings on long dictations. "off" = no thinking at low effort (faster, ~2.5 s instead of ~4.7 s).
STRUCTURE_EFFORT = cfg("STRUCTURE_EFFORT", "medium")
HISTORY = cfg("HISTORY", "1") == "1"
KEEP_AUDIO = cfg("KEEP_AUDIO", "0") == "1"
PROJECT_MAX_AGE_H = float(cfg("PROJECT_MAX_AGE_HOURS", "24"))


# ---------------------------------------------------------------- utilities

def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


class Notifier:
    """One desktop notification that is updated in place."""

    def __init__(self) -> None:
        self.nid: str | None = None

    def __call__(self, title: str, body: str = "", urgency: str = "low", close: bool = False) -> None:
        if not shutil.which("notify-send"):
            return
        cmd = ["notify-send", "-a", "Dictation", "-u", urgency, "-p", "-h", "boolean:transient:true"]
        if self.nid:
            cmd += ["-r", self.nid]
        if close:
            cmd += ["-t", "1500"]
        cmd += [title, body]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout.strip()
            self.nid = out or self.nid
        except (OSError, subprocess.SubprocessError):
            pass
        if close:
            self.nid = None


def read_head(path: Path, limit: int) -> str:
    text = path.read_text(errors="replace")
    return text if len(text) <= limit else text[:limit] + "\n[... truncated]"


def load_system_prompt() -> str:
    for p in (CONFIG_DIR / "system-prompt.md", SCRIPT_DIR / "cleanup-system-prompt.md"):
        if p.is_file():
            return p.read_text()
    sys.exit(f"{APP}: cleanup-system-prompt.md not found next to the script or in {CONFIG_DIR}")


def api_key() -> str | None:
    # A dictation-only key, kept out of ANTHROPIC_API_KEY so Claude Code keeps using
    # your claude.ai login (an exported ANTHROPIC_API_KEY would take precedence there).
    key = os.environ.get("DICTATE_ANTHROPIC_API_KEY")
    if key:
        return key.strip()
    f = CONFIG_DIR / "anthropic_key"
    return f.read_text().strip() if f.is_file() else None


# ---------------------------------------------------------- project context

def resolve_project(explicit: str | None = None) -> Path | None:
    for cand in (explicit, os.environ.get("DICTATE_PROJECT")):
        if cand:
            return Path(cand).expanduser().resolve()
    try:
        data = json.loads(PROJECT_FILE.read_text())
        p = Path(data["cwd"])
        if p.is_dir() and time.time() - data.get("ts", 0) < PROJECT_MAX_AGE_H * 3600:
            return p
    except (OSError, ValueError, KeyError):
        pass
    return None


def git_branch(start: Path) -> str | None:
    for d in (start, *start.parents):
        g = d / ".git"
        if g.is_file():  # worktree or submodule: "gitdir: <path>"
            m = re.match(r"gitdir:\s*(.+)", g.read_text().strip())
            g = (d / m.group(1)).resolve() if m else g
        if (g / "HEAD").is_file():
            head = (g / "HEAD").read_text().strip()
            return head.removeprefix("ref: refs/heads/") if head.startswith("ref:") else head[:12]
    return None


_TERM_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*`?([^`:=—–]{1,48}?)`?\s*(?::|=|—|–|\s-\s|$)")


def glossary_terms(text: str) -> list[str]:
    terms = re.findall(r"`([^`\n]{1,48})`", text)
    for line in text.splitlines():
        m = _TERM_RE.match(line)
        if m:
            terms.append(m.group(1).strip())
    return terms


def project_context(project: Path | None) -> tuple[str, list[str]]:
    """Returns (context block for Haiku, vocabulary hints for Whisper)."""
    parts: list[str] = []
    terms: list[str] = []
    if project and project.is_dir():
        branch = git_branch(project)
        parts.append(f"Project: {project.name} ({project})" + (f"\nGit branch: {branch}" if branch else ""))
        terms.append(project.name)
        for rel in (".claude/dictation.md", ".dictation-glossary.md"):
            f = project / rel
            if f.is_file():
                g = read_head(f, 6000)
                parts.append(f'<glossary source="{rel}">\n{g}\n</glossary>')
                terms += glossary_terms(g)
                break
        for rel in ("CLAUDE.md", ".claude/CLAUDE.md"):
            f = project / rel
            if f.is_file():
                parts.append(f"<claude_md>\n{read_head(f, 8000)}\n</claude_md>")
                break
        try:
            names = sorted(p.name for p in project.iterdir() if not p.name.startswith("."))[:80]
            if names:
                parts.append("Top-level files: " + ", ".join(names))
        except OSError:
            pass
    personal = CONFIG_DIR / "glossary.md"
    if personal.is_file():
        g = read_head(personal, 4000)
        parts.append(f'<glossary source="personal">\n{g}\n</glossary>')
        terms += glossary_terms(g)
    seen: set[str] = set()
    uniq = [t for t in terms if t and not (t.lower() in seen or seen.add(t.lower()))]
    return "\n\n".join(parts), uniq


# ------------------------------------------------------------ transcription

def load_wav(path: Path):
    import numpy as np  # local import keeps `dictate.py hook` fast

    with wave.open(str(path)) as w:
        if w.getframerate() != 16000 or w.getsampwidth() != 2:
            raise ValueError("expected 16 kHz 16-bit WAV (record with --rate 16000 --format s16)")
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        if w.getnchannels() > 1:
            data = data.reshape(-1, w.getnchannels()).mean(axis=1)
    return data.astype(np.float32) / 32768.0


class Transcriber:
    def __init__(self) -> None:
        from faster_whisper import WhisperModel

        t = time.monotonic()
        self.model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE,
                                  compute_type=WHISPER_COMPUTE, cpu_threads=WHISPER_THREADS)
        log(f"loaded Whisper {WHISPER_MODEL} ({WHISPER_DEVICE}/{WHISPER_COMPUTE}) in {time.monotonic() - t:.1f}s")

    def __call__(self, path: Path, terms: list[str]) -> str:
        audio = load_wav(path)
        hint = ("Glossary: " + ", ".join(terms))[:600] if terms else None
        segments, _ = self.model.transcribe(
            audio, language=LANGUAGE or None, beam_size=int(cfg("BEAM", "1")),
            vad_filter=True, initial_prompt=hint, condition_on_previous_text=False)
        text = " ".join(s.text.strip() for s in segments).strip()
        if hint and text.startswith("Glossary:"):  # hallucinated echo of the hint on silence
            return ""
        return text


# ------------------------------------------------------------------ cleanup

# Structure mode returns this shape, so the at-a-glance layout is guaranteed by code, not left to the model.
STRUCTURE_SCHEMA = {
    "type": "object",
    "properties": {
        "lead": {"type": "string", "description": "One or two short sentences, at most about 30 words, naming what the speaker "
                 "asks for in general terms. No specifics: file names, steps and details belong in points."},
        "steps": {"type": "array", "items": {"type": "string"},
                  "description": "Actions the speaker described in a set order, one per step; otherwise empty."},
        "points": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Two to four words, no full stop."},
                "text": {"type": "string", "description": "One idea in one or two sentences, about 25 words."},
                "details": {"type": "array", "items": {"type": "string"},
                            "description": "Extra specifics for this point as short sub-points; often empty."},
            },
            "required": ["name", "text", "details"],
            "additionalProperties": False,
        }},
        "closing": {"type": "string", "description": "The speaker's own request for feedback or ideas, if they made one; "
                    "otherwise empty. Never invent one."},
    },
    "required": ["lead", "steps", "points", "closing"],
    "additionalProperties": False,
}


def render_structure(data: dict) -> str:
    """Lead, numbered steps, bold-named bullets with sub-bullets, closing; blank lines between blocks."""
    blocks = [data.get("lead", "").strip()]
    steps = [s.strip() for s in data.get("steps", []) if s.strip()]
    if steps:
        blocks.append("\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)))
    lines = []
    for pt in data.get("points", []):
        name, text = pt.get("name", "").strip().rstrip(".:"), pt.get("text", "").strip()
        if not (name or text):
            continue
        lines.append(f"- **{name}.** {text}" if name else f"- {text}")
        lines += [f"  - {d.strip()}" for d in pt.get("details", []) if d.strip()]
    if lines:
        blocks.append("\n".join(lines))
    blocks.append(data.get("closing", "").strip())
    return "\n\n".join(b for b in blocks if b)


def clean(transcript: str, context: str, mode: str = "clean") -> tuple[str, str | None]:
    """Returns (text, warning). Falls back to the raw transcript on any API problem."""
    transcript = transcript.strip()
    if mode == "raw" or not transcript:
        return transcript, None
    import anthropic

    key = api_key()
    client = anthropic.Anthropic(api_key=key, timeout=20.0, max_retries=1) if key \
        else anthropic.Anthropic(timeout=20.0, max_retries=1)
    user = ""
    if context:
        user += f"<project_context>\n{context}\n</project_context>\n\n"
    if mode != "clean":
        user += f"<mode>{mode}</mode>\n\n"
    user += f"<transcript>\n{transcript}\n</transcript>"
    # Clean mode: no thinking, low effort (fastest; disabled thinking is accepted on Haiku 5.5 at effort <= high).
    thinking: dict = {"type": "disabled"}
    output_config: dict = {"effort": "low"}
    if mode == "structure":
        output_config["format"] = {"type": "json_schema", "schema": STRUCTURE_SCHEMA}
        if STRUCTURE_EFFORT != "off":
            thinking, output_config["effort"] = {"type": "adaptive"}, STRUCTURE_EFFORT
    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=8000,  # room for thinking plus the text
            system=load_system_prompt(),
            thinking=thinking,
            output_config=output_config,
            messages=[{"role": "user", "content": user}],
        )
    except TypeError as e:  # the SDK raises this when it finds no credentials at all
        if "authentication" not in str(e):
            raise
        return transcript, "Haiku cleanup skipped: no API key configured (see setup guide)"
    except anthropic.AuthenticationError:
        return transcript, "Haiku cleanup skipped: API key rejected"
    except anthropic.RateLimitError:
        return transcript, "Haiku cleanup skipped: rate limited"
    except anthropic.APIStatusError as e:
        return transcript, f"Haiku cleanup failed (HTTP {e.status_code})"
    except anthropic.APIConnectionError:
        return transcript, "Haiku cleanup skipped: no connection"
    if resp.stop_reason == "refusal":
        return transcript, "Haiku declined to rewrite this; showing the raw transcript"
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    warning = None
    if mode == "structure" and resp.stop_reason != "max_tokens":
        try:
            text = render_structure(json.loads(text))
        except (ValueError, AttributeError, TypeError):
            return transcript, "structure output could not be read; showing the raw transcript"
    if resp.stop_reason == "max_tokens":
        warning = "cleanup was cut off"
    elif len(text.split()) > 1.5 * len(transcript.split()) + 25:
        warning = "cleanup is much longer than what you said; it may have answered instead of rewriting"
    return text, warning


# ----------------------------------------------------------------- delivery

KEYCODES = {"ctrl": 29, "shift": 42, "alt": 56, "super": 125, "meta": 125,
            "v": 47, "insert": 110, "enter": 28}


def ydotool_keys(combo: str) -> bool:
    if not shutil.which("ydotool"):
        return False
    codes = [KEYCODES[k] for k in combo.lower().split("+")]
    args = [f"{c}:1" for c in codes] + [f"{c}:0" for c in reversed(codes)]
    return subprocess.run(["ydotool", "key", *args], capture_output=True).returncode == 0


def set_clipboard(text: str) -> None:
    subprocess.run(["wl-copy"], input=text.encode(), stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=True)


def deliver(text: str, how: str, notify: Notifier) -> None:
    if how == "print":
        print(text)
        return
    time.sleep(0.25)  # let focus return to the target window after the preview closes
    if how == "type":
        one_line = re.sub(r"\s*\n\s*", " ", text)  # Enter would submit in Claude Code
        if shutil.which("kwin-type"):
            subprocess.run(["kwin-type"], input=one_line.encode(), check=False)
            return
        if shutil.which("ydotool"):
            subprocess.run(["ydotool", "type", "--key-delay", "3", "--", one_line], check=False)
            return
        how = "clipboard"
    old = None
    if RESTORE_CLIPBOARD:
        r = subprocess.run(["wl-paste", "-n"], capture_output=True, timeout=2)
        old = r.stdout if r.returncode == 0 else None
    set_clipboard(text)
    if how == "paste" and ydotool_keys(PASTE_KEYS):
        if old is not None:
            time.sleep(1.0)
            subprocess.run(["wl-copy"], input=old, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    notify("Copied to clipboard", f"Paste with {PASTE_KEYS.title()}", close=True)


def preview_enabled() -> bool:
    try:
        return PREVIEW_FILE.read_text().strip() != "off"
    except OSError:
        return CONFIRM


def set_preview(value: str) -> bool:
    on = (not preview_enabled()) if value == "toggle" else value == "on"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PREVIEW_FILE.write_text("on\n" if on else "off\n")
    return on


def preview(raw: str, cleaned: str, warning: str | None, show: bool) -> str | None:
    if not show or not shutil.which("kdialog"):
        return cleaned
    heard = raw if len(raw) <= 400 else raw[:400] + "…"
    label = ("⚠ " + warning + "\n\n" if warning else "") + \
        "Edit if needed. OK delivers it, Cancel discards it.\n\nHeard: " + heard
    r = subprocess.run(["kdialog", "--title", "Dictation → Claude", "--geometry", "900x420",
                        "--textinputbox", label, cleaned], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return r.stdout.rstrip("\n")


def append_history(entry: dict) -> None:
    if not HISTORY:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with HISTORY_FILE.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------- daemon

class Daemon:
    def __init__(self) -> None:
        self.transcribe = Transcriber()
        self.proc: subprocess.Popen | None = None
        self.wav: Path | None = None
        self.started = 0.0
        self.mode = "clean"
        self.show_preview = True
        self.project: str | None = None
        self.busy = False
        self.lock = threading.Lock()
        self.notify = Notifier()
        self.timer: threading.Timer | None = None

    def start(self, mode: str, project: str | None, show_preview: bool | None = None) -> str:
        if self.proc:
            return "already recording"
        if self.busy:
            return "busy: still processing the last dictation"
        RUNTIME.mkdir(parents=True, exist_ok=True)
        self.wav = RUNTIME / f"{APP}-{int(time.time() * 1000)}.wav"
        cmd = ["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16"]
        if SOURCE:
            cmd += ["--target", SOURCE]
        self.proc = subprocess.Popen([*cmd, str(self.wav)], stdout=subprocess.DEVNULL, stderr=sys.stderr)
        self.started, self.mode, self.project = time.monotonic(), mode, project
        self.show_preview = preview_enabled() if show_preview is None else show_preview
        self.timer = threading.Timer(MAX_SECONDS, lambda: self.handle({"cmd": "stop"}))
        self.timer.daemon = True
        self.timer.start()
        how = "preview" if self.show_preview else "paste directly"
        self.notify("● Recording", f"{mode}, {how} — press the hotkey again to stop", urgency="normal")
        return "recording"

    def _stop_recorder(self) -> tuple[Path | None, float]:
        if self.timer:
            self.timer.cancel()
        proc, wav, dur = self.proc, self.wav, time.monotonic() - self.started
        self.proc = None
        if proc:
            proc.send_signal(signal.SIGINT)  # pw-record finalises the WAV header on SIGINT
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        return wav, dur

    def stop(self) -> str:
        if not self.proc:
            return "not recording"
        wav, dur = self._stop_recorder()
        if dur < 0.4 or not wav:
            self.notify("Dictation discarded", "too short", close=True)
            return "too short"
        self.busy = True
        threading.Thread(target=self.process, args=(wav, dur, self.mode, self.project, self.show_preview), daemon=True).start()
        return "processing"

    def cancel(self) -> str:
        wav, _ = self._stop_recorder()
        if wav and not KEEP_AUDIO:
            wav.unlink(missing_ok=True)
        self.notify("Dictation cancelled", close=True)
        return "cancelled"

    def process(self, wav: Path, dur: float, mode: str, project_arg: str | None, show: bool) -> None:
        try:
            t0 = time.monotonic()
            project = resolve_project(project_arg)
            context, terms = project_context(project)
            self.notify("Transcribing…", f"{dur:.1f}s of audio")
            raw = self.transcribe(wav, terms)
            t1 = time.monotonic()
            if not raw:
                self.notify("No speech detected", close=True)
                return
            if mode != "raw":
                self.notify("Cleaning up…", raw[:200])
            cleaned, warning = clean(raw, context, mode)
            t2 = time.monotonic()
            log(f"stt {t1 - t0:.2f}s, cleanup {t2 - t1:.2f}s, project={project}")
            if show:
                self.notify("Ready", "review the preview", close=True)
            elif warning:  # no preview to show it in, so make it visible here
                self.notify("⚠ Dictation", warning, urgency="normal")
            else:
                self.notify("Pasted", cleaned[:200], close=True)
            final = preview(raw, cleaned, warning, show)
            if final:
                deliver(final, DELIVERY, self.notify)
            append_history({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "project": str(project or ""),
                            "mode": mode, "audio_s": round(dur, 2), "stt_s": round(t1 - t0, 2),
                            "cleanup_s": round(t2 - t1, 2), "whisper": WHISPER_MODEL, "claude": CLAUDE_MODEL,
                            "raw": raw, "cleaned": cleaned, "final": final, "warning": warning})
        except Exception as e:  # keep the daemon alive whatever happens
            import traceback
            log("error: " + "".join(traceback.format_exception(e)))
            self.notify("Dictation failed", str(e)[:200], urgency="critical")
        finally:
            if not KEEP_AUDIO:
                wav.unlink(missing_ok=True)
            self.busy = False

    def handle(self, req: dict) -> str:
        with self.lock:
            cmd = req.get("cmd")
            if cmd == "toggle":
                return self.stop() if self.proc else \
                    self.start(req.get("mode", "clean"), req.get("project"), req.get("preview"))
            if cmd == "start":
                return self.start(req.get("mode", "clean"), req.get("project"), req.get("preview"))
            if cmd == "stop":
                return self.stop()
            if cmd == "cancel":
                return self.cancel()
            if cmd == "status":
                return json.dumps({"recording": bool(self.proc), "busy": self.busy, "whisper": WHISPER_MODEL,
                                   "preview": preview_enabled(), "project": str(resolve_project() or "")})
            return f"unknown command {cmd!r}"

    def serve(self) -> None:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        SOCK.unlink(missing_ok=True)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(SOCK))
        os.chmod(SOCK, 0o600)
        srv.listen(4)
        log(f"listening on {SOCK}")
        signal.signal(signal.SIGTERM, lambda *_: (SOCK.unlink(missing_ok=True), sys.exit(0)))
        while True:
            conn, _ = srv.accept()
            with conn:
                try:
                    req = json.loads(conn.recv(65536).decode() or "{}")
                    conn.sendall(self.handle(req).encode())
                except Exception as e:
                    conn.sendall(f"error: {e!r}".encode())


def send(req: dict) -> int:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(str(SOCK))
            s.sendall(json.dumps(req).encode())
            print(s.recv(65536).decode())
        return 0
    except OSError:
        msg = f"daemon not running (start it with: systemctl --user start {APP})"
        print(msg, file=sys.stderr)
        Notifier()("Dictation unavailable", msg, urgency="critical")
        return 1


# --------------------------------------------------------------------- main

def cmd_hook() -> int:
    """Claude Code SessionStart / UserPromptSubmit hook. Prints nothing: hook stdout becomes context."""
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        data = {}
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = PROJECT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"cwd": cwd, "session_id": data.get("session_id"), "ts": time.time()}))
        tmp.replace(PROJECT_FILE)
    except OSError:
        pass
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog=APP, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["serve", "toggle", "start", "stop", "cancel", "status", "preview",
                                        "hook", "file", "clean", "context"])
    ap.add_argument("arg", nargs="?", help="WAV path for `file`, text for `clean`, on/off/toggle for `preview`")
    ap.add_argument("-p", "--project", help="project directory (default: last Claude Code project)")
    ap.add_argument("-m", "--mode", default="clean", choices=["clean", "structure", "raw"])
    pv = ap.add_mutually_exclusive_group()
    pv.add_argument("--preview", dest="preview", action="store_true", default=None,
                    help="show the preview for this dictation")
    pv.add_argument("--no-preview", dest="preview", action="store_false",
                    help="paste directly for this dictation")
    a = ap.parse_args()

    if a.command == "hook":
        return cmd_hook()
    if a.command == "serve":
        Daemon().serve()
        return 0
    if a.command in ("toggle", "start", "stop", "cancel", "status"):
        return send({"cmd": a.command, "mode": a.mode, "project": a.project, "preview": a.preview})
    if a.command == "preview":
        if a.arg not in (None, "on", "off", "toggle"):
            ap.error("preview takes on, off or toggle")
        on = set_preview(a.arg) if a.arg else preview_enabled()
        msg = "Preview on: you review each dictation" if on else "Preview off: dictations paste directly"
        print(msg)
        if a.arg:
            Notifier()("Dictation", msg, close=True)
        return 0

    project = resolve_project(a.project)
    context, terms = project_context(project)
    if a.command == "context":
        print(f"# project: {project}\n# whisper hints: {terms}\n\n{context}")
        return 0
    if a.command == "file":
        t0 = time.monotonic()
        raw = Transcriber()(Path(a.arg), terms)
        print(f"raw ({time.monotonic() - t0:.2f}s incl. model load): {raw}", file=sys.stderr)
    else:
        raw = a.arg if a.arg else sys.stdin.read()
    t0 = time.monotonic()
    cleaned, warning = clean(raw, context, a.mode)
    print(f"cleanup {time.monotonic() - t0:.2f}s" + (f" — warning: {warning}" if warning else ""), file=sys.stderr)
    print(cleaned)
    return 0


if __name__ == "__main__":
    sys.exit(main())
