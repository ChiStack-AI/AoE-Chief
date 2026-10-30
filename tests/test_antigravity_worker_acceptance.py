"""Acceptance contract for agy workers; all homes, sessions and content are synthetic.

Exercise existing public Chief entry points, without prescribing new parser helpers.
Loading a private module under a temporary HOME also isolates import-time paths.
"""
from contextlib import ExitStack
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / "chief.py"
CONVERSATION = "00000000-0000-4000-8000-000000000001"
OTHER_CONVERSATION = "00000000-0000-4000-8000-000000000002"
READY = "\n> \n? for shortcuts\n"
TRUST = ("Do you trust the contents of this project?\n"
         "> Yes, I trust this folder\n"
         "  No, exit\n")
EDIT = ("Accept this file edit?\n"
        "> 1. Yes, accept this change\n"
        "  2. No, reject this change\n")
COMMAND = ("Run this command?\n"
           "> 1. Yes, run command\n"
           "  2. Yes, and always allow in this conversation for commands that start with python3\n"
           "  3. Yes, and always allow (Persist to settings.json)\n")
FINAL = ("Conclusion: Synthetic parser work is ready for review.\n"
         "Evidence: synthetic checks ran\n"
         "Blockers: none\n"
         "Next step: review the result\n"
         "Decision needed: none")
QUESTION = "Choose the output format. Options: A) JSON B) text. Recommendation: A"


def step(index, kind, content, **extra):
    return dict(step_index=index, source="USER_EXPLICIT" if kind == "USER_INPUT" else "MODEL",
                type=kind, status="COMPLETED", created_at="2026-01-01T00:00:{:02d}Z".format(index),
                content=content, **extra)


def prompt(index, text):
    return step(index, "USER_INPUT", "<USER_REQUEST>\n" + text + "\n</USER_REQUEST>")


def reply(index, text, **extra):
    return step(index, "PLANNER_RESPONSE", text, input_tokens=900000,
                cache_read_tokens=800000, output_tokens=100, **extra)


