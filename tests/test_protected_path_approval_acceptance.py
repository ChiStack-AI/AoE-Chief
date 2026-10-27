"""Acceptance contract for explicit, change-derived protected-path approval.

Run real Git scans, task/decision persistence, CLI dispatch and Inbox actions.
Only external worker/session I/O and AI are mocked. Existing acceptance tests
remain unchanged. Contract records in ``allowed`` use exact normalized paths:
``{path, kind: addition}`` or ``{path, kind: state, hash: <digest | deleted>}``.
The latter approves a version, never an unrestricted path. No approval is implied
by worker output, feedback, dismissal, an autonomy mode, or a successful check.
"""
import copy
from contextlib import contextmanager, ExitStack
from pathlib import Path
from unittest import mock

import chief
import chief_tui as tui
from test_intent_task_flow import ChiefFixture


ADDITIONS = "New files in protected paths: "
CHANGES = "Allow changes to protected "


class ProtectedApprovalFixture(ChiefFixture):
    def setUp(self):
        super().setUp()
        for path, contents in {
                "tests/test_existing.py": "# existing acceptance test\n" * 24,
                "tests/helpers/existing.py": "# existing protected support\n" * 24,
                "tests/fixtures/existing.json": '{"baseline": true}\n',
                "config/existing.json": '{"protected": true}\n',
                "src/application.py": "# application\n"}.items():
            self.write(path, contents)
        self.git("add", ".")
        self.git("commit", "-q", "-m", "protected baseline")
        self.task = dict(id="protected-approval", name="Protected deliverables",
                         goal="Implement the requested behavior", repo=str(self.repo),
                         worktree=str(self.repo), scope=".", base=self.git("rev-parse", "HEAD"),
                         protected=["tests", "config"], phase="build", autonomy="suggest",
                         state="needs-you", session_id="0123456789abcdef", tool="codex")
        chief.save_task(self.task)
        self.log = dict(turn="done", last_ts=chief.now_utc().isoformat(),
                        footer={"conclusion": "Changes are ready", "decision": None})

    def item(self, task=None, log=None):
        task = chief.load_task(self.task["id"]) if task is None else task
        item = dict(id=task["session_id"], title=task["name"], task=task,
                    status="idle", path=str(self.repo), log=self.log if log is None else log,
                    scope=chief.scope_info(str(self.repo), task, self.cfg.get("scope_ignore", [])),
                    git={"head": self.git("rev-parse", "HEAD"), "dirty": 1, "branch": "fixture"})
        self.assertIsNotNone(item["scope"], "the real Git scan must succeed")
        item["signals"] = chief.judge(item)
        return item

    def sync(self, task=None, log=None, cfg=None):
        item = self.item(task, log)
        decision = chief.sync_decisions(self.cfg if cfg is None else cfg, item["task"], item)
        if decision:
            item["decision"] = decision
            item["signals"] = chief.decision_signals(item["signals"], decision,
                                                       (item["log"] or {}).get("footer") or {})
        return item

    def pending(self, prefix=None):
        return [d for d in chief.load_decisions(self.task["id"])
                if d["state"] == "pending" and (prefix is None or d["question"].startswith(prefix))]

    def proposal(self, paths, prefix=ADDITIONS, task=None):
        item = self.sync(task)
        pending = self.pending(prefix)
        self.assertEqual(len(pending), 1, "detected changes need one aggregate pending decision")
        decision = pending[0]
        self.assertEqual(decision["size"], "big")
        self.assertFalse(decision.get("draft"), "protected permissions must never have a Chief draft")
        self.assertCountEqual(decision["files"], paths)
        for path in paths:
            self.assertIn(path, decision["question"])
        item["decision"] = decision
        return item, decision

    def assert_red(self, *paths, **kwargs):
        item = self.item(kwargs.get("task"))
        for path in paths:
            self.assertIn(path, item["scope"]["protected_violations"])
        self.assertTrue(any(level == "red" and kind == "protected"
                            for level, _, kind in item["signals"]))
        return item

    def assert_clear(self, *paths, **kwargs):
        item = self.item(kwargs.get("task"))
        for path in paths:
            self.assertNotIn(path, item["scope"]["protected_violations"])
        return item

    def allowance(self, path, kind):
        allowed = chief.load_task(self.task["id"]).get("allowed", [])
        entries = [entry for entry in allowed if isinstance(entry, dict) and entry.get("path") == path]
        self.assertEqual(len(entries), 1, "persist one exact normalized allowance per path")
        self.assertEqual(entries[0]["kind"], kind)
        return entries[0]

    @contextmanager
    def worker(self, delivered=True, turn=None):
        with ExitStack() as stack:
            record = {"id": self.task["session_id"], "project_path": str(self.repo), "status": "idle"}
            stack.enter_context(mock.patch.object(chief, "aoe_records", return_value={record["id"]: record}))
            stack.enter_context(mock.patch.object(chief, "transcript", return_value=self.log if turn is None else turn))
            stack.enter_context(mock.patch.object(chief, "wait_ready", return_value="ready"))
            deliver = stack.enter_context(mock.patch.object(chief, "deliver", return_value=delivered))
            yield deliver

    def accept(self, decision):
        with self.worker() as deliver:
            self.assertTrue(chief.accept_decision(self.cfg, decision))
        deliver.assert_called_once()
        self.assertEqual(chief.find_decision(decision["id"])["resolution"], "accepted")
        return chief.load_task(self.task["id"])

    def inbox(self, item):
        app = tui.Inbox(self.cfg, chief, 15)
        app.st["entries"] = [tui.entry(chief, item)]

        def run_job(label, fn):
            result = fn()
            if result:
                app.st["msg"] = result
            return result

        patch = mock.patch.object(app, "job", side_effect=run_job)
        patch.start()
        self.addCleanup(patch.stop)
        return app

    def submit(self, app, key, text):
        app.key(key)
        self.assertIsNotNone(app.st["input"])
        app.st["input"]["buf"] = text
        app.typed("\n")


