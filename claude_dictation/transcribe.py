"""Speech to text with faster-whisper, steered by the project's glossary."""
from __future__ import annotations

import time
from pathlib import Path

from .audio import load_wav
from .config import LANGUAGE, WHISPER_BEAM, WHISPER_COMPUTE, WHISPER_DEVICE, WHISPER_MODEL, WHISPER_THREADS, log


class Transcriber:
    """Holds the Whisper model; load once (about 1 s) and reuse."""

    def __init__(self) -> None:
        from faster_whisper import WhisperModel

        t = time.monotonic()
        self.model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE,
                                  compute_type=WHISPER_COMPUTE, cpu_threads=WHISPER_THREADS)
        log(f"loaded Whisper {WHISPER_MODEL} ({WHISPER_DEVICE}/{WHISPER_COMPUTE}) in {time.monotonic() - t:.1f}s")

    def __call__(self, path: Path, terms: list[str]) -> str:
        audio = load_wav(path)
        # The glossary goes in as "text said just before", which biases Whisper toward those spellings.
        hint = ("Glossary: " + ", ".join(terms))[:600] if terms else None
        segments, _ = self.model.transcribe(
            audio, language=LANGUAGE or None, beam_size=WHISPER_BEAM,
            vad_filter=True, initial_prompt=hint, condition_on_previous_text=False)
        text = " ".join(s.text.strip() for s in segments).strip()
        if hint and text.startswith("Glossary:"):  # hallucinated echo of the hint on silence
            return ""
        return text
