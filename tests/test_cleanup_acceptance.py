"""Acceptance contract for safe cleanup and archived task closure.

Run against real, disposable Git repositories and real Chief task storage. Only
the AoE boundary is simulated: no live sessions or user worktrees are touched.
"""
import copy
import json
from pathlib import Path
from documentation_support import read_documentation
import shutil
import subprocess
import sys
from unittest import mock

import chief
import chief_tui as tui
from test_intent_task_flow import ChiefFixture


class CleanupFixture(ChiefFixture):
    def setUp(self):
        super().setUp()
        self.git("branch", "-M", "main")
        self.write("tracked.txt", "base\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "cleanup baseline")
        self.cfg["profile"] = "cleanup-qa"
        chief.save_cfg(self.cfg)
        self.sessions = {}
        self.operations = []
        self.git_deletions = []
        self.fail_remove = set()
        self.fail_archive = set()
        self.progress_at_remove = {}
        self.aoe_home = self.root / "aoe"
        self.patch(chief, "AOE_HOME", new=self.aoe_home)
        self.patch(chief, "aoe", side_effect=self.fake_aoe)
        real_call = chief.call

        def record_git(args, *a, **kw):
            if args[0] == "git" and "branch" in args and any(
                    flag in args for flag in ("-d", "-D", "--delete")):
                self.git_deletions.append((list(args), list(self.operations)))
            return real_call(args, *a, **kw)

        self.patch(chief, "call", side_effect=record_git)

    def patch(self, obj, name, **kwargs):
        patcher = mock.patch.object(obj, name, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def write_records(self):
        path = self.aoe_home / "profiles" / self.cfg["profile"] / "sessions.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"sessions": list(self.sessions.values())}))

    def task(self, tid="cleanup-demo", state="done", archived=False, **changes):
        branch = "chief/" + tid
        wt = self.root / ("worktree " + tid)
        self.git("worktree", "add", "-q", "-b", branch, str(wt), "main")
        progress = wt / ".chief" / "progress.md"
        progress.parent.mkdir()
        progress.write_text("## Status\nCompleted café cleanup.\n", encoding="utf-8")
        task = dict(id=tid, name="Cleanup " + tid, repo=str(self.repo), worktree=str(wt),
                    branch=branch, base_branch="main", state=state, session_id="session-" + tid,
                    goal="safe cleanup", checks=[], closed_at="2026-10-20T12:00:00+00:00")
        task.update(changes)
        chief.save_task(task)
        self.sessions[task["session_id"]] = dict(
            id=task["session_id"], title=task["name"], project_path=str(wt),
            state="archived" if archived else "live", archived=archived,
            status="stopped" if archived else "idle", tool="codex", view="terminal")
        self.write_records()
        return task

    def fake_aoe(self, cfg, *args):
        self.assertEqual(cfg["profile"], "cleanup-qa", "every AoE operation must use the configured profile")
        self.assertNotIn("--all", args, "cleanup must not enumerate other profiles")
        self.operations.append(tuple(args))
        if args[0] == "list":
            rows = list(self.sessions.values())
            if "--state=live" in args or ("--state" in args and args[args.index("--state") + 1] == "live"):
                rows = [r for r in rows if r["state"] == "live"]
            return json.dumps(rows)
        if args[:2] == ("session", "show"):
            return json.dumps(self.sessions[args[-1]])
        if args[:2] == ("session", "color"):
            return ""
        if args[:2] == ("session", "archive"):
            sid = args[2]
            self.assertNotIn("--no-kill", args, "archive must stop the process")
            if sid in self.fail_archive:
                raise RuntimeError("archive failed: AoE unavailable")
            self.sessions[sid].update(state="archived", archived=True, status="stopped")
            self.write_records()
            return ""
        if args[0] == "remove":
            sid = args[1]
            self.assertIn("--delete-worktree", args)
            self.assertNotIn("--purge", args, "removal must remain recoverable through AoE trash")
            self.assertNotIn("--delete-branch", args, "Chief must apply its own safe branch policy")
            task = next(t for t in chief.all_tasks() if t.get("session_id") == sid)
            saved = chief.TASKS / task["id"] / "progress.md"
            self.progress_at_remove[sid] = saved.read_bytes() if saved.exists() else None
            if sid in self.fail_remove:
                raise RuntimeError("remove failed: simulated trash failure")
            wt = Path(task["worktree"])
            if wt.exists():
                trash = self.root / "trash" / sid
                trash.parent.mkdir(exist_ok=True)
                shutil.move(str(wt), str(trash))
                self.git("worktree", "prune")
            self.sessions[sid].update(state="trashed", trashed_at="2026-10-26T12:00:00Z")
            self.write_records()
            return ""
        raise AssertionError("unexpected AoE operation: " + repr(args))

    def mutations(self):
        return [a for a in self.operations if a[0] == "remove" or a[:2] == ("session", "archive")]

    def branch_exists(self, branch):
        result = subprocess.run(["git", "-C", str(self.repo), "show-ref", "--verify", "refs/heads/" + branch],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return result.returncode == 0

    def stored(self, task):
        return chief.load_task(task["id"])

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*")
                if p.is_file() and ".git" not in p.parts}

    def assert_removed(self, task):
        self.assertTrue(self.stored(task).get("session_removed"), "record cleanup only after AoE succeeds")
        self.assertFalse(Path(task["worktree"]).exists())
        self.assertEqual(self.sessions[task["session_id"]]["state"], "trashed")

    def clean(self, *flags, answer="y"):
        with mock.patch("builtins.input", return_value=answer) as ask:
            code, out, err = self.cli("clean", *flags)
        return code, out + err, ask


class CleanupConfirmationTests(CleanupFixture):
    def test_candidates_are_fully_listed_before_default_no_confirmation(self):
        task = self.task("long-task-id-that-must-never-be-truncated-at-24-characters")
        before = self.snapshot()

        def decline(prompt=""):
            visible = sys.stdout.getvalue()
            for value in (task["id"], task["name"], task["branch"], "merged", "clean"):
                self.assertIn(value, visible)
            self.assertIn("y/N", prompt)
            self.assertEqual(self.mutations(), [])
            return ""

        with mock.patch("builtins.input", side_effect=decline) as ask:
            code, _, err = self.cli("clean")
        self.assertEqual(code, 0, err)
        ask.assert_called_once()
        self.assertEqual(self.snapshot(), before, "declining must not even archive progress")
        self.assertEqual(self.mutations(), [])

    def test_no_eof_and_interrupt_leave_everything_untouched(self):
        self.task()
        before = self.snapshot()
        for response in ("n", EOFError(), KeyboardInterrupt()):
            with self.subTest(response=repr(response)):
                kwargs = {"side_effect": response} if isinstance(response, BaseException) else {"return_value": response}
                with mock.patch("builtins.input", **kwargs):
                    try:
                        code, _, _ = self.cli("clean")
                    except KeyboardInterrupt:
                        code = 130
                self.assertIn(code, (0, 130))
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.mutations(), [])

    def test_no_candidates_never_prompts_or_changes_state(self):
        self.task("active", state="running")
        before = self.snapshot()
        code, _, ask = self.clean()
        self.assertEqual(code, 0)
        ask.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.mutations(), [])

    def test_dry_run_lists_skips_and_proposed_branch_actions_without_mutation(self):
        merged = self.task("merged")
        dirty = self.task("dirty")
        unmerged = self.task("unmerged")
        self.git("-C", unmerged["worktree"], "commit", "-q", "--allow-empty", "-m", "not merged")
        unknown = self.task("unknown", base_branch="missing-base")
        (Path(dirty["worktree"]) / "untracked.txt").write_text("do not discard")
        before = self.snapshot()
        code, out, ask = self.clean("--dry-run", "--branches")
        self.assertEqual(code, 0, out)
        ask.assert_not_called()
        for value in (merged["id"], merged["branch"], dirty["id"], unmerged["branch"],
                      unknown["branch"], "dirty", "skip"):
            self.assertIn(value, out.lower())
        self.assertRegex(out.lower(), r"delet|would remove.*branch")
        self.assertRegex(out.lower(), r"keep|kept|retain")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.git_deletions, [])

    def test_dry_run_without_branches_proposes_retaining_branches(self):
        task = self.task()
        before = self.snapshot()
        code, out, ask = self.clean("--dry-run")
        self.assertEqual(code, 0, out)
        ask.assert_not_called()
        self.assertIn(task["branch"], out)
        self.assertRegex(out.lower(), r"keep|kept|retain")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.mutations(), [])

    def test_discovers_archived_sessions_and_only_closed_tasks_in_configured_profile(self):
        done = self.task("done", archived=True)
        abandoned = self.task("abandoned", state="abandoned", archived=True)
        for state in ("draft", "running", "needs-you", "review"):
            self.task(state, state=state)
        gone = self.task("missing")
        del self.sessions[gone["session_id"]]
        trashed = self.task("already-trashed")
        self.sessions[trashed["session_id"]].update(state="trashed", trashed_at="2026-10-25T12:00:00Z")
        foreign = self.task("other-profile")
        record = self.sessions.pop(foreign["session_id"])
        other = self.aoe_home / "profiles" / "other" / "sessions.json"
        other.parent.mkdir(parents=True)
        other.write_text(json.dumps([record]))
        self.write_records()
        code, out, _ = self.clean()
        self.assertEqual(code, 0, out)
        for task in (done, abandoned):
            self.assert_removed(task)
            self.assertEqual(self.stored(task)["state"], task["state"])
        self.assertCountEqual([a[1] for a in self.mutations()], [done["session_id"], abandoned["session_id"]])
        for tid in ("draft", "running", "needs-you", "review", "missing", "already-trashed", "other-profile"):
            self.assertFalse(chief.load_task(tid).get("session_removed"))


