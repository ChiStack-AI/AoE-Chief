"""Test-first contract for the process-local, combined Inbox operation log.

Exercise real key dispatch, rendering, parsing and owned worker processes with
the existing offline terminal/AoE/AI fixtures. No new production API or log
storage layout is prescribed. Existing acceptance tests remain unchanged,
including the legacy expectations that selection leaves result overlays open;
those expectations conflict with this contract's transient detail behavior.
"""
import io
import os
from pathlib import Path
from documentation_support import read_documentation
import re
import unittest
from unittest import mock

import chief
import chief_tui as tui
import inbox_ai_support as support
import test_inbox_composer as composer


class UnifiedLogFixture(support.AssistantFixture):
    def board(self):
        self.selected_items()
        self.items[0]["log"]["last_reply"] = "ITEM_ALPHA_DETAIL"
        self.items[1]["log"]["last_reply"] = "ITEM_BETA_DETAIL"

    def output_rule(self, tag, stdout="COMMAND_RESULT\n", **extra):
        self.control["rules"].append(dict(argv=["session", "show", tag], stdout=stdout, **extra))
        self.write_control()
        return "aoe session show " + tag

    def log_text(self):
        # Render the complete retained transcript for ordering/count assertions;
        # separate tests exercise normal-height scrolling through the key loop.
        return "\n".join(line.rstrip() for line in self.frame(height=1600)[1:-2]).rstrip()

    def assert_log_open(self):
        self.assertIsNone(self.app.st["input"])
        self.assertIsNone(self.app.st["confirm"])
        self.assertIn("Esc", self.frame()[-1], "the log advertises how to leave it")
        self.assertRegex("\n".join(self.frame()), r"(?i)\b(?:log|history)\b|日志|历史|记录")

    def assert_item(self, sid, *hidden):
        self.assertEqual(self.app.selected()["sid"], sid)
        marker = {"live-1111": "ITEM_ALPHA_DETAIL", "live-2222": "ITEM_BETA_DETAIL"}[sid]
        self.assertIn(marker, self.detail(), "selection restores the selected item's details")
        for text in hidden:
            self.assertNotIn(text, self.detail(), "dismissed output must stay in the log")

    def assert_no_new_operations(self, observed):
        # Refresh/completion may read session listings. Browsing may not launch
        # another explicit command, generate AI, or invoke a state mutation.
        calls = [args for args in self.aoe_args() if args[0] != "list" and
                 args[:2] not in (["session", "list"], ["trash", "list"])]
        self.assertEqual(calls, observed["aoe"])
        self.assertEqual(len(self.ai_calls()), observed["ai"])
        self.assertEqual(len(self.traces("generate_ai")), observed["generate"])
        self.assertEqual([row for row in self.traces() if row["name"] in support.MUTATIONS],
                         observed["mutations"])
        self.assertEqual(self.state_snapshot(), observed["state"])

    def operations(self):
        return dict(aoe=[args for args in self.aoe_args() if args[0] != "list" and
                         args[:2] not in (["session", "list"], ["trash", "list"])],
                    ai=len(self.ai_calls()), generate=len(self.traces("generate_ai")),
                    mutations=[row for row in self.traces() if row["name"] in support.MUTATIONS],
                    state=self.state_snapshot())


