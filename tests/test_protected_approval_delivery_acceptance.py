"""Protected permission is durable independently of its worker notification.

Exercise CLI/inbox acceptance, real Git validation, real storage and real tick
passes. External AoE/transcript/screen/delivery I/O is simulated. Readiness is
also stubbed so legacy wait_ready calls cannot sleep for twenty seconds; the
same cases expose real status/screen observations for nonblocking probes.
No notification storage schema or new implementation API is prescribed.
"""
import copy
from contextlib import contextmanager, ExitStack
import json
from pathlib import Path
from unittest import mock

import chief
from approval_reload_support import load_source
from test_protected_path_approval_acceptance import ProtectedApprovalFixture, ADDITIONS, CHANGES
from test_test_approval_ux_acceptance import ApprovalFixture


class InterruptedDelivery(BaseException):
    """Model process termination after transport starts but before confirmation."""


class DurableProtectedApprovalFixture(ProtectedApprovalFixture):
    def setUp(self):
        super().setUp()
        # Explicit human notices must be drained even in observe mode; disable
        # unrelated automation so every transport call is an approval notice.
        self.task["autonomy"] = "observe"
        self.cfg["aoe_colors"] = False
        chief.save_cfg(self.cfg)
        chief.save_task(self.task)

    def prepare(self, kind):
        self.git("checkout", "HEAD", "--", "config/existing.json", "tests/helpers/existing.py")
        for name in ("tests/deliverable.py", "config/later.json"):
            path = self.repo / name
            if path.exists():
                path.unlink()
        chief.save_task(copy.deepcopy(self.task))
        chief.save_decisions(self.task["id"], [])
        self.path = "tests/deliverable.py" if kind == "addition" else "config/existing.json"
        self.write(self.path, "# displayed protected change\n")
        item, decision = self.proposal([self.path], ADDITIONS if kind == "addition" else CHANGES)
        return item, copy.deepcopy(decision)

    @contextmanager
    def worker_state(self, state="ready", ch=chief, outcome=True, before_probe=None, stub_readiness=True):
        sid = self.task["session_id"]
        status = {"ready": "idle", "busy": "running", "stopped": "stopped",
                  "missing": "missing", "unavailable": "unknown", "trust": "waiting"}[state]
        record = {"id": sid, "project_path": str(self.repo), "status": status, "tool": "codex"}

        def records(cfg):
            if before_probe:
                before_probe()
            if state == "unavailable":
                raise RuntimeError("AoE records unavailable")
            return {} if state == "missing" else {sid: record}

        def aoe(cfg, *args):
            if args[:3] == ("session", "show", "--json"):
                return json.dumps({"status": status})
            raise AssertionError("unexpected worker action: " + repr(args))

        screen = ("Do you trust the files in this folder?\n1. Yes\n2. No" if state == "trust" else
                  "Ask Codex to do anything" if state in ("ready", "busy") else "")
        with ExitStack() as stack:
            probes = stack.enter_context(mock.patch.object(ch, "aoe_records", side_effect=records))
            stack.enter_context(mock.patch.object(ch, "listings", return_value=(
                [] if state == "missing" else [{"id": sid, "state": "live"}])))
            stack.enter_context(mock.patch.object(ch, "aoe", side_effect=aoe))
            stack.enter_context(mock.patch.object(ch, "screen", return_value=screen))
            stack.enter_context(mock.patch.object(ch, "transcript", return_value=self.log))
            readiness = (stack.enter_context(mock.patch.object(ch, "wait_ready", return_value=(
                "ready" if state == "ready" else "trust" if state == "trust" else "timeout")))
                if stub_readiness else None)
            delivery = stack.enter_context(mock.patch.object(ch, "deliver", **(
                {"side_effect": outcome} if isinstance(outcome, BaseException) or callable(outcome)
                else {"return_value": outcome})))
            yield delivery, probes, readiness

    def accept_action(self, action, item, decision):
        if action == "cli":
            code, out, err = self.cli("decide", decision["id"], "--accept")
            self.assertEqual(code, 0, "granting validated permission must succeed: " + err)
            return out
        app = self.inbox(item)
        try:
            app.key("a")
        except (RuntimeError, OSError) as exc:
            self.fail("validated permission must succeed independently of worker delivery: " + str(exc))
        return app.st["msg"]

    def assert_granted(self, kind, decision, ch=chief):
        task = ch.load_task(self.task["id"])
        allowed = [a for a in task.get("allowed", []) if a.get("path") == self.path]
        self.assertEqual(len(allowed), 1, "permission must survive independent storage reloads")
        self.assertEqual(allowed[0]["kind"], kind)
        if kind == "state":
            self.assertEqual(allowed[0]["hash"], decision["snapshot"][self.path]["hash"])
            self.assertEqual(allowed[0]["mode"], decision["snapshot"][self.path]["mode"])
        resolved = ch.find_decision(decision["id"])
        self.assertEqual(resolved.get("resolution"), "accepted")
        self.assertNotEqual(resolved["state"], "pending", "permission resolution is independent of delivery")
        return resolved

    def assert_message(self, message, status):
        text = message.lower()
        self.assertRegex(text, r"approv|permission.*grant")
        if status == "delivered":
            self.assertRegex(text, r"\b(?:delivered|sent|confirmed)\b")
            self.assertNotIn("unconfirmed", text)
            self.assertNotIn("not confirmed", text)
            self.assertNotIn("queued", text)
        elif status == "queued":
            self.assertIn("queued", text)
            self.assertNotIn("unconfirmed", text)
            self.assertNotIn("not confirmed", text)
        else:
            self.assertRegex(text, r"\b(?:unconfirmed|not confirmed)\b")
            self.assertNotIn("queued", text, "an attempted send must never look retryable")

    def restarted(self):
        return load_source(Path(chief.__file__).resolve(), "chief_delivery_restart", self.home)


