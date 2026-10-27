"""Acceptance coverage for normalized, truncated, and collision-safe new task IDs."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import chief


class TaskIdAcceptanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tasks = Path(temporary.name) / "tasks"
        self.tasks.mkdir()
        patcher = mock.patch.object(chief, "TASKS", self.tasks)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_truncation_at_separator_leaves_no_trailing_hyphen(self):
        for separator in (" ", "-", "_", "/", " ... "):
            with self.subTest(separator=separator):
                name = "a" * 29 + separator + "more words"
                self.assertEqual(chief.new_task_id(name), "a" * 29)

    def test_existing_normalization_and_normal_slugs_are_unchanged(self):
        cases = (
            ("Simple task", "simple-task"),
            ("  FIX__Trust / Prompt!!! ", "fix-trust-prompt"),
            ("already-normalized-42", "already-normalized-42"),
            ("Café Über", "caf-ber"),
            ("a" * 30, "a" * 30),
            ("a" * 31, "a" * 30),
            ("long-task-name-with-many-characters", "long-task-name-with-many-chara"),
        )
        for name, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(chief.new_task_id(name), expected)

    def test_empty_and_punctuation_only_names_keep_timestamp_fallback(self):
        moment = datetime(2026, 10, 28, 11, 34, tzinfo=timezone.utc)
        with mock.patch.object(chief, "now_utc", return_value=moment):
            for name in (None, "", " \t\n", "!!!___---.../", "中文"):
                with self.subTest(name=name):
                    self.assertEqual(chief.new_task_id(name), "task-1028-1134")

    def test_collisions_start_at_two_and_increment_without_overwriting(self):
        for tid in ("simple-task", "simple-task-2", "simple-task-3"):
            (self.tasks / tid).mkdir()
        self.assertEqual(chief.new_task_id("Simple task"), "simple-task-4")
        (self.tasks / "simple-task-2").rmdir()
        self.assertEqual(chief.new_task_id("Simple task"), "simple-task-2")

    def test_collision_suffix_is_added_after_truncation_and_separator_stripping(self):
        base = "a" * 29
        name = base + " more words"
        (self.tasks / base).mkdir()
        self.assertEqual(chief.new_task_id(name), base + "-2")
        (self.tasks / (base + "-2")).mkdir()
        self.assertEqual(chief.new_task_id(name), base + "-3")

    def test_full_length_slug_keeps_collision_suffix_beyond_thirty_characters(self):
        base = "b" * 30
        (self.tasks / base).mkdir()
        self.assertEqual(chief.new_task_id(base + "more"), base + "-2")

    def test_timestamp_fallback_also_uses_collision_suffix_starting_at_two(self):
        moment = datetime(2026, 10, 28, 11, 34, tzinfo=timezone.utc)
        for tid in ("task-1028-1134", "task-1028-1134-2"):
            (self.tasks / tid).mkdir()
        with mock.patch.object(chief, "now_utc", return_value=moment):
            self.assertEqual(chief.new_task_id("!!!"), "task-1028-1134-3")

    def test_generating_new_id_does_not_rename_or_rewrite_existing_ids(self):
        legacy_id = "a" * 29 + "-"
        legacy = self.tasks / legacy_id
        legacy.mkdir()
        task_file = legacy / "task.json"
        content = json.dumps({"id": legacy_id, "name": "Legacy task"}) + "\n"
        task_file.write_text(content, encoding="utf-8")

        self.assertEqual(chief.new_task_id("a" * 29 + " more words"), "a" * 29)
        self.assertEqual(chief.load_task(legacy_id)["id"], legacy_id)
        self.assertEqual(task_file.read_text(encoding="utf-8"), content)
        self.assertEqual(sorted(path.name for path in self.tasks.iterdir()), [legacy_id])


if __name__ == "__main__":
    unittest.main()