class TransientResultTests(UnifiedLogFixture):
    def test_command_selection_dismisses_result_and_refresh_cannot_reopen_it(self):
        self.board()
        command = self.output_rule("TRANSIENT_COMMAND", "TRANSIENT_STDOUT\n")
        self.scenario(*self.command(command), self.completed("TRANSIENT_STDOUT"),
                      "j", composer.Action(lambda: self.assert_item("live-2222", "TRANSIENT_STDOUT")),
                      "g", composer.Check(self.assert_idle),
                      composer.Action(lambda: self.assert_item("live-2222", "TRANSIENT_STDOUT")),
                      "k", composer.Action(lambda: self.assert_item("live-1111", "TRANSIENT_STDOUT")),
                      "L", composer.Action(self.assert_log_open),
                      composer.Action(lambda: self.assertIn("TRANSIENT_STDOUT", self.log_text())),
                      "\x1b", composer.Action(lambda: self.assert_item("live-1111", "TRANSIENT_STDOUT")))

    def test_ai_selection_dismisses_answer_and_new_question_shows_only_its_own_turn(self):
        self.board()
        self.reply("FIRST_TRANSIENT_ANSWER")
        self.scenario(*self.ask("FIRST_TRANSIENT_QUESTION"), self.answered_with("FIRST_TRANSIENT_ANSWER"),
                      "j", composer.Action(lambda: self.assert_item("live-2222", "FIRST_TRANSIENT_ANSWER")),
                      "g", composer.Check(self.assert_idle), "k",
                      composer.Action(lambda: self.assert_item("live-1111", "FIRST_TRANSIENT_ANSWER")),
                      composer.Action(lambda: self.reply("SECOND_TRANSIENT_ANSWER")),
                      *self.ask("SECOND_TRANSIENT_QUESTION"), self.answered_with("SECOND_TRANSIENT_ANSWER"),
                      composer.Action(lambda: self.assertNotIn("FIRST_TRANSIENT_ANSWER", self.detail())),
                      "L", composer.Action(self.assert_log_open),
                      composer.Action(lambda: self.assertIn("FIRST_TRANSIENT_ANSWER", self.log_text())))

    def test_new_command_replaces_ai_transient_but_combined_log_keeps_chronology(self):
        first = self.output_rule("FIRST_COMMAND", "FIRST_COMMAND_ANSWER\n")
        last = self.output_rule("LAST_COMMAND", "LAST_COMMAND_ANSWER\n")
        self.reply("MIDDLE_AI_ANSWER")

        def ordered():
            text = self.log_text()
            tokens = (first, "FIRST_COMMAND_ANSWER", "MIDDLE_QUESTION", "MIDDLE_AI_ANSWER",
                      last, "LAST_COMMAND_ANSWER")
            for token in tokens:
                self.assertIn(token, text)
            positions = [text.index(token) for token in tokens]
            self.assertEqual(positions, sorted(positions))

        self.scenario(*self.command(first), self.completed("FIRST_COMMAND_ANSWER"),
                      *self.ask("MIDDLE_QUESTION"), self.answered_with("MIDDLE_AI_ANSWER"),
                      composer.Action(lambda: self.assertNotIn("FIRST_COMMAND_ANSWER", self.detail())),
                      *self.command(last), self.completed("LAST_COMMAND_ANSWER"),
                      composer.Action(lambda: self.assertNotIn("MIDDLE_AI_ANSWER", self.detail())),
                      "L", composer.Action(ordered))

    def test_refresh_and_a_boundary_move_keep_result_when_selected_identity_is_unchanged(self):
        self.board()
        command = self.output_rule("KEEP_SELECTED", "STILL_TRANSIENT\n")
        self.scenario(*self.command(command), self.completed("STILL_TRANSIENT"),
                      "k", composer.Action(lambda: self.assertIn("STILL_TRANSIENT", self.detail())),
                      "g", composer.Check(self.assert_idle),
                      composer.Action(lambda: self.assertIn("STILL_TRANSIENT", self.detail())),
                      "\x1b", composer.Action(lambda: self.assert_item("live-1111", "STILL_TRANSIENT")),
                      "g", composer.Check(self.assert_idle),
                      composer.Action(lambda: self.assert_item("live-1111", "STILL_TRANSIENT")))

    def test_refresh_that_removes_selected_item_dismisses_transient_result(self):
        self.board()
        command = self.output_rule("REMOVED_SELECTION", "REMOVED_ITEM_RESULT\n")
        self.scenario(*self.command(command), self.completed("REMOVED_ITEM_RESULT"),
                      composer.Action(lambda: self.items.pop(0)), "g", composer.Check(self.assert_idle),
                      composer.Action(lambda: self.assert_item("live-2222", "REMOVED_ITEM_RESULT")),
                      "L", composer.Action(lambda: self.assertIn("REMOVED_ITEM_RESULT", self.log_text())))

    def test_dismissed_streaming_command_finishes_in_log_without_reopening_detail(self):
        self.board()
        rule = self.gated(["session", "show", "DISMISSED_STREAM"], stdout="STREAM_EARLY\n",
                          stderr="STREAM_STDERR\n", late="STREAM_FINISHED\n", code=7)

        def streaming():
            self.assert_started(rule)
            self.assertIn("STREAM_EARLY", self.detail())
            self.assertIn("STREAM_STDERR", self.detail())
            self.assertTrue(self.app.st["busy"])

        def finished_log():
            text = self.log_text()
            for token in ("aoe session show DISMISSED_STREAM", "STREAM_EARLY", "STREAM_STDERR", "STREAM_FINISHED"):
                self.assertEqual(text.count(token), 1, "streaming updates one entry in place")
            self.assertRegex(text.lower(), r"(?:exit|status|code|退出码)[^\n]*7")

        self.scenario(*self.command("aoe session show DISMISSED_STREAM"), composer.Check(streaming),
                      "j", composer.Action(lambda: self.assert_item("live-2222", "STREAM_EARLY")),
                      "\n\r", (tui.curses.KEY_ENTER,),
                      composer.Action(lambda: Path(rule["release"]).touch()), composer.Check(self.assert_idle),
                      composer.Action(lambda: self.assert_item("live-2222", "STREAM_FINISHED")),
                      "g", composer.Check(self.assert_idle),
                      composer.Action(lambda: self.assert_item("live-2222", "STREAM_FINISHED")),
                      "L", composer.Action(finished_log))
        self.assertEqual(self.aoe_args().count(rule["argv"]), 1)

    def test_dismissed_thinking_question_finishes_in_log_without_reopening_detail(self):
        self.board()
        rule = self.gated_ai("BACKGROUND_LOG_ANSWER")

        def thinking():
            self.assertTrue(Path(rule["started"]).exists())
            self.assertRegex("\n".join(self.frame()).lower(), r"thinking|generating|思考|生成")

        self.scenario(*self.ask("BACKGROUND_LOG_QUESTION"), composer.Check(thinking), "j",
                      composer.Action(lambda: self.assert_item("live-2222", "BACKGROUND_LOG_QUESTION")),
                      "\n\r", (tui.curses.KEY_ENTER,),
                      composer.Action(lambda: Path(rule["release"]).touch()), composer.Check(self.assert_idle),
                      composer.Action(lambda: self.assert_item("live-2222", "BACKGROUND_LOG_ANSWER")),
                      "g", composer.Check(self.assert_idle),
                      composer.Action(lambda: self.assert_item("live-2222", "BACKGROUND_LOG_ANSWER")),
                      "L", composer.Action(lambda: self.assertIn("BACKGROUND_LOG_ANSWER", self.log_text())))
        self.assertEqual(len(self.ai_calls()), 1)