class ProtectedDecisionDetectionTests(ProtectedApprovalFixture):
    def test_new_non_tests_aggregate_without_a_worker_question_in_every_mode_and_build_contract(self):
        paths = ["config/new.json", "tests/fast.py", "tests/fixtures/new sample.json",
                 "tests/helpers/new.py", "tests/data.csv", "config/another.json"]
        for path in paths:
            self.write(path, "new deliverable\n")
        self.assertFalse(chief.is_test_file("tests/fast.py"))
        for mode in ("observe", "suggest", "auto"):
            for phase in ("build", "legacy"):
                with self.subTest(mode=mode, phase=phase):
                    chief.save_decisions(self.task["id"], [])
                    task = dict(self.task, autonomy=mode)
                    if phase == "legacy":
                        task.pop("phase")
                    with mock.patch.object(chief, "generate_ai") as generate:
                        item, decision = self.proposal(paths, task=task)
                    generate.assert_not_called()
                    self.assertEqual(decision["question"], ADDITIONS + ", ".join(sorted(paths)) +
                                     ". Allow them as task deliverables?")
                    self.assertEqual(len(self.pending()), 1)
                    self.assertEqual(chief.level_of(item), "red")
                    self.assertCountEqual(item["scope"]["protected_violations"], paths)
                    self.assertFalse(chief.load_task(self.task["id"]).get("allowed"))

    def test_real_watch_pass_queues_and_keeps_permission_pending_in_every_autonomy_mode(self):
        self.write("tests/fast.py", "# deliverable\n")
        sid = self.task["session_id"]
        for mode in ("observe", "suggest", "auto"):
            with self.subTest(mode=mode):
                chief.save_task(dict(self.task, autonomy=mode))
                chief.save_decisions(self.task["id"], [])
                cfg = dict(self.cfg, decision_ai=True, aoe_colors=False)
                with self.worker() as deliver, \
                        mock.patch.object(chief, "listings", return_value=[{"id": sid, "state": "live"}]), \
                        mock.patch.object(chief, "aoe", return_value='{"status":"idle"}'), \
                        mock.patch.object(chief, "screen", return_value=""), \
                        mock.patch.object(chief, "generate_ai") as generate, \
                        mock.patch.object(chief, "accept_decision") as accept, \
                        mock.patch.object(chief, "review_task") as review:
                    for _ in range(3):
                        chief.tick(cfg)
                    result = chief.collect(cfg, checks=False, persist=False)
                self.assertEqual(len(self.pending(ADDITIONS)), 1)
                decision = result["items"][0].get("decision")
                self.assertIsNotNone(decision, "the real Inbox/brief collector must expose the generated decision")
                self.assertFalse(decision.get("draft"))
                self.assertEqual(decision["size"], "big")
                self.assertEqual(chief.level_of(result["items"][0]), "red")
                for external in (generate, accept, deliver, review):
                    external.assert_not_called()

    def test_detection_does_not_need_a_completed_transcript_or_footer(self):
        self.write("tests/fast.py", "# deliverable\n")
        for log in ({}, {"turn": "open", "last_ts": "new-turn"},
                    dict(self.log, footer={"decision": "none"})):
            with self.subTest(log=log):
                chief.save_decisions(self.task["id"], [])
                self.sync(log=log)
                self.assertEqual(len(self.pending(ADDITIONS)), 1)

    def test_aggregate_updates_added_and_removed_paths_without_duplicate_pending_decisions(self):
        first, second = "tests/fast.py", "config/new.json"
        self.write(first, "# deliverable\n")
        _, decision = self.proposal([first])
        for _ in range(3):
            self.sync()
        self.assertEqual([d["id"] for d in self.pending()], [decision["id"]])
        self.write(second, "new config\n")
        self.proposal([first, second])
        (self.repo / first).unlink()
        self.proposal([second])
        self.assertNotIn(first, self.pending()[0]["question"])
        self.assertEqual(len(self.pending()), 1)

    def test_existing_changes_are_also_big_undrafted_decisions_in_every_mode_and_legacy_contract(self):
        path = "config/existing.json"
        self.write(path, "changed configuration\n")
        for mode in ("observe", "suggest", "auto"):
            for phase in ("build", "legacy"):
                with self.subTest(mode=mode, phase=phase):
                    chief.save_decisions(self.task["id"], [])
                    task = dict(self.task, autonomy=mode)
                    if phase == "legacy":
                        task.pop("phase")
                    with mock.patch.object(chief, "generate_ai") as generate:
                        self.proposal([path], CHANGES, task=task)
                    generate.assert_not_called()
                    self.assert_red(path, task=task)
                    self.assertFalse(chief.load_task(self.task["id"]).get("allowed"))

    def test_changed_transcript_timestamp_does_not_supersede_a_still_valid_permission(self):
        self.write("tests/fast.py", "# deliverable\n")
        _, decision = self.proposal(["tests/fast.py"])
        changed_log = dict(self.log, last_ts="2030-01-01T00:00:00+00:00")
        for _ in range(3):
            self.sync(log=changed_log)
        self.assertEqual([d["id"] for d in self.pending()], [decision["id"]])
        with self.worker(turn=changed_log):
            self.assertTrue(chief.accept_decision(self.cfg, chief.find_decision(decision["id"])))
        self.allowance("tests/fast.py", "addition")
        self.assert_clear("tests/fast.py")

    def test_reverted_changes_retire_the_pending_decision_without_permission(self):
        self.write("tests/fast.py", "# deliverable\n")
        self.proposal(["tests/fast.py"])
        (self.repo / "tests/fast.py").unlink()
        for _ in range(3):
            self.sync()
        self.assertEqual(self.pending(), [])
        self.assertFalse(chief.load_task(self.task["id"]).get("allowed"))

    def test_additions_and_existing_changes_have_distinct_aggregates_alongside_worker_decisions(self):
        self.write("tests/fast.py", "# deliverable\n")
        self.write("config/existing.json", "changed config\n")
        log = dict(self.log, footer={"decision": "Plain text or JSON? Options: A) text B) JSON. Recommendation: A"})
        self.sync(log=log)
        for _ in range(3):
            self.sync(log=log)
        self.assertEqual(len(self.pending(ADDITIONS)), 1)
        self.assertEqual(len(self.pending(CHANGES)), 1)
        self.assertEqual(len([d for d in self.pending() if d["question"] == log["footer"]["decision"]]), 1)
        self.assertEqual(len(self.pending()), 3)

    def test_new_tests_keep_yellow_review_and_are_not_deliverable_permission_requests(self):
        self.write("tests/test_new.py", "# new test\n")
        self.write("tests/fast.py", "# deliverable\n")
        item, _ = self.proposal(["tests/fast.py"])
        self.assertIn("tests/test_new.py", item["scope"]["protected_added_tests"])
        self.assertNotIn("tests/test_new.py", item["scope"]["protected_violations"])
        self.assertTrue(any(level == "yellow" and "tests/test_new.py" in message
                            for level, message, _ in item["signals"]))


