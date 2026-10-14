from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from daily_streak.history import RunHistoryStore
from daily_streak.models import RecipientStatus
from daily_streak.sender import TikTokSender
from daily_streak.state import DailyStateStore


class FakeNotifier:
    def desktop(self, title: str, message: str) -> None:
        pass

    async def telegram(self, message: str) -> None:
        pass

    async def captcha_alert(self, page_url: str) -> None:
        pass


class FixedVideoPool:
    def random_video(self) -> str:
        return "https://video.example/shared"


class SimulatedSender(TikTokSender):
    def __init__(
        self,
        config: dict[str, object],
        data_dir: Path,
        now: datetime,
        outcomes: dict[str, list[BaseException | None]],
    ) -> None:
        super().__init__(
            config,
            FakeNotifier(),
            FixedVideoPool(),  # type: ignore[arg-type]
            state_store=DailyStateStore(data_dir),
            history_store=RunHistoryStore(data_dir),
            now_provider=lambda: now,
        )
        self.outcomes = defaultdict(list, outcomes)
        self.attempted_usernames: list[str] = []

    async def _send_recipient_once(
        self,
        context: object,
        recipient: dict[str, object],
        selected_video: str,
    ) -> None:
        username = str(recipient["username"])
        self.attempted_usernames.append(username)
        outcome = self.outcomes[username].pop(0)
        if outcome is not None:
            raise outcome

    async def _sleep_between_messages(self) -> None:
        pass


def sender_config() -> dict[str, object]:
    return {
        "schedule": {"timezone": "UTC"},
        "tiktok": {"recipient_retries": 0},
        "recipients": [
            {"name": "Alice", "username": "Alice"},
            {"name": "Bob", "username": "@BOB"},
            {"name": "Carol", "username": "carol"},
        ],
    }


class SenderStateIntegrationTests(unittest.TestCase):
    def test_process_resume_skips_persisted_successes_and_next_day_retries_them(self) -> None:
        day_one = datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc)
        day_two = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory)
            first_run = SimulatedSender(
                sender_config(),
                data_dir,
                day_one,
                {
                    "Alice": [None],
                    "@BOB": [None],
                    "carol": [asyncio.CancelledError()],
                },
            )
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(first_run.send_recipients(context=object()))

            resumed_run = SimulatedSender(
                sender_config(),
                data_dir,
                day_one,
                {"carol": [None]},
            )
            resumed_summary = asyncio.run(resumed_run.send_recipients(context=object()))

            next_day_run = SimulatedSender(
                sender_config(),
                data_dir,
                day_two,
                {"Alice": [None], "@BOB": [None], "carol": [None]},
            )
            next_day_summary = asyncio.run(next_day_run.send_recipients(context=object()))

        self.assertEqual(first_run.attempted_usernames, ["Alice", "@BOB", "carol"])
        self.assertEqual(
            [result.status for result in resumed_summary.results],
            [RecipientStatus.SKIPPED, RecipientStatus.SKIPPED, RecipientStatus.SUCCESS],
        )
        self.assertEqual(resumed_run.attempted_usernames, ["carol"])
        self.assertEqual(
            next_day_run.attempted_usernames,
            ["Alice", "@BOB", "carol"],
        )
        self.assertEqual(next_day_summary.success_count, 3)

    def test_force_attempts_a_recipient_already_completed_today(self) -> None:
        now = datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory)
            first = SimulatedSender(
                sender_config(),
                data_dir,
                now,
                {"Alice": [None], "@BOB": [None], "carol": [None]},
            )
            asyncio.run(first.send_recipients(context=object()))

            forced = SimulatedSender(
                sender_config(),
                data_dir,
                now,
                {"Alice": [None], "@BOB": [None], "carol": [None]},
            )
            summary = asyncio.run(forced.send_recipients(context=object(), force=True))

        self.assertEqual(forced.attempted_usernames, ["Alice", "@BOB", "carol"])
        self.assertEqual(summary.success_count, 3)
        self.assertEqual(summary.skipped_count, 0)

    def test_failed_recipient_is_eligible_on_a_later_run(self) -> None:
        now = datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc)
        config = sender_config()
        config["recipients"] = [{"name": "Bob", "username": "bob"}]
        with TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory)
            failed = SimulatedSender(
                config,
                data_dir,
                now,
                {"bob": [RuntimeError("temporary failure")]},
            )
            with self.assertLogs("daily_streak.sender", level="ERROR"):
                failed_summary = asyncio.run(failed.send_recipients(context=object()))

            retried = SimulatedSender(config, data_dir, now, {"bob": [None]})
            retried_summary = asyncio.run(retried.send_recipients(context=object()))

        self.assertEqual(failed_summary.failed_count, 1)
        self.assertEqual(retried.attempted_usernames, ["bob"])
        self.assertEqual(retried_summary.success_count, 1)