class RetentionAndNavigationTests(UnifiedLogFixture):
    def test_latest_twenty_combined_submissions_are_retained_once_in_submission_order(self):
        commands = {i: self.output_rule("ENTRY_CMD_{:02d}".format(i), "RESPONSE_{:02d}\n".format(i))
                    for i in range(0, 22, 2)}
        expected = []
        for i in range(22):
            marker = "ENTRY_{}_{:02d}".format("QUESTION" if i % 2 else "CMD", i)
            expected.append(marker)
            if i % 2:
                self.reply("ANSWER_{:02d}".format(i))
                self.scenario(*self.ask(marker), self.answered_with("ANSWER_{:02d}".format(i)))
            else:
                self.scenario(*self.command(commands[i]), self.completed("RESPONSE_{:02d}".format(i)))
            if i >= 19:
                def retained():
                    text = self.log_text()
                    self.assertEqual(re.findall(r"ENTRY_(?:CMD|QUESTION)_\d{2}", text), expected[-20:])
                    for evicted in range(i - 19):
                        self.assertNotIn("RESPONSE_{:02d}".format(evicted), text)
                        self.assertNotIn("ANSWER_{:02d}".format(evicted), text)
                self.scenario("L", composer.Action(retained), "\x1b")

    def test_L_opens_empty_log_on_empty_board_and_escape_does_not_clear_it(self):
        observed = {}
        self.scenario(composer.Action(lambda: observed.update(self.operations())),
                      "L", composer.Action(self.assert_log_open),
                      (tui.curses.KEY_PPAGE, tui.curses.KEY_NPAGE), " \n\r", (tui.curses.KEY_ENTER,),
                      "\x1bL", composer.Action(self.assert_log_open), "\x1b",
                      composer.Action(lambda: self.assert_no_new_operations(observed)))

    def test_blank_and_abandoned_bars_do_not_create_log_entries(self):
        self.scenario(*self.command("show ABANDONED_COMMAND", submit=False), "\x1b",
                      "?ABANDONED_QUESTION\x1b", *self.command("   "), "?   \n",
                      "L", composer.Action(self.assert_log_open),
                      composer.Action(lambda: self.assertNotRegex(self.log_text(), r"ABANDONED_|\$ |Q: |问：")))
        self.assertFalse(self.ai_calls())
        self.assertFalse(self.traces("generate_ai"))

    def test_log_opens_at_newest_bottom_and_PgUp_PgDn_and_space_scroll_both_kinds(self):
        command = self.output_rule("LONG_COMMAND", "\n".join("CMD_LINE_{:03d}".format(n)
                                                           for n in range(65)) + "\n")
        self.reply("\n".join("AI_LINE_{:03d}".format(n) for n in range(65)))
        seen = set()

        def capture():
            seen.update(re.findall(r"(?:CMD|AI)_LINE_\d{3}", self.detail()))

        def newest():
            self.assertIn("AI_LINE_064", self.detail(), "L opens at the newest bottom")
            self.assertNotIn("CMD_LINE_000", self.detail())

        scroll = []
        for key in (tui.curses.KEY_PPAGE,) * 35 + (tui.curses.KEY_NPAGE,) * 35:
            scroll.extend([(key,), composer.Action(capture)])
        self.scenario(*self.command(command), composer.Check(self.assert_idle),
                      *self.ask("LONG_QUESTION"), composer.Check(self.assert_idle),
                      "L", composer.Action(newest), *scroll,
                      (tui.curses.KEY_PPAGE,) * 35,
                      composer.Action(lambda: self.assertIn("CMD_LINE_000", self.detail())))
        self.assertEqual(seen, {"{}_LINE_{:03d}".format(kind, n)
                                for kind in ("CMD", "AI") for n in range(65)})
        top = self.detail()
        self.scenario(" ", composer.Action(lambda: self.assertNotEqual(self.detail(), top)),
                      "\x1bL", composer.Action(newest))

    def test_log_shows_live_updates_and_leaving_it_keeps_output_dismissed(self):
        self.board()
        rule = self.gated(["session", "show", "LIVE_LOG"], stdout="LIVE_LOG_EARLY\n", late="LIVE_LOG_DONE\n")
        self.scenario(*self.command("aoe session show LIVE_LOG"),
                      composer.Check(lambda: self.assertIn("LIVE_LOG_EARLY", self.detail())),
                      "jL", composer.Action(self.assert_log_open),
                      composer.Action(lambda: self.assertIn("LIVE_LOG_EARLY", self.log_text())),
                      "j", composer.Action(lambda: self.assertEqual(self.app.selected()["sid"], "live-2222")),
                      composer.Action(lambda: Path(rule["release"]).touch()), composer.Check(self.assert_idle),
                      composer.Action(lambda: self.assertIn("LIVE_LOG_DONE", self.log_text())),
                      "\x1bg", composer.Check(self.assert_idle),
                      composer.Action(lambda: self.assert_item("live-2222", "LIVE_LOG_DONE")),
                      "L", composer.Action(lambda: self.assertIn("LIVE_LOG_DONE", self.log_text())))

    def test_log_is_local_to_inbox_instance_and_is_not_written_to_chief_storage(self):
        command = self.output_rule("NO_PERSIST_COMMAND", "NO_PERSIST_OUTPUT\n")
        self.reply("NO_PERSIST_ANSWER")
        stored = {}

        def storage():
            return {str(path.relative_to(chief.ROOT)): path.read_bytes()
                    for path in chief.ROOT.rglob("*") if path.is_file()}

        self.scenario(composer.Action(lambda: stored.update(storage())),
                      *self.command(command), self.completed("NO_PERSIST_OUTPUT"),
                      *self.ask("NO_PERSIST_QUESTION"), self.answered_with("NO_PERSIST_ANSWER"),
                      "L", composer.Action(lambda: self.assertIn("NO_PERSIST_OUTPUT", self.log_text())), "\x1b")
        self.assertEqual(storage(), stored)
        self.app = tui.Inbox(self.cfg, chief, 3600)
        self.scenario("L", composer.Action(self.assert_log_open),
                      composer.Action(lambda: self.assertNotIn("NO_PERSIST_", self.log_text())))


