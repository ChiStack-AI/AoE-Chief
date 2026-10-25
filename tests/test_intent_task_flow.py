"""Acceptance tests for intent CLI fallback and the explicit test-approval gate.

Exercise the real CLI, task/decision storage, and tick dispatcher. AI generation,
worker startup, and worker message delivery are mocked; no external session runs.
"""
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("AOE_CHIEF_HOME", tempfile.mkdtemp())
import chief  # noqa: E402


class ChiefFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.home = self.root / "chief-state"
        state_paths = {"CFG": "config.json", "LAST": "latest.json", "BRIEF": "latest.md",
                       "TICK": "tick.json", "CHECKS": "checks.json", "ARCHIVE": "reports",
                       "TASKS": "tasks", "PREFS": "preferences.md"}
        state_patch = mock.patch.multiple(
            chief, ROOT=self.home, LANG="en",
            **{name: self.home / path for name, path in state_paths.items()})
        state_patch.start()
        self.addCleanup(state_patch.stop)
        aoe_patch = mock.patch.object(chief, "aoe", side_effect=AssertionError("unexpected AoE call"))
        aoe_patch.start()
        self.addCleanup(aoe_patch.stop)
        self.cfg = chief.config()
        self.git("init", "-q")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Chief Tests",
                               "-c", "user.email=chief-tests@example.invalid", *args],
                              check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True).stdout.strip()

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def cli(self, *args, cwd=None):
        out, err = io.StringIO(), io.StringIO()
        previous_cwd = os.getcwd()
        try:
            if cwd is not None:
                os.chdir(str(cwd))
            with mock.patch.object(sys, "argv", ["chief.py", *args]), \
                    redirect_stdout(out), redirect_stderr(err):
                try:
                    code = chief.main()
                except SystemExit as exc:
                    code = exc.code
        finally:
            os.chdir(previous_cwd)
        return code or 0, out.getvalue(), err.getvalue()


class IntentFallbackTests(ChiefFixture):
    def test_new_intent_yes_uses_inferred_defaults_when_ai_fails(self):
        self.write("tests/test_existing.py", "import unittest\nclass Existing(unittest.TestCase):\n    pass\n")
        intent = "Accept quoted commas in imported CSV rows"

        def fake_start(cfg, task, trust=False):
            return dict(task, session_id="0123456789abcdef", worktree=str(self.repo), state="running")

        with mock.patch.object(chief, "generate_ai", side_effect=RuntimeError("AI unavailable")) as generate, \
                mock.patch.object(chief, "start_task", side_effect=fake_start) as start, \
                mock.patch("builtins.input", side_effect=AssertionError("--yes must skip confirmation")) as ask:
            code, out, err = self.cli("new", intent, "--yes", cwd=self.repo)

        self.assertEqual(code, 0, err)
        generate.assert_called_once()
        prompt_args, prompt_kwargs = generate.call_args
        self.assertEqual(str(prompt_kwargs["cwd"]), str(self.repo))
        self.assertIs(prompt_kwargs["read_files"], True)
        self.assertIn(intent, prompt_args[0])
        self.assertIn("python3 -m unittest discover -s tests", prompt_args[0],
                      "AI refinement must receive the inferred defaults")
        ask.assert_not_called()
        start.assert_called_once()
        tasks = chief.all_tasks()
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task["repo"], str(self.repo), "--repo defaults to the current directory")
        self.assertEqual(task["goal"], intent)
        self.assertTrue(task["name"].strip())
        self.assertEqual(task["checks"], ["python3 -m unittest discover -s tests"])
        self.assertEqual(task["protected"], ["tests"])
        self.assertGreater(task["budget"]["rounds"], 0)
        self.assertGreater(task["budget"]["hours"], 0)
        self.assertIs(task["test_first"], True)
        self.assertEqual(task["phase"], "test")
        self.assertIn(intent, out, "--yes still shows the draft")
        self.assertIn(task["checks"][0], out)


