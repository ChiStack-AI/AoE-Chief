"""Regressions for UI-owned recheck recovery and hidden path completion.

Keep the approved acceptance files unchanged. Real Inbox jobs and temporary Git
repositories exercise recovery; completion is also driven through the editor.
"""
import os
import threading
from unittest import mock

import chief_tui as tui
from test_inbox_composer import Action, Check, ComposerFixture


class RecheckRecoveryTests(ComposerFixture):
    def test_failed_recheck_restores_input_only_when_ui_polls(self):
        owner = threading.get_ident()
        intent = "  Import CSV  "
        before = self.artifacts()
        self.app._refresh()
        for active in ("intent", "repo"):
            with self.subTest(active=active):
                self.app.key("N")
                for key in intent:
                    self.app.typed(key)
                if active == "repo":
                    self.app.typed("KEY_DOWN")
                restores = []
                original = self.app.composer_input

                def restore(new):
                    restores.append(threading.get_ident())
                    return original(new)

                with mock.patch.object(self.app, "composer_input", side_effect=restore), \
                        mock.patch.object(self.app, "resolve_repository", side_effect=ValueError("recheck rejected")):
                    try:
                        self.app.typed("\n")
                        self.idle()  # Let the worker finish without pumping UI results.
                        self.assertEqual(restores, [], "the draft thread must not call composer_input")
                        self.assertIsNone(self.app.st["input"], "only the UI poll may reopen input")
                        self.app.validate_repository()
                        self.assertEqual(restores, [owner])
                        self.assert_fields(intent.rstrip(), self.repo)
                        self.assertEqual(self.app.st["new_task"]["intent"], intent)
                        self.assertEqual(self.app.st["input"]["buf"],
                                         intent if active == "intent" else str(self.repo))
                        self.assert_invalid()
                        self.draft.assert_not_called()
                        self.no_submission()
                    finally:
                        self.idle()
                        self.app.cancel_new()
        self.assertEqual(self.artifacts(), before)

    def test_late_failed_recheck_cannot_replace_a_new_composer(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        before = self.artifacts()
        self.app._refresh()
        self.app.key("N")
        for key in "Old intent":
            self.app.typed(key)

        def reject(path):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("recheck blocked cancellation")
            raise ValueError("late recheck failure")

        try:
            with mock.patch.object(self.app, "resolve_repository", side_effect=reject):
                self.app.typed("\n")
                self.assertTrue(entered.wait(1))
                self.app.key("\x1b")
                release.set()
                self.idle()
                self.app.key("N")
                for key in "Replacement intent":
                    self.app.typed(key)
                replacement, input_line = self.app.st["new_task"], self.app.st["input"]
                self.app.validate_repository()
                self.assertIs(self.app.st["new_task"], replacement)
                self.assertIs(self.app.st["input"], input_line)
                self.assert_fields("Replacement intent", self.repo)
                self.assert_valid(self.repo)
                self.draft.assert_not_called()
                self.no_submission()
        finally:
            release.set()
            self.idle()
            self.app.cancel_new()
        self.assertEqual(self.artifacts(), before)

    def test_failed_git_recheck_restores_both_buffers_on_ui_thread(self):
        other = self.make_repo("recheck source")
        owner, calls = threading.get_ident(), []
        intent = "  Import CSV  "
        original = self.app.composer_input
        before = self.artifacts()

        def restore(new):
            calls.append(threading.get_ident())
            return original(new)

        def restored():
            self.assert_fields(intent.rstrip(), other)
            self.assertEqual(self.app.st["new_task"]["intent"], intent)
            self.assert_invalid()
            self.assert_idle()

        with mock.patch.object(self.app, "composer_input", side_effect=restore):
            self.scenario(*self.open_repo(other, intent), Check(lambda: self.assert_valid(other)),
                          Action(lambda: (other / ".git").rename(other / ".git-removed")),
                          "\n", Check(restored), "\x1b")
        self.assertGreaterEqual(len(calls), 3, "exercise initial focus, navigation and failed recheck recovery")
        self.assertEqual(set(calls), {owner}, "every input restoration must run on the terminal thread")
        self.draft.assert_not_called()
        self.assertEqual(self.artifacts(), before)


class HiddenDirectoryCompletionTests(ComposerFixture):
    def test_empty_name_prefix_ignores_hidden_directories_and_symlinks(self):
        parent = self.root / "directory choices"
        parent.mkdir()
        visible = parent / "visible project"
        visible.mkdir()
        (parent / ".cache").mkdir()
        (parent / ".shortcut").symlink_to(self.repo, target_is_directory=True)
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            for spelling in (str(parent) + "/", "../directory choices/", "~/directory choices/"):
                with self.subTest(path=spelling):
                    self.assertEqual(tui.complete_directory(spelling), spelling + "visible project/")

    def test_only_hidden_children_leave_parent_path_unchanged(self):
        parent = self.root / "only hidden children"
        parent.mkdir()
        (parent / ".cache").mkdir()
        spelling = str(parent) + "/"
        self.assertEqual(tui.complete_directory(spelling), spelling)

    def test_explicit_dot_prefix_completes_hidden_matches_and_common_prefix(self):
        parent = self.root / "dot choices"
        parent.mkdir()
        for name in (".settings one", ".settings other", "visible"):
            (parent / name).mkdir()
        self.assertEqual(tui.complete_directory(str(parent) + "/.s"),
                         str(parent) + "/.settings o")
        self.assertEqual(tui.complete_directory(str(parent) + "/.settings on"),
                         str(parent) + "/.settings one/")

    def test_hidden_parent_does_not_enable_hidden_children_in_repository_tab(self):
        parent = self.root / ".container"
        parent.mkdir()
        (parent / ".child").mkdir()
        visible = parent / "visible child"
        visible.symlink_to(self.repo, target_is_directory=True)
        before = self.artifacts()
        self.assertEqual(tui.complete_directory(str(parent) + "/."), str(parent / ".child") + "/")
        self.scenario(*self.open_repo(str(parent) + "/"), "\t",
                      Check(lambda: self.assert_fields("Import CSV", str(visible) + "/")),
                      Check(lambda: self.assert_valid(self.repo)), "\n",
                      Check(lambda: self.drafted(self.repo)), "\x1b")
        self.assertEqual(self.artifacts(), before)
