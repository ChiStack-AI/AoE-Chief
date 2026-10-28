"""Acceptance contract for the editable Inbox N composer and queued pastes.

Exercise the real event loop, renderer and Git resolution. Reuse only the
existing test fixture's isolated repositories and mocked AI/AoE boundaries;
no new production method or state key is required by these tests.
"""
from collections import deque
import math
import os
from pathlib import Path
import re
import threading
import time
import unittest
from unittest import mock

import chief
import chief_tui as tui
import test_inbox_delegation as delegation


class Check:
    """Wait for an observable condition at an idle terminal boundary."""

    def __init__(self, fn):
        self.fn = fn
        self.deadline = None


class Action:
    """Perform a test action once, after the preceding input was dispatched."""

    def __init__(self, fn):
        self.fn = fn


class ComposerFixture(delegation.InboxFixture):
    def setUp(self):
        super().setUp()
        self.read_timeout = 250
        self.read_count = 0
        self.draw_count = 0
        self.draw_positions = []
        self.last_rows = []
        self.scr.timeout.side_effect = self.set_timeout
        self.scr.nodelay.side_effect = lambda enabled: self.set_timeout(0 if enabled else -1)
        names = {getattr(tui.curses, name): name.encode() for name in
                 ("KEY_DOWN", "KEY_UP", "KEY_BACKSPACE", "KEY_ENTER")}
        self.patch_object(tui.curses, "keyname", side_effect=lambda key: names[key])

        def draw(scr, rows, styles, width, height):
            self.draw_count += 1
            self.draw_positions.append(self.read_count)
            self.last_rows = rows

        # Avoid retaining a full terminal frame per character in mock history.
        self.patch_object(tui, "draw", new=draw)

    def set_timeout(self, delay):
        self.read_timeout = delay

    def scenario(self, *events):
        """Strings are queued bursts; Check/Action separate terminal bursts.

        A nonblocking read sees curses.error at a barrier, just as it would at
        the end of a real paste. Checks never run during input read-ahead.
        Both timeout(0) and nodelay(True) are supported, with real time allowed
        for background validation and debounce rather than mocked timers.
        """
        script = deque([Check(self.assert_idle), *events])
        pending = deque()
        deadline = time.monotonic() + 60

        def read_key():
            if time.monotonic() > deadline:
                self.fail("terminal scenario did not complete within 60 seconds")
            while not pending:
                if not script:
                    if self.read_timeout == 0:
                        raise tui.curses.error()
                    raise delegation.EndOfScript()
                event = script[0]
                if isinstance(event, (Check, Action)):
                    if self.read_timeout == 0:
                        raise tui.curses.error()
                    if isinstance(event, Action):
                        event.fn()
                        script.popleft()
                    else:
                        if event.deadline is None:
                            event.deadline = time.monotonic() + 1.5
                        try:
                            event.fn()
                        except AssertionError:
                            if time.monotonic() >= event.deadline:
                                raise
                            # An idle terminal blocks instead of producing an
                            # artificial redraw storm while debounce runs.
                            delay = self.read_timeout / 1000 if self.read_timeout > 0 else 0.05
                            time.sleep(min(delay, 0.05))
                        else:
                            script.popleft()
                    raise tui.curses.error()
                script.popleft()
                pending.extend(event)
            self.read_count += 1
            return pending.popleft()

        self.scr.get_wch.side_effect = read_key
        try:
            with self.assertRaises(delegation.EndOfScript):
                self.app.loop(self.scr)
        finally:
            self.idle()

    def assert_idle(self):
        self.assertFalse(self.app.st.get("busy"))
        self.assertFalse(self.app.lock.locked())

    def editor(self):
        rows = tui.render(chief, self.app.st, 240, 50)
        found = {}
        for index, row in enumerate(rows):
            text = "".join(part for part, _ in row)
            for name, label in (("intent", r"(?:task\s+)?intent"), ("repo", r"(?:repository|repo)")):
                match = re.search(r"\b" + label + r":\s?(.*)", text, re.I)
                if match:
                    found[name] = (index, match.group(1).rstrip(), row)
        self.assertEqual(set(found), {"intent", "repo"}, "show both labeled composer lines")
        self.assertEqual(found["repo"][0], found["intent"][0] + 1,
                         "the repository editor must be immediately below the intent editor")
        return found

    def assert_fields(self, intent, repo):
        fields = self.editor()
        self.assertEqual(fields["intent"][1], intent)
        self.assertEqual(fields["repo"][1], str(repo))
        self.assertIsNotNone(self.app.st["input"], "both buffers must remain editable")

    def assert_valid(self, repo):
        lines = self.frame()
        self.assertNotRegex("\n".join(lines).lower(), r"pending|checking|validating")
        status = [line for line in lines if re.search(
            r"\b(valid|resolved|toplevel|top[- ]level|git root)\b", line, re.I)]
        self.assertTrue(any(str(repo) in line for line in status),
                        "visibly show the resolved Git toplevel, separately from the editable path")

    def assert_invalid(self):
        self.assertRegex("\n".join(self.frame()).lower(),
                         r"invalid|not (?:a )?(?:git|directory)|not found|does not exist|"
                         r"missing|no repository|repository.*required|repository.*empty")

    def assert_pending(self):
        self.assertRegex("\n".join(self.frame()).lower(), r"pending|checking|validating")

    def assert_not_drafted(self):
        self.draft.assert_not_called()
        self.no_submission()
        self.assertIsNotNone(self.app.st["input"])

    def open_repo(self, path, intent="Import CSV"):
        return ["N", intent, (tui.curses.KEY_DOWN,), "\x15", str(path)]

    def drafted(self, repo, intent="Import CSV"):
        self.assert_idle()
        self.draft.assert_called_once_with(self.cfg, str(repo), intent)
        self.preview()
        self.no_submission()