class TestPhaseTickTests(ChiefFixture):
    def run_tick(self, passing, phase="test", dry=False):
        task = {"id": "csv-import", "name": "CSV import", "goal": "Accept quoted commas",
                "repo": str(self.repo), "worktree": str(self.repo), "session_id": "0123456789abcdef",
                "state": "running", "autonomy": "suggest", "test_first": True, "phase": phase,
                "checks": ["python3 -m unittest discover -s tests"]}
        chief.save_task(task)
        check = {"cmd": task["checks"][0], "ok": passing, "code": 0 if passing else 1,
                 "tail": "OK" if passing else "AssertionError: quoted commas are not supported"}
        item = {"id": task["session_id"], "task": task, "status": "idle", "signals": [],
                "log": {"turn": "done", "last_ts": "2026-10-25T12:00:00+00:00"},
                "checks": {"passed": int(passing), "total": 1, "results": [check]},
                "git": {"head": "acceptance-test-commit"}}
        # No pending decision or red signal: the phase itself must stop automation.
        with mock.patch.object(chief, "collect", return_value={"items": [item]}) as collect, \
                mock.patch.object(chief, "wait_ready", return_value="ready"), \
                mock.patch.object(chief, "deliver", return_value=True) as deliver, \
                mock.patch.object(chief, "review_task", return_value={"verdict": "pass"}) as review:
            actions = chief.tick(self.cfg, dry=dry)
        collect.assert_called_once()
        return actions, deliver, review

    def test_failing_checks_do_not_auto_continue_during_test_phase(self):
        _, deliver, review = self.run_tick(passing=False)
        deliver.assert_not_called()
        review.assert_not_called()
        self.assertEqual(chief.load_task("csv-import")["phase"], "test")

    def test_passing_checks_do_not_start_review_during_test_phase(self):
        _, deliver, review = self.run_tick(passing=True)
        deliver.assert_not_called()
        review.assert_not_called()
        self.assertEqual(chief.load_task("csv-import")["phase"], "test")

    def test_dry_tick_does_not_propose_continuation_or_review_during_test_phase(self):
        for passing in (False, True):
            with self.subTest(passing=passing):
                actions, deliver, review = self.run_tick(passing=passing, dry=True)
                deliver.assert_not_called()
                review.assert_not_called()
                text = " ".join(actions).lower()
                self.assertNotIn("would continue", text)
                self.assertNotIn("would run a cross review", text)

    def test_build_phase_still_auto_continues_on_failing_checks(self):
        actions, deliver, review = self.run_tick(passing=False, phase="build")
        deliver.assert_called_once()
        review.assert_not_called()
        self.assertTrue(actions)

    def test_build_phase_still_reviews_passing_checks(self):
        actions, deliver, review = self.run_tick(passing=True, phase="build")
        deliver.assert_not_called()
        review.assert_called_once()
        self.assertTrue(actions)