class ProtectedAdditionAllowanceTests(ProtectedApprovalFixture):
    def test_accept_persists_exact_addition_paths_and_allows_later_edits_after_reload(self):
        paths = ["tests/fast.py", "tests/fixtures/new sample.json"]
        for path in paths:
            self.write(path, "# initial deliverable\n")
        _, decision = self.proposal(paths)
        original = copy.deepcopy(self.task)
        with self.worker() as deliver:
            def inspect_contract(*args):
                for path in paths:
                    self.allowance(path, "addition")
                return True
            deliver.side_effect = inspect_contract
            self.assertTrue(chief.accept_decision(self.cfg, decision))
        current = chief.load_task(self.task["id"])
        for key in ("phase", "base", "protected", "scope"):
            self.assertEqual(current[key], original[key], "protected approval must not rebase or widen the contract")
        for path in paths:
            self.write(path, "# subsequent deliverable edit\n")
        self.assert_clear(*paths)
        self.git("add", ".")
        self.git("commit", "-q", "-m", "deliver approved additions")
        self.write(paths[0], "# edit after committing the approved addition\n")
        self.assert_clear(*paths)
        for _ in range(3):
            self.sync()
        self.assertEqual(self.pending(), [])

    def test_untracked_staged_and_committed_additions_are_approvable(self):
        paths = ["tests/untracked.py", "tests/staged.py", "config/committed.json"]
        self.write(paths[2], "committed deliverable\n")
        self.git("add", paths[2])
        self.git("commit", "-q", "-m", "new config since base")
        self.write(paths[1], "staged deliverable\n")
        self.git("add", paths[1])
        self.write(paths[0], "untracked deliverable\n")
        self.assert_red(*paths)
        _, decision = self.proposal(paths)
        self.accept(decision)
        self.assert_clear(*paths)
        for path in paths:
            self.allowance(path, "addition")

    def test_legacy_contract_can_accept_deliverables_without_becoming_test_first(self):
        task = dict(self.task)
        task.pop("phase")
        chief.save_task(task)
        self.write("tests/fast.py", "# deliverable\n")
        _, decision = self.proposal(["tests/fast.py"])
        current = self.accept(decision)
        self.assertNotIn("phase", current)
        self.assert_clear("tests/fast.py")

    def test_addition_allowance_does_not_cover_siblings_or_directory_descendants(self):
        self.write("tests/artifact", "a file, not a directory\n")
        _, decision = self.proposal(["tests/artifact"])
        self.accept(decision)
        (self.repo / "tests/artifact").unlink()
        self.write("tests/artifact/child.py", "# child must be reviewed separately\n")
        self.write("tests/artifact_sibling.py", "# sibling must be reviewed separately\n")
        self.assert_red("tests/artifact/child.py", "tests/artifact_sibling.py")
        self.proposal(["tests/artifact/child.py", "tests/artifact_sibling.py"])

    def test_allowance_cannot_be_transferred_by_renaming_an_approved_addition(self):
        original, moved = "tests/fast.py", "tests/other.py"
        self.write(original, "# deliverable\n")
        _, decision = self.proposal([original])
        self.accept(decision)
        self.git("add", original)
        self.git("commit", "-q", "-m", "commit approved addition")
        self.git("mv", original, moved)
        self.assert_red(moved)
        self.assertFalse(any(entry.get("path") == moved
                             for entry in chief.load_task(self.task["id"])["allowed"]))

    def test_addition_allowances_never_authorize_baseline_existing_or_explicitly_protected_files(self):
        paths = ["tests/test_existing.py", "tests/helpers/existing.py", "config/existing.json",
                 "tests/test_approved.py", "tests/approved_support.py"]
        task = dict(self.task, protected=self.task["protected"] + paths[-2:],
                    allowed=[{"path": path, "kind": "addition"} for path in paths])
        chief.save_task(task)
        for path in paths:
            self.write(path, "# unapproved version\n")
        self.assert_red(*paths)
        self.proposal(paths, CHANGES)

    def test_exact_normalization_cannot_widen_an_addition_allowance(self):
        chief.save_task(dict(self.task, protected=["./tests/", "./config/"]))
        self.write("tests/fast.py", "# deliverable\n")
        _, decision = self.proposal(["tests/fast.py"])
        self.accept(decision)
        self.allowance("tests/fast.py", "addition")
        self.write("tests/nested/fast.py", "# separate deliverable\n")
        self.write("config/fast.py", "# separate protected directory\n")
        self.assert_red("tests/nested/fast.py", "config/fast.py")
        self.assert_clear("tests/fast.py")
        self.assertEqual(len(chief.load_task(self.task["id"])["allowed"]), 1)

    def test_approval_does_not_implicitly_include_paths_added_after_the_card_was_displayed(self):
        self.write("tests/fast.py", "# first deliverable\n")
        _, decision = self.proposal(["tests/fast.py"])
        self.write("tests/later.py", "# not in the displayed approval set\n")
        with self.worker():
            try:
                chief.accept_decision(self.cfg, decision)
            except (RuntimeError, ValueError):
                pass  # Rejecting a stale aggregate is also safe.
        self.assert_red("tests/later.py")
        self.assertFalse(any(entry.get("path") == "tests/later.py"
                             for entry in chief.load_task(self.task["id"]).get("allowed", [])))

    def test_scope_ignores_boundaries_and_unrelated_red_signals_survive_approval(self):
        self.cfg["scope_ignore"] = ["generated-cache"]
        chief.save_task(dict(self.task, scope="src", note="manual hold"))
        self.write("tests/fast.py", "# deliverable\n")
        self.write("tests/generated-cache/cache.py", "# generated\n")
        _, decision = self.proposal(["tests/fast.py"])
        self.accept(decision)
        item = self.assert_clear("tests/fast.py")
        self.assertNotIn("tests/generated-cache/cache.py", item["scope"]["protected_touched"])
        self.assertIn("tests/fast.py", item["scope"]["out_of_scope"])
        self.assertTrue(any(kind == "scope" for _, _, kind in item["signals"]))
        self.assertIn(("red", "manual hold", "note"), item["signals"])


