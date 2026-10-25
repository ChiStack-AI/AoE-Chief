"""Tests for where a new task's branch starts: the local branch, not AoE's default (the remote's default branch)."""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ.setdefault("AOE_CHIEF_HOME", tempfile.mkdtemp())
import chief  # noqa: E402

def git(repo, *a):
    return subprocess.run(["git", "-C", repo] + list(a), check=True, universal_newlines=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()

class T(unittest.TestCase):
    def setUp(self):
        # origin/main has one commit; local main has an extra commit that was never pushed
        root = tempfile.mkdtemp()
        origin, self.repo = os.path.join(root, "origin.git"), os.path.join(root, "repo")
        subprocess.run(["git", "init", "-q", "--bare", origin], check=True)
        subprocess.run(["git", "clone", "-q", origin, self.repo], check=True, stderr=subprocess.PIPE)
        ident = ["-c", "user.name=t", "-c", "user.email=t@l"]
        git(self.repo, *ident, "commit", "-q", "--allow-empty", "-m", "pushed")
        git(self.repo, "branch", "-M", "main")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, *ident, "commit", "-q", "--allow-empty", "-m", "local only")
        self.head = git(self.repo, "rev-parse", "HEAD")
        self.cfg = {"profile": "main"}

    def task(self, **kw):
        return dict({"repo": self.repo, "name": "demo", "branch": "chief/demo", "tool": "codex"}, **kw)

    def test_branch_starts_at_local_head(self):
        cmd = chief.add_command(self.cfg, self.task())
        self.assertEqual(git(self.repo, "rev-parse", "chief/demo"), self.head)
        self.assertIn("-w", cmd)
        self.assertEqual(cmd[cmd.index("-w") + 1], "chief/demo")
        self.assertNotIn("-b", cmd, "the branch already exists; -b would let AoE re-create it from the remote default")
        self.assertNotIn("--base-branch", cmd)

    def test_explicit_base_branch(self):
        git(self.repo, "branch", "feature", "main~1")
        chief.add_command(self.cfg, self.task(base_branch="feature"))
        self.assertEqual(git(self.repo, "rev-parse", "chief/demo"), git(self.repo, "rev-parse", "feature"))

    def test_existing_branch_is_kept(self):
        git(self.repo, "branch", "chief/demo", "main~1")
        before = git(self.repo, "rev-parse", "chief/demo")
        chief.add_command(self.cfg, self.task())
        self.assertEqual(git(self.repo, "rev-parse", "chief/demo"), before)

if __name__ == "__main__":
    unittest.main()