class ComposerNavigationTests(ComposerFixture):
    def test_t_stays_literal_text_in_both_composer_lines_and_never_ticks(self):
        target = self.make_repo("trust target")
        tick = self.patch_object(chief, "tick", return_value=[])
        before = self.artifacts()
        self.scenario("N", "tT trust this task", (tui.curses.KEY_DOWN,), "\x15", str(target),
                      Check(lambda: self.assert_fields("tT trust this task", target)),
                      Check(lambda: self.assert_valid(target)), "\n",
                      Check(lambda: self.drafted(target, "tT trust this task")),
                      Check(lambda: self.assertIn("t trust: off", self.frame()[-1])), "\x1b")
        tick.assert_not_called()
        self.assertEqual(self.artifacts(), before)

    def test_intent_is_first_repository_second_and_focus_is_visible(self):
        focused = []

        def first():
            self.assert_fields("", self.repo)
            focused.append((self.editor()["intent"][2], self.editor()["repo"][2]))

        def second():
            self.assert_fields("", self.repo)
            rows = (self.editor()["intent"][2], self.editor()["repo"][2])
            self.assertNotEqual(rows, focused[0], "show which editor line is active")
            focused.append(rows)

        def back():
            self.assert_fields("", self.repo)
            self.assertEqual((self.editor()["intent"][2], self.editor()["repo"][2]), focused[0])

        self.scenario("N", Check(first), (tui.curses.KEY_DOWN,), Check(second),
                      (tui.curses.KEY_UP,), Check(back), "\x1b")
        self.draft.assert_not_called()

    def test_navigation_preserves_both_buffers_and_clear_and_backspace_affect_only_active_line(self):
        other = self.make_repo("other project")
        before = self.artifacts()
        self.scenario("N", "Keep 雪X", (tui.curses.KEY_BACKSPACE, tui.curses.KEY_DOWN),
                      "\x15", str(other) + "X", "\x7f",
                      Check(lambda: self.assert_fields("Keep 雪", other)),
                      (tui.curses.KEY_UP,), " receipts",
                      Check(lambda: self.assert_fields("Keep 雪 receipts", other)),
                      (tui.curses.KEY_DOWN,), "\x15",
                      Check(lambda: self.assert_fields("Keep 雪 receipts", "")), str(other),
                      Check(lambda: self.assert_valid(other)), "\n",
                      Check(lambda: self.drafted(other, "Keep 雪 receipts")), "\x1b")
        self.assertEqual(self.artifacts(), before)

    def test_enter_from_either_line_drafts_with_trimmed_intent_and_latest_valid_repo(self):
        other = self.make_repo("edited project")
        subdir = other / "src"
        subdir.mkdir()
        before = self.artifacts()
        for line in ("intent", "repo"):
            for enter in ("\n", "\r", tui.curses.KEY_ENTER):
                with self.subTest(line=line, enter=enter):
                    self.draft.reset_mock()
                    move = (tui.curses.KEY_UP,) if line == "intent" else ()
                    self.scenario(*self.open_repo(subdir, "  Import CSV  "),
                                  Check(lambda: self.assert_valid(other)), move, (enter,),
                                  Check(lambda: self.drafted(other)), "\x1b")
        self.assertEqual(self.artifacts(), before)

    def test_blank_intent_stays_editable_and_cannot_submit_from_either_line(self):
        before = self.artifacts()
        self.scenario("N", "   ", Check(lambda: self.assert_valid(self.repo)),
                      "\n\r", (tui.curses.KEY_ENTER, tui.curses.KEY_DOWN), "\n\r",
                      (tui.curses.KEY_ENTER,), Check(self.assert_not_drafted),
                      (tui.curses.KEY_UP,), "Import CSV", "\n",
                      Check(lambda: self.drafted(self.repo)), "\x1b")
        self.assertEqual(self.artifacts(), before)

    def test_invalid_paths_are_visible_and_all_enter_variants_leave_input_editable(self):
        plain = self.root / "not a repository"
        plain.mkdir()
        regular_file = self.root / "a file"
        regular_file.write_text("not a directory", encoding="utf-8")
        before = self.artifacts()
        for path in ("", str(self.root / "missing"), str(plain), str(regular_file)):
            with self.subTest(path=path):
                self.scenario(*self.open_repo(path), Check(self.assert_invalid), "\n\r",
                              (tui.curses.KEY_ENTER,), Check(self.assert_not_drafted),
                              (tui.curses.KEY_UP,), "\n\r", (tui.curses.KEY_ENTER,),
                              Check(self.assert_not_drafted), "\x1b")
        self.assertEqual(self.artifacts(), before)

    def test_invalid_edit_never_reuses_old_valid_repo_and_can_be_corrected_in_place(self):
        other = self.make_repo("corrected")
        before = self.artifacts()
        self.scenario("N", "Import CSV", Check(lambda: self.assert_valid(self.repo)),
                      (tui.curses.KEY_DOWN,), "\x15", str(self.root / "missing"), "\n",
                      Check(self.assert_not_drafted), Check(self.assert_invalid), "\x15", str(other),
                      Check(lambda: self.assert_fields("Import CSV", other)),
                      Check(lambda: self.assert_valid(other)), "\n",
                      Check(lambda: self.drafted(other)), "\x1b")
        self.assertEqual(self.artifacts(), before)

    def test_manual_paths_keep_source_normalization_for_subdirs_aliases_and_worktrees(self):
        other = self.make_repo("elsewhere")
        subdir = other / "nested"
        subdir.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(subdir, target_is_directory=True)
        worktree = self.worktree()
        cases = [(subdir, other), (alias, other), (worktree, self.repo),
                 (os.path.relpath(subdir, self.repo), other), ("~/elsewhere/nested", other)]
        before = self.artifacts()
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            for spelling, resolved in cases:
                with self.subTest(path=str(spelling)):
                    self.draft.reset_mock()
                    self.scenario(*self.open_repo(spelling),
                                  Check(lambda: self.assert_fields("Import CSV", spelling)),
                                  Check(lambda: self.assert_valid(resolved)), "\n",
                                  Check(lambda: self.drafted(resolved)), "\x1b")
        self.assertEqual(self.artifacts(), before)


