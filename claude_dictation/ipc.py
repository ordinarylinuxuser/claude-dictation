"""Talking to the daemon over its Unix socket (one JSON request, one reply)."""
from __future__ import annotations

import json
import socket
import sys

from .config import APP, SOCK
from .notify import Notifier


def request(req: dict, timeout: float = 5) -> str | None:
    """None if the daemon isn't running."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(SOCK))
            s.sendall(json.dumps(req).encode())
            return s.recv(65536).decode()
    except OSError:
        return None


def status() -> dict | None:
    reply = request({"cmd": "status"}, timeout=1)
    try:
        return json.loads(reply) if reply else None
    except ValueError:
        return None


def notice(state: str, body: str) -> None:
    """Show a notification through the daemon's bubble when it runs, else directly."""
    if request({"cmd": "notice", "state": state, "body": body}, timeout=2) is None:
        Notifier()(state, body)


def send(req: dict) -> int:
    """For CLI commands: prints the reply, or explains that the service isn't running."""
    reply = request(req)
    if reply is None:
        msg = f"The dictation service isn't running. Start it with: systemctl --user start {APP}"
        print(msg, file=sys.stderr)
        Notifier()("unavailable", msg)
        return 1
    print(reply)
    return 0
