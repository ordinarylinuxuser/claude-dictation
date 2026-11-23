"""The clipboard: rich (HTML + Markdown) through clip/wl-copy-multi, plain wl-copy otherwise."""
from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import time

from .config import REPO_DIR, RICH_PASTE, log


def _inline_html(s: str) -> str:
    s = html.escape(s, quote=False)
    s = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", s)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)


_ITEM_RE = re.compile(r"^(\s*)([-*]|\d+[.)])\s+(.*)$")


def md_to_html(md: str) -> str:
    """The small Markdown subset the cleanup produces: paragraphs, (nested) - and 1. lists, **bold**, `code`."""
    out = ['<meta charset="utf-8">']
    for block in re.split(r"\n\s*\n", md.strip()):
        lines = [l for l in block.split("\n") if l.strip()]
        if not lines:
            continue
        if not _ITEM_RE.match(lines[0]):
            out.append("<p>" + "<br>".join(_inline_html(l.strip()) for l in lines) + "</p>")
            continue
        items: list[list] = []
        for l in lines:
            m = _ITEM_RE.match(l)
            if m:
                items.append([len(m.group(1).expandtabs(4)), "ol" if m.group(2)[0].isdigit() else "ul", m.group(3)])
            else:
                items[-1][2] += " " + l.strip()
        stack: list[tuple[int, str]] = []
        for indent, tag, text in items:
            if stack and indent > stack[-1][0]:
                out.append(f"<{tag}>")
                stack.append((indent, tag))
            else:
                while stack and indent < stack[-1][0]:
                    out.append(f"</li></{stack.pop()[1]}>")
                if stack and stack[-1][1] == tag:
                    out.append("</li>")
                else:
                    if stack:
                        out.append(f"</li></{stack.pop()[1]}>")
                    out.append(f"<{tag}>")
                    stack.append((indent, tag))
            out.append(f"<li>{_inline_html(text)}")
        while stack:
            out.append(f"</li></{stack.pop()[1]}>")
    return "".join(out)


def clip_helper() -> str | None:
    for p in (REPO_DIR / "clip" / "wl-copy-multi", shutil.which("wl-copy-multi")):
        if p and os.access(p, os.X_OK):
            return str(p)
    return None


def set_clipboard(text: str) -> None:
    helper = clip_helper() if RICH_PASTE else None
    if helper:
        plain = text.encode()
        payload = b""
        for mime in ("text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "STRING", "TEXT"):
            payload += mime.encode() + b"\0" + plain + b"\0"
        payload += b"text/html\0" + md_to_html(text).encode() + b"\0"
        proc = subprocess.Popen([helper], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)  # keeps serving after we return
        proc.stdin.write(payload)
        proc.stdin.close()
        for _ in range(20):  # wait until the compositor shows our selection
            r = subprocess.run(["wl-paste", "--list-types"], capture_output=True, text=True, timeout=2)
            if "text/html" in r.stdout:
                return
            time.sleep(0.05)
        log("wl-copy-multi did not take the clipboard; falling back to wl-copy")
    subprocess.run(["wl-copy"], input=text.encode(), stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=True)


def get_clipboard() -> bytes | None:
    r = subprocess.run(["wl-paste", "-n"], capture_output=True, timeout=2)
    return r.stdout if r.returncode == 0 else None


def restore_clipboard(data: bytes) -> None:
    subprocess.run(["wl-copy"], input=data, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
