"""Which project you're in, and the context and vocabulary it gives the cleanup and Whisper."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from .config import CONFIG_DIR, PROJECT_FILE, PROJECT_MAX_AGE_H, REPO_DIR, STATE_DIR


def read_head(path: Path, limit: int) -> str:
    text = path.read_text(errors="replace")
    return text if len(text) <= limit else text[:limit] + "\n[... truncated]"


def resolve_project(explicit: str | None = None) -> Path | None:
    """--project, then $DICTATE_PROJECT, then the project the Claude Code hook last recorded."""
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


def record_project_from_hook() -> int:
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
    """Backticked words, and the term at the start of each "- term: note" line."""
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


def glossary_path() -> Path:
    """The current project's glossary (created from the template if missing), else the personal one."""
    project = resolve_project()
    target = project / ".claude" / "dictation.md" if project else CONFIG_DIR / "glossary.md"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        template = REPO_DIR / "examples" / "dictation.md"
        target.write_text(template.read_text() if template.is_file() else "# Dictation glossary\n\n")
    return target
