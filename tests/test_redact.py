"""Secrets in agent transcripts and screens are masked before Chief stores, shows or forwards them."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ.setdefault("AOE_CHIEF_HOME", tempfile.mkdtemp())
import chief  # noqa: E402

SECRETS = {
    "密码是不是 Fake9Pass（c 和 n 对调）？": "Fake9Pass",
    "密码：Zx81!qq": "Zx81!qq",
    "the password is hunter2-Blue": "hunter2-Blue",
    "password: s3cr3t-Value": "s3cr3t-Value",
    "export API_KEY=sk-abcdefghijklmnopqrstuvwx": "sk-abcdefghijklmnopqrstuvwx",
    "token = abc123def456": "abc123def456",
    "use ghp_abcdefghijklmnopqrstuvwxyz0123456789 for the push": "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig": "eyJhbGciOiJIUzI1NiJ9.payload.sig",
    "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----": "b3BlbnNzaC1rZXktdjEAAAAA",
}

PROSE = [
    "the password prompt appeared twice",
    "密码没有确认",
    "throughput: 1234 tokens per second",
    "Decision needed: approve the acceptance tests tests/test_x.py? Recommendation: A",
    "Conclusion: done; see chief.py:1365",
]

class T(unittest.TestCase):
    def test_secrets_are_masked(self):
        for text, secret in SECRETS.items():
            with self.subTest(text=text):
                out = chief.redact(text)
                self.assertNotIn(secret, out)
                self.assertNotEqual(out, text)

    def test_context_around_a_secret_is_kept(self):
        out = chief.redact("密码是不是 Fake9Pass（c 和 n 对调）？")
        self.assertIn("密码", out)
        self.assertIn("（c 和 n 对调）", out)

    def test_ordinary_prose_is_unchanged(self):
        for text in PROSE:
            with self.subTest(text=text):
                self.assertEqual(chief.redact(text), text)

    def test_redaction_is_idempotent(self):
        for text in list(SECRETS) + PROSE:
            once = chief.redact(text)
            self.assertEqual(chief.redact(once), once)

    def test_claude_transcript_is_redacted(self):
        rows = [{"type": "user", "message": {"content": "密码：Zx81!qq"}, "timestamp": "2026-10-25T10:00:00Z"},
                {"type": "assistant", "timestamp": "2026-10-25T10:00:05Z",
                 "message": {"content": [{"type": "text", "text": "密码是不是 Fake9Pass？"}], "stop_reason": "end_turn"}}]
        dump = json.dumps(chief.claude_events(rows), ensure_ascii=False)
        self.assertNotIn("Zx81!qq", dump)
        self.assertNotIn("Fake9Pass", dump)

    def test_codex_transcript_is_redacted(self):
        rows = [{"timestamp": "2026-10-25T10:00:00Z", "type": "response_item",
                 "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "token = abc123def456"}]}},
                {"timestamp": "2026-10-25T10:00:05Z", "type": "response_item",
                 "payload": {"type": "message", "role": "assistant",
                             "content": [{"type": "output_text", "text": "the password is hunter2-Blue"}]}},
                {"timestamp": "2026-10-25T10:00:06Z", "type": "event_msg", "payload": {"type": "task_complete"}}]
        ev = chief.codex_events(rows)
        self.assertEqual([k for _, k, _ in ev], ["prompt", "reply", "done"], "the fixture must reach the parser")
        dump = json.dumps(ev, ensure_ascii=False)
        self.assertNotIn("abc123def456", dump)
        self.assertNotIn("hunter2-Blue", dump)

    def test_screen_capture_is_redacted(self):
        captured = json.dumps({"status": "idle", "content": "● 密码是不是 Fake9Pass？\n› Ask Codex to do anything"})
        with mock.patch.object(chief, "aoe", return_value=captured):
            sc = chief.screen({"profile": "main"}, "0123456789abcdef")
        self.assertNotIn("Fake9Pass", sc)
        self.assertIn("Ask Codex to do anything", sc)

if __name__ == "__main__":
    unittest.main()
