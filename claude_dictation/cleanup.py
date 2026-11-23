"""Claude Haiku turns the transcript into the prompt you meant. Structure mode returns JSON we lay out."""
from __future__ import annotations

import json
import os
import threading

from .config import CLAUDE_MODEL, CONFIG_DIR, PROMPT_FILE, STRUCTURE_EFFORT, log

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


def load_system_prompt() -> str:
    override = CONFIG_DIR / "system-prompt.md"
    return (override if override.is_file() else PROMPT_FILE).read_text()


def api_key() -> str | None:
    # A dictation-only key, kept out of ANTHROPIC_API_KEY so Claude Code keeps using your claude.ai
    # login (an exported ANTHROPIC_API_KEY would take precedence there).
    key = os.environ.get("DICTATE_ANTHROPIC_API_KEY")
    if key:
        return key.strip()
    f = CONFIG_DIR / "anthropic_key"
    return f.read_text().strip() if f.is_file() else None


class Cleaner:
    """Keeps one API client, so its HTTPS connection is reused instead of re-opened per dictation."""

    def __init__(self) -> None:
        self._client = None
        self._key: str | None = None
        self._lock = threading.Lock()

    def _get_client(self):
        import anthropic

        key = api_key()
        with self._lock:
            if self._client is None or key != self._key:  # a changed key file takes effect at once
                kwargs = {"api_key": key} if key else {}
                self._client = anthropic.Anthropic(timeout=20.0, max_retries=1, **kwargs)
                self._key = key
            return self._client

    def warm(self) -> None:
        """Opens the HTTPS connection while you're still talking (a free metadata call, no tokens)."""
        def run() -> None:
            try:
                self._get_client().models.retrieve(CLAUDE_MODEL)
            except Exception as e:  # warming is best effort; the real call reports problems
                log(f"warm-up skipped: {type(e).__name__}")
        threading.Thread(target=run, daemon=True).start()

    def __call__(self, transcript: str, context: str, mode: str = "clean") -> tuple[str, str | None]:
        """Returns (text, warning). Falls back to the raw transcript on any API problem."""
        transcript = transcript.strip()
        if mode == "raw" or not transcript:
            return transcript, None
        import anthropic

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
            resp = self._get_client().messages.create(
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
