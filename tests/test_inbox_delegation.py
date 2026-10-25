"""Acceptance tests for one-line Inbox delegation and the two work groups.

Drive the real Inbox event loop with a scripted screen, real temporary git
repositories and isolated Chief state. AI and external sessions are mocked.
No new Inbox methods or state keys are prescribed by this contract.
"""
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("AOE_CHIEF_HOME", tempfile.mkdtemp())
import chief  # noqa: E402
import chief_tui as tui  # noqa: E402


class EndOfScript(BaseException):
    """End a simulated terminal even while an input/preview is open."""


def row_text(rows):
    return ["".join(part for part, _ in row) for row in rows]


def item(sid, title, repo=None, task=None, level="grey"):
    return {"id": sid, "title": title, "path": str(repo) if repo else "",
            "tool": "codex", "status": "idle", "task": task or {}, "log": {},
            "signals": [] if level == "grey" else [(level, title + " signal", "checks")]}


class InboxFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="inbox-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.make_repo("source")
        self.cwd = Path.cwd()
        os.chdir(str(self.repo))
        self.addCleanup(os.chdir, str(self.cwd))
        home = self.root / "state"
        paths = {"CFG": "config.json", "LAST": "latest.json", "BRIEF": "latest.md",
                 "TICK": "tick.json", "CHECKS": "checks.json", "ARCHIVE": "reports",
                 "TASKS": "tasks", "PREFS": "preferences.md"}
        self.patch(chief, ROOT=home, LANG="en", **{k: home / v for k, v in paths.items()})
        self.cfg = chief.config()
        self.items = []
        self.collect = self.patch_object(chief, "collect", side_effect=lambda *a, **k: {
            "items": copy.deepcopy(self.items)})
        self.patch_object(chief, "aoe", side_effect=AssertionError("unexpected real AoE call"))
        self.contract = {"repo": str(self.repo), "name": "CSV receipts", "goal": "Accept quoted commas",
                         "checks": ["python3 -m unittest discover -s tests"],
                         "protected": ["tests", "config/policy.json"],
                         "budget": {"rounds": 7, "hours": 1.5}}
        self.draft = self.patch_object(chief, "intent_draft", side_effect=self.draft_contract)
        self.real_start = chief.start_task
        self.start = self.patch_object(chief, "start_task", side_effect=self.start_contract)
        self.app = tui.Inbox(self.cfg, chief, 3600)
        self.ui_thread = threading.get_ident()
        self.scr = mock.Mock()
        self.scr.getmaxyx.return_value = (50, 240)
        self.terminal_events = []
        self.patch_object(tui, "setup_colors", return_value={})
        self.patch_object(tui, "draw")
        self.patch_object(tui.curses, "curs_set")
        for name, label in (("def_prog_mode", "saved"), ("endwin", "ended"),
                            ("reset_prog_mode", "restored")):
            self.patch_object(tui.curses, name, side_effect=lambda label=label: self.terminal_events.append(label))
        self.patch_object("builtins", "input", side_effect=AssertionError("Inbox must use terminal input"))

    def patch(self, target, **kwargs):
        p = mock.patch.multiple(target, **kwargs)
        p.start()
        self.addCleanup(p.stop)

    def patch_object(self, target, name, **kwargs):
        p = mock.patch(target + "." + name, **kwargs) if isinstance(target, str) else mock.patch.object(target, name, **kwargs)
        result = p.start()
        self.addCleanup(p.stop)
        return result

    def git(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Inbox Tests",
                               "-c", "user.email=inbox-tests@example.invalid", *args],
                              check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True).stdout.strip()

    def make_repo(self, name):
        repo = self.root / name
        repo.mkdir()
        self.git(repo, "init", "-q")
        self.git(repo, "commit", "-q", "--allow-empty", "-m", "base")
        return repo

    def worktree(self):
        wt = self.root / "delegated-worktree"
        self.git(self.repo, "worktree", "add", "-q", "-b", "chief/existing", str(wt))
        return wt

    def draft_contract(self, cfg, repo, intent):
        return dict(copy.deepcopy(self.contract), repo=str(repo))

    def start_contract(self, cfg, task, trust=False):
        # Match the existing lifecycle: the contract is saved before startup.
        self.assertEqual(chief.load_task(task["id"]), task)
        task = dict(task, session_id="abcdef0123456789", worktree=str(self.repo), state="running")
        chief.save_task(task)
        return task

    def idle(self):
        deadline = time.monotonic() + 3
        while self.app.st.get("busy") or self.app.lock.locked():
            if time.monotonic() >= deadline:
                self.fail("Inbox background job did not finish within three seconds")
            time.sleep(0.002)

    def ui(self, *events):
        script = iter([self.idle, *events])

        def read_key():
            while True:
                event = next(script, EndOfScript)
                if event is EndOfScript:
                    raise EndOfScript()
                if callable(event):
                    event()
                else:
                    return event

        self.scr.get_wch.side_effect = read_key
        try:
            with self.assertRaises(EndOfScript):
                self.app.loop(self.scr)
        finally:
            self.idle()

    def frame(self, width=240, height=50):
        return row_text(tui.render(chief, self.app.st, width, height))

    def input_open(self):
        self.assertIsNotNone(self.app.st["input"], "uppercase N must open one-line task intent input")

    def preview(self, goal=None):
        self.assertIsNone(self.app.st["input"])
        # The result belongs in the detail pane, above the message and footer.
        detail = "\n".join(self.frame()[1:-2])
        for value in (goal or self.contract["goal"], self.contract["name"],
                      self.contract["checks"][0], "config/policy.json", "7", "1.5"):
            self.assertIn(value, detail)

    def compose(self, intent="Import CSV receipts"):
        return ["N", self.input_open, *intent, "\n", self.idle]

    def artifacts(self):
        return {"tasks": chief.all_tasks(), "config": chief.CFG.read_text(),
                "refs": self.git(self.repo, "show-ref"),
                "worktrees": self.git(self.repo, "worktree", "list", "--porcelain"),
                "files": self.git(self.repo, "status", "--porcelain", "--untracked-files=all")}

    def no_submission(self):
        self.assertEqual(chief.all_tasks(), [])
        self.start.assert_not_called()