class AntigravityFixture(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.home = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="chief-agy-test-")))
        stack.enter_context(mock.patch.dict(os.environ, {
            "HOME": str(self.home), "AOE_CHIEF_HOME": str(self.home / "chief-state"),
            "AOE_HOME": str(self.home / "aoe-state"), "AOE_CHIEF_LANG": "en",
            "CODEX_HOME": str(self.home / "codex"), "CLAUDE_CONFIG_DIR": str(self.home / "claude"),
        }))
        # No undocumented Antigravity override is needed to run these fixtures.
        os.environ.pop("ANTIGRAVITY_HOME", None)
        stack.enter_context(mock.patch.object(Path, "home", return_value=self.home))
        spec = importlib.util.spec_from_file_location("chief_agy_acceptance", SOURCE)
        self.ch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.ch)
        self.base = self.home / ".gemini" / "antigravity-cli"
        self.workspace = self.home / "worktrees" / "synthetic task"
        self.workspace.mkdir(parents=True)
        self.rec = {"id": "a" * 16, "tool": "antigravity", "project_path": str(self.workspace),
                    "title": "AoE placeholder", "status": "idle"}
        self.task = {"id": "synthetic-task", "name": "Synthetic task", "goal": "Fix synthetic parser",
                     "repo": str(self.workspace), "worktree": str(self.workspace), "tool": "antigravity",
                     "session_id": self.rec["id"], "state": "running", "phase": "build", "trust": True}
        self.cfg = {"profile": "test", "tracked_ids": [self.rec["id"]], "tasks": {},
                    "decision_ai": False, "aoe_colors": False, "review": False}
        self.path = self.write_conversation(CONVERSATION, self.rows())
        self.mapping = self.base / "cache" / "last_conversations.json"
        self.mapping.parent.mkdir(parents=True)
        self.mapping.write_text(json.dumps({str(self.workspace): CONVERSATION}), encoding="utf-8")
        self.ch.save_task(self.task)

    def rows(self, final=FINAL):
        return [prompt(0, "Fix the synthetic parser."), reply(1, "First turn finished."),
                prompt(2, "Now handle empty input.\nKeep Unicode: 测试."),
                reply(3, "Inspecting the fixture.", thinking="private synthetic reasoning",
                      tool_calls=[{"name": "read_file", "args": {"path": json.dumps("sample.py")}}]),
                step(4, "GENERIC", "Created At: 2026-01-01T00:00:03Z\n"
                     "Completed At: 2026-01-01T00:00:04Z\nSynthetic file contents"),
                reply(5, "Editing the fixture.", tool_calls=[{
                    "name": "edit_file", "args": {"path": json.dumps("sample.py"),
                                                   "content": json.dumps("return []\n")}}]),
                step(6, "GENERIC", "Created At: 2026-01-01T00:00:05Z\n"
                     "Completed At: 2026-01-01T00:00:06Z\nEdit applied"),
                reply(7, "Checking the fixture.", tool_calls=[{
                    "name": "run_command", "args": {"command": json.dumps("git status")}}]),
                step(8, "GENERIC", "Created At: 2026-01-01T00:00:07Z\n"
                     "Completed At: 2026-01-01T00:00:08Z\nCommand exited with code 128."),
                reply(9, final)]

    def write_conversation(self, uuid, rows, title='Synthetic "parser" task'):
        path = self.base / "brain" / uuid / ".system_generated" / "logs" / "transcript.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        annotation = self.base / "annotations" / (uuid + ".pbtxt")
        annotation.parent.mkdir(parents=True, exist_ok=True)
        annotation.write_text("title: " + json.dumps(title) + "\n", encoding="utf-8")
        return path

    def log(self):
        result = self.ch.transcript(self.rec)
        self.assertIsNotNone(result, "resolve agy by project_path without an agent_session_id")
        return result

    def item(self, log=None, screen=READY, status="idle"):
        return dict(id=self.rec["id"], title="Synthetic task", tool="antigravity", task=self.task,
                    path=str(self.workspace), status=status, log=log, screen=screen, changed=True,
                    checks={"passed": 0, "total": 1, "results": [
                        {"cmd": "python3 synthetic_check.py", "ok": False, "code": 1, "tail": "empty input fails"}]})

    def run_act(self, item):
        item["signals"] = self.ch.judge(item)
        with mock.patch.object(self.ch, "wait_ready", return_value="ready"), \
                mock.patch.object(self.ch, "deliver", return_value=True) as deliver, \
                mock.patch.object(self.ch, "rotate_task") as rotate:
            result = self.ch.act(self.cfg, item, self.task)
        return result, deliver, rotate


