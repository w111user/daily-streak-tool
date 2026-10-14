from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from daily_streak.history import RunHistoryStore
from daily_streak.models import RecipientResult, RecipientStatus, RunSummary
from daily_streak.state import DailyStateStore, logical_date, normalize_username


class DailyStateTests(unittest.TestCase):
    def test_fresh_day_has_no_completed_recipients(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            store = DailyStateStore(temporary_directory)
            state = store.load("2026-10-11", "Asia/Ho_Chi_Minh")

        self.assertFalse(state.is_completed("alice"))
        self.assertEqual(state.recipients, {})

    def test_success_is_persisted_atomically_with_normalized_username(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            store = DailyStateStore(temporary_directory)
            state = store.load("2026-10-11", "Asia/Ho_Chi_Minh")
            store.record_success(
                state,
                " @Alice ",
                "https://video.example/a",
                datetime(2026, 10, 11, 0, 4, 21, tzinfo=timezone.utc),
            )
            persisted = json.loads(store.path.read_text(encoding="utf-8"))
            reloaded = store.load("2026-10-11", "Asia/Ho_Chi_Minh")
            temporary_files = list(Path(temporary_directory).glob("*.tmp"))

        self.assertEqual(persisted["version"], 1)
        self.assertIn("alice", persisted["recipients"])
        self.assertTrue(reloaded.is_completed("ALICE"))
        self.assertEqual(normalize_username(" @Alice "), "alice")
        self.assertEqual(temporary_files, [])

    def test_failed_recipients_are_not_stored_as_completed(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            state = DailyStateStore(temporary_directory).load("2026-10-11", "UTC")

        self.assertFalse(state.is_completed("failed-recipient"))

    def test_different_logical_day_does_not_reuse_prior_completion(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            store = DailyStateStore(temporary_directory)
            state = store.load("2026-10-11", "UTC")
            store.record_success(
                state,
                "alice",
                "https://video.example/a",
                datetime(2026, 10, 11, tzinfo=timezone.utc),
            )
            next_day = store.load("2026-10-12", "UTC")

        self.assertFalse(next_day.is_completed("alice"))
        self.assertEqual(next_day.date, "2026-10-12")

    def test_configured_timezone_selects_logical_day(self) -> None:
        instant = datetime(2026, 10, 10, 18, 30, tzinfo=timezone.utc)
        self.assertEqual(logical_date("Asia/Ho_Chi_Minh", instant), "2026-10-11")
        self.assertEqual(logical_date("America/Los_Angeles", instant), "2026-10-10")

    def test_malformed_state_is_preserved_and_recovered(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            store = DailyStateStore(temporary_directory)
            Path(store.path).parent.mkdir(parents=True, exist_ok=True)
            store.path.write_text("{not json", encoding="utf-8")
            with self.assertLogs("daily_streak.state", level="ERROR"):
                state = store.load("2026-10-11", "UTC")
            preserved = list(Path(temporary_directory).glob("daily-state.corrupt-*.json"))
            preserved_content = preserved[0].read_text(encoding="utf-8")

        self.assertEqual(state.recipients, {})
        self.assertFalse(store.path.exists())
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved_content, "{not json")


class RunHistoryTests(unittest.TestCase):
    def test_history_appends_and_loads_completed_run_summaries(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            history = RunHistoryStore(temporary_directory)
            first = RunSummary(started_at=datetime(2026, 10, 11, tzinfo=timezone.utc))
            first.add_result(RecipientResult("Alice", RecipientStatus.SUCCESS, username="alice"))
            first.finish(datetime(2026, 10, 11, 0, 0, 5, tzinfo=timezone.utc))
            second = RunSummary(started_at=datetime(2026, 10, 12, tzinfo=timezone.utc))
            second.add_result(RecipientResult("Bob", RecipientStatus.FAILED, username="bob"))
            second.finish(datetime(2026, 10, 12, 0, 0, 2, tzinfo=timezone.utc))
            history.append(first)
            history.append(second)
            records = history.load()
            recent = history.load(limit=1)

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["success"], 1)
        self.assertEqual(records[0]["elapsed_seconds"], 5.0)
        self.assertEqual(records[1]["failed"], 1)
        self.assertEqual(recent, [records[1]])
