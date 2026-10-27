"""Inbox latency contract: short active waits, quiet idle waits, real processes.

Scheduling assertions use the effective terminal timeout, without sleep-based
microbenchmarks. Process tests retain real subprocess I/O and tree cleanup.
Full-suite (<90s) and fast-subset (<20s) wall-clock gates are measured externally
so discovering this module never recursively launches either complete suite.
"""
import json
import os
from pathlib import Path
import queue
import signal
import sys
import tempfile
import time
import unittest
from unittest import mock

import chief
import chief_tui as tui


class EndTerminal(BaseException):
    """Stop the fake terminal without catching assertion failures."""


class ActivePollingAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.app = tui.Inbox({}, chief, 3600)
        self.app.last_refresh = time.time()
        self.timeout = None

    def loop(self, read):
        screen = mock.Mock()
        screen.getmaxyx.return_value = (24, 80)
        screen.timeout.side_effect = lambda delay: setattr(self, "timeout", delay)
        screen.nodelay.side_effect = lambda enabled: setattr(self, "timeout", 0 if enabled else -1)
        screen.get_wch.side_effect = read
        # Isolate scheduling from terminal rendering and external board refreshes.
        # The production event loop and completion-delivery methods remain real.
        with mock.patch.object(tui, "setup_colors", return_value={}), \
                mock.patch.object(tui.curses, "curs_set"), \
                mock.patch.object(tui, "draw"), \
                mock.patch.object(self.app, "refresh"), \
                mock.patch.object(self.app, "job", return_value=True), \
                mock.patch.object(self.app, "tick_note", return_value=""):
            with self.assertRaises(EndTerminal):
                self.app.loop(screen)

    def assert_active_wait(self):
        self.assertGreaterEqual(self.timeout, 1, "active work must not cause a terminal busy-spin")
        self.assertLessEqual(self.timeout, 50, "worker events must be polled within 50 ms, rather than 250 ms")

    def assert_idle_wait(self):
        self.assertGreaterEqual(self.timeout, 200, "an idle inbox should keep a quiet blocking wait")
        self.assertLessEqual(self.timeout, 1000, "idle refresh and keyboard input must remain usable")

    def test_command_completion_gets_short_active_wait_then_returns_to_idle(self):
        execution = tui.CommandExecution([], protocol=True)
        execution.turn = self.app.log_operation("command", "tasks")
        execution.turn.update(output="", running=True)
        self.app.execution = execution
        self.app.lock.acquire()
        self.app.st["busy"] = "running command"
        reads = []

        def read():
            if not reads:
                self.assert_active_wait()
                reads.append(True)
                # Arrive just after the UI starts waiting for input.
                execution.events.put(("stdout", "ready\n"))
                execution.events.put(("done", 0))
                raise tui.curses.error()
            self.assertIsNone(self.app.execution)
            self.assertFalse(self.app.lock.locked())
            self.assertEqual(execution.turn["output"], "ready\n")
            self.assertEqual(execution.turn["exit_status"], 0)
            self.assertFalse(execution.turn["running"])
            self.assert_idle_wait()
            raise EndTerminal()

        self.loop(read)

    def test_ai_completion_gets_short_active_wait_then_returns_to_idle(self):
        execution = tui.CommandExecution([], protocol=True, request=lambda: {})
        execution.turn = self.app.log_operation("ai", "Explain the board")
        execution.response, execution.ai_error = None, None
        self.app.ai_execution = execution
        self.app.lock.acquire()
        self.app.st.update(busy="thinking", ai_thinking=True)
        reads = []

        def read():
            if not reads:
                self.assert_active_wait()
                reads.append(True)
                execution.events.put(("answer", {"answer": "Ready", "suggestions": []}))
                execution.events.put(("done", 0))
                raise tui.curses.error()
            self.assertIsNone(self.app.ai_execution)
            self.assertFalse(self.app.lock.locked())
            self.assertFalse(self.app.st["ai_thinking"])
            self.assertEqual(execution.turn["answer"], "Ready")
            self.assert_idle_wait()
            raise EndTerminal()

        self.loop(read)

    def test_background_jobs_get_short_wait_and_idle_does_not_keep_polling_fast(self):
        self.app.st["busy"] = "refreshing"
        self.app.lock.acquire()
        reads = []

        def read():
            if not reads:
                self.assert_active_wait()
                reads.append(True)
                self.app.st["busy"] = None
                self.app.lock.release()
                raise tui.curses.error()
            self.assert_idle_wait()
            raise EndTerminal()

        self.loop(read)

    def test_idle_inbox_keeps_blocking_input_without_busy_spin(self):
        def read():
            self.assert_idle_wait()
            raise EndTerminal()

        self.loop(read)

    def test_repository_validation_keeps_its_short_wait(self):
        self.app.st["new_task"] = {
            "phase": "intent", "repo_status": "pending", "repo_version": 1,
            "repo": str(Path.cwd()), "intent": "", "active": "intent",
            "validate_after": time.monotonic() + 60,
        }

        def read():
            self.assert_active_wait()
            raise EndTerminal()

        self.loop(read)


