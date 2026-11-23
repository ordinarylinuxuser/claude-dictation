"""Claude Dictation: push-to-talk dictation for Claude Code on KDE Wayland.

Pipeline: pw-record -> faster-whisper (local) -> Claude Haiku cleanup with project context ->
optional preview -> paste into the window you dictated for.

Modules import only the standard library at load time, so quick commands such as the Claude Code
hook stay fast; numpy, faster-whisper, anthropic and GTK are imported where they are used.
"""