class IntentEntryTests(InboxFixture):
    def test_N_is_discoverable_and_opens_input_even_when_empty_in_both_languages(self):
        for lang in ("en", "zh"):
            with self.subTest(lang=lang):
                chief.LANG = lang

                def check():
                    self.input_open()
                    self.assertIn(str(self.repo), "\n".join(self.frame()))
                    prompt = self.app.st["input"]["prompt"]
                    self.assertIn("Tab", self.frame()[-1])
                    self.assertIn("Esc", self.frame()[-1])
                    if lang == "zh":
                        self.assertRegex(prompt, r"[\u4e00-\u9fff]")
                    else:
                        self.assertRegex(prompt.lower(), r"intent|task|delegate")

                self.ui(lambda: self.assertRegex(self.frame()[-1], r"\bN\b"), "N", check, "\x1b")
                self.draft.assert_not_called()
                self.no_submission()

    def test_selected_delegated_worktree_defaults_to_its_source_repository(self):
        wt = self.worktree()
        task = {"id": "existing", "state": "running", "repo": str(self.repo), "worktree": str(wt)}
        chief.save_task(task)
        self.items = [item("1111111111111111", "delegated", wt, task)]
        elsewhere = self.make_repo("elsewhere")
        os.chdir(str(elsewhere))
        self.ui(*self.compose(), self.preview, "\x1b")
        self.draft.assert_called_once_with(self.cfg, str(self.repo), "Import CSV receipts")
        self.start.assert_not_called()
        self.assertEqual(chief.all_tasks(), [task])

    def test_selected_thinking_session_defaults_to_its_repository_root(self):
        other = self.make_repo("thinking")
        subdir = other / "src"
        subdir.mkdir()
        self.items = [item("2222222222222222", "think", subdir)]
        self.ui(*self.compose(), self.preview, "\x1b")
        self.draft.assert_called_once_with(self.cfg, str(other), "Import CSV receipts")
        self.no_submission()

    def test_unannotated_worktree_and_current_worktree_resolve_to_original_repository(self):
        wt = self.worktree()
        for selected in (True, False):
            with self.subTest(selected=selected):
                self.draft.reset_mock()
                self.items = [item("3333333333333333", "think in worktree", wt)] if selected else []
                os.chdir(str(wt if not selected else self.repo))
                self.ui(*self.compose(), "\x1b")
                self.draft.assert_called_once_with(self.cfg, str(self.repo), "Import CSV receipts")
                self.no_submission()

    def test_missing_selected_repository_falls_back_to_current_repository(self):
        self.items = [item("4444444444444444", "no project path")]
        subdir = self.repo / "nested"
        subdir.mkdir()
        os.chdir(str(subdir))
        self.ui(*self.compose(), "\x1b")
        self.draft.assert_called_once_with(self.cfg, str(self.repo), "Import CSV receipts")

    def test_Tab_cycles_known_task_and_session_repositories_once_in_stable_order(self):
        other, draft_repo = self.make_repo("thinking"), self.make_repo("unstarted")
        wt = self.worktree()
        alias = self.root / "alias"
        alias.symlink_to(other, target_is_directory=True)
        task = {"id": "existing", "state": "running", "repo": str(self.repo), "worktree": str(wt)}
        chief.save_task(task)
        chief.save_task({"id": "unstarted", "state": "draft", "repo": str(draft_repo)})
        self.items = [item("1111111111111111", "delegate", wt, task),
                      item("2222222222222222", "think", other),
                      item("3333333333333333", "alias duplicate", alias),
                      item("4444444444444444", "worktree duplicate", wt)]
        expected = {str(self.repo), str(other), str(draft_repo)}
        before = self.artifacts()

        def cycle():
            chosen, screens = [], []

            def remember():
                chosen.append(self.draft.call_args[0][1])
                self.assertIn(chosen[-1], screens[-1], "show the selected repository before submission")

            for tabs in range(4):
                self.ui("N", self.input_open, *(["\t"] * tabs), *"Import CSV",
                        lambda: screens.append("\n".join(self.frame())), "\n", self.idle, remember, "\x1b")
            return chosen

        first_cycle = cycle()
        self.assertEqual(set(first_cycle[:3]), expected)
        self.assertEqual(first_cycle[0], str(self.repo))
        self.assertEqual(first_cycle[0], first_cycle[3], "aliases and worktrees must not add duplicate choices")
        self.items.reverse()
        self.assertEqual(cycle(), first_cycle, "collection order must not change the repository cycle")
        self.start.assert_not_called()
        self.assertEqual(self.artifacts(), before)

    def test_Tab_choice_is_the_repository_passed_to_drafting(self):
        other = self.make_repo("other")
        self.items = [item("1111111111111111", "here", self.repo), item("2222222222222222", "other", other)]
        self.ui("N", self.input_open, *"Import CSV", "\t", "\n", self.idle, "\x1b")
        self.draft.assert_called_once_with(self.cfg, str(other), "Import CSV")

    def test_empty_whitespace_and_Esc_never_draft_or_create_artifacts(self):
        before = self.artifacts()
        for enter in ("\n", "\r", "KEY_ENTER"):
            with self.subTest(enter=enter):
                self.ui("N", self.input_open, " ", " ", enter, self.idle, "\x1b")
        self.ui("N", self.input_open, *"cancel this", "\x1b")
        self.draft.assert_not_called()
        self.no_submission()
        self.assertEqual(self.artifacts(), before)

    def test_one_line_editing_trims_intent_and_does_not_nudge(self):
        nudge = self.patch_object(chief, "nudge")
        self.ui("N", self.input_open, *"discard", "\x15", *"  导入 CSVx", "KEY_BACKSPACE", *" n  ",
                "\r", self.idle, self.preview, "\x1b")
        self.draft.assert_called_once_with(self.cfg, str(self.repo), "导入 CSV n")
        nudge.assert_not_called()
        self.no_submission()

    def test_lowercase_n_still_nudges_the_selected_task(self):
        task = {"id": "existing", "state": "running", "repo": str(self.repo)}
        chief.save_task(task)
        self.items = [item("1111111111111111", "delegate", self.repo, task)]
        nudge = self.patch_object(chief, "nudge", return_value=True)
        self.ui("n", *"keep going", "\n", self.idle)
        nudge.assert_called_once_with(self.cfg, task, "keep going")
        self.draft.assert_not_called()
        self.start.assert_not_called()