class AntigravityTranscriptAcceptanceTests(AntigravityFixture):
    def test_native_transcript_has_the_shared_contract_without_agent_session_id(self):
        log = self.log()
        self.assertTrue({"source", "goal", "title", "last_prompt", "last_reply", "footer", "turn",
                         "error", "tools", "last_ts", "turns_total"}.issubset(log))
        self.assertEqual(Path(log["source"]), self.path)
        self.assertEqual(log["goal"], "Fix the synthetic parser.")
        self.assertEqual(log["title"], 'Synthetic "parser" task')
        self.assertEqual(log["last_prompt"], "Now handle empty input.\nKeep Unicode: 测试.")
        self.assertEqual(log["last_reply"], FINAL)
        self.assertEqual(log["footer"], self.ch.parse_footer(FINAL))
        self.assertEqual(log["turn"], "done")
        self.assertEqual(log["error"], "", "a command exit 128 is not a failed agent turn")
        self.assertEqual(log["tools"], 3, "count calls, not their GENERIC results")
        self.assertEqual(log["last_ts"], "2026-01-01T00:00:09Z")
        self.assertEqual(log["turns_total"], 2)

    def test_workspace_mapping_wins_over_unrelated_session_id_or_newer_conversation(self):
        other_path = self.home / "worktrees" / "other-task"
        self.write_conversation(OTHER_CONVERSATION, [prompt(0, "Unrelated prompt"), reply(1, "Unrelated reply")])
        self.mapping.write_text(json.dumps({str(other_path): OTHER_CONVERSATION,
                                            str(self.workspace): CONVERSATION}), encoding="utf-8")
        self.rec["agent_session_id"] = OTHER_CONVERSATION
        self.assertEqual(self.log()["last_reply"], FINAL)

    def test_changed_workspace_mapping_is_reread(self):
        self.assertEqual(self.log()["last_reply"], FINAL)
        self.write_conversation(OTHER_CONVERSATION, [prompt(0, "Fresh session"), reply(1, "Fresh reply")])
        self.mapping.write_text(json.dumps({str(self.workspace): OTHER_CONVERSATION}), encoding="utf-8")
        self.assertEqual(self.log()["last_reply"], "Fresh reply")

    def test_missing_mapping_entry_does_not_fall_back_to_another_workspace(self):
        self.rec["project_path"] = str(self.home / "not-mapped")
        self.assertIsNone(self.ch.transcript(self.rec))

    def test_unavailable_or_partial_native_files_do_not_crash(self):
        for mode in ("missing-map", "malformed-map", "missing-transcript", "empty-transcript"):
            with self.subTest(mode=mode):
                self.mapping.write_text(json.dumps({str(self.workspace): CONVERSATION}), encoding="utf-8")
                self.write_conversation(CONVERSATION, self.rows())
                if mode == "missing-map":
                    self.mapping.unlink()
                elif mode == "malformed-map":
                    self.mapping.write_text('{"unfinished":', encoding="utf-8")
                elif mode == "missing-transcript":
                    self.path.unlink()
                else:
                    self.path.write_text("", encoding="utf-8")
                self.assertIsNone(self.ch.transcript(self.rec))

    def test_missing_optional_annotation_keeps_transcript_usable(self):
        (self.base / "annotations" / (CONVERSATION + ".pbtxt")).unlink()
        log = self.log()
        self.assertEqual(log["last_reply"], FINAL)
        self.assertEqual(log["title"], "")

    def test_partial_jsonl_tail_preserves_last_complete_step(self):
        with self.path.open("a", encoding="utf-8") as out:
            out.write('{"step_index":10,"content":')
        self.assertEqual(self.log()["last_reply"], FINAL)

    def test_new_user_input_starts_an_open_turn_without_stale_footer_or_tools(self):
        self.write_conversation(CONVERSATION, self.rows() + [prompt(10, "One more edge case")])
        log = self.log()
        self.assertEqual(log["turn"], "open")
        self.assertEqual(log["last_prompt"], "One more edge case")
        self.assertEqual(log["last_reply"], "")
        self.assertIsNone(log["footer"])
        self.assertEqual(log["tools"], 0)
        self.assertEqual(log["turns_total"], 3)
        self.assertEqual(log["last_ts"], "2026-01-01T00:00:10Z")

    def test_tool_step_and_result_do_not_finish_a_turn(self):
        for count in (8, 9):
            with self.subTest(rows=count):
                self.write_conversation(CONVERSATION, self.rows()[:count])
                log = self.log()
                self.assertEqual(log["turn"], "open")
                self.assertEqual(log["last_reply"], "Checking the fixture.")
                self.assertNotIn("Created At:", log["last_reply"])
                self.assertNotIn("private synthetic reasoning", log["last_reply"])

    def test_recent_turns_and_show_use_agy_history(self):
        turns = self.ch.recent_turns(self.rec, 2)
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["prompt"], "Fix the synthetic parser.")
        self.assertEqual(turns[-1]["replies"][-1], FINAL)
        self.assertEqual(turns[-1]["state"], "done")
        self.assertEqual(turns[-1]["tools"], 3)
        self.assertEqual(self.ch.recent_turns(self.rec, 1), turns[-1:])
        self.assertEqual(self.ch.recent_turns(self.rec, 0), [])
        with mock.patch.object(self.ch, "aoe_records", return_value={self.rec["id"]: self.rec}), \
                mock.patch.object(self.ch, "scope_info", return_value=None), \
                mock.patch.object(self.ch, "git_info", return_value=None), \
                mock.patch.object(self.ch, "screen", return_value=READY):
            shown = self.ch.show(self.cfg, self.rec["id"], 2)
        self.assertIn("Fix the synthetic parser.", shown)
        self.assertIn(FINAL, shown)
        self.assertNotIn("<USER_REQUEST>", shown)

    def test_transcript_redacts_synthetic_secrets(self):
        secret = "sk-" + "z" * 40
        self.write_conversation(CONVERSATION, [prompt(0, "api_key=" + secret), reply(1, "api_key=" + secret)])
        log = self.log()
        for key in ("goal", "last_prompt", "last_reply"):
            self.assertNotIn(secret, log[key])
        self.assertNotIn(secret, json.dumps(self.ch.recent_turns(self.rec, 1)))


