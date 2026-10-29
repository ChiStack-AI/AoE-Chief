"""Tests for the "commands" config: launch a tool through a custom command such as claude-proxy."""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ.setdefault("AOE_CHIEF_HOME", tempfile.mkdtemp())
import chief  # noqa: E402

class T(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        subprocess.run(["git", "-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@l",
                        "commit", "-q", "--allow-empty", "-m", "init"], check=True)
        self.mix = {"profile": "main", "commands": {"claude": "claude-proxy"}}

    def task(self, tool):
        return {"id": "demo", "repo": self.repo, "name": "demo", "branch": "chief/demo", "tool": tool}

    def test_no_config_keeps_the_default_command(self):
        cmd = chief.add_command({"profile": "main"}, self.task("claude"))
        self.assertNotIn("--cmd-override", cmd)
        self.assertNotIn("command", chief.pick_model({"profile": "main"}, "claude", 1))

    def test_new_task_uses_the_configured_command(self):
        cmd = chief.add_command(self.mix, self.task("claude"))
        self.assertEqual(cmd[cmd.index("--cmd-override") + 1], "claude-proxy")

    def test_other_tools_are_unaffected(self):
        self.assertNotIn("--cmd-override", chief.add_command(self.mix, self.task("codex")))

    def test_blank_command_is_ignored(self):
        self.assertNotIn("--cmd-override", chief.add_command({"profile": "main", "commands": {"claude": " "}},
                                                             self.task("claude")))

    def test_rotation_uses_the_configured_command(self):
        t = dict(self.task("claude"), session_id="a" * 16, worktree=self.repo)
        seen = []

        def fake_call(cmd, **kw):
            seen.append(cmd)
            raise RuntimeError("stop after aoe add")
        with mock.patch.object(chief, "call", fake_call):
            with self.assertRaises(RuntimeError):
                chief.rotate_task(self.mix, t, {}, "test")
        self.assertIn("add", seen[0])
        self.assertEqual(seen[0][seen[0].index("--cmd-override") + 1], "claude-proxy")

    def test_ai_calls_use_the_configured_command(self):
        spec = chief.pick_model(self.mix, "claude", 1)
        self.assertEqual(spec["command"], "claude-proxy")
        seen = []

        def fake_call(cmd, **kw):
            seen.append(cmd)
            return "ok"
        with mock.patch.object(chief, "call", fake_call), \
                mock.patch.object(chief.shutil, "which", lambda name: "/opt/bin/" + name):
            chief.generate_ai("hi", "claude", spec)
        self.assertEqual(seen[0][0], "/opt/bin/claude-proxy")
        self.assertIn("-p", seen[0])

    def test_command_path(self):
        self.assertEqual(chief.command_path("~/bin/x"), os.path.expanduser("~/bin/x"))
        self.assertIsNone(chief.command_path(None))

if __name__ == "__main__":
    unittest.main()