class RealExecutionAcceptanceTests(unittest.TestCase):
    def start(self, script):
        execution = tui.CommandExecution([sys.executable, "-c", script])
        self.addCleanup(self.cleanup_execution, execution)
        execution.start()
        return execution

    def cleanup_execution(self, execution):
        if not execution.finished.is_set():
            execution.cancel()
        self.assertTrue(execution.finished.wait(3), "owned process and readers must be reaped")

    def test_real_process_delivers_all_output_before_exactly_one_completion_with_actual_status(self):
        execution = self.start(
            "import os,sys\n"
            "os.write(1, b'first ' + bytes([0xe9]))\n"
            "os.write(1, bytes([0x9b, 0xaa]) + b' tail')\n"
            "os.write(2, b'stderr without newline')\n"
            "sys.exit(7)\n")
        self.assertTrue(execution.finished.wait(3), "an immediate real worker must complete promptly")
        events = []
        while True:
            try:
                events.append(execution.events.get_nowait())
            except queue.Empty:
                break
        self.assertEqual("".join(data for kind, data in events if kind == "stdout"), "first 雪 tail")
        self.assertEqual("".join(data for kind, data in events if kind == "stderr"), "stderr without newline")
        self.assertEqual([event for event in events if event[0] == "done"], [("done", 7)])
        self.assertEqual(events[-1], ("done", 7), "completion must not overtake stream delivery")
        self.assertFalse([event for event in events if event[0] == "error"], events)
        self.assertIsNotNone(execution.process)
        self.assertEqual(execution.process.returncode, 7)

    def test_cancellation_reaps_a_real_descendant_holding_output_pipes_open(self):
        temporary = tempfile.TemporaryDirectory(prefix="chief-worker-tree-")
        self.addCleanup(temporary.cleanup)
        ready = Path(temporary.name) / "ready.json"
        child = ("import os,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                 "os.write(1, b'child ready\\n'); time.sleep(30)")
        execution = self.start(
            "import json,os,pathlib,signal,subprocess,sys,time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "child = subprocess.Popen([sys.executable, '-c', {child!r}])\n"
            "pathlib.Path({ready!r}).write_text(json.dumps([os.getpid(), child.pid, os.getpgrp()]))\n"
            "time.sleep(30)\n".format(child=child, ready=str(ready)))
        deadline = time.monotonic() + 3
        saw_child = False
        while time.monotonic() < deadline:
            try:
                kind, data = execution.events.get(timeout=0.05)
            except queue.Empty:
                continue
            if kind == "stdout" and "child ready" in data:
                saw_child = True
                break
        self.assertTrue(saw_child, "both real processes must be running before cancellation")
        parent, descendant, group = json.loads(ready.read_text())
        self.assertEqual(group, parent, "workers must own a process group")
        execution.cancel()
        self.assertTrue(execution.finished.wait(3), "cancellation must close inherited pipes and reap readers")
        for pid in (parent, descendant):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            stat = Path("/proc") / str(pid) / "stat"
            if stat.exists() and stat.read_text().split(")", 1)[1].lstrip().startswith("Z"):
                continue
            self.fail("cancelled worker tree still contains live PID {}".format(pid))
        self.assertTrue(execution.cancelled.is_set())
        self.assertEqual(execution.process.returncode, -signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