class SafetyAndSuggestionTests(UnifiedLogFixture):
    def test_reading_scrolling_refreshing_and_enter_in_log_never_submit_any_operation(self):
        self.board()
        command = self.output_rule("READ_ONLY_LOG", "LOG_TEXT: chief nudge task-alpha 'DO_NOT_SEND'\n")
        self.reply("READ_ONLY_ANSWER", ["aoe session stop live-1111"])
        self.scenario(*self.command(command), self.completed("DO_NOT_SEND"),
                      *self.ask("READ_ONLY_QUESTION"), self.answered_with("READ_ONLY_ANSWER"))
        observed = self.operations()
        self.scenario("L", composer.Action(self.assert_log_open),
                      (tui.curses.KEY_PPAGE, tui.curses.KEY_NPAGE), " \n\r", (tui.curses.KEY_ENTER,),
                      "g", composer.Check(self.assert_idle), "\x1bL",
                      composer.Action(lambda: self.assert_no_new_operations(observed)),
                      composer.Action(lambda: self.assertIsNone(self.app.st["input"])),
                      composer.Action(lambda: self.assertIsNone(self.app.st["confirm"])))

    def test_command_text_stdout_stderr_and_errors_are_not_added_to_ai_context(self):
        command = self.output_rule("CONTEXT_COMMAND_MARKER", "CONTEXT_STDOUT_MARKER\n",
                                   stderr="CONTEXT_STDERR_MARKER\n", code=23)
        self.reply("CONTEXT_SAFE_ANSWER")
        self.scenario(*self.command(command), self.completed("CONTEXT_STDOUT_MARKER"),
                      *self.command("show --CONTEXT_INVALID_FLAG"), composer.Check(self.assert_idle),
                      "L", "\x1b", *self.ask("CONTEXT_FIRST_QUESTION"), self.answered_with("CONTEXT_SAFE_ANSWER"),
                      *self.ask("CONTEXT_SECOND_QUESTION"), self.answered_with("CONTEXT_SAFE_ANSWER"))
        self.assertEqual(len(self.traces("generate_ai")), 2)
        for trace in self.traces("generate_ai"):
            for token in ("CONTEXT_COMMAND_MARKER", "CONTEXT_STDOUT_MARKER", "CONTEXT_STDERR_MARKER",
                          "CONTEXT_INVALID_FLAG"):
                self.assertNotIn(token, trace["prompt"], "the combined log is not an AI context source")

    def test_each_answer_retains_its_own_validated_suggestions_and_latest_digit_only_prefills(self):
        first, second = "show live-1111 --turns 3", "show live-2222 --turns 8"
        self.reply("SUGGESTION_FIRST_ANSWER", [first, "aoe send live-1111 INVALID_SUGGESTION"])
        self.scenario(*self.ask("SUGGESTION_FIRST_QUESTION"), self.answered_with("SUGGESTION_FIRST_ANSWER"))
        self.reply("SUGGESTION_SECOND_ANSWER", [second])

        def attached():
            text = self.log_text()
            tokens = ("SUGGESTION_FIRST_ANSWER", first, "SUGGESTION_SECOND_QUESTION",
                      "SUGGESTION_SECOND_ANSWER", second)
            for token in tokens:
                self.assertIn(token, text)
            positions = [text.index(token) for token in tokens]
            self.assertEqual(positions, sorted(positions), "suggestions belong beside their own answer")
            self.assertNotIn("INVALID_SUGGESTION", text)

        self.scenario(*self.ask("SUGGESTION_SECOND_QUESTION"), self.answered_with("SUGGESTION_SECOND_ANSWER"),
                      "L", composer.Action(attached))
        observed = self.operations()
        self.scenario("1", composer.Action(lambda: self.assert_buffer(second)),
                      "\x15tasks", composer.Action(lambda: self.assert_buffer("tasks")),
                      composer.Action(lambda: self.assert_no_new_operations(observed)), "\x1b")

    def test_suggestion_from_log_is_editable_and_enter_still_requires_y_N_before_execution(self):
        self.reply("CONFIRM_FROM_LOG", ["aoe session stop live-1111"])
        self.scenario(*self.ask(), self.answered_with("CONFIRM_FROM_LOG"), "L1",
                      composer.Action(lambda: self.assert_buffer("aoe session stop live-1111")),
                      "\x15aoe session archive live-2222\n",
                      composer.Action(lambda: self.assertRegex(self.app.st["confirm"]["prompt"], r"y/N")),
                      "\x1b", composer.Check(self.assert_idle))
        self.assertNotIn(["session", "archive", "live-2222"], self.aoe_args())
        self.scenario("L1", composer.Action(lambda: self.assert_buffer("aoe session stop live-1111")),
                      "\x15aoe session stop live-2222\n",
                      composer.Action(lambda: self.assertRegex(self.app.st["confirm"]["prompt"], r"y/N")),
                      "y\n", self.completed("aoe completed session stop live-2222"), "L")
        text = self.log_text()
        self.assertEqual(text.count("aoe session archive live-2222"), 1, "declining retains the submitted entry")
        self.assertEqual(self.aoe_args().count(["session", "stop", "live-2222"]), 1)
        self.assertNotIn(["session", "stop", "live-1111"], self.aoe_args())

    def test_L_is_literal_in_input_and_escape_cancels_input_before_leaving_log(self):
        self.scenario("L", composer.Action(self.assert_log_open), ":show live-L",
                      composer.Action(lambda: self.assert_buffer("show live-L")), "\x1b",
                      composer.Action(self.assert_log_open), "?Explain L",
                      composer.Action(lambda: self.assert_buffer("Explain L")), "\x1b",
                      composer.Action(self.assert_log_open), "\x1b")
        self.assertFalse(self.ai_calls())
        self.assertFalse(self.traces("generate_ai"))

    def test_retained_text_keeps_ai_redaction_and_terminal_control_sanitization(self):
        command = self.output_rule("SANITIZED_COMMAND", "SAFE_STDOUT\x1b[31m\x00\x07\r\n",
                                   stderr="SAFE_STDERR\x1b[2J\x08\n")
        secret = "FixtureAnswerSecret9-value"
        self.reply("SAFE_AI_ANSWER password=" + secret + "\n\x1b[31m\x00\x07", ["tasks"])
        self.scenario(*self.command(command), self.completed("SAFE_STDOUT", "SAFE_STDERR"),
                      *self.ask("SAFE_QUESTION token=FixtureQuestionSecret9-value"),
                      self.answered_with("SAFE_AI_ANSWER"), "L")
        text = self.log_text()
        for marker in ("SAFE_STDOUT", "SAFE_STDERR", "SAFE_AI_ANSWER", "SAFE_QUESTION"):
            self.assertIn(marker, text)
        self.assertNotIn(secret, text)
        self.assertNotIn("FixtureQuestionSecret9-value", text)
        self.assertTrue(all(c in "\n\t" or c.isprintable() for c in text))