class AcceptanceDecisionTests(ChiefFixture):
    def setUp(self):
        super().setUp()
        self.git("commit", "-q", "--allow-empty", "-m", "contract base")
        self.base = self.git("rev-parse", "HEAD")
        self.files = ["tests/test_csv_acceptance.py", "tests/test_csv_errors.py"]
        for name in self.files:
            self.write(name, "# proposed acceptance test\n")
        self.git("add", "tests")
        self.git("commit", "-q", "-m", "Propose acceptance tests")
        self.tests_head = self.git("rev-parse", "HEAD")
        self.task = {"id": "csv-import", "name": "CSV import", "goal": "Accept quoted commas",
                     "repo": str(self.repo), "worktree": str(self.repo), "base": self.base,
                     "session_id": "0123456789abcdef", "state": "needs-you", "autonomy": "suggest",
                     "test_first": True, "phase": "test", "protected": ["config/production.json"],
                     "checks": ["python3 -m unittest discover -s tests"]}
        chief.save_task(self.task)
        self.log = {"turn": "done", "last_ts": "2026-10-25T12:00:00+00:00",
                    "footer": {"conclusion": "Committed failing acceptance tests", "blocker": "",
                               "next": "Wait for approval",
                               "decision": "approve the acceptance tests " + " ".join(self.files)}}
        item = {"id": self.task["session_id"], "log": self.log, "signals": []}
        self.decision = chief.sync_decisions(self.cfg, self.task, item)
        self.assertIsNotNone(self.decision, "the worker's approval request must enter the decision queue")
        self.record = {"id": self.task["session_id"], "project_path": str(self.repo)}

    def test_decide_accept_approves_tests_then_sends_build_message(self):
        # Spy on the real helper when implemented, while keeping the red phase importable.
        implementation = getattr(chief, "approve_tests", None)
        approvals, order = [], []

        def observe_approval(task, files):
            self.assertTrue(callable(implementation), "approve_tests must exist")
            approvals.append((copy.deepcopy(task), list(files)))
            order.append("approve")
            return implementation(task, files)

        def observe_delivery(*args, **kwargs):
            order.append("deliver")
            return True

        with mock.patch.object(chief, "approve_tests", create=True, side_effect=observe_approval) as approve, \
                mock.patch.object(chief, "aoe_records", return_value={self.task["session_id"]: self.record}), \
                mock.patch.object(chief, "transcript", return_value=self.log), \
                mock.patch.object(chief, "wait_ready", return_value="ready"), \
                mock.patch.object(chief, "deliver", side_effect=observe_delivery) as deliver:
            code, _, err = self.cli("decide", self.decision["id"], "--accept")

        self.assertEqual(code, 0, err)
        approve.assert_called_once()
        self.assertEqual(approvals[0][0]["phase"], "test")
        self.assertCountEqual(approvals[0][1], self.files)
        self.assertEqual(order, ["approve", "deliver"])
        updated = chief.load_task(self.task["id"])
        self.assertEqual(updated["phase"], "build")
        self.assertEqual(updated["base"], self.tests_head)
        self.assertCountEqual(updated["protected"], self.task["protected"] + self.files)
        deliver.assert_called_once()
        self.assertEqual(deliver.call_args[0][1], self.task["session_id"])
        message = deliver.call_args[0][2].lower()
        self.assertIn("acceptance tests", message)
        self.assertRegex(message, r"\bimplement(?:ation)?\b|\bbuild\b")
        resolved = chief.find_decision(self.decision["id"])
        self.assertEqual(resolved["state"], "sent")
        self.assertEqual(resolved["resolution"], "accepted")

    def test_decide_reply_sends_feedback_and_keeps_tests_unapproved(self):
        feedback = "Add coverage for empty input before approval."
        with mock.patch.object(chief, "approve_tests", create=True,
                               side_effect=AssertionError("feedback must not approve tests")) as approve, \
                mock.patch.object(chief, "aoe_records", return_value={self.task["session_id"]: self.record}), \
                mock.patch.object(chief, "transcript", return_value=self.log), \
                mock.patch.object(chief, "wait_ready", return_value="ready"), \
                mock.patch.object(chief, "deliver", return_value=True) as deliver:
            code, _, err = self.cli("decide", self.decision["id"], "--reply", feedback)

        self.assertEqual(code, 0, err)
        approve.assert_not_called()
        updated = chief.load_task(self.task["id"])
        self.assertEqual(updated["phase"], "test")
        self.assertEqual(updated["base"], self.base)
        self.assertEqual(updated["protected"], self.task["protected"])
        deliver.assert_called_once()
        self.assertEqual(deliver.call_args[0][1], self.task["session_id"])
        self.assertIn(feedback, deliver.call_args[0][2])
        self.assertEqual(chief.find_decision(self.decision["id"])["state"], "sent")


if __name__ == "__main__":
    unittest.main()
