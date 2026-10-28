"""Isolated terminal and executable boundaries for command-bar acceptance tests.

The Inbox, CLI parser, task storage and subprocesses are real. AoE, AI engines
and tmux are temporary executable fixtures; no installed service is contacted.
No new production helper API or command state layout is prescribed.
"""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

import chief
import chief_tui as tui
import test_inbox_composer as composer
import test_inbox_delegation as delegation


CHIEF_COMMANDS = ("tasks", "show", "new", "decide", "nudge", "close", "clean",
                  "track", "untrack", "tick")
SESSION_COMMANDS = ("show", "start", "stop", "archive", "unarchive", "restore")
REAL_AOE = chief.aoe


AOE_SCRIPT = r'''
import json, os, pathlib, shutil, sys, time
root = pathlib.Path(os.environ["CHIEF_ACCEPTANCE_ROOT"])
argv = sys.argv[1:]
with (root / "aoe-calls.jsonl").open("a") as f:
    f.write(json.dumps({"argv": argv, "pid": os.getpid(), "ppid": os.getppid(), "tty": os.isatty(0)}) + "\n")
assert argv[:2] == ["-p", os.environ["CHIEF_ACCEPTANCE_PROFILE"]], argv
args = argv[2:]
control = json.loads((root / "aoe-control.json").read_text())
for rule in control.get("rules", []):
    if args != rule["argv"]:
        continue
    if rule.get("started"):
        pathlib.Path(rule["started"]).write_text(str(os.getpid()))
    if rule.get("early_effect"):
        pathlib.Path(rule["early_effect"]).write_text("completed before cancellation")
    print(rule.get("stdout", ""), end="", flush=True)
    print(rule.get("stderr", ""), end="", file=sys.stderr, flush=True)
    if rule.get("release"):
        deadline = time.monotonic() + 15
        while not pathlib.Path(rule["release"]).exists() and time.monotonic() < deadline:
            time.sleep(.01)
    if rule.get("effect"):
        pathlib.Path(rule["effect"]).write_text("completed side effect")
    print(rule.get("late_stdout", ""), end="", flush=True)
    sys.exit(rule.get("code", 0))
records_path = pathlib.Path(os.environ["CHIEF_ACCEPTANCE_RECORDS"])
records = json.loads(records_path.read_text())["sessions"]
if args[0] == "list" or args[:2] in (["session", "list"], ["trash", "list"]):
    rows = records if len(args) > 2 else [r for r in records if r["state"] == "live"]
    print(json.dumps(rows))
elif args[:2] == ["settings", "explain"]:
    print("session.delete_to_trash = " + str(control.get("trash", True)).lower())
elif args[:2] == ["session", "capture"]:
    print(json.dumps({"status": "idle", "content": "Ask Codex to do anything\n› "}))
elif args[:2] == ["session", "show"]:
    sid = next((a for a in reversed(args[2:]) if not a.startswith("-")), "")
    rec = next((r for r in records if r["id"].startswith(sid)), {})
    if "--json" in args:
        if (root / "sent").exists():
            rec = dict(rec, status="running")
        print(json.dumps(rec))
    else:
        print("shown session " + sid)
elif args[0] == "send":
    (root / "sent").write_text(args[-1])
elif args[:2] == ["session", "color"]:
    pass
elif args[0] == "remove" or (args[0] == "session" and args[1] in
                             ("start", "stop", "archive", "unarchive", "restore")):
    sid = args[1] if args[0] == "remove" else args[2]
    for rec in records:
        if rec["id"] == sid:
            action = args[0] if args[0] == "remove" else args[1]
            rec["state"] = {"archive": "archived", "remove": "trashed"}.get(action, "live")
            if action == "remove":
                rec["trashed_at"] = "2026-10-26T12:00:00Z"
                wt = pathlib.Path(rec["project_path"])
                if wt.exists() and wt.name.startswith("worktree-"):
                    shutil.move(str(wt), str(root / ("trash-" + sid)))
            rec["status"] = "stopped" if action in ("archive", "stop") else "idle"
    records_path.write_text(json.dumps({"sessions": records}))
    print("aoe completed " + " ".join(args))
else:
    print("fixture: unsupported AoE invocation " + repr(args), file=sys.stderr)
    sys.exit(61)
'''