class AntigravitySupervisionAcceptanceTests(AntigravityFixture):
    def test_collect_brief_and_decisions_consume_native_footer(self):
        final = FINAL.replace("Decision needed: none", "Decision needed: " + QUESTION)
        self.write_conversation(CONVERSATION, self.rows(final))
        with mock.patch.object(self.ch, "listings", return_value=[dict(self.rec, state="live")]), \
                mock.patch.object(self.ch, "aoe_records", return_value={self.rec["id"]: self.rec}), \
                mock.patch.object(self.ch, "aoe", return_value=json.dumps({"status": "idle"})), \
                mock.patch.object(self.ch, "screen", return_value=READY), \
                mock.patch.object(self.ch, "git_info", return_value=None), \
                mock.patch.object(self.ch, "scope_info", return_value=None):
            result = self.ch.collect(self.cfg, checks=False, persist=False)
        item = result["items"][0]
        self.assertIsNotNone(item["log"])
        self.assertEqual(item["decision"]["question"], QUESTION)
        self.assertEqual(self.ch.load_decisions(self.task["id"])[0]["turn_ts"], "2026-01-01T00:00:09Z")
        self.assertIn("decision", [s[2] for s in item["signals"]])
        brief = self.ch.basic(result)
        self.assertIn("Synthetic parser work is ready for review.", brief)
        self.assertIn(QUESTION, brief)
        digest = self.ch.digest(item, 2)
        self.assertEqual(digest["last_reply"], final)
        self.assertEqual(digest["agent_footer"]["decision"], QUESTION)
        _, deliver, rotate = self.run_act(item)
        deliver.assert_not_called()
        rotate.assert_not_called()

    def test_no_guessed_context_window_despite_large_token_counts(self):
        item = self.item(self.log())
        result, deliver, rotate = self.run_act(item)
        self.assertIsNotNone(result)
        deliver.assert_called_once()
        self.assertIn(self.ch.CONTINUE_MARK, deliver.call_args[0][2])
        rotate.assert_not_called()
        self.assertEqual(self.task["auto"]["continues"], 1)

    def test_explicit_context_percentage_allows_rotation(self):
        item = self.item(self.log(), screen=READY + "Context 85% used\n")
        _, deliver, rotate = self.run_act(item)
        rotate.assert_called_once()
        deliver.assert_not_called()

    def test_repeated_failures_across_native_turns_are_escalated_as_stuck(self):
        rows = self.rows()
        for index in range(3):
            if index:
                rows += [prompt(10 + index * 2, "Continue synthetic repair"), reply(11 + index * 2, FINAL)]
                self.write_conversation(CONVERSATION, rows)
            _, deliver, rotate = self.run_act(self.item(self.log()))
            rotate.assert_not_called()
            self.assertEqual(deliver.call_count, 1 if index < 2 else 0)
        self.assertEqual(self.task["state"], "needs-you")
        self.assertIn("stuck", self.task["note"])

    def test_prompt_only_turn_can_recover_once(self):
        self.write_conversation(CONVERSATION, self.rows() + [prompt(10, "Continue")])
        item = self.item(self.log())
        _, deliver, _ = self.run_act(item)
        deliver.assert_called_once()
        self.assertIn("previous turn ended without finishing", deliver.call_args[0][2])
        _, deliver, _ = self.run_act(item)
        deliver.assert_not_called()


