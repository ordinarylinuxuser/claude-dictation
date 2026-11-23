"""The background service: one recording at a time, finished recordings processed in order.

Each dictation is a Job with its own notification bubble. You can start the next recording while
the previous one is still being transcribed or tidied up; a single worker handles them in order.
"""
from __future__ import annotations

import itertools
import json
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import history, windows
from .audio import Recorder, peak_level
from .cleanup import Cleaner
from .config import (APP, CLAUDE_MODEL, DELIVERY, KEEP_AUDIO, MAX_SECONDS, RUNTIME, SILENT_PEAK, SOCK,
                     WHISPER_MODEL, autostop_enabled, default_mode, log, preview_enabled)
from .deliver import deliver
from .notify import Notifier, describe
from .project import project_context, resolve_project
from .transcribe import Transcriber

STAGE_TEXT = {"transcribing": "transcribing", "cleaning": "tidying up", "reviewing": "waiting for your review",
              "delivering": "pasting"}


@dataclass
class Job:
    id: int
    mode: str
    project: str | None
    show_preview: bool
    autostop: bool
    wav: Path
    recorder: Recorder
    notify: Notifier = field(default_factory=Notifier)
    duration: float = 0.0
    target_window: str | None = None


def review(raw: str, cleaned: str, warning: str | None) -> str | None:
    """The optional preview: an editable box. None if you cancel."""
    if not shutil.which("kdialog"):
        return cleaned
    heard = raw if len(raw) <= 400 else raw[:400] + "…"
    label = ("⚠ " + warning + "\n\n" if warning else "") + \
        "Edit if needed. OK delivers it, Cancel discards it.\n\nHeard: " + heard
    r = subprocess.run(["kdialog", "--title", "Dictation → Claude", "--geometry", "900x420",
                        "--textinputbox", label, cleaned], capture_output=True, text=True)
    return r.stdout.rstrip("\n") if r.returncode == 0 else None


