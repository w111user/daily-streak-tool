"""Schedule parsing and next-run calculations shared by CLI and daemon code."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Schedule:
    """A validated daily wall-clock schedule."""

    hour: int
    minute: int
    timezone: ZoneInfo

    @property
    def time_text(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"


def parse_hour_minute(value: str) -> tuple[int, int]:
    """Parse the legacy ``HH:MM`` schedule field with helpful validation."""

    if not isinstance(value, str):
        raise ValueError("Schedule time must be a string in HH:MM format.")
    parts = value.split(":", maxsplit=1)
    if len(parts) != 2:
        raise ValueError("Schedule time must use HH:MM format.")
    try:
        hour, minute = (int(part) for part in parts)
    except ValueError as exc:
        raise ValueError("Schedule time must use numeric HH:MM values.") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("Schedule time must be between 00:00 and 23:59.")
    return hour, minute


def schedule_from_config(config: Mapping[str, Any]) -> Schedule:
    """Read a validated schedule while preserving legacy optional defaults."""

    raw_schedule = config.get("schedule", {})
    schedule_config: Mapping[str, Any] = (
        raw_schedule if isinstance(raw_schedule, Mapping) else {}
    )
    hour, minute = parse_hour_minute(str(schedule_config.get("time", "00:00")))
    timezone_name = str(schedule_config.get("timezone", "Asia/Ho_Chi_Minh"))
    try:
        schedule_timezone = ZoneInfo(timezone_name)
    except Exception as exc:
        raise ValueError(f"Invalid schedule timezone: {timezone_name!r}") from exc
    return Schedule(hour=hour, minute=minute, timezone=schedule_timezone)


def next_run(schedule: Schedule, now: datetime | None = None) -> datetime:
    """Return the next scheduled instant in the schedule's local timezone."""

    if now is None:
        now = datetime.now(schedule.timezone)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=schedule.timezone)
    else:
        now = now.astimezone(schedule.timezone)

    candidate = datetime.combine(now.date(), time(schedule.hour, schedule.minute), schedule.timezone)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate
