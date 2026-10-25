"""Acceptance contract for intent drafts and the test-first approval boundary.

These tests use temporary repositories; they never invoke an AI or an AoE session.
"""
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("AOE_CHIEF_HOME", tempfile.mkdtemp())
import chief  # noqa: E402


class RepoFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()

    def write(self, name, text="", repo=None):
        path = (repo or self.repo) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


class InferChecksTests(RepoFixture):
    def test_no_test_signals_means_no_invented_check(self):
        self.assertEqual(chief.infer_checks(str(self.repo)), [])

    def test_pytest_configuration(self):
        configs = {
            "pytest.ini": "[pytest]\naddopts = -q\n",
            "pyproject.toml": "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
            "setup.cfg": "[tool:pytest]\ntestpaths = tests\n",
            "tox.ini": "[pytest]\ntestpaths = tests\n",
        }
        for name, content in configs.items():
            with self.subTest(config=name):
                path = self.write(name, content)
                self.assertEqual(chief.infer_checks(str(self.repo)), ["python3 -m pytest"])
                path.unlink()

    def test_tests_directory_defaults_to_pytest(self):
        self.write("tests/test_feature.py", "def test_feature():\n    assert True\n")
        self.assertEqual(chief.infer_checks(str(self.repo)), ["python3 -m pytest"])

    def test_unittest_suite_uses_standard_library_discovery(self):
        self.write("tests/test_feature.py", "import unittest\nclass Feature(unittest.TestCase):\n    pass\n")
        self.assertEqual(chief.infer_checks(str(self.repo)), ["python3 -m unittest discover -s tests"])

    def test_explicit_pytest_configuration_wins_over_unittest_style(self):
        self.write("pytest.ini", "[pytest]\n")
        self.write("tests/test_feature.py", "import unittest\nclass Feature(unittest.TestCase):\n    pass\n")
        self.assertEqual(chief.infer_checks(str(self.repo)), ["python3 -m pytest"])

    def test_unrelated_python_configuration_is_not_a_pytest_signal(self):
        self.write("pyproject.toml", "[tool.black]\nline-length = 100\n")
        self.write("setup.cfg", "[metadata]\nname = demo\n")
        self.write("tox.ini", "[tox]\nenvlist = py\n")
        self.assertEqual(chief.infer_checks(str(self.repo)), [])

    def test_package_test_script(self):
        self.write("package.json", json.dumps({"scripts": {"test": "node --test"}}))
        self.assertEqual(chief.infer_checks(str(self.repo)), ["npm test"])

    def test_package_without_test_script_has_no_check(self):
        for package in ({}, {"scripts": {"build": "vite build"}}, {"scripts": {"test": ""}}):
            with self.subTest(package=package):
                self.write("package.json", json.dumps(package))
                self.assertEqual(chief.infer_checks(str(self.repo)), [])

    def test_malformed_package_does_not_hide_other_checks(self):
        self.write("package.json", "{broken json")
        self.write("Makefile", ".PHONY: test\ntest:\n\tpython3 -m unittest\n")
        self.assertEqual(chief.infer_checks(str(self.repo)), ["make test"])

    def test_make_test_target(self):
        self.write("Makefile", ".PHONY: test\ntest: unit\n\tpython3 -m unittest\n")
        self.assertEqual(chief.infer_checks(str(self.repo)), ["make test"])

    def test_similar_make_target_and_comments_do_not_count(self):
        self.write("Makefile", "# test: not a real target\ntest-unit:\n\ttrue\n")
        self.assertEqual(chief.infer_checks(str(self.repo)), [])

    def test_independent_ecosystems_all_contribute_checks_once(self):
        self.write("pytest.ini", "[pytest]\n")
        self.write("tests/test_feature.py", "def test_feature():\n    assert True\n")
        self.write("package.json", json.dumps({"scripts": {"test": "node --test"}}))
        self.write("Makefile", "test:\n\tnpm test\n")
        checks = chief.infer_checks(str(self.repo))
        self.assertCountEqual(checks, ["python3 -m pytest", "npm test", "make test"])
        self.assertEqual(checks, chief.infer_checks(str(self.repo)), "inference must be deterministic")


class InferProtectedTests(RepoFixture):
    def test_existing_tests_directory_is_protected(self):
        self.write("tests/test_feature.py")
        self.assertEqual(chief.infer_protected(str(self.repo)), ["tests"])

    def test_singular_test_directory_is_protected(self):
        self.write("test/feature.test.js")
        self.assertEqual(chief.infer_protected(str(self.repo)), ["test"])

    def test_both_test_directories_are_protected(self):
        self.write("tests/test_feature.py")
        self.write("test/feature.test.js")
        self.assertCountEqual(chief.infer_protected(str(self.repo)), ["tests", "test"])

    def test_no_test_directory_means_no_invented_protected_path(self):
        self.write("src/app.py")
        self.assertEqual(chief.infer_protected(str(self.repo)), [])

    def test_file_named_tests_is_not_a_test_directory(self):
        self.write("tests", "This is a file, not a test suite.\n")
        self.assertEqual(chief.infer_protected(str(self.repo)), [])


