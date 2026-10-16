"""Versioned, atomic daily completion state for interruption-safe resume."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping
from zoneinfo import ZoneInfo


logger = logging.getLogger(__name__)

STATE_VERSION = 1
STATE_FILENAME = "daily-state.json"


def normalize_username(username: str) -> str:
    """Return the canonical state key for a TikTok username."""

    return username.strip().lstrip("@").casefold()


def logical_now(timezone_name: str, now: datetime | None = None) -> datetime:
    """Return the current instant represented in the configured schedule timezone."""

    schedule_timezone = ZoneInfo(timezone_name)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(schedule_timezone)


def logical_date(timezone_name: str, now: datetime | None = None) -> str:
    """Return the schedule-local calendar date used to scope daily state."""

    return logical_now(timezone_name, now).date().isoformat()


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically replace a text file using a sibling temporary file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically replace a JSON file in a readable, inspectable format."""

    atomic_write_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


@dataclass(slots=True)
class DailyRecipientState:
    status: str
    video: str
    completed_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "status": self.status,
            "video": self.video,
            "completed_at": self.completed_at,
        }


@dataclass(slots=True)
class DailyState:
    date: str
    timezone: str
    recipients: dict[str, DailyRecipientState] = field(default_factory=dict)
    version: int = STATE_VERSION

    @classmethod
    def fresh(cls, date: str, timezone_name: str) -> "DailyState":
        return cls(date=date, timezone=timezone_name)

    def is_completed(self, username: str) -> bool:
        completion = self.recipients.get(normalize_username(username))
        return completion is not None and completion.status == "success"

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "date": self.date,
            "timezone": self.timezone,
            "recipients": {
                username: completion.to_dict()
                for username, completion in self.recipients.items()
            },
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DailyState":
        if value.get("version") != STATE_VERSION:
            raise ValueError(f"Unsupported daily state version: {value.get('version')!r}")
        date = value.get("date")
        timezone_name = value.get("timezone")
        recipients = value.get("recipients")
        if not isinstance(date, str) or not isinstance(timezone_name, str):
            raise ValueError("Daily state requires string date and timezone values")
        if not isinstance(recipients, Mapping):
            raise ValueError("Daily state recipients must be an object")

        parsed_recipients: dict[str, DailyRecipientState] = {}
        for username, raw_completion in recipients.items():
            if not isinstance(username, str) or not isinstance(raw_completion, Mapping):
                raise ValueError("Daily state contains an invalid recipient entry")
            status = raw_completion.get("status")
            video = raw_completion.get("video")
            completed_at = raw_completion.get("completed_at")
            if not all(isinstance(item, str) for item in (status, video, completed_at)):
                raise ValueError("Daily state completion entries require string fields")
            if status != "success":
                continue
            normalized_username = normalize_username(username)
            if normalized_username:
                parsed_recipients[normalized_username] = DailyRecipientState(
                    status=status,
                    video=video,
                    completed_at=completed_at,
                )
        return cls(
            date=date,
            timezone=timezone_name,
            recipients=parsed_recipients,
            version=STATE_VERSION,
        )


class DailyStateStore:
    """Loads and immediately persists success-only state for one logical day."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / STATE_FILENAME

    def load(self, date: str, timezone_name: str) -> DailyState:
        """Return usable state for this day, recovering malformed state safely."""

        if not self.path.exists():
            return DailyState.fresh(date, timezone_name)
        try:
            with self.path.open("r", encoding="utf-8") as file:
                value = json.load(file)
            if not isinstance(value, Mapping):
                raise ValueError("Daily state root must be a JSON object")
            state = DailyState.from_dict(value)
        except Exception as exc:
            logger.error("Daily state at %s is malformed: %s", self.path, exc)
            self._preserve_corrupt_state()
            return DailyState.fresh(date, timezone_name)

        if state.date != date or state.timezone != timezone_name:
            logger.info(
                "Ignoring daily state for %s %s while running %s %s",
                state.date,
                state.timezone,
                date,
                timezone_name,
            )
            return DailyState.fresh(date, timezone_name)
        return state

    def record_success(
        self,
        state: DailyState,
        username: str,
        video: str,
        completed_at: datetime,
    ) -> None:
        """Record a successful delivery and atomically persist it immediately."""

        normalized_username = normalize_username(username)
        if not normalized_username:
            raise ValueError("Cannot persist completion without a username")
        if completed_at.tzinfo is None:
            raise ValueError("Completion timestamps must include a timezone")
        state.recipients[normalized_username] = DailyRecipientState(
            status="success",
            video=video,
            completed_at=completed_at.isoformat(),
        )
        atomic_write_json(self.path, state.to_dict())

    def _preserve_corrupt_state(self) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = self.path.with_name(
            f"{self.path.stem}.corrupt-{timestamp}{self.path.suffix}"
        )
        try:
            self.path.replace(backup_path)
            logger.warning("Preserved malformed daily state at %s", backup_path)
        except Exception:
            logger.exception("Could not preserve malformed daily state at %s", self.path)


PAUSE_FILENAME = "daemon-pause.json"


class PauseStore:
    """Persists daemon pause state atomically across processes and restarts."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / PAUSE_FILENAME

    def is_paused(self) -> bool:
        """Return True if daemon automated runs are currently paused."""
        info = self.get_pause_info()
        return bool(info.get("paused"))

    def get_pause_info(self) -> dict[str, Any]:
        """Return structured pause metadata or default unpaused state."""
        if not self.path.exists():
            return {"paused": False}
        try:
            with self.path.open("r", encoding="utf-8") as file:
                value = json.load(file)
            if isinstance(value, dict) and bool(value.get("paused")):
                return {
                    "paused": True,
                    "paused_at": value.get("paused_at"),
                    "reason": value.get("reason"),
                }
        except Exception as exc:
            logger.warning("Failed to read pause state at %s: %s", self.path, exc)
        return {"paused": False}

    def pause(self, reason: str = "telegram") -> None:
        """Atomically persist pause state."""
        now_iso = datetime.now(timezone.utc).isoformat()
        payload = {
            "version": 1,
            "paused": True,
            "paused_at": now_iso,
            "reason": str(reason),
        }
        atomic_write_json(self.path, payload)
        logger.info("Daemon scheduler paused (reason=%s)", reason)

    def resume(self) -> bool:
        """Atomically clear pause state. Return True if was paused."""
        was_paused = self.is_paused()
        if self.path.exists():
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            except Exception as exc:
                logger.warning("Failed to remove pause file %s: %s", self.path, exc)
        logger.info("Daemon scheduler resumed")
        return was_paused
