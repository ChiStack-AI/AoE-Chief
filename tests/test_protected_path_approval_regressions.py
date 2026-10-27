"""Additional build regressions; the approved acceptance contract is unchanged."""
from unittest import mock

import chief
from test_protected_path_approval_acceptance import ProtectedApprovalFixture, CHANGES


class ProtectedApprovalRegressions(ProtectedApprovalFixture):
    def test_worker_echoing_generated_question_does_not_duplicate_it_or_invoke_ai(self):
        self.write("tests/fast.py", "# deliverable\n")
        _, decision = self.proposal(["tests/fast.py"])
        log = dict(self.log, last_ts="2030-01-01T00:00:00+00:00",
                   footer={"decision": decision["question"]})
        with mock.patch.object(chief, "generate_ai") as generate:
            for _ in range(3):
                self.sync(log=log, cfg=dict(self.cfg, decision_ai=True))
        generate.assert_not_called()
        self.assertEqual([d["id"] for d in self.pending()], [decision["id"]])

    def test_ignored_protected_rename_source_does_not_extend_protection_outside_it(self):
        self.cfg["scope_ignore"] = ["helpers"]
        self.git("mv", "tests/helpers/existing.py", "src/moved_helper.py")
        item = self.sync()
        self.assertEqual(item["scope"]["protected_violations"], [])
        self.assertEqual(self.pending(), [])

    def test_binary_content_and_executable_mode_are_version_bound(self):
        path = "config/existing.json"
        (self.repo / path).write_bytes(b"\x00\xff\x80approved\r\n")
        _, decision = self.proposal([path], CHANGES)
        self.accept(decision)
        self.assert_clear(path)
        (self.repo / path).chmod(0o755)
        self.assert_red(path)
        _, decision = self.proposal([path], CHANGES)
        self.accept(decision)
        self.assert_clear(path)
        (self.repo / path).write_bytes(b"\x00\xff\x80unapproved\r\n")
        self.assert_red(path)

    def test_symlink_hash_tracks_its_identity_and_does_not_follow_its_target(self):
        path = "tests/helpers/existing.py"
        file = self.repo / path
        file.unlink()
        file.symlink_to("first-missing-target")
        _, decision = self.proposal([path], CHANGES)
        self.accept(decision)
        self.git("add", path)
        self.assert_clear(path)
        file.unlink()
        file.symlink_to("second-missing-target")
        self.assert_red(path)

    def test_state_changed_after_display_cannot_be_accepted_without_refresh(self):
        path = "config/existing.json"
        self.write(path, "displayed version\n")
        _, decision = self.proposal([path], CHANGES)
        self.write(path, "unreviewed version\n")
        with self.worker() as deliver, self.assertRaises(RuntimeError):
            chief.accept_decision(self.cfg, decision)
        deliver.assert_not_called()
        self.assert_red(path)
        self.assertFalse(chief.load_task(self.task["id"]).get("allowed"))

    def test_inbox_old_card_cannot_approve_an_expanded_aggregate(self):
        self.write("tests/fast.py", "# first deliverable\n")
        item, _ = self.proposal(["tests/fast.py"])
        app = self.inbox(item)
        self.write("tests/later.py", "# not on the displayed card\n")
        self.sync()
        with self.worker() as deliver, self.assertRaises(RuntimeError):
            app.key("a")
        deliver.assert_not_called()
        self.assertFalse(chief.load_task(self.task["id"]).get("allowed"))
        self.assert_red("tests/fast.py", "tests/later.py")

    def test_inbox_feedback_still_sends_when_the_displayed_aggregate_has_changed(self):
        self.write("tests/fast.py", "# first deliverable\n")
        item, _ = self.proposal(["tests/fast.py"])
        app = self.inbox(item)
        self.write("tests/later.py", "# another deliverable\n")
        self.sync()
        with self.worker() as deliver:
            self.submit(app, "r", "Please remove the second deliverable")
        deliver.assert_called_once()
        self.assertIn("Please remove the second deliverable", deliver.call_args[0][2])
        self.assertFalse(chief.load_task(self.task["id"]).get("allowed"))
        self.assert_red("tests/fast.py", "tests/later.py")

    def test_general_addition_record_cannot_hide_new_test_review(self):
        path = "tests/test_new.py"
        self.write(path, "# new acceptance coverage\n")
        task = dict(self.task, allowed=[{"path": path, "kind": "addition"}])
        item = self.item(task)
        self.assertIn(path, item["scope"]["protected_added_tests"])
        self.assertNotIn(path, item["scope"]["protected_violations"])

    def test_invalidated_state_approval_blocks_act_even_with_an_existing_passing_review(self):
        path = "config/existing.json"
        self.write(path, "approved version\n")
        _, decision = self.proposal([path], CHANGES)
        self.accept(decision)
        self.write(path, "unapproved later version\n")
        item = self.assert_red(path)
        item["task"]["review"] = {"head": item["git"]["head"], "verdict": "pass"}
        for passing in (True, False):
            with self.subTest(passing=passing):
                item["checks"] = {"passed": int(passing), "total": 1,
                                  "results": [{"cmd": "check", "ok": passing, "code": int(not passing), "tail": "failure"}]}
                with self.worker() as deliver, mock.patch.object(chief, "review_task") as review:
                    self.assertIsNone(chief.act(self.cfg, item, item["task"]))
                deliver.assert_not_called()
                review.assert_not_called()
