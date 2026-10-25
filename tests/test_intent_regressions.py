"""Additional regression coverage; approved acceptance-test files stay untouched."""
import json
from pathlib import Path
from unittest import mock

import chief
import chief_tui as tui
import test_intent_task_flow as flow


class IntentInteractionTests(flow.ChiefFixture):
    def test_abort_creates_no_task_or_worker(self):
        with mock.patch.object(chief, "generate_ai", side_effect=RuntimeError("offline")), \
                mock.patch("builtins.input", return_value="n"), \
                mock.patch.object(chief, "start_task") as start:
            code, _, err = self.cli("new", "Add CSV import", cwd=self.repo)
        self.assertEqual(code, 0, err)
        self.assertEqual(chief.all_tasks(), [])
        start.assert_not_called()

    def test_enter_accepts_ai_refinement_and_retains_test_protection(self):
        self.write("tests/test_existing.py", "def test_existing(): pass\n")
        answer = json.dumps({"name": "CSV import", "protected": [], "checks": ["python3 -m pytest -q"]})
        with mock.patch.object(chief, "generate_ai", return_value=answer), \
                mock.patch("builtins.input", return_value="") as ask:
            code, out, err = self.cli("new", "Add CSV import", "--draft", cwd=self.repo)
        self.assertEqual(code, 0, err)
        ask.assert_called_once()
        task = chief.all_tasks()[0]
        self.assertEqual(task["name"], "CSV import")
        self.assertEqual(task["checks"], ["python3 -m pytest -q"])
        self.assertEqual(task["protected"], ["tests"])
        self.assertEqual(task["phase"], "test")
        self.assertIn("CSV import", out)

    def test_editor_changes_contract_before_confirmation(self):
        def edit(cmd):
            self.assertEqual(cmd[:-1], ["contract-editor", "--wait"])
            path = Path(cmd[-1])
            contract = json.loads(path.read_text())
            contract.update(goal="Accept CSV with quoted commas", checks=["make test"])
            path.write_text(json.dumps(contract))
            return 0

        with mock.patch.object(chief, "generate_ai", side_effect=RuntimeError("offline")), \
                mock.patch.dict("os.environ", {"EDITOR": "contract-editor --wait"}), \
                mock.patch("builtins.input", side_effect=["e", ""]), \
                mock.patch.object(chief.subprocess, "call", side_effect=edit) as editor:
            code, _, err = self.cli("new", "Add CSV import", "--draft", cwd=self.repo)
        self.assertEqual(code, 0, err)
        editor.assert_called_once()
        task = chief.all_tasks()[0]
        self.assertEqual(task["goal"], "Accept CSV with quoted commas")
        self.assertEqual(task["checks"], ["make test"])

    def test_flag_based_new_keeps_its_contract_and_never_prompts_or_calls_ai(self):
        for skip in (False, True):
            with self.subTest(no_test_first=skip):
                args = ["new", "--repo", str(self.repo), "--name", "CSV import", "--goal", "Import CSV",
                        "--check", "custom-test-command", "--protected", "custom-tests", "--draft"]
                if skip:
                    args.append("--no-test-first")
                with mock.patch.object(chief, "generate_ai") as generate, mock.patch("builtins.input") as ask:
                    code, _, err = self.cli(*args)
                self.assertEqual(code, 0, err)
                generate.assert_not_called()
                ask.assert_not_called()
                task = chief.all_tasks()[-1]
                self.assertEqual(task["checks"], ["custom-test-command"])
                self.assertEqual(task["protected"], ["custom-tests"])
                self.assertEqual(task["test_first"], not skip)
                self.assertEqual(task["phase"], "build" if skip else "test")

    def test_invalid_ai_contract_falls_back_without_losing_defaults(self):
        self.write("tests/test_existing.py", "def test_existing(): pass\n")
        for answer in ("not JSON", "[]", '{"checks": "pytest"}', '{"budget": {"rounds": -1}}'):
            with self.subTest(answer=answer), mock.patch.object(chief, "generate_ai", return_value=answer):
                code, _, err = self.cli("new", "Add CSV import", "--yes", "--draft", cwd=self.repo)
                self.assertEqual(code, 0, err)
                task = chief.all_tasks()[-1]
                self.assertEqual(task["checks"], ["python3 -m pytest"])
                self.assertEqual(task["protected"], ["tests"])

    def test_repo_default_also_works_for_flag_based_creation(self):
        with mock.patch.object(chief, "generate_ai") as generate, mock.patch("builtins.input") as ask:
            code, _, err = self.cli("new", "--name", "CSV", "--goal", "Import CSV", "--draft", cwd=self.repo)
        self.assertEqual(code, 0, err)
        self.assertEqual(chief.all_tasks()[0]["repo"], str(self.repo))
        generate.assert_not_called()
        ask.assert_not_called()