class CompletionTests(ComposerFixture):
    def complete(self, initial, expected):
        self.scenario(*self.open_repo(initial), "\t",
                      Check(lambda: self.assert_fields("Import CSV", expected)), "\x1b")
        self.draft.assert_not_called()

    def test_unique_completion_preserves_absolute_relative_and_home_path_spelling(self):
        parent = self.root / "projects"
        parent.mkdir()
        (parent / "receipts").mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            for prefix in (str(parent) + "/", "../projects/", "~/projects/"):
                with self.subTest(prefix=prefix):
                    self.complete(prefix + "rec", prefix + "receipts/")

    def test_relative_completion_without_dot_prefix_and_chained_directory_tabs(self):
        parent = self.repo / "source code"
        parent.mkdir()
        (parent / "receipts").mkdir()
        self.scenario(*self.open_repo("sou"), "\t",
                      Check(lambda: self.assert_fields("Import CSV", "source code/")), "rec\t",
                      Check(lambda: self.assert_fields("Import CSV", "source code/receipts/")),
                      Check(lambda: self.assert_valid(self.repo)), "\n",
                      Check(lambda: self.drafted(self.repo)), "\x1b")

    def test_completion_keeps_spaces_and_quote_characters_literal(self):
        parent = self.root / "my projects"
        parent.mkdir()
        target = parent / "receipt books ' [final]"
        target.mkdir()
        self.complete(str(parent / "receipt b"), str(target) + "/")

    def test_ambiguous_directory_matches_extend_only_to_longest_common_prefix(self):
        parent = self.root / "choices"
        parent.mkdir()
        for name in ("project alpha", "project alpine"):
            (parent / name).mkdir()
        (parent / "project aardvark.txt").write_text("ignore files", encoding="utf-8")
        common = str(parent / "project alp")
        self.scenario(*self.open_repo(parent / "pro"), "\t",
                      Check(lambda: self.assert_fields("Import CSV", common)), "\t",
                      Check(lambda: self.assert_fields("Import CSV", common)), "ha\t",
                      Check(lambda: self.assert_fields("Import CSV", str(parent / "project alpha") + "/")),
                      "\x1b")
        self.draft.assert_not_called()

    def test_exact_directory_name_still_ambiguous_with_longer_sibling_gets_no_slash(self):
        parent = self.root / "prefixes"
        parent.mkdir()
        (parent / "alpha").mkdir()
        (parent / "alphabet").mkdir()
        self.complete(str(parent / "al"), str(parent / "alpha"))

    def test_completion_ignores_files_accepts_directory_symlinks_and_leaves_no_match_unchanged(self):
        parent = self.root / "candidates"
        parent.mkdir()
        (parent / "repo link").symlink_to(self.repo, target_is_directory=True)
        (parent / "repo file").write_text("ignore", encoding="utf-8")
        self.complete(str(parent / "repo"), str(parent / "repo link") + "/")
        self.complete(str(parent / "absent"), str(parent / "absent"))
        self.complete(str(parent / "repo file"), str(parent / "repo file"))

    def test_empty_repository_tabs_cycle_deduplicated_known_repos_in_stable_order(self):
        other, unstarted = self.make_repo("thinking"), self.make_repo("unstarted")
        worktree = self.worktree()
        alias = self.root / "alias"
        alias.symlink_to(other, target_is_directory=True)
        chief.save_task({"id": "unstarted", "state": "draft", "repo": str(unstarted)})
        self.items = [delegation.item("1111111111111111", "here", self.repo),
                      delegation.item("2222222222222222", "thinking", other),
                      delegation.item("3333333333333333", "alias", alias),
                      delegation.item("4444444444444444", "worktree", worktree)]
        cycles = []
        before = self.artifacts()
        for reverse in (False, True):
            if reverse:
                self.items.reverse()
            seen = []

            def remember():
                fields = self.editor()
                self.assertEqual(fields["intent"][1], "Import CSV")
                self.assertIn(fields["repo"][1], {str(self.repo), str(other), str(unstarted)})
                seen.append(fields["repo"][1])

            events = self.open_repo("")
            for _ in range(4):
                events.extend(["\t", Check(remember)])
            self.scenario(*events, "\x1b")
            self.assertEqual(set(seen[:3]), {str(self.repo), str(other), str(unstarted)})
            self.assertEqual(seen[0], seen[3], "aliases/worktrees must not add duplicate choices")
            cycles.append(seen)
        self.assertEqual(cycles[0], cycles[1])
        self.draft.assert_not_called()
        self.assertEqual(self.artifacts(), before)

    def test_intent_tab_keeps_existing_repo_cycle_and_preserves_intent(self):
        other = self.make_repo("thinking")
        self.items = [delegation.item("1111111111111111", "here", self.repo),
                      delegation.item("2222222222222222", "there", other)]
        self.scenario("N", "Import CSV", "\t",
                      Check(lambda: self.assert_fields("Import CSV", other)),
                      Check(lambda: self.assert_valid(other)), "\n",
                      Check(lambda: self.drafted(other)), "\x1b")