class ErrorAndCancellationLogTests(UnifiedLogFixture):
    def test_declined_confirmations_keep_cancelled_entries_without_executing_commands(self):
        commands = []
        for suffix, decline in (("ENTER", "\n"), ("NO", "n"), ("ESC", "\x1b")):
            command = "aoe session stop DECLINED_" + suffix
            commands.append(command)
            self.scenario(*self.command(command),
                          composer.Action(lambda: self.assertRegex(self.app.st["confirm"]["prompt"], r"y/N")),
                          decline, composer.Check(self.assert_idle))
        self.scenario("L", composer.Action(self.assert_log_open))
        text = self.log_text()
        for index, command in enumerate(commands):
            self.assertEqual(text.count(command), 1)
            end = text.index(commands[index + 1]) if index + 1 < len(commands) else len(text)
            part = text[text.index(command):end].lower()
            self.assertRegex(part, r"cancel|declin|not run|已取消|未执行")
            self.assertNotRegex(part, r"(?:exit status|exit code|退出码)[^\n]*\b0\b")
        self.assertFalse([args for args in self.aoe_args() if args[:2] == ["session", "stop"]])

    def test_parse_and_launch_errors_and_actual_exit_codes_survive_later_entries(self):
        command = self.output_rule("REAL_EXIT_COMMAND", "REAL_EXIT_STDOUT\n", stderr="REAL_EXIT_STDERR\n", code=17)
        self.scenario(*self.command("tasks --INVALID_LOG_FLAG"),
                      composer.Check(lambda: self.assertRegex(self.detail().lower(), r"error|invalid|unrecognized")))
        with mock.patch.dict(os.environ, {"AOE_BIN": str(self.root / "MISSING_LOG_EXECUTABLE")}):
            self.scenario(*self.command("aoe session show LAUNCH_ERROR_COMMAND"),
                          composer.Check(self.assert_idle),
                          composer.Action(lambda: self.assertRegex(self.detail().lower(), r"error|failed|no such file")))
        self.scenario(*self.command(command), self.completed("REAL_EXIT_STDOUT", "REAL_EXIT_STDERR"), "L")
        text = self.log_text()
        for token in ("INVALID_LOG_FLAG", "LAUNCH_ERROR_COMMAND", "MISSING_LOG_EXECUTABLE", "REAL_EXIT_STDOUT", "REAL_EXIT_STDERR"):
            self.assertIn(token, text)
        self.assertRegex(text.lower(), r"(?:exit|status|code|退出码)[^\n]*17")

    def test_localized_ai_errors_remain_with_their_question_after_successful_retry(self):
        for lang in ("en", "zh"):
            for kind in ("unavailable", "timeout", "failed", "unusable"):
                with self.subTest(lang=lang, kind=kind):
                    self.app = tui.Inbox(self.cfg, chief, 3600)
                    chief.LANG = lang
                    self.reply("ERROR_FIXTURE", code=19 if kind == "failed" else 0)
                    if kind == "unusable":
                        self.ai_control["output"] = "not JSON"
                        self.write_ai_control()
                    question = "FAILED_{}_QUESTION".format(kind.upper())
                    with mock.patch.dict(os.environ, {"AOE_CHIEF_LANG": lang,
                            "CHIEF_AI_BOUNDARY_FAILURE": kind if kind in ("unavailable", "timeout") else ""}):
                        self.scenario(*self.ask(question), composer.Check(self.assert_idle))
                    patterns = {"unavailable": (r"unavailable|not found|not installed", r"不可用|未找到|未安装"),
                                "timeout": (r"timed?\s*out|timeout", r"超时"),
                                "failed": (r"failed|failure|error", r"失败|错误"),
                                "unusable": (r"unusable|invalid|empty|malformed", r"无效|空")}
                    self.reply("RETRY_LOG_ANSWER")
                    self.scenario(*self.ask("RETRY_LOG_QUESTION"), self.answered_with("RETRY_LOG_ANSWER"), "L")
                    text = self.log_text()
                    self.assertIn(question, text)
                    self.assertIn("RETRY_LOG_QUESTION", text)
                    error_part = text[text.index(question):text.index("RETRY_LOG_QUESTION")]
                    self.assertRegex(error_part.lower(), patterns[kind][lang == "zh"])
                    self.assertRegex(error_part.lower(), r"retry|try again|重试")

    def test_escape_in_log_cancels_owned_command_first_and_discards_late_queue_results(self):
        self.board()
        rule = self.gated(["session", "show", "CANCEL_LOG_COMMAND"], stdout="CANCEL_LOG_STDOUT\n",
                          stderr="CANCEL_LOG_STDERR\n", late="STALE_LOG_STDOUT\n",
                          effect=str(self.root / "cancel-log-effect"))
        owned = {}

        def started():
            self.assert_started(rule)
            self.assertIn("CANCEL_LOG_STDOUT", self.detail())
            self.assertIn("CANCEL_LOG_STDERR", self.detail())
            owned["execution"] = self.app.execution

        def cancelled():
            self.assert_idle()
            self.assert_process_dead(int(Path(rule["started"]).read_text()))
            self.assert_log_open()  # First Esc cancels; second Esc leaves the log.
            self.assertRegex(self.log_text().lower(), r"cancel|已取消")

        def late():
            # Simulate buffered reader events arriving after ownership detached.
            execution = owned["execution"]
            for event in (("stdout", "STALE_LOG_STDOUT"), ("error", "STALE_LOG_ERROR"), ("done", 0)):
                execution.events.put(event)
            Path(rule["release"]).touch()

        next_command = self.output_rule("AFTER_CANCEL_COMMAND", "AFTER_CANCEL_OUTPUT\n")
        self.scenario(*self.command("aoe session show CANCEL_LOG_COMMAND"), composer.Check(started),
                      "L\x1b", composer.Check(cancelled), composer.Action(late), "\x1b",
                      composer.Action(lambda: self.assert_item("live-1111", "CANCEL_LOG_STDOUT")),
                      *self.command(next_command), self.completed("AFTER_CANCEL_OUTPUT"), "L")
        text = self.log_text()
        self.assertEqual(text.count("aoe session show CANCEL_LOG_COMMAND"), 1)
        self.assertIn("CANCEL_LOG_STDOUT", text)
        self.assertIn("CANCEL_LOG_STDERR", text)
        self.assertNotRegex(text, r"STALE_LOG_(?:STDOUT|ERROR)")
        self.assertFalse(Path(rule["effect"]).exists())

    def test_escape_in_log_cancels_ai_tree_first_and_late_answers_never_enter_log(self):
        self.board()
        rule = self.gated_ai("STALE_LOG_AI_ANSWER", descendant=True)
        owned = {}

        def started():
            self.assertTrue(Path(rule["started"]).exists())
            self.assertTrue(Path(rule["descendant"]).exists())
            owned["execution"] = self.app.ai_execution

        def cancelled():
            self.assert_idle()
            self.assert_process_dead(int(Path(rule["started"]).read_text()))
            self.assert_process_dead(int(Path(rule["descendant"]).read_text()))
            self.assert_process_dead(self.ai_calls()[-1]["ppid"])
            self.assert_log_open()
            self.assertRegex(self.log_text().lower(), r"cancel|已取消")

        def late():
            execution = owned["execution"]
            execution.events.put(("answer", {"answer": "STALE_LOG_AI_ANSWER", "suggestions": ["tick"]}))
            execution.events.put(("ai_error", "unavailable"))
            execution.events.put(("done", 0))
            Path(rule["release"]).touch()

        self.scenario(*self.ask("CANCEL_LOG_AI_QUESTION"), composer.Check(started), "L\x1b",
                      composer.Check(cancelled), composer.Action(late), "\x1b",
                      composer.Action(lambda: self.assert_item("live-1111", "CANCEL_LOG_AI_QUESTION")),
                      composer.Action(lambda: self.reply("AFTER_CANCEL_AI_ANSWER")),
                      *self.ask("AFTER_CANCEL_AI_QUESTION"), self.answered_with("AFTER_CANCEL_AI_ANSWER"), "L")
        text = self.log_text()
        self.assertEqual(text.count("CANCEL_LOG_AI_QUESTION"), 1)
        self.assertIn("AFTER_CANCEL_AI_ANSWER", text)
        self.assertNotIn("STALE_LOG_AI_ANSWER", text)
        self.assertFalse(Path(rule["effect"]).exists())