class AntigravityScreenAcceptanceTests(AntigravityFixture):
    def wait(self, initial, trust, timeout=12, dismiss=True):
        clock, keys, accepted = [0.0], [], []
        selected = [0]
        is_trust = "Do you trust the contents" in initial
        if "\n> No, exit" in initial:
            selected[0] = 1

        def capture(*args):
            if accepted and dismiss:
                return READY
            if not is_trust:
                return initial
            return ("Do you trust the contents of this project?\n" +
                    (">" if selected[0] == 0 else " ") + " Yes, I trust this folder\n" +
                    (">" if selected[0] == 1 else " ") + " No, exit\n")

        def send(cmd, **kwargs):
            self.assertEqual(cmd[:4], ["tmux", "send-keys", "-t", "synthetic-terminal"])
            for key in cmd[4:]:
                keys.append(key)
                if key in ("Up", "Down"):
                    selected[0] = 1 - selected[0]
                elif key in ("Enter", "C-m"):
                    accepted.append(selected[0])
                else:
                    self.fail("Unexpected key: " + key)
            return ""

        def sleep(seconds):
            clock[0] += seconds

        with mock.patch.object(self.ch, "aoe", return_value=json.dumps({"status": "waiting"})), \
                mock.patch.object(self.ch, "screen", side_effect=capture), \
                mock.patch.object(self.ch, "tmux_target", return_value="synthetic-terminal"), \
                mock.patch.object(self.ch, "call", side_effect=send), \
                mock.patch.object(self.ch.time, "time", side_effect=lambda: clock[0]), \
                mock.patch.object(self.ch.time, "sleep", side_effect=sleep):
            result = self.ch.wait_ready(self.cfg, self.rec["id"], trust, timeout=timeout)
        return result, keys, accepted

    def test_exact_trust_question_is_recognized(self):
        self.assertIsNotNone(self.ch.TRUST_RE.search(TRUST))

    def test_ascii_selected_trust_option_is_recognized(self):
        options = list(self.ch.TRUST_OPTION_RE.finditer(TRUST))
        self.assertEqual(len(options), 2)
        self.assertTrue(options[0].group("selected"))
        self.assertTrue(options[0].group("yes"))

    def test_trust_disabled_never_sends_keys(self):
        result, keys, accepted = self.wait(TRUST, False)
        self.assertEqual(result, "trust")
        self.assertEqual(keys, [])
        self.assertEqual(accepted, [])

    def test_trust_enabled_confirms_yes_even_if_selection_moved_to_no(self):
        for menu in (TRUST, TRUST.replace("> Yes", "  Yes").replace("  No", "> No")):
            with self.subTest(menu=menu):
                result, _, accepted = self.wait(menu, True)
                self.assertEqual(accepted, [0], "only the affirmative trust choice may be confirmed")
                self.assertEqual(result, "ready")

    def test_observation_only_does_not_answer_trust(self):
        result, keys, _ = self.wait(TRUST, True, timeout=0)
        self.assertEqual(result, "trust")
        self.assertEqual(keys, [])

    def test_trust_is_confirmed_at_most_once_if_menu_persists(self):
        result, _, accepted = self.wait(TRUST, True, dismiss=False)
        self.assertEqual(accepted, [0])
        self.assertEqual(result, "trust")

    def test_approval_menus_are_waiting_even_if_aoe_reports_idle_and_footer_is_visible(self):
        for menu in (EDIT, COMMAND):
            for status in ("idle", "waiting"):
                with self.subTest(menu=menu, status=status):
                    screen = menu + READY
                    self.assertFalse(self.ch.at_prompt(status, screen))
                    item = self.item({"turn": "done", "last_reply": FINAL}, screen=screen, status=status)
                    signals = self.ch.judge(item)
                    self.assertIn(("red", "waiting"), [(lv, kind) for lv, _, kind in signals])
                    _, deliver, rotate = self.run_act(item)
                    deliver.assert_not_called()
                    rotate.assert_not_called()

    def test_trust_permission_never_authorizes_edit_or_command_approval(self):
        for menu in (EDIT, COMMAND):
            for trust in (False, True):
                with self.subTest(menu=menu, trust=trust):
                    result, keys, _ = self.wait(menu + READY, trust)
                    self.assertNotEqual(result, "ready")
                    self.assertEqual(keys, [])

    def test_idle_and_busy_footers(self):
        self.assertTrue(self.ch.at_prompt("idle", READY))
        self.assertTrue(self.ch.at_prompt("waiting", READY))
        self.assertFalse(self.ch.at_prompt("running", "esc to cancel\n"))
        self.assertFalse(self.ch.at_prompt("idle", "esc to cancel\n"))

    def test_prompt_words_in_prose_do_not_become_menus(self):
        for prose in ('The screen says "Run this command?" when approval is required.',
                      'Documentation: Accept this file edit?',
                      'Decision needed: Do you trust the contents of this project?'):
            with self.subTest(prose=prose):
                self.assertIsNone(self.ch.TRUST_RE.search(prose))
                self.assertTrue(self.ch.at_prompt("idle", prose + READY))