class ProtectedPermissionPersistenceTests(DurableProtectedApprovalFixture):
    def test_immediate_cli_and_inbox_approvals_report_delivery_for_both_permission_kinds(self):
        for kind in ("addition", "state"):
            for action in ("cli", "inbox"):
                with self.subTest(kind=kind, action=action):
                    item, decision = self.prepare(kind)
                    with self.worker_state() as (deliver, _, _):
                        message = self.accept_action(action, item, decision)
                    deliver.assert_called_once()
                    self.assertEqual(deliver.call_args[0][1], decision["session"])
                    self.assertIn(self.path, deliver.call_args[0][2])
                    self.assertIn("All other protected changes still require explicit approval", deliver.call_args[0][2])
                    self.assert_granted(kind, decision)
                    self.assert_clear(self.path)
                    self.assert_message(message, "delivered")

    def test_permission_and_resolution_are_durable_before_first_worker_probe_and_delivery(self):
        for kind in ("addition", "state"):
            with self.subTest(kind=kind):
                _, decision = self.prepare(kind)
                observed = []

                def inspect():
                    self.assert_granted(kind, decision)
                    observed.append("probe")

                def send(*args):
                    self.assert_granted(kind, decision)
                    observed.append("delivery")
                    return True

                with self.worker_state(before_probe=inspect, outcome=send) as (deliver, _, _):
                    chief.accept_decision(self.cfg, decision)
                deliver.assert_called_once()
                self.assertTrue(observed)
                self.assertEqual(observed[-1], "delivery")

    def test_cli_and_inbox_grant_and_queue_when_worker_cannot_take_input_now(self):
        for kind in ("addition", "state"):
            for action in ("cli", "inbox"):
                for state in ("busy", "stopped", "missing", "unavailable", "trust"):
                    with self.subTest(kind=kind, action=action, worker=state):
                        item, decision = self.prepare(kind)
                        with self.worker_state(state) as (deliver, _, _):
                            message = self.accept_action(action, item, decision)
                        deliver.assert_not_called()
                        self.assert_granted(kind, decision)
                        self.assert_clear(self.path)
                        self.assert_message(message, "queued")

    def test_new_transcript_turn_does_not_invalidate_protected_permission(self):
        for kind in ("addition", "state"):
            with self.subTest(kind=kind):
                item, decision = self.prepare(kind)
                self.log = dict(self.log, last_ts="2035-01-01T00:00:00+00:00", turn="open")
                with self.worker_state("busy"):
                    self.assert_message(self.accept_action("cli", item, decision), "queued")
                self.assert_granted(kind, decision)

    def test_unready_worker_is_queued_without_polling_or_waiting_for_a_prompt(self):
        for state in ("busy", "stopped", "trust"):
            with self.subTest(worker=state):
                item, decision = self.prepare("addition")
                with self.worker_state(state, stub_readiness=False) as (deliver, _, _), \
                        mock.patch.object(chief.time, "sleep", side_effect=AssertionError(
                            "protected approval must queue now rather than wait for a prompt")) as sleep:
                    self.assert_message(self.accept_action("cli", item, decision), "queued")
                sleep.assert_not_called()
                deliver.assert_not_called()
                self.assert_granted("addition", decision)

    def test_prompt_inspection_failure_queues_notice_without_losing_permission(self):
        item, decision = self.prepare("state")
        with self.worker_state(stub_readiness=False) as (deliver, _, _), \
                mock.patch.object(chief, "screen", side_effect=RuntimeError("screen unavailable")):
            self.assert_message(self.accept_action("cli", item, decision), "queued")
        deliver.assert_not_called()
        self.assert_granted("state", decision)


