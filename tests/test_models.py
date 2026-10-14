from __future__ import annotations

import unittest
from datetime import datetime, timezone

from daily_streak.models import RecipientResult, RecipientStatus, RunSummary


class ResultModelTests(unittest.TestCase):
    def test_run_summary_aggregates_each_terminal_status(self) -> None:
        summary = RunSummary()
        summary.add_result(
            RecipientResult("Alice", RecipientStatus.SUCCESS, username="alice")
        )
        summary.add_result(
            RecipientResult("Bob", RecipientStatus.FAILED, error_message="not found")
        )
        summary.add_result(
            RecipientResult("Carol", RecipientStatus.SKIPPED, error_message="missing username")
        )
        summary.add_result(
            RecipientResult("Dave", RecipientStatus.UNCONFIRMED, error_message="timeout")
        )

        self.assertEqual(summary.success_count, 1)
        self.assertEqual(summary.failed_count, 1)
        self.assertEqual(summary.skipped_count, 1)
        self.assertEqual(summary.unconfirmed_count, 1)

    def test_successful_recipient_result_exposes_delivery_details(self) -> None:
        result = RecipientResult(
            recipient_name="Alice",
            username="alice",
            selected_video="https://www.tiktok.com/@creator/video/123",
            status=RecipientStatus.SUCCESS,
            attempts=1,
            elapsed_seconds=1.25,
        )

        self.assertTrue(result.succeeded)
        self.assertIsNone(result.error_message)
        self.assertEqual(result.attempts, 1)

    def test_run_summary_records_finished_time_and_elapsed_duration(self) -> None:
        started_at = datetime(2026, 10, 11, 0, 0, tzinfo=timezone.utc)
        summary = RunSummary(started_at=started_at)
        summary.finish(datetime(2026, 10, 11, 0, 0, 3, tzinfo=timezone.utc))

        self.assertEqual(summary.finished_at, datetime(2026, 10, 11, 0, 0, 3, tzinfo=timezone.utc))
        self.assertEqual(summary.elapsed_seconds, 3.0)
