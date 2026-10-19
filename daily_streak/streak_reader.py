"""TikTok streak count detection, caching, and display for Daily Streak Tool."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import json
import logging
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence

from daily_streak.paths import get_data_dir
from daily_streak.state import atomic_write_text, normalize_username


logger = logging.getLogger(__name__)

DEFAULT_CACHE_FILENAME = "streak_cache.json"
DEFAULT_STREAK_TTL_SECONDS = 6 * 3600  # 6 hours
DEFAULT_READ_TIMEOUT_MS = 6_000

STREAK_PROBE_SCRIPT = """
() => {
    // 1. Search for dedicated streak elements by e2e, class, id, or aria-label
    const streakElements = Array.from(document.querySelectorAll(
        "[data-e2e*='streak' i], [class*='streak' i], [id*='streak' i], [aria-label*='streak' i], [aria-label*='chuỗi' i]"
    ));
    for (const el of streakElements) {
        const text = (el.textContent || el.getAttribute("aria-label") || "").trim();
        const countMatch = text.match(/(?:🔥|\\bstreak\\b|\\bchuỗi\\b)[^\\d]*(\\d+)/i) ||
                           text.match(/(\\d+)\\s*(?:ngày|days?|🔥)/i);
        if (countMatch && parseInt(countMatch[1], 10) > 0) {
            return {
                found: true,
                type: "available",
                count: parseInt(countMatch[1], 10),
                evidence: text.slice(0, 100),
            };
        }
        if (/lost|ended|mất|kết thúc|hết hạn/i.test(text)) {
            return {
                found: true,
                type: "not_active",
                count: null,
                evidence: text.slice(0, 100),
            };
        }
    }

    // 2. Search visible text in chat header or conversation pane for flame emoji / streak text
    const header = document.querySelector("[class*='ChatHeader'], [class*='DivHeader']") ||
                   document.querySelector("[data-e2e='chat-uniqueid']")?.parentElement;
    const scope = header || document.body;

    if (scope) {
        const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
        let node;
        while ((node = walker.nextNode())) {
            const val = (node.nodeValue || "").trim();
            if (val.includes("🔥") || /\\bstreak\\b/i.test(val) || /\\bchuỗi\\b/i.test(val)) {
                const countMatch = val.match(/🔥\\s*(\\d+)/) ||
                                   val.match(/(\\d+)\\s*🔥/) ||
                                   val.match(/streak\\s*[:\\s]?\\s*(\\d+)/i) ||
                                   val.match(/(\\d+)\\s*ngày\\s*chuỗi/i);
                if (countMatch && parseInt(countMatch[1], 10) > 0) {
                    return {
                        found: true,
                        type: "available",
                        count: parseInt(countMatch[1], 10),
                        evidence: val.slice(0, 100),
                    };
                }
                if (/lost|ended|mất|kết thúc|hết hạn/i.test(val)) {
                    return {
                        found: true,
                        type: "not_active",
                        count: null,
                        evidence: val.slice(0, 100),
                    };
                }
            }
        }
    }

    // 3. Check if chat conversation is loaded cleanly
    const chatLoaded = Boolean(
        document.querySelector("[data-e2e='chat-uniqueid']") ||
        document.querySelector(".public-DraftEditor-content") ||
        document.querySelector("div[contenteditable='true'][placeholder*='Send' i]") ||
        document.querySelector("div[contenteditable='true'][placeholder*='message' i]")
    );

    return {
        found: false,
        chatLoaded: chatLoaded,
        type: "none",
        count: null,
        evidence: null,
    };
}
"""


class StreakStatus(str, Enum):
    """Categorized status of a recipient's TikTok interaction streak."""

    AVAILABLE = "available"
    NOT_VISIBLE = "not_visible"
    NOT_ACTIVE = "not_active"
    UNKNOWN = "unknown"