class ProtectedStateApprovalTests(ProtectedApprovalFixture):
    def test_modifications_and_deletions_aggregate_as_exact_state_approvals(self):
        changed, deleted = "config/existing.json", "tests/fixtures/existing.json"
        self.write(changed, "approved configuration\n")
        (self.repo / deleted).unlink()
        _, decision = self.proposal([changed, deleted], CHANGES)
        self.assertEqual(decision["question"], CHANGES + ", ".join(sorted([changed, deleted])) + "?")
        self.accept(decision)
        record = self.allowance(changed, "state")
        self.assertRegex(record["hash"], r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
        self.assertEqual(self.allowance(deleted, "state")["hash"], "deleted")
        self.assert_clear(changed, deleted)

    def test_edited_existing_files_need_new_hash_approval_after_each_further_change(self):
        path = "config/existing.json"
        self.write(path, "first approved configuration\n")
        _, first = self.proposal([path], CHANGES)
        self.accept(first)
        old_hash = self.allowance(path, "state")["hash"]
        self.assert_clear(path)
        self.write(path, "later unapproved configuration\n")
        self.assert_red(path)
        _, second = self.proposal([path], CHANGES)
        self.assertNotEqual(first["id"], second["id"])
        self.accept(second)
        self.assertNotEqual(self.allowance(path, "state")["hash"], old_hash)
        self.assert_clear(path)
        self.sync()
        self.assertEqual(self.pending(), [])

    def test_restoring_an_approved_content_hash_clears_only_its_protected_violation(self):
        path = "tests/helpers/existing.py"
        self.write(path, "approved helper\n")
        _, decision = self.proposal([path], CHANGES)
        self.accept(decision)
        self.write(path, "unapproved helper\n")
        self.assert_red(path)
        self.sync()
        self.write(path, "approved helper\n")
        self.assert_clear(path)
        self.sync()
        self.assertEqual(self.pending(), [], "a matching approved state no longer needs renewed approval")

    def test_recreated_deletion_and_new_deletion_of_approved_content_require_fresh_approval(self):
        path = "tests/helpers/existing.py"
        (self.repo / path).unlink()
        _, decision = self.proposal([path], CHANGES)
        self.accept(decision)
        self.assertEqual(self.allowance(path, "state")["hash"], "deleted")
        self.write(path, "recreated helper\n")
        self.assert_red(path)
        _, recreated = self.proposal([path], CHANGES)
        self.accept(recreated)
        self.assert_clear(path)
        (self.repo / path).unlink()
        self.assert_red(path)
        self.proposal([path], CHANGES)

    def test_approving_one_existing_path_does_not_allow_other_protected_changes(self):
        changed, other = "config/existing.json", "tests/helpers/existing.py"
        self.write(changed, "approved config\n")
        _, decision = self.proposal([changed], CHANGES)
        self.accept(decision)
        self.write(other, "unapproved helper\n")
        self.assert_clear(changed)
        self.assert_red(other)
        self.proposal([other], CHANGES)

    def test_unstaged_staged_and_committed_existing_changes_can_be_approved(self):
        for stage in ("unstaged", "staged", "committed"):
            with self.subTest(stage=stage):
                path = "config/existing.json"
                self.write(path, stage + " changed configuration\n")
                if stage != "unstaged":
                    self.git("add", path)
                if stage == "committed":
                    self.git("commit", "-q", "-m", "protected configuration change")
                self.assert_red(path)
                _, decision = self.proposal([path], CHANGES)
                self.accept(decision)
                self.assert_clear(path)

    def test_staged_only_further_edits_invalidate_approval_even_when_disk_matches(self):
        path = "tests/helpers/existing.py"
        self.write(path, "approved helper\n")
        self.git("add", path)
        _, decision = self.proposal([path], CHANGES)
        self.accept(decision)
        self.write(path, "unapproved staged helper\n")
        self.git("add", path)
        self.write(path, "approved helper\n")
        self.assert_red(path)
        self.proposal([path], CHANGES)

    def test_approval_cannot_hide_divergent_staged_contents_or_staged_deletion(self):
        for index_state in ("different-content", "deleted"):
            with self.subTest(index_state=index_state):
                path = "config/existing.json"
                self.git("reset", "-q", "HEAD", "--", path)
                self.write(path, "hidden staged contents\n")
                if index_state == "deleted":
                    (self.repo / path).unlink()
                self.git("add", "-u", "--", path)
                self.write(path, "visible contents\n")
                _, decision = self.proposal([path], CHANGES)
                with self.worker():
                    try:
                        chief.accept_decision(self.cfg, decision)
                    except (RuntimeError, ValueError):
                        pass  # Refusing ambiguous versions is preferable to hiding staged changes.
                self.assert_red(path)

    def test_existing_deletions_are_approvable_unstaged_staged_and_committed(self):
        paths = ["tests/helpers/existing.py", "tests/fixtures/existing.json", "config/existing.json"]
        for path, stage in zip(paths, ("unstaged", "staged", "committed")):
            with self.subTest(stage=stage):
                (self.repo / path).unlink()
                if stage != "unstaged":
                    self.git("add", "-u", "--", path)
                if stage == "committed":
                    self.git("commit", "-q", "-m", "delete protected configuration")
                self.assert_red(path)
                _, decision = self.proposal([path], CHANGES)
                self.accept(decision)
                self.assertEqual(self.allowance(path, "state")["hash"], "deleted")
                self.assert_clear(path)

    def test_rename_within_protected_paths_approves_both_sides_as_states(self):
        source, destination = "tests/helpers/existing.py", "tests/helpers/renamed.py"
        self.git("mv", source, destination)
        self.assert_red(source, destination)
        _, decision = self.proposal([source, destination], CHANGES)
        self.assertEqual(self.pending(ADDITIONS), [], "a rename is not a new deliverable allowance")
        self.accept(decision)
        self.assertEqual(self.allowance(source, "state")["hash"], "deleted")
        self.assertNotEqual(self.allowance(destination, "state")["hash"], "deleted")
        self.assert_clear(source, destination)
        self.git("commit", "-q", "-m", "approved protected rename")
        self.assert_clear(source, destination)
        self.write(destination, "unapproved revision to renamed helper\n")
        self.assert_red(destination)
        self.proposal([destination], CHANGES)

    def test_rename_outside_protection_keeps_destination_bound_to_approved_content(self):
        source, destination = "tests/helpers/existing.py", "src/moved_helper.py"
        self.git("mv", source, destination)
        _, decision = self.proposal([source, destination], CHANGES)
        self.assert_red(source)
        self.accept(decision)
        self.allowance(source, "state")
        self.allowance(destination, "state")
        self.assert_clear(source, destination)
        self.git("commit", "-q", "-m", "approved move outside protected directory")
        self.write(destination, "unapproved further edit to moved protected helper\n")
        item = self.item()
        self.assertTrue(item["scope"]["protected_violations"], "moving outside must not bypass state protection")
        self.proposal([source, destination], CHANGES)

    def test_unstaged_move_is_not_mistaken_for_an_unrestricted_addition(self):
        source, destination = "tests/helpers/existing.py", "tests/helpers/renamed.py"
        (self.repo / source).rename(self.repo / destination)
        _, decision = self.proposal([source, destination], CHANGES)
        self.assertEqual(self.pending(ADDITIONS), [])
        self.accept(decision)
        self.allowance(destination, "state")
        self.write(destination, "unapproved edit\n")
        self.assert_red(destination)

    def test_move_into_protection_does_not_become_an_addition_allowance(self):
        source, destination = "src/application.py", "tests/moved_application.py"
        self.git("mv", source, destination)
        _, decision = self.proposal([source, destination], CHANGES)
        self.accept(decision)
        self.allowance(destination, "state")
        self.write(destination, "# later changed application\n")
        self.assert_red(destination)

    def test_renamed_approved_test_keeps_state_protection_instead_of_new_test_review(self):
        source, destination = "tests/test_existing.py", "tests/test_renamed.py"
        chief.save_task(dict(self.task, protected=self.task["protected"] + [source]))
        self.git("mv", source, destination)
        item, decision = self.proposal([source, destination], CHANGES)
        self.assertNotIn(destination, item["scope"]["protected_added_tests"])
        self.accept(decision)
        self.allowance(destination, "state")
        self.assert_clear(source, destination)
        self.write(destination, "# unapproved changed acceptance tests\n")
        self.assert_red(destination)

    def test_approved_acceptance_and_support_files_require_exact_changed_state_approval(self):
        test, helper = "tests/test_approved.py", "tests/approved_helper.py"
        self.write(test, "# approved acceptance tests\n")
        self.write(helper, "# approved support\n")
        self.git("add", test, helper)
        self.git("commit", "-q", "-m", "approve acceptance and support files")
        contract = chief.approve_tests(dict(self.task, phase="test", test_first=True), [test, helper])
        contract["allowed"] = [{"path": path, "kind": "addition"} for path in (test, helper)]
        chief.save_task(contract)
        for path in (test, helper):
            self.write(path, "# unapproved revision\n")
        self.assert_red(test, helper)
        _, decision = self.proposal([test, helper], CHANGES)
        self.accept(decision)
        for path in (test, helper):
            self.allowance(path, "state")
        self.assert_clear(test, helper)
        self.write(test, "# next unapproved acceptance change\n")
        self.assert_red(test)


class ProtectedApprovalActionTests(ProtectedApprovalFixture):
    def test_cli_accept_and_inbox_a_accept_undrafted_additions_and_existing_states(self):
        cases = (("tests/fast.py", ADDITIONS), ("config/existing.json", CHANGES))
        for path, prefix in cases:
            for action in ("cli", "inbox"):
                with self.subTest(path=path, action=action):
                    chief.save_task(copy.deepcopy(self.task))
                    chief.save_decisions(self.task["id"], [])
                    self.write(path, "# requested deliverable or approved state\n")
                    item, decision = self.proposal([path], prefix)
                    with self.worker() as deliver:
                        if action == "cli":
                            code, out, err = self.cli("decide", decision["id"], "--accept")
                            self.assertEqual(code, 0, err)
                            self.assertRegex(out.lower(), r"approv|allow")
                            self.assertNotIn("chief's draft", out.lower())
                        else:
                            app = self.inbox(item)
                            app.key("a")
                            self.assertRegex(app.st["msg"].lower(), r"approv|allow")
                    deliver.assert_called_once()
                    self.allowance(path, "addition" if prefix == ADDITIONS else "state")
                    self.assert_clear(path)
                    self.assertEqual(chief.find_decision(decision["id"])["resolution"], "accepted")
            # Isolate the two change categories without rewriting Git history.
            if prefix == ADDITIONS:
                (self.repo / path).unlink()

    def test_cli_and_inbox_replies_including_consent_only_send_feedback(self):
        for path, prefix in (("tests/fast.py", ADDITIONS), ("config/existing.json", CHANGES)):
            for action in ("cli", "inbox"):
                for text in ("Allow only the first path", "approved", "yes", "同意"):
                    with self.subTest(path=path, action=action, text=text):
                        chief.save_task(copy.deepcopy(self.task))
                        chief.save_decisions(self.task["id"], [])
                        self.write(path, "# changed file\n")
                        item, decision = self.proposal([path], prefix)
                        with self.worker() as deliver, mock.patch.object(chief, "accept_decision") as accept:
                            if action == "cli":
                                code, _, err = self.cli("decide", decision["id"], "--reply", text)
                                self.assertEqual(code, 0, err)
                            else:
                                app = self.inbox(item)
                                self.submit(app, "r", text)
                                self.assertIsNone(app.st["confirm"], "protected-path feedback never grants permission")
                        accept.assert_not_called()
                        deliver.assert_called_once()
                        self.assertIn(text, deliver.call_args[0][2])
                        self.assertFalse(chief.load_task(self.task["id"]).get("allowed"))
                        self.assert_red(path)
                        self.assertNotEqual(chief.find_decision(decision["id"]).get("resolution"), "accepted")
            if prefix == ADDITIONS:
                (self.repo / path).unlink()

    def test_cli_and_inbox_dismiss_send_nothing_and_leave_violations_red_on_later_passes(self):
        self.write("tests/fast.py", "# deliverable\n")
        for action in ("cli", "inbox"):
            with self.subTest(action=action):
                chief.save_decisions(self.task["id"], [])
                item, decision = self.proposal(["tests/fast.py"])
                with self.worker() as deliver:
                    if action == "cli":
                        code, _, err = self.cli("decide", decision["id"], "--dismiss", "not authorized")
                        self.assertEqual(code, 0, err)
                    else:
                        self.submit(self.inbox(item), "d", "not authorized")
                deliver.assert_not_called()
                self.assertEqual(chief.find_decision(decision["id"])["state"], "dismissed")
                for _ in range(3):
                    self.sync()
                self.assertLessEqual(len(self.pending(ADDITIONS)), 1)
                self.assertFalse(chief.load_task(self.task["id"]).get("allowed"))
                self.assert_red("tests/fast.py")

    def test_feedback_and_dismissal_do_not_suppress_fresh_approval_when_paths_change_in_the_same_turn(self):
        first, later = "tests/fast.py", "tests/later.py"
        for resolution in ("feedback", "dismissed"):
            with self.subTest(resolution=resolution):
                chief.save_task(copy.deepcopy(self.task))
                chief.save_decisions(self.task["id"], [])
                if (self.repo / later).exists():
                    (self.repo / later).unlink()
                self.write(first, "# deliverable\n")
                _, decision = self.proposal([first])
                if resolution == "feedback":
                    with self.worker():
                        self.assertTrue(chief.reply_decision(self.cfg, decision, "Please revise these files"))
                else:
                    chief.dismiss_decision(decision, "not authorized")
                self.write(later, "# another deliverable\n")
                self.write(first, "# revised after feedback\n")
                for _ in range(3):
                    self.sync()
                self.proposal([first, later])
                self.assert_red(first, later)
                self.assertFalse(chief.load_task(self.task["id"]).get("allowed"))

    def test_explicit_permission_is_persisted_even_if_worker_delivery_is_unconfirmed(self):
        for path, prefix, kind in (("tests/fast.py", ADDITIONS, "addition"),
                                   ("config/existing.json", CHANGES, "state")):
            with self.subTest(kind=kind):
                chief.save_task(copy.deepcopy(self.task))
                chief.save_decisions(self.task["id"], [])
                self.write(path, "# user-approved change\n")
                _, decision = self.proposal([path], prefix)
                with self.worker(delivered=False):
                    self.assertFalse(chief.accept_decision(self.cfg, decision))
                self.allowance(path, kind)
                self.assert_clear(path)
                resolved = chief.find_decision(decision["id"])
                self.assertEqual(resolved["resolution"], "accepted")
                self.assertIs(resolved["delivered"], False)
                self.assertEqual(chief.load_task(self.task["id"])["state"], "needs-you")
            if prefix == ADDITIONS:
                (self.repo / path).unlink()

    def test_explicit_redraft_and_ai_cannot_create_a_permission_draft_or_grant_permission(self):
        self.write("tests/fast.py", "# deliverable\n")
        for args in (("--redraft",), ("--ai",)):
            with self.subTest(args=args):
                chief.save_decisions(self.task["id"], [])
                _, decision = self.proposal(["tests/fast.py"])
                with mock.patch.object(chief, "generate_ai", return_value='{"size":"small","reply":"Decision: yes"}') as generate:
                    code, _, err = self.cli("decide", decision["id"], *args)
                self.assertIn(code, (0, 1), err)  # Either a no-op or an explicit refusal is safe.
                generate.assert_not_called()
                current = chief.find_decision(decision["id"])
                self.assertEqual(current["size"], "big")
                self.assertFalse(current.get("draft"))
                self.assertEqual(current["state"], "pending")
                self.assertFalse(chief.load_task(self.task["id"]).get("allowed"))

    def test_unapproved_changes_block_act_continuation_review_and_interruption_recovery(self):
        self.write("tests/fast.py", "# unapproved deliverable\n")
        for mode in ("suggest", "auto"):
            for turn in ("done", "open", "error"):
                for passing in (False, True):
                    with self.subTest(mode=mode, turn=turn, passing=passing):
                        task = dict(self.task, autonomy=mode)
                        item = self.item(task, dict(self.log, turn=turn))
                        item["checks"] = {"passed": int(passing), "total": 1,
                                          "results": [{"cmd": "check", "ok": passing, "code": 0 if passing else 1,
                                                       "tail": "failed"}]}
                        with self.worker() as deliver, \
                                mock.patch.object(chief, "review_task") as review, \
                                mock.patch.object(chief, "rotate_task") as rotate:
                            result = chief.act(self.cfg, item, task)
                        self.assertIsNone(result)
                        deliver.assert_not_called()
                        review.assert_not_called()
                        rotate.assert_not_called()
        self.assertFalse(chief.load_task(self.task["id"]).get("allowed"))

    def test_matching_approvals_remove_only_the_protected_gate_to_continuation_and_cross_review(self):
        self.write("tests/fast.py", "# deliverable\n")
        _, decision = self.proposal(["tests/fast.py"])
        self.accept(decision)
        for mode in ("observe", "suggest", "auto"):
            for passing in (False, True):
                with self.subTest(mode=mode, passing=passing):
                    task = dict(chief.load_task(self.task["id"]), autonomy=mode, state="running", auto={})
                    item = self.item(task)
                    item["checks"] = {"passed": int(passing), "total": 1,
                                      "results": [{"cmd": "check", "ok": passing, "code": 0 if passing else 1,
                                                   "tail": "failed"}]}
                    with self.worker() as deliver, \
                            mock.patch.object(chief, "review_task", return_value={"verdict": "pass"}) as review:
                        result = chief.act(dict(self.cfg, review=True), item, task)
                    if mode == "observe":
                        self.assertIsNone(result)
                        review.assert_not_called()
                        deliver.assert_not_called()
                    elif passing:
                        review.assert_called_once()
                        deliver.assert_not_called()
                    else:
                        deliver.assert_called_once()
                        review.assert_not_called()
        chief.save_task(dict(chief.load_task(self.task["id"]), note="manual hold"))
        item = self.item()
        item["checks"] = {"passed": 1, "total": 1, "results": [{"ok": True}]}
        with mock.patch.object(chief, "review_task") as review:
            self.assertIsNone(chief.act(self.cfg, item, item["task"]))
        review.assert_not_called()


class ProtectedApprovalVisibilityTests(ProtectedApprovalFixture):
    def test_inbox_detail_and_footer_explain_protected_acceptance_feedback_and_dismissal_in_both_languages(self):
        self.write("tests/fast.py", "# deliverable\n")
        item, _ = self.proposal(["tests/fast.py"])
        for lang in ("en", "zh"):
            with self.subTest(language=lang), mock.patch.object(chief, "LANG", lang):
                entry = tui.entry(chief, item)
                actions = [action for action in tui.keys_for(chief, entry) if action.startswith("a ")]
                self.assertEqual(len(actions), 1, "undrafted protected approvals need a visible accept action")
                detail = "\n".join(text for _, text in entry["detail"])
                self.assertIn("tests/fast.py", detail)
                if lang == "en":
                    self.assertRegex(actions[0], r"approv|allow|accept")
                    self.assertRegex(detail.lower(), r"\ba\b[^\n]*(?:approv|allow)[^\n]*(?:path|deliverable)")
                    self.assertRegex(detail.lower(), r"\br\b[^\n]*feedback[^\n]*(?:no|without|never|does not)")
                    self.assertRegex(detail.lower(), r"\bd\b[^\n]*(?:dismiss|close)[^\n]*(?:no|without|remain|red)")
                else:
                    self.assertRegex(actions[0], r"批准|允许|采纳")
                    self.assertRegex(detail, r"a[^\n]*(?:批准|允许)[^\n]*(?:路径|交付)")
                    self.assertRegex(detail, r"r[^\n]*反馈[^\n]*(?:不|无)")
                    self.assertRegex(detail, r"d[^\n]*(?:关闭|驳回|忽略)[^\n]*(?:不|无|红)")
                app = self.inbox(item)
                footer = "".join(text for text, _ in tui.render(chief, app.st, 240, 40)[-1])
                self.assertIn(actions[0], footer)

    def test_existing_state_inbox_detail_explains_hash_bound_permission_and_renewal_in_both_languages(self):
        self.write("config/existing.json", "changed configuration\n")
        item, _ = self.proposal(["config/existing.json"], CHANGES)
        for lang in ("en", "zh"):
            with self.subTest(language=lang), mock.patch.object(chief, "LANG", lang):
                entry = tui.entry(chief, item)
                self.assertTrue(any(action.startswith("a ") for action in tui.keys_for(chief, entry)))
                detail = "\n".join(text for _, text in entry["detail"])
                if lang == "en":
                    self.assertRegex(detail.lower(), r"hash|current (?:state|version)|exact (?:state|version)")
                    self.assertRegex(detail.lower(), r"(?:further|later|edit|chang)[^\n]*(?:approv|red)")
                else:
                    self.assertRegex(detail, r"哈希|散列|当前(?:状态|版本)|精确(?:状态|版本)")
                    self.assertRegex(detail, r"(?:再改|后续|修改|更改)[^\n]*(?:批准|审批|红)")

    def test_bilingual_cli_help_explains_exact_approval_feedback_dismissal_and_renewal(self):
        for lang in ("en", "zh"):
            with self.subTest(language=lang), mock.patch.object(chief, "LANG", lang):
                code, out, err = self.cli("decide", "--help")
                self.assertEqual(code, 0, err)
                help_text = " ".join(out.split())
                if lang == "en":
                    self.assertRegex(help_text.lower(), r"--accept.*(?:protected|deliverable)")
                    self.assertRegex(help_text.lower(), r"--reply.*feedback.*(?:no permission|without.*permission|does not.*(?:allow|approv|grant))")
                    self.assertRegex(help_text.lower(), r"--dismiss.*(?:no permission|without.*permission|red|does not.*(?:allow|approv|grant))")
                    self.assertRegex(help_text.lower(), r"(?:further|later|edited|changed).*approv|approv.*(?:again|renew)")
                else:
                    self.assertRegex(help_text, r"--accept.*(?:受保护|交付)")
                    self.assertRegex(help_text, r"--reply.*反馈.*(?:不.*(?:授权|允许|批准)|无权限)")
                    self.assertRegex(help_text, r"--dismiss.*(?:不.*(?:授权|允许|批准)|红|无权限)")
                    self.assertRegex(help_text, r"(?:再次|重新|后续).*批准|(?:再改|修改后).*审批")

    def test_approved_paths_types_and_states_appear_in_worker_inbox_review_prompt_and_card(self):
        added, changed, deleted = "tests/fast.py", "config/existing.json", "tests/fixtures/existing.json"
        self.write(added, "# deliverable\n")
        self.write(changed, "approved configuration\n")
        (self.repo / deleted).unlink()
        self.sync()
        for prefix in (ADDITIONS, CHANGES):
            pending = self.pending(prefix)
            self.assertEqual(len(pending), 1)
            self.accept(pending[0])
        task = chief.load_task(self.task["id"])
        digest = self.allowance(changed, "state")["hash"]
        item = self.item(task)
        kickoff = chief.kickoff_prompt(task)
        detail = "\n".join(text for _, text in tui.entry(chief, item)["detail"])
        prompt = chief.review_prompt(task, {"total": 1}, "diff", "notes")
        for text in (kickoff, detail, prompt):
            self.assertIn(added, text)
            self.assertIn(changed, text)
            self.assertIn(deleted, text)
            self.assertIn("deleted", text.lower())
            self.assertIn(digest[:10], text)
            self.assertRegex(text.lower(), r"addition")
            self.assertRegex(text.lower(), r"state|hash|version")
        item["checks"] = {"passed": 1, "total": 1, "results": [{"ok": True}]}
        answer = "Verdict: pass\nSummary: meets the goal\nRisks: none\nLook at: src/application.py:1\nIssues: none"
        with mock.patch.object(chief, "generate_ai", return_value=answer) as generate:
            chief.review_task(self.cfg, task, item)
        reviewer_material = generate.call_args[0][0]
        card = (chief.TASKS / task["id"] / "review.md").read_text(encoding="utf-8")
        for text in (reviewer_material, card):
            self.assertIn(added, text, "approval visibility must not depend on the reviewer's free-form answer")
            self.assertIn(changed, text)
            self.assertIn(deleted, text)
            self.assertIn("deleted", text.lower())
            self.assertIn(digest[:10], text)
        self.write(changed, "unapproved further change\n")
        stale = chief.kickoff_prompt(chief.load_task(task["id"]))
        self.assertRegex(stale.lower(), r"(?:further|changed|mismatch|stale|again|renew)")

    def test_readme_documents_deliverable_allowances_and_version_bound_existing_changes(self):
        text = (Path(chief.__file__).resolve().parent / "README.md").read_text(encoding="utf-8")
        self.assertTrue("New files in protected paths:" in text, "README must name the deliverable approval decision")
        self.assertTrue("Allow changes to protected" in text, "README must name the protected-state approval decision")
        self.assertRegex(text.lower(), r"exact[^.\n]*path")
        self.assertRegex(text.lower(), r"hash|content version")
        self.assertRegex(text.lower(), r"deletion[^.\n]*marker|deleted[^.\n]*state")
        self.assertRegex(text.lower(), r"(?:further|later|subsequent)[^.\n]*(?:edit|change)[^.\n]*(?:approv|red)")
        self.assertRegex(text.lower(), r"(?:dismiss|dismissal)[^.\n]*(?:permission|red|authoriz)")


class ProtectedApprovalTestPhaseRegressionTests(ProtectedApprovalFixture):
    def test_build_permission_cannot_be_accepted_after_contract_returns_to_test_phase(self):
        self.write("config/new.json", "# build-phase deliverable\n")
        _, decision = self.proposal(["config/new.json"])
        chief.save_task(dict(self.task, phase="test", test_first=True))
        with self.worker():
            with self.assertRaises((RuntimeError, ValueError)):
                chief.accept_decision(self.cfg, decision)
        self.assertFalse(chief.load_task(self.task["id"]).get("allowed"))
        self.assert_red("config/new.json")

    def test_test_phase_keeps_exempt_tests_support_review_and_other_files_red_without_new_flow(self):
        task = dict(self.task, phase="test", test_first=True,
                    protected=self.task["protected"] + ["tests/locked.py"])
        chief.save_task(task)
        self.write("tests/test_existing.py", "# revised acceptance coverage\n")
        self.write("tests/test_new.py", "# new acceptance coverage\n")
        self.write("tests/fast.py", "# proposed test support\n")
        self.write("tests/locked.py", "# explicitly protected file\n")
        self.write("tests/helpers/existing.py", "# changed existing support\n")
        self.write("config/new.json", "new protected config\n")
        for mode in ("observe", "suggest", "auto"):
            for implicit_phase in (False, True):
                with self.subTest(mode=mode, implicit_phase=implicit_phase):
                    contract = dict(task, autonomy=mode)
                    if implicit_phase:
                        contract.pop("phase")
                    item = self.sync(contract)
                    self.assertEqual(self.pending(), [], "test-phase violations must not enable build approvals")
                    for path in ("tests/test_existing.py", "tests/test_new.py"):
                        self.assertNotIn(path, item["scope"]["protected_touched"])
                    self.assertIn("tests/fast.py", item["scope"]["protected_added_support"])
                    self.assertNotIn("tests/fast.py", item["scope"]["protected_violations"])
                    for path in ("tests/locked.py", "tests/helpers/existing.py", "config/new.json"):
                        self.assertIn(path, item["scope"]["protected_violations"])

    def test_test_approval_still_locks_tests_and_support_and_transitions_to_build_without_allowances(self):
        listed, support = "tests/test_acceptance.py", "tests/new_support.py"
        task = dict(self.task, phase="test", test_first=True)
        chief.save_task(task)
        self.write(listed, "# proposed acceptance tests\n")
        self.write(support, "# test support omitted from worker footer\n")
        self.git("add", listed, support)
        self.git("commit", "-q", "-m", "propose tests and support")
        self.log["footer"] = {"decision": "approve the acceptance tests " + listed}
        item = self.sync()
        decision = item.get("decision")
        self.assertIsNotNone(decision)
        self.assertEqual(decision["kind"], "test-approval")
        self.assertFalse(decision.get("draft"))
        self.assertEqual(len(self.pending()), 1)
        current = self.accept(decision)
        self.assertEqual(current["phase"], "build")
        self.assertEqual(current["base"], self.git("rev-parse", "HEAD"))
        for path in (listed, support):
            self.assertIn(path, current["protected"])
            self.write(path, "# unapproved change after test approval\n")
        self.assertFalse(current.get("allowed"))
        self.assert_red(listed, support)