class LiveValidationTests(ComposerFixture):
    def slow_validation(self, path):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = tui.source_repository

        def resolve(ch, candidate):
            if os.path.realpath(os.path.expanduser(str(candidate))) != str(path):
                return original(ch, candidate)
            entered.set()
            try:
                if not release.wait(3):
                    raise RuntimeError("repository validation blocked terminal input")
                return original(ch, candidate)
            finally:
                finished.set()

        self.addCleanup(release.set)
        self.patch_object(tui, "source_repository", side_effect=resolve)
        return entered, release, finished

    def test_pending_validation_is_visible_blocks_enter_and_allows_escape_without_artifacts(self):
        other = self.make_repo("slow validation")
        entered, release, finished = self.slow_validation(other)
        before = self.artifacts()

        def pending():
            self.assertTrue(entered.is_set(), "repository edits must validate without pressing Enter")
            self.assert_pending()
            self.assert_not_drafted()

        def cancelled():
            self.assertIsNone(self.app.st.get("new_task"))
            self.assertIsNone(self.app.st["input"])
            self.assertFalse(release.is_set(), "Esc must be handled while validation is still blocked")

        try:
            self.scenario(*self.open_repo(other), Check(pending), "\n\r",
                          (tui.curses.KEY_ENTER,), Check(pending), "\x1b", Check(cancelled),
                          Action(release.set), Check(lambda: self.assertTrue(finished.is_set())),
                          Check(lambda: self.assertIsNone(self.app.st.get("new_task"))))
        finally:
            release.set()
        self.draft.assert_not_called()
        self.assertEqual(self.artifacts(), before)

    def test_late_validation_cannot_overwrite_a_newer_repository_edit(self):
        other = self.make_repo("slow validation")
        final = self.repo / "final path"
        final.mkdir()
        entered, release, finished = self.slow_validation(other)

        def pending():
            self.assertTrue(entered.is_set())
            self.assert_pending()

        try:
            self.scenario(*self.open_repo(other), Check(pending), "\x15", str(final),
                          Check(lambda: self.assert_fields("Import CSV", final)), Action(release.set),
                          Check(lambda: self.assertTrue(finished.is_set())),
                          Check(lambda: self.assert_valid(self.repo)), "\n",
                          Check(lambda: self.drafted(self.repo)), "\x1b")
        finally:
            release.set()

    def test_enter_rechecks_a_repository_that_loses_git_metadata_after_successful_validation(self):
        other = self.make_repo("removed before submit")
        before = self.artifacts()

        def no_draft_and_invalid():
            self.assert_not_drafted()
            self.assert_invalid()

        self.scenario(*self.open_repo(other), Check(lambda: self.assert_valid(other)),
                      Action(lambda: (other / ".git").rename(other / ".git-hidden")),
                      "\n", Check(no_draft_and_invalid), "\x1b")
        self.assertEqual(self.artifacts(), before)


