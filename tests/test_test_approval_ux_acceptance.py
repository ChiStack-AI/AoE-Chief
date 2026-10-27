"""Acceptance contract for explicit test approval in Inbox and the CLI.

Real task/decision storage and temporary Git histories exercise the approval gate.
Only worker discovery, readiness, delivery and background scheduling are replaced;
no AI, terminal, dependency or external session is needed.
"""
from contextlib import contextmanager, ExitStack
import copy
from unittest import mock

import chief
import chief_tui as tui
from test_intent_task_flow import ChiefFixture


CONSENT = (
    "ok", "okay", "yes", "y", "lgtm", "approve", "approved", "looks good", "go ahead",
    "可以", "好", "好的", "行", "同意", "批准", "没问题",
    "OK", "Okay", "YeS", "Y", "LGTM", "APPROVE", "Approved", "Looks Good", "GO AHEAD",
    "  (okay!)  ", "【好的。】", "okay 可以", "OK, yes!", "LGTM; approved",
    "looks good / go ahead", "可以，没问题！", "批准；OKAY。", "yes\n可以",
)
FEEDBACK = (
    "ok but rename the test", "okay, but add edge cases", "not ok", "not approved",
    "okayish", "token", "yesplease", "yesterday", "approves", "approval", "unapproved",
    "looks good except the fixture", "go ahead and rename it", "yes no", "ok 1",
    "不可以", "不好", "好的，请改名称", "同意但需要补充", "批准修改", "没问题不过要修改",
    "ok可以", "好的好", "yesno", "👍", "...", "Add empty input coverage.",
)


class ApprovalFixture(ChiefFixture):
    """A pending test approval with existing protections and a committed proposal."""

    def setUp(self):
        super().setUp()
        for path, content in {
            "tests/test_existing.py": "# existing acceptance coverage\n" * 12,
            "tests/helpers/existing.py": "# existing protected helper\n" * 12,
            "tests/fixtures/existing.json": '{"baseline": true}\n',
            "test/existing.txt": "existing singular test directory\n",
            "qa/tests/existing_helper.py": "# existing custom test helper\n" * 12,
            "config/production.json": '{"protected": true}\n',
            "src/application.py": "# application\n",
        }.items():
            self.write(path, content)
        self.git("add", ".")
        self.git("commit", "-q", "-m", "protected baseline")
        self.base = self.git("rev-parse", "HEAD")
        self.listed = "tests/test_acceptance.py"
        self.write(self.listed, "# proposed failing acceptance coverage\n")
        self.git("add", self.listed)
        self.git("commit", "-q", "-m", "propose acceptance tests")
        self.tests_head = self.git("rev-parse", "HEAD")
        self.task = dict(id="approval-ux", name="Approval UX", goal="Implement the requested behavior",
                         repo=str(self.repo), worktree=str(self.repo), base=self.base, scope=".",
                         session_id="0123456789abcdef", state="needs-you", phase="test", test_first=True,
                         protected=["tests", "qa/tests", "config/production.json"], autonomy="suggest")
        chief.save_task(self.task)
        self.log = {"turn": "done", "last_ts": chief.now_utc().isoformat(),
                    "footer": {"decision": "approve the acceptance tests " + self.listed}}
        self.item = dict(id=self.task["session_id"], task=self.task, status="idle", log=self.log,
                         signals=[])
        self.proposal = copy.deepcopy(chief.sync_decisions(self.cfg, self.task, self.item))
        self.reset_waiting()
        self.patch_object(chief, "generate_ai", side_effect=AssertionError("consent must be deterministic"))

    def patch_object(self, target, name, **kwargs):
        patch = mock.patch.object(target, name, **kwargs)
        result = patch.start()
        self.addCleanup(patch.stop)
        return result

    def reset_waiting(self):
        chief.save_task(copy.deepcopy(self.task))
        self.decision = copy.deepcopy(self.proposal)
        chief.save_decisions(self.task["id"], [self.decision])
        self.item["decision"] = self.decision

    @contextmanager
    def ready_worker(self, delivered=True, turn=None, readiness="ready"):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(chief, "aoe_records", return_value={
                self.task["session_id"]: {"id": self.task["session_id"], "project_path": str(self.repo)}}))
            stack.enter_context(mock.patch.object(chief, "transcript", return_value=turn or self.log))
            stack.enter_context(mock.patch.object(chief, "wait_ready", return_value=readiness))
            deliver = stack.enter_context(mock.patch.object(chief, "deliver", return_value=delivered))
            yield deliver

    def inbox(self, item=None):
        app = tui.Inbox(self.cfg, chief, 15)
        app.st["entries"] = [tui.entry(chief, self.item if item is None else item)]
        # Keep key dispatch and callbacks real, while executing jobs synchronously.
        self.patch_object(app, "job", side_effect=lambda label, fn: fn())
        return app

    def submit_reply(self, app, text):
        app.key("r")
        self.assertIsNotNone(app.st["input"])
        app.st["input"]["buf"] = text
        app.typed("\n")

    def assert_test_contract(self):
        current = chief.load_task(self.task["id"])
        for key in ("phase", "base", "protected"):
            self.assertEqual(current[key], self.task[key])


