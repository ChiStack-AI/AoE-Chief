"""Tests for `chief.py task` on a board task: flags that are not given must not change the task."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

CHIEF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "chief.py")

class T(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.path = os.path.join(self.home, "tasks", "demo", "task.json")
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            json.dump({"id": "demo", "session_id": "0123456789abcdef", "state": "running", "goal": "g"}, f)

    def task(self, *flags):
        env = dict(os.environ, AOE_CHIEF_HOME=self.home, AOE_CHIEF_LANG="en")
        subprocess.run([sys.executable, CHIEF, "task", "demo"] + list(flags), env=env, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with open(self.path) as f:
            return json.load(f)

    def test_parked_survives_other_updates(self):
        self.assertTrue(self.task("--parked")["parked"])
        t = self.task("--goal", "new goal")
        self.assertEqual(t["goal"], "new goal")
        self.assertTrue(t.get("parked"), "updating the goal must not unpark the task")

    def test_unpark(self):
        self.task("--parked")
        self.assertFalse(self.task("--unpark").get("parked"))

if __name__ == "__main__":
    unittest.main()
