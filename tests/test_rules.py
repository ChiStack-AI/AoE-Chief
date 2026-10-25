"""Tests for chief's deterministic screen rules."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ.setdefault("AOE_CHIEF_HOME", tempfile.mkdtemp())
import chief  # noqa: E402

class T(unittest.TestCase):
    def test_approval_prompts(self):
        for prompt in ("Allow command?", "Approve this edit?", "Do you want to proceed?", "Continue? (y/n)"):
            self.assertTrue(chief.ASK_PATTERNS.search(prompt), prompt)
        for prose in ("all three tests pass with the approved plain text receipt", "Allowed values: a, b"):
            self.assertFalse(chief.ASK_PATTERNS.search(prose), prose)

    def test_prompt_words_inside_a_reply_are_prose(self):
        sc = ("  Decision needed: Approve the acceptance tests tests/test_x.py? Recommendation: A\n"
              "› Ask Codex to do anything\n  ? for shortcuts")
        self.assertTrue(chief.at_prompt("waiting", sc))

    def test_at_prompt_ignores_prose(self):
        sc = "Conclusion: done with the approved format\n\n› Ask Codex to do anything\n  ? for shortcuts"
        self.assertTrue(chief.at_prompt("waiting", sc))
        self.assertFalse(chief.at_prompt("waiting", sc + "\nAllow command? [y/n]"))

    def test_at_prompt_claude_auto_mode(self):
        rule = "─" * 60
        sc = ("● done\n\n" + rule + "\n❯\u00a0\n" + rule +
              "\n  me@host:~/repo | Opus | effort: high\n  ⏵⏵ auto mode on (shift+tab to cycle)")
        self.assertTrue(chief.at_prompt("idle", sc))
        self.assertFalse(chief.at_prompt("running", sc))
        self.assertFalse(chief.at_prompt("idle", "❯ an old prompt in the history\n● an answer"))

if __name__ == "__main__":
    unittest.main()