class PreviewLifecycleTests(InboxFixture):
    def test_draft_is_shown_in_detail_without_creating_anything(self):
        before = self.artifacts()
        self.ui(*self.compose(), self.preview)
        self.draft.assert_called_once_with(self.cfg, str(self.repo), "Import CSV receipts")
        self.no_submission()
        self.assertEqual(self.artifacts(), before)

    def test_Esc_from_preview_leaves_no_task_branch_worktree_or_session(self):
        before = self.artifacts()
        self.ui(*self.compose(), self.preview, "\x1b", "\n", "KEY_ENTER", self.idle,
                lambda: self.assertNotIn(self.contract["goal"], "\n".join(self.frame()[1:-2])))
        self.assertEqual(self.artifacts(), before)
        self.no_submission()

    def test_Enter_saves_and_starts_exactly_one_task_with_new_lifecycle_defaults(self):
        self.ui(*self.compose(), self.preview, "KEY_ENTER", "\n", "\r", self.idle, "\n", self.idle)
        self.start.assert_called_once()
        task = chief.all_tasks()[0]
        self.assertEqual(len(chief.all_tasks()), 1)
        for key in ("name", "goal", "repo", "checks", "protected", "budget"):
            self.assertEqual(task[key], self.contract[key])
        self.assertIs(task["test_first"], True)
        self.assertEqual(task["phase"], "test")
        self.assertEqual(task["autonomy"], "suggest")
        self.assertEqual(task["tool"], "codex")
        self.assertEqual(task["branch"], "chief/" + task["id"])
        self.assertTrue(task["created_at"])
        self.assertEqual(task["state"], "running")
        self.assertIn("Decision needed: approve the acceptance tests <files>", chief.kickoff_prompt(task))
        self.draft.assert_called_once()

    def test_confirmation_uses_real_branch_worktree_and_test_first_kickoff_lifecycle(self):
        # Only AoE's boundary is simulated here; start_task, add_command, git,
        # prepare_worktree, task persistence and send_kickoff run for real.
        self.start.side_effect = self.real_start
        original_call = chief.call
        sid, wt = "abcdef0123456789", self.root / "started-worktree"

        def call(args, **kwargs):
            if args[0] == "aoe-inbox-test":
                self.assertEqual(args[:5], ["aoe-inbox-test", "-p", "main", "add", str(self.repo)])
                branch = args[args.index("-w") + 1]
                self.git(self.repo, "worktree", "add", "-q", str(wt), branch)
                return "ID: " + sid + "\nPath: " + str(wt) + "\n"
            return original_call(args, **kwargs)

        with mock.patch.object(chief, "aoe_bin", return_value="aoe-inbox-test"), \
                mock.patch.object(chief, "call", side_effect=call), \
                mock.patch.object(chief, "aoe_records", return_value={sid: {"project_path": str(wt)}}), \
                mock.patch.object(chief, "wait_ready", return_value="ready") as ready, \
                mock.patch.object(chief, "deliver", return_value=True) as deliver:
            self.ui(*self.compose(), self.no_submission, "\n", self.idle, "\n", self.idle)
        self.start.assert_called_once()
        ready.assert_called_once_with(self.cfg, sid, False)
        deliver.assert_called_once()
        self.assertIn("Decision needed: approve the acceptance tests <files>", deliver.call_args[0][2])
        task = chief.all_tasks()[0]
        self.assertEqual(len(chief.all_tasks()), 1)
        self.assertEqual(task["session_id"], sid)
        self.assertEqual(task["worktree"], str(wt))
        self.assertEqual(task["state"], "running")
        self.assertEqual(task["phase"], "test")
        self.assertEqual(self.git(wt, "branch", "--show-current"), task["branch"])
        self.assertTrue((wt / ".chief" / "progress.md").is_file())

    def test_drafting_is_background_and_repeated_Enter_or_N_cannot_duplicate_it(self):
        entered, release = threading.Event(), threading.Event()
        worker = []
        self.addCleanup(release.set)

        def draft(*args):
            worker.append(threading.get_ident())
            entered.set()
            if not release.wait(2):
                raise RuntimeError("drafting blocked the terminal event loop")
            return self.draft_contract(*args)

        self.draft.side_effect = draft

        def responsive():
            self.assertTrue(entered.wait(1))
            self.assertNotEqual(worker, [self.ui_thread], "AI drafting must not run in the terminal thread")
            self.assertTrue(self.app.st["busy"])
            self.frame(40, 10)
            self.draft.assert_called_once()
            self.no_submission()
            release.set()

        self.ui("N", self.input_open, *"Import CSV", "\n", "\n", "KEY_ENTER", "N", responsive,
                self.idle, self.preview, "\x1b")
        self.no_submission()

    def test_cancel_while_drafting_discards_late_result_and_allows_another_intent(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def draft(*args):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("terminal could not cancel a pending draft")
            return self.draft_contract(*args)

        self.draft.side_effect = draft
        before = self.artifacts()

        def cancelled():
            self.assertTrue(entered.wait(1))
            release.set()

        self.ui("N", self.input_open, *"Old intent", "\n", "\x1b", cancelled, self.idle,
                lambda: self.assertNotIn(self.contract["goal"], "\n".join(self.frame()[1:-2])), "\n")
        self.assertEqual(self.artifacts(), before)
        self.draft.side_effect = self.draft_contract
        self.ui(*self.compose("New intent"), self.preview, "\x1b")
        self.assertEqual(self.draft.call_args[0][-1], "New intent")
        self.no_submission()

    def test_startup_is_background_and_repeated_confirmation_never_creates_a_second_task(self):
        entered, release = threading.Event(), threading.Event()
        worker = []
        self.addCleanup(release.set)

        def start(*args, **kwargs):
            worker.append(threading.get_ident())
            entered.set()
            if not release.wait(2):
                raise RuntimeError("startup blocked the terminal event loop")
            return self.start_contract(*args, **kwargs)

        self.start.side_effect = start

        def responsive():
            self.assertTrue(entered.wait(1))
            self.assertNotEqual(worker, [self.ui_thread])
            self.assertTrue(self.app.st["busy"])
            self.assertEqual(len(chief.all_tasks()), 1)
            self.start.assert_called_once()
            release.set()

        self.ui(*self.compose(), "\n", "\n", "N", "KEY_ENTER", responsive, self.idle, "\n", self.idle)
        self.assertEqual(len(chief.all_tasks()), 1)
        self.start.assert_called_once()
        self.draft.assert_called_once()

    def test_draft_failure_is_visible_and_the_inbox_can_retry(self):
        self.draft.side_effect = RuntimeError("draft engine offline")
        self.ui(*self.compose(), lambda: self.assertIn("draft engine offline", "\n".join(self.frame())))
        self.no_submission()
        self.draft.side_effect = self.draft_contract
        self.ui(*self.compose("Retry import"), self.preview, "\x1b")
        self.assertEqual(self.draft.call_count, 2)
        self.no_submission()

    def test_start_failure_is_visible_and_repeated_Enter_does_not_relaunch(self):
        self.start.side_effect = RuntimeError("session launcher unavailable")
        self.ui(*self.compose(), "\n", self.idle,
                lambda: self.assertIn("session launcher unavailable", "\n".join(self.frame())),
                "\n", "KEY_ENTER", self.idle, "\x1b", "g", self.idle)
        self.start.assert_called_once()
        self.assertLessEqual(len(chief.all_tasks()), 1)

    def test_refresh_during_preview_keeps_the_contract_until_confirm_or_cancel(self):
        self.ui(*self.compose(), self.preview, self.app._refresh, self.preview, "\x1b")
        self.no_submission()

    def test_preview_exposes_Enter_e_and_Esc_in_both_languages(self):
        for lang in ("en", "zh"):
            with self.subTest(lang=lang):
                chief.LANG = lang

                def check():
                    footer = self.frame()[-1]
                    for key in (r"\bEnter\b", r"\be\b", r"\bEsc\b"):
                        self.assertRegex(footer, key)
                    if lang == "zh":
                        self.assertRegex(footer, r"[\u4e00-\u9fff]")

                self.ui(*self.compose(), self.preview, check, "\x1b")
                self.no_submission()


class ContractEditorTests(InboxFixture):
    def editor_ready(self, cmd):
        self.assertEqual(threading.get_ident(), self.ui_thread, "curses suspension belongs to the UI thread")
        self.assertEqual(self.terminal_events[-2:], ["saved", "ended"])
        self.assertEqual(cmd[:-1], ["/fake tools/contract-editor", "--wait"])
        path = Path(cmd[-1])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["goal"], self.contract["goal"])
        self.no_submission()
        return path

    def restored(self):
        self.assertEqual(self.terminal_events, ["saved", "ended", "restored"])
        self.scr.clear.assert_called()

    def test_e_edits_validates_restores_terminal_and_submits_the_edited_contract(self):
        def edit(cmd):
            path = self.editor_ready(cmd)
            edited = json.loads(path.read_text(encoding="utf-8"))
            edited.update(goal="Import quoted CSV fields", checks=["make test"], scope="src,tests")
            path.write_text(json.dumps(edited), encoding="utf-8")
            return 0

        editor = self.patch_object(tui.subprocess, "call", side_effect=edit)
        with mock.patch.dict(os.environ, {"EDITOR": '"/fake tools/contract-editor" --wait'}):
            self.ui(*self.compose(), self.preview, "e", self.restored,
                    lambda: self.assertIn("Import quoted CSV fields", "\n".join(self.frame()[1:-2])),
                    lambda: self.assertIn("make test", "\n".join(self.frame()[1:-2])),
                    lambda: self.assertIn("src,tests", "\n".join(self.frame()[1:-2])),
                    self.no_submission, "\n", self.idle)
        editor.assert_called_once()
        self.start.assert_called_once()
        task = chief.all_tasks()[0]
        self.assertEqual(task["goal"], "Import quoted CSV fields")
        self.assertEqual(task["checks"], ["make test"])
        self.assertEqual(task["scope"], "src,tests")
        self.assertEqual(task["phase"], "test")
        self.assertIs(task["test_first"], True)

    def test_editor_errors_and_invalid_contracts_keep_the_last_valid_preview(self):
        before = self.artifacts()
        cases = ("exit", "missing-editor", "bad-json", "not-object", "blank-goal", "bad-checks",
                 "bad-protected", "bad-budget", "bad-test-first")
        for case in cases:
            with self.subTest(case=case):
                self.terminal_events.clear()

                def edit(cmd):
                    path = self.editor_ready(cmd)
                    if case == "exit":
                        return 2
                    if case == "missing-editor":
                        raise OSError("editor is unavailable")
                    content = copy.deepcopy(self.contract)
                    if case == "bad-json":
                        path.write_text("{broken", encoding="utf-8")
                        return 0
                    if case == "not-object":
                        content = []
                    elif case == "blank-goal":
                        content["goal"] = " "
                    elif case == "bad-checks":
                        content["checks"] = "not a list"
                    elif case == "bad-protected":
                        content["protected"] = "tests"
                    elif case == "bad-budget":
                        content["budget"]["rounds"] = -1
                    elif case == "bad-test-first":
                        content["test_first"] = "true"
                    path.write_text(json.dumps(content), encoding="utf-8")
                    return 0

                with mock.patch.dict(os.environ, {"EDITOR": '"/fake tools/contract-editor" --wait'}), \
                        mock.patch.object(tui.subprocess, "call", side_effect=edit) as editor:
                    self.ui(*self.compose(), "e", self.restored, self.preview,
                            lambda: self.assertRegex(self.frame()[-2].lower(), r"fail|invalid|could not|error"), "\x1b")
                editor.assert_called_once()
                self.no_submission()
                self.assertEqual(self.artifacts(), before)


