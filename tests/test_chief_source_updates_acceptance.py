"""Loaded-source hashes control watch re-exec and persistent inbox warnings.

Run actual modules copied into a temporary installation. Change real file
contents independently of size/mtime. Mock only tick, terminal/session I/O,
sleep and exec; never re-execute or edit the repository's running sources.
"""
from contextlib import contextmanager, ExitStack, redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
from documentation_support import read_documentation
import re
import shutil
import sys
from unittest import mock

import chief
import chief_tui
from approval_reload_support import load_source
from test_intent_task_flow import ChiefFixture


class Reexecuted(BaseException):
    """os.exec* has replaced the process; no further Python code may run."""


class SourceUpdateFixture(ChiefFixture):
    def setUp(self):
        super().setUp()
        self.install = self.root / "installed Chief with spaces"
        self.install.mkdir()
        self.chief_path = self.install / "chief.py"
        self.tui_path = self.install / "chief_tui.py"
        shutil.copyfile(chief.__file__, str(self.chief_path))
        shutil.copyfile(chief_tui.__file__, str(self.tui_path))
        self.original_argv = [str(self.chief_path), "watch", "--every", "7"]
        with mock.patch.object(sys, "argv", list(self.original_argv)):
            self.ch = load_source(self.chief_path, "chief_loaded_for_source_updates", self.home)
            self.tui = load_source(self.tui_path, "tui_loaded_for_source_updates", self.home)
        self.ch.config()

    def change_content(self, path):
        before = path.stat()
        source = path.read_bytes()
        # A valid, same-size edit. Restore nanosecond timestamps to rule out
        # mtime/size-based detection and bytecode-cache coincidences.
        self.assertIn(b"AoE Chief", source)
        changed = source.replace(b"AoE Chief", b"AoE CHIEF", 1)
        self.assertNotEqual(changed, source)
        path.write_bytes(changed)
        os.utime(str(path), ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(path.stat().st_size, before.st_size)
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)

    @contextmanager
    def capture_exec(self):
        calls = []

        def observed(kind):
            def execute(program, *args):
                if kind in ("execv", "execve", "execvp", "execvpe"):
                    argv = list(args[0])
                else:
                    argv = list(args[:-1] if kind in ("execle", "execlpe") else args)
                calls.append((program, argv))
                raise Reexecuted()
            return execute

        with ExitStack() as stack:
            for name in ("execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe"):
                stack.enter_context(mock.patch.object(os, name, side_effect=observed(name)))
            yield calls

    def run_watch(self, tick, sleep):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", list(self.original_argv)), \
                mock.patch.object(self.ch, "tick", side_effect=tick) as ticks, \
                mock.patch.object(self.ch.time, "sleep", side_effect=sleep), \
                redirect_stdout(out), redirect_stderr(err), self.capture_exec() as calls:
            try:
                code = self.ch.main()
            except Reexecuted:
                code = "reexecuted"
        return code, ticks, calls, out.getvalue() + err.getvalue()

    def assert_reexec(self, result, tick_count):
        code, ticks, calls, output = result
        self.assertEqual(code, "reexecuted", output)
        self.assertEqual(ticks.call_count, tick_count, "updates must be detected before the next pass starts")
        self.assertEqual(calls, [(sys.executable, [sys.executable] + self.original_argv)])
        notices = [line for line in output.splitlines()
                   if re.search(r"(?i)(?:updat|chang).*(?:re.exec|restart|reload)", line)]
        self.assertEqual(len(notices), 1, "log one clear update/re-exec line: " + output)

    def refresh(self, app, records=None):
        # Real refresh bookkeeping/rendering, with board/session I/O isolated.
        with mock.patch.object(self.ch, "collect", return_value={"items": []}), \
                mock.patch.object(self.ch, "aoe_records", return_value=records or {}):
            app._refresh()
        return ["".join(text for text, style in row)
                for row in self.tui.render(self.ch, app.st, 200, 30)]


