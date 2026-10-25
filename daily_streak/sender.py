"""Resilient, reusable TikTok batch sender core."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
from enum import Enum
import inspect
import logging
from pathlib import Path
import random
import re
import time
from typing import Any, Callable, Mapping, Protocol, Sequence

from daily_streak.browser import (
    check_cookies_valid,
    get_action_timeout_ms,
    get_content_ready_timeout_ms,
    get_dm_load_attempts,
    get_dm_readiness_timeout_ms,
    get_low_memory_mode,
    get_low_memory_stabilization_seconds,
    get_navigation_timeout_ms,
    get_probe_timeout_ms,
    is_context_closed,
    is_last_usable_page,
    launch_browser_session,
)
from daily_streak.history import RunHistoryStore
from daily_streak.lock import DEFAULT_STALE_LOCK_SECONDS, RunLock
from daily_streak.models import DeliveryStatus, RecipientResult, RecipientStatus, RunSummary
from daily_streak.navigation import navigate_to_messages
from daily_streak.paths import get_data_dir
from daily_streak.recipients import RecipientService, username_from_href
from daily_streak.state import DailyState, DailyStateStore, logical_now, normalize_username
from daily_streak.tiktok import (
    handle_screen_time_popup,
    handle_sleep_hours_popup,
    wait_for_captcha_if_present,
)
from daily_streak.videos import (
    VideoPool,
    extract_canonical_video_id,
    resolve_canonical_video,
)


logger = logging.getLogger(__name__)


class DeliveryVerificationError(RuntimeError):
    """Base class for delivery verification failures."""


class DeliveryUnconfirmedError(DeliveryVerificationError):
    """Enter or submit was pressed, but delivery could not be confirmed within the timeout.

    This outcome is ambiguous; callers must not automatically retry.
    """


class DeliveryConfirmedNotSentError(DeliveryVerificationError):
    """Submission did not occur; safe to retry if attempts remain."""


def extract_video_identifier(video_url: str) -> str:
    """Extract a stable video ID or shortcode from a TikTok video URL."""

    return extract_canonical_video_id(video_url)


DELIVERY_VERIFICATION_SCRIPT = """
(args) => {
    const { expectedVideoId, expectedUrl, initialCount, expectedUsername } = args;
    const peerName = (expectedUsername || "").replace(/^@/, "").toLowerCase();
    const items = Array.from(document.querySelectorAll("[data-e2e='dm-new-chat-item']"));
    const currentCount = items.length;

    const startIdx = Math.max(0, Math.min(initialCount, currentCount - 2));
    const candidates = items.slice(startIdx);

    const extractFiberData = (sharedEl) => {
        if (!sharedEl) return { itemId: null, isMyself: null };
        const reactKey = Object.keys(sharedEl).find(k => k.startsWith("__reactFiber"));
        if (!reactKey) return { itemId: null, isMyself: null };
        let curr = sharedEl[reactKey];
        let itemId = null;
        let isMyself = null;
        for (let d = 0; d < 15 && curr; d++) {
            if (curr.memoizedProps) {
                if (curr.memoizedProps.itemId && !itemId) {
                    itemId = String(curr.memoizedProps.itemId);
                }
                if (typeof curr.memoizedProps.isMyself === "boolean" && isMyself === null) {
                    isMyself = curr.memoizedProps.isMyself;
                }
            }
            curr = curr.return;
        }
        return { itemId, isMyself };
    };

    for (let i = candidates.length - 1; i >= 0; i--) {
        const item = candidates[i];

        // In TikTok DM, messages sent by the peer show an avatar linking to the peer profile
        const isFromPeer = Boolean(peerName && item.querySelector("a[href*='/@" + peerName + "']"));
        if (isFromPeer) {
            continue;
        }

        const hContainer = item.querySelector("[class*='HorizontalContainer']") || item;
        const isRowReverse = window.getComputedStyle(hContainer).flexDirection === "row-reverse";
        if (peerName && !isRowReverse && item.querySelector("a[href*='/@" + peerName + "']")) {
            continue;
        }

        const sharedVideo = item.querySelector("[data-e2e='dm-new-shared-video']");
        const links = Array.from(item.querySelectorAll("a")).map(a => a.href || "");
        const allText = (item.innerText || "") + " " + (item.textContent || "");

        // Native shared video card check
        if (sharedVideo) {
            const { itemId, isMyself } = extractFiberData(sharedVideo);
            if (isMyself === false) {
                continue;
            }

            const matchesFiberId = Boolean(
                expectedVideoId && itemId && (
                    itemId === expectedVideoId ||
                    expectedVideoId.includes(itemId) ||
                    itemId.includes(expectedVideoId)
                )
            );
            const matchesTextId = Boolean(expectedVideoId && allText.includes(expectedVideoId));
            const matchesUrl = Boolean(expectedUrl && (allText.includes(expectedUrl) || links.some(l => l.includes(expectedUrl))));

            if (matchesFiberId || matchesTextId || matchesUrl || (currentCount > initialCount && !expectedVideoId)) {
                return {
                    verified: true,
                    current_count: currentCount,
                    matched_by: "native_video_card",
                    matched_item_id: itemId || expectedVideoId,
                };
            }
        }

        // Raw text / URL link check
        const matchesUrl = Boolean(expectedUrl && (allText.includes(expectedUrl) || links.some(l => l.includes(expectedUrl))));
        const matchesId = Boolean(expectedVideoId && (allText.includes(expectedVideoId) || links.some(l => l.includes(expectedVideoId))));

        if (matchesUrl || matchesId) {
            return {
                verified: true,
                current_count: currentCount,
                matched_by: "raw_url",
            };
        }
    }

    const composer = document.querySelector(".public-DraftEditor-content, [contenteditable='true']");
    const composerText = composer ? (composer.innerText || composer.textContent || "").trim() : "";
    const composerCleared = composerText === "";

    return {
        verified: false,
        current_count: currentCount,
        composer_cleared: composerCleared,
    };
}
"""


class SenderNotifier(Protocol):
    def desktop(self, title: str, message: str) -> None:
        """Display a local notification."""

    async def telegram(self, message: str) -> None:
        """Send a Telegram notification when configured."""

    async def captcha_alert(self, page_url: str) -> None:
        """Notify the user about CAPTCHA intervention."""


RecipientResultCallback = Callable[[RecipientResult, RunSummary], None]


def show_cookie_warning_if_ui_running() -> None:
    """Show the legacy GUI warning only when Tkinter is already active."""

    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter._default_root
        if root:
            root.after(
                0,
                lambda: messagebox.showwarning(
                    "Cookies expired",
                    "Cookies expired or invalid. Please re-export from EditThisCookie.",
                ),
            )
    except Exception:
        logger.debug("Could not show cookie expiry warning in UI", exc_info=True)


GENERIC_SHARE_STRUCTURAL_MARKERS: frozenset[str] = frozenset({
    "share-user-item",
    "share-user-list",
    "share-user-info",
    "share-user-avatar",
    "share-user-name",
    "share-user",
    "share-item",
    "share-items",
    "share-list",
    "share-avatar",
    "share-avatars",
    "share-icon",
    "share-btn",
    "share-button",
    "share-dialog",
    "share-modal",
    "share-search",
    "share-search-input",
    "share-input",
    "share-container",
    "share-panel",
    "share-header",
    "share-title",
    "share-close",
    "share-checkbox",
    "share-check",
    "share-select",
    "share-selected",
    "share-unselected",
    "user-item",
    "user-list",
    "user-info",
    "user-avatar",
    "user-name",
    "user",
    "item",
    "items",
    "list",
    "avatar",
    "avatars",
    "icon",
    "btn",
    "button",
    "dialog",
    "modal",
    "search",
    "search-input",
    "input",
    "container",
    "panel",
    "header",
    "title",
    "close",
    "checkbox",
    "check",
    "select",
    "selected",
    "unselected",
})


def parse_share_avatar_username(val: str | None) -> str | None:
    """Extract a verified username from an avatar element data-e2e attribute.

    Returns normalized username if valid and not a structural row/modal marker,
    or None otherwise.
    """
    if not val:
        return None
    val_clean = str(val).strip()
    val_lower = val_clean.lower()
    if val_lower in GENERIC_SHARE_STRUCTURAL_MARKERS:
        return None

    raw_user: str | None = None
    for prefix in ("share-avatar-", "avatar-", "share-"):
        if val_lower.startswith(prefix):
            candidate = val_clean[len(prefix):].strip().lstrip("@")
            if candidate and candidate.lower() not in GENERIC_SHARE_STRUCTURAL_MARKERS:
                raw_user = candidate
            break

    if raw_user and re.fullmatch(r"^[A-Za-z0-9_.-]+$", raw_user):
        return normalize_username(raw_user)
    return None


class RowSelectionState(str, Enum):
    """Tri-state recipient row selection indicator in the share modal."""

    SELECTED = "selected"
    UNSELECTED = "unselected"
    UNKNOWN = "unknown"


class TikTokSender:
    """Send one random pool video per recipient with isolated retries."""

    def __init__(
        self,
        config: Mapping[str, Any],
        notifier: SenderNotifier,
        video_pool: VideoPool,
        recipient_service: RecipientService | None = None,
        state_store: DailyStateStore | None = None,
        history_store: RunHistoryStore | None = None,
        run_lock: RunLock | None = None,
        on_recipient_result: RecipientResultCallback | None = None,
        now_provider: Callable[[], datetime] | None = None,
        streak_refresher: Callable[..., Any] | None = None,
        config_path: str | Path | None = None,
        profile_resolver: Callable[[int], Any] | None = None,
        streak_cache: Any = None,
    ) -> None:
        self.config = config
        self.notifier = notifier
        self.video_pool = video_pool
        raw_tiktok_config = config.get("tiktok", {})
        self.tiktok_config: Mapping[str, Any] = (
            raw_tiktok_config if isinstance(raw_tiktok_config, Mapping) else {}
        )
        self.message_format = (
            str(self.tiktok_config.get("message_format", "native_video_card"))
            .strip()
            .lower()
        )
        self.allow_raw_url_fallback = bool(
            self.tiktok_config.get("allow_raw_url_fallback", False)
        )
        raw_recipients = config.get("recipients", [])
        self.recipients: Sequence[Mapping[str, Any]] = (
            raw_recipients if isinstance(raw_recipients, list) else []
        )
        self.action_timeout_ms = get_action_timeout_ms(config)
        self.navigation_timeout_ms = get_navigation_timeout_ms(config)
        self.content_ready_timeout_ms = get_content_ready_timeout_ms(config)
        self.dm_readiness_timeout_ms = get_dm_readiness_timeout_ms(config)
        self.dm_load_attempts = get_dm_load_attempts(config)
        self.probe_timeout_ms = get_probe_timeout_ms(config)
        self.recipient_service = recipient_service or RecipientService(
            navigation_timeout_ms=self.navigation_timeout_ms,
            action_timeout_ms=self.action_timeout_ms,
            content_ready_timeout_ms=self.content_ready_timeout_ms,
            dm_readiness_timeout_ms=self.dm_readiness_timeout_ms,
            dm_load_attempts=self.dm_load_attempts,
            probe_timeout_ms=self.probe_timeout_ms,
            low_memory_mode=get_low_memory_mode(config),
            low_memory_stabilization_seconds=get_low_memory_stabilization_seconds(config),
        )
        raw_schedule_config = config.get("schedule", {})
        schedule_config: Mapping[str, Any] = (
            raw_schedule_config if isinstance(raw_schedule_config, Mapping) else {}
        )
        self.schedule_timezone = str(schedule_config.get("timezone", "Asia/Ho_Chi_Minh"))
        data_dir = get_data_dir(config)
        self.state_store = state_store or DailyStateStore(data_dir)
        self.history_store = history_store or RunHistoryStore(data_dir)
        self.run_lock = run_lock or RunLock(
            data_dir,
            stale_seconds=self._run_lock_stale_seconds(config),
        )
        self.on_recipient_result = on_recipient_result
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self.streak_refresher = streak_refresher
        self._active_dm_page: Any = None

        self.config_path: Path | None = None
        if config_path is not None:
            self.config_path = Path(config_path)
        elif "_config_path" in config:
            self.config_path = Path(str(config["_config_path"]))
        elif "config_path" in config:
            self.config_path = Path(str(config["config_path"]))

        self.profile_resolver = profile_resolver
        self.streak_cache = streak_cache

    def _get_streak_cache(self) -> Any:
        if self.streak_cache is not None:
            return self.streak_cache
        try:
            from daily_streak.streak_reader import StreakCache

            data_dir = get_data_dir(self.config)
            self.streak_cache = StreakCache(data_dir)
            return self.streak_cache
        except Exception:
            return None

    def _get_recipient_user_id(self, recipient: Mapping[str, Any]) -> int | None:
        """Extract or resolve the verified numeric user ID for a recipient."""
        raw_uid = recipient.get("user_id") or recipient.get("uid")
        if raw_uid is not None:
            try:
                uid_int = int(raw_uid)
                if uid_int > 0:
                    return uid_int
            except (ValueError, TypeError):
                pass

        username = recipient.get("username")
        if username:
            norm_user = normalize_username(str(username))
            cache = self._get_streak_cache()
            if cache is not None:
                record = cache.get(norm_user)
                if record and record.user_id is not None:
                    try:
                        uid_val = int(record.user_id)
                        if uid_val > 0:
                            return uid_val
                    except (ValueError, TypeError):
                        pass
        return None

    def _resolve_cookie_file(self) -> Path:
        """Resolve the cookie file path, relative to the config file directory if not absolute."""
        raw_cookie = self.config.get("cookie_file", "cookies.json")
        p = Path(str(raw_cookie)).expanduser()
        if p.is_absolute():
            return p.resolve()
        if self.config_path is not None:
            return (self.config_path.parent / p).resolve()
        cfg_val = self.config.get("_config_path") or self.config.get("config_path")
        if cfg_val:
            return (Path(str(cfg_val)).parent / p).resolve()
        return p.resolve()

    def _resolve_profile(self, user_id: int) -> Any:
        if self.profile_resolver is not None:
            return self.profile_resolver(user_id)
        from daily_streak.recipients import resolve_profile_by_user_id

        cookie_file = self._resolve_cookie_file()
        return resolve_profile_by_user_id(
            user_id,
            config=self.config,
            cookie_file=cookie_file,
        )

    def _on_recipient_recovered(
        self,
        recipient: Mapping[str, Any],
        new_name: str,
        new_username: str,
        user_id: int,
    ) -> None:
        """Update recipient metadata in-memory, in config, and in cache."""
        old_username = str(recipient.get("username") or "")
        old_name = str(recipient.get("name") or "")

        # 1. Update recipient dict if mutable
        if isinstance(recipient, dict):
            recipient["name"] = new_name
            if new_username:
                recipient["username"] = new_username
            recipient["user_id"] = user_id

        # 2. Update config recipients list with strict matching priority
        raw_recipients = self.config.get("recipients")
        target_entry: dict[str, Any] | None = None

        if isinstance(raw_recipients, list):
            dict_recipients = [r for r in raw_recipients if isinstance(r, dict)]

            # Step 1: Verified UID match
            uid_matches: list[dict[str, Any]] = []
            if user_id:
                for r in dict_recipients:
                    r_uid = r.get("user_id") or r.get("uid")
                    if r_uid is not None:
                        try:
                            if int(r_uid) == int(user_id):
                                uid_matches.append(r)
                        except (ValueError, TypeError):
                            pass

            if len(uid_matches) == 1:
                target_entry = uid_matches[0]
            elif len(uid_matches) > 1:
                logger.warning(
                    "Ambiguous config match: found %d recipient entries with user_id %s; skipping config update.",
                    len(uid_matches),
                    user_id,
                )
            else:
                # Step 2: Unique exact username match
                norm_old_user = normalize_username(old_username) if old_username else ""
                username_matches: list[dict[str, Any]] = []
                if norm_old_user:
                    for r in dict_recipients:
                        r_user = normalize_username(str(r.get("username") or ""))
                        if r_user == norm_old_user:
                            username_matches.append(r)

                if len(username_matches) == 1:
                    target_entry = username_matches[0]
                elif len(username_matches) > 1:
                    logger.warning(
                        "Ambiguous config match: found %d recipient entries with username @%s; skipping config update.",
                        len(username_matches),
                        old_username,
                    )
                else:
                    # Step 3: Old display name as last resort only if uniquely matched
                    norm_old_name = old_name.strip().casefold()
                    name_matches: list[dict[str, Any]] = []
                    if norm_old_name:
                        for r in dict_recipients:
                            r_name = str(r.get("name") or "").strip().casefold()
                            if r_name == norm_old_name:
                                name_matches.append(r)

                    if len(name_matches) == 1:
                        target_entry = name_matches[0]
                    elif len(name_matches) > 1:
                        logger.warning(
                            "Ambiguous config match: found %d recipient entries with display name %r; skipping config update.",
                            len(name_matches),
                            old_name,
                        )
                    else:
                        logger.warning(
                            "No matching recipient entry found in config for UID %s, username %r, display_name %r; skipping config update.",
                            user_id,
                            old_username,
                            old_name,
                        )

        if target_entry is not None:
            target_entry["name"] = new_name
            if new_username:
                target_entry["username"] = new_username
            target_entry["user_id"] = user_id

            # 3. Persist updated configuration if path is known
            if self.config_path is not None:
                try:
                    from daily_streak.config import save_config

                    save_config(self.config, self.config_path)
                    logger.info("Persisted recovered recipient %r to %s", new_name, self.config_path)
                except Exception as exc:
                    logger.warning("Failed to persist updated config to %s: %s", self.config_path, exc)

        # 4. Synchronize StreakCache
        cache = self._get_streak_cache()
        if cache is not None:
            try:
                from daily_streak.streak_reader import StreakRecord, StreakStatus

                target_user = new_username or old_username
                if target_user:
                    record = cache.get(target_user)
                    if record is not None:
                        record.user_id = user_id
                        record.username = target_user
                        cache.set(record)
                    else:
                        cache.set(StreakRecord(
                            username=target_user,
                            status=StreakStatus.UNKNOWN,
                            user_id=user_id,
                        ))
            except Exception as exc:
                logger.warning("Failed to update streak cache for UID %s: %s", user_id, exc)

    def _filter_service_kwargs(
        self,
        func: Callable[..., Any],
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """Filter kwargs to only those accepted by func's signature."""
        try:
            sig = inspect.signature(func)
            for param in sig.parameters.values():
                if param.kind == inspect.Parameter.VAR_KEYWORD:
                    return dict(kwargs)
            accepted = set(sig.parameters.keys())
            return {k: v for k, v in kwargs.items() if k in accepted}
        except (ValueError, TypeError):
            return dict(kwargs)

    async def _verify_chat_header_identity(
        self,
        page: Any,
        expected_username: str,
        context: Any = None,
    ) -> None:
        """Verify that the opened DM conversation header username exactly matches expected recipient.

        Fails closed with DeliveryConfirmedNotSentError if missing, mismatched,
        or cannot be reliably read.
        """
        if context is not None and is_context_closed(context):
            raise DeliveryConfirmedNotSentError("Browser context is closed.")
        extract_fn = getattr(self.recipient_service, "extract_username_from_chat", None)
        if not callable(extract_fn):
            raise DeliveryConfirmedNotSentError(
                f"Recipient service does not support chat header username extraction for expected @{expected_username}"
            )
        try:
            chat_username = await extract_fn(page)
        except Exception as exc:
            if context is not None and is_context_closed(context):
                raise DeliveryConfirmedNotSentError("Browser context is closed.") from exc
            raise DeliveryConfirmedNotSentError(
                f"Failed to read chat header username for expected @{expected_username}: {exc}"
            ) from exc

        if not chat_username or not str(chat_username).strip():
            raise DeliveryConfirmedNotSentError(
                f"Chat header username is missing or empty for expected @{expected_username}"
            )

        norm_chat = normalize_username(str(chat_username))
        norm_expected = normalize_username(str(expected_username))
        if norm_chat != norm_expected:
            raise DeliveryConfirmedNotSentError(
                f"Opened chat header @{chat_username} does not match expected @{expected_username}"
            )

    async def send_daily_links(self, *, force: bool = False) -> RunSummary:
        """Complete session setup, then send every configured recipient once."""

        self.run_lock.acquire()
        try:
            summary = self._new_summary()
            daily_state = self._load_daily_state()
            if not self.recipients:
                logger.warning("No recipients configured.")
                return self._finish_run(summary)

            cookie_file = self._resolve_cookie_file()
            if not check_cookies_valid(cookie_file):
                message = "Cookies expired or invalid. Please re-export from EditThisCookie."
                logger.error(message)
                summary.setup_error = message
                show_cookie_warning_if_ui_running()
                return self._finish_run(summary)

            # Imported lazily so summary/result tests do not require a live browser
            # installation, while normal application use still uses Playwright.
            from playwright.async_api import async_playwright

            async with async_playwright() as playwright:
                session = await launch_browser_session(playwright, self.config)
                try:
                    cookies_loaded = await session.load_cookies(cookie_file)
                    page = await session.get_page()
                    await self._ensure_logged_in(page, cookies_loaded=cookies_loaded)
                    summary = await self.send_recipients(
                        session.context,
                        summary,
                        daily_state=daily_state,
                        force=force,
                        dm_page=page,
                    )
                finally:
                    await session.close()
            await self._auto_refresh_streaks(summary)
            return summary
        finally:
            self.run_lock.release()

    async def _auto_refresh_streaks(self, summary: RunSummary) -> None:
        """Automatically refresh streaks once after sending if any messages were attempted."""
        if summary.setup_error:
            return
        has_attempts = any(
            result.status is not RecipientStatus.SKIPPED
            for result in summary.results
        )
        if not has_attempts:
            logger.debug("No messages were attempted in this batch; skipping post-send streak refresh.")
            return

        logger.info("Batch attempted sends; automatically refreshing recipient streaks once.")
        try:
            if self.streak_refresher is not None:
                await self.streak_refresher(self.config, self.recipients)
            else:
                from daily_streak.recipient_actions import refresh_streaks

                await refresh_streaks(self.config, self.recipients, force=True)
        except Exception as exc:
            logger.warning("Post-send automatic streak refresh failed (non-fatal): %s", exc)

    async def send_recipients(
        self,
        context: Any,
        summary: RunSummary | None = None,
        *,
        force: bool = False,
        daily_state: DailyState | None = None,
        dm_page: Any = None,
    ) -> RunSummary:
        """Send recipients sequentially with resume-safe state and history."""

        summary = summary if summary is not None else self._new_summary()
        daily_state = daily_state or self._load_daily_state()
        self._active_dm_page = dm_page
        created_dm_page_internally = False

        if is_context_closed(context):
            message = "Browser context was closed before sending started."
            logger.error(message)
            summary.setup_error = message
            return self._finish_run(summary)

        try:
            for idx, recipient in enumerate(self.recipients):
                if is_context_closed(context):
                    logger.error("Browser context was closed during run; aborting remaining recipients.")
                    for remaining in self.recipients[idx:]:
                        rem_name = str(remaining.get("name") or "recipient")
                        rem_user = str(remaining.get("username") or "") or None
                        abort_result = RecipientResult(
                            recipient_name=rem_name,
                            username=rem_user,
                            selected_video=None,
                            status=RecipientStatus.FAILED,
                            delivery_status=DeliveryStatus.CONFIRMED_NOT_SENT,
                            error_message="Batch aborted: browser context or browser was closed unexpectedly.",
                            elapsed_seconds=0.0,
                        )
                        summary.add_result(abort_result)
                        self._report_recipient_result(abort_result, summary)
                    break

                result = self._resume_skip_result(recipient, daily_state, force=force)
                if result is None:
                    # If active DM page was closed, reset it so next recipient recreates cleanly
                    if self._active_dm_page is not None and getattr(self._active_dm_page, "is_closed", lambda: False)():
                        logger.info("Retained DM page was closed; recreating page for next recipient")
                        self._active_dm_page = None

                    # If no active DM page, try to obtain or create one
                    if self._active_dm_page is None and callable(getattr(context, "new_page", None)):
                        pages = getattr(context, "pages", ())
                        alive_pages = [p for p in pages if not getattr(p, "is_closed", lambda: False)()]
                        if alive_pages:
                            self._active_dm_page = alive_pages[0]
                        else:
                            if is_context_closed(context):
                                logger.error("Cannot create new page: browser context is closed.")
                                for remaining in self.recipients[idx:]:
                                    abort_result = RecipientResult(
                                        recipient_name=str(remaining.get("name") or "recipient"),
                                        username=str(remaining.get("username") or "") or None,
                                        selected_video=None,
                                        status=RecipientStatus.FAILED,
                                        delivery_status=DeliveryStatus.CONFIRMED_NOT_SENT,
                                        error_message="Batch aborted: browser context is closed.",
                                        elapsed_seconds=0.0,
                                    )
                                    summary.add_result(abort_result)
                                    self._report_recipient_result(abort_result, summary)
                                break
                            try:
                                t_init = time.monotonic()
                                self._active_dm_page = await context.new_page()
                                created_dm_page_internally = True
                                logger.info("Timing: DM page initialization took %.2fs", time.monotonic() - t_init)
                            except Exception as np_exc:
                                logger.error("Cannot create new DM page: %s", np_exc)
                                for remaining in self.recipients[idx:]:
                                    abort_result = RecipientResult(
                                        recipient_name=str(remaining.get("name") or "recipient"),
                                        username=str(remaining.get("username") or "") or None,
                                        selected_video=None,
                                        status=RecipientStatus.FAILED,
                                        delivery_status=DeliveryStatus.CONFIRMED_NOT_SENT,
                                        error_message=f"Batch aborted: browser context page creation failed: {np_exc}",
                                        elapsed_seconds=0.0,
                                    )
                                    summary.add_result(abort_result)
                                    self._report_recipient_result(abort_result, summary)
                                break

                    result = await self._send_recipient_with_retries(context, recipient)
                summary.add_result(result)
                if result.status is RecipientStatus.SUCCESS:
                    self._persist_success(daily_state, result)
                self._report_recipient_result(result, summary)
                await self._sleep_between_messages()
        finally:
            if created_dm_page_internally and self._active_dm_page is not None:
                if not is_last_usable_page(context, self._active_dm_page):
                    if hasattr(self._active_dm_page, "close") and not getattr(self._active_dm_page, "is_closed", lambda: False)():
                        with suppress(Exception):
                            await self._active_dm_page.close()
            self._active_dm_page = None

        return self._finish_run(summary)

    async def _ensure_logged_in(self, page: Any, cookies_loaded: bool = False) -> None:
        await navigate_to_messages(page, navigation_timeout_ms=self.navigation_timeout_ms)
        await self._handle_tiktok_interruptions(page)

        if "login" in page.url.lower():
            if cookies_loaded:
                logger.error("Cookie expired, please re-export")
            await self.notifier.telegram(
                "TikTok login is required. Please log in using the open browser window."
            )
            self.notifier.desktop(
                "TikTok login required",
                "Please log in using the open browser window. The sender is waiting.",
            )
            logger.info("Waiting for TikTok login.")
            await page.wait_for_url(lambda url: "login" not in url.lower(), timeout=0)
            await self._handle_tiktok_interruptions(page)

    async def _send_recipient_with_retries(
        self,
        context: Any,
        recipient: Mapping[str, Any],
    ) -> RecipientResult:
        recipient_name = str(recipient.get("name") or "recipient")
        username_value = recipient.get("username")
        username = str(username_value) if username_value else None
        start_time = time.monotonic()

        if not username:
            message = "Username is missing."
            logger.warning("Skipping %s because username is missing.", recipient_name)
            return RecipientResult(
                recipient_name=recipient_name,
                username=None,
                selected_video=None,
                status=RecipientStatus.SKIPPED,
                delivery_status=DeliveryStatus.CONFIRMED_NOT_SENT,
                error_message=message,
                elapsed_seconds=time.monotonic() - start_time,
            )

        try:
            selected_video = self.video_pool.random_video()
        except Exception as exc:
            logger.exception("Could not select a video for %s (@%s)", recipient_name, username)
            return RecipientResult(
                recipient_name=recipient_name,
                username=username,
                selected_video=None,
                status=RecipientStatus.FAILED,
                delivery_status=DeliveryStatus.CONFIRMED_NOT_SENT,
                error_message=self._error_message(exc),
                elapsed_seconds=time.monotonic() - start_time,
            )

        attempts_allowed = self._recipient_attempts_allowed()
        last_error: Exception | None = None
        for attempt in range(1, attempts_allowed + 1):
            if is_context_closed(context):
                last_error = RuntimeError("Browser context was closed during recipient send attempts.")
                break

            try:
                if self._active_dm_page is not None and getattr(self._active_dm_page, "is_closed", lambda: False)():
                    self._active_dm_page = None

                await self._send_recipient_once(context, recipient, selected_video)
            except DeliveryUnconfirmedError as exc:
                # Ambiguous delivery: Enter or submit was pressed, but verification timed out.
                # Do NOT automatically retry during the same operation to avoid duplicate sends!
                # Do NOT close the shared persistent-context page merely because delivery is unconfirmed.
                logger.warning(
                    "delivery_unconfirmed: Delivery unconfirmed on attempt %d for %s (@%s): %s. Will not retry.",
                    attempt,
                    recipient_name,
                    username,
                    exc,
                )
                if is_context_closed(context):
                    self._active_dm_page = None
                return RecipientResult(
                    recipient_name=recipient_name,
                    username=username,
                    selected_video=selected_video,
                    status=RecipientStatus.UNCONFIRMED,
                    delivery_status=DeliveryStatus.UNCONFIRMED,
                    attempts=attempt,
                    error_message=self._error_message(exc),
                    elapsed_seconds=time.monotonic() - start_time,
                )
            except asyncio.CancelledError:
                if not is_last_usable_page(context, self._active_dm_page):
                    if self._active_dm_page is not None:
                        with suppress(Exception):
                            if hasattr(self._active_dm_page, "close") and not getattr(self._active_dm_page, "is_closed", lambda: False)():
                                await self._active_dm_page.close()
                        self._active_dm_page = None
                raise
            except Exception as exc:
                last_error = exc
                logger.exception(
                    "Send attempt %d/%d failed for %s (@%s)",
                    attempt,
                    attempts_allowed,
                    recipient_name,
                    username,
                )
                if is_context_closed(context):
                    break
                # If we are retrying, keep the active DM page alive; do not close it merely to retry!
                # If attempts are exhausted, only close it if it's NOT the last usable page in context.
                if attempt >= attempts_allowed:
                    if not is_last_usable_page(context, self._active_dm_page):
                        if self._active_dm_page is not None:
                            with suppress(Exception):
                                if hasattr(self._active_dm_page, "close") and not getattr(self._active_dm_page, "is_closed", lambda: False)():
                                    await self._active_dm_page.close()
                            self._active_dm_page = None
            else:
                logger.info(
                    "delivery_verified: Sent and verified %s to %s (@%s) on attempt %d: %s",
                    self.message_format,
                    recipient_name,
                    username,
                    attempt,
                    selected_video,
                )
                return RecipientResult(
                    recipient_name=recipient_name,
                    username=username,
                    selected_video=selected_video,
                    status=RecipientStatus.SUCCESS,
                    delivery_status=DeliveryStatus.VERIFIED,
                    attempts=attempt,
                    elapsed_seconds=time.monotonic() - start_time,
                )

        assert last_error is not None
        return RecipientResult(
            recipient_name=recipient_name,
            username=username,
            selected_video=selected_video,
            status=RecipientStatus.FAILED,
            delivery_status=DeliveryStatus.CONFIRMED_NOT_SENT,
            attempts=attempts_allowed,
            error_message=self._error_message(last_error),
            elapsed_seconds=time.monotonic() - start_time,
        )

    async def _send_recipient_once(
        self,
        context: Any,
        recipient: Mapping[str, Any],
        selected_video: str,
        *,
        dm_page: Any = None,
    ) -> None:
        username = str(recipient["username"])
        configured_display_name = str(recipient.get("name") or "").strip()
        target_dm_page = dm_page if dm_page is not None else getattr(self, "_active_dm_page", None)

        if self.message_format == "raw_url":
            logger.info(
                "raw_url: Sending video to %s (@%s)",
                configured_display_name or "recipient",
                username,
            )
            await self._send_raw_url_flow(context, recipient, selected_video, username=username, dm_page=target_dm_page)
            return

        # Default: native_video_card
        logger.info(
            "native_video_card: Sharing video to %s (@%s)",
            configured_display_name or "recipient",
            username,
        )
        try:
            await self._send_native_card_flow(context, recipient, selected_video, username=username, dm_page=target_dm_page)
        except DeliveryConfirmedNotSentError as exc:
            if self.allow_raw_url_fallback:
                logger.warning(
                    "native_video_card: Sharing failed before submit for %s (@%s): %s; falling back to raw_url",
                    configured_display_name or "recipient",
                    username,
                    exc,
                )
                await self._send_raw_url_flow(context, recipient, selected_video, username=username, dm_page=target_dm_page)
            else:
                raise

    async def _send_native_card_flow(
        self,
        context: Any,
        recipient: Mapping[str, Any],
        selected_video: str,
        username: str,
        *,
        dm_page: Any = None,
    ) -> None:
        configured_display_name = str(recipient.get("name") or "").strip()
        canonical_url, video_id = resolve_canonical_video(selected_video)

        reused_page = dm_page is not None
        send_submitted = False
        delivery_verified = False
        if not reused_page:
            if is_context_closed(context):
                raise DeliveryConfirmedNotSentError("Cannot open DM page: browser context is closed.")
            t0 = time.monotonic()
            dm_page = await context.new_page()
            logger.info("Timing: DM page initialization took %.2fs", time.monotonic() - t0)

        video_page = None

        def mark_submitted() -> None:
            nonlocal send_submitted
            send_submitted = True

        try:
            # 1. Open recipient's DM conversation first to verify recipient and count initial messages
            t_select = time.monotonic()
            user_id = self._get_recipient_user_id(recipient)
            select_kwargs: dict[str, Any] = {
                "configured_display_name": configured_display_name or None,
                "user_id": user_id,
                "profile_resolver": self._resolve_profile,
                "on_recipient_recovered": lambda new_name, new_user, uid: self._on_recipient_recovered(
                    recipient, new_name, new_user, uid
                ),
            }
            if reused_page:
                select_kwargs["reuse_existing_page"] = True

            call_fn = self.recipient_service.select_recipient_for_username_with_page
            filtered_kwargs = self._filter_service_kwargs(call_fn, select_kwargs)
            dm_page, _ = await call_fn(
                dm_page,
                username,
                self.notifier,
                **filtered_kwargs,
            )

            if is_context_closed(context):
                raise DeliveryConfirmedNotSentError("Browser context was closed during recipient selection.")

            # Update username in case it was refreshed by recipient recovery
            username = str(recipient.get("username") or username)

            if reused_page:
                self._active_dm_page = dm_page

            await self._handle_tiktok_interruptions(dm_page)

            # Strict identity check: chat header must match expected recipient
            await self._verify_chat_header_identity(dm_page, username, context=context)

            initial_item_count = await self._count_chat_items(dm_page)
            logger.info(
                "Timing: Conversation selection and composer readiness took %.2fs for %s",
                time.monotonic() - t_select,
                username,
            )

            # 2. Open video page in separate page and execute native share
            t_video = time.monotonic()
            if is_context_closed(context):
                raise DeliveryConfirmedNotSentError("Browser context was closed before video page could be opened.")
            video_page = await context.new_page()
            try:
                try:
                    await self._share_video_card(
                        video_page,
                        recipient=recipient,
                        canonical_url=canonical_url,
                        video_id=video_id,
                        on_submitted=mark_submitted,
                    )
                except (asyncio.CancelledError, DeliveryConfirmedNotSentError, DeliveryUnconfirmedError):
                    raise
                except Exception as exc:
                    if send_submitted:
                        raise DeliveryUnconfirmedError(
                            f"Exception in native card share flow after submission: {exc}"
                        ) from exc
                    raise DeliveryConfirmedNotSentError(
                        f"Exception in native card share flow before submission: {exc}"
                    ) from exc
            finally:
                if hasattr(video_page, "close"):
                    t_cleanup = time.monotonic()
                    try:
                        await video_page.close()
                        logger.info("Timing: Post-send cleanup (closed video page) took %.2fs", time.monotonic() - t_cleanup)
                    except Exception as exc:
                        logger.warning(
                            "Post-share video page close encountered an error for @%s (send_submitted=%s): %s",
                            username,
                            send_submitted,
                            exc,
                        )
            logger.info("Timing: Video share operation took %.2fs for %s", time.monotonic() - t_video, username)

            # 3. Verify delivery in the recipient's DM
            t_delivery = time.monotonic()
            try:
                await self._wait_for_delivery_evidence(
                    dm_page,
                    canonical_url,
                    initial_item_count=initial_item_count,
                    expected_username=username,
                    expected_video_id=video_id,
                )
                delivery_verified = True
            except (asyncio.CancelledError, DeliveryUnconfirmedError):
                raise
            except Exception as exc:
                raise DeliveryUnconfirmedError(
                    f"Delivery verification failed after native card submission: {exc}"
                ) from exc
            logger.info("Timing: Delivery verification took %.2fs for %s", time.monotonic() - t_delivery, username)

            # 4. Post-verification popup handling (failures here must not cause resend)
            try:
                await self._handle_tiktok_interruptions(dm_page)
            except Exception as exc:
                logger.warning("Post-delivery popup handling encountered an error for @%s: %s", username, exc)
        finally:
                if not reused_page and hasattr(dm_page, "close"):
                    t_close = time.monotonic()
                    try:
                        await dm_page.close()
                        logger.info("Timing: Post-send cleanup (closed DM page) took %.2fs", time.monotonic() - t_close)
                    except Exception as exc:
                        if delivery_verified:
                            logger.warning("Post-delivery DM page close encountered an error for @%s: %s", username, exc)
                        else:
                            logger.warning("DM page cleanup encountered an error for @%s: %s", username, exc)

    async def _send_raw_url_flow(
        self,
        context: Any,
        recipient: Mapping[str, Any],
        selected_video: str,
        username: str,
        *,
        dm_page: Any = None,
    ) -> None:
        configured_display_name = str(recipient.get("name") or "").strip()
        reused_page = dm_page is not None
        if not reused_page:
            if is_context_closed(context):
                raise DeliveryConfirmedNotSentError("Cannot open DM page: browser context is closed.")
            t0 = time.monotonic()
            dm_page = await context.new_page()
            logger.info("Timing: DM page initialization took %.2fs", time.monotonic() - t0)

        page = dm_page
        delivery_verified = False
        try:
            t_select = time.monotonic()
            user_id = self._get_recipient_user_id(recipient)
            select_kwargs: dict[str, Any] = {
                "configured_display_name": configured_display_name or None,
                "user_id": user_id,
                "profile_resolver": self._resolve_profile,
                "on_recipient_recovered": lambda new_name, new_user, uid: self._on_recipient_recovered(
                    recipient, new_name, new_user, uid
                ),
            }
            if reused_page:
                select_kwargs["reuse_existing_page"] = True

            call_fn = self.recipient_service.select_recipient_for_username_with_page
            filtered_kwargs = self._filter_service_kwargs(call_fn, select_kwargs)
            page, _ = await call_fn(
                page,
                username,
                self.notifier,
                **filtered_kwargs,
            )

            if is_context_closed(context):
                raise DeliveryConfirmedNotSentError("Browser context was closed during recipient selection.")

            # Update username in case it was refreshed by recipient recovery
            username = str(recipient.get("username") or username)

            if reused_page:
                self._active_dm_page = page

            await self._handle_tiktok_interruptions(page)

            # Strict identity check: chat header must match expected recipient
            await self._verify_chat_header_identity(page, username, context=context)
            logger.info(
                "Timing: Conversation selection and composer readiness took %.2fs for %s",
                time.monotonic() - t_select,
                username,
            )

            t_send = time.monotonic()
            await self._send_message(page, selected_video, username=username)
            delivery_verified = True
            logger.info("Timing: Video send operation took %.2fs for %s", time.monotonic() - t_send, username)

            # Post-verification popup handling (failures here must not cause resend)
            try:
                await self._handle_tiktok_interruptions(page)
            except Exception as exc:
                logger.warning("Post-delivery popup handling encountered an error for @%s: %s", username, exc)
        finally:
            if not reused_page and hasattr(page, "close"):
                t_close = time.monotonic()
                try:
                    await page.close()
                    logger.info("Timing: Post-send cleanup (closed DM page) took %.2fs", time.monotonic() - t_close)
                except Exception as exc:
                    if delivery_verified:
                        logger.warning("Post-delivery DM page close encountered an error for @%s: %s", username, exc)
                    else:
                        logger.warning("DM page cleanup encountered an error for @%s: %s", username, exc)

    async def _extract_share_row_usernames(self, item: Any) -> set[str]:
        """Extract authoritative username from a share modal recipient row.

        Returns a set containing the single authoritative username if verified and consistent,
        or an empty set if authoritative identity is missing, ambiguous, contradictory,
        or if reading required signals fails.
        """
        authoritative_usernames: set[str] = set()

        # 1. Profile links inside row (authoritative signal)
        locator = getattr(item, "locator", None)
        if callable(locator):
            try:
                links = item.locator("a[href^='/@'], a[href*='tiktok.com/@'], a[href*='/@']")
                if hasattr(links, "evaluate_all") and callable(links.evaluate_all):
                    hrefs = await links.evaluate_all("""(els) => els.map(e => e.getAttribute('href') || '')""")
                    for href in hrefs:
                        u = username_from_href(href)
                        if u:
                            authoritative_usernames.add(normalize_username(u))
                elif hasattr(links, "count") and callable(links.count):
                    cnt = await links.count()
                    for i in range(cnt):
                        link_el = links.nth(i)
                        get_attr = getattr(link_el, "get_attribute", None)
                        if callable(get_attr):
                            href = await get_attr("href")
                            u = username_from_href(href)
                            if u:
                                authoritative_usernames.add(normalize_username(u))
            except Exception as exc:
                logger.warning("Failed to read profile links in share row: %s; failing closed", exc)
                return set()

            # 2. Specific unique-id / handle DOM elements within row whose semantics
            # as a username/unique ID are verified.
            # NOTE: Generic display-name or 'user-name' fields are intentionally excluded.
            for sel in [
                "[data-e2e*='unique-id']",
                "[class*='UniqueId']",
                "[class*='UserSubTitle']",
                "[class*='handle']",
            ]:
                try:
                    el = item.locator(sel)
                    cnt = await el.count() if hasattr(el, "count") and callable(el.count) else 0
                    for i in range(cnt):
                        txt = (await el.nth(i).inner_text()).strip()
                        if txt:
                            if txt.startswith("@"):
                                authoritative_usernames.add(normalize_username(txt))
                            elif re.fullmatch(r"^[A-Za-z0-9_.-]+$", txt):
                                authoritative_usernames.add(normalize_username(txt))
                except Exception as exc:
                    logger.warning("Failed to read handle element %s in share row: %s; failing closed", sel, exc)
                    return set()

        # 3. Avatar button data-e2e attribute if it explicitly encodes the handle
        # Only inspect child elements reliably identified as avatars or identity-bearing elements.
        if callable(locator):
            try:
                avatar_nodes = item.locator(
                    "[data-e2e*='avatar'], [data-e2e^='share-avatar-'], [class*='Avatar'], [class*='avatar']"
                )
                cnt = await avatar_nodes.count() if hasattr(avatar_nodes, "count") and callable(avatar_nodes.count) else 0
                for i in range(cnt):
                    node = avatar_nodes.nth(i)
                    get_node_attr = getattr(node, "get_attribute", None)
                    if callable(get_node_attr):
                        val = await get_node_attr("data-e2e")
                        user = parse_share_avatar_username(val)
                        if user:
                            authoritative_usernames.add(user)
            except Exception as exc:
                logger.warning("Failed to read child avatar data-e2e in share row: %s; failing closed", exc)
                return set()

        # 4. Explicit unique-id attributes on the row item container
        # Note: data-e2e on a generic row container is NEVER treated as an avatar username.
        get_attr = getattr(item, "get_attribute", None)
        if callable(get_attr):
            try:
                for attr in ("data-username", "data-unique-id"):
                    val = await item.get_attribute(attr)
                    if val:
                        val = val.strip().lstrip("@")
                        if val and re.fullmatch(r"^[A-Za-z0-9_.-]+$", val):
                            if val.lower() not in GENERIC_SHARE_STRUCTURAL_MARKERS:
                                authoritative_usernames.add(normalize_username(val))
            except Exception as exc:
                logger.warning("Failed to read attributes on share row: %s; failing closed", exc)
                return set()

        # If multiple authoritative signals disagree, reject the row
        if len(authoritative_usernames) > 1:
            logger.warning(
                "Share row has conflicting authoritative usernames: %s; rejecting row",
                authoritative_usernames,
            )
            return set()

        # If no authoritative identity signal was found, reject the row (never guess)
        if len(authoritative_usernames) == 0:
            return set()

        auth_username = next(iter(authoritative_usernames))

        # 4. Check for contradictory secondary signals (e.g. @handle tokens in row text)
        inner_text_fn = getattr(item, "inner_text", None)
        if callable(inner_text_fn):
            try:
                raw_text = await item.inner_text()
                if raw_text:
                    for match in re.finditer(r"@([A-Za-z0-9_.-]+)", raw_text):
                        token_user = normalize_username(match.group(1))
                        if token_user != auth_username:
                            logger.warning(
                                "Share row authoritative username @%s contradicts text token @%s; rejecting row",
                                auth_username,
                                token_user,
                            )
                            return set()
            except Exception as exc:
                logger.warning("Failed to read inner text of share row: %s; failing closed", exc)
                return set()

        return {auth_username}

    async def _extract_chip_username(self, chip: Any) -> str | None:
        """Extract exact verifiable username from a selected recipient chip.

        Collects all available authoritative identity signals from the chip and
        rejects conflicting or ambiguous identities, or failed inspections.
        """
        authoritative_usernames: set[str] = set()

        # 1. Profile links inside chip
        locator = getattr(chip, "locator", None)
        if callable(locator):
            try:
                links = chip.locator("a[href^='/@'], a[href*='tiktok.com/@'], a[href*='/@']")
                if hasattr(links, "evaluate_all") and callable(links.evaluate_all):
                    hrefs = await links.evaluate_all("""(els) => els.map(e => e.getAttribute('href') || '')""")
                    for href in hrefs:
                        u = username_from_href(href)
                        if u:
                            authoritative_usernames.add(normalize_username(u))
                elif hasattr(links, "count") and callable(links.count):
                    cnt = await links.count()
                    for i in range(cnt):
                        link_el = links.nth(i)
                        get_attr = getattr(link_el, "get_attribute", None)
                        if callable(get_attr):
                            href = await get_attr("href")
                            u = username_from_href(href)
                            if u:
                                authoritative_usernames.add(normalize_username(u))
            except Exception as exc:
                logger.warning("Failed to read profile links in selected chip: %s; failing closed", exc)
                return None

            # 2. Specific unique-id / handle elements inside chip
            for sel in [
                "[data-e2e*='unique-id']",
                "[class*='UniqueId']",
                "[class*='handle']",
            ]:
                try:
                    el = chip.locator(sel)
                    cnt = await el.count() if hasattr(el, "count") and callable(el.count) else 0
                    for i in range(cnt):
                        txt = (await el.nth(i).inner_text()).strip()
                        if txt:
                            if txt.startswith("@"):
                                authoritative_usernames.add(normalize_username(txt))
                            elif re.fullmatch(r"^[A-Za-z0-9_.-]+$", txt):
                                authoritative_usernames.add(normalize_username(txt))
                except Exception as exc:
                    logger.warning("Failed to read handle element %s in selected chip: %s; failing closed", sel, exc)
                    return None

        # 3. Attributes on the chip itself
        get_attr = getattr(chip, "get_attribute", None)
        if callable(get_attr):
            try:
                for attr in ("data-username", "data-unique-id"):
                    val = await chip.get_attribute(attr)
                    if val:
                        val = val.strip().lstrip("@")
                        if val and re.fullmatch(r"^[A-Za-z0-9_.-]+$", val):
                            authoritative_usernames.add(normalize_username(val))

                data_e2e = await chip.get_attribute("data-e2e")
                if data_e2e:
                    raw_user: str | None = None
                    for prefix in ("share-", "chip-"):
                        if data_e2e.startswith(prefix):
                            candidate = data_e2e[len(prefix):].strip().lstrip("@")
                            if candidate and candidate.lower() not in GENERIC_SHARE_STRUCTURAL_MARKERS:
                                raw_user = candidate
                            break
                    if raw_user and re.fullmatch(r"^[A-Za-z0-9_.-]+$", raw_user):
                        authoritative_usernames.add(normalize_username(raw_user))
            except Exception as exc:
                logger.warning("Failed to read attribute on selected chip: %s; failing closed", exc)
                return None

        # If multiple conflicting authoritative signals exist on the chip, reject
        if len(authoritative_usernames) > 1:
            logger.warning(
                "Selected chip has conflicting authoritative usernames: %s; rejecting",
                authoritative_usernames,
            )
            return None

        # 4. Text tokens: parse explicit @handle tokens
        text_handles: set[str] = set()
        inner_text_fn = getattr(chip, "inner_text", None)
        if callable(inner_text_fn):
            try:
                txt = (await chip.inner_text()).strip()
                if txt:
                    for match in re.finditer(r"@([A-Za-z0-9_.-]+)", txt):
                        text_handles.add(normalize_username(match.group(1)))
            except Exception as exc:
                logger.warning("Failed to read text on selected chip: %s; failing closed", exc)
                return None

        if len(authoritative_usernames) == 1:
            auth_user = next(iter(authoritative_usernames))
            # Any explicit text handle token must not contradict auth_user
            if any(th != auth_user for th in text_handles):
                logger.warning(
                    "Selected chip authoritative username @%s contradicts text handle %s; rejecting",
                    auth_user,
                    text_handles,
                )
                return None
            return auth_user

        # If no profile link or unique-id attribute was present, check text handle tokens
        if len(text_handles) == 1:
            return next(iter(text_handles))
        if len(text_handles) > 1:
            logger.warning("Selected chip has multiple conflicting text handles: %s; rejecting", text_handles)
            return None

        return None

    async def _is_row_element_selected(self, item: Any) -> RowSelectionState:
        """Inspect a single share modal row element to determine if it is selected."""
        explicit_trues: list[str] = []
        explicit_falses: list[str] = []

        # 1. Explicit state attributes
        get_attr = getattr(item, "get_attribute", None)
        if callable(get_attr):
            for attr in ("aria-checked", "aria-selected", "data-selected"):
                val = await item.get_attribute(attr)
                if val in ("true", "True", True):
                    explicit_trues.append(f"{attr}=true")
                elif val in ("false", "False", False):
                    explicit_falses.append(f"{attr}=false")

        # 2. Child checkbox elements
        locator = getattr(item, "locator", None)
        if callable(locator):
            cb = item.locator("input[type='checkbox'], [role='checkbox'], [class*='Checkbox'], svg[class*='Check']").first
            if hasattr(cb, "count") and callable(cb.count) and await cb.count() > 0:
                if hasattr(cb, "is_checked") and callable(cb.is_checked):
                    if await cb.is_checked():
                        explicit_trues.append("cb.is_checked=true")
                    else:
                        explicit_falses.append("cb.is_checked=false")
                get_cb_attr = getattr(cb, "get_attribute", None)
                if callable(get_cb_attr):
                    val = await cb.get_attribute("aria-checked")
                    if val in ("true", "True", True):
                        explicit_trues.append("cb[aria-checked]=true")
                    elif val in ("false", "False", False):
                        explicit_falses.append("cb[aria-checked]=false")

        # Check explicit conflicts between true and false signals
        if explicit_trues and explicit_falses:
            logger.warning(
                "Conflicting explicit selection signals on row: trues=%s, falses=%s; failing closed",
                explicit_trues,
                explicit_falses,
            )
            raise ValueError(
                f"Conflicting explicit selection signals on row: trues={explicit_trues}, falses={explicit_falses}"
            )

        # 3. Inspect CSS classes
        has_positive_class = False
        has_negative_class = False
        if callable(get_attr):
            cls = await item.get_attribute("class")
            if cls:
                tokens = {t.strip() for t in cls.lower().split() if t.strip()}
                negative_tokens = {"unselected", "not-selected", "unchecked", "not-checked"}
                if tokens & negative_tokens:
                    has_negative_class = True

                for t in tokens:
                    if t in {"selected", "checked", "is-selected", "is-checked"}:
                        has_positive_class = True
                        break
                    if t.endswith("--selected") or t.endswith("--checked"):
                        has_positive_class = True
                        break

        # A negative class must never be treated as selected
        if has_negative_class:
            if explicit_trues:
                logger.warning(
                    "Conflicting selection state: explicit true %s with negative class; failing closed",
                    explicit_trues,
                )
                raise ValueError("Explicit true selection attribute contradicts negative class")
            return RowSelectionState.UNSELECTED

        # Positive class contradicting explicit false
        if has_positive_class and explicit_falses:
            logger.warning(
                "Conflicting selection state: positive class contradicts explicit false %s; failing closed",
                explicit_falses,
            )
            raise ValueError(f"Positive CSS class contradicts explicit false attribute: {explicit_falses}")

        if explicit_trues or has_positive_class:
            return RowSelectionState.SELECTED

        if explicit_falses:
            return RowSelectionState.UNSELECTED

        return RowSelectionState.UNKNOWN

    async def _verify_share_recipient_selected(
        self,
        item: Any,
        page: Any,
        target_username: str,
        target_display: str | None = None,
    ) -> bool:
        """Verify that the intended recipient—and ONLY the intended recipient—is selected."""
        norm_target = normalize_username(target_username)
        if not norm_target:
            return False

        page_loc = getattr(page, "locator", None)
        if not callable(page_loc):
            return False

        chip_verified = False

        # 1. Check selected recipient chip/tag in the modal
        try:
            chips = page.locator(
                ".TUXModal [class*='Chip'], .TUXModal [class*='Tag'], "
                ".TUXModal [class*='SelectedUser'], .TUXModal [data-e2e*='selected-user']"
            )
            chip_count = await chips.count() if hasattr(chips, "count") and callable(chips.count) else 0
            if chip_count > 0:
                if chip_count != 1:
                    logger.warning(
                        "Selected recipient chips count is %d (expected 1); rejecting",
                        chip_count,
                    )
                    return False

                chip = chips.nth(0) if hasattr(chips, "nth") and callable(chips.nth) else chips.first
                chip_user = await self._extract_chip_username(chip)
                if not chip_user:
                    logger.warning("Could not extract exact username from selected recipient chip; rejecting")
                    return False

                if chip_user != norm_target:
                    logger.warning(
                        "Selected recipient chip @%s does not match expected @%s; rejecting",
                        chip_user,
                        norm_target,
                    )
                    return False

                chip_verified = True
        except Exception as exc:
            logger.warning("Exception while inspecting modal chips: %s; failing closed", exc)
            return False

        # 2. Inspect recipient rows in the share modal (cross-checking rows even when a chip is found)
        try:
            containers = page.locator(".TUXModal [class*='DivUserItemContainer'], .TUXModal [data-e2e*='share-user-item']")
            count = await containers.count() if hasattr(containers, "count") and callable(containers.count) else 0
            if count == 0:
                avatar_containers = page.locator(".TUXModal [data-e2e^='share-']")
                count = await avatar_containers.count() if hasattr(avatar_containers, "count") and callable(avatar_containers.count) else 0
                if count > 0:
                    containers = avatar_containers

            if count == 0:
                if chip_verified:
                    # Genuinely exposes chips but no enumerable rows; accept verified chip
                    return True
                logger.warning("Could not reliably enumerate recipient rows in share modal; failing closed")
                return False

            selected_rows: list[tuple[Any, set[str]]] = []
            for i in range(count):
                row_item = containers.nth(i)
                state = await self._is_row_element_selected(row_item)
                if state == RowSelectionState.UNKNOWN:
                    logger.warning(
                        "Share modal row %d has unknown selection state (not reliably determined); failing closed",
                        i,
                    )
                    return False
                if state == RowSelectionState.SELECTED:
                    row_usernames = await self._extract_share_row_usernames(row_item)
                    selected_rows.append((row_item, row_usernames))
                elif state == RowSelectionState.UNSELECTED:
                    continue

            # Require exactly one selected recipient row
            if len(selected_rows) != 1:
                logger.warning(
                    "Share modal has %d selected recipient rows (expected exactly 1); failing closed",
                    len(selected_rows),
                )
                return False

            _, row_usernames = selected_rows[0]
            # Require that row's authoritative username to exactly match the intended username
            if norm_target not in row_usernames:
                logger.warning(
                    "Selected row authoritative username %s does not match intended @%s",
                    row_usernames,
                    norm_target,
                )
                return False

            return True
        except Exception as exc:
            logger.warning("Exception while verifying recipient row selection states: %s; failing closed", exc)
            return False

    async def _share_video_card(
        self,
        page: Any,
        recipient: Mapping[str, Any],
        canonical_url: str,
        video_id: str,
        *,
        on_submitted: Callable[[], None] | None = None,
    ) -> None:
        """Open the video page, use TikTok's Share to friends dialog, and click Send.

        Raises:
            DeliveryConfirmedNotSentError: If the share dialog, search, or
            selection fails before submission.
            DeliveryUnconfirmedError: If submission occurred but verification is
            ambiguous.
        """
        username = str(recipient.get("username") or "").lstrip("@")
        configured_display_name = str(recipient.get("name") or "").strip()
        target_norm = normalize_username(username)
        if not target_norm:
            raise DeliveryConfirmedNotSentError("Cannot share video card without a verified recipient username.")

        logger.info(
            "native_video_card: Navigating to %s to share with %s (@%s)",
            canonical_url,
            configured_display_name or "recipient",
            username,
        )

        # 1. Navigate to video
        goto = getattr(page, "goto", None)
        if callable(goto):
            try:
                await page.goto(
                    canonical_url,
                    wait_until="commit",
                    timeout=self.navigation_timeout_ms,
                )
            except Exception as exc:
                raise DeliveryConfirmedNotSentError(
                    f"Failed to navigate to video page {canonical_url}: {exc}"
                ) from exc

        await self._handle_tiktok_interruptions(page)

        # 2. Click Share icon button on video
        locator = getattr(page, "locator", None)
        if not callable(locator):
            return

        try:
            share_btn = page.locator("[data-e2e='share-icon'], [aria-label*='Share' i]").first
            await share_btn.wait_for(state="visible", timeout=self.action_timeout_ms)
            await share_btn.click(timeout=self.action_timeout_ms)
        except Exception as exc:
            raise DeliveryConfirmedNotSentError(
                f"Failed to click share button on video page: {exc}"
            ) from exc

        await self._handle_tiktok_interruptions(page)

        # 3. Switch to 'Send to friends' search view if search navbar button exists
        try:
            search_nav_btn = page.locator(
                ".TUXModalNavBar-leading button, .TUXModal button:has(svg.flip-rtl), .TUXModal .TUXNavBarIconButton"
            ).first
            if await search_nav_btn.count() > 0 and await search_nav_btn.is_visible():
                await search_nav_btn.click(timeout=self.action_timeout_ms)
        except Exception as exc:
            logger.debug("Note while opening Send to friends search view: %s", exc)

        # 4. Search for recipient
        try:
            search_input = page.locator(
                ".TUXModal input.TUXTextInputCore-input, .TUXModal input[placeholder*='Search' i]"
            ).first
            if await search_input.count() > 0:
                await search_input.wait_for(state="visible", timeout=self.action_timeout_ms)
                if hasattr(search_input, "fill"):
                    await search_input.fill("")
                if hasattr(search_input, "type"):
                    await search_input.type(username or configured_display_name)
                await asyncio.sleep(0.5)
        except Exception as exc:
            logger.warning(
                "Could not type into friends search box: %s; checking visible list",
                exc,
            )

        # 5. Select recipient row strictly by verifiable username
        try:
            containers = page.locator(".TUXModal [class*='DivUserItemContainer'], .TUXModal [data-e2e*='share-user-item']")
            count = await containers.count()

            # If no containers found via user item selectors, try initial horizontal avatar buttons
            if count == 0:
                avatar_containers = page.locator(".TUXModal [data-e2e^='share-']")
                count = await avatar_containers.count()
                if count > 0:
                    containers = avatar_containers

            matching_items: list[tuple[Any, set[str]]] = []

            for i in range(count):
                item = containers.nth(i)
                row_usernames = await self._extract_share_row_usernames(item)
                if target_norm in row_usernames:
                    matching_items.append((item, row_usernames))

            if len(matching_items) == 0:
                raise DeliveryConfirmedNotSentError(
                    f"Recipient @{username} not found with exact verifiable username in TikTok share friends list."
                )
            if len(matching_items) > 1:
                raise DeliveryConfirmedNotSentError(
                    f"Ambiguous recipient @{username}: found {len(matching_items)} exact matches in share friends list."
                )

            selected_item, _ = matching_items[0]
            await selected_item.click(timeout=self.action_timeout_ms)

            # 5b. Verify selection state before proceeding to Send
            verified_selection = await self._verify_share_recipient_selected(
                selected_item,
                page,
                target_username=target_norm,
                target_display=configured_display_name or None,
            )
            if not verified_selection:
                raise DeliveryConfirmedNotSentError(
                    f"Could not verify selection state for recipient @{username} in share dialog."
                )
        except DeliveryConfirmedNotSentError:
            raise
        except Exception as exc:
            raise DeliveryConfirmedNotSentError(
                f"Failed to select recipient in share dialog: {exc}"
            ) from exc

        # 6. Click Send button
        try:
            send_btn = page.locator(
                ".TUXModal button.TUXButton--primary:has-text('Send'), .TUXModal [class*='StyledSendButton']"
            ).first
            await send_btn.wait_for(state="visible", timeout=self.action_timeout_ms)
        except Exception as exc:
            raise DeliveryConfirmedNotSentError(
                f"Send button not found or not visible in share dialog: {exc}"
            ) from exc

        # 7. Click Send button - submission boundary
        try:
            try:
                await send_btn.click(timeout=self.action_timeout_ms)
            finally:
                if on_submitted is not None:
                    try:
                        on_submitted()
                    except Exception:
                        pass
        except asyncio.CancelledError:
            logger.warning("Task cancelled during Send button click in native share dialog for @%s", username)
            raise
        except Exception as exc:
            raise DeliveryUnconfirmedError(
                f"Exception during Send button click in native share dialog: {exc}"
            ) from exc

        # Any operation after Send click has completed/dispatched MUST raise DeliveryUnconfirmedError
        # on non-cancellation errors so that it never retries or invokes fallback!
        try:
            logger.info(
                "native_video_card: Send button clicked for %s (@%s)",
                configured_display_name or "recipient",
                username,
            )
            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise DeliveryUnconfirmedError(
                f"Exception after Send button click in native share dialog: {exc}"
            ) from exc

    async def _send_message(self, page: Any, message: str, username: str | None = None) -> None:
        """Type and submit a message, then wait for observable evidence of delivery."""

        # 1. Verify chat header recipient matches if known
        if username:
            try:
                chat_username = await self.recipient_service.extract_username_from_chat(page)
                if chat_username and normalize_username(chat_username) != normalize_username(username):
                    raise DeliveryConfirmedNotSentError(
                        f"Opened chat header @{chat_username} does not match expected @{username}"
                    )
            except (asyncio.CancelledError, DeliveryConfirmedNotSentError):
                raise
            except Exception as exc:
                logger.warning("Could not verify chat header username before sending: %s", exc)

        # 2. Count existing chat items before typing
        initial_item_count = await self._count_chat_items(page)

        # 3. Enter message into composer
        try:
            input_locator = await self.recipient_service.wait_for_message_input(page)
            await input_locator.click()
            await page.keyboard.type(message)
        except (asyncio.CancelledError, DeliveryConfirmedNotSentError):
            raise
        except Exception as exc:
            raise DeliveryConfirmedNotSentError(f"Failed to enter message into composer: {exc}") from exc

        # 4. Submit message via Enter
        try:
            await page.keyboard.press("Enter")
        except asyncio.CancelledError:
            logger.warning("Task cancelled during Enter keypress for @%s; delivery state is unconfirmed", username)
            raise
        except Exception as exc:
            raise DeliveryUnconfirmedError(f"Exception during Enter keypress: {exc}") from exc

        # From this point forward, Enter has been submitted.
        # Any failure to confirm delivery is ambiguous and MUST NOT be retried automatically.
        try:
            await self._wait_for_delivery_evidence(
                page,
                message,
                initial_item_count=initial_item_count,
                expected_username=username,
            )
        except (asyncio.CancelledError, DeliveryUnconfirmedError):
            raise
        except Exception as exc:
            raise DeliveryUnconfirmedError(
                f"Delivery verification failed after Enter keypress: {exc}"
            ) from exc

    async def _count_chat_items(self, page: Any) -> int:
        try:
            locator = getattr(page, "locator", None)
            if callable(locator):
                return await page.locator("[data-e2e='dm-new-chat-item']").count()
        except Exception:
            pass
        return 0

    async def _wait_for_delivery_evidence(
        self,
        page: Any,
        message: str,
        *,
        initial_item_count: int,
        expected_username: str | None = None,
        expected_video_id: str | None = None,
    ) -> None:
        """Poll for observable proof that the outgoing message appeared in the chat."""

        video_id = expected_video_id or extract_video_identifier(message)
        timeout_seconds = self.action_timeout_ms / 1000
        start_time = time.monotonic()
        deadline = start_time + timeout_seconds

        logger.info(
            "Waiting for delivery verification: video_id=%s timeout=%.1fs",
            video_id,
            timeout_seconds,
        )

        last_probe_details: dict[str, Any] = {}
        while time.monotonic() < deadline:
            context = getattr(page, "context", None)
            if callable(context):
                context = context()
            if (context is not None and is_context_closed(context)) or getattr(page, "is_closed", lambda: False)():
                raise DeliveryUnconfirmedError(
                    "Browser context or page was closed while waiting for delivery confirmation after submission."
                )

            try:
                await self._handle_tiktok_interruptions(page)

                probe = await self._probe_outgoing_message(
                    page,
                    expected_video_id=video_id,
                    expected_url=message.split("?")[0].rstrip("/"),
                    initial_count=initial_item_count,
                    expected_username=expected_username,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if (context is not None and is_context_closed(context)) or getattr(page, "is_closed", lambda: False)():
                    raise DeliveryUnconfirmedError(
                        f"Browser closed during delivery verification: {exc}"
                    ) from exc
                logger.debug("Exception during delivery probe: %s", exc)
                probe = {}

            last_probe_details = probe
            if probe.get("verified"):
                elapsed = time.monotonic() - start_time
                matched_by = probe.get("matched_by", "matched")
                logger.info(
                    "delivery_verified: Observed %s in %.2fs for %s (matched_by=%s)",
                    matched_by,
                    elapsed,
                    expected_username or "recipient",
                    matched_by,
                )
                logger.info(
                    "Timing: Delivery verification took %.2fs for %s",
                    elapsed,
                    expected_username or "recipient",
                )
                return

            remaining = deadline - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(min(0.5, remaining))

        elapsed = time.monotonic() - start_time
        logger.warning(
            "delivery_unconfirmed: Verification timed out after %.2fs for %s: "
            "initial_items=%d current_items=%s composer_cleared=%s",
            elapsed,
            expected_username or "recipient",
            initial_item_count,
            last_probe_details.get("current_count", "<unknown>"),
            last_probe_details.get("composer_cleared", "<unknown>"),
        )
        raise DeliveryUnconfirmedError(
            f"Submission occurred, but outgoing message was not observed within {timeout_seconds:.1f}s."
        )

    async def _probe_outgoing_message(
        self,
        page: Any,
        *,
        expected_video_id: str,
        expected_url: str,
        initial_count: int,
        expected_username: str | None = None,
    ) -> dict[str, Any]:
        evaluate = getattr(page, "evaluate", None)
        if callable(evaluate):
            try:
                res = await evaluate(
                    DELIVERY_VERIFICATION_SCRIPT,
                    {
                        "expectedVideoId": expected_video_id,
                        "expectedUrl": expected_url,
                        "initialCount": initial_count,
                        "expectedUsername": expected_username or "",
                    },
                )
                if isinstance(res, dict):
                    return res
            except Exception:
                pass

        return await self._probe_outgoing_message_via_locators(
            page,
            expected_video_id=expected_video_id,
            expected_url=expected_url,
            initial_count=initial_count,
            expected_username=expected_username,
        )

    async def _probe_outgoing_message_via_locators(
        self,
        page: Any,
        *,
        expected_video_id: str,
        expected_url: str,
        initial_count: int,
        expected_username: str | None = None,
    ) -> dict[str, Any]:
        locator = getattr(page, "locator", None)
        if not callable(locator):
            return {"verified": False, "current_count": 0, "composer_cleared": False}

        try:
            items = page.locator("[data-e2e='dm-new-chat-item']")
            count = await items.count()
            if count > initial_count:
                last_item = items.nth(count - 1)
                is_outgoing = True
                if expected_username:
                    norm_user = expected_username.lstrip("@").lower()
                    peer_link = last_item.locator(f"a[href*='/@{norm_user}']")
                    if await peer_link.count() > 0:
                        is_outgoing = False

                if is_outgoing:
                    shared = last_item.locator("[data-e2e='dm-new-shared-video']")
                    if await shared.count() > 0:
                        return {"verified": True, "current_count": count, "matched_by": "native_video_card"}
                    text = await last_item.inner_text()
                    if (expected_video_id and expected_video_id in text) or (expected_url and expected_url in text):
                        return {"verified": True, "current_count": count, "matched_by": "text"}
            return {"verified": False, "current_count": count, "composer_cleared": False}
        except Exception:
            return {"verified": False, "current_count": 0, "composer_cleared": False}

    async def _handle_tiktok_interruptions(self, page: Any) -> None:
        await handle_screen_time_popup(
            page,
            action_timeout_ms=self.action_timeout_ms,
            probe_timeout_ms=self.probe_timeout_ms,
        )
        await handle_sleep_hours_popup(
            page,
            action_timeout_ms=self.action_timeout_ms,
            probe_timeout_ms=self.probe_timeout_ms,
        )
        await wait_for_captcha_if_present(
            page,
            self.notifier,
            probe_timeout_ms=self.probe_timeout_ms,
        )
        await handle_screen_time_popup(
            page,
            action_timeout_ms=self.action_timeout_ms,
            probe_timeout_ms=self.probe_timeout_ms,
        )
        await handle_sleep_hours_popup(
            page,
            action_timeout_ms=self.action_timeout_ms,
            probe_timeout_ms=self.probe_timeout_ms,
        )

    def _resume_skip_result(
        self,
        recipient: Mapping[str, Any],
        daily_state: DailyState,
        *,
        force: bool,
    ) -> RecipientResult | None:
        username_value = recipient.get("username")
        username = str(username_value) if username_value else None
        if force or not username or not daily_state.is_completed(username):
            return None

        completion = daily_state.recipients[normalize_username(username)]
        recipient_name = str(recipient.get("name") or "recipient")
        return RecipientResult(
            recipient_name=recipient_name,
            username=username,
            selected_video=completion.video,
            status=RecipientStatus.SKIPPED,
            delivery_status=DeliveryStatus.CONFIRMED_NOT_SENT,
            attempts=0,
            error_message=(
                f"Already sent successfully on {daily_state.date}; "
                "use force=True to send again."
            ),
            elapsed_seconds=0.0,
        )

    def _persist_success(self, daily_state: DailyState, result: RecipientResult) -> None:
        """Persist success before advancing to the next recipient."""

        assert result.username is not None
        assert result.selected_video is not None
        try:
            self.state_store.record_success(
                daily_state,
                result.username,
                result.selected_video,
                self._logical_now(),
            )
        except Exception as exc:
            logger.exception(
                "Could not persist successful delivery for %s (@%s)",
                result.recipient_name,
                result.username,
            )
            raise RuntimeError(
                "Successful delivery could not be persisted; stopping to avoid unsafe resume behavior."
            ) from exc

    def _report_recipient_result(self, result: RecipientResult, summary: RunSummary) -> None:
        """Notify an optional UI adapter after the result has been persisted."""

        if self.on_recipient_result is None:
            return
        try:
            self.on_recipient_result(result, summary)
        except Exception:
            logger.exception("Recipient result progress callback failed")

    def _load_daily_state(self) -> DailyState:
        current = self._logical_now()
        return self.state_store.load(current.date().isoformat(), self.schedule_timezone)

    def _new_summary(self) -> RunSummary:
        return RunSummary(started_at=self._now_utc())

    def _finish_run(self, summary: RunSummary) -> RunSummary:
        summary.finish(self._now_utc())
        try:
            self.history_store.append(summary)
        except Exception:
            logger.exception("Could not persist run history")
            raise
        return summary

    def _logical_now(self) -> datetime:
        return logical_now(self.schedule_timezone, self._now_utc())

    def _now_utc(self) -> datetime:
        current = self.now_provider()
        if current.tzinfo is None:
            return current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc)

    def _recipient_attempts_allowed(self) -> int:
        retries = self.tiktok_config.get("recipient_retries", 1)
        try:
            return max(0, int(retries)) + 1
        except (TypeError, ValueError):
            logger.warning("Invalid recipient_retries value %r; using one retry.", retries)
            return 2

    @staticmethod
    def _run_lock_stale_seconds(config: Mapping[str, Any]) -> float:
        raw_runtime = config.get("runtime", {})
        runtime_config: Mapping[str, Any] = (
            raw_runtime if isinstance(raw_runtime, Mapping) else {}
        )
        value = runtime_config.get("run_lock_stale_seconds", DEFAULT_STALE_LOCK_SECONDS)
        try:
            return max(1.0, float(value))
        except (TypeError, ValueError):
            logger.warning(
                "Invalid runtime.run_lock_stale_seconds value %r; using default.",
                value,
            )
            return float(DEFAULT_STALE_LOCK_SECONDS)

    @staticmethod
    def _error_message(error: Exception) -> str:
        return str(error) or error.__class__.__name__

    async def _sleep_between_messages(self) -> None:
        delay_range = self.tiktok_config.get("message_delay_seconds", [8, 18])
        if not isinstance(delay_range, list) or len(delay_range) != 2:
            delay_range = [8, 18]
        low, high = sorted(int(value) for value in delay_range)
        duration = random.randint(low, high)
        t0 = time.monotonic()
        await asyncio.sleep(duration)
        elapsed = time.monotonic() - t0
        logger.info(
            "Timing: Inter-message delay slept for %.2fs (target %ds, range %s)",
            elapsed,
            duration,
            delay_range,
        )