class Daemon:
    def __init__(self) -> None:
        self.transcribe = Transcriber()
        self.clean = Cleaner()
        self.lock = threading.RLock()
        self.notify = Notifier()          # for setting changes and "paste again"
        self.recording: Job | None = None
        self.jobs: "queue.Queue[Job]" = queue.Queue()
        self.pending = 0                  # finished recordings not yet delivered
        self.current: Job | None = None   # the one the worker is on
        self.stage = ""
        self.last_text: str | None = None
        self._ids = itertools.count(1)
        threading.Thread(target=self._worker, daemon=True).start()

    # ------------------------------------------------------------ recording

    def start(self, mode: str, project: str | None, show_preview: bool | None) -> str:
        if self.recording:
            return "already recording"
        RUNTIME.mkdir(parents=True, exist_ok=True)
        job_id = next(self._ids)
        autostop = autostop_enabled()
        wav = RUNTIME / f"{APP}-{int(time.time() * 1000)}-{job_id}.wav"
        on_pause = (lambda: self.handle({"cmd": "stop", "job": job_id, "auto": True})) if autostop else None
        job = Job(job_id, mode, project, preview_enabled() if show_preview is None else show_preview,
                  autostop, wav, Recorder(wav, on_pause))
        job.recorder.start()
        self.recording = job
        timer = threading.Timer(MAX_SECONDS, lambda: self.handle({"cmd": "stop", "job": job_id}))
        timer.daemon = True
        timer.start()
        self.clean.warm()  # opens the API connection while you talk
        line = describe(mode, job.show_preview, resolve_project(project), autostop)
        if self.pending:
            line += f" · {self.pending} earlier still in progress"
        job.notify("recording", line)
        return "recording"

    def stop(self, job_id: int | None = None, auto: bool = False) -> str:
        job = self.recording
        if not job or (job_id is not None and job.id != job_id):
            return "not recording"
        self.recording = None
        # The window focused now is where you want the text (with auto-stop: where you were dictating).
        job.target_window = windows.active_window()
        job.duration = job.recorder.stop()
        if job.duration < 0.4:
            job.notify("too-short", "Hold the recording a little longer.")
            self._discard(job)
            return "too short"
        self.pending += 1
        self.jobs.put(job)
        return "processing"

    def cancel(self) -> str:
        job = self.recording
        if not job:
            return "nothing to cancel"
        self.recording = None
        job.recorder.stop()
        self._discard(job)
        job.notify("cancelled", "Nothing was pasted.")
        return "cancelled"

    def _discard(self, job: Job) -> None:
        if not KEEP_AUDIO:
            job.wav.unlink(missing_ok=True)

    # ----------------------------------------------------------- processing

    def _worker(self) -> None:
        while True:
            job = self.jobs.get()
            self.current = job
            try:
                self._process(job)
            except Exception as e:  # keep the service alive whatever happens
                import traceback
                log("error: " + "".join(traceback.format_exception(e)))
                job.notify("error", f"{e}. Details: journalctl --user -u {APP}")
            finally:
                self._discard(job)
                with self.lock:
                    self.current, self.stage = None, ""
                    self.pending -= 1

    def _process(self, job: Job) -> None:
        t0 = time.monotonic()
        if peak_level(job.wav) < SILENT_PEAK:
            log(f"job {job.id}: recording is silent")
            job.notify("silent", "The recording is pure silence, so no audio reaches the microphone. "
                                 "In a VM, check that the viewer's recording stream goes to your mic. "
                                 "Test with: dictate doctor --mic")
            return
        project = resolve_project(job.project)
        context, terms = project_context(project)
        line = describe(job.mode, job.show_preview, project)
        self.stage = "transcribing"
        job.notify("transcribing", f"{job.duration:.0f} s of audio · {line}")
        raw = self.transcribe(job.wav, terms)
        t1 = time.monotonic()
        if not raw:
            log(f"job {job.id}: no speech recognised")
            job.notify("no-speech", "Speak a little closer to the mic, or check it with: dictate doctor --mic")
            return
        if job.mode != "raw":
            self.stage = "cleaning"
            job.notify("cleaning", line)
        cleaned, warning = self.clean(raw, context, job.mode)
        t2 = time.monotonic()
        log(f"job {job.id}: stt {t1 - t0:.2f}s, cleanup {t2 - t1:.2f}s, project={project}")
        if job.show_preview:
            self.stage = "reviewing"
            job.notify("review", "Edit if needed, then OK to paste.")
            final = review(raw, cleaned, warning)
        else:
            final = cleaned
        result = None
        if final:
            self.stage = "delivering"
            self.last_text = final
            result = deliver(final, DELIVERY, job.notify, job.target_window)
            if warning and not job.show_preview:  # with no preview, this is the only place to see it
                job.notify("check", warning)
            elif result == "pasted":
                job.notify("pasted", final)
        else:
            job.notify("cancelled", "Discarded in the preview.")
        history.append({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "project": str(project or ""),
                        "mode": job.mode, "audio_s": round(job.duration, 2), "stt_s": round(t1 - t0, 2),
                        "cleanup_s": round(t2 - t1, 2), "whisper": WHISPER_MODEL, "claude": CLAUDE_MODEL,
                        "raw": raw, "cleaned": cleaned, "final": final, "warning": warning, "delivery": result})

    def repaste(self) -> str:
        """Pastes the last dictation again into whatever window is focused now."""
        text = self.last_text or history.last_text()
        if not text:
            self.notify("nothing", "No dictation yet.")
            return "nothing to paste"
        threading.Thread(target=self._repaste, args=(text,), daemon=True).start()
        return "pasting"

    def _repaste(self, text: str) -> None:
        if deliver(text, DELIVERY, self.notify) == "pasted":
            self.notify("pasted", text)

    # ------------------------------------------------------------ interface

    def status(self) -> dict:
        rec, cur = self.recording, self.current
        state = "recording" if rec else "processing" if self.pending else "idle"
        job = rec or cur
        return {
            "state": state, "stage": self.stage, "stage_text": STAGE_TEXT.get(self.stage, ""),
            "pending": self.pending,
            "mode": job.mode if job else default_mode(), "default_mode": default_mode(),
            "preview": job.show_preview if job else preview_enabled(),
            "autostop": rec.autostop if rec else autostop_enabled(),
            "seconds": round(rec.recorder.seconds) if rec else 0,
            "project": str(resolve_project() or ""), "whisper": WHISPER_MODEL,
            "recording": bool(rec), "busy": bool(self.pending),
        }

    def handle(self, req: dict) -> str:
        with self.lock:
            cmd = req.get("cmd")
            if cmd == "toggle":
                if self.recording:
                    return self.stop()
                return self.start(req.get("mode") or default_mode(), req.get("project"), req.get("preview"))
            if cmd == "start":
                return self.start(req.get("mode") or default_mode(), req.get("project"), req.get("preview"))
            if cmd == "stop":
                return self.stop(req.get("job"), req.get("auto", False))
            if cmd == "cancel":
                return self.cancel()
            if cmd == "repaste":
                return self.repaste()
            if cmd == "status":
                return json.dumps(self.status())
            if cmd == "notice":  # settings changed from the CLI or tray: reuse the service's bubble
                self.notify(req.get("state", "setting"), req.get("body", ""))
                return "ok"
            return f"unknown command {cmd!r}"

    def serve(self) -> None:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        SOCK.unlink(missing_ok=True)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(SOCK))
        os.chmod(SOCK, 0o600)
        srv.listen(8)
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
