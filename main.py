import asyncio
from typing import Any

from daily_streak.config import DEFAULT_CONFIG_PATH
from daily_streak.daemon import DaemonRunner
from notifier import Notifier
from sender import TikTokSender
from video_pool import VideoPool


CONFIG_PATH = DEFAULT_CONFIG_PATH


def parse_hour_minute(time_value: str) -> tuple[int, int]:
    """Compatibility wrapper for callers of the original scheduler helper."""

    from daily_streak.schedule import parse_hour_minute as shared_parse_hour_minute

    return shared_parse_hour_minute(time_value)


async def run_sender(config: dict[str, Any]) -> None:
    notifier = Notifier(config)
    video_pool = VideoPool(config)
    sender = TikTokSender(config, notifier, video_pool)
    await sender.send_daily_links()


async def main() -> None:
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    await DaemonRunner(CONFIG_PATH).run_forever()


if __name__ == "__main__":
    asyncio.run(main())
