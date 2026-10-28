"""Regression coverage using a real, ANSI-stripped Claude Code trust screen."""
import json
import unittest
from unittest import mock

import chief


QUESTION = "Is this a project you created or one you trust?"
REAL_SCREEN = """ Accessing workspace:
 /tmp/new-project
 Quick safety check: Is this a project you created or one you trust? (Like your own code, a well-known open source project, or work from your team). If not, take a moment to review what's in this folder first.
 Claude Code'll be able to read, edit, and execute files here.
 Security guide
 ❯ No, exit
   Yes, I trust this folder
 Enter to confirm · Esc to cancel
"""
READY = "────────────────────────────────────────\n❯ \n? for shortcuts\n"
NO_SELECTED = " ❯ No, exit\n   Yes, I trust this folder\n"
YES_SELECTED = "   No, exit\n ❯ Yes, I trust this folder\n"


class ClaudeMenu:
    """An unnumbered menu whose captures follow the actual arrow-key state."""
    def __init__(self, test, screen=REAL_SCREEN, arrows_work=True, stale_captures=0):
        self.test = test
        self.visible = screen
        self.arrows_work = arrows_work
        self.stale_captures = stale_captures
        self.pending = None
        self.keys = []
        self.events = []
        self.last_capture = None
        self.confirmed = False

    def capture(self, *args):
        if self.pending is not None:
            if self.stale_captures:
                self.stale_captures -= 1
            else:
                self.visible, self.pending = self.pending, None
        self.last_capture = READY if self.confirmed else self.visible
        self.events.append(("capture", self.last_capture))
        return self.last_capture

    def send(self, command, **kwargs):
        self.test.assertEqual(command[:4], ["tmux", "send-keys", "-t", "claude-trust-test"])
        for key in command[4:]:
            self.keys.append(key)
            self.events.append(("key", key))
            if key in ("Down", "Up"):
                if self.arrows_work and self.pending is None:
                    if NO_SELECTED in self.visible:
                        self.pending = self.visible.replace(NO_SELECTED, YES_SELECTED)
                    elif " ❯ No, exit\n" in self.visible:
                        self.pending = self.visible.replace(
                            "   Yes, I trust this folder\n ❯ No, exit\n",
                            " ❯ Yes, I trust this folder\n   No, exit\n")
            elif key in ("Enter", "C-m"):
                self.test.assertIn(" ❯ Yes, I trust this folder\n", self.visible,
                                   "never confirm while No is highlighted")
                self.test.assertIn(" ❯ Yes, I trust this folder\n", self.last_capture,
                                   "a fresh capture must confirm the Yes highlight")
                self.test.assertFalse(self.confirmed, "the prompt may only be answered once")
                self.confirmed = True
            else:
                self.test.fail("Unnumbered trust menu received unsupported key {!r}".format(key))
        return ""


