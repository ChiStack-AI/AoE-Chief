"""Acceptance tests for left/right console, current-window reuse and user help.

tmux and the user's shell are executable fixtures. Launcher strings are run
only against an isolated copied installation and a mocked curses entry point.
"""
import json
import os
from pathlib import Path
from documentation_support import read_documentation
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
            "    value = dict(argv=sys.argv, cfg=cfg, lang=ch.LANG, every=every, cwd=os.getcwd())\n"
            "    (root / 'inbox-launched.json').write_text(json.dumps(value))\n"
            "    return 0\n"
            "chief_tui.run = isolated_inbox\n", encoding="utf-8")
        self.shell = self.executable("interactive user's shell; literal", r'''
import json, os, pathlib, sys
root = pathlib.Path(os.environ["CHIEF_ACCEPTANCE_ROOT"])
with (root / "shell-calls.jsonl").open("a") as f:
    f.write(json.dumps({"argv": sys.argv, "cwd": os.getcwd()}) + "\n")
print("INTERACTIVE-SHELL-STARTED", flush=True)
''')
        self.user_home = self.root / "user's home; literal"
        self.user_home.mkdir()
        self.console_env = dict(os.environ, SHELL=str(self.shell), HOME=str(self.user_home),
                                PYTHONPATH=os.pathsep.join((str(self.install), str(probe))))
        self.console_env.pop("CHIEF_ACCEPTANCE_TMUX_FAIL", None)
        self.console_env.pop("TMUX", None)
        self.console_env.pop("TMUX_PANE", None)

    def console(self, inside=False, *args, env=None):
        marker = self.root / "inbox-launched.json"
        if marker.exists():
            marker.unlink()
        environ = dict(self.console_env if env is None else env)
        if inside:
            environ["TMUX"] = "/tmp/acceptance-tmux.sock,1234,0"
            environ["TMUX_PANE"] = "%17"
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

    def has_flag(self, call, flag):
        return any(arg.startswith("-") and not arg.startswith("--") and flag in arg[1:]
                   for arg in call[1:])

    def assert_current_target(self, call):
        if "-t" in call:
            target = call[call.index("-t") + 1]
            self.assertIn(target, ("%17", "@42", "acceptance-current:", "acceptance-current:0"),
                          "target the current AoE pane/window, never another tmux window")

    def assert_layout(self, calls, inside=False, layout="left-right"):
        splits = [c for c in calls if c[0] == "split-window"]
        self.assertEqual(len(splits), 1, "console has one inbox pane and one shell pane")
        self.assertEqual(self.has_flag(splits[0], "h"), layout == "left-right",
                         "default to inbox left/shell right; top/bottom is an explicit option")
        self.assertFalse(self.has_flag(splits[0], "b"), "place the shell after the inbox (right/bottom)")
        if inside:
            self.assert_current_target(splits[0])
            self.assertFalse(any(c[0] in ("new-window", "new-session", "attach", "attach-session") for c in calls))
            # Inside AoE the current process can enter the inbox directly, or
            # replace its current pane with an inbox launcher. Both preserve it.
            inbox_launchers = [c for c in calls if c[0] == "respawn-pane"]
            if (self.root / "inbox-launched.json").exists():
                self.assertEqual(inbox_launchers, [], "start only one inbox")
                inbox_command = None
            else:
                self.assertEqual(len(inbox_launchers), 1, "run the inbox in the original pane")
                self.assertIn("-t", inbox_launchers[0], "do not replace the newly focused shell pane")
                self.assertEqual(inbox_launchers[0][inbox_launchers[0].index("-t") + 1], "%17")
                inbox_command = inbox_launchers[0][-1]
                self.assertIn("inbox", inbox_command)
        else:
            self.assertIn("-t", splits[0], "split the newly created console session explicitly")
            inbox_command = self.launcher(calls, lambda arg: "chief.py" in arg and "inbox" in arg)
            self.assertIn(inbox_command, next(c for c in calls if c[0] == "new-session"))
        return inbox_command, splits[0][-1]

    def launch_pane(self, command, call):
        cwd = str(self.repo)
        if "-c" in call:
            cwd = call[call.index("-c") + 1]
            if cwd == "~" or cwd.startswith("~/"):
                cwd = str(self.user_home) + cwd[1:]
            if not os.path.isabs(cwd):
                cwd = str(self.repo / cwd)
        result = subprocess.run(["/bin/sh", "-c", command], env=self.console_env,
                                cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, universal_newlines=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        if call[0] == "split-window":
            self.assertIn("INTERACTIVE-SHELL-STARTED", result.stdout)
            sheet = result.stdout.split("INTERACTIVE-SHELL-STARTED", 1)[0]
            for name in support.CHIEF_COMMANDS + support.SESSION_COMMANDS:
                self.assertRegex(sheet, r"\b" + name + r"\b", "print the cheat sheet before every shell")
            argv = self.shell_records()[-1]["argv"]
            self.assertEqual(argv[0], str(self.shell))
            self.assertTrue(any(arg.startswith("-") and "i" in arg[1:] for arg in argv[1:]))
        return result

    def assert_inbox_context(self, launcher, calls):
        if launcher is not None:
            call = next(c for c in calls if launcher in c)
            self.launch_pane(launcher, call)
        inbox = json.loads((self.root / "inbox-launched.json").read_text())
        self.assertEqual(Path(inbox["argv"][0]), self.install / "chief.py")
        self.assertEqual(inbox["cfg"]["profile"], self.cfg["profile"])
        self.assertEqual(inbox["lang"], "en")
        self.assertEqual(Path(inbox["cwd"]), self.repo, "the inbox retains its AoE project context")

    def shell_records(self):
        return [json.loads(line) for line in (self.root / "shell-calls.jsonl").read_text().splitlines()]

    def test_inside_tmux_splits_the_current_aoe_window_and_keeps_inbox_in_its_original_pane(self):
        result = self.console(inside=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.tmux_calls()
        inbox, _ = self.assert_layout(calls, inside=True)
        self.assert_inbox_context(inbox, calls)

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

    def test_launchers_safely_quote_paths_and_print_the_shared_cheat_sheet_before_the_shell(self):
        result = self.console()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.tmux_calls()
        inbox, shell = self.assert_layout(calls)
        self.assert_inbox_context(inbox, calls)
        self.assertEqual(json.loads((self.root / "inbox-launched.json").read_text())["argv"][1], "inbox")
        split = next(c for c in calls if c[0] == "split-window")
        launched = self.launch_pane(shell, split)
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
        shell_calls = self.shell_records()
        self.assertEqual(len(shell_calls), 1)
        self.assertEqual(shell_calls[0]["argv"][0], str(self.shell))
        self.assertTrue(any(arg.startswith("-") and "i" in arg[1:]
                            for arg in shell_calls[0]["argv"][1:]), "launch the user's shell interactively")
        self.assertEqual(Path(shell_calls[0]["cwd"]), self.user_home)

    def test_shell_defaults_to_home_inside_and_outside_tmux(self):
        for inside in (True, False):
            with self.subTest(inside=inside):
                self.clear_tmux_calls()
                result = self.console(inside)
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = self.tmux_calls()
                _, shell = self.assert_layout(calls, inside=inside)
                self.launch_pane(shell, next(c for c in calls if c[0] == "split-window"))
                self.assertEqual(Path(self.shell_records()[-1]["cwd"]), self.user_home)

    def clear_tmux_calls(self):
        path = self.root / "tmux-calls.jsonl"
        if path.exists():
            path.unlink()

    def test_shell_working_directory_is_configurable_with_literal_and_home_relative_paths(self):
        custom = self.user_home / "work area's directory; literal"
        custom.mkdir()
        for directory in (str(custom), "~/" + custom.name):
            for inside in (True, False):
                with self.subTest(directory=directory, inside=inside):
                    self.clear_tmux_calls()
                    result = self.console(inside, "--shell-cwd", directory)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    calls = self.tmux_calls()
                    inbox, shell = self.assert_layout(calls, inside=inside)
                    self.assert_inbox_context(inbox, calls)
                    self.launch_pane(shell, next(c for c in calls if c[0] == "split-window"))
                    self.assertEqual(Path(self.shell_records()[-1]["cwd"]), custom)

    def test_top_bottom_layout_remains_available_as_an_explicit_option(self):
        for inside in (True, False):
            with self.subTest(inside=inside):
                self.clear_tmux_calls()
                result = self.console(inside, "--layout", "top-bottom")
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = self.tmux_calls()
                inbox, shell = self.assert_layout(calls, inside=inside, layout="top-bottom")
                self.assert_inbox_context(inbox, calls)
                launched = self.launch_pane(shell, next(c for c in calls if c[0] == "split-window"))
                self.assertIn("tasks", launched.stdout.split("INTERACTIVE-SHELL-STARTED", 1)[0])
                self.assertEqual(Path(self.shell_records()[-1]["cwd"]), self.user_home)

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
        for word in ("tmux", "inbox", "left", "right", "top", "bottom", "shell", "interactive", "session"):
            self.assertIn(word, console)

    def test_console_help_explains_layout_and_shell_directory_options(self):
        console = self.cli_help("console")
        for option in ("--layout", "left-right", "top-bottom", "--shell-cwd"):
            self.assertIn(option, console)
        self.assertRegex(console, r"default.{0,100}left|left.{0,100}default")
        self.assertRegex(console, r"(?:current|existing).{0,50}(?:window|pane)")
        self.assertRegex(console, r"home|~")

    def test_readme_explains_restricted_commands_completion_history_confirmation_and_cancellation(self):
        readme = re.sub(r"\s+", " ", read_documentation()).lower()
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
        self.assertRegex(readme, r"(?:left.{0,100}inbox|inbox.{0,100}left)")
        self.assertRegex(readme, r"(?:right.{0,100}(?:interactive|shell)|(?:interactive|shell).{0,100}right)")
        self.assertRegex(readme, r"(?:shell.*(?:pipeline|redirection)|(?:pipeline|redirection).*shell)")

    def test_readme_documents_default_split_current_window_and_configurable_home_directory(self):
        readme = re.sub(r"\s+", " ", read_documentation()).lower()
        for phrase in ("chief console", "--layout top-bottom", "--shell-cwd"):
            self.assertTrue(phrase in readme, "README should explain " + phrase)
        self.assertRegex(readme, r"(?:current|existing).{0,70}(?:tmux )?window")
        self.assertRegex(readme, r"(?:shell|directory|cwd).{0,100}(?:~|home)|(?:~|home).{0,100}(?:shell|directory|cwd)")
        self.assertRegex(readme, r"half.{0,20}width|narrow")

    def test_readme_points_aoe_chief_custom_agent_to_console_and_preserves_launch_example(self):
        source = read_documentation()
        blocks = re.findall(r"```toml\s*\n(.*?)```", source, re.S | re.I)
        commands = [match.group(2) for block in blocks if "custom_agents" in block
                    for match in re.finditer(r'''(?<![\w-])chief\s*=\s*(["'])(.*?)\1''', block)]
        self.assertTrue(commands, "show how to configure the AoE custom agent named chief")
        self.assertTrue(any(re.search(r"\bchief(?:\.py)?\s+console\b", command) for command in commands),
                        "the chief custom agent should launch chief console for the default split")
        self.assertFalse(any(re.search(r"\bchief(?:\.py)?\s+inbox\b", command) for command in commands),
                         "do not leave the chief custom-agent example pointing at the unsplit inbox")
        self.assertRegex(source, r'''aoe\s+add[^\n]*-t\s+["']chief inbox["'][^\n]*--tool\s+chief\b[^\n]*--launch''')