class GroupAndRenderingTests(InboxFixture):
    def grouped_items(self):
        def delegated(sid, title, level):
            return item(sid, title, self.repo, {"id": title, "state": "running"}, level)
        # Interleave both groups and urgency levels, including a legacy tracked
        # session with guidance but no delegated task ID.
        return [item("think-red", "think red", self.repo, level="red"),
                delegated("task-green", "task green", "green"),
                item("think-grey", "think grey", self.repo, {"name": "legacy", "goal": "Explore"}),
                delegated("task-red", "task red", "red"),
                delegated("task-draft", "task draft", "draft")]

    def test_grouped_entries_keep_urgency_order_within_each_group(self):
        es = tui.entries(chief, {"items": self.grouped_items()})
        self.assertEqual([e["sid"] for e in es],
                         ["task-red", "task-draft", "task-green", "think-red", "think-grey"])
        self.assertEqual(len(es), 5, "headings must not become selectable entries")

    def test_all_urgency_levels_sort_independently_and_keep_ties_stable(self):
        raw = []
        for group in ("think", "task"):
            for level in reversed(tui.ORDER):
                task = {"id": level, "state": "running"} if group == "task" else None
                entry = item(group + "-" + level, level, task=task, level=level)
                entry["changed"] = level == "yellow"
                raw.append(entry)
        raw.append(item("task-red-second", "second red", task={"id": "second", "state": "running"}, level="red"))
        expected = ["task-red", "task-red-second", "task-draft", "task-green", "task-yellow", "task-grey",
                    "think-red", "think-draft", "think-green", "think-yellow", "think-grey"]
        self.assertEqual([e["sid"] for e in tui.entries(chief, {"items": raw})], expected)

    def test_both_groups_are_labeled_localized_and_not_counted_as_items(self):
        self.items = self.grouped_items()
        for lang, labels in (("en", (r"Delegated tasks", r"Thinking sessions")),
                             ("zh", (r"委派任务|委托任务", r"思考会话|思考中的会话"))):
            with self.subTest(lang=lang):
                chief.LANG = lang

                def check():
                    rows = self.frame()
                    positions = [next((i for i, row in enumerate(rows) if re.search(label, row)), -1) for label in labels]
                    self.assertTrue(all(i > 0 for i in positions), rows)
                    self.assertLess(positions[0], positions[1])
                    self.assertRegex(rows[0], r"5 tracked" if lang == "en" else r"跟踪 5")
                    styled = tui.render(chief, self.app.st, 240, 50)
                    for pos in positions:
                        self.assertFalse(any(style == "sel" for _, style in styled[pos]), "headings cannot be selected")

                self.ui(check)

    def test_navigation_crosses_group_boundary_skips_headings_and_clamps_at_ends(self):
        self.items = self.grouped_items()
        visited = []
        remember = lambda: visited.append(self.app.selected()["sid"])
        self.ui(remember, "k", remember, "j", remember, "KEY_DOWN", remember,
                "j", remember, "KEY_DOWN", remember, "j", remember, "KEY_UP", remember)
        self.assertEqual(visited, ["task-red", "task-red", "task-draft", "task-green",
                                   "think-red", "think-grey", "think-grey", "think-red"])

    def test_open_and_reply_still_target_the_item_selected_across_groups(self):
        self.items = self.grouped_items()
        opened = self.patch_object(self.app, "open_session")
        reply = self.patch_object(chief, "reply_session", return_value=True)
        self.ui("o", "j", "j", "j", "o", "r", *"keep thinking", "\n", self.idle)
        self.assertEqual([call[0][1]["sid"] for call in opened.call_args_list], ["task-red", "think-red"])
        reply.assert_called_once_with(self.cfg, "think-red", "keep thinking")
        self.no_submission()

    def test_refresh_keeps_selected_session_when_urgency_or_membership_changes(self):
        self.items = self.grouped_items()

        def reorder():
            self.items[0]["signals"] = []
            self.items[2]["signals"] = [("red", "needs thought", "checks")]
            self.items.reverse()

        self.ui("j", "j", "j", lambda: self.assertEqual(self.app.selected()["sid"], "think-red"),
                reorder, "g", self.idle,
                lambda: self.assertEqual(self.app.selected()["sid"], "think-red"))
        self.items = [x for x in self.items if x["id"] == "task-red"]
        self.ui(lambda: self.assertEqual(self.app.selected()["sid"], "task-red"))
        self.items = []
        self.ui("j", "k", "N", self.input_open, "\x1b")
        self.assertIsNone(self.app.selected())

    def test_input_preview_and_groups_fit_narrow_terminals_in_both_languages(self):
        self.items = self.grouped_items()

        def fits():
            for width, height in ((8, 4), (20, 8), (40, 10), (60, 16), (100, 30)):
                rows = self.frame(width, height)
                self.assertEqual(len(rows), height)
                for row in rows:
                    self.assertEqual(tui.width(row), width, (width, height, row))

        for lang in ("en", "zh"):
            with self.subTest(lang=lang):
                chief.LANG = lang
                self.ui(fits, "N", self.input_open, *"修复导入时的中文字段和引号" * 4, fits,
                        "\n", self.idle, fits, "KEY_NPAGE", fits, "KEY_PPAGE", "\x1b", fits)


class InboxDocumentationTests(unittest.TestCase):
    def test_README_documents_delegation_keys_and_both_work_groups(self):
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
        inbox = readme.split("### 📥 The inbox", 1)[1].split("\n### ", 1)[0]
        for key in ("N", "Tab", "Enter", "e", "Esc", "n"):
            self.assertIn("`" + key + "`", inbox)
        self.assertIn("$EDITOR", inbox)
        self.assertRegex(inbox.lower(), r"delegated tasks")
        self.assertRegex(inbox.lower(), r"thinking sessions")


if __name__ == "__main__":
    unittest.main()
