"""Inspectable, atomically-written JSON Lines run history."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from daily_streak.models import RunSummary
from daily_streak.state import atomic_write_text


logger = logging.getLogger(__name__)

HISTORY_FILENAME = "history.jsonl"


def summary_to_record(summary: RunSummary) -> dict[str, Any]:
    """Serialize a completed run summary for the durable history log."""

    return {
        "started_at": summary.started_at.isoformat(),
        "finished_at": summary.finished_at.isoformat() if summary.finished_at else None,
        "elapsed_seconds": summary.elapsed_seconds,
        "success": summary.success_count,
        "failed": summary.failed_count,
        "unconfirmed": summary.unconfirmed_count,
        "skipped": summary.skipped_count,
        "setup_error": summary.setup_error,
        "results": [result.to_dict() for result in summary.results],
    }


class RunHistoryStore:
    """Append and read history without involving GUI or browser code."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / HISTORY_FILENAME

    def append(self, summary: RunSummary) -> None:
        """Atomically add one JSON Lines record while preserving existing bytes."""

        existing = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        record = json.dumps(summary_to_record(summary), ensure_ascii=False, separators=(",", ":"))
        atomic_write_text(self.path, existing + record + "\n")

    def load(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Load valid history records, retaining readable records after corruption."""

        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as file:
            for line_number, line in enumerate(file, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("Ignoring malformed history line %d in %s", line_number, self.path)
                    continue
                if not isinstance(value, dict):
                    logger.warning("Ignoring non-object history line %d in %s", line_number, self.path)
                    continue
                records.append(value)
        return records[-limit:] if limit is not None else records
