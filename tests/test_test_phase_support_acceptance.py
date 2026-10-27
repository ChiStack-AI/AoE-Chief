"""Acceptance contract for reviewing and locking new test-phase support files.

Use real Git history, the real approval path, and both display languages. Existing
acceptance-test revision rules and build behavior remain covered by the old suite;
these cases extend protection to helpers, conftest files and fixtures.
"""
import copy
import subprocess
from unittest import mock

import chief
from test_test_approval_ux_acceptance import ApprovalFixture


class SupportReviewTests(ApprovalFixture):
    def signals(self, task=None, extra_ignore=()):
        task = self.task if task is None else task
        scope = chief.scope_info(str(self.repo), task, extra_ignore)
        self.assertIsNotNone(scope, "the fixture must yield verifiable Git scope data")
        signals = chief.judge(dict(status="idle", task=task, scope=scope, log={"turn": "done"}))
        return scope, signals

    def messages(self, signals, level):
        return "\n".join(message for severity, message, _ in signals if severity == level)

    def assert_review(self, path, task=None):
        for lang in ("en", "zh"):
            with self.subTest(language=lang, path=path), mock.patch.object(chief, "LANG", lang):
                scope, signals = self.signals(task)
                self.assertNotIn(path, scope["protected_violations"])
                self.assertIn(path, self.messages(signals, "yellow"), "every proposed support file needs visible review")
                self.assertNotIn(path, self.messages(signals, "red"))

    def assert_conventional_exemption(self, path, task=None):
        self.assertTrue(chief.is_test_file(path), "the fixture must use a conventional test filename")
        for lang in ("en", "zh"):
            with self.subTest(language=lang, path=path), mock.patch.object(chief, "LANG", lang):
                scope, signals = self.signals(task)
                self.assertNotIn(path, scope["protected_touched"])
                self.assertNotIn(path, scope["protected_violations"])
                self.assertNotIn(path, scope["protected_added_tests"])
                self.assertNotIn(path, self.messages(signals, "red"))
                self.assertNotIn(path, self.messages(signals, "yellow"),
                                 "conventional tests retain their existing test-phase exemption")

    def assert_red_in_every_phase(self, source, destination=None):
        for phase in ("test", "build", "legacy"):
            task = dict(self.task, phase=phase)
            if phase == "legacy":
                task.pop("phase")
                task.pop("test_first")
            for lang in ("en", "zh"):
                with self.subTest(phase=phase, language=lang), mock.patch.object(chief, "LANG", lang):
                    scope, signals = self.signals(task)
                    self.assertIn(source, scope["protected_touched"])
                    self.assertIn(source, scope["protected_violations"])
                    self.assertIn(source, self.messages(signals, "red"))
                    if destination:
                        self.assertNotIn(destination, self.messages(signals, "yellow"),
                                         "a renamed existing file is not a genuinely new review addition")

    def test_new_helpers_conftest_and_fixtures_are_reviewed_in_all_git_states(self):
        paths = ("tests/helpers/new_helper.py", "tests/conftest.py", "tests/fixtures/new sample.json",
                 "test/helpers.py", "test/conftest.py", "qa/tests/new_helper.py")
        for path in paths:
            self.write(path, "new support for " + path + "\n")
        for state in ("untracked", "staged", "committed"):
            with self.subTest(state=state):
                if state == "staged":
                    self.git("add", "tests", "test", "qa/tests")
                elif state == "committed":
                    self.git("commit", "-q", "-m", "add acceptance test support")
                for path in paths:
                    self.assert_review(path)
                self.assert_conventional_exemption(self.listed)

    def test_conventional_test_edits_additions_deletions_and_renames_keep_the_existing_exemption(self):
        edited = ("tests/test_existing.py", "test/feature.spec.ts")
        added = ("tests/test_new.py", "test/new_feature_test.go")
        deleted = ("tests/test_deleted.py", "test/LegacyTest.java")
        renamed = (("tests/test_old_name.py", "tests/test_new_name.py"),
                   ("test/feature.test.js", "test/renamed.test.js"),
                   ("tests/test_moved.py", "src/moved_test.py"))
        for path in edited + deleted + tuple(source for source, _ in renamed):
            self.write(path, "original conventional test " + path + "\n")
        self.git("add", "tests", "test")
        self.git("commit", "-q", "-m", "baseline conventional test lifecycle fixtures")
        task = dict(self.task, base=self.git("rev-parse", "HEAD"),
                    protected=self.task["protected"] + ["test"])
        for path in edited + added:
            self.write(path, "revised conventional test " + path + "\n")
        for path in deleted:
            (self.repo / path).unlink()
        for source, destination in renamed:
            (self.repo / source).rename(self.repo / destination)
        affected = edited + added + deleted + tuple(path for pair in renamed for path in pair)
        for state in ("unstaged", "staged", "committed"):
            if state == "staged":
                self.git("add", "tests", "test", "src")
            elif state == "committed":
                self.git("commit", "-q", "-m", "revise add delete and rename conventional tests")
            for implicit_phase in (False, True):
                contract = dict(task)
                if implicit_phase:
                    contract.pop("phase")
                with self.subTest(state=state, implicit_phase=implicit_phase):
                    for path in affected:
                        self.assert_conventional_exemption(path, contract)
                    scope, _ = self.signals(contract)
                    self.assertEqual(scope["protected_touched"], [])
                    self.assertEqual(scope["protected_violations"], [])
                    self.assertEqual(scope["protected_added_tests"], [])

    def test_conventional_test_edit_outside_inferred_directories_keeps_its_existing_protection(self):
        path = "qa/tests/test_existing.py"
        self.write(path, "# conventional test outside inferred directories\n")
        self.git("add", path)
        self.git("commit", "-q", "-m", "baseline protected custom test")
        self.task["base"] = self.git("rev-parse", "HEAD")
        self.write(path, "# unapproved change outside the established exemption\n")
        self.assert_red_in_every_phase(path)

    def test_inferred_test_directories_are_reviewed_even_without_explicit_directory_protection(self):
        task = dict(self.task, protected=["config/production.json"])
        for path in ("tests/helper.py", "test/fixtures/input.csv"):
            self.write(path, "new test support\n")
            self.assert_review(path, task)

    def test_legacy_test_first_contract_without_phase_gets_the_same_support_review(self):
        task = dict(self.task)
        task.pop("phase")
        self.write("tests/helper.py", "new test support\n")
        self.assert_review("tests/helper.py", task)

    def test_revised_newly_committed_support_is_still_a_review_addition_since_base(self):
        path = "tests/helper.py"
        self.write(path, "# initial support\n")
        self.git("add", path)
        self.git("commit", "-q", "-m", "add helper")
        self.write(path, "# revised support awaiting approval\n")
        self.assert_review(path)

    def test_copy_with_unchanged_protected_source_is_a_genuinely_new_support_addition(self):
        source, added = "tests/helpers/existing.py", "tests/helpers/copied.py"
        self.write(added, (self.repo / source).read_text(encoding="utf-8"))
        self.git("add", added)
        self.git("commit", "-q", "-m", "copy helper without modifying the source")
        self.assert_review(added)
        _, signals = self.signals()
        self.assertNotIn(source, self.messages(signals, "red"))

    def test_review_lists_all_support_files_alongside_protected_violations(self):
        paths = ["tests/fixtures/input_%02d.json" % n for n in range(7)]
        for path in paths:
            self.write(path, '{"new": true}\n')
        self.write("config/production.json", '{"changed": true}\n')
        for path in paths:
            self.assert_review(path)
        self.assert_red_in_every_phase("config/production.json")

    def test_support_review_signal_is_described_in_both_languages(self):
        self.write("tests/conftest.py", "# new test support\n")
        for lang in ("en", "zh"):
            with self.subTest(language=lang), mock.patch.object(chief, "LANG", lang):
                _, signals = self.signals()
                messages = [message for severity, message, _ in signals
                            if severity == "yellow" and "tests/conftest.py" in message]
                self.assertTrue(messages, "the new support file needs a localized review signal")
                self.assertRegex("\n".join(messages).lower(), "support" if lang == "en" else "辅助|支持|配套|支撑")

    def test_existing_support_edits_and_deletions_remain_red_in_all_git_states(self):
        changed, deleted = "tests/helpers/existing.py", "tests/fixtures/existing.json"
        self.write(changed, "# unapproved change to existing support\n")
        (self.repo / deleted).unlink()
        for state in ("unstaged", "staged", "committed"):
            with self.subTest(state=state):
                if state == "staged":
                    self.git("add", "-u")
                elif state == "committed":
                    self.git("commit", "-q", "-m", "edit and delete existing support")
                self.assert_red_in_every_phase(changed)
                self.assert_red_in_every_phase(deleted)

    def test_explicitly_protected_test_edits_and_deletions_remain_red_in_every_phase(self):
        self.task["protected"].append("tests/test_existing.py")
        self.write("tests/test_existing.py", "# changed explicitly protected acceptance test\n")
        self.assert_red_in_every_phase("tests/test_existing.py")
        (self.repo / "tests/test_existing.py").unlink()
        self.assert_red_in_every_phase("tests/test_existing.py")

    def test_staged_support_edits_remain_red_when_the_working_copy_is_restored(self):
        path = "tests/helpers/existing.py"
        original = (self.repo / path).read_text(encoding="utf-8")
        self.write(path, "# staged change still present in the index\n")
        self.git("add", path)
        self.write(path, original)
        self.assert_red_in_every_phase(path)

    def test_support_rename_inside_test_directory_is_never_a_new_support_addition(self):
        source, destination = "tests/helpers/existing.py", "tests/helpers/renamed.py"
        self.git("mv", source, destination)
        for state in ("staged", "committed"):
            with self.subTest(state=state):
                if state == "committed":
                    self.git("commit", "-q", "-m", "rename protected helper")
                self.assert_red_in_every_phase(source, destination)

    def test_unstaged_support_rename_does_not_masquerade_as_a_new_support_file(self):
        source, destination = "tests/helpers/existing.py", "tests/helpers/renamed.py"
        (self.repo / source).rename(self.repo / destination)
        self.assert_red_in_every_phase(source, destination)

    def test_explicitly_protected_test_rename_cannot_masquerade_as_a_new_support_file(self):
        source, destination = "tests/test_existing.py", "tests/helpers/renamed_acceptance.py"
        self.assertFalse(chief.is_test_file(destination))
        self.task["protected"].append(source)
        self.git("mv", source, destination)
        for state in ("staged", "committed"):
            with self.subTest(state=state):
                if state == "committed":
                    self.git("commit", "-q", "-m", "rename an existing test to a helper")
                self.assert_red_in_every_phase(source, destination)

    def test_conventional_test_rename_to_support_keeps_source_exempt_and_destination_red(self):
        source, destination = "tests/test_existing.py", "tests/helpers/renamed_acceptance.py"
        self.assertFalse(chief.is_test_file(destination))
        self.git("mv", source, destination)
        for state in ("staged", "committed"):
            with self.subTest(state=state):
                if state == "committed":
                    self.git("commit", "-q", "-m", "rename conventional test to non-test support")
                self.assert_conventional_exemption(source)
                for lang in ("en", "zh"):
                    with self.subTest(language=lang), mock.patch.object(chief, "LANG", lang):
                        scope, signals = self.signals()
                        self.assertIn(destination, scope["protected_violations"])
                        self.assertIn(destination, self.messages(signals, "red"))
                        self.assertNotIn(destination, self.messages(signals, "yellow"))

    def test_non_test_protected_rename_source_stays_red_even_when_destination_is_a_test(self):
        source, destination = "tests/helpers/existing.py", "tests/test_renamed_helper.py"
        self.git("mv", source, destination)
        for state in ("staged", "committed"):
            with self.subTest(state=state):
                if state == "committed":
                    self.git("commit", "-q", "-m", "rename protected support to a conventional test")
                self.assert_red_in_every_phase(source, destination)
                self.assert_conventional_exemption(destination)

    def test_rename_outside_test_directory_keeps_the_protected_source_visible(self):
        source, destination = "tests/helpers/existing.py", "src/moved_helper.py"
        self.git("mv", source, destination)
        for state in ("staged", "committed"):
            with self.subTest(state=state):
                if state == "committed":
                    self.git("commit", "-q", "-m", "move protected helper outside tests")
                self.assert_red_in_every_phase(source, destination)

    def test_explicit_file_protection_takes_precedence_over_new_support_review(self):
        path = "tests/explicit_helper.py"
        self.task["protected"].append(path)
        self.write(path, "# explicitly protected support\n")
        self.assert_red_in_every_phase(path)

    def test_generated_and_custom_ignored_files_are_not_review_or_protection_items(self):
        for path in ("tests/__pycache__/helper.pyc", "tests/.pytest_cache/fixture.json",
                     "test/.chief/status.json", "tests/generated-cache/helper.py"):
            self.write(path, "generated\n")
        self.write("tests/helpers/new_helper.py", "# human review required\n")
        scope, signals = self.signals(extra_ignore=["generated-cache"])
        # One proposed test and one support file, both new since the contract base.
        self.assertEqual(scope["changed"], 2)
        for _, message, _ in signals:
            self.assertNotIn("__pycache__", message)
            self.assertNotIn(".pytest_cache", message)
            self.assertNotIn(".chief", message)
            self.assertNotIn("generated-cache", message)

    def test_build_behavior_keeps_support_additions_red_and_new_tests_yellow(self):
        self.write("tests/new_helper.py", "# support created during implementation\n")
        self.write("tests/new_feature_test.py", "# eligible new build-phase test\n")
        self.write("tests/conftest.py", "# build-phase config addition\n")
        self.write("tests/fixtures/new.json", '{"new": true}\n')
        for phase in ("build", "legacy"):
            task = dict(self.task, phase=phase)
            if phase == "legacy":
                task.pop("phase")
                task.pop("test_first")
            for lang in ("en", "zh"):
                with self.subTest(phase=phase, language=lang), mock.patch.object(chief, "LANG", lang):
                    scope, signals = self.signals(task)
                    for path in ("tests/new_helper.py", "tests/conftest.py", "tests/fixtures/new.json"):
                        self.assertIn(path, scope["protected_violations"])
                        self.assertIn(path, self.messages(signals, "red"))
                    self.assertIn("tests/new_feature_test.py", self.messages(signals, "yellow"))
                    self.assertNotIn("tests/new_feature_test.py", self.messages(signals, "red"))


