"""Real-Git acceptance tests for test-phase out-of-scope membership only.

Scope filtering must preserve protection/review signals and explicit approval.
Only external worker discovery and transcript/screen I/O are mocked for display.
"""
from unittest import mock

import chief
import chief_tui as tui
from test_intent_task_flow import ChiefFixture


class TestPhaseScopeNoiseTests(ChiefFixture):
    def setUp(self):
        super().setUp()
        for path in ("tests/test_shop.py", "tests/test_locked.py",
                     "tests/helpers/existing.py", "tests/fixtures/existing.json",
                     "test/feature.spec.ts", "qa/specs/shop.spec.ts",
                     "qa/cases/test_existing.py", "qa/misc/existing.txt",
                     "loose/test_outside.py", "src/application.py", "lib/module.py",
                     "config/settings.json", "README.md"):
            self.write(path, "original contents for " + path + "\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "scope baseline")
        self.task = dict(id="scope-noise", name="Shop", goal="Implement shop behavior",
                         repo=str(self.repo), worktree=str(self.repo),
                         base=self.git("rev-parse", "HEAD"), scope="src", protected=["tests"],
                         phase="test", test_first=True, state="needs-you", autonomy="suggest",
                         session_id="0123456789abcdef")
        self.log = dict(turn="done", last_ts=chief.now_utc().isoformat(), footer={})

    def git_states(self):
        yield "unstaged"
        self.git("add", "-A")
        yield "staged"
        self.git("commit", "-q", "-m", "acceptance changes since scope base")
        yield "committed"

    def phase_contracts(self, task=None):
        task = self.task if task is None else task
        yield "explicit", dict(task)
        explicit_only = dict(task)
        explicit_only.pop("test_first")
        yield "explicit-without-test-first", explicit_only
        implicit = dict(task)
        implicit.pop("phase")
        yield "implicit", implicit

    def scope(self, task=None, extra_ignore=(), path=None):
        task = self.task if task is None else task
        path = str(self.repo) if path is None else str(path)
        scope = chief.scope_info(path, task, extra_ignore)
        self.assertIsNotNone(scope, "real Git scope discovery must succeed")
        # A scope boundary changes only membership, never the other dictionary fields.
        unrestricted = chief.scope_info(path, dict(task, scope="."), extra_ignore)
        self.assertIsNotNone(unrestricted)
        self.assertEqual(dict(scope, out_of_scope=[]), dict(unrestricted, out_of_scope=[]))
        return scope

    def item(self, task=None, log=None):
        task = self.task if task is None else task
        item = dict(id=task["session_id"], title=task["name"], task=task, status="idle",
                    log=self.log if log is None else log, scope=self.scope(task), changed=True)
        item["signals"] = chief.judge(item)
        return item

    def collect(self, task, log):
        chief.save_task(task)
        sid = task["session_id"]
        record = dict(id=sid, project_path=str(self.repo), status="idle", tool="codex")
        cfg = dict(self.cfg, aoe_colors=False)
        with mock.patch.object(chief, "listings", return_value=[dict(id=sid, state="live")]), \
                mock.patch.object(chief, "aoe_records", return_value={sid: record}), \
                mock.patch.object(chief, "aoe", return_value='{"status": "idle"}'), \
                mock.patch.object(chief, "transcript", return_value=log), \
                mock.patch.object(chief, "screen", return_value=None):
            return chief.collect(cfg, checks=False, persist=False)

    def test_pending_modified_shop_test_has_no_scope_warning_in_brief_or_inbox(self):
        self.write("tests/test_shop.py", "# revised acceptance test\n")
        question = "approve the acceptance tests tests/test_shop.py"
        log = dict(self.log, footer={"decision": question})
        original_base = self.task["base"]
        for state in self.git_states():
            for lang in ("en", "zh"):
                with self.subTest(state=state, language=lang), mock.patch.object(chief, "LANG", lang):
                    result = self.collect(self.task, log)
                    item = result["items"][0]
                    decision = chief.find_decision(item["decision"]["id"])
                    self.assertEqual(decision["kind"], "test-approval")
                    self.assertEqual(decision["state"], "pending")
                    self.assertEqual(decision["size"], "big")
                    self.assertFalse(decision.get("draft"))
                    self.assertEqual(decision["files"], ["tests/test_shop.py"])
                    current = chief.load_task(self.task["id"])
                    self.assertEqual(current["phase"], "test")
                    self.assertEqual(current["base"], original_base)
                    self.assertEqual(current["protected"], ["tests"])
                    self.assertEqual(item["scope"]["changed"], 1)
                    self.assertEqual(item["scope"]["protected_touched"], [])
                    self.assertEqual(item["scope"]["protected_violations"], [])
                    entry = tui.entry(chief, item)
                    self.assertEqual(entry["level"], "red", "approval must still need the user")
                    self.assertIn("a approve tests" if lang == "en" else "a 批准测试",
                                  tui.keys_for(chief, entry))
                    app = tui.Inbox(self.cfg, chief, 15)
                    app.st["entries"] = [entry]
                    inbox = "\n".join("".join(text for text, _ in row)
                                      for row in tui.render(chief, app.st, 240, 60))
                    warning = "out of scope" if lang == "en" else "超出范围"
                    brief = chief.basic(result)
                    for surface, text in (("brief", brief), ("inbox", inbox)):
                        with self.subTest(surface=surface):
                            self.assertIn("tests/test_shop.py", text)
                            self.assertFalse(warning in text, surface + " still displays the out-of-scope warning")
                    self.assertFalse(any(level == "yellow" and kind == "scope"
                                         for level, _, kind in item["signals"]))
                    self.assertNotIn("tests/test_shop.py", item["scope"]["out_of_scope"])

    def test_inferred_conventional_tests_are_exempt_without_an_approval_request(self):
        conventional = ("tests/test_shop.py", "test/feature.spec.ts", "tests/shop_test.py",
                        "tests/test_shop.rb", "tests/shop_spec.rb", "tests/shop_test.go",
                        "tests/shop.test.jsx", "tests/shop.spec.mts", "tests/ShopTest.java",
                        "tests/TestShop.kt")
        for path in conventional:
            self.assertTrue(chief.is_test_file(path), path)
            self.write(path, "revised conventional coverage for " + path + "\n")
        task = dict(self.task, protected=[])
        for state in self.git_states():
            for phase, contract in self.phase_contracts(task):
                with self.subTest(state=state, phase=phase):
                    item = self.item(contract)
                    self.assertEqual(item["scope"]["changed"], len(conventional))
                    self.assertEqual(item["scope"]["protected_touched"], [])
                    self.assertEqual(item["scope"]["out_of_scope"], [])
                    self.assertFalse(any(kind == "scope" for _, _, kind in item["signals"]))

    def test_protected_custom_test_directories_keep_protection_and_review_signals(self):
        modified = ["qa/specs/shop.spec.ts", "qa/cases/test_existing.py"]
        added = ["qa/specs/new.test.js", "qa/cases/test_new.py", "qa/misc/test_new.py"]
        task = dict(self.task, protected=["qa/specs", "qa/cases", "qa/misc"])
        for path in modified + added:
            self.write(path, "revised coverage for " + path + "\n")
        for state in self.git_states():
            for phase, contract in self.phase_contracts(task):
                with self.subTest(state=state, phase=phase):
                    item = self.item(contract)
                    scope = item["scope"]
                    self.assertEqual(scope["changed"], len(modified + added))
                    self.assertEqual(scope["protected_touched"], sorted(modified + added))
                    self.assertEqual(scope["protected_violations"], sorted(modified))
                    self.assertEqual(scope["protected_added_tests"], sorted(added))
                    self.assertTrue(any(level == "red" and kind == "protected"
                                        for level, _, kind in item["signals"]))
                    self.assertTrue(any(level == "yellow" and kind == "new-tests"
                                        for level, _, kind in item["signals"]))
                    self.assertEqual(scope["out_of_scope"], [])

    def test_qualifying_new_support_is_reviewed_without_being_out_of_scope(self):
        paths = ["tests/helpers/new.py", "tests/conftest.py", "test/fixtures/sample.json",
                 "qa/specs/fixtures/sample.json", "qa/cases/helper.py"]
        task = dict(self.task, protected=["tests", "qa/specs", "qa/cases"])
        for path in paths:
            self.write(path, "new test support for " + path + "\n")
        for state in self.git_states():
            for phase, contract in self.phase_contracts(task):
                with self.subTest(state=state, phase=phase):
                    item = self.item(contract)
                    scope = item["scope"]
                    self.assertEqual(scope["changed"], len(paths))
                    self.assertEqual(scope["protected_added_support"], sorted(paths))
                    self.assertEqual(scope["protected_touched"], sorted(paths))
                    self.assertEqual(scope["protected_violations"], [])
                    reviews = [message for level, message, kind in item["signals"]
                               if level == "yellow" and kind == "new-test-support"]
                    self.assertEqual(len(reviews), 1)
                    for path in paths:
                        self.assertIn(path, reviews[0])
                    self.assertEqual(scope["out_of_scope"], [])

    def test_existing_and_explicitly_protected_support_stays_out_of_scope_and_red(self):
        modified, deleted = "tests/helpers/existing.py", "tests/fixtures/existing.json"
        explicit, unrelated = "tests/locked_helper.py", "qa/misc/new_helper.py"
        task = dict(self.task, protected=["tests", explicit, "qa/misc"])
        for path in (modified, explicit, unrelated):
            self.write(path, "unapproved non-test contents for " + path + "\n")
        (self.repo / deleted).unlink()
        paths = sorted([modified, deleted, explicit, unrelated])
        for state in self.git_states():
            with self.subTest(state=state):
                item = self.item(task)
                scope = item["scope"]
                self.assertEqual(scope["protected_added_support"], [])
                self.assertEqual(scope["protected_violations"], paths)
                self.assertEqual(scope["out_of_scope"], paths)
                self.assertTrue(any(level == "red" and kind == "protected"
                                    for level, _, kind in item["signals"]))

    def test_explicit_test_file_protection_stays_red_despite_scope_exemption(self):
        path = "tests/test_locked.py"
        task = dict(self.task, protected=["tests", path])
        self.write(path, "# unapproved explicitly protected test edit\n")
        for state in self.git_states():
            with self.subTest(state=state):
                item = self.item(task)
                scope = item["scope"]
                self.assertEqual(scope["changed"], 1)
                self.assertEqual(scope["protected_touched"], [path])
                self.assertEqual(scope["protected_violations"], [path])
                self.assertEqual(scope["protected_added_tests"], [])
                self.assertIn(("red", "protected files changed: " + path, "protected"), item["signals"])
                self.assertEqual(scope["out_of_scope"], [])

    def test_requesting_approval_cannot_exempt_arbitrary_non_test_or_unclassified_test_paths(self):
        paths = ["README.md", "src/application.py", "loose/test_outside.py", "loose/test_new.py"]
        self.write("tests/test_shop.py", "# revised acceptance coverage\n")
        for path in paths:
            self.write(path, "unrelated change to " + path + "\n")
        task = dict(self.task, scope="lib")
        question = "approve the acceptance tests tests/test_shop.py " + " ".join(paths)
        log = dict(self.log, footer={"decision": question})
        for state in self.git_states():
            with self.subTest(state=state):
                result = self.collect(task, log)
                item = result["items"][0]
                decision = chief.find_decision(item["decision"]["id"])
                self.assertEqual(decision["state"], "pending")
                self.assertCountEqual(decision["files"], ["tests/test_shop.py"] + paths)
                self.assertEqual(item["scope"]["changed"], len(paths) + 1)
                for path in paths:
                    self.assertIn(path, item["scope"]["out_of_scope"])
                self.assertTrue(any(level == "yellow" and kind == "scope"
                                    for level, _, kind in item["signals"]))
                self.assertEqual(item["scope"]["out_of_scope"], sorted(paths))

    def test_build_and_legacy_tasks_report_test_changes_in_every_git_state(self):
        existing, new, support = "tests/test_shop.py", "tests/test_new.py", "tests/helper.py"
        for path in (existing, new, support):
            self.write(path, "unapproved build change for " + path + "\n")
        legacy = {key: value for key, value in self.task.items() if key not in ("phase", "test_first")}
        contracts = (dict(self.task, phase="build"), legacy, dict(legacy, test_first=False))
        for state in self.git_states():
            for contract in contracts:
                with self.subTest(state=state, phase=contract.get("phase", "legacy"),
                                  test_first=contract.get("test_first")):
                    item = self.item(contract)
                    scope = item["scope"]
                    self.assertEqual(scope["changed"], 3)
                    self.assertEqual(scope["out_of_scope"], sorted([existing, new, support]))
                    self.assertEqual(scope["protected_touched"], sorted([existing, new, support]))
                    self.assertEqual(scope["protected_violations"], sorted([existing, support]))
                    self.assertEqual(scope["protected_added_tests"], [new])
                    self.assertEqual(scope["protected_added_support"], [])
                    self.assertTrue(any(level == "yellow" and kind == "scope"
                                        for level, _, kind in item["signals"]))

    def test_order_limit_and_ignores_apply_after_test_phase_membership_filtering(self):
        tests = ["tests/test_%02d.py" % n for n in range(25)]
        unrelated = ["zzz/change_%02d.txt" % n for n in range(25)]
        support = "tests/helper.py"
        ignored = ["tests/__pycache__/cache.pyc", "tests/.pytest_cache/state.json",
                   ".chief/progress.md", "zzz/generated-cache/cache.txt"]
        for path in tests + unrelated + [support, "README.md", "src/application.py"] + ignored:
            self.write(path, "changed contents for " + path + "\n")
        task = dict(self.task, scope="lib")
        for state in self.git_states():
            with self.subTest(state=state):
                scope = self.scope(task, extra_ignore=["generated-cache"])
                self.assertEqual(scope["changed"], len(tests + unrelated) + 3)
                self.assertEqual(scope["protected_added_support"], [support])
                self.assertEqual(scope["protected_touched"], [support])
                self.assertEqual(scope["out_of_scope"], sorted(unrelated + ["README.md", "src/application.py"])[:20])

    def test_scope_base_excludes_earlier_changes_and_keeps_later_committed_changes(self):
        self.write("README.md", "change preceding the selected scope base\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "pre-contract documentation change")
        task = dict(self.task, base=self.git("rev-parse", "HEAD"), scope="lib")
        self.write("tests/test_shop.py", "# later acceptance change\n")
        self.write("src/application.py", "# later out-of-scope implementation\n")
        for state in self.git_states():
            with self.subTest(state=state):
                scope = self.scope(task)
                self.assertEqual(scope["base"], task["base"][:10])
                self.assertEqual(scope["changed"], 2)
                self.assertEqual(scope["out_of_scope"], ["src/application.py"])

    def test_nested_repository_path_keeps_relative_scope_and_outside_changes(self):
        self.write("package/tests/test_nested.py", "# baseline nested test\n")
        self.write("package/src/module.py", "# baseline nested source\n")
        self.git("add", "package")
        self.git("commit", "-q", "-m", "nested project baseline")
        task = dict(self.task, base=self.git("rev-parse", "HEAD"))
        self.write("package/tests/test_nested.py", "# changed nested test\n")
        self.write("package/src/module.py", "# changed in-scope source\n")
        self.write("README.md", "outside the nested project\n")
        for state in self.git_states():
            with self.subTest(state=state):
                scope = self.scope(task, path=self.repo / "package")
                self.assertEqual(scope["changed"], 3)
                self.assertEqual(scope["protected_touched"], [])
                self.assertEqual(scope["out_of_scope"], ["../README.md"])

    def test_show_scope_line_keeps_changed_and_protected_count_semantics(self):
        task = dict(self.task, protected=["tests", "tests/test_locked.py", "config/settings.json"])
        paths = ["tests/test_shop.py", "tests/test_locked.py", "tests/helper.py",
                 "config/settings.json", "README.md", "src/application.py"]
        for path in paths:
            self.write(path, "changed contents for " + path + "\n")
        chief.save_task(task)
        sid = task["session_id"]
        record = dict(id=sid, project_path=str(self.repo), status="idle", tool="codex")
        for state in self.git_states():
            for lang in ("en", "zh"):
                with self.subTest(state=state, language=lang), mock.patch.object(chief, "LANG", lang), \
                        mock.patch.object(chief, "aoe_records", return_value={sid: record}), \
                        mock.patch.object(chief, "recent_turns", return_value=[]), \
                        mock.patch.object(chief, "screen", return_value=None):
                    scope = self.scope(task)
                    self.assertEqual(scope["changed"], 6)
                    self.assertEqual(scope["protected_touched"],
                                     ["config/settings.json", "tests/helper.py", "tests/test_locked.py"])
                    self.assertEqual(scope["protected_violations"],
                                     ["config/settings.json", "tests/test_locked.py"])
                    self.assertEqual(scope["protected_added_support"], ["tests/helper.py"])
                    text = chief.show(self.cfg, sid, 1)
                    expected = ("Scope: 6 files changed since {} · out of scope 2 · protected touched 2" if lang == "en"
                                else "范围：自 {} 起改动 6 个文件 · 超出范围 2 · 碰受保护 2").format(task["base"][:10])
                    self.assertIn(expected, text)
                    self.assertIn("tests/helper.py", text)
                    self.assertEqual(scope["out_of_scope"], ["README.md", "config/settings.json"])
