#!/usr/bin/env python3
"""Fast iteration subset. The completion check remains full unittest discovery."""
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import io
import sys
import time
import unittest


EXCLUDED_MODULES = frozenset((
    "test_inbox_ai_assistant_acceptance.py",
    "test_inbox_unified_log_acceptance.py",
    "test_inbox_command_bar_acceptance.py",
))


class FastLoader(unittest.TestLoader):
    def _match_path(self, path, full_path, pattern):
        # Filter before import, retaining unittest's packages and load_tests hooks.
        return path not in EXCLUDED_MODULES and super()._match_path(path, full_path, pattern)


def run_suite(suite):
    # Test modules may change cwd, environment and globals. Processes isolate
    # those fixtures; threads would let them interfere with each other.
    output = io.StringIO()
    with redirect_stdout(output), redirect_stderr(output):
        result = unittest.TextTestRunner(stream=output).run(suite)
    return result.wasSuccessful(), result.testsRun, output.getvalue()


def main():
    tests = Path(__file__).resolve().parent
    sys.path.insert(0, str(tests.parent))
    suite = FastLoader().discover(str(tests))
    started = time.monotonic()
    successful, count = True, 0
    # Discovery still runs normally, including load_tests. Its module suites
    # retain all cases and are the independent units sent to the workers.
    with ProcessPoolExecutor(max_workers=4) as pool:
        for passed, tests_run, output in pool.map(run_suite, suite):
            sys.stderr.write(output)
            successful = successful and passed
            count += tests_run
    sys.stderr.write("\nFast subset: {} tests in {:.3f}s; {}\n".format(
        count, time.monotonic() - started, "OK" if successful else "FAILED"))
    return 0 if successful else 1


if __name__ == "__main__":
    sys.exit(main())