class CleanupPreservationTests(CleanupFixture):
    def test_progress_is_preserved_before_trash_and_branches_stay_by_default(self):
        task = self.task()
        expected = (Path(task["worktree"]) / ".chief" / "progress.md").read_bytes()
        code, out, _ = self.clean()
        self.assertEqual(code, 0, out)
        self.assert_removed(task)
        self.assertEqual(self.progress_at_remove[task["session_id"]], expected)
        self.assertEqual((chief.TASKS / task["id"] / "progress.md").read_bytes(), expected)
        self.assertTrue(self.branch_exists(task["branch"]))
        self.assertEqual(self.git_deletions, [])
        self.assertEqual(self.stored(task)["closed_at"], task["closed_at"])

    def test_missing_progress_is_allowed(self):
        task = self.task()
        (Path(task["worktree"]) / ".chief" / "progress.md").unlink()
        code, out, _ = self.clean()
        self.assertEqual(code, 0, out)
        self.assert_removed(task)
        self.assertIsNone(self.progress_at_remove[task["session_id"]])

    def test_current_progress_replaces_an_older_archive_before_removal(self):
        task = self.task()
        saved = chief.TASKS / task["id"] / "progress.md"
        saved.write_text("obsolete progress from an earlier turn\n")
        expected = (Path(task["worktree"]) / ".chief" / "progress.md").read_bytes()
        code, out, _ = self.clean()
        self.assertEqual(code, 0, out)
        self.assert_removed(task)
        self.assertEqual(self.progress_at_remove[task["session_id"]], expected)
        self.assertEqual(saved.read_bytes(), expected)

    def test_missing_worktree_progress_preserves_an_existing_archive(self):
        task = self.task()
        (Path(task["worktree"]) / ".chief" / "progress.md").unlink()
        saved = chief.TASKS / task["id"] / "progress.md"
        expected = b"progress already preserved by an earlier operation\n"
        saved.write_bytes(expected)
        code, out, _ = self.clean()
        self.assertEqual(code, 0, out)
        self.assert_removed(task)
        self.assertEqual(self.progress_at_remove[task["session_id"]], expected)
        self.assertEqual(saved.read_bytes(), expected)

    def test_progress_copy_failure_blocks_only_that_task(self):
        failed = self.task("a-copy-fails")
        good = self.task("b-continues")
        # A directory at the destination causes a real, deterministic copy/write
        # failure even when the test process has elevated filesystem permissions.
        (chief.TASKS / failed["id"] / "progress.md").mkdir()
        code, out, _ = self.clean("--branches")
        self.assertIn(failed["id"], out)
        self.assertRegex(out.lower(), r"progress|copy|preserv")
        self.assertTrue(Path(failed["worktree"]).exists())
        self.assertEqual(self.stored(failed), failed)
        self.assertTrue(self.branch_exists(failed["branch"]))
        self.assertNotIn(failed["session_id"], self.progress_at_remove)
        self.assertFalse(any(a[0] == "remove" and a[1] == failed["session_id"] for a in self.operations),
                         "a failed progress copy must block even an attempted AoE removal")
        self.assert_removed(good)

    def test_aoe_failure_is_reported_and_does_not_mark_removal_or_delete_branch(self):
        failed = self.task("a-remove-fails")
        good = self.task("b-continues")
        self.fail_remove.add(failed["session_id"])
        _, out, _ = self.clean("--branches")
        self.assertIn(failed["id"], out)
        self.assertIn("simulated trash failure", out)
        self.assertEqual(self.stored(failed), failed)
        self.assertTrue(self.branch_exists(failed["branch"]))
        self.assert_removed(good)

    def test_tracked_staged_and_untracked_changes_are_all_skipped(self):
        for kind in ("tracked", "staged", "untracked", "nested-untracked"):
            task = self.task(kind)
            rel = "tracked.txt" if kind in ("tracked", "staged") else (
                "new file.txt" if kind == "untracked" else "new dir/new file.txt")
            path = Path(task["worktree"]) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("valuable uncommitted work\n")
            if kind == "staged":
                self.git("-C", task["worktree"], "add", "tracked.txt")
        good = self.task("clean-continues")
        _, out, _ = self.clean("--branches")
        for kind in ("tracked", "staged", "untracked", "nested-untracked"):
            self.assertIn(kind, out)
            self.assertFalse(chief.load_task(kind).get("session_removed"))
            self.assertTrue((self.root / ("worktree " + kind)).exists())
        self.assert_removed(good)

    def test_scope_ignore_and_builtin_exclusions_apply_to_cleanup(self):
        task = self.task()
        self.cfg["scope_ignore"] = ["generated-cache"]
        chief.save_cfg(self.cfg)
        for rel in ("__pycache__/cache.pyc", "scratch/__pycache__/nested.pyc", ".chief/log.txt",
                    "generated-cache/result.txt"):
            path = Path(task["worktree"]) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("ignored output\n")
        code, out, _ = self.clean()
        self.assertEqual(code, 0, out)
        self.assert_removed(task)

    def test_unknown_cleanliness_is_skipped_and_other_candidates_continue(self):
        unknown = self.task("a-not-a-worktree")
        # The directory exists and contains progress, but Git cannot verify it.
        (Path(unknown["worktree"]) / ".git").unlink()
        good = self.task("b-clean")
        _, out, _ = self.clean()
        self.assertIn(unknown["id"], out)
        self.assertRegex(out.lower(), r"unknown|cannot verify|unverif")
        self.assertEqual(self.stored(unknown), unknown)
        self.assertTrue(Path(unknown["worktree"]).exists())
        self.assert_removed(good)

    def test_failed_cleanliness_verification_is_unknown_even_when_head_is_readable(self):
        unknown = self.task("a-verification-fails")
        good = self.task("b-continues")
        self.assertIsNotNone(chief.git_head(unknown["worktree"]))
        real_call = chief.call

        def fail_verification(args, *a, **kw):
            if args[0] == "git" and unknown["worktree"] in args and any(
                    command in args for command in ("status", "diff", "ls-files", "diff-index", "diff-files")):
                raise RuntimeError("worktree cleanliness check denied")
            return real_call(args, *a, **kw)

        with mock.patch.object(chief, "call", side_effect=fail_verification):
            _, out, _ = self.clean("--branches")
        self.assertIn(unknown["id"], out)
        self.assertRegex(out.lower(), r"unknown|cannot verify|unverif|check denied")
        self.assertEqual(self.stored(unknown), unknown)
        self.assertTrue(Path(unknown["worktree"]).exists())
        self.assertTrue(self.branch_exists(unknown["branch"]))
        self.assertFalse(any(a[0] == "remove" and a[1] == unknown["session_id"] for a in self.operations))
        self.assert_removed(good)

    def test_cleanliness_is_rechecked_after_confirmation(self):
        task = self.task()

        def race(prompt=""):
            (Path(task["worktree"]) / "late.txt").write_text("appeared during confirmation")
            return "y"

        with mock.patch("builtins.input", side_effect=race):
            _, out, err = self.cli("clean", "--branches")
        self.assertRegex((out + err).lower(), r"dirty|uncommitted|changed")
        self.assertEqual(self.stored(task), task)
        self.assertEqual(self.mutations(), [])
        self.assertTrue(self.branch_exists(task["branch"]))

    def test_cleanliness_becoming_unverifiable_after_confirmation_is_skipped(self):
        task = self.task()

        def race(prompt=""):
            (Path(task["worktree"]) / ".git").unlink()
            return "y"

        with mock.patch("builtins.input", side_effect=race):
            _, out, err = self.cli("clean")
        self.assertRegex((out + err).lower(), r"unknown|cannot verify|unverif")
        self.assertEqual(self.stored(task), task)
        self.assertEqual(self.mutations(), [])