class UnifiedLogDocumentationTests(unittest.TestCase):
    def test_readme_explains_combined_retention_transient_details_and_L_navigation(self):
        root = Path(__file__).resolve().parent.parent
        text = read_documentation()
        section = re.split(r"\n#{2,3} ", text.split("### 📥 The inbox", 1)[1], maxsplit=1)[0]
        self.assertRegex(section, r"`L`")
        flat = re.sub(r"\s+", " ", section).lower()
        for pattern in (r"(?:combined|unified|shared).*log", r"20", r"chronolog|submission order",
                        r"memory|in.memory|process.local", r"not (?:saved|persisted)|between launches",
                        r"select\w*.*(?:dismiss|hid|restor)", r"(?:newest|latest|bottom)",
                        r"esc.*(?:leav|hid|return|clos)", r"pgup.*pgdn", r"space",
                        r"command.*(?:not|never|exclud).*context|context.*(?:not|never|exclud).*command"):
            self.assertRegex(flat, pattern)
        self.assertNotRegex(flat, r"latest 12 questions")

    def test_english_and_chinese_inbox_help_explain_L_retention_and_selection_dismissal(self):
        for lang in ("en", "zh"):
            with self.subTest(lang=lang), mock.patch.object(chief, "LANG", lang):
                output = io.StringIO()
                with mock.patch("sys.stdout", output), self.assertRaises(SystemExit) as exited:
                    chief.argument_parser().parse_args(["inbox", "--help"])
                self.assertEqual(exited.exception.code, 0)
                text = re.sub(r"\s+", " ", output.getvalue())
                self.assertRegex(text, r"\bL\b", "uppercase L is the combined-log shortcut")
                for token in ("20", "Esc", "PgUp", "PgDn"):
                    self.assertIn(token, text)
                for pattern in ((r"log|history", r"command", r"AI", r"memory|process", r"newest|bottom|latest",
                                 r"select\w*.*(?:dismiss|hid|restor)") if lang == "en" else
                                (r"日志|历史|记录", r"命令", r"AI|问答|提问", r"内存|进程", r"最新|底部",
                                 r"(?:切换|选择).*(?:隐藏|关闭|详情|收起)")):
                    self.assertRegex(text, pattern)