class PasteBatchingTests(ComposerFixture):
    def reset_metrics(self):
        self.read_count = 0
        self.draw_count = 0
        self.draw_positions = []

    def assert_batched(self, length):
        # Allow any batching strategy that amortizes at least 32 queued keys
        # per frame, with room for status frames and debounce. No wall-clock
        # benchmark, private batching API or specific chunk size is required.
        self.assertLessEqual(self.draw_count, math.ceil(length / 32) + 16,
                             "queued paste must not trigger a full redraw for every character")

    def test_long_intent_paste_preserves_every_character_and_order_with_bounded_redraws(self):
        payload = ("nNjkg09 雪🙂 café e\u0301 [CSV] / " * 160) + "END"
        self.scr.getmaxyx.return_value = (16, 100)
        before = self.artifacts()

        def paste_complete():
            self.assert_batched(len(payload))
            positions = sorted(set([0, *self.draw_positions, len(payload)]))
            self.assertTrue(any(0 < position < len(payload) for position in positions),
                            "show progress before draining the entire long paste")
            self.assertLessEqual(max(b - a for a, b in zip(positions, positions[1:])), 2048,
                                 "a continuous queue must yield to rendering within 2048 keys")
            self.draft.assert_not_called()

        self.scenario("N", Check(self.input_open), Action(self.reset_metrics),
                      payload, Check(paste_complete), "\n",
                      Check(lambda: self.drafted(self.repo, payload)), "\x1b")
        self.assertEqual(self.artifacts(), before)

    def test_long_repository_paste_batches_git_validation_and_drafts_the_resolved_root(self):
        path = self.repo
        for index in range(16):
            path = path / ("segment %02d " % index + "x" * 44)
            path.mkdir()
        # Every character prefix in this tail is an existing directory. A
        # per-key validator cannot pass by skipping nonexistent partial names.
        payload = str(path) + "/." * 160
        self.scr.getmaxyx.return_value = (16, 100)
        before = self.artifacts()
        original = chief.call
        with mock.patch.object(chief, "call", wraps=original) as calls:

            def begin():
                calls.reset_mock()
                self.reset_metrics()

            def pasted():
                self.assert_batched(len(payload))
                self.assert_valid(self.repo)
                git_calls = [call for call in calls.call_args_list if call[0][0][0] == "git"]
                self.assertLessEqual(len(git_calls), 2 * math.ceil(len(payload) / 32) + 16,
                                     "do not spawn repository validation subprocesses per character")

            self.scenario("N", "Import CSV", (tui.curses.KEY_DOWN,), "\x15", Action(begin),
                          payload, Check(pasted), "\n", Check(lambda: self.drafted(self.repo)), "\x1b")
        self.assertEqual(self.artifacts(), before)

    def test_one_queued_burst_preserves_editing_navigation_and_submission_order(self):
        other = self.make_repo("pasted repository")
        entered, release = threading.Event(), threading.Event()

        def draft(*args):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("drafting blocked terminal input")
            return self.draft_contract(*args)

        self.draft.side_effect = draft
        self.addCleanup(release.set)
        # Establish validity first, then enqueue control keys and Enter together.
        # Repeated Enter while drafting must still be ignored across batch edges.
        burst = [*"discard", "\x15", *"Import CSVx", tui.curses.KEY_BACKSPACE,
                 tui.curses.KEY_DOWN, tui.curses.KEY_UP, *" receipts", "\n", "\r",
                 tui.curses.KEY_ENTER, "N"]

        def pending():
            self.assertTrue(entered.is_set())
            self.draft.assert_called_once_with(self.cfg, str(other), "Import CSV receipts")
            self.no_submission()

        try:
            self.scenario(*self.open_repo(other, ""), Check(lambda: self.assert_valid(other)),
                          (tui.curses.KEY_UP,), burst, Check(pending), Action(release.set),
                          Check(lambda: self.drafted(other, "Import CSV receipts")), "\x1b")
        finally:
            release.set()


