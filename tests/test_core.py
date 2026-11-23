"""Unit tests for the parts that don't need audio, a desktop or the API.

Run: python3 -m unittest discover -s tests
"""
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_dictation import clipboard, cleanup, deliver, project, windows  # noqa: E402
from claude_dictation.audio import FRAME_SECONDS, PauseDetector  # noqa: E402
from claude_dictation.notify import describe  # noqa: E402


class MarkdownToHtml(unittest.TestCase):
    def test_paragraphs_lists_and_inline(self):
        md = ("Lead with `code` & <tags>.\n\n1. First\n2. Second\n\n"
              "- **Name.** Text\n  - Sub one\n  - Sub two\n- **Other.** More\n\nClosing.")
        self.assertEqual(clipboard.md_to_html(md),
                         '<meta charset="utf-8"><p>Lead with <code>code</code> &amp; &lt;tags&gt;.</p>'
                         "<ol><li>First</li><li>Second</li></ol>"
                         "<ul><li><strong>Name.</strong> Text<ul><li>Sub one</li><li>Sub two</li></ul></li>"
                         "<li><strong>Other.</strong> More</li></ul><p>Closing.</p>")

    def test_switching_list_type_in_one_block(self):
        self.assertEqual(clipboard.md_to_html("- a\n1. b"),
                         '<meta charset="utf-8"><ul><li>a</li></ul><ol><li>b</li></ol>')


class StructureLayout(unittest.TestCase):
    def test_full(self):
        data = {"lead": "Two things.", "steps": ["Do A", " "], "closing": "Thoughts?",
                "points": [{"name": "Kit update.", "text": "Bump it.", "details": ["Read logs", ""]},
                           {"name": "", "text": "", "details": []}]}
        self.assertEqual(cleanup.render_structure(data),
                         "Two things.\n\n1. Do A\n\n- **Kit update.** Bump it.\n  - Read logs\n\nThoughts?")

    def test_lead_only(self):
        self.assertEqual(cleanup.render_structure({"lead": "Fix it.", "steps": [], "points": [], "closing": ""}),
                         "Fix it.")


class Glossary(unittest.TestCase):
    def test_terms(self):
        text = "- `orbitd`: the daemon\n- FreeOrbit: sibling\n* kubectl\n1. validateSession — helper\nplain line"
        self.assertEqual(project.glossary_terms(text), ["orbitd", "orbitd", "FreeOrbit", "kubectl", "validateSession"])

    def test_context_dedupes_and_reads_glossary(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "atlas"
            (p / ".claude").mkdir(parents=True)
            (p / ".claude" / "dictation.md").write_text("- `atlas`: me\n- Orbit: thing\n")
            ctx, terms = project.project_context(p)
            self.assertIn('<glossary source=".claude/dictation.md">', ctx)
            self.assertEqual(terms, ["atlas", "Orbit"])


class Pauses(unittest.TestCase):
    def frames(self, det, levels):
        return [det.feed(x) for x in levels]

    def test_stops_after_pause_following_speech(self):
        det = PauseDetector(pause_seconds=0.5, speech_level=0.01)
        n = round(0.5 / FRAME_SECONDS)
        out = self.frames(det, [0.001] * 10 + [0.05] * 5 + [0.002] * n)
        self.assertFalse(any(out[:-1]))
        self.assertTrue(out[-1])

    def test_silence_alone_never_stops(self):
        det = PauseDetector(pause_seconds=0.3, speech_level=0.01)
        self.assertFalse(any(self.frames(det, [0.0] * 100)))

    def test_noisy_room_raises_the_threshold(self):
        det = PauseDetector(pause_seconds=0.3, speech_level=0.01)
        # background at 0.02 would count as speech with a fixed threshold; 4x the noise floor doesn't
        self.assertFalse(any(self.frames(det, [0.02] * 50)))
        self.assertFalse(det.heard_speech)

    def test_short_breath_doesnt_stop(self):
        det = PauseDetector(pause_seconds=1.0, speech_level=0.01)
        self.assertFalse(any(self.frames(det, [0.05] * 5 + [0.001] * 5 + [0.05] * 5 + [0.001] * 5)))


class PasteKeys(unittest.TestCase):
    def test_by_window_class(self):
        self.assertTrue(windows.is_terminal("org.kde.konsole"))
        self.assertFalse(windows.is_terminal("com.anthropic.claude"))
        if deliver.PASTE_KEYS == "auto":
            self.assertEqual(deliver.keys_for("org.kde.konsole"), "ctrl+shift+v")
            self.assertEqual(deliver.keys_for("com.anthropic.claude"), "ctrl+v")
            self.assertEqual(deliver.keys_for(None), deliver.PASTE_KEYS_FALLBACK)


class Describe(unittest.TestCase):
    def test_line(self):
        self.assertEqual(describe("structure", False, Path("/x/atlas"), autostop=True),
                         "Structure mode · pastes directly · stops after a pause · atlas")


class Settings(unittest.TestCase):
    def test_switches_persist(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["XDG_STATE_HOME"] = d
            try:
                from claude_dictation import config
                config = importlib.reload(config)
                self.assertEqual(config.default_mode(), "clean")
                self.assertEqual(config.set_mode("next"), "structure")
                self.assertEqual(config.set_mode("next"), "raw")
                self.assertEqual(config.set_mode("next"), "clean")
                self.assertFalse(config.set_preview("off"))
                self.assertTrue(config.set_preview("toggle"))
                self.assertFalse(config.set_autostop("off"))
                self.assertFalse(config.autostop_enabled())
            finally:
                del os.environ["XDG_STATE_HOME"]
                importlib.reload(config)


if __name__ == "__main__":
    unittest.main()
