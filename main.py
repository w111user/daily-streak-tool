import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from notifier import Notifier
from sender import TikTokSender
from video_pool import VideoPool


CONFIG_PATH = Path("config.json")


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


def parse_hour_minute(time_value: str) -> tuple[int, int]:
    hour_text, minute_text = time_value.split(":", maxsplit=1)
    return int(hour_text), int(minute_text)


async def run_sender(config: dict[str, Any]) -> None:
    notifier = Notifier(config)
    video_pool = VideoPool(config)
    sender = TikTokSender(config, notifier, video_pool)
    await sender.send_daily_links()


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config()
    schedule_config = config.get("schedule", {})
    hour, minute = parse_hour_minute(schedule_config.get("time", "09:00"))
    timezone = schedule_config.get("timezone", "Asia/Ho_Chi_Minh")

    scheduler = AsyncIOScheduler(timezone=timezone)
    scheduler.add_job(
        run_sender,
        CronTrigger(hour=hour, minute=minute, timezone=timezone),
        args=[config],
        id="daily_tiktok_streak_sender",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=None,
        coalesce=True,
    )
    scheduler.start()

    logging.info("Scheduled daily TikTok streak sender for %02d:%02d %s", hour, minute, timezone)

    if schedule_config.get("run_on_start"):
        await run_sender(config)

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
