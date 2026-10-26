"""Regression coverage for Enter while repository validation is pending.

The approved composer tests remain unchanged. Exercise visible feedback, the
worker-result boundary and the requirement for a fresh Enter after validation.
"""
import os
import threading

import chief
import chief_tui as tui
from test_inbox_composer import Action, Check, ComposerFixture


class PendingEnterTests(ComposerFixture):
    def visible(self):
        return "\n".join(line.rstrip() for line in self.frame() if line.strip())

    def hold_validation(self, path):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = tui.source_repository

        def resolve(ch, candidate):
            if os.path.realpath(os.path.expanduser(str(candidate))) != str(path):
                return original(ch, candidate)
            entered.set()
            try:
                if not release.wait(3):
                    raise RuntimeError("validation blocked terminal input")
                return original(ch, candidate)
            finally:
                finished.set()

        self.addCleanup(release.set)
        self.patch_object(tui, "source_repository", side_effect=resolve)
        return entered, release, finished

    def assert_checking_feedback(self, path):
        self.assert_not_drafted()
        self.assert_fields("Import CSV", path)
        screen = self.visible()
        self.assertIn("Checking repository…", screen)
        self.assertIn("Enter again", screen, "a blocked Enter must visibly explain how to proceed")

    def test_pending_enter_from_both_lines_requires_another_enter_after_validation(self):
        other = self.make_repo("slow repository")
        entered, release, finished = self.hold_validation(other)
        before = self.artifacts()

        def pending():
            self.assertTrue(entered.is_set())
            self.assert_pending()
            self.assertNotIn("Enter again", self.visible())

        def ready():
            self.assertTrue(finished.is_set())
            self.assert_valid(other)
            self.assert_not_drafted()
            self.assert_fields("Import CSV", other)
            self.assertIn("Enter again", self.visible())

        events = self.open_repo(other) + [Check(pending)]
        for move in ((), (tui.curses.KEY_UP,)):
            events.append(move)
            for key in ("\n", "\r", tui.curses.KEY_ENTER):
                events.extend([(key,), Check(lambda: self.assert_checking_feedback(other))])
        try:
            self.scenario(*events, Action(release.set), Check(ready),
                          (tui.curses.KEY_DOWN,), Check(ready), "\n",
                          Check(lambda: self.drafted(other)), "\x1b")
        finally:
            release.set()
            if entered.is_set():
                self.assertTrue(finished.wait(3))
        self.assertEqual(self.artifacts(), before)

    def test_queued_success_cannot_turn_a_pending_enter_into_a_submission(self):
        other = self.make_repo("completed worker")
        before = self.artifacts()
        self.app._refresh()
        self.app.key("N")
        for key in [*"Import CSV", "KEY_DOWN", "\x15", *str(other)]:
            self.app.typed(key)
        new = self.app.st["new_task"]
        self.assertEqual(new["repo_status"], "pending")
        # The worker finished just before the key arrived, but its result has
        # not yet been applied and the UI still displays the pending state.
        self.app.validation_results.put((new, new["repo_version"], str(other), ""))
        try:
            self.app.typed("\n")
            self.assert_checking_feedback(other)
            self.app.validate_repository()
            self.assert_valid(other)
            self.assert_not_drafted()
            self.app.typed("\n")
            self.idle()
            self.drafted(other)
            self.app.cancel_new()
        finally:
            self.idle()
        self.assertEqual(self.artifacts(), before)

    def test_pending_enter_feedback_is_localized_and_escape_still_cancels(self):
        before = self.artifacts()
        for language, checking, retry in (("en", "Checking repository…", "Enter again"),
                                          ("zh", "正在检查仓库", "再次按 Enter")):
            with self.subTest(language=language):
                chief.LANG = language
                self.app._refresh()
                self.app.key("N")
                # Editing the path invalidates the default immediately. No
                # event loop is running, so debounce cannot finish here.
                for key in [*"Import CSV", "KEY_DOWN", "/", "\r"]:
                    self.app.typed(key)
                screen = self.visible()
                self.assertIn(checking, screen)
                self.assertIn(retry, screen)
                self.assert_not_drafted()
                self.app.typed("\x1b")
                self.assertIsNone(self.app.st.get("new_task"))
                self.assertIsNone(self.app.st["input"])
        self.assertEqual(self.artifacts(), before)
