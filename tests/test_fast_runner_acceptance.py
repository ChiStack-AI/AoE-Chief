"""Black-box contract for the optional fast runner, without recursively running it.

Copy only the runner into an isolated checkout and give it instrumented test
modules. Normal discovery remains the completion check. Real-suite timing and
original test-ID comparisons belong in .chief/progress.md, outside this suite.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


REPO = Path(__file__).resolve().parent.parent
EXCLUDED = (
    "test_inbox_ai_assistant_acceptance",
    "test_inbox_unified_log_acceptance",
    "test_inbox_command_bar_acceptance",
)


class FastRunnerAcceptanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="chief-fast-contract-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "checkout with spaces"
        self.tests = self.root / "tests"
        self.tests.mkdir(parents=True)
        self.trace = self.root / "executed.txt"

    def module(self, name, methods=("test_first", "test_second"), fail=False):
        path = self.tests / (name + ".py")
        path.parent.mkdir(parents=True, exist_ok=True)
        source = "import unittest\nfrom pathlib import Path\nclass Cases(unittest.TestCase):\n"
        for method in methods:
            label = name + ".Cases." + method
            source += (
                "    def {method}(self):\n"
                "        with Path({trace!r}).open('a') as stream:\n"
                "            stream.write({label!r} + '\\n')\n"
                "        self.assertTrue({success!r}, 'intentional fixture failure')\n"
            ).format(method=method, trace=str(self.trace), label=label, success=not fail)
        path.write_text(source, encoding="utf-8")
        return [name + ".Cases." + method for method in methods]

    def run_fast(self, cwd=None):
        runner = REPO / "tests" / "fast.py"
        self.assertTrue(runner.is_file(), "add the standard-library-only python3 tests/fast.py runner")
        shutil.copyfile(str(runner), str(self.tests / "fast.py"))
        env = dict(os.environ)
        # The copied runner must establish its own repository/test import paths.
        env.pop("PYTHONPATH", None)
        result = subprocess.run(
            [sys.executable, str(self.tests / "fast.py")],
            cwd=str(cwd or self.root), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=5,
        )
        executed = self.trace.read_text().splitlines() if self.trace.exists() else []
        return result, executed

    def test_runs_every_test_in_every_other_module_once_and_never_imports_the_three_exclusions(self):
        expected = []
        for name in ("test_alpha", "testNovel", "test_zeta"):
            expected.extend(self.module(name))
        for name in EXCLUDED:
            (self.tests / (name + ".py")).write_text(
                "raise AssertionError('the fast runner must not import this slow suite')\n")
            expected.extend(self.module(name + "_extra", ("test_retained",)))
        package = self.tests / "nested"
        package.mkdir()
        (package / "__init__.py").write_text("")
        expected.extend(self.module("nested/test_inside"))
        result, executed = self.run_fast()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertCountEqual(executed, expected)

    def test_failure_is_nonzero_and_does_not_prevent_remaining_tests_from_running(self):
        expected = self.module("test_a_failure", fail=True)
        expected.extend(self.module("test_z_success"))
        result, executed = self.run_fast()
        self.assertNotEqual(result.returncode, 0, "the fast command must propagate test failures")
        self.assertCountEqual(executed, expected)

    def test_discovery_import_errors_are_nonzero_and_other_tests_still_run(self):
        (self.tests / "test_broken.py").write_text("raise ImportError('intentional discovery failure')\n")
        expected = self.module("test_success")
        result, executed = self.run_fast()
        self.assertNotEqual(result.returncode, 0, "discovery errors cannot disappear from the exit status")
        self.assertCountEqual(executed, expected)

    def test_tests_can_import_repository_code_and_test_support_from_another_working_directory(self):
        (self.root / "project_fixture.py").write_text("VALUE = 'repository'\n")
        (self.tests / "support_fixture.py").write_text("VALUE = 'test support'\n")
        expected = self.module("test_import_paths")
        path = self.tests / "test_import_paths.py"
        path.write_text(
            "from project_fixture import VALUE as project\n"
            "from support_fixture import VALUE as support\n"
            "assert (project, support) == ('repository', 'test support')\n" + path.read_text())
        result, executed = self.run_fast(cwd=self.root.parent)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertCountEqual(executed, expected)

    def test_module_load_tests_hook_is_honored(self):
        expected = self.module("test_custom_loader", ("check_custom",))
        path = self.tests / "test_custom_loader.py"
        with path.open("a") as stream:
            stream.write("\ndef load_tests(loader, tests, pattern):\n"
                         "    return unittest.TestSuite([Cases('check_custom')])\n")
        result, executed = self.run_fast()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertCountEqual(executed, expected)

    def test_runner_has_no_artificial_wait_and_does_not_rewrite_test_sources(self):
        expected = self.module("test_quick")
        before = {str(path.relative_to(self.tests)): path.read_bytes()
                  for path in self.tests.rglob("*.py")}
        started = time.monotonic()
        result, executed = self.run_fast()
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertCountEqual(executed, expected)
        self.assertLess(elapsed, 2, "an immediate fixture suite must not incur worker/polling waits")
        after = {str(path.relative_to(self.tests)): path.read_bytes()
                 for path in self.tests.rglob("*.py") if path.name != "fast.py"}
        self.assertEqual(after, before, "the optional runner cannot rewrite normal discovery inputs")


if __name__ == "__main__":
    unittest.main()