class WatchSourceUpdateTests(SourceUpdateFixture):
    def test_update_since_process_load_reexecs_before_even_the_first_tick(self):
        self.change_content(self.chief_path)
        result = self.run_watch(lambda cfg: [], lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()))
        self.assert_reexec(result, tick_count=0)

    def test_update_during_a_pass_waits_until_it_finishes_then_reexecs_before_next_tick(self):
        events = []

        def tick(cfg):
            events.append("tick started")
            self.change_content(self.chief_path)
            events.append("tick completed")
            return ["completed current pass"]

        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) > 1:
                raise KeyboardInterrupt()

        result = self.run_watch(tick, sleep)
        self.assert_reexec(result, tick_count=1)
        self.assertEqual(events, ["tick started", "tick completed"])
        self.assertIn("completed current pass", result[3])

    def test_update_during_sleep_is_detected_at_the_next_iteration_boundary(self):
        def sleep(seconds):
            if self.chief_path.read_bytes() == original:
                self.change_content(self.chief_path)
            else:
                raise KeyboardInterrupt()

        original = self.chief_path.read_bytes()
        self.assert_reexec(self.run_watch(lambda cfg: [], sleep), tick_count=1)

    def test_unchanged_content_and_metadata_only_changes_do_not_reexec(self):
        for metadata_change in (False, True):
            with self.subTest(metadata_change=metadata_change):
                if metadata_change:
                    before = self.chief_path.stat()
                    os.utime(str(self.chief_path), ns=(before.st_atime_ns, before.st_mtime_ns + 1000000000))
                code, ticks, calls, _ = self.run_watch(
                    lambda cfg: [], lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()))
                self.assertEqual(code, 0)
                ticks.assert_called_once()
                self.assertEqual(calls, [])

    def test_inbox_source_only_does_not_reexec_watch(self):
        self.change_content(self.tui_path)
        code, ticks, calls, _ = self.run_watch(
            lambda cfg: [], lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()))
        self.assertEqual(code, 0)
        ticks.assert_called_once()
        self.assertEqual(calls, [])

    def test_single_tick_command_never_reexecs_during_a_pass(self):
        task = dict(id="source-pass", session_id="0123456789abcdef", state="running")
        self.ch.save_task(task)

        def collect(cfg, **kwargs):
            self.change_content(self.chief_path)
            return {"items": [{"id": task["session_id"], "task": task}]}

        with self.capture_exec() as calls, mock.patch.object(self.ch, "collect", side_effect=collect), \
                mock.patch.object(self.ch, "act", return_value="tick completed") as act, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(self.ch.main(["tick"]), 0)
        act.assert_called_once()
        self.assertEqual(calls, [])