class CleanupBranchTests(CleanupFixture):
    def test_branches_flag_deletes_only_after_success_using_nonforced_git(self):
        task = self.task()
        code, out, _ = self.clean("--branches")
        self.assertEqual(code, 0, out)
        self.assert_removed(task)
        self.assertFalse(self.branch_exists(task["branch"]))
        self.assertEqual(len(self.git_deletions), 1)
        args, operations = self.git_deletions[0]
        self.assertTrue("-d" in args or "--delete" in args)
        self.assertFalse(any(a in args for a in ("-D", "-f", "--force")))
        self.assertTrue(any(a[0] == "remove" and a[1] == task["session_id"] for a in operations))

    def test_unmerged_and_unknown_branches_are_retained_after_cleanup(self):
        unmerged = self.task("unmerged")
        self.git("-C", unmerged["worktree"], "commit", "-q", "--allow-empty", "-m", "not in main")
        unknown = self.task("unknown", base_branch="does-not-exist")
        _, out, _ = self.clean("--branches")
        for task in (unmerged, unknown):
            self.assert_removed(task)
            self.assertTrue(self.branch_exists(task["branch"]))
        self.assertIn("unmerged", out.lower())
        self.assertIn("unknown", out.lower())
        self.assertEqual(self.git_deletions, [])

    def test_merge_target_is_base_branch_even_when_repository_head_disagrees(self):
        task = self.task()
        self.git("-C", task["worktree"], "commit", "-q", "--allow-empty", "-m", "task work")
        self.git("branch", "integration", task["branch"])
        task["base_branch"] = "integration"
        chief.save_task(task)
        code, out, _ = self.clean("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("merged", out.lower())
        self.assertNotIn("unmerged", out.lower())

    def test_repository_current_head_is_fallback_when_base_branch_is_absent(self):
        task = self.task()
        del task["base_branch"]
        chief.save_task(task)
        # The repository is now on integration, while main lacks the task commit.
        self.git("-C", task["worktree"], "commit", "-q", "--allow-empty", "-m", "task work")
        self.git("checkout", "-q", "-b", "integration", task["branch"])
        code, out, _ = self.clean("--dry-run", "--branches")
        self.assertEqual(code, 0, out)
        self.assertIn("merged", out.lower())
        self.assertNotIn("unmerged", out.lower())

    def test_explicit_base_prevents_branch_deletion_even_when_repository_head_contains_task(self):
        task = self.task()
        self.git("-C", task["worktree"], "commit", "-q", "--allow-empty", "-m", "task work")
        self.git("checkout", "-q", "-b", "integration", task["branch"])
        # Git -d would allow this branch because integration contains it, but
        # the task's actual base_branch (main) has not received the work.
        code, out, _ = self.clean("--branches")
        self.assertEqual(code, 0, out)
        self.assert_removed(task)
        self.assertTrue(self.branch_exists(task["branch"]))
        self.assertEqual(self.git_deletions, [])

    def test_head_fallback_uses_repository_head_instead_of_task_worktree_head(self):
        task = self.task()
        del task["base_branch"]
        chief.save_task(task)
        self.git("-C", task["worktree"], "commit", "-q", "--allow-empty", "-m", "not in repository HEAD")
        code, out, _ = self.clean("--branches")
        self.assertEqual(code, 0, out)
        self.assert_removed(task)
        self.assertTrue(self.branch_exists(task["branch"]))
        self.assertEqual(self.git_deletions, [])

    def test_checked_out_branch_is_retained_even_after_session_worktree_is_removed(self):
        task = self.task()
        self.git("-C", task["worktree"], "checkout", "-q", "--detach")
        other = self.root / "other checkout"
        self.git("worktree", "add", "-q", str(other), task["branch"])
        _, out, _ = self.clean("--branches")
        self.assert_removed(task)
        self.assertTrue(self.branch_exists(task["branch"]))
        self.assertTrue(other.exists())
        self.assertRegex(out.lower(), r"checked.out|in use|keep|kept|retain")

    def test_branch_deletion_failure_leaves_cleanup_successful_and_continues(self):
        first = self.task("a-delete-fails")
        second = self.task("b-continues")
        real_call = chief.call

        def fail_delete(args, *a, **kw):
            if args[0] == "git" and "branch" in args and first["branch"] in args and any(
                    flag in args for flag in ("-d", "--delete")):
                raise RuntimeError("branch deletion denied")
            return real_call(args, *a, **kw)

        with mock.patch.object(chief, "call", side_effect=fail_delete):
            _, out, _ = self.clean("--branches")
        self.assertIn("branch deletion denied", out)
        for task in (first, second):
            self.assert_removed(task)
        self.assertTrue(self.branch_exists(first["branch"]))
        self.assertFalse(self.branch_exists(second["branch"]), "a branch failure must not stop later branch cleanup")


class CloseArchiveTests(CleanupFixture):
    def test_default_close_archives_and_stops_session_preserving_all_files_and_branch(self):
        for abandon in (False, True):
            task = self.task("abandon" if abandon else "done", state="review")
            before = (Path(task["worktree"]) / ".chief" / "progress.md").read_bytes()
            # Default archival is also safe for dirty worktrees.
            (Path(task["worktree"]) / "dirty.txt").write_text("preserve me")
            flags = ("--abandon",) if abandon else ()
            code, out, err = self.cli("close", task["id"], *flags)
            self.assertEqual(code, 0, err)
            self.assertEqual(self.sessions[task["session_id"]]["state"], "archived")
            self.assertEqual(self.sessions[task["session_id"]]["status"], "stopped")
            self.assertEqual(self.stored(task)["state"], "abandoned" if abandon else "done")
            self.assertFalse(self.stored(task).get("session_removed"))
            self.assertEqual((Path(task["worktree"]) / ".chief" / "progress.md").read_bytes(), before)
            self.assertTrue(self.branch_exists(task["branch"]))
            self.assertRegex(out.lower(), r"archiv")
        self.assertTrue(all(a[:2] == ("session", "archive") for a in self.mutations()))

    def test_archive_failure_does_not_claim_session_or_task_success(self):
        task = self.task(state="review")
        self.fail_archive.add(task["session_id"])
        code, out, err = self.cli("close", task["id"])
        self.assertNotEqual(code, 0)
        self.assertIn("archive failed", out + err)
        self.assertEqual(self.stored(task), task)
        self.assertEqual(self.sessions[task["session_id"]]["state"], "live")

    def test_remove_session_preserves_progress_before_trash_and_keeps_branch(self):
        task = self.task(state="review")
        expected = (Path(task["worktree"]) / ".chief" / "progress.md").read_bytes()
        code, out, err = self.cli("close", task["id"], "--remove-session")
        self.assertEqual(code, 0, err)
        self.assert_removed(task)
        self.assertEqual(self.stored(task)["state"], "done")
        self.assertEqual(self.progress_at_remove[task["session_id"]], expected)
        self.assertTrue(self.branch_exists(task["branch"]))
        self.assertEqual(len(self.mutations()), 1, "immediate trash does not need a preceding archive")

    def test_remove_session_allows_missing_progress(self):
        task = self.task(state="review")
        (Path(task["worktree"]) / ".chief" / "progress.md").unlink()
        code, _, err = self.cli("close", task["id"], "--remove-session", "--abandon")
        self.assertEqual(code, 0, err)
        self.assert_removed(task)
        self.assertEqual(self.stored(task)["state"], "abandoned")

    def test_remove_session_copy_failure_blocks_even_with_force(self):
        task = self.task(state="review")
        (chief.TASKS / task["id"] / "progress.md").mkdir()
        code, _, _ = self.cli("close", task["id"], "--remove-session", "--force")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.stored(task), task)
        self.assertEqual(self.mutations(), [])

    def test_remove_session_refuses_dirty_worktree_but_force_explicitly_overrides(self):
        task = self.task(state="review")
        (Path(task["worktree"]) / "untracked.txt").write_text("unfinished")
        code, _, _ = self.cli("close", task["id"], "--remove-session")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.stored(task), task)
        self.assertEqual(self.mutations(), [])
        code, _, err = self.cli("close", task["id"], "--remove-session", "--force")
        self.assertEqual(code, 0, err)
        self.assert_removed(task)
        self.assertTrue(self.branch_exists(task["branch"]))

    def test_remove_failure_does_not_mark_task_done_or_session_removed(self):
        task = self.task(state="review")
        self.fail_remove.add(task["session_id"])
        code, _, err = self.cli("close", task["id"], "--remove-session")
        self.assertNotEqual(code, 0)
        self.assertIn("simulated trash failure", err)
        self.assertEqual(self.stored(task), task)