class ProtectedNotificationQueueTests(DurableProtectedApprovalFixture):
    def queue(self, kind, state="busy"):
        item, decision = self.prepare(kind)
        with self.worker_state(state) as (deliver, _, _):
            self.accept_action("cli", item, decision)
        deliver.assert_not_called()
        self.assert_granted(kind, decision)
        return decision

    def test_deferred_notice_survives_reload_unready_passes_then_is_delivered_only_once(self):
        for kind in ("addition", "state"):
            with self.subTest(kind=kind):
                decision = self.queue(kind)
                original = self.assert_granted(kind, decision)
                for state in ("busy", "stopped", "missing"):
                    ch = self.restarted()
                    with self.worker_state(state, ch=ch) as (deliver, _, _):
                        ch.tick(ch.config())
                        ch.tick(ch.config())
                    deliver.assert_not_called()
                    self.assert_granted(kind, decision, ch)
                ch = self.restarted()
                with self.worker_state(ch=ch) as (deliver, _, _):
                    ch.tick(ch.config())
                    ch.tick(ch.config())
                    ch.tick(ch.config())
                deliver.assert_called_once()
                self.assertEqual(deliver.call_args[0][1], decision["session"])
                self.assertIn(self.path, deliver.call_args[0][2])
                if kind == "state":
                    self.assertIn(decision["snapshot"][self.path]["hash"][:10], deliver.call_args[0][2])
                resolved = self.assert_granted(kind, decision, ch)
                self.assertEqual(resolved["resolved_at"], original["resolved_at"])
                self.assertIs(resolved["delivered"], True)
                again = self.restarted()
                with self.worker_state(ch=again) as (deliver, _, _):
                    again.tick(again.config())
                deliver.assert_not_called()

    def test_immediate_delivery_is_not_repeated_by_ticks_or_fresh_processes(self):
        for kind in ("addition", "state"):
            with self.subTest(kind=kind):
                _, decision = self.prepare(kind)
                with self.worker_state() as (deliver, _, _):
                    self.assertTrue(chief.accept_decision(self.cfg, decision))
                deliver.assert_called_once()
                for _ in range(2):
                    ch = self.restarted()
                    with self.worker_state(ch=ch) as (deliver, _, _):
                        ch.tick(ch.config())
                        ch.tick(ch.config())
                    deliver.assert_not_called()
                    self.assert_granted(kind, decision, ch)

    def test_dry_tick_leaves_notice_queued_for_the_next_real_tick(self):
        decision = self.queue("addition", state="missing")
        ch = self.restarted()
        with self.worker_state(ch=ch) as (deliver, _, _):
            ch.tick(ch.config(), dry=True)
            deliver.assert_not_called()
            ch.tick(ch.config())
            deliver.assert_called_once()
        self.assert_granted("addition", decision, ch)

    def test_delivery_after_recovery_from_unavailable_worker_is_not_lost(self):
        decision = self.queue("state", state="unavailable")
        ch = self.restarted()
        with self.worker_state(ch=ch) as (deliver, _, _):
            ch.tick(ch.config())
            ch.tick(ch.config())
        deliver.assert_called_once()
        self.assert_granted("state", decision, ch)

    def test_later_edits_do_not_expand_queued_state_permission_or_replace_approved_hash(self):
        decision = self.queue("state")
        approved_hash = decision["snapshot"][self.path]["hash"]
        self.write(self.path, "# later unapproved version\n")
        ch = self.restarted()
        with self.worker_state(ch=ch) as (deliver, _, _):
            ch.tick(ch.config())
            ch.tick(ch.config())
        deliver.assert_called_once()
        self.assertIn(approved_hash[:10], deliver.call_args[0][2])
        self.assert_granted("state", decision, ch)
        self.assert_red(self.path)
        pending = [d for d in ch.load_decisions(self.task["id"]) if d["state"] == "pending"]
        self.assertEqual(len(pending), 1)
        self.assertNotEqual(pending[0]["id"], decision["id"])

    def test_queued_notice_is_not_sent_to_a_replacement_worker(self):
        decision = self.queue("addition")
        replacement = "fedcba9876543210"
        chief.save_task(dict(chief.load_task(self.task["id"]), session_id=replacement))
        self.task["session_id"] = replacement
        ch = self.restarted()
        with self.worker_state(ch=ch) as (deliver, _, _):
            ch.tick(ch.config())
        self.assertFalse(any(call[0][1] == replacement for call in deliver.call_args_list))
        self.assert_granted("addition", decision, ch)


class ProtectedNotificationFailureTests(DurableProtectedApprovalFixture):
    def test_immediate_false_or_exception_keeps_grant_and_reports_unconfirmed_without_retry(self):
        for kind in ("addition", "state"):
            for action in ("cli", "inbox"):
                for outcome in (False, RuntimeError("send failed"), OSError("transport disconnected")):
                    with self.subTest(kind=kind, action=action, outcome=repr(outcome)):
                        item, decision = self.prepare(kind)
                        with self.worker_state(outcome=outcome) as (deliver, _, _):
                            message = self.accept_action(action, item, decision)
                        deliver.assert_called_once()
                        self.assert_granted(kind, decision)
                        self.assert_message(message, "unconfirmed")
                        ch = self.restarted()
                        with self.worker_state(ch=ch) as (retry, _, _):
                            ch.tick(ch.config())
                            ch.tick(ch.config())
                        retry.assert_not_called()

    def test_interruption_during_immediate_send_keeps_resolution_and_prevents_restart_resend(self):
        for kind in ("addition", "state"):
            with self.subTest(kind=kind):
                _, decision = self.prepare(kind)
                with self.worker_state(outcome=InterruptedDelivery()) as (deliver, _, _):
                    with self.assertRaises(InterruptedDelivery):
                        chief.accept_decision(self.cfg, decision)
                deliver.assert_called_once()
                ch = self.restarted()
                self.assert_granted(kind, decision, ch)
                with self.worker_state(ch=ch) as (retry, _, _):
                    ch.tick(ch.config())
                    ch.tick(ch.config())
                retry.assert_not_called()

    def test_failed_or_interrupted_deferred_send_is_never_blindly_retried(self):
        for kind in ("addition", "state"):
            for outcome in (False, RuntimeError("send failed"), InterruptedDelivery()):
                with self.subTest(kind=kind, outcome=repr(outcome)):
                    item, decision = self.prepare(kind)
                    with self.worker_state("stopped"):
                        self.accept_action("cli", item, decision)
                    ch = self.restarted()
                    with self.worker_state(ch=ch, outcome=outcome) as (deliver, _, _):
                        if isinstance(outcome, InterruptedDelivery):
                            with self.assertRaises(InterruptedDelivery):
                                ch.tick(ch.config())
                        else:
                            ch.tick(ch.config())
                    deliver.assert_called_once()
                    again = self.restarted()
                    self.assert_granted(kind, decision, again)
                    with self.worker_state(ch=again) as (retry, _, _):
                        again.tick(again.config())
                        again.tick(again.config())
                    retry.assert_not_called()


