"""Cover AoE configuration and partial success outside the approved fixtures."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import chief
from test_cleanup_acceptance import CleanupFixture
from test_intent_task_flow import ChiefFixture


REAL_AOE = chief.aoe


class TrashConfigurationTests(unittest.TestCase):
    def test_remove_checks_selected_profiles_trash_setting_before_running(self):
        cfg = {"profile": "cleanup-qa"}
        with mock.patch.object(chief, "aoe_bin", return_value="aoe-test"), \
                mock.patch.object(chief, "call", side_effect=[
                    "session.delete_to_trash = true\n  source: schema default\n", "trashed"]) as call:
            self.assertEqual(REAL_AOE(cfg, "remove", "sid", "--delete-worktree", "--force"), "trashed")
        self.assertEqual(call.call_args_list, [
            mock.call(["aoe-test", "-p", "cleanup-qa", "settings", "explain", "session.delete_to_trash"]),
            mock.call(["aoe-test", "-p", "cleanup-qa", "remove", "sid", "--delete-worktree", "--force"])])

    def test_disabled_or_unknown_trash_setting_never_runs_remove(self):
        for setting in ("session.delete_to_trash = false\n", "unrecognized settings output\n"):
            with self.subTest(setting=setting), mock.patch.object(chief, "aoe_bin", return_value="aoe-test"), \
                    mock.patch.object(chief, "call", return_value=setting) as call:
                with self.assertRaisesRegex(RuntimeError, "enable session.delete_to_trash"):
                    REAL_AOE({"profile": "cleanup-qa"}, "remove", "sid", "--delete-worktree", "--force")
                call.assert_called_once()
                self.assertIn("explain", call.call_args[0][0])

    def test_archiving_does_not_require_trash_to_be_enabled(self):
        with mock.patch.object(chief, "aoe_bin", return_value="aoe-test"), \
                mock.patch.object(chief, "call", return_value="archived") as call:
            REAL_AOE({"profile": "cleanup-qa"}, "session", "archive", "sid")
        call.assert_called_once_with(["aoe-test", "-p", "cleanup-qa", "session", "archive", "sid"])

    def test_profile_cannot_disable_trash_even_when_global_settings_report_true(self):
        declarations = (
            "[session]\ndelete_to_trash = false\n",
            "session = { delete_to_trash = false }\n",
            'session."delete\\u005fto_trash" = false\n',
            "[session]\ndelete_to_trash = 'unknown'\n",
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            profile = root / "profiles" / "cleanup-qa" / "config.toml"
            profile.parent.mkdir(parents=True)
            for declaration in declarations:
                with self.subTest(declaration=declaration):
                    profile.write_text(declaration)
                    with mock.patch.object(chief, "AOE_HOME", root), \
                            mock.patch.object(chief, "aoe_bin", return_value="aoe-test"), \
                            mock.patch.object(chief, "call", return_value="session.delete_to_trash = true\n") as call:
                        with self.assertRaisesRegex(RuntimeError, "profile trash override"):
                            REAL_AOE({"profile": "cleanup-qa"}, "remove", "sid", "--delete-worktree", "--force")
                        call.assert_called_once()

    def test_plain_true_profile_override_allows_trash(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            profile = root / "profiles" / "cleanup-qa" / "config.toml"
            profile.parent.mkdir(parents=True)
            profile.write_text('[session]\n"delete_to_trash" = true # keep removal reversible\n')
            with mock.patch.object(chief, "AOE_HOME", root), \
                    mock.patch.object(chief, "aoe_bin", return_value="aoe-test"), \
                    mock.patch.object(chief, "call", side_effect=["session.delete_to_trash = true\n", "trashed"]):
                self.assertEqual(REAL_AOE({"profile": "cleanup-qa"}, "remove", "sid"), "trashed")


class PartialRemovalTests(CleanupFixture):
    def test_zero_exit_with_unmoved_worktree_does_not_record_cleanup_and_continues(self):
        failed = self.task("a-partial")
        good = self.task("b-continues")
        real_aoe = self.fake_aoe

        def partial(cfg, *args):
            if args[0] == "remove" and args[1] == failed["session_id"]:
                self.operations.append(tuple(args))
                self.sessions[args[1]].update(state="trashed", trashed_at="2026-10-26T12:00:00Z")
                self.write_records()
                return "Moved session to trash (worktree relocation failed)"
            return real_aoe(cfg, *args)

        with mock.patch.object(chief, "aoe", side_effect=partial):
            code, out, _ = self.clean("--branches")
        self.assertNotEqual(code, 0)
        self.assertIn("left the worktree in place", out)
        self.assertEqual(self.stored(failed), failed)
        self.assertTrue(Path(failed["worktree"]).exists())
        self.assertTrue(self.branch_exists(failed["branch"]))
        self.assert_removed(good)

    def test_zero_exit_without_trash_transition_cannot_close_task_successfully(self):
        task = self.task(state="review")
        real_aoe = self.fake_aoe

        def missing_transition(cfg, *args):
            result = real_aoe(cfg, *args)
            if args[0] == "remove":
                self.sessions[task["session_id"]].update(state="archived", trashed_at=None)
                self.write_records()
            return result

        with mock.patch.object(chief, "aoe", side_effect=missing_transition):
            code, _, err = self.cli("close", task["id"], "--remove-session")
        self.assertNotEqual(code, 0)
        self.assertIn("did not confirm", err)
        self.assertEqual(self.stored(task), task)
        self.assertTrue(self.branch_exists(task["branch"]))


class ScopeIndexRegressionTests(ChiefFixture):
    def setUp(self):
        super().setUp()
        self.write("tests/test_existing.py", "# original protected tests\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "protected scope base")
        self.task = dict(goal="add behavior", scope=".", protected=["tests"],
                         phase="build", base=self.git("rev-parse", "HEAD"))

    def signals(self):
        scope = chief.scope_info(str(self.repo), self.task)
        return chief.judge(dict(status="idle", task=self.task, scope=scope, log={"turn": "done"}))

    def test_staged_protected_edit_is_red_when_working_file_has_been_restored(self):
        path = "tests/test_existing.py"
        self.write(path, "# staged unapproved changes\n")
        self.git("add", path)
        self.write(path, "# original protected tests\n")
        self.assertTrue(any(level == "red" and path in message for level, message, _ in self.signals()))

    def test_staged_new_test_is_yellow_when_working_file_has_been_deleted(self):
        path = "tests/test_staged.py"
        self.write(path, "# staged new regression\n")
        self.git("add", path)
        (self.repo / path).unlink()
        signals = self.signals()
        self.assertTrue(any(level == "yellow" and path in message for level, message, _ in signals))
        self.assertFalse(any(level == "red" and path in message for level, message, _ in signals))
