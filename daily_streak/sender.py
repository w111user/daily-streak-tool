"""Resilient, reusable TikTok batch sender core."""

from __future__ import annotations

import asyncio
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
from daily_streak.videos import VideoPool


logger = logging.getLogger(__name__)


class DeliveryVerificationError(RuntimeError):
    """Base class for delivery verification failures."""


class DeliveryUnconfirmedError(DeliveryVerificationError):
    """Enter was pressed, but delivery could not be confirmed within the timeout.

    This outcome is ambiguous; callers must not automatically retry.
    """


class DeliveryConfirmedNotSentError(DeliveryVerificationError):
    """Submission did not occur; safe to retry if attempts remain."""


def extract_video_identifier(video_url: str) -> str:
    """Extract a stable video ID or shortcode from a TikTok video URL."""

    cleaned = video_url.strip().split("?")[0].rstrip("/")
    if "/video/" in cleaned:
        return cleaned.split("/video/")[-1].split("/")[0]
    if "vt.tiktok.com/" in cleaned:
        return cleaned.split("vt.tiktok.com/")[-1].split("/")[0]
    if "/v/" in cleaned:
        part = cleaned.split("/v/")[-1].split("/")[0]
        return part.replace(".html", "")
    return cleaned


DELIVERY_VERIFICATION_SCRIPT = """
(args) => {
    const { expectedVideoId, expectedUrl, initialCount, expectedUsername } = args;
    const peerName = (expectedUsername || "").replace(/^@/, "").toLowerCase();
    const items = Array.from(document.querySelectorAll("[data-e2e='dm-new-chat-item']"));
    const currentCount = items.length;

    const startIdx = Math.max(0, Math.min(initialCount, currentCount - 2));
    const candidates = items.slice(startIdx);

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

        const matchesUrl = Boolean(expectedUrl && (allText.includes(expectedUrl) || links.some(l => l.includes(expectedUrl))));
        const matchesId = Boolean(expectedVideoId && (allText.includes(expectedVideoId) || links.some(l => l.includes(expectedVideoId))));
        const isNewSharedVideo = Boolean(currentCount > initialCount && sharedVideo);

        if (matchesUrl || matchesId || isNewSharedVideo) {
            return {
                verified: true,
                current_count: currentCount,
                matched_by: matchesUrl ? "url" : (matchesId ? "video_id" : "shared_video_card")
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
    ) -> None:
        self.config = config
        self.notifier = notifier
        self.video_pool = video_pool
        raw_tiktok_config = config.get("tiktok", {})
        self.tiktok_config: Mapping[str, Any] = (
            raw_tiktok_config if isinstance(raw_tiktok_config, Mapping) else {}
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
                    return await self.send_recipients(
                        session.context,
                        summary,
                        daily_state=daily_state,
                        force=force,
                    )
                finally:
                    await session.close()
        finally:
            self.run_lock.release()

    async def send_recipients(
        self,
        context: Any,
        summary: RunSummary | None = None,
        *,
        force: bool = False,
        daily_state: DailyState | None = None,
    ) -> RunSummary:
        """Send recipients sequentially with resume-safe state and history."""

        summary = summary if summary is not None else self._new_summary()
        daily_state = daily_state or self._load_daily_state()
        for recipient in self.recipients:
            result = self._resume_skip_result(recipient, daily_state, force=force)
            if result is None:
                result = await self._send_recipient_with_retries(context, recipient)
            summary.add_result(result)
            if result.status is RecipientStatus.SUCCESS:
                self._persist_success(daily_state, result)
            self._report_recipient_result(result, summary)
            await self._sleep_between_messages()
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
                await self._send_recipient_once(context, recipient, selected_video)
            except DeliveryUnconfirmedError as exc:
                # Ambiguous delivery: Enter was submitted, but verification timed out.
                # Do NOT automatically retry during the same operation to avoid duplicate sends!
                logger.warning(
                    "Delivery unconfirmed on attempt %d for %s (@%s): %s. Will not retry.",
                    attempt,
                    recipient_name,
                    username,
                    exc,
                )
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
            else:
                logger.info(
                    "Sent and verified video link to %s (@%s) on attempt %d: %s",
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
    ) -> None:
        username = str(recipient["username"])
        configured_display_name = str(recipient.get("name") or "").strip()
        page = await context.new_page()
        try:
            page, _ = await self.recipient_service.select_recipient_for_username_with_page(
                page,
                username,
                self.notifier,
                configured_display_name=configured_display_name or None,
            )
            await self._handle_tiktok_interruptions(page)
            await self._send_message(page, selected_video, username=username)
            await self._handle_tiktok_interruptions(page)
        finally:
            await page.close()

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
    ) -> None:
        """Poll for observable proof that the outgoing message appeared in the chat."""

        video_id = extract_video_identifier(message)
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
                logger.info(
                    "Delivery verified in %.2fs for %s: outgoing message observed (%s)",
                    elapsed,
                    expected_username or "recipient",
                    probe.get("matched_by", "matched"),
                )
                return

            remaining = deadline - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(min(0.5, remaining))

        elapsed = time.monotonic() - start_time
        logger.warning(
            "Delivery verification timed out after %.2fs for %s: "
            "initial_items=%d current_items=%s composer_cleared=%s",
            elapsed,
            expected_username or "recipient",
            initial_item_count,
            last_probe_details.get("current_count", "<unknown>"),
            last_probe_details.get("composer_cleared", "<unknown>"),
        )
        raise DeliveryUnconfirmedError(
            f"Enter was submitted, but outgoing message was not observed within {timeout_seconds:.1f}s."
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
                    text = await last_item.inner_text()
                    if (expected_video_id and expected_video_id in text) or (expected_url and expected_url in text):
                        return {"verified": True, "current_count": count, "matched_by": "text"}
                    shared = last_item.locator("[data-e2e='dm-new-shared-video']")
                    if await shared.count() > 0:
                        return {"verified": True, "current_count": count, "matched_by": "shared_video"}
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
        await asyncio.sleep(random.randint(low, high))