TMUX_SCRIPT = r'''
import json, os, pathlib, sys
root = pathlib.Path(os.environ["CHIEF_ACCEPTANCE_ROOT"])
with (root / "tmux-calls.jsonl").open("a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
if os.environ.get("CHIEF_ACCEPTANCE_TMUX_FAIL"):
    print("fixture tmux launch failed", file=sys.stderr)
    sys.exit(23)
args = sys.argv[1:]
if args[0] in ("display-message", "display"):
    query = " ".join(args)
    if "pane_id" in query:
        print("%17")
    elif "pane_current_path" in query:
        print(root / "source")
    else:
        print("acceptance-current" if "session" in query else "@42")
elif args[0] in ("new-window", "new-session"):
    print("@42")
elif args[0] == "split-window":
    print("%43")
'''


class CommandFixture(composer.ComposerFixture):
    def setUp(self):
        super().setUp()
        self.bin = self.root / "fake executables"
        self.bin.mkdir()
        self.aoe_executable = self.executable("aoe fixture's binary", AOE_SCRIPT)
        self.executable("tmux", TMUX_SCRIPT)
        for name in ("codex", "claude"):
            self.executable(name, "import sys\nsys.stderr.write('fixture AI unavailable\\n')\nsys.exit(9)\n")
        self.aoe_home = self.root / "aoe-state"
        self.cfg.update(profile="acceptance team's profile", tracked_ids=[])
        chief.save_cfg(self.cfg)
        self.records_path = self.aoe_home / "profiles" / self.cfg["profile"] / "sessions.json"
        self.records_path.parent.mkdir(parents=True)
        self.records = [self.record("live-1111"), self.record("live-2222"),
                        self.record("archived-3333", "archived"),
                        self.record("trashed-4444", "trashed")]
        self.write_records()
        self.control = {"rules": [], "trash": True}
        self.write_control()
        self.patch_object(chief, "AOE_HOME", new=self.aoe_home)
        self.patch_object(chief, "aoe", new=REAL_AOE)
        from unittest import mock
        patcher = mock.patch.dict(os.environ, {
            "AOE_CHIEF_HOME": str(chief.ROOT), "AOE_HOME": str(self.aoe_home),
            "AOE_BIN": str(self.aoe_executable), "AOE_CHIEF_LANG": "en",
            "CODEX_HOME": str(self.root / "codex"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "CHIEF_ACCEPTANCE_ROOT": str(self.root),
            "CHIEF_ACCEPTANCE_PROFILE": self.cfg["profile"],
            "CHIEF_ACCEPTANCE_RECORDS": str(self.records_path),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.release_workers)
        self.processes = []
        self.launches = []
        real_popen = subprocess.Popen

        def popen(argv, *args, **kwargs):
            self.launches.append((argv, dict(kwargs)))
            process = real_popen(argv, *args, **kwargs)
            self.processes.append((list(argv) if isinstance(argv, (list, tuple)) else argv, process))
            return process

        self.patch_object(subprocess, "Popen", side_effect=popen)
        self.addCleanup(self.reap_workers)
        names = {getattr(tui.curses, name): name.encode() for name in
                 ("KEY_DOWN", "KEY_UP", "KEY_BACKSPACE", "KEY_ENTER", "KEY_NPAGE", "KEY_PPAGE")}
        tui.curses.keyname.side_effect = lambda key: names[key]

    def executable(self, name, body):
        path = self.bin / name
        path.write_text("#!" + sys.executable + "\n" + body, encoding="utf-8")
        path.chmod(0o700)
        return path

    def record(self, sid, state="live", path=None):
        return {"id": sid, "title": "Fixture " + sid, "state": state,
                "status": "idle" if state == "live" else "stopped", "view": "terminal",
                "tool": "codex", "project_path": str(path or self.repo),
                **({"trashed_at": "2026-10-26T12:00:00Z"} if state == "trashed" else {})}

    def write_records(self):
        self.records_path.write_text(json.dumps({"sessions": self.records}), encoding="utf-8")

    def write_control(self):
        (self.root / "aoe-control.json").write_text(json.dumps(self.control), encoding="utf-8")

    def calls(self):
        path = self.root / "aoe-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def aoe_args(self):
        return [row["argv"][2:] for row in self.calls()]

    def task(self, tid="task-alpha", state="running", sid="live-1111", **changes):
        task = dict(id=tid, name="Fixture " + tid, goal="Acceptance fixture goal", checks=[],
                    repo=str(self.repo), branch="chief/" + tid, state=state, autonomy="observe")
        if sid:
            task["session_id"] = sid
        task.update(changes)
        chief.save_task(task)
        return task

    def decision(self, ident="task-alpha:1", state="pending"):
        task = self.task()
        rows = chief.load_decisions(task["id"])
        rows.append(dict(id=ident, task=task["id"], state=state, size="big", by="rules",
                         question="Which fixture option?", session=task["session_id"],
                         created_at=chief.now_utc().isoformat()))
        chief.save_decisions(task["id"], rows)

    def command_open(self):
        self.assertIsNotNone(self.app.st["input"], "':' must open the command bar")

    def command(self, text, submit=True):
        return [":", composer.Action(self.command_open), text, "\n" if submit else ""]

    def buffer(self):
        self.command_open()
        return self.app.st["input"]["buf"]

    def assert_buffer(self, expected):
        self.assertEqual(self.buffer().rstrip(), expected.rstrip())

    def detail(self):
        return "\n".join(self.frame()[1:-2])

    def assert_output(self, *pieces):
        self.assert_idle()
        for piece in pieces:
            self.assertIn(piece, self.detail(), "command output belongs in the detail pane")

    def completed(self, *pieces, seconds=3):
        check = composer.Check(lambda: self.assert_output(*pieces))
        check.deadline = time.monotonic() + seconds
        return check

    def completes(self, expected):
        # On a cold cache, Tab may report that data is loading. Retry at idle
        # input boundaries instead of requiring an automatic pending edit.
        def check():
            if self.buffer().rstrip() != expected.rstrip():
                self.app.typed("\t")
            self.assert_buffer(expected)
        return composer.Check(check)

    def gated(self, argv, stdout="before gate\n", stderr="", late="late result\n", **extra):
        tag = str(len(self.control["rules"]))
        rule = dict(argv=list(argv), stdout=stdout, stderr=stderr, late_stdout=late,
                    started=str(self.root / ("started-" + tag)),
                    release=str(self.root / ("release-" + tag)), **extra)
        self.control["rules"].append(rule)
        self.write_control()
        return rule

    def assert_started(self, rule):
        self.assertTrue(Path(rule["started"]).exists(), "the owned command process must start")

    def release_workers(self):
        for rule in self.control["rules"]:
            if rule.get("release"):
                Path(rule["release"]).touch()

    def reap_workers(self):
        self.release_workers()
        for _, process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)

    def assert_process_dead(self, pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        try:
            # The process may finish exiting between kill() and this read; a vanished entry means it is gone.
            if (Path("/proc") / str(pid) / "stat").read_text().split(")", 1)[1].strip().startswith("Z"):
                return
        except (FileNotFoundError, ProcessLookupError):
            return
        self.fail("cancelled command still has a live owned process: " + str(pid))

    def selected_items(self):
        task = self.task()
        self.items = [delegation.item("live-1111", "Delegated fixture", self.repo, task),
                      delegation.item("live-2222", "Thinking fixture", self.repo)]

    def q(self, value):
        return shlex.quote(str(value))