class TestFirstKickoffTests(unittest.TestCase):
    def task(self, **changes):
        task = {"name": "CSV import", "goal": "Accept quoted commas in imported CSV rows",
                "checks": ["python3 -m unittest discover -s tests"], "protected": ["tests"],
                "budget": {"rounds": 10, "hours": 2}, "test_first": True, "phase": "test"}
        task.update(changes)
        return task

    def assert_test_phase(self, prompt):
        prose = re.sub(r"\s+", " ", prompt.lower())
        self.assertRegex(prose, r"failing acceptance tests|acceptance tests.{0,80}fail")
        self.assertRegex(prose, r"commit.{0,100}test|test.{0,100}commit")
        self.assertIn("Decision needed: approve the acceptance tests <files>", prompt)
        self.assertRegex(prose, r"do not implement|don't implement|without implementing|no implementation")

    def test_test_first_kickoff_requires_tests_commit_and_approval(self):
        self.assert_test_phase(chief.kickoff_prompt(self.task()))

    def test_test_first_without_explicit_phase_starts_with_tests(self):
        task = self.task()
        task.pop("phase")
        self.assert_test_phase(chief.kickoff_prompt(task))

    def test_resumed_test_phase_still_requires_approval(self):
        prompt = chief.kickoff_prompt(self.task(), resume=True)
        self.assert_test_phase(prompt)
        self.assertIn(".chief/progress.md", prompt)

    def test_test_first_retains_goal_and_checks(self):
        task = self.task()
        prompt = chief.kickoff_prompt(task)
        self.assertIn(task["goal"], prompt)
        self.assertIn(task["checks"][0], prompt)

    def test_build_phase_does_not_restart_test_approval(self):
        prompt = chief.kickoff_prompt(self.task(phase="build"))
        self.assertNotIn("approve the acceptance tests", prompt.lower())
        self.assertIn("- Never modify: tests", prompt)

    def test_disabled_test_first_retains_regular_kickoff(self):
        prompt = chief.kickoff_prompt(self.task(test_first=False, phase="build"))
        self.assertNotIn("approve the acceptance tests", prompt.lower())
        self.assertIn("- Never modify: tests", prompt)

    def test_legacy_task_without_test_first_retains_regular_kickoff(self):
        task = self.task()
        task.pop("test_first")
        task.pop("phase")
        prompt = chief.kickoff_prompt(task)
        self.assertNotIn("approve the acceptance tests", prompt.lower())
        self.assertIn("- Never modify: tests", prompt)


class ApproveTestsTests(RepoFixture):
    def git(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Chief Tests",
                               "-c", "user.email=chief-tests@example.invalid", *args],
                              check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True).stdout.strip()

    def setUp(self):
        super().setUp()
        self.git(self.repo, "init", "-q")
        self.git(self.repo, "commit", "-q", "--allow-empty", "-m", "contract base")
        self.base = self.git(self.repo, "rev-parse", "HEAD")
        self.worktree = self.root / "worker"
        self.git(self.repo, "worktree", "add", "-q", "--detach", str(self.worktree))
        self.files = ["tests/test_csv_acceptance.py", "tests/test_csv_errors.py"]
        for name in self.files:
            self.write(name, "# proposed acceptance test\n", repo=self.worktree)
        self.git(self.worktree, "add", "tests")
        self.git(self.worktree, "commit", "-q", "-m", "Add failing acceptance tests")
        self.tests_head = self.git(self.worktree, "rev-parse", "HEAD")
        self.task = {"id": "csv-import", "name": "CSV import", "goal": "Accept quoted commas",
                     "repo": str(self.repo), "worktree": str(self.worktree), "base": self.base,
                     "phase": "test", "test_first": True, "state": "needs-you",
                     "checks": ["python3 -m unittest discover -s tests"],
                     "protected": ["config/production.json"], "scope": ".",
                     "budget": {"rounds": 10, "hours": 2}}

    def approve(self, task=None, files=None):
        return chief.approve_tests(copy.deepcopy(self.task if task is None else task),
                                   list(self.files if files is None else files))

    def test_approval_enters_build_phase_and_protects_each_test_file(self):
        updated = self.approve()
        self.assertEqual(updated["phase"], "build")
        self.assertCountEqual(updated["protected"], ["config/production.json"] + self.files)

    def test_scope_base_is_the_acceptance_commit_in_worker_worktree(self):
        updated = self.approve()
        self.assertNotEqual(self.tests_head, self.base)
        self.assertEqual(updated["base"], self.tests_head)
        scope = chief.scope_info(str(self.worktree), updated)
        self.assertEqual(scope["changed"], 0, "approved tests are part of the new scope baseline")
        self.assertEqual(scope["protected_touched"], [])

    def test_later_test_edits_are_detected_as_protected(self):
        updated = self.approve()
        self.write(self.files[0], "# weakened acceptance test\n", repo=self.worktree)
        scope = chief.scope_info(str(self.worktree), updated)
        self.assertEqual(scope["protected_touched"], [self.files[0]])

    def test_repo_is_used_when_task_has_no_worktree(self):
        task = dict(self.task, base="old-base")
        task.pop("worktree")
        self.assertEqual(self.approve(task)["base"], self.base)

    def test_approval_handles_missing_protected_list(self):
        task = dict(self.task)
        task.pop("protected")
        self.assertCountEqual(self.approve(task)["protected"], self.files)

    def test_existing_protection_and_repeated_files_are_not_duplicated(self):
        task = dict(self.task, protected=["tests", self.files[0]])
        updated = self.approve(task, self.files + [self.files[0]])
        self.assertCountEqual(updated["protected"], ["tests"] + self.files)

    def test_approval_preserves_unrelated_contract_fields(self):
        updated = self.approve()
        for field in ("id", "name", "goal", "repo", "worktree", "checks", "scope", "budget", "test_first"):
            with self.subTest(field=field):
                self.assertEqual(updated[field], self.task[field])


if __name__ == "__main__":
    unittest.main()