class ApprovalInteractionTests(flow.ChiefFixture):
    def setUp(self):
        super().setUp()
        self.git("commit", "-q", "--allow-empty", "-m", "base")
        base = self.git("rev-parse", "HEAD")
        self.file = "tests/test_acceptance.py"
        self.write(self.file, "# acceptance tests\n")
        self.git("add", "tests")
        self.git("commit", "-q", "-m", "acceptance tests")
        self.task = {"id": "demo", "name": "demo", "goal": "Import CSV", "repo": str(self.repo),
                     "worktree": str(self.repo), "session_id": "0123456789abcdef", "base": base,
                     "phase": "test", "test_first": True, "state": "needs-you", "protected": []}
        chief.save_task(self.task)
        self.log = {"turn": "done", "last_ts": chief.now_utc().isoformat(),
                    "footer": {"decision": "approve the acceptance tests " + self.file}}
        self.item = {"id": self.task["session_id"], "task": self.task, "status": "idle", "log": self.log}
        self.item["signals"] = chief.judge(self.item)
        self.decision = chief.sync_decisions(self.cfg, self.task, self.item)
        self.item["decision"] = self.decision
        self.item["signals"] = chief.decision_signals(self.item["signals"], self.decision, self.log["footer"])

    def test_stale_decision_cannot_change_phase_or_test_protection(self):
        with mock.patch.object(chief, "aoe_records", return_value={self.task["session_id"]: {"id": "worker"}}), \
                mock.patch.object(chief, "transcript", return_value={"last_ts": "newer-turn"}), \
                mock.patch.object(chief, "approve_tests") as approve, \
                mock.patch.object(chief, "deliver") as deliver:
            code, _, _ = self.cli("decide", self.decision["id"], "--accept")
        self.assertEqual(code, 1)
        approve.assert_not_called()
        deliver.assert_not_called()
        self.assertEqual(chief.load_task("demo"), self.task)
        self.assertEqual(chief.find_decision(self.decision["id"])["state"], "superseded")

    def test_uncommitted_test_edits_cannot_be_frozen_as_approved(self):
        self.write(self.file, "# uncommitted revision\n")
        with mock.patch.object(chief, "aoe_records", return_value={self.task["session_id"]: {"id": "worker"}}), \
                mock.patch.object(chief, "transcript", return_value=self.log), \
                mock.patch.object(chief, "wait_ready", return_value="ready"), \
                mock.patch.object(chief, "deliver") as deliver:
            code, _, _ = self.cli("decide", self.decision["id"], "--accept")
        self.assertEqual(code, 1)
        deliver.assert_not_called()
        self.assertEqual(chief.load_task("demo"), self.task)
        self.assertEqual(chief.find_decision(self.decision["id"])["state"], "pending")

    def test_inbox_accepts_big_undrafted_test_approval(self):
        entry = tui.entry(chief, self.item)
        self.assertEqual(entry["level"], "red")
        self.assertFalse(entry["draft"])
        self.assertIn("a accept", tui.keys_for(chief, entry))
        app = tui.Inbox(self.cfg, chief, 15)
        app.st["entries"] = [entry]
        with mock.patch.object(app, "job", side_effect=lambda label, fn: fn()), \
                mock.patch.object(chief, "accept_decision", return_value=True) as accept:
            app.key("a")
        accept.assert_called_once_with(self.cfg, self.decision)

    def test_decision_list_offers_manual_accept_for_undrafted_approval(self):
        code, out, err = self.cli("decisions", "--no-refresh")
        self.assertEqual(code, 0, err)
        self.assertIn("--accept", out)
        self.assertIn("big", out)
        self.assertNotIn("chief's choice:", out)

    def test_malformed_file_list_stays_big_pending_without_ai(self):
        log = dict(self.log, last_ts="another-turn", footer={"decision": 'approve the acceptance tests "unclosed'})
        with mock.patch.object(chief, "generate_ai") as generate:
            dec = chief.sync_decisions(dict(self.cfg, decision_ai=True), self.task,
                                       {"id": self.task["session_id"], "log": log, "signals": []})
        self.assertEqual(dec["size"], "big")
        self.assertEqual(dec["state"], "pending")
        self.assertNotIn("draft", dec)
        generate.assert_not_called()

    def test_approval_paths_support_quoted_spaces_and_commas(self):
        question = ('approve the acceptance tests `tests/test space.py`, "tests/test,comma.py". '
                    'Options: A) approve B) revise. Recommendation: A')
        self.assertEqual(chief.approval_files(question), ["tests/test space.py", "tests/test,comma.py"])
