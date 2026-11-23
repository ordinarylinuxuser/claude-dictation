"""Recording with pw-record, pause detection for auto-stop, and reading the WAV back."""
from __future__ import annotations

import array
import math
import signal
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path
from typing import Callable

from .config import AUTOSTOP_SECONDS, SOURCE, SPEECH_LEVEL

RATE = 16000
FRAME_SECONDS = 0.1
FRAME_BYTES = int(RATE * FRAME_SECONDS) * 2  # 16-bit mono


def rms(samples: array.array) -> float:
    return math.sqrt(sum(x * x for x in samples) / len(samples)) / 32768 if samples else 0.0


class PauseDetector:
    """Says when a pause follows speech. Fed one RMS value per 100 ms frame.

    Speech is any frame above the threshold: the configured level, raised above the quietest
    background noise seen so far so a noisy room doesn't count as talking.
    """

    def __init__(self, pause_seconds: float, speech_level: float = SPEECH_LEVEL) -> None:
        self.pause_frames = max(1, round(pause_seconds / FRAME_SECONDS))
        self.speech_level = speech_level
        self.noise = None
        self.heard_speech = False
        self.quiet = 0

    def feed(self, level: float) -> bool:
        self.noise = level if self.noise is None else min(self.noise, level)
        threshold = max(self.speech_level, self.noise * 4)
        if level > threshold:
            self.heard_speech, self.quiet = True, 0
        elif self.heard_speech:
            self.quiet += 1
        return self.heard_speech and self.quiet >= self.pause_frames


class Recorder:
    """pw-record into a 16 kHz mono WAV. Optionally calls on_pause when you stop talking."""

    def __init__(self, wav: Path, on_pause: Callable[[], None] | None = None) -> None:
        self.wav = wav
        self.on_pause = on_pause
        self.proc: subprocess.Popen | None = None
        self.started = 0.0
        self._done = threading.Event()

    def start(self) -> None:
        cmd = ["pw-record", "--rate", str(RATE), "--channels", "1", "--format", "s16"]
        if SOURCE:
            cmd += ["--target", SOURCE]
        self.proc = subprocess.Popen([*cmd, str(self.wav)], stdout=subprocess.DEVNULL, stderr=sys.stderr)
        self.started = time.monotonic()
        if self.on_pause and AUTOSTOP_SECONDS > 0:
            threading.Thread(target=self._watch, daemon=True).start()

    @property
    def seconds(self) -> float:
        return time.monotonic() - self.started

    def stop(self) -> float:
        """Stops recording and returns its length in seconds."""
        self._done.set()
        dur = self.seconds
        if self.proc:
            self.proc.send_signal(signal.SIGINT)  # pw-record finalises the WAV header on SIGINT
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
        return dur

    def _watch(self) -> None:
        """Reads the growing WAV file and calls on_pause once speech is followed by a pause."""
        detector = PauseDetector(AUTOSTOP_SECONDS)
        data_at, buf = None, b""
        while not self._done.wait(FRAME_SECONDS):
            try:
                with self.wav.open("rb") as f:
                    if data_at is None:
                        head = f.read(512)
                        i = head.find(b"data")
                        if i < 0:
                            continue
                        data_at = i + 8
                    f.seek(data_at)
                    chunk = f.read()
            except OSError:
                continue
            data_at += len(chunk)
            buf += chunk
            while len(buf) >= FRAME_BYTES:
                frame, buf = array.array("h", buf[:FRAME_BYTES]), buf[FRAME_BYTES:]
                if detector.feed(rms(frame)):
                    if not self._done.is_set() and self.on_pause:
                        self.on_pause()
                    return


def peak_level(path: Path) -> float:
    """Loudest sample, 0..1. Pure digital silence (0.0) means no audio reaches this machine."""
    with wave.open(str(path)) as w:
        raw = w.readframes(w.getnframes())
    try:
        import numpy as np

        data = np.frombuffer(raw, dtype=np.int16)
        return float(np.abs(data.astype(np.int32)).max()) / 32768 if data.size else 0.0
    except ImportError:
        samples = array.array("h", raw)
        return max((abs(x) for x in samples), default=0) / 32768


def load_wav(path: Path):
    """16 kHz mono float32 numpy array, as faster-whisper wants it."""
    import numpy as np

    with wave.open(str(path)) as w:
        if w.getframerate() != RATE or w.getsampwidth() != 2:
            raise ValueError("expected 16 kHz 16-bit WAV (record with --rate 16000 --format s16)")
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        if w.getnchannels() > 1:
            data = data.reshape(-1, w.getnchannels()).mean(axis=1)
    return data.astype(np.float32) / 32768.0