class ApprovalPresentationTests(ApprovalFixture):
    def test_footer_and_detail_explain_approval_and_feedback_in_both_languages(self):
        for lang, label in (("en", "a approve tests"), ("zh", "a 批准测试")):
            with self.subTest(language=lang), mock.patch.object(chief, "LANG", lang):
                entry = tui.entry(chief, self.item)
                actions = tui.keys_for(chief, entry)
                self.assertEqual([action for action in actions if action.startswith("a ")], [label])
                detail = "\n".join(text for _, text in entry["detail"])
                self.assertIn(self.listed, detail)
                if lang == "en":
                    self.assertRegex(detail.lower(), r"\ba\b[^\n]*approv[^\n]*(?:lock|protect)[^\n]*build")
                    self.assertRegex(detail.lower(), r"\br\b[^\n]*feedback[^\n]*test phase")
                else:
                    self.assertRegex(detail, r"a[^\n]*批准[^\n]*(?:锁定|保护)[^\n]*(?:构建|实现|build)")
                    self.assertRegex(detail, r"r[^\n]*反馈[^\n]*测试阶段")
                app = self.inbox()
                rows = tui.render(chief, app.st, 220, 40)
                self.assertIn(label, "".join(text for text, _ in rows[-1]))

    def test_ordinary_draft_keeps_generic_accept_label(self):
        for lang, label in (("en", "a accept"), ("zh", "a 采纳")):
            with self.subTest(language=lang), mock.patch.object(chief, "LANG", lang):
                item = copy.deepcopy(self.item)
                item["decision"] = dict(self.decision, kind="ordinary", draft="Decision: A", question="A or B?")
                self.assertIn(label, tui.keys_for(chief, tui.entry(chief, item)))

    def test_decide_help_describes_test_approval_and_feedback_in_both_languages(self):
        for lang in ("en", "zh"):
            with self.subTest(language=lang), mock.patch.object(chief, "LANG", lang):
                code, out, err = self.cli("decide", "--help")
                self.assertEqual(code, 0, err)
                prose = " ".join(out.split())
                if lang == "en":
                    self.assertRegex(prose.lower(), r"--accept[^\n]*approv[^\n]*tests")
                    self.assertRegex(prose.lower(), r"--reply[^\n]*feedback[^\n]*test phase")
                else:
                    self.assertRegex(prose, r"--accept.*批准.*测试")
                    self.assertRegex(prose, r"--reply.*反馈.*测试阶段")