class AntigravityConfigurationAcceptanceTests(AntigravityFixture):
    def test_new_accepts_the_aoe_tool_name_and_launches_that_tool(self):
        parser = self.ch.argument_parser()
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            try:
                args = parser.parse_args(["new", "--tool", "antigravity", "--draft"])
            except SystemExit as error:
                self.fail("chief new must accept --tool antigravity (exit {})".format(error.code))
        self.assertEqual(args.tool, "antigravity")
        task = dict(self.task, branch="chief/synthetic-task")
        with mock.patch.object(self.ch, "aoe_bin", return_value="aoe"):
            cmd = self.ch.add_command(self.cfg, task, dry_run=True)
        self.assertEqual(cmd[cmd.index("--tool") + 1], "antigravity")
        self.assertNotIn("--cmd-override", cmd, "AoE already knows this built-in tool")

    def test_inbox_contract_preserves_antigravity_tool(self):
        contract = dict(self.task, checks=["python3 synthetic_check.py"], protected=["tests"],
                        budget={"rounds": 10, "hours": 2})
        contract.pop("id")
        validated = self.ch.validate_draft(contract)
        with mock.patch.object(self.ch, "call", return_value=str(self.workspace)):
            prepared = self.ch.prepare_new_task(validated, tid="another-synthetic-task")
        self.assertEqual(prepared["tool"], "antigravity")

    def test_engine_choices_remain_codex_and_claude(self):
        for prefix in (["brief", "--engine"], ["decide", "synthetic-task:1", "--engine"], ["engine"]):
            for engine in ("codex", "claude"):
                with self.subTest(prefix=prefix, engine=engine):
                    self.ch.argument_parser().parse_args(prefix + [engine])
            with self.subTest(prefix=prefix), mock.patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as exited:
                    self.ch.argument_parser().parse_args(prefix + ["antigravity"])
                self.assertEqual(exited.exception.code, 2)

    def test_antigravity_work_is_reviewed_by_an_existing_engine(self):
        for configured in (None, "codex", "claude"):
            with self.subTest(engine=configured):
                cfg = dict(self.cfg)
                if configured:
                    cfg["review_engine"] = configured
                with mock.patch.object(self.ch, "call", return_value=""), \
                        mock.patch.object(self.ch, "generate_ai", return_value=(
                            "Verdict: pass\nSummary: synthetic review\nRisks: none")) as generate:
                    self.ch.review_task(cfg, self.task, self.item())
                self.assertEqual(generate.call_args[0][1], configured or "codex")

    def test_english_and_chinese_new_help_advertise_the_worker(self):
        for language in ("en", "zh"):
            with self.subTest(language=language), mock.patch.object(self.ch, "LANG", language):
                output = io.StringIO()
                with mock.patch("sys.stdout", output), self.assertRaises(SystemExit) as exited:
                    self.ch.argument_parser().parse_args(["new", "--help"])
                self.assertEqual(exited.exception.code, 0)
                text = re.sub(r"\s+", " ", output.getvalue())
                self.assertRegex(text, r"--tool\s+\{[^}]*antigravity[^}]*\}")

    def test_readme_documents_worker_and_native_storage(self):
        readme = (SOURCE.parent / "README.md").read_text(encoding="utf-8")
        self.assertTrue("--tool antigravity" in readme, "README must show --tool antigravity")
        self.assertTrue(re.search(r"(?i)Antigravity.*agy|agy.*Antigravity", readme),
                        "README must identify Antigravity CLI as agy")
        for path in ("~/.gemini/antigravity-cli", "last_conversations.json", "brain/",
                     ".system_generated/logs/transcript.jsonl", "annotations/", ".pbtxt"):
            self.assertTrue(path in readme, "README must document " + path)


if __name__ == "__main__":
    unittest.main()
