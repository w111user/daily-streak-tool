"""Optional desktop and Telegram notifications for sender adapters."""

from __future__ import annotations

import asyncio
import logging
import platform
import subprocess
from typing import Any, Mapping


logger = logging.getLogger(__name__)


class Notifier:
    """Notification adapter that remains usable without desktop packages."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        raw_telegram = config.get("telegram", {})
        telegram_config: Mapping[str, Any] = (
            raw_telegram if isinstance(raw_telegram, Mapping) else {}
        )
        self.telegram_enabled = bool(telegram_config.get("enabled"))
        self.bot_token = telegram_config.get("bot_token")
        self.chat_id = telegram_config.get("chat_id")

    def desktop(self, title: str, message: str) -> None:
        """Try to display a desktop notification without making it required."""

        try:
            if platform.system() == "Darwin":
                safe_title = title.replace('"', '\\"')
                safe_message = message.replace('"', '\\"')
                subprocess.run(
                    [
                        "osascript",
                        "-e",
                        f'display notification "{safe_message}" with title "{safe_title}"',
                    ],
                    check=False,
                )
                return
            try:
                from plyer import notification
            except ImportError:
                logger.info("Desktop notifications are unavailable (install the desktop extra).")
                return
            notification.notify(
                title=title,
                message=message,
                app_name="TikTok Streak Sender",
                timeout=10,
            )
        except Exception:
            logger.exception("Failed to send desktop notification")

    async def telegram(self, message: str) -> None:
        if not self.telegram_enabled:
            return
        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram is enabled but bot_token/chat_id is missing")
            return
        try:
            from telegram import Bot
        except ImportError:
            logger.warning("Telegram notifications require the telegram optional extra")
            return

        try:
            bot = Bot(token=str(self.bot_token))
            await bot.send_message(chat_id=self.chat_id, text=message)
        except Exception:
            logger.exception("Failed to send Telegram notification")

    async def captcha_alert(self, page_url: str) -> None:
        message = (
            "TikTok CAPTCHA detected. Please solve it in the open browser window; "
            "the sender will resume automatically after it clears.\n\n"
            f"Page: {page_url}"
        )
        self.desktop("TikTok CAPTCHA detected", message)
        await self.telegram(message)

    def captcha_alert_sync(self, page_url: str) -> None:
        asyncio.run(self.captcha_alert(page_url))

    async def batch_completion(self, summary: Any) -> None:
        """Send a concise Telegram summary after an automated batch finishes."""
        message = format_batch_completion_message(summary)
        try:
            await self.telegram(message)
        except Exception:
            logger.exception("Failed to send batch completion notification")

    def batch_completion_sync(self, summary: Any) -> None:
        asyncio.run(self.batch_completion(summary))


def format_batch_completion_message(summary: Any) -> str:
    """Format the concise Telegram batch completion summary."""
    success = getattr(summary, "success_count", 0)
    failed = getattr(summary, "failed_count", 0)
    unconfirmed = getattr(summary, "unconfirmed_count", 0)
    skipped = getattr(summary, "skipped_count", 0)
    return (
        "Daily Streak Tool \u2014 batch completed\n"
        f"Success: {success}\n"
        f"Failed: {failed}\n"
        f"Unconfirmed: {unconfirmed}\n"
        f"Skipped: {skipped}"
    )