class ProtectedApprovalValidationOrderTests(DurableProtectedApprovalFixture):
    def test_invalid_approval_is_rejected_before_worker_io_and_without_permission(self):
        for kind in ("addition", "state"):
            cases = ["not-pending", "missing-decision", "changed-display", "worker-changed",
                     "test-phase", "inspection-failed", "unnormalized-path"]
            if kind == "state":
                cases += ["changed-snapshot", "divergent-index"]
            for case in cases:
                with self.subTest(kind=kind, invalid=case), ExitStack() as stack:
                    _, decision = self.prepare(kind)
                    if case == "not-pending":
                        chief.dismiss_decision(copy.deepcopy(decision))
                    elif case == "missing-decision":
                        chief.save_decisions(self.task["id"], [])
                    elif case == "changed-display":
                        self.write("config/later.json", "another protected addition\n")
                        if kind == "state":
                            self.write("tests/helpers/existing.py", "# another protected state\n")
                        self.sync()
                    elif case == "worker-changed":
                        chief.save_task(dict(self.task, session_id="fedcba9876543210"))
                    elif case == "test-phase":
                        chief.save_task(dict(self.task, phase="test", test_first=True))
                    elif case == "inspection-failed":
                        stack.enter_context(mock.patch.object(chief, "scope_info", return_value=None))
                    elif case == "unnormalized-path":
                        decision["files"] = ["config/../" + self.path]
                        chief.update_decision(decision)
                    elif case == "changed-snapshot":
                        self.write(self.path, "# state changed after display\n")
                    elif case == "divergent-index":
                        self.git("add", self.path)
                        self.write(self.path, "# unstaged different version\n")
                    with self.worker_state() as (deliver, probes, ready):
                        with self.assertRaises((RuntimeError, ValueError)):
                            chief.accept_decision(self.cfg, decision)
                    self.assertFalse(chief.load_task(self.task["id"]).get("allowed"))
                    deliver.assert_not_called()
                    probes.assert_not_called()
                    ready.assert_not_called()

    def test_accepting_the_same_displayed_decision_twice_cannot_repeat_delivery(self):
        _, decision = self.prepare("addition")
        stale = copy.deepcopy(decision)
        with self.worker_state() as (deliver, _, _):
            chief.accept_decision(self.cfg, decision)
            with self.assertRaises((RuntimeError, ValueError)):
                chief.accept_decision(self.cfg, stale)
        deliver.assert_called_once()
        self.assert_granted("addition", decision)

    def test_reply_and_dismiss_keep_their_existing_semantics(self):
        for kind in ("addition", "state"):
            for action in ("reply", "dismiss"):
                with self.subTest(kind=kind, action=action):
                    _, decision = self.prepare(kind)
                    with self.worker_state() as (deliver, _, _):
                        if action == "reply":
                            self.assertTrue(chief.reply_decision(self.cfg, decision, "approved"))
                            deliver.assert_called_once()
                            self.assertIn("feedback only", deliver.call_args[0][2])
                        else:
                            chief.dismiss_decision(decision, "not authorized")
                            deliver.assert_not_called()
                    self.assertFalse(chief.load_task(self.task["id"]).get("allowed"))
                    self.assert_red(self.path)
                    self.assertNotEqual(chief.find_decision(decision["id"]).get("resolution"), "accepted")


class OtherDecisionReadinessRegressionTests(ApprovalFixture):
    def test_ordinary_and_test_approvals_still_require_worker_readiness(self):
        for kind in ("ordinary", "test-approval"):
            for readiness in ("stopped", "timeout", "trust", "missing"):
                with self.subTest(kind=kind, readiness=readiness):
                    self.reset_waiting()
                    decision = copy.deepcopy(self.decision)
                    if kind == "ordinary":
                        decision.update(kind=kind, question="Use JSON?", draft="Decision: use JSON")
                        chief.update_decision(decision)
                    with self.ready_worker(readiness=readiness) as deliver, ExitStack() as stack:
                        if readiness == "missing":
                            stack.enter_context(mock.patch.object(chief, "aoe_records", return_value={}))
                        with self.assertRaises(RuntimeError):
                            chief.accept_decision(self.cfg, decision)
                    deliver.assert_not_called()
                    self.assert_test_contract()
                    self.assertEqual(chief.find_decision(decision["id"])["state"], "pending")

    def test_ordinary_and_test_approvals_still_reject_stale_worker_turns(self):
        for kind in ("ordinary", "test-approval"):
            with self.subTest(kind=kind):
                self.reset_waiting()
                decision = copy.deepcopy(self.decision)
                if kind == "ordinary":
                    decision.update(kind=kind, question="Use JSON?", draft="Decision: use JSON")
                    chief.update_decision(decision)
                with self.ready_worker(turn=dict(self.log, last_ts="a-new-turn")) as deliver:
                    with self.assertRaises(RuntimeError):
                        chief.accept_decision(self.cfg, decision)
                deliver.assert_not_called()
                self.assert_test_contract()
                self.assertEqual(chief.find_decision(decision["id"])["state"], "superseded")