class InboxSourceUpdateTests(SourceUpdateFixture):
    def test_either_loaded_source_change_shows_exact_restart_notice_in_header_without_restart(self):
        inbox_id = "1122334455667788"
        for path in (self.chief_path, self.tui_path):
            with self.subTest(source=path.name), mock.patch.dict(os.environ, {"AOE_SESSION_ID": inbox_id}):
                original = path.read_bytes()
                app = self.tui.Inbox(self.ch.config(), self.ch, 15)
                before = self.refresh(app)
                self.assertFalse(any("Chief was updated" in line for line in before))
                self.change_content(path)
                with self.capture_exec() as calls, \
                        mock.patch.object(self.ch, "aoe", side_effect=AssertionError("inbox must not restart itself")):
                    rows = self.refresh(app)
                    # This is persistent header state, independent of the action
                    # footer and its transient success/error messages.
                    expected = "Chief was updated; restart this inbox: aoe session stop/start " + inbox_id
                    self.assertIn(expected, "\n".join(rows[:3]))
                    app.st["msg"] = "an unrelated action completed"
                    self.assertIn(expected, "\n".join(self.refresh(app)[:3]))
                    self.assertEqual(calls, [])
                path.write_bytes(original)

    def test_first_refresh_compares_loaded_hashes_even_if_sources_changed_before_inbox_construction(self):
        for path in (self.chief_path, self.tui_path):
            with self.subTest(source=path.name):
                original = path.read_bytes()
                self.change_content(path)
                app = self.tui.Inbox(self.ch.config(), self.ch, 15)
                rows = self.refresh(app)
                self.assertIn("Chief was updated; restart this inbox: aoe session stop/start ", "\n".join(rows[:3]))
                path.write_bytes(original)

    def test_no_session_id_uses_an_explicit_placeholder(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            app = self.tui.Inbox(self.ch.config(), self.ch, 15)
            self.change_content(self.tui_path)
            rows = self.refresh(app)
        self.assertIn("Chief was updated; restart this inbox: aoe session stop/start <id>", "\n".join(rows[:3]))

    def test_inbox_session_id_can_be_resolved_from_current_aoe_tmux_session(self):
        inbox_id = "1122334455667788"
        worker_id = "0123456789abcdef"
        real_call = self.ch.call

        def call(args, **kwargs):
            if args[0] == "tmux":
                self.assertIn(args[1], ("display-message", "display"))
                return "aoe_main_" + inbox_id[:8] + "\n"
            return real_call(args, **kwargs)

        with mock.patch.dict(os.environ, {"TMUX": "/tmp/acceptance-tmux,1234,0", "TMUX_PANE": "%17"}):
            os.environ.pop("AOE_SESSION_ID", None)
            with mock.patch.object(self.ch, "call", side_effect=call):
                app = self.tui.Inbox(self.ch.config(), self.ch, 15)
                self.change_content(self.chief_path)
                records = {inbox_id: {"id": inbox_id, "tool": "chief"},
                           worker_id: {"id": worker_id, "tool": "codex"}}
                header = "\n".join(self.refresh(app, records=records)[:3])
        self.assertIn("Chief was updated; restart this inbox: aoe session stop/start " + inbox_id, header)
        self.assertNotIn("stop/start " + worker_id, header)

    def test_unchanged_content_and_metadata_changes_do_not_show_update_notice(self):
        app = self.tui.Inbox(self.ch.config(), self.ch, 15)
        self.assertFalse(any("Chief was updated" in line for line in self.refresh(app)))
        for path in (self.chief_path, self.tui_path):
            before = path.stat()
            os.utime(str(path), ns=(before.st_atime_ns, before.st_mtime_ns + 1000000000))
        self.assertFalse(any("Chief was updated" in line for line in self.refresh(app)))

    def test_freshly_loaded_process_does_not_warn_about_previously_installed_changes(self):
        self.change_content(self.chief_path)
        self.change_content(self.tui_path)
        self.ch = load_source(self.chief_path, "chief_after_real_restart", self.home)
        self.tui = load_source(self.tui_path, "tui_after_real_restart", self.home)
        app = self.tui.Inbox(self.ch.config(), self.ch, 15)
        self.assertFalse(any("Chief was updated" in line for line in self.refresh(app)))


class ApprovalAndUpdateDocumentationTests(ChiefFixture):
    def test_readme_explains_durable_permissions_queued_notices_and_uncertain_delivery(self):
        text = read_documentation()
        approval = text[text.index("Use Inbox **`a approve paths`"):text.index("Approved paths, approval types")]
        self.assertRegex(approval.lower(), r"(?:busy|stopped|missing|unavailable)")
        self.assertRegex(approval.lower(), r"queu[\s\S]*tick")
        self.assertRegex(approval.lower(), r"(?:unconfirmed|not confirmed|uncertain)[\s\S]*(?:retry|resend|re-send)")

    def test_readme_explains_watch_boundary_reload_and_manual_inbox_restart(self):
        text = read_documentation()
        updates = text[text.index("**Updating Chief:"):text.index("**Changing `custom_agents`:")]
        self.assertRegex(updates.lower(), r"watch[\s\S]*(?:hash|content)")
        self.assertRegex(updates.lower(), r"(?:before|between|next)[\s\S]*(?:pass|iteration|tick)")
        self.assertIn("Chief was updated; restart this inbox: aoe session stop/start", updates)
        self.assertIn("chief_tui.py", updates)
        self.assertRegex(updates.lower(), r"inbox[\s\S]*(?:manually|manual|stop.*start)")
