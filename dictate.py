#!/usr/bin/env python3
"""Launcher for the `dictate` command; the code lives in claude_dictation/.

~/.local/bin/dictate links here, and the services run it, so the repo can live anywhere.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from claude_dictation.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
