"""Acceptance tests for tmux console layout, safe launchers and user help.

tmux and the user's shell are executable fixtures. Launcher strings are run
only against an isolated copied installation and a mocked curses entry point.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

import chief
import chief_tui as tui
import inbox_command_support as support


REPO = Path(__file__).resolve().parent.parent


class ConsoleTests(support.CommandFixture):
    def setUp(self):
        super().setUp()
        self.install = self.root / "install's directory; literal"
        self.install.mkdir()
        for name in ("chief.py", "chief_tui.py"):
            (self.install / name).write_bytes((REPO / name).read_bytes())
        probe = self.root / "startup-probe"
        probe.mkdir()
        (probe / "sitecustomize.py").write_text(
            "import json, os, pathlib, sys\n"
            "import chief_tui\n"
            "def isolated_inbox(cfg, ch, every=15):\n"
            "    root = pathlib.Path(os.environ['CHIEF_ACCEPTANCE_ROOT'])\n"
            "    value = dict(argv=sys.argv, cfg=cfg, lang=ch.LANG, every=every)\n"
            "    (root / 'inbox-launched.json').write_text(json.dumps(value))\n"
            "    return 0\n"
            "chief_tui.run = isolated_inbox\n", encoding="utf-8")
        self.shell = self.executable("interactive user's shell; literal", r'''
import json, os, pathlib, sys
root = pathlib.Path(os.environ["CHIEF_ACCEPTANCE_ROOT"])
with (root / "shell-calls.jsonl").open("a") as f:
    f.write(json.dumps(sys.argv) + "\n")
print("INTERACTIVE-SHELL-STARTED", flush=True)
''')
        self.console_env = dict(os.environ, SHELL=str(self.shell),
                                PYTHONPATH=os.pathsep.join((str(self.install), str(probe))))
        self.console_env.pop("CHIEF_ACCEPTANCE_TMUX_FAIL", None)
        self.console_env.pop("TMUX", None)

    def console(self, inside=False, *args, env=None):
        environ = dict(self.console_env if env is None else env)
        if inside:
            environ["TMUX"] = "/tmp/acceptance-tmux.sock,1234,0"
        result = subprocess.run([sys.executable, str(self.install / "chief.py"), "console", *args],
                                env=environ, cwd=str(self.repo), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, timeout=5)
        return result

    def tmux_calls(self):
        path = self.root / "tmux-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def launcher(self, calls, predicate):
        candidates = [arg for call in calls if call[0] in ("new-window", "new-session", "split-window")
                      for arg in call[1:] if predicate(arg)]
        self.assertEqual(len(candidates), 1, "launch exactly one matching pane: " + repr(calls))
        return candidates[0]

    def assert_layout(self, calls):
        splits = [c for c in calls if c[0] == "split-window"]
        self.assertEqual(len(splits), 1, "console has one inbox pane and one shell pane")
        self.assertFalse(any(arg == "-h" or (arg.startswith("-") and not arg.startswith("--")
                                             and "h" in arg[1:]) for arg in splits[0]),
                         "top/bottom panes need a vertical split")
        self.assertIn("-t", splits[0], "split the console window explicitly")
        top = self.launcher(calls, lambda arg: "chief.py" in arg and "inbox" in arg)
        bottom = self.launcher(calls, lambda arg: "interactive user" in arg or "$SHELL" in arg)
        self.assertIn(top, next(c for c in calls if c[0] in ("new-window", "new-session")))
        self.assertIn(bottom, splits[0])
        return top, bottom

    def test_inside_tmux_opens_a_window_in_the_current_session_without_attaching_another(self):
        result = self.console(inside=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.tmux_calls()
        self.assertEqual(sum(c[0] == "new-window" for c in calls), 1)
        self.assertFalse(any(c[0] in ("new-session", "attach", "attach-session") for c in calls))
        self.assert_layout(calls)
        self.assertFalse((self.root / "inbox-launched.json").exists(), "the parent CLI only launches tmux")

    def test_outside_tmux_creates_configures_then_attaches_a_session(self):
        result = self.console()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.tmux_calls()
        creates = [i for i, call in enumerate(calls) if call[0] == "new-session"]
        attaches = [i for i, call in enumerate(calls) if call[0] in ("attach", "attach-session")]
        self.assertEqual(len(creates), 1)
        self.assertEqual(len(attaches), 1)
        self.assertIn("-d", calls[creates[0]], "configure both panes before the blocking attach")
        self.assertLess(creates[0], next(i for i, call in enumerate(calls) if call[0] == "split-window"))
        self.assertEqual(attaches[0], len(calls) - 1)
        self.assert_layout(calls)

    def test_launchers_safely_quote_installation_and_shell_paths_and_print_the_shared_cheat_sheet(self):
        result = self.console(inside=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        top, bottom = self.assert_layout(self.tmux_calls())
        for command in (top, bottom):
            launched = subprocess.run(["/bin/sh", "-c", command], env=self.console_env,
                                      cwd=str(self.repo), stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      universal_newlines=True, timeout=5)
            self.assertEqual(launched.returncode, 0, launched.stderr)
            if command == top:
                inbox = json.loads((self.root / "inbox-launched.json").read_text())
                self.assertEqual(inbox["argv"][1], "inbox")
                self.assertEqual(Path(inbox["argv"][0]), self.install / "chief.py")
                self.assertEqual(inbox["cfg"]["profile"], self.cfg["profile"])
                self.assertEqual(inbox["lang"], "en")
            else:
                output = re.sub(r"\x1b\[[0-9;]*m", "", launched.stdout)
                self.assertIn("INTERACTIVE-SHELL-STARTED", output)
                sheet = output.split("INTERACTIVE-SHELL-STARTED", 1)[0]
                for name in support.CHIEF_COMMANDS + support.SESSION_COMMANDS:
                    self.assertRegex(sheet, r"\b" + name + r"\b")
                self.app.key(":")
                self.command_open()
                lines = [line.strip() for line in self.frame()[-8:-2] if
                         sum(bool(re.search(r"\b" + name + r"\b", line))
                             for name in support.CHIEF_COMMANDS) >= 3]
                self.assertTrue(lines, "the bar must show its compact cheat sheet above input")
                for line in lines:
                    self.assertIn(line, sheet, "console and command bar share the same cheat sheet")
                shell_calls = [json.loads(line) for line in
                               (self.root / "shell-calls.jsonl").read_text().splitlines()]
                self.assertEqual(len(shell_calls), 1)
                self.assertEqual(shell_calls[0][0], str(self.shell))
                self.assertTrue(any(arg.startswith("-") and "i" in arg[1:]
                                    for arg in shell_calls[0][1:]), "launch the user's shell interactively")

    def test_missing_tmux_is_a_clear_cli_error_without_a_traceback(self):
        empty = self.root / "empty-path"
        empty.mkdir()
        result = self.console(env=dict(self.console_env, PATH=str(empty)))
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex((result.stdout + result.stderr).lower(), r"tmux.*(?:not found|missing|install|unavailable|required)")
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.tmux_calls(), [])

    def test_failed_tmux_launch_is_reported_and_does_not_attach(self):
        result = self.console(env=dict(self.console_env, CHIEF_ACCEPTANCE_TMUX_FAIL="1"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fixture tmux launch failed", result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(any(c[0] in ("attach", "attach-session") for c in self.tmux_calls()))


class DocumentationTests(unittest.TestCase):
    def cli_help(self, command):
        result = subprocess.run([sys.executable, str(REPO / "chief.py"), command, "--help"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, timeout=5,
                                env=dict(os.environ, AOE_CHIEF_LANG="en"))
        self.assertEqual(result.returncode, 0, result.stderr)
        return re.sub(r"\s+", " ", result.stdout).lower()

    def test_cli_help_documents_command_bar_and_console(self):
        inbox = self.cli_help("inbox")
        for word in (":", "command", "tab", "history", "esc", "confirm"):
            self.assertIn(word, inbox)
        console = self.cli_help("console")
        for word in ("tmux", "inbox", "top", "bottom", "shell", "interactive", "session"):
            self.assertIn(word, console)

    def test_readme_explains_restricted_commands_completion_history_confirmation_and_cancellation(self):
        readme = re.sub(r"\s+", " ", (REPO / "README.md").read_text()).lower()
        for phrase in ("chief console", "command bar", "tab", "history", "y/n", "esc"):
            self.assertTrue(phrase in readme, "README should explain " + phrase)
        for name in support.CHIEF_COMMANDS + support.SESSION_COMMANDS:
            self.assertRegex(readme, r"\b" + name + r"\b")
        self.assertRegex(readme, r"(?:restricted|allowlist|supported commands)")
        self.assertRegex(readme, r"(?:quoted|quoting|quotes)")
        self.assertRegex(readme, r"(?:up/down|up and down|↑.*↓)")
        self.assertRegex(readme, r"(?:pending decision|decision id)")
        self.assertRegex(readme, r"(?:background|responsive)")
        self.assertRegex(readme, r"(?:stdout|stderr|output)")
        self.assertRegex(readme, r"(?:scroll|pgup|pgdn)")
        self.assertRegex(readme, r"(?:terminat|kill|cancel).*(?:process|command)")
        self.assertRegex(readme, r"(?:side effects|already completed|does not undo|not undone|no rollback)")
        self.assertRegex(readme, r"(?:top.{0,100}inbox|inbox.{0,100}top)")
        self.assertRegex(readme, r"(?:bottom.{0,100}(?:interactive|shell)|(?:interactive|shell).{0,100}bottom)")
        self.assertRegex(readme, r"(?:shell.*(?:pipeline|redirection)|(?:pipeline|redirection).*shell)")