@dataclass
class StreakRecord:
    """Structured streak status for one recipient."""

    username: str
    status: StreakStatus
    count: int | None = None
    updated_at: str = ""
    stale: bool = False
    raw_evidence: str | None = None
    conversation_id: str | None = None
    user_id: int | None = None
    timezone: str | None = None
    active_before: int | None = None
    end_at: int | None = None

    def __post_init__(self) -> None:
        self.username = normalize_username(self.username)
        if isinstance(self.status, str) and not isinstance(self.status, StreakStatus):
            try:
                self.status = StreakStatus(self.status)
            except ValueError:
                self.status = StreakStatus.UNKNOWN
        if not self.updated_at:
            self.updated_at = datetime.now(timezone.utc).isoformat()
        if self.count is not None:
            self.count = int(self.count)
        if self.status != StreakStatus.AVAILABLE:
            self.count = None
        if self.user_id is not None:
            try:
                self.user_id = int(self.user_id)
            except (ValueError, TypeError):
                self.user_id = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "status": self.status.value,
            "count": self.count,
            "updated_at": self.updated_at,
            "stale": self.stale,
            "raw_evidence": self.raw_evidence,
            "conversation_id": self.conversation_id,
            "user_id": self.user_id,
            "timezone": self.timezone,
            "active_before": self.active_before,
            "end_at": self.end_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, is_stale: bool = False) -> StreakRecord:
        status_val = str(data.get("status") or StreakStatus.UNKNOWN.value)
        try:
            status = StreakStatus(status_val)
        except ValueError:
            status = StreakStatus.UNKNOWN
        count = data.get("count")
        parsed_count = int(count) if count is not None and str(count).isdigit() else None
        raw_uid = data.get("user_id")
        user_id = int(raw_uid) if raw_uid is not None and str(raw_uid).isdigit() else None
        return cls(
            username=str(data.get("username") or ""),
            status=status,
            count=parsed_count,
            updated_at=str(data.get("updated_at") or ""),
            stale=bool(data.get("stale", False) or is_stale),
            raw_evidence=str(data.get("raw_evidence")) if data.get("raw_evidence") is not None else None,
            conversation_id=str(data.get("conversation_id")) if data.get("conversation_id") else None,
            user_id=user_id,
            timezone=str(data.get("timezone")) if data.get("timezone") else None,
            active_before=int(data["active_before"]) if data.get("active_before") is not None else None,
            end_at=int(data["end_at"]) if data.get("end_at") is not None else None,
        )


def format_streak_display(record: StreakRecord | None, *, include_stale_marker: bool = True) -> str:
    """Format a streak record for TUI or CLI presentation."""

    if record is None:
        return "Chưa xác định"
    stale_suffix = " (cũ)" if (include_stale_marker and record.stale) else ""
    if record.status == StreakStatus.AVAILABLE and record.count is not None:
        return f"🔥 {record.count} ngày{stale_suffix}"
    if record.status == StreakStatus.NOT_VISIBLE:
        return f"Không hiển thị{stale_suffix}"
    if record.status == StreakStatus.NOT_ACTIVE:
        return f"Chưa kích hoạt{stale_suffix}"
    return "Chưa xác định"