class UnifiedLogFooterTests(UnifiedLogFixture):
    def test_localized_footers_expose_L_limit_and_transient_dismissal(self):
        for lang in ("en", "zh"):
            with self.subTest(lang=lang):
                self.app = tui.Inbox(self.cfg, chief, 3600)
                chief.LANG = lang

                def ordinary():
                    self.assertRegex(self.frame()[-1], r"\bL\b")
                    self.assertRegex(self.frame()[-1], r"log|history" if lang == "en" else r"日志|历史|记录")

                def transient():
                    footer = self.frame()[-1]
                    self.assertRegex(footer, r"\bL\b")
                    self.assertRegex(footer.lower(),
                                     r"(?:move|select|↑↓).*(?:detail|dismiss|hide)|"
                                     r"(?:detail|dismiss|hide).*(?:move|select|↑↓)" if lang == "en" else
                                     r"(?:选择|切换|↑↓).*(?:详情|隐藏|收起)|(?:详情|隐藏|收起).*(?:选择|切换|↑↓)")

                def history():
                    footer = self.frame()[-1]
                    self.assertIn("20", footer)
                    self.assertIn("Esc", footer)
                    self.assertRegex(footer.lower(), r"memory|session|process|launch" if lang == "en" else
                                     r"内存|进程|本次|启动")

                self.reply("FOOTER_ANSWER")
                self.scenario(composer.Action(ordinary), *self.ask("FOOTER_QUESTION"),
                              self.answered_with("FOOTER_ANSWER"), composer.Action(transient),
                              "L", composer.Action(history), "\x1b")

    def test_combined_log_renders_and_scrolls_in_half_width_English_and_Chinese_panes(self):
        self.reply("\n".join("NARROW_AI_{:02d} 雪".format(i) for i in range(45)))
        self.scenario(*self.ask("NARROW_QUESTION"), composer.Check(self.assert_idle))
        for lang in ("en", "zh"):
            for width, height in ((39, 12), (59, 18), (79, 28)):
                with self.subTest(lang=lang, width=width):
                    chief.LANG = lang
                    self.scr.getmaxyx.return_value = (height, width)

                    def narrow():
                        rows = tui.render(chief, self.app.st, width, height)
                        self.assertEqual(len(rows), height)
                        self.assertTrue(all(tui.width("".join(t for t, _ in row)) == width for row in rows))

                    self.scenario("L", composer.Action(narrow), (tui.curses.KEY_PPAGE, tui.curses.KEY_NPAGE),
                                  " ", composer.Action(narrow), "\x1b")


if __name__ == "__main__":
    unittest.main()
