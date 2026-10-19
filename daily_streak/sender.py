"""Resilient, reusable TikTok batch sender core."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import logging
import random
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
    launch_browser_session,
)
from daily_streak.history import RunHistoryStore
from daily_streak.lock import DEFAULT_STALE_LOCK_SECONDS, RunLock
from daily_streak.models import DeliveryStatus, RecipientResult, RecipientStatus, RunSummary
from daily_streak.navigation import navigate_to_messages
from daily_streak.paths import get_data_dir
from daily_streak.recipients import RecipientService
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

    async def send_daily_links(self, *, force: bool = False) -> RunSummary:
        """Complete session setup, then send every configured recipient once."""

        self.run_lock.acquire()
        try:
            summary = self._new_summary()
            daily_state = self._load_daily_state()
            if not self.recipients:
                logger.warning("No recipients configured.")
                return self._finish_run(summary)

            cookie_file = self.config.get("cookie_file", "cookies.json")
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

        try:
            for recipient in self.recipients:
                result = self._resume_skip_result(recipient, daily_state, force=force)
                if result is None:
                    # If active DM page was closed, reset it so next recipient recreates cleanly
                    if self._active_dm_page is not None and getattr(self._active_dm_page, "is_closed", lambda: False)():
                        logger.info("Retained DM page was closed; recreating page for next recipient")
                        self._active_dm_page = None

                    # If no active DM page, try to obtain or create one
                    if self._active_dm_page is None and callable(getattr(context, "new_page", None)):
                        pages = getattr(context, "pages", ())
                        if pages and not getattr(pages[0], "is_closed", lambda: False)():
                            self._active_dm_page = pages[0]
                        else:
                            t_init = time.monotonic()
                            self._active_dm_page = await context.new_page()
                            created_dm_page_internally = True
                            logger.info("Timing: DM page initialization took %.2fs", time.monotonic() - t_init)

                    result = await self._send_recipient_with_retries(context, recipient)
                summary.add_result(result)
                if result.status is RecipientStatus.SUCCESS:
                    self._persist_success(daily_state, result)
                self._report_recipient_result(result, summary)
                await self._sleep_between_messages()
        finally:
            if created_dm_page_internally and self._active_dm_page is not None:
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
            try:
                if self._active_dm_page is not None and getattr(self._active_dm_page, "is_closed", lambda: False)():
                    self._active_dm_page = None

                await self._send_recipient_once(context, recipient, selected_video)
            except DeliveryUnconfirmedError as exc:
                # Ambiguous delivery: Enter or submit was pressed, but verification timed out.
                # Do NOT automatically retry during the same operation to avoid duplicate sends!
                logger.warning(
                    "delivery_unconfirmed: Delivery unconfirmed on attempt %d for %s (@%s): %s. Will not retry.",
                    attempt,
                    recipient_name,
                    username,
                    exc,
                )
                if self._active_dm_page is not None:
                    with suppress(Exception):
                        if hasattr(self._active_dm_page, "close") and not getattr(self._active_dm_page, "is_closed", lambda: False)():
                            await self._active_dm_page.close()
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
        if not reused_page:
            t0 = time.monotonic()
            dm_page = await context.new_page()
            logger.info("Timing: DM page initialization took %.2fs", time.monotonic() - t0)

        video_page = None
        try:
            # 1. Open recipient's DM conversation first to verify recipient and count initial messages
            t_select = time.monotonic()
            select_kwargs: dict[str, Any] = {
                "configured_display_name": configured_display_name or None,
            }
            if reused_page:
                select_kwargs["reuse_existing_page"] = True

            try:
                dm_page, _ = await self.recipient_service.select_recipient_for_username_with_page(
                    dm_page,
                    username,
                    self.notifier,
                    **select_kwargs,
                )
            except TypeError:
                select_kwargs.pop("reuse_existing_page", None)
                dm_page, _ = await self.recipient_service.select_recipient_for_username_with_page(
                    dm_page,
                    username,
                    self.notifier,
                    **select_kwargs,
                )

            if reused_page:
                self._active_dm_page = dm_page

            await self._handle_tiktok_interruptions(dm_page)

            # Verify chat header matches expected recipient
            extract_fn = getattr(self.recipient_service, "extract_username_from_chat", None)
            if callable(extract_fn):
                try:
                    chat_username = await extract_fn(dm_page)
                    if chat_username and normalize_username(chat_username) != normalize_username(username):
                        raise DeliveryConfirmedNotSentError(
                            f"Opened chat header @{chat_username} does not match expected @{username}"
                        )
                except (asyncio.CancelledError, DeliveryConfirmedNotSentError):
                    raise
                except Exception as exc:
                    logger.warning("Could not verify chat header username before sending: %s", exc)

            initial_item_count = await self._count_chat_items(dm_page)
            logger.info(
                "Timing: Conversation selection and composer readiness took %.2fs for %s",
                time.monotonic() - t_select,
                username,
            )

            # 2. Open video page in separate page and execute native share
            t_video = time.monotonic()
            video_page = await context.new_page()
            try:
                await self._share_video_card(
                    video_page,
                    recipient=recipient,
                    canonical_url=canonical_url,
                    video_id=video_id,
                )
            finally:
                if hasattr(video_page, "close"):
                    t_cleanup = time.monotonic()
                    await video_page.close()
                    logger.info("Timing: Post-send cleanup (closed video page) took %.2fs", time.monotonic() - t_cleanup)
            logger.info("Timing: Video share operation took %.2fs for %s", time.monotonic() - t_video, username)

            # 3. Verify delivery in the recipient's DM
            t_delivery = time.monotonic()
            await self._wait_for_delivery_evidence(
                dm_page,
                canonical_url,
                initial_item_count=initial_item_count,
                expected_username=username,
                expected_video_id=video_id,
            )
            logger.info("Timing: Delivery verification took %.2fs for %s", time.monotonic() - t_delivery, username)
            await self._handle_tiktok_interruptions(dm_page)
        finally:
            if not reused_page and hasattr(dm_page, "close"):
                t_close = time.monotonic()
                await dm_page.close()
                logger.info("Timing: Post-send cleanup (closed DM page) took %.2fs", time.monotonic() - t_close)

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
            t0 = time.monotonic()
            dm_page = await context.new_page()
            logger.info("Timing: DM page initialization took %.2fs", time.monotonic() - t0)

        page = dm_page
        try:
            t_select = time.monotonic()
            select_kwargs: dict[str, Any] = {
                "configured_display_name": configured_display_name or None,
            }
            if reused_page:
                select_kwargs["reuse_existing_page"] = True

            try:
                page, _ = await self.recipient_service.select_recipient_for_username_with_page(
                    page,
                    username,
                    self.notifier,
                    **select_kwargs,
                )
            except TypeError:
                select_kwargs.pop("reuse_existing_page", None)
                page, _ = await self.recipient_service.select_recipient_for_username_with_page(
                    page,
                    username,
                    self.notifier,
                    **select_kwargs,
                )

            if reused_page:
                self._active_dm_page = page

            await self._handle_tiktok_interruptions(page)
            logger.info(
                "Timing: Conversation selection and composer readiness took %.2fs for %s",
                time.monotonic() - t_select,
                username,
            )

            t_send = time.monotonic()
            await self._send_message(page, selected_video, username=username)
            logger.info("Timing: Video send operation took %.2fs for %s", time.monotonic() - t_send, username)
            await self._handle_tiktok_interruptions(page)
        finally:
            if not reused_page and hasattr(page, "close"):
                t_close = time.monotonic()
                await page.close()
                logger.info("Timing: Post-send cleanup (closed DM page) took %.2fs", time.monotonic() - t_close)

    async def _share_video_card(
        self,
        page: Any,
        recipient: Mapping[str, Any],
        canonical_url: str,
        video_id: str,
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

        # 5. Select recipient row
        try:
            containers = page.locator(".TUXModal [class*='DivUserItemContainer']")
            count = await containers.count()
            selected = False
            target_norm = username.lower()
            target_display = configured_display_name.lower() if configured_display_name else None

            for i in range(count):
                item = containers.nth(i)
                text = (await item.inner_text()).lower()
                if (target_norm and target_norm in text) or (target_display and target_display in text):
                    await item.click(timeout=self.action_timeout_ms)
                    selected = True
                    break

            # Fallback to horizontal share avatars in initial share-group if not found in list
            if not selected:
                avatar_btn = page.locator(f".TUXModal [data-e2e='share-{username}']").first
                if await avatar_btn.count() > 0 and await avatar_btn.is_visible():
                    await avatar_btn.click(timeout=self.action_timeout_ms)
                    selected = True

            if not selected:
                raise DeliveryConfirmedNotSentError(
                    f"Recipient @{username} ({configured_display_name}) not found in TikTok share friends list."
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
            await send_btn.click(timeout=self.action_timeout_ms)
        except Exception as exc:
            raise DeliveryConfirmedNotSentError(
                f"Failed to submit native share Send button: {exc}"
            ) from exc

        logger.info(
            "native_video_card: Send button clicked for %s (@%s)",
            configured_display_name or "recipient",
            username,
        )
        await asyncio.sleep(1.0)

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
            raise
        except Exception as exc:
            raise DeliveryConfirmedNotSentError(f"Failed to submit message with Enter: {exc}") from exc

        # From this point forward, Enter has been submitted.
        # Any failure to confirm delivery is ambiguous and MUST NOT be retried automatically.
        await self._wait_for_delivery_evidence(
            page,
            message,
            initial_item_count=initial_item_count,
            expected_username=username,
        )

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
            await self._handle_tiktok_interruptions(page)

            probe = await self._probe_outgoing_message(
                page,
                expected_video_id=video_id,
                expected_url=message.split("?")[0].rstrip("/"),
                initial_count=initial_item_count,
                expected_username=expected_username,
            )
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
