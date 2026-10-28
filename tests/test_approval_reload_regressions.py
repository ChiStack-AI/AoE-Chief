"""Build regressions for concurrent delivery and stale-inbox rendering.

The approved acceptance files remain unchanged. Exercise two independent Chief
instances sharing disk state, rather than relying on an in-memory send flag.
"""
from contextlib import ExitStack
import threading
from unittest import mock

import chief
import chief_tui
from test_protected_approval_delivery_acceptance import DurableProtectedApprovalFixture
from test_chief_source_updates_acceptance import SourceUpdateFixture
from test_tui import items


class ConcurrentNoticeRegressions(DurableProtectedApprovalFixture):
    def test_missing_worker_still_exposes_protected_approval_to_the_real_inbox(self):
        for kind in ("addition", "state"):
            with self.subTest(kind=kind):
                _, decision = self.prepare(kind)
                with self.worker_state("missing") as (deliver, _, _):
                    result = chief.collect(self.cfg, checks=False, persist=False)
                    item = result["items"][0]
                    self.assertEqual(item.get("decision", {}).get("id"), decision["id"])
                    app = self.inbox(item)
                    self.assertIn("a approve paths", chief_tui.keys_for(chief, app.selected()))
                    app.key("a")
                deliver.assert_not_called()
                self.assert_granted(kind, decision)
                self.assert_message(app.st["msg"], "queued")

    def test_missing_worker_refresh_rechecks_the_displayed_protected_state(self):
        _, decision = self.prepare("state")
        old_hash = decision["snapshot"][self.path]["hash"]
        self.write(self.path, "# changed after worker disappeared\n")
        with self.worker_state("missing") as (deliver, _, _):
            item = chief.collect(self.cfg, checks=False, persist=False)["items"][0]
            current = item.get("decision")
            self.assertIsNotNone(current)
            self.assertNotEqual(current["snapshot"][self.path]["hash"], old_hash)
            self.assert_message(self.accept_action("inbox", item, current), "queued")
        deliver.assert_not_called()
        self.assert_granted("state", current)

    def test_archived_or_trashed_record_does_not_receive_input_despite_cached_idle_status(self):
        for state in ("archived", "trashed", "deleted"):
            with self.subTest(state=state):
                item, decision = self.prepare("addition")
                record = {"id": decision["session"], "status": "idle", "state": state}
                with self.worker_state() as (deliver, _, _), \
                        mock.patch.object(chief, "aoe_records", return_value={decision["session"]: record}):
                    message = self.accept_action("cli", item, decision)
                deliver.assert_not_called()
                self.assert_granted("addition", decision)
                self.assert_message(message, "queued")

    def test_acceptance_racing_a_tick_attempts_the_notice_once_even_when_unconfirmed(self):
        _, decision = self.prepare("addition")
        accepting, ticking = self.restarted(), self.restarted()
        probing = threading.Event()
        barrier = threading.Barrier(2, timeout=5)
        errors = []

        def ready(*args, **kwargs):
            probing.set()
            barrier.wait()
            return "ready"

        def run(fn):
            try:
                fn()
            except BaseException as exc:
                errors.append(exc)

        with self.worker_state(ch=accepting, outcome=False) as (first, _, _), \
                self.worker_state(ch=ticking, outcome=False) as (second, _, _), ExitStack() as stack:
            stack.enter_context(mock.patch.object(accepting, "wait_ready", side_effect=ready))
            stack.enter_context(mock.patch.object(ticking, "wait_ready", side_effect=ready))
            # Isolate notice transport from unrelated board actions. Tick's
            # actual durable drain and process lock still run.
            stack.enter_context(mock.patch.object(ticking, "collect", return_value={"items": []}))
            workers = [threading.Thread(target=run, args=(fn,), daemon=True) for fn in (
                lambda: accepting.accept_decision(accepting.config(), decision),
                lambda: ticking.tick(ticking.config()))]
            workers[0].start()
            self.assertTrue(probing.wait(5), "acceptance must persist consent before probing")
            workers[1].start()
            for worker in workers:
                worker.join(6)
                self.assertFalse(worker.is_alive(), "both contenders must finish")
            self.assertEqual(errors, [])
            self.assertEqual(first.call_count + second.call_count, 1)

        restarted = self.restarted()
        with self.worker_state(ch=restarted) as (retry, _, _):
            restarted.tick(restarted.config())
        retry.assert_not_called()
        self.assert_granted("addition", decision, restarted)


class SourceUpdateRenderingRegressions(SourceUpdateFixture):
    def test_restart_notice_keeps_header_footer_and_dimensions_on_small_screens(self):
        app = self.tui.Inbox(self.ch.config(), self.ch, 15)
        notice = "Chief was updated; restart this inbox: aoe session stop/start 1122334455667788"
        app.st["source_notice"] = notice
        for entries in ([], self.tui.entries(self.ch, {"items": items()})):
            app.st["entries"] = entries
            for width, height in ((200, 30), (100, 16), (60, 10), (40, 6)):
                with self.subTest(entries=len(entries), width=width, height=height):
                    rows = self.tui.render(self.ch, app.st, width, height)
                    self.assertEqual(len(rows), height)
                    for row in rows:
                        self.assertEqual(self.tui.width("".join(s for s, _ in row)), width)
                    self.assertTrue(any("Chief was updated" in s for row in rows[:3] for s, _ in row))
                    self.assertTrue(any(style == "footer" for _, style in rows[-1]))

    def test_restart_notice_survives_a_failed_board_refresh(self):
        app = self.tui.Inbox(self.ch.config(), self.ch, 15)
        self.change_content(self.tui_path)
        with mock.patch.object(self.ch, "collect", side_effect=RuntimeError("AoE unavailable")):
            with self.assertRaises(RuntimeError):
                app._refresh()
        header = "\n".join("".join(s for s, _ in row)
                           for row in self.tui.render(self.ch, app.st, 200, 30)[:3])
        self.assertIn("Chief was updated; restart this inbox: aoe session stop/start ", header)
