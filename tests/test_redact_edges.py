"""Additional boundary and ingestion checks for credential redaction."""
import copy
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ.setdefault("AOE_CHIEF_HOME", tempfile.mkdtemp())
import chief  # noqa: E402


class RedactEdges(unittest.TestCase):
    def assert_redacted(self, original, expected):
        self.assertEqual(chief.redact(original), expected)
        self.assertEqual(chief.redact(expected), expected, "redaction must be idempotent")

    def test_keyword_aliases_and_connectors(self):
        for label in ("password", "PASSWD", "密码", "口令", "TOKEN", "secret", "SECRET_KEY",
                      "API_KEY", "api-key", "api key", "apikey", "DB_PASSWORD", "ACCESS_TOKEN"):
            for connector in (" ", ": ", "：", "=", " is ", "是", "是不是 ", "为 "):
                with self.subTest(label=label, connector=connector):
                    prefix = label + connector
                    self.assert_redacted(prefix + "Ab12!x，context", prefix + "••••，context")

    def test_value_boundaries_and_surrounding_text(self):
        pairs = [
            ('{"password": "Ab12!x", "safe": "kept"}', '{"password": "••••", "safe": "kept"}'),
            ("password is `Ab12!x` and token=xx99yy.", "password is `••••` and token=••••."),
            ("**password**: 'ab cd!'; done", "**password**: '••••'; done"),
            ('password="abcde?"; done', 'password="••••"; done'),
            ('token="xx99yy', 'token="••••'),
            ("the shared secret token = abc123def456", "the shared secret token = ••••"),
            ("password=token=abc123def456", "password=••••"),
            ("password=abc123? Continue", "password=••••? Continue"),
            ("password:\nAb12!x\nContinue", "password:\n••••\nContinue"),
        ]
        for original, expected in pairs:
            with self.subTest(original=original):
                self.assert_redacted(original, expected)

    def test_short_values_plain_words_and_nonkeywords_are_kept(self):
        for text in ("", "password=abc12", "password=abcdef", "password reset completed",
                     "token count increased", "passwordless=abc123def456", "tokens=abc123def456",
                     "tokenizer: abc123def456", "notasecret=abc123def456", "密码没有确认",
                     "password\nConclusion: 123456 tests passed"):
            with self.subTest(text=text):
                self.assert_redacted(text, text)
        self.assert_redacted("password=abc123", "password=••••")
        self.assert_redacted("password=abcde!", "password=••••")

    def test_known_token_formats_without_keyword_labels(self):
        for token in ("sk-abcdefghijklmnop", "sk-proj-abcDEF123_456-xyz", "ghp_abcdefghijklmnop",
                      "github_pat_abcDEF123_456", "AKIA0123456789ABCDEF"):
            with self.subTest(token=token):
                self.assert_redacted("before (" + token + "), after", "before (••••), after")
        for token in ("abcdefgh", "abc.def123.signature", "ab+/cd123=="):
            with self.subTest(bearer=token):
                self.assert_redacted("Authorization: Bearer " + token + ". End",
                                     "Authorization: Bearer ••••. End")

    def test_private_key_bodies_and_truncated_capture(self):
        for kind in ("PRIVATE KEY", "RSA PRIVATE KEY", "EC PRIVATE KEY", "ENCRYPTED PRIVATE KEY",
                     "OPENSSH PRIVATE KEY"):
            for newline in ("\n", "\r\n"):
                with self.subTest(kind=kind, newline=newline):
                    begin = "-----BEGIN " + kind + "-----" + newline
                    end = newline + "-----END " + kind + "-----"
                    self.assert_redacted("before" + newline + begin + "AA11BB22" + end + newline + "after",
                                         "before" + newline + begin + "••••" + end + newline + "after")
                    self.assert_redacted(begin + "AA11BB22", begin + "••••")

    def test_claude_list_prompts_and_api_errors(self):
        rows = [
            {"type": "user", "timestamp": "t1",
             "message": {"content": [{"type": "text", "text": "password:"},
                                     {"type": "text", "text": "Ab12!x"}]}},
            {"type": "assistant", "timestamp": "t2", "isApiErrorMessage": True,
             "message": {"content": [{"type": "text", "text": "API_KEY=Ab12!x"}]}},
        ]
        original = copy.deepcopy(rows)
        self.assertEqual(chief.claude_events(rows), [("t1", "prompt", "password:\n••••"),
                                                   ("t2", "error", "API_KEY=••••")])
        self.assertEqual(rows, original, "redaction should not mutate transcript rows")

    def test_codex_errors_keep_metadata(self):
        rows = [
            {"timestamp": "t1", "type": "event_msg",
             "payload": {"type": "turn_aborted", "reason": "token=abc123def456"}},
            {"timestamp": "t2", "type": "event_msg",
             "payload": {"type": "error", "message": "password=Ab12!x"}},
        ]
        original = copy.deepcopy(rows)
        events = chief.codex_events(rows)
        self.assertEqual([(ts, kind) for ts, kind, _ in events], [("t1", "error"), ("t2", "error")])
        self.assertTrue(events[0][2].endswith("token=••••"))
        self.assertEqual(events[1][2], "password=••••")
        self.assertEqual(rows, original)

    def test_screen_absent_stopped_and_prose_behavior(self):
        for capture, expected in (({"status": "stopped", "content": "password=Ab12!x"}, None),
                                  ({"status": "idle", "content": "  "}, None),
                                  ({"status": "idle", "content": "ready to continue"}, "ready to continue")):
            with self.subTest(capture=capture):
                with mock.patch.object(chief, "aoe", return_value=json.dumps(capture)):
                    self.assertEqual(chief.screen({"profile": "main"}, "sid"), expected)


if __name__ == "__main__":
    unittest.main()
