"""Regression coverage using a real, ANSI-stripped Codex 0.165 trust screen."""
import json
import unittest
from unittest import mock

import chief


REAL_SCREEN = """  Folder access
  /tmp/codexprobe2-1793196696
  Trust this folder? Codex can read, edit, and run files here, subject to your permission settings. Folder settings can run code automatically, even without a model request. Continue only if you trust these files. Your trust decision will be saved.
› 1. Trust and continue
  2. Quit
  enter continue · esc quit
"""
QUIT_SELECTED = REAL_SCREEN.replace("› 1. Trust and continue\n  2. Quit\n", "  1. Trust and continue\n› 2. Quit\n")
READY = "› Ask Codex to do anything\n"


class CodexMenu:
    """A numbered menu: arrows move the highlight, Enter confirms it. Digits are refused: they may confirm at once."""
    def __init__(self, test, screen=REAL_SCREEN):
        self.test, self.visible, self.keys, self.confirmed = test, screen, [], False

    def capture(self, *args):
        return READY if self.confirmed else self.visible

    def send(self, command, **kwargs):
        self.test.assertEqual(command[:4], ["tmux", "send-keys", "-t", "codex-trust-test"])
        for key in command[4:]:
            self.keys.append(key)
            if key == "Up":
                self.visible = REAL_SCREEN
            elif key == "Down":
                self.visible = QUIT_SELECTED
            elif key in ("Enter", "C-m"):
                self.test.assertIn("› 1. Trust and continue", self.visible, "never confirm while Quit is highlighted")
                self.test.assertFalse(self.confirmed, "the prompt may only be answered once")
                self.confirmed = True
            else:
                self.test.fail("unexpected key {!r}".format(key))
        return ""


class RealCodexTrustPromptTests(unittest.TestCase):
    def wait(self, screen=REAL_SCREEN, trust=True):
        self.menu = CodexMenu(self, screen)
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds

        with mock.patch.object(chief, "aoe", return_value=json.dumps({"status": "waiting"})), \
                mock.patch.object(chief, "screen", side_effect=self.menu.capture), \
                mock.patch.object(chief, "tmux_target", return_value="codex-trust-test"), \
                mock.patch.object(chief, "call", side_effect=self.menu.send), \
                mock.patch.object(chief.time, "time", side_effect=lambda: clock[0]), \
                mock.patch.object(chief.time, "sleep", side_effect=sleep):
            return chief.wait_ready({"profile": "test"}, "codex-worker", trust, timeout=30)

    def test_exact_real_screen_is_recognized(self):
        self.assertIsNotNone(chief.TRUST_RE.search(REAL_SCREEN))

    def test_question_with_trailing_text_needs_the_trust_menu(self):
        line = "Trust this folder? Codex can read, edit, and run files here."
        self.assertIsNone(chief.TRUST_RE.search(line + "\n"))
        self.assertIsNone(chief.TRUST_RE.search("Trust this folder? is an example of a startup question.\n"))

    def test_disabled_trust_reports_trust_and_sends_nothing(self):
        self.assertEqual(self.wait(trust=False), "trust")
        self.assertEqual(self.menu.keys, [])

    def test_enabled_trust_confirms_trust_and_continue(self):
        self.assertEqual(self.wait(), "ready")
        self.assertEqual(self.menu.keys, ["Enter"])
        self.assertTrue(self.menu.confirmed)

    def test_quit_highlighted_is_never_confirmed(self):
        self.assertEqual(self.wait(QUIT_SELECTED), "ready")
        self.assertEqual(self.menu.keys, ["Up", "Enter"])
        self.assertTrue(self.menu.confirmed)


if __name__ == "__main__":
    unittest.main()