class InboxConsentTests(ApprovalFixture):
    def test_complete_consent_phrases_prompt_without_sending_or_approving(self):
        for lang in ("en", "zh"):
            for reply in CONSENT:
                with self.subTest(language=lang, reply=reply), mock.patch.object(chief, "LANG", lang), \
                        mock.patch.object(chief, "accept_decision") as accept, \
                        mock.patch.object(chief, "reply_decision") as feedback:
                    app = self.inbox()
                    self.submit_reply(app, reply)
                    accept.assert_not_called()
                    feedback.assert_not_called()
                    self.assertIsNone(app.st["input"])
                    self.assertIsNotNone(app.st["confirm"], "complete consent needs an explicit second action")
                    prompt = app.st["confirm"]["prompt"].strip()
                    if lang == "en":
                        self.assertEqual(prompt, "Approve the tests? [y/N]")
                    else:
                        self.assertRegex(prompt, r"批准.*测试.*\[y/N\]")
                    self.assert_test_contract()

    def test_extra_words_substrings_and_nonconsent_send_feedback_without_prompting(self):
        for lang in ("en", "zh"):
            for reply in FEEDBACK:
                with self.subTest(language=lang, reply=reply), mock.patch.object(chief, "LANG", lang), \
                        mock.patch.object(chief, "accept_decision") as accept, \
                        mock.patch.object(chief, "reply_decision", return_value=True) as feedback:
                    app = self.inbox()
                    self.submit_reply(app, reply)
                    self.assertIsNone(app.st["confirm"])
                    accept.assert_not_called()
                    feedback.assert_called_once_with(self.cfg, self.decision, reply)

    def test_any_confirmation_answer_except_y_sends_original_feedback_and_keeps_test_phase(self):
        for lang in ("en", "zh"):
            for answer in ("n", "N", "\n", "\r", "\x1b", "x", "yes", "好"):
                with self.subTest(language=lang, answer=answer), mock.patch.object(chief, "LANG", lang):
                    self.reset_waiting()
                    app = self.inbox()
                    original = "Okay，可以!"
                    with self.ready_worker() as deliver, \
                            mock.patch.object(chief, "accept_decision", wraps=chief.accept_decision) as accept:
                        self.submit_reply(app, original)
                        self.assertIsNotNone(app.st["confirm"])
                        deliver.assert_not_called()
                        app.answered(answer)
                    accept.assert_not_called()
                    self.assertIsNone(app.st["confirm"])
                    self.assert_test_contract()
                    deliver.assert_called_once()
                    message = deliver.call_args[0][2]
                    self.assertIn(original, message)
                    self.assertIn("Remain in the test phase", message)
                    self.assertNotEqual(chief.find_decision(self.decision["id"])["resolution"], "accepted")

    def test_a_and_confirmation_y_share_real_acceptance_validation_and_delivery(self):
        for lang in ("en", "zh"):
            for action in ("a", "y", "Y"):
                for delivered in (True, False):
                    with self.subTest(language=lang, action=action, delivered=delivered), \
                            mock.patch.object(chief, "LANG", lang):
                        self.reset_waiting()
                        app = self.inbox()
                        with self.ready_worker(delivered=delivered) as deliver, \
                                mock.patch.object(chief, "accept_decision", wraps=chief.accept_decision) as accept, \
                                mock.patch.object(chief, "reply_decision") as feedback:
                            if action == "a":
                                app.key("a")
                            else:
                                self.submit_reply(app, "lgtm")
                                deliver.assert_not_called()
                                self.assertIsNotNone(app.st["confirm"])
                                app.answered(action)
                        accept.assert_called_once()
                        self.assertEqual(accept.call_args[0][0], self.cfg)
                        self.assertEqual(accept.call_args[0][1]["id"], self.decision["id"])
                        feedback.assert_not_called()
                        deliver.assert_called_once()
                        updated = chief.load_task(self.task["id"])
                        self.assertEqual(updated["phase"], "build")
                        self.assertEqual(updated["base"], self.tests_head)
                        self.assertIn(self.listed, updated["protected"])
                        self.assertEqual(updated["state"], "running" if delivered else "needs-you")
                        resolved = chief.find_decision(self.decision["id"])
                        self.assertEqual(resolved["resolution"], "accepted")
                        self.assertIs(resolved["delivered"], delivered)

    def test_confirmed_y_preserves_dirty_file_and_stale_turn_validation(self):
        for failure in ("dirty", "stale", "not-ready"):
            with self.subTest(failure=failure):
                self.reset_waiting()
                self.write(self.listed, "# proposed failing acceptance coverage\n")
                turn = dict(self.log, last_ts="newer-turn") if failure == "stale" else self.log
                app = self.inbox()
                with self.ready_worker() as deliver:
                    self.submit_reply(app, "approved")
                    self.assertIsNotNone(app.st["confirm"])
                    deliver.assert_not_called()
                    if failure == "dirty":
                        self.write(self.listed, "# changed while confirmation was open\n")
                    # Recheck the contract/session when y arrives, after a change
                    # that happened while the user was viewing the confirmation.
                    with mock.patch.object(chief, "transcript", return_value=turn), \
                            mock.patch.object(chief, "wait_ready", return_value=(
                                "stopped" if failure == "not-ready" else "ready")), \
                            self.assertRaises(RuntimeError):
                        app.answered("y")
                    deliver.assert_not_called()
                self.assert_test_contract()
                self.assertEqual(chief.find_decision(self.decision["id"])["state"],
                                 "superseded" if failure == "stale" else "pending")

    def test_consent_in_other_reply_contexts_never_prompts(self):
        for lang in ("en", "zh"):
            for context in ("ordinary-decision", "task-guidance", "session"):
                with self.subTest(language=lang, context=context), mock.patch.object(chief, "LANG", lang), \
                        mock.patch.object(chief, "accept_decision") as accept, \
                        mock.patch.object(chief, "reply_decision", return_value=True) as feedback, \
                        mock.patch.object(chief, "nudge", return_value=True) as nudge, \
                        mock.patch.object(chief, "reply_session", return_value=True) as session:
                    item = copy.deepcopy(self.item)
                    if context == "ordinary-decision":
                        item["decision"] = dict(self.decision, kind="ordinary", question="Keep the parser?")
                        chief.save_decisions(self.task["id"], [item["decision"]])
                    else:
                        item.pop("decision")
                        if context == "session":
                            item["task"] = {}
                    app = self.inbox(item)
                    self.submit_reply(app, "okay 可以")
                    self.assertIsNone(app.st["confirm"])
                    accept.assert_not_called()
                    [feedback, nudge, session][("ordinary-decision", "task-guidance", "session").index(context)].assert_called_once()
        self.reset_waiting()


