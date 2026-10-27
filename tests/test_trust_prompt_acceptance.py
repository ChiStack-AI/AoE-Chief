"""Acceptance coverage for recognizing and safely answering worker trust prompts."""
import json
import unittest
from unittest import mock

import chief


CLAUDE_QUESTION = "Is this a project you created or one you trust?"
QUESTIONS = (
    "Trust this folder?",
    "Do you trust the files in this folder?",
    "Do you trust the files in this folder",
    CLAUDE_QUESTION,
)
READY = "Ask Codex to do anything\n"


def trust_screen(question, selected=1):
    return "\n".join((
        "Accessing workspace: /tmp/chief-acceptance",
        "",
        "  " + question,
        "",
        ("❯" if selected == 1 else " ") + " 1. Yes, I trust this folder",
        ("❯" if selected == 2 else " ") + " 2. No, exit",
        "",
        "Enter to confirm · Esc to cancel",
    ))


class TrustPromptAcceptanceTests(unittest.TestCase):
    def wait(self, screens, trust=True, selected=1, target="aoe_acceptance_worker"):
        """Drive real wait_ready with a fake terminal, clock, and trust menu.

        Record the option actually confirmed, so a bare Enter cannot masquerade
        as selecting Yes when the menu currently highlights No. Menu navigation
        and numeric shortcuts are both accepted; no particular key sequence is
        prescribed by the tests.
        """
        frames = iter(screens)
        clock = [0.0]
        menu = [selected]
        self.answers = []
        self.keys = []

        def sleep(seconds):
            clock[0] += seconds

        def send(command, **kwargs):
            self.assertEqual(command[:4], ["tmux", "send-keys", "-t", target])
            for key in command[4:]:
                self.keys.append(key)
                if key in ("1", "Home"):
                    menu[0] = 1
                elif key in ("2", "End"):
                    menu[0] = 2
                elif key in ("Up", "Down"):
                    menu[0] = 3 - menu[0]
                elif key in ("Enter", "C-m"):
                    self.answers.append(menu[0])
                else:
                    self.fail("Unexpected trust-menu key: {!r}".format(key))
            return ""

        with mock.patch.object(chief, "aoe", return_value=json.dumps({"status": "waiting"})), \
                mock.patch.object(chief, "screen", side_effect=lambda *args: next(frames, screens[-1])), \
                mock.patch.object(chief, "tmux_target", return_value=target) as find_target, \
                mock.patch.object(chief, "call", side_effect=send) as call, \
                mock.patch.object(chief.time, "time", side_effect=lambda: clock[0]), \
                mock.patch.object(chief.time, "sleep", side_effect=sleep):
            result = chief.wait_ready({"profile": "acceptance"}, "worker-id", trust, timeout=12)
        self.find_target = find_target
        self.call = call
        return result

    def test_regex_recognizes_supported_standalone_prompt_lines(self):
        for question in QUESTIONS:
            for line in (question, "  " + question, "│ " + question + " │"):
                with self.subTest(line=line):
                    self.assertIsNotNone(chief.TRUST_RE.search("Workspace\n" + line + "\n"))

    def test_enabled_trust_answers_each_supported_prompt_and_reaches_ready(self):
        for question in QUESTIONS:
            with self.subTest(question=question):
                result = self.wait([trust_screen(question), READY])
                self.assertEqual(self.answers, [1], "the affirmative trust option must be confirmed")
                self.assertEqual(result, "ready")

    def test_new_claude_prompt_explicitly_selects_yes_when_no_is_highlighted(self):
        result = self.wait([trust_screen(CLAUDE_QUESTION, selected=2), READY], selected=2)
        self.assertEqual(self.answers, [1], "Enter alone would confirm No; explicitly select Yes first")
        self.assertEqual(result, "ready")

    def test_disabled_trust_returns_trust_without_sending_any_keys(self):
        for question in QUESTIONS:
            with self.subTest(question=question):
                result = self.wait([trust_screen(question), READY], trust=False)
                self.assertEqual(self.keys, [])
                self.call.assert_not_called()
                self.find_target.assert_not_called()
                self.assertEqual(result, "trust")

    def test_repeated_prompt_is_answered_only_once(self):
        for question in QUESTIONS:
            with self.subTest(question=question):
                prompt = trust_screen(question)
                result = self.wait([prompt, prompt, READY])
                self.assertEqual(self.answers, [1])
                self.assertEqual(result, "trust", "a second prompt needs manual intervention")

    def test_one_answer_safeguard_is_shared_across_prompt_wordings(self):
        for first, second in ((QUESTIONS[0], CLAUDE_QUESTION), (CLAUDE_QUESTION, QUESTIONS[0])):
            with self.subTest(first=first, second=second):
                result = self.wait([trust_screen(first), trust_screen(second), READY])
                self.assertEqual(self.answers, [1])
                self.assertEqual(result, "trust")

    def test_missing_tmux_target_never_sends_keys(self):
        for question in QUESTIONS:
            with self.subTest(question=question):
                self.assertEqual(self.wait([trust_screen(question)], target=None), "timeout")
                self.assertEqual(self.keys, [])
                self.call.assert_not_called()

    def test_prose_and_quoted_questions_are_not_trust_prompts(self):
        prose = [
            "Trust is important when reviewing a project.",
            "Only trust files after you have reviewed them.",
        ]
        for question in QUESTIONS:
            prose.extend((
                'The documentation quotes "' + question + '" as an example.',
                "Earlier the agent asked: " + question,
                question + " is an example of a startup question.",
                '"' + question + '"',
                "'" + question + "'",
                "`" + question + "`",
                "> " + question,
            ))
        for text in prose:
            with self.subTest(text=text):
                self.assertIsNone(chief.TRUST_RE.search(text))

    def test_prose_does_not_block_readiness_or_trigger_keys_with_either_trust_setting(self):
        for question in QUESTIONS:
            for trust in (False, True):
                with self.subTest(question=question, trust=trust):
                    text = 'The documentation quotes "' + question + '" as an example.\n' + READY
                    result = self.wait([text, READY], trust=trust)
                    self.assertEqual(self.keys, [])
                    self.call.assert_not_called()
                    self.find_target.assert_not_called()
                    self.assertEqual(result, "ready")


if __name__ == "__main__":
    unittest.main()
