import asyncio
import logging
import platform
import subprocess
from typing import Any

from plyer import notification
from telegram import Bot


logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, config: dict[str, Any]) -> None:
        telegram_config = config.get("telegram", {})
        self.telegram_enabled = bool(telegram_config.get("enabled"))
        self.bot_token = telegram_config.get("bot_token")
        self.chat_id = telegram_config.get("chat_id")

    def desktop(self, title: str, message: str) -> None:
        system = platform.system()
        try:
            if system == "Darwin":
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
            bot = Bot(token=self.bot_token)
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
