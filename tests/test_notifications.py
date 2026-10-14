from __future__ import annotations

import asyncio
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from daily_streak.models import RecipientResult, RecipientStatus, RunSummary
from daily_streak.notifications import Notifier, format_batch_completion_message


class NotificationsTests(unittest.TestCase):
    def test_format_batch_completion_message_counts(self) -> None:
        summary = RunSummary(
            results=[
                RecipientResult("Alice", RecipientStatus.SUCCESS),
                RecipientResult("Bob", RecipientStatus.SUCCESS),
                RecipientResult("Charlie", RecipientStatus.FAILED),
                RecipientResult("David", RecipientStatus.UNCONFIRMED),
                RecipientResult("Eve", RecipientStatus.SKIPPED),
            ]
        ).finish()

        message = format_batch_completion_message(summary)
        expected = (
            "Daily Streak Tool \u2014 batch completed\n"
            "Success: 2\n"
            "Failed: 1\n"
            "Unconfirmed: 1\n"
            "Skipped: 1"
        )
        self.assertEqual(message, expected)

    def test_batch_completion_when_telegram_disabled_is_noop(self) -> None:
        notifier = Notifier({"telegram": {"enabled": False}})
        summary = RunSummary(results=[RecipientResult("Alice", RecipientStatus.SUCCESS)]).finish()

        mock_telegram = MagicMock()
        with patch.dict("sys.modules", {"telegram": mock_telegram}):
            asyncio.run(notifier.batch_completion(summary))

        mock_telegram.Bot.assert_not_called()

    def test_batch_completion_when_token_or_chat_id_missing_is_noop(self) -> None:
        notifier = Notifier({"telegram": {"enabled": True, "bot_token": "", "chat_id": ""}})
        summary = RunSummary(results=[RecipientResult("Alice", RecipientStatus.SUCCESS)]).finish()

        mock_telegram = MagicMock()
        with patch.dict("sys.modules", {"telegram": mock_telegram}):
            asyncio.run(notifier.batch_completion(summary))

        mock_telegram.Bot.assert_not_called()

    def test_batch_completion_sends_exactly_one_telegram_message(self) -> None:
        notifier = Notifier(
            {
                "telegram": {
                    "enabled": True,
                    "bot_token": "secret_token_123",
                    "chat_id": "group_chat_456",
                }
            }
        )
        summary = RunSummary(
            results=[
                RecipientResult("Alice", RecipientStatus.SUCCESS),
                RecipientResult("Bob", RecipientStatus.UNCONFIRMED),
            ]
        ).finish()

        mock_telegram = MagicMock()
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()
        mock_telegram.Bot.return_value = mock_bot

        with patch.dict("sys.modules", {"telegram": mock_telegram}):
            asyncio.run(notifier.batch_completion(summary))

        mock_telegram.Bot.assert_called_once_with(token="secret_token_123")
        mock_bot.send_message.assert_awaited_once_with(
            chat_id="group_chat_456",
            text=(
                "Daily Streak Tool \u2014 batch completed\n"
                "Success: 1\n"
                "Failed: 0\n"
                "Unconfirmed: 1\n"
                "Skipped: 0"
            ),
        )

    def test_batch_completion_swallows_telegram_exceptions(self) -> None:
        notifier = Notifier(
            {
                "telegram": {
                    "enabled": True,
                    "bot_token": "token",
                    "chat_id": "chat",
                }
            }
        )
        summary = RunSummary().finish()

        mock_telegram = MagicMock()
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(side_effect=RuntimeError("Telegram Network Timeout"))
        mock_telegram.Bot.return_value = mock_bot

        with patch.dict("sys.modules", {"telegram": mock_telegram}):
            # Must not raise an exception
            asyncio.run(notifier.batch_completion(summary))

    def test_batch_completion_sync_helper(self) -> None:
        notifier = Notifier(
            {
                "telegram": {
                    "enabled": True,
                    "bot_token": "token",
                    "chat_id": "chat",
                }
            }
        )
        summary = RunSummary().finish()

        mock_telegram = MagicMock()
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()
        mock_telegram.Bot.return_value = mock_bot

        with patch.dict("sys.modules", {"telegram": mock_telegram}):
            notifier.batch_completion_sync(summary)

        mock_bot.send_message.assert_awaited_once()
