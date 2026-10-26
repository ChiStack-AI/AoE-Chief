"""New tests under protected directories need review; existing files stay red.

Use real Git history to distinguish additions from modifications, deletions and
renames relative to the task's scope base, rather than just the current index.
"""
import chief
from test_intent_task_flow import ChiefFixture


class ProtectedAdditionsTests(ChiefFixture):
    def setUp(self):
        super().setUp()
        self.write("tests/test_existing.py", "# original acceptance tests\n" * 12)
        self.write("tests/fixtures/reference.json", '{"protected": true}\n')
        self.write("config/production.json", '{"protected": true}\n')
        self.write("src/application.py", "# application\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "protected baseline")
        self.task = dict(id="protected-review", repo=str(self.repo), goal="extend behavior", scope=".",
                         base=self.git("rev-parse", "HEAD"), protected=["tests", "config"], phase="build")

    def signals(self, task=None):
        task = task or self.task
        scope = chief.scope_info(str(self.repo), task)
        self.assertIsNotNone(scope, "Git verification must succeed for these fixture repositories")
        return chief.judge(dict(status="idle", task=task, scope=scope, log={"turn": "done"})), scope

    def messages(self, signals, level):
        return "\n".join(message for severity, message, _ in signals if severity == level)

    def assert_yellow_addition(self, path, task=None):
        signals, scope = self.signals(task)
        self.assertIn(path, self.messages(signals, "yellow"), "list the added test path for human review")
        self.assertNotIn(path, self.messages(signals, "red"), "a genuinely new test is a review item")
        return signals, scope

    def assert_red(self, path, task=None):
        signals, scope = self.signals(task)
        self.assertIn(path, scope["protected_touched"])
        self.assertIn(path, self.messages(signals, "red"))
        return signals, scope

    def test_committed_staged_and_untracked_additions_are_yellow_relative_to_scope_base(self):
        paths = ["tests/test_committed.py", "tests/nested/test_staged.py", "tests/test untracked.py"]
        self.write(paths[0], "# committed regression\n")
        self.git("add", paths[0])
        self.git("commit", "-q", "-m", "new regression since scope base")
        self.write(paths[1], "# staged regression\n")
        self.git("add", paths[1])
        self.write(paths[2], "# untracked regression\n")
        for path in paths:
            with self.subTest(path=path):
                self.assert_yellow_addition(path)

    def test_legacy_build_contracts_also_review_new_tests_as_yellow(self):
        path = "tests/test_new.py"
        self.write(path, "# new regression\n")
        task = {k: v for k, v in self.task.items() if k != "phase"}
        self.assert_yellow_addition(path, task)

    def test_modifying_a_newly_committed_test_still_counts_as_addition_since_base(self):
        path = "tests/test_new.py"
        self.write(path, "# first draft\n")
        self.git("add", path)
        self.git("commit", "-q", "-m", "add regression")
        self.write(path, "# revised after commit\n")
        self.assert_yellow_addition(path)

    def test_review_lists_every_new_test_path_without_truncating_after_three(self):
        paths = ["tests/test_regression_%02d.py" % n for n in range(6)]
        for path in paths:
            self.write(path, "# additional regression\n")
        signals, _ = self.signals()
        yellow = self.messages(signals, "yellow")
        for path in paths:
            self.assertIn(path, yellow)
        self.assertFalse(any(level == "red" for level, _, _ in signals))

    def test_existing_test_modifications_remain_red_in_all_git_states(self):
        path = "tests/test_existing.py"
        self.write(path, "# changed existing acceptance tests\n")
        for stage in ("unstaged", "staged", "committed"):
            with self.subTest(stage=stage):
                if stage == "staged":
                    self.git("add", path)
                elif stage == "committed":
                    self.git("commit", "-q", "-m", "modify existing tests")
                self.assert_red(path)

    def test_existing_test_deletions_remain_red_in_all_git_states(self):
        path = "tests/test_existing.py"
        (self.repo / path).unlink()
        for stage in ("unstaged", "staged", "committed"):
            with self.subTest(stage=stage):
                if stage == "staged":
                    self.git("add", "-u")
                elif stage == "committed":
                    self.git("commit", "-q", "-m", "delete existing test")
                self.assert_red(path)

    def test_rename_within_protected_tests_does_not_turn_existing_test_into_yellow_addition(self):
        old, new = "tests/test_existing.py", "tests/test_renamed.py"
        self.git("mv", old, new)
        self.git("commit", "-q", "-m", "rename protected test")
        self.assert_rename_protected(old)

    def test_rename_out_of_protected_directory_cannot_hide_protected_source(self):
        old, new = "tests/test_existing.py", "src/renamed_test.py"
        self.git("mv", old, new)
        for stage in ("staged", "committed"):
            with self.subTest(stage=stage):
                if stage == "committed":
                    self.git("commit", "-q", "-m", "move test outside protected directory")
                self.assert_rename_protected(old)

    def assert_rename_protected(self, source):
        signals, scope = self.signals()
        self.assertIn(source, scope["protected_touched"], "the protected source path must remain visible")
        self.assertIn(source, self.messages(signals, "red"))

    def test_rename_of_existing_non_test_protected_file_remains_red(self):
        source = "config/production.json"
        self.git("mv", source, "src/production.json")
        self.git("commit", "-q", "-m", "move protected configuration")
        self.assert_rename_protected(source)

    def test_non_test_protected_additions_remain_red_even_inside_test_directory(self):
        for path in ("config/new-production.json", "tests/fixtures/new-reference.json"):
            with self.subTest(path=path):
                self.write(path, '{"new": true}\n')
                self.assert_red(path)

    def test_explicitly_approved_new_test_path_remains_protected(self):
        path = "tests/test_approved.py"
        # Explicit file protection takes precedence even if the file did not
        # exist at the scope base (for example, a separately approved contract).
        task = dict(self.task, protected=self.task["protected"] + [path])
        self.write(path, "# explicitly approved acceptance tests\n")
        self.assert_red(path, task)

    def test_approved_test_commit_becomes_scope_base_and_later_edits_stay_red(self):
        path = "tests/test_approved.py"
        self.write(path, "# approved acceptance tests\n")
        self.git("add", path)
        self.git("commit", "-q", "-m", "approved acceptance tests")
        task = chief.approve_tests(dict(self.task, phase="test"), [path])
        self.assertEqual(chief.scope_info(str(self.repo), task)["changed"], 0)
        self.write(path, "# unapproved revision\n")
        self.assert_red(path, task)

    def test_test_phase_still_allows_acceptance_test_edits_and_additions(self):
        self.write("tests/test_existing.py", "# revised acceptance tests\n")
        self.write("tests/test_new.py", "# new acceptance tests\n")
        signals, scope = self.signals(dict(self.task, test_first=True, phase="test"))
        self.assertEqual(scope["protected_touched"], [])
        self.assertFalse(any(level == "red" for level, _, _ in signals))

    def test_test_phase_does_not_exempt_explicit_approved_files_or_non_test_protected_files(self):
        path = "tests/test_existing.py"
        self.write(path, "# revised explicitly approved tests\n")
        self.write("tests/fixtures/reference.json", '{"changed": true}\n')
        task = dict(self.task, test_first=True, phase="test", protected=self.task["protected"] + [path])
        self.assert_red(path, task)
        self.assert_red("tests/fixtures/reference.json", task)

    def test_custom_scope_ignore_still_excludes_generated_test_paths(self):
        self.write("tests/generated-cache/test_generated.py", "# generated output\n")
        scope = chief.scope_info(str(self.repo), self.task, ["generated-cache"])
        self.assertEqual(scope["changed"], 0)
        self.assertEqual(scope["protected_touched"], [])