class RealClaudeTrustPromptTests(unittest.TestCase):
    def wait(self, menu=None, trust=True):
        self.menu = menu or ClaudeMenu(self)
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds

        with mock.patch.object(chief, "aoe", return_value=json.dumps({"status": "waiting"})), \
                mock.patch.object(chief, "screen", side_effect=self.menu.capture) as capture, \
                mock.patch.object(chief, "tmux_target", return_value="claude-trust-test") as target, \
                mock.patch.object(chief, "call", side_effect=self.menu.send) as call, \
                mock.patch.object(chief.time, "time", side_effect=lambda: clock[0]), \
                mock.patch.object(chief.time, "sleep", side_effect=sleep):
            result = chief.wait_ready({"profile": "test"}, "claude-worker", trust, timeout=30)
        self.capture, self.target, self.call = capture, target, call
        return result

    def test_exact_real_screen_is_recognized(self):
        self.assertIsNotNone(chief.TRUST_RE.search(REAL_SCREEN))

    def test_prefix_and_parenthetical_are_independently_optional(self):
        for prefix in ("", "Quick safety check: "):
            for suffix in ("", " (Like your own code). Review unfamiliar files first."):
                for frame in (" {} ", "  │ {} │  ", "  ┃ {} ┃  "):
                    line = frame.format(prefix + QUESTION + suffix)
                    with self.subTest(line=line):
                        self.assertIsNotNone(chief.TRUST_RE.search(line))

    def test_arrows_select_yes_and_fresh_capture_confirms_before_enter(self):
        self.assertEqual(self.wait(), "ready")
        self.assertEqual(self.menu.keys, ["Down", "Enter"])
        down = self.menu.events.index(("key", "Down"))
        enter = self.menu.events.index(("key", "Enter"))
        self.assertTrue(any(kind == "capture" and YES_SELECTED in screen
                            for kind, screen in self.menu.events[down + 1:enter]))
        self.assertTrue(self.menu.confirmed)

    def test_disabled_trust_sends_nothing(self):
        self.assertEqual(self.wait(trust=False), "trust")
        self.assertEqual(self.menu.keys, [])
        self.call.assert_not_called()
        self.target.assert_not_called()

    def test_ignored_arrows_give_up_without_enter(self):
        self.assertEqual(self.wait(ClaudeMenu(self, arrows_work=False)), "trust")
        self.assertGreater(len(self.menu.keys), 0)
        self.assertLessEqual(len(self.menu.keys), 3, "selection retries must be bounded")
        self.assertTrue(all(key == "Down" for key in self.menu.keys))
        self.assertFalse(self.menu.confirmed)

    def test_stale_capture_never_causes_enter_on_no(self):
        self.assertEqual(self.wait(ClaudeMenu(self, stale_captures=1)), "ready")
        self.assertEqual(self.menu.keys.count("Enter"), 1)
        self.assertTrue(self.menu.confirmed)

    def test_yes_already_highlighted_needs_no_navigation(self):
        menu = ClaudeMenu(self, screen=REAL_SCREEN.replace(NO_SELECTED, YES_SELECTED))
        self.assertEqual(self.wait(menu), "ready")
        self.assertEqual(self.menu.keys, ["Enter"])

    def test_reversed_menu_uses_up_to_reach_yes(self):
        screen = REAL_SCREEN.replace(NO_SELECTED, "   Yes, I trust this folder\n ❯ No, exit\n")
        self.assertEqual(self.wait(ClaudeMenu(self, screen=screen)), "ready")
        self.assertEqual(self.menu.keys, ["Up", "Enter"])

    def test_legacy_questions_also_never_confirm_a_highlighted_no(self):
        original_line = REAL_SCREEN.splitlines()[2]
        for question in ("Trust this folder?", "Do you trust the files in this folder?"):
            with self.subTest(question=question):
                screen = REAL_SCREEN.replace(original_line, " " + question)
                self.assertEqual(self.wait(ClaudeMenu(self, screen=screen)), "ready")
                self.assertEqual(self.menu.keys, ["Down", "Enter"])

    def test_disappearing_menu_after_arrow_returns_trust_without_enter(self):
        menu = ClaudeMenu(self)
        capture = menu.capture
        menu.capture = lambda *args: "" if menu.keys else capture(*args)
        self.assertEqual(self.wait(menu), "trust")
        self.assertEqual(self.menu.keys, ["Down"])
        self.assertFalse(self.menu.confirmed)

    def test_missing_yes_option_returns_trust_without_keys(self):
        screen = REAL_SCREEN.replace("   Yes, I trust this folder\n", "")
        self.assertEqual(self.wait(ClaudeMenu(self, screen=screen)), "trust")
        self.assertEqual(self.menu.keys, [])

    def test_missing_highlight_returns_trust_without_keys(self):
        screen = REAL_SCREEN.replace(" ❯ No, exit\n", "   No, exit\n")
        self.assertEqual(self.wait(ClaudeMenu(self, screen=screen)), "trust")
        self.assertEqual(self.menu.keys, [])

    def test_new_wording_in_prose_or_quotes_is_not_a_prompt(self):
        for line in (
                '"Quick safety check: ' + QUESTION + ' (Like your own code)"',
                "> Quick safety check: " + QUESTION + " (Like your own code)",
                "Earlier the agent asked: " + QUESTION + " (Like your own code)",
                "Quick safety check: " + QUESTION + " is an example of a question.",
                "We trust our team and review outside code."):
            with self.subTest(line=line):
                self.assertIsNone(chief.TRUST_RE.search(line))


if __name__ == "__main__":
    unittest.main()