class StreakCache:
    """Thread-safe persistent cache for recipient streak statuses."""

    def __init__(
        self,
        data_dir: Path,
        *,
        filename: str = DEFAULT_CACHE_FILENAME,
        ttl_seconds: float = DEFAULT_STREAK_TTL_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.cache_file = self.data_dir / filename
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._records: dict[str, StreakRecord] = {}
        self.load()

    def load(self) -> dict[str, StreakRecord]:
        """Load records from disk and evaluate staleness."""

        self._records.clear()
        if not self.cache_file.is_file():
            return self._records
        try:
            content = self.cache_file.read_text(encoding="utf-8")
            data = json.loads(content)
        except Exception as exc:
            logger.warning("Failed to read streak cache %s: %s", self.cache_file, exc)
            return self._records

        if not isinstance(data, dict):
            return self._records

        entries = data.get("streaks")
        if not isinstance(entries, dict):
            return self._records

        now = self._clock()
        for raw_username, entry_data in entries.items():
            if isinstance(entry_data, dict):
                norm_user = normalize_username(raw_username)
                record = StreakRecord.from_dict(entry_data)
                record.username = norm_user
                record.stale = self._is_stale(record, now)
                self._records[norm_user] = record
        return self._records

    def _is_stale(self, record: StreakRecord, now: datetime) -> bool:
        if record.stale:
            return True
        try:
            updated_dt = datetime.fromisoformat(record.updated_at)
            if updated_dt.tzinfo is None:
                updated_dt = updated_dt.replace(tzinfo=timezone.utc)
            age = (now - updated_dt).total_seconds()
            return age > self.ttl_seconds
        except Exception:
            return True

    def get(self, username: str) -> StreakRecord | None:
        """Retrieve a cached record, updating its stale state."""

        norm = normalize_username(username)
        if not norm:
            return None
        record = self._records.get(norm)
        if record is None:
            return None
        now = self._clock()
        is_stale = self._is_stale(record, now)
        if record.stale != is_stale:
            record.stale = is_stale
        return record

    def set(self, record: StreakRecord) -> None:
        """Store a record and persist atomically to disk."""

        norm = normalize_username(record.username)
        if not norm:
            return
        record.username = norm
        self._records[norm] = record
        self.save()

    def mark_stale(self, username: str) -> StreakRecord | None:
        """Mark an existing cached record as stale (e.g. after a refresh failure)."""

        norm = normalize_username(username)
        record = self._records.get(norm)
        if record is not None:
            record.stale = True
            self.save()
        return record

    def save(self) -> None:
        """Serialize cached records to disk atomically."""

        payload = {
            "version": 1,
            "streaks": {k: v.to_dict() for k, v in self._records.items()},
        }
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self.cache_file, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning("Failed to save streak cache to %s: %s", self.cache_file, exc)


class StreakReader:
    """Read-only detector for recipient streak counts via TikTok Web."""

    def __init__(
        self,
        cache: StreakCache,
        *,
        read_timeout_ms: int = DEFAULT_READ_TIMEOUT_MS,
    ) -> None:
        self.cache = cache
        self.read_timeout_ms = max(1_000, int(read_timeout_ms))

    async def probe_page_for_streak(
        self,
        page: Any,
        expected_username: str,
    ) -> StreakRecord:
        """Inspect the current open page for streak indicators of expected_username."""

        normalized_expected = normalize_username(expected_username)
        now_iso = datetime.now(timezone.utc).isoformat()

        # 1. Verify username on the opened conversation to prevent cross-recipient misattribution
        try:
            chat_user_locator = page.locator("[data-e2e='chat-uniqueid']").first
            if await chat_user_locator.count() > 0:
                raw_chat_user = await chat_user_locator.inner_text(timeout=self.read_timeout_ms)
                actual_user = normalize_username(raw_chat_user)
                if actual_user and actual_user != normalized_expected:
                    logger.warning(
                        "Chat username mismatch: expected @%s but found @%s. Aborting streak probe.",
                        normalized_expected,
                        actual_user,
                    )
                    return StreakRecord(
                        username=normalized_expected,
                        status=StreakStatus.UNKNOWN,
                        count=None,
                        updated_at=now_iso,
                        stale=False,
                        raw_evidence=f"Username mismatch (found @{actual_user})",
                    )
        except Exception as exc:
            logger.debug("Chat uniqueid check raised %s; proceeding to DOM probe", exc)

        # 2. Evaluate DOM streak indicators
        try:
            result = await page.evaluate(STREAK_PROBE_SCRIPT)
        except Exception as exc:
            logger.warning("Failed to evaluate streak probe script: %s", exc)
            return StreakRecord(
                username=normalized_expected,
                status=StreakStatus.UNKNOWN,
                count=None,
                updated_at=now_iso,
                stale=False,
                raw_evidence=f"Evaluation error: {exc}",
            )

        if not isinstance(result, dict):
            return StreakRecord(
                username=normalized_expected,
                status=StreakStatus.UNKNOWN,
                count=None,
                updated_at=now_iso,
            )

        res_type = result.get("type")
        evidence = result.get("evidence")
        if res_type == "available":
            count = result.get("count")
            if count is not None and int(count) > 0:
                return StreakRecord(
                    username=normalized_expected,
                    status=StreakStatus.AVAILABLE,
                    count=int(count),
                    updated_at=now_iso,
                    stale=False,
                    raw_evidence=str(evidence) if evidence else None,
                )
        elif res_type == "not_active":
            return StreakRecord(
                username=normalized_expected,
                status=StreakStatus.NOT_ACTIVE,
                count=None,
                updated_at=now_iso,
                stale=False,
                raw_evidence=str(evidence) if evidence else None,
            )

        # Cleanly loaded conversation with no streak indicators
        if result.get("chatLoaded"):
            return StreakRecord(
                username=normalized_expected,
                status=StreakStatus.NOT_VISIBLE,
                count=None,
                updated_at=now_iso,
                stale=False,
                raw_evidence="Chat loaded cleanly with no streak elements in DOM",
            )

        return StreakRecord(
            username=normalized_expected,
            status=StreakStatus.UNKNOWN,
            count=None,
            updated_at=now_iso,
            stale=False,
            raw_evidence="Chat did not load cleanly",
        )

    async def read_recipient_streak(
        self,
        page: Any,
        username: str,
        *,
        configured_display_name: str | None = None,
        recipient_service: Any = None,
        force_refresh: bool = False,
    ) -> StreakRecord:
        """Open a recipient conversation and read its streak, using cache when valid."""

        norm_username = normalize_username(username)
        if not force_refresh:
            cached = self.cache.get(norm_username)
            if cached is not None and not cached.stale:
                return cached

        if recipient_service is None:
            from daily_streak.recipients import RecipientService

            recipient_service = RecipientService()

        class _NoOpNotifier:
            async def notify(self, message: str, **kwargs: Any) -> None:
                pass

        try:
            await recipient_service.select_recipient_for_username_with_page(
                page,
                norm_username,
                _NoOpNotifier(),
                configured_display_name=configured_display_name,
            )
            record = await self.probe_page_for_streak(page, norm_username)
            if record.status != StreakStatus.UNKNOWN:
                self.cache.set(record)
            else:
                # If unknown and we have an existing cache, keep stale record
                existing = self.cache.get(norm_username)
                if existing is not None:
                    existing.stale = True
                    self.cache.save()
                    return existing
            return record
        except Exception as exc:
            logger.warning("Error reading streak for @%s: %s", norm_username, exc)
            existing = self.cache.mark_stale(norm_username)
            if existing is not None:
                return existing
            return StreakRecord(
                username=norm_username,
                status=StreakStatus.UNKNOWN,
                count=None,
                updated_at=datetime.now(timezone.utc).isoformat(),
                stale=False,
                raw_evidence=str(exc),
            )