class SupportApprovalTests(ApprovalFixture):
    def commit_support(self, path="tests/helpers/new_helper.py"):
        self.write(path, "support proposed for " + path + "\n")
        self.git("add", path)
        self.git("commit", "-q", "-m", "commit omitted test support")
        return path

    def assert_approval_refused(self):
        before = copy.deepcopy(chief.load_task(self.task["id"]))
        with self.ready_worker() as deliver:
            code, _, _ = self.cli("decide", self.decision["id"], "--accept")
        self.assertEqual(code, 1, "the complete approval set must pass validation before permission is delivered")
        deliver.assert_not_called()
        self.assertEqual(chief.load_task(self.task["id"]), before)
        self.assertEqual(chief.find_decision(self.decision["id"])["state"], "pending")

    def test_explicit_approval_locks_omitted_support_preserves_protections_and_uses_head(self):
        support = ["tests/helpers/new_helper.py", "tests/conftest.py", "tests/fixtures/new sample.json",
                   "test/fixtures/input.csv", "qa/tests/new_helper.py"]
        for path in support:
            self.write(path, "new support for " + path + "\n")
        self.write("src/unrelated_helper.py", "# outside test directories\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "commit helpers omitted from the approval request")
        head = self.git("rev-parse", "HEAD")
        for lang in ("en", "zh"):
            with self.subTest(language=lang), mock.patch.object(chief, "LANG", lang):
                self.reset_waiting()
                with self.ready_worker() as deliver:
                    def receive(*args):
                        locked = chief.load_task(self.task["id"])
                        self.assertEqual(locked["phase"], "build")
                        self.assertCountEqual(locked["protected"], self.task["protected"] + [self.listed] + support)
                        return True
                    deliver.side_effect = receive
                    code, _, err = self.cli("decide", self.decision["id"], "--accept")
                self.assertEqual(code, 0, err)
                deliver.assert_called_once()
                updated = chief.load_task(self.task["id"])
                self.assertEqual(updated["base"], head)
                self.assertEqual(chief.scope_info(str(self.repo), updated)["changed"], 0)
                for path in support + [self.listed]:
                    self.assertIn(path, deliver.call_args[0][2], "the worker must be told the full locked set")
                self.assertNotIn("src/unrelated_helper.py", updated["protected"])
                self.assertNotIn("tests/helpers/existing.py", updated["protected"],
                                 "discovery must distinguish additions from the existing baseline")

    def test_discovery_and_scope_base_use_worker_worktree_not_the_source_checkout(self):
        worker = self.root / "worker"
        self.git("worktree", "add", "-q", "--detach", str(worker))
        path = "tests/worker_helper.py"
        (worker / path).write_text("# only present in the worker\n", encoding="utf-8")
        def worker_git(*args):
            return subprocess.run(["git", "-C", str(worker), "-c", "user.name=Chief Tests",
                                   "-c", "user.email=chief-tests@example.invalid", *args], check=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True).stdout.strip()
        worker_git("add", path)
        worker_git("commit", "-q", "-m", "worker-only support")
        self.write("tests/source_only_helper.py", "# source checkout must not be scanned\n")
        task = dict(self.task, worktree=str(worker))
        chief.save_task(task)
        with self.ready_worker() as deliver:
            code, _, err = self.cli("decide", self.decision["id"], "--accept")
        self.assertEqual(code, 0, err)
        deliver.assert_called_once()
        updated = chief.load_task(self.task["id"])
        self.assertIn(path, updated["protected"])
        self.assertNotIn("tests/source_only_helper.py", updated["protected"])
        self.assertEqual(updated["base"], worker_git("rev-parse", "HEAD"))

    def test_untracked_omitted_support_prevents_approval(self):
        self.write("tests/helpers/new_helper.py", "# untracked support\n")
        self.assert_approval_refused()

    def test_staged_but_uncommitted_omitted_support_prevents_approval(self):
        self.write("test/conftest.py", "# staged support\n")
        self.git("add", "test/conftest.py")
        self.assert_approval_refused()

    def test_missing_newly_committed_omitted_support_prevents_approval(self):
        path = self.commit_support()
        (self.repo / path).unlink()
        self.assert_approval_refused()

    def test_unstaged_edits_to_omitted_support_prevent_approval(self):
        path = self.commit_support()
        self.write(path, "# modified after the support commit\n")
        self.assert_approval_refused()

    def test_staged_edits_to_omitted_support_prevent_approval(self):
        path = self.commit_support()
        self.write(path, "# staged change after the support commit\n")
        self.git("add", path)
        self.assert_approval_refused()

    def test_missing_listed_test_still_prevents_approval(self):
        (self.repo / self.listed).unlink()
        self.assert_approval_refused()

    def test_untracked_listed_test_still_prevents_approval(self):
        path = "tests/test_untracked_proposal.py"
        self.write(path, "# untracked listed proposal\n")
        self.decision["question"] = "approve the acceptance tests " + path
        chief.update_decision(self.decision)
        self.assert_approval_refused()

    def test_staged_listed_test_edits_still_prevent_approval(self):
        self.write(self.listed, "# staged edit to listed tests\n")
        self.git("add", self.listed)
        self.assert_approval_refused()

    def test_generated_files_are_not_discovered_or_locked_and_do_not_block_approval(self):
        support = self.commit_support()
        ignored = ("tests/__pycache__/helper.pyc", "tests/.pytest_cache/data.json",
                   "test/.chief/status.json", "tests/generated-cache/helper.py")
        for path in ignored:
            self.write(path, "generated output\n")
        cfg = dict(self.cfg, scope_ignore=["generated-cache"])
        with self.ready_worker() as deliver:
            code, _, err = self.cli_with_config(cfg)
        self.assertEqual(code, 0, err)
        deliver.assert_called_once()
        updated = chief.load_task(self.task["id"])
        self.assertIn(support, updated["protected"])
        for path in ignored:
            self.assertNotIn(path, updated["protected"])

    def cli_with_config(self, cfg):
        with mock.patch.object(chief, "config", return_value=cfg):
            return self.cli("decide", self.decision["id"], "--accept")

    def test_confirmation_approval_also_locks_omitted_support(self):
        support = self.commit_support()
        app = self.inbox()
        with self.ready_worker() as deliver:
            self.submit_reply(app, "可以，LGTM")
            self.assertIsNotNone(app.st["confirm"])
            deliver.assert_not_called()
            app.answered("y")
        self.assertIn(support, chief.load_task(self.task["id"])["protected"])
        self.assertEqual(chief.load_task(self.task["id"])["phase"], "build")

    def test_confirmation_approval_cannot_bypass_omitted_support_validation(self):
        self.write("tests/helpers/new_helper.py", "# untracked omitted support\n")
        app = self.inbox()
        with self.ready_worker() as deliver:
            self.submit_reply(app, "okay")
            self.assertIsNotNone(app.st["confirm"])
            with self.assertRaises(RuntimeError):
                app.answered("y")
            deliver.assert_not_called()
        self.assert_test_contract()

    def test_cli_consent_feedback_never_locks_discovered_support(self):
        self.commit_support()
        with self.ready_worker() as deliver, mock.patch("builtins.input") as ask:
            code, out, err = self.cli("decide", self.decision["id"], "--reply", "okay 可以")
        self.assertEqual(code, 0, err)
        self.assertIn("--accept", out)
        ask.assert_not_called()
        deliver.assert_called_once()
        self.assert_test_contract()

    def test_locked_support_changes_are_red_after_approval(self):
        helper = self.commit_support()
        fixture = self.commit_support("test/fixtures/new.json")
        with self.ready_worker():
            code, _, err = self.cli("decide", self.decision["id"], "--accept")
        self.assertEqual(code, 0, err)
        task = chief.load_task(self.task["id"])
        self.assertIn(helper, task["protected"])
        self.assertIn(fixture, task["protected"])
        self.write(helper, "# changed approved support\n")
        (self.repo / fixture).unlink()
        for lang in ("en", "zh"):
            with self.subTest(language=lang), mock.patch.object(chief, "LANG", lang):
                scope = chief.scope_info(str(self.repo), task)
                self.assertIn(helper, scope["protected_violations"])
                self.assertIn(fixture, scope["protected_violations"])