class ComposerDocumentationTests(unittest.TestCase):
    def inbox_docs(self):
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
        section = re.split(r"\n#{2,3} ", readme.split("### 📥 The inbox", 1)[1], maxsplit=1)[0]
        return re.sub(r"\s+", " ", section).lower()

    def test_readme_explains_two_editable_lines_navigation_and_submission_gating(self):
        docs = self.inbox_docs()
        self.assertRegex(docs, r"(?:first|1st).*intent|intent.*(?:first|1st)")
        self.assertRegex(docs, r"(?:second|2nd).*repositor|repositor.*(?:second|2nd)")
        self.assertRegex(docs, r"up.*down|↑.*↓")
        self.assertRegex(docs, r"(?:valid|invalid).*repositor|repositor.*(?:valid|invalid)")
        self.assertRegex(docs, r"nonblank|non-blank|nonempty|non-empty|"
                         r"intent.*(?:not be|is not) (?:blank|empty)|intent.*contain.*text")

    def test_readme_explains_path_completion_empty_line_cycling_and_fast_paste(self):
        docs = self.inbox_docs()
        for phrase in ("absolute", "relative", "~/", "spaces", "common prefix", "paste"):
            self.assertIn(phrase, docs)
        self.assertRegex(docs, r"empty.*(?:tab|cycl)|(?:tab|cycl).*empty")

    def test_readme_documents_preview_trust_default_scope_and_manual_confirmation(self):
        docs = self.inbox_docs()
        for pattern in (r"`t`", r"preview", r"trust this folder", r"default.*off|off.*default",
                        r"per.task|each task|this task only", r"chief.*answer.*(?:agent|trust)",
                        r"confirm.*session", r"chief\.py kickoff <(?:id|task-id)>"):
            self.assertRegex(docs, pattern)


if __name__ == "__main__":
    unittest.main()
