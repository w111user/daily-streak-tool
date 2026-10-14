"""Typed outcome models shared by desktop, CLI, and daemon adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class RecipientStatus(str, Enum):
    """The terminal outcome of one recipient within a sending run."""

    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNCONFIRMED = "unconfirmed"


class DeliveryStatus(str, Enum):
    """The delivery verification state of an attempted message."""

    VERIFIED = "verified"
    CONFIRMED_NOT_SENT = "confirmed_not_sent"
    UNCONFIRMED = "unconfirmed"


@dataclass(slots=True)
class RecipientResult:
    """A recorded outcome for one intended recipient delivery."""

    recipient_name: str
    status: RecipientStatus
    username: str | None = None
    selected_video: str | None = None
    attempts: int = 0
    error_message: str | None = None
    elapsed_seconds: float | None = None
    delivery_status: DeliveryStatus | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is RecipientStatus.SUCCESS

    def to_dict(self) -> dict[str, object]:
        return {
            "recipient_name": self.recipient_name,
            "username": self.username,
            "selected_video": self.selected_video,
            "status": self.status.value,
            "attempts": self.attempts,
            "error_message": self.error_message,
            "elapsed_seconds": self.elapsed_seconds,
            "delivery_status": self.delivery_status.value if self.delivery_status else None,
        }


@dataclass(slots=True)
class RunSummary:
    """Aggregate result for one sender run and its durable history record."""

    results: list[RecipientResult] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    setup_error: str | None = None

    def add_result(self, result: RecipientResult) -> None:
        self.results.append(result)

    def finish(self, finished_at: datetime | None = None) -> "RunSummary":
        self.finished_at = finished_at or datetime.now(timezone.utc)
        return self

    @property
    def elapsed_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def success_count(self) -> int:
        return sum(result.status is RecipientStatus.SUCCESS for result in self.results)

    @property
    def failed_count(self) -> int:
        return sum(result.status is RecipientStatus.FAILED for result in self.results)

    @property
    def unconfirmed_count(self) -> int:
        return sum(result.status is RecipientStatus.UNCONFIRMED for result in self.results)

    @property
    def skipped_count(self) -> int:
        return sum(result.status is RecipientStatus.SKIPPED for result in self.results)