class InboxCleanupTests(CleanupFixture):
    def inbox(self, task):
        app = tui.Inbox(self.cfg, chief, 15)
        item = dict(id=task["session_id"], title=task["name"], task=copy.deepcopy(task), status="idle",
                    signals=[("green", "checks pass", "checks")], log={"turn": "done"}, changed=True)
        app.st["entries"] = [tui.entry(chief, item)]

        def job(label, fn, refresh=True):
            try:
                msg = fn()
                if msg is not None:
                    app.st["msg"] = msg
            except Exception as exc:
                app.st["msg"] = "failed: " + str(exc)
            return True

        self.patch(app, "job", side_effect=job)
        return app

    def close_inbox(self, task):
        app = self.inbox(task)
        app.key("c")
        self.assertIsNotNone(app.st["confirm"])
        app.answered("y")
        # Poll completed work, as the real UI loop does. This also supports a
        # follow-up confirmation queued by the background close job.
        app.validate_repository()
        self.assertEqual(self.stored(task)["state"], "done")
        self.assertEqual(self.sessions[task["session_id"]]["state"], "archived")
        self.assertIsNotNone(app.st["confirm"], "successful close must offer a separate cleanup confirmation")
        self.assertIn("y/N", app.st["confirm"]["prompt"])
        return app

    def test_declining_second_confirmation_keeps_archived_session_worktree_and_branch(self):
        task = self.task(state="review")
        app = self.close_inbox(task)
        app.answered("\n")
        self.assertTrue(Path(task["worktree"]).exists())
        self.assertTrue(self.branch_exists(task["branch"]))
        self.assertFalse(self.stored(task).get("session_removed"))
        self.assertFalse(any(a[0] == "remove" for a in self.mutations()))

    def test_declining_first_confirmation_does_not_close_archive_or_offer_cleanup(self):
        task = self.task(state="review")
        app = self.inbox(task)
        before = self.snapshot()
        for answer in ("\n", "n", "\x1b"):
            with self.subTest(answer=repr(answer)):
                app.key("c")
                self.assertIsNotNone(app.st["confirm"])
                self.assertIn("y/N", app.st["confirm"]["prompt"])
                app.answered(answer)
                app.validate_repository()
                self.assertIsNone(app.st["confirm"])
                self.assertEqual(self.stored(task), task)
                self.assertEqual(self.sessions[task["session_id"]]["state"], "live")
                self.assertEqual(self.mutations(), [])
                self.assertEqual(self.snapshot(), before)

    def test_accepting_second_confirmation_preserves_progress_and_keeps_branch(self):
        task = self.task(state="review")
        expected = (Path(task["worktree"]) / ".chief" / "progress.md").read_bytes()
        app = self.close_inbox(task)
        app.answered("y")
        self.assert_removed(task)
        self.assertEqual(self.progress_at_remove[task["session_id"]], expected)
        self.assertTrue(self.branch_exists(task["branch"]))
        self.assertEqual(self.git_deletions, [])

    def test_dirty_or_failed_cleanup_leaves_task_closed_with_actionable_message(self):
        for kind in ("dirty", "unknown", "copy-failure", "trash-failure"):
            with self.subTest(kind=kind):
                task = self.task(kind, state="review")
                app = self.close_inbox(task)
                if kind == "dirty":
                    (Path(task["worktree"]) / "late.txt").write_text("new work")
                elif kind == "unknown":
                    (Path(task["worktree"]) / ".git").unlink()
                elif kind == "copy-failure":
                    (chief.TASKS / task["id"] / "progress.md").mkdir()
                else:
                    self.fail_remove.add(task["session_id"])
                app.answered("y")
                self.assertEqual(self.stored(task)["state"], "done")
                self.assertFalse(self.stored(task).get("session_removed"))
                self.assertTrue(Path(task["worktree"]).exists())
                self.assertTrue(self.branch_exists(task["branch"]))
                message = str(app.st["msg"]).lower()
                self.assertRegex(message, r"closed|done")
                self.assertRegex(message, r"chief.*clean|retry|commit|inspect|check|progress|permission")

    def test_failed_close_never_offers_cleanup(self):
        task = self.task(state="review")
        self.fail_archive.add(task["session_id"])
        app = self.inbox(task)
        app.key("c")
        app.answered("y")
        app.validate_repository()
        self.assertIsNone(app.st["confirm"])
        self.assertEqual(self.stored(task), task)
        self.assertFalse(any(a[0] == "remove" for a in self.mutations()))


class TaskDisplayAndHelpTests(CleanupFixture):
    def test_tasks_prints_complete_ids_including_identical_long_prefixes(self):
        prefix = "same-long-task-prefix-more-than-twenty-four-characters-"
        tasks = [self.task(prefix + suffix) for suffix in ("one", "two")]
        code, out, err = self.cli("tasks")
        self.assertEqual(code, 0, err)
        for task in tasks:
            self.assertIn(task["id"], out)

    def test_help_explains_clean_flags_and_close_archive_default(self):
        code, out, err = self.cli("clean", "--help")
        self.assertEqual(code, 0, err)
        self.assertIn("--dry-run", out)
        self.assertIn("--branches", out)
        self.assertRegex(out.lower(), r"merged")
        code, out, err = self.cli("close", "--help")
        self.assertEqual(code, 0, err)
        self.assertIn("--remove-session", out)
        self.assertIn("--force", out)
        self.assertRegex(out.lower(), r"archiv")

    def test_readme_documents_cleanup_archive_and_progress_preservation(self):
        readme = read_documentation()
        self.assertIn("chief clean", readme)
        self.assertIn("--branches", readme)
        self.assertIn("--dry-run", readme)
        self.assertIn("aoe session archive", readme)
        self.assertRegex(readme, r"(?i)tasks/[^\s]*progress\.md")