class CliConsentTests(ApprovalFixture):
    def test_cli_consent_is_feedback_with_one_localized_accept_hint_and_no_prompt(self):
        for lang in ("en", "zh"):
            for reply in CONSENT:
                with self.subTest(language=lang, reply=reply), mock.patch.object(chief, "LANG", lang):
                    self.reset_waiting()
                    with self.ready_worker() as deliver, \
                            mock.patch("builtins.input", side_effect=AssertionError("CLI reply must not prompt")), \
                            mock.patch.object(chief, "accept_decision") as accept:
                        code, out, err = self.cli("decide", self.decision["id"], "--reply", reply)
                    self.assertEqual(code, 0, err)
                    accept.assert_not_called()
                    self.assert_test_contract()
                    deliver.assert_called_once()
                    self.assertIn(reply, deliver.call_args[0][2])
                    self.assertIn("Remain in the test phase", deliver.call_args[0][2])
                    hints = [line for line in out.splitlines() if "--accept" in line]
                    self.assertEqual(len(hints), 1, out)
                    self.assertRegex(hints[0], r"(?i)approv.*tests" if lang == "en" else r"批准.*测试")
                    self.assertNotEqual(chief.find_decision(self.decision["id"])["resolution"], "accepted")

    def test_cli_nonconsent_feedback_does_not_print_consent_hint(self):
        for lang in ("en", "zh"):
            for reply in ("ok but rename the test", "不可以", "okayish"):
                with self.subTest(language=lang, reply=reply), mock.patch.object(chief, "LANG", lang):
                    self.reset_waiting()
                    with self.ready_worker() as deliver, mock.patch("builtins.input") as ask:
                        code, out, err = self.cli("decide", self.decision["id"], "--reply", reply)
                    self.assertEqual(code, 0, err)
                    self.assertNotIn("--accept", out)
                    ask.assert_not_called()
                    self.assert_test_contract()
                    self.assertIn(reply, deliver.call_args[0][2])

    def test_cli_consent_on_an_ordinary_decision_has_no_test_approval_hint(self):
        decision = dict(self.decision, kind="ordinary", question="Keep the parser?")
        chief.save_decisions(self.task["id"], [decision])
        with self.ready_worker() as deliver, mock.patch("builtins.input") as ask:
            code, out, err = self.cli("decide", decision["id"], "--reply", "approved")
        self.assertEqual(code, 0, err)
        self.assertNotIn("--accept", out)
        ask.assert_not_called()
        self.assert_test_contract()
        deliver.assert_called_once()

    def test_cli_consent_outside_test_phase_remains_an_ordinary_reply(self):
        chief.save_task(dict(self.task, phase="build"))
        with self.ready_worker() as deliver, mock.patch("builtins.input") as ask, \
                mock.patch.object(chief, "accept_decision") as accept:
            code, out, err = self.cli("decide", self.decision["id"], "--reply", "okay 可以")
        self.assertEqual(code, 0, err)
        self.assertNotIn("--accept", out)
        ask.assert_not_called()
        accept.assert_not_called()
        deliver.assert_called_once()
        updated = chief.load_task(self.task["id"])
        self.assertEqual(updated["phase"], "build")
        self.assertEqual(updated["base"], self.base)
        self.assertEqual(updated["protected"], self.task["protected"])
