"""Reusable TikTok direct-message recipient operations."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import logging
import re
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from daily_streak.browser import (
    DEFAULT_ACTION_TIMEOUT_MS,
    DEFAULT_CONTENT_READY_TIMEOUT_MS,
    DEFAULT_DM_LOAD_ATTEMPTS,
    DEFAULT_LOW_MEMORY_STABILIZATION_SECONDS,
    DEFAULT_NAVIGATION_TIMEOUT_MS,
    DEFAULT_PROBE_TIMEOUT_MS,
)
from daily_streak.navigation import (
    is_tiktok_http_url,
    navigate_to_messages,
    navigate_to_profile,
)
from daily_streak.tiktok import (
    handle_screen_time_popup,
    handle_sleep_hours_popup,
    wait_for_captcha_if_present,
)


logger = logging.getLogger(__name__)

CONVERSATION_LIST_SELECTOR = "[data-e2e='dm-new-conversation-list']"
CONVERSATION_ITEM_SELECTOR = "[data-e2e='dm-new-conversation-item']"
CONVERSATION_NICKNAME_SELECTOR = "[data-e2e='dm-new-conversation-nickname']"
EMPTY_CONVERSATION_SELECTOR = (
    "[data-e2e='dm-empty-state'], "
    "[data-e2e='dm-empty'], "
    "[data-e2e='dm-empty-conversation-list'], "
    "[data-e2e='dm-empty-conversation']"
)
READINESS_BODY_PREFIX_MAX_CHARS = 500
RESOLUTION_CLICK_TIMEOUT_MS = 3_000
# Kept as a compatibility alias for code that used the earlier
# Resolve-specific internal constant.
MAX_RESOLVE_DM_LOAD_ATTEMPTS = DEFAULT_DM_LOAD_ATTEMPTS
SENSITIVE_DIAGNOSTIC_VALUE_PATTERN = re.compile(
    r"(?i)\b(sessionid|csrf(?:token)?|access[ _-]?token|authorization|cookie|password)\b"
    r"\s*[:=]\s*[^\s,;]+"
)
TRANSIENT_RENDERER_PROBE_ERROR_MARKERS = (
    "execution context was destroyed",
    "execution context is not available",
    "cannot find execution context",
    "cannot find context with specified id",
    "frame was detached",
    "navigation interrupted",
)
CHAT_USERNAME_SELECTOR = "[data-e2e='chat-uniqueid']"
MESSAGE_INPUT_SELECTOR = (
    ".public-DraftEditor-content, "
    "div[contenteditable='true'][placeholder*='Send' i], "
    "div[contenteditable='true'][placeholder*='message' i], "
    "[contenteditable='true'][aria-label*='Send' i], "
    "[contenteditable='true'][aria-label*='message' i]"
)
DOM_EXECUTION_CONTEXT_SCRIPT = "() => document.readyState"
READINESS_SNAPSHOT_SCRIPT = """
() => {
    const count = (selector) => document.querySelectorAll(selector).length;
    const bodyText = (document.body?.textContent || "").slice(0, 4096).toLowerCase();
    const hasText = (phrases) => phrases.some((phrase) => bodyText.includes(phrase));
    const hasHeading = (text) => Array.from(document.querySelectorAll("h1"))
        .some((heading) => (heading.textContent || "").includes(text));
    const hasButton = (texts) => Array.from(document.querySelectorAll("button"))
        .some((button) => texts.some((text) => (button.textContent || "").includes(text)));
    return {
        dmList: count("[data-e2e='dm-new-conversation-list']"),
        dmItems: count("[data-e2e='dm-new-conversation-item']"),
        emptyState: count(
            "[data-e2e='dm-empty-state'], [data-e2e='dm-empty'], " +
            "[data-e2e='dm-empty-conversation-list'], [data-e2e='dm-empty-conversation']"
        ),
        login: count(
            "[data-e2e='login-button'], [data-e2e='login-modal'], " +
            "[data-e2e='login-container'], a[href*='/login']"
        ),
        captcha: count(
            "iframe[src*='captcha'], iframe[src*='verify'], [class*='captcha' i], " +
            "[id*='captcha' i], [class*='verify' i], [id*='verify' i]"
        ) + Number(hasText([
            "captcha", "verify", "verification", "security check", "drag the slider", "please try again"
        ])),
        screenTime: hasHeading("Ready to close TikTok?"),
        sleepHours: hasButton(["Return for now", "Quay lại"]),
    };
}
"""


@dataclass
class _ReadinessProbeState:
    """Bounded diagnostics gathered from the renderer-friendly readiness loop."""

    successful_probes: int = 0
    failed_probes: int = 0
    consecutive_failures: int = 0
    consecutive_failure_started_at: float | None = None
    last_renderer_report_at: float | None = None
    last_snapshot: dict[str, int | bool] | None = None


@dataclass(frozen=True)
class _MatchedConversationState:
    """Safe, row-local state captured before a resolver click."""

    aria_selected: str | None
    already_selected: bool


def username_from_href(href: str | None) -> str:
    """Extract a TikTok username from a profile URL or relative profile path."""

    if not href or "/@" not in href:
        return ""
    return href.split("/@")[-1].split("?")[0].split("/")[0].strip().lstrip("@")


class TikTokLoginRequiredError(RuntimeError):
    """TikTok redirected a recipient operation to its login flow."""


class TikTokChallengeRequiredError(RuntimeError):
    """TikTok exposed a CAPTCHA or other challenge before DM content loaded."""


class TikTokDMReadinessError(RuntimeError):
    """An expected, user-actionable failure while loading TikTok DM content."""

    expected_operation_error = True


class TikTokRendererUnresponsiveError(TikTokDMReadinessError):
    """The Chromium renderer repeatedly failed the lightweight readiness probe."""


class AmbiguousConversationError(ValueError):
    """More than one loaded DM conversation has the requested display name."""

    def __init__(self, display_name: str, match_count: int) -> None:
        self.display_name = display_name
        self.match_count = match_count
        super().__init__(
            "Ambiguous DM conversations for display name "
            f"{display_name!r}: found {match_count} exact matches."
        )


class _ReadinessProbeUnavailable(RuntimeError):
    """A bounded DOM snapshot timed out before the renderer could respond."""


class RecipientService:
    """Recipient discovery, matching, and username-resolution operations."""

    def __init__(
        self,
        *,
        navigation_timeout_ms: int = DEFAULT_NAVIGATION_TIMEOUT_MS,
        action_timeout_ms: int = DEFAULT_ACTION_TIMEOUT_MS,
        content_ready_timeout_ms: int = DEFAULT_CONTENT_READY_TIMEOUT_MS,
        dm_readiness_timeout_ms: int | None = None,
        dm_load_attempts: int = DEFAULT_DM_LOAD_ATTEMPTS,
        probe_timeout_ms: int = DEFAULT_PROBE_TIMEOUT_MS,
        low_memory_mode: bool = False,
        low_memory_stabilization_seconds: float = DEFAULT_LOW_MEMORY_STABILIZATION_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.navigation_timeout_ms = max(1, int(navigation_timeout_ms))
        self.action_timeout_ms = max(1, int(action_timeout_ms))
        self.content_ready_timeout_ms = max(1, int(content_ready_timeout_ms))
        self.dm_readiness_timeout_ms = max(
            1,
            int(
                content_ready_timeout_ms
                if dm_readiness_timeout_ms is None
                else dm_readiness_timeout_ms
            ),
        )
        self.dm_load_attempts = max(1, int(dm_load_attempts))
        self.probe_timeout_ms = min(1_000, max(500, int(probe_timeout_ms)))
        self.low_memory_mode = bool(low_memory_mode)
        self.low_memory_stabilization_seconds = max(
            0.0,
            float(low_memory_stabilization_seconds),
        )
        self._monotonic = monotonic

    async def scan_dm_list(self, page: Any) -> list[dict[str, str]]:
        """Scan visible DM conversations and return display-name recipients."""

        page, readiness = await self._load_messages_with_renderer_recovery(
            page,
            caller="Scan",
        )
        conversation_list, conversation_items, is_empty = readiness
        if is_empty:
            logger.info("TikTok reports an empty DM conversation list.")
            return []

        found: dict[str, dict[str, str]] = {}
        previous_count = -1
        stable_scrolls = 0
        for _ in range(40):
            count = await conversation_items.count()
            for index in range(count):
                item = conversation_items.nth(index)
                name = await item.locator(CONVERSATION_NICKNAME_SELECTOR).inner_text(
                    timeout=self.action_timeout_ms
                )
                name = name.strip()
                found[name] = {"name": name, "username": ""}

            if len(found) == previous_count:
                stable_scrolls += 1
            else:
                stable_scrolls = 0
            if stable_scrolls >= 3:
                break

            previous_count = len(found)
            await conversation_list.evaluate(
                "(element) => element.scrollBy(0, 600)",
                timeout=self.action_timeout_ms,
            )
            await page.wait_for_timeout(500)

        return list(found.values())

    async def resolve_usernames(
        self,
        page: Any,
        recipients: Sequence[Mapping[str, Any]],
        *,
        on_progress: Callable[[int, int], None] | None = None,
        force: bool = False,
    ) -> list[dict[str, str]]:
        """Resolve selected display names from their existing DM chat headers.

        The normal path intentionally mirrors the legacy, proven workflow:
        open ``/messages``, locate an exact conversation display name, open the
        chat, and read ``data-e2e='chat-uniqueid'``.  It never needs profile
        navigation.  Each selected recipient is isolated so one missing,
        ambiguous, or otherwise unusable conversation does not prevent later
        recipients from being attempted.
        """

        pending: list[tuple[str, Mapping[str, Any]]] = []
        total = len(recipients)
        for index, recipient in enumerate(recipients, start=1):
            if on_progress:
                on_progress(index, total)
            display_name = str(recipient.get("name") or "").strip()
            if not display_name:
                logger.warning("Skipping a recipient without a display name during username resolution")
                continue

            existing_username = str(recipient.get("username") or "").strip().lstrip("@")
            if existing_username and not force:
                logger.info(
                    "Skipping %s because its username is already resolved as @%s",
                    display_name,
                    existing_username,
                )
                continue
            pending.append((display_name, recipient))

        # There is no reason to navigate a page when every selected recipient
        # already has a username (unless a caller explicitly requests force).
        if not pending:
            return []

        page, readiness = await self._load_messages_for_resolution(page)
        _, _, is_empty = readiness
        if is_empty:
            logger.info("TikTok reports an empty DM conversation list.")
            return []

        resolved: list[dict[str, str]] = []
        for pending_index, (display_name, _) in enumerate(pending):
            try:
                if readiness is None:
                    # Returning to /messages after a chat does not need the
                    # full browser lifecycle or first-load readiness loop.
                    await navigate_to_messages(
                        page,
                        navigation_timeout_ms=self.navigation_timeout_ms,
                    )
                    readiness = await self.wait_for_dm_conversation_list(page)

                await self.click_conversation_by_display_name(
                    page,
                    display_name,
                    readiness=readiness,
                    reject_ambiguous=True,
                    after_click_wait_ms=0,
                )
                username = await self.extract_username_from_chat(page)
                if not username:
                    raise ValueError("TikTok chat header did not expose a username.")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A recipient-level failure is intentionally retained as a
                # warning (including ambiguous display names) and the batch
                # proceeds with the next selected recipient.
                logger.warning(
                    "Could not resolve TikTok username for %s: %s",
                    display_name,
                    exc,
                )
            else:
                resolved.append({"name": display_name, "username": username})
                logger.info("Resolved %s to @%s", display_name, username)
            finally:
                # A successful click changes page state, and a failed lookup
                # may have scrolled the list.  Before every next target, reuse
                # the same page and obtain a fresh targeted list readiness.
                # If that return fails, leave readiness unset so the next
                # recipient gets its own bounded retry rather than aborting
                # this partial batch.
                if pending_index < len(pending) - 1:
                    readiness = None
                    try:
                        await navigate_to_messages(
                            page,
                            navigation_timeout_ms=self.navigation_timeout_ms,
                        )
                        readiness = await self.wait_for_dm_conversation_list(page)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning(
                            "Could not return to the DM conversation list after %s: %s",
                            display_name,
                            exc,
                        )

        return resolved

    async def _load_messages_for_resolution(self, page: Any) -> tuple[Any, tuple[Any, Any, bool]]:
        """Compatibility wrapper for Resolve's initial DM load recovery."""

        return await self._load_messages_with_renderer_recovery(page, caller="Resolve")

    async def _load_messages_with_renderer_recovery(
        self,
        page: Any,
        *,
        caller: str,
    ) -> tuple[Any, tuple[Any, Any, bool]]:
        """Load `/messages`, recreating one page only for a stalled renderer.

        A fresh page can recover a renderer that stopped servicing readiness
        probes after navigation.  The browser and its context (including their
        cookies and optional low-memory routing) remain intact.  No other
        readiness outcome is retried.  ``caller`` identifies the operation in
        bounded diagnostics without changing its selectors or readiness flow.
        """

        current_page = page
        for attempt in range(1, self.dm_load_attempts + 1):
            logger.info(
                "%s DM load attempt %d/%d",
                caller,
                attempt,
                self.dm_load_attempts,
            )
            await navigate_to_messages(
                current_page,
                navigation_timeout_ms=self.navigation_timeout_ms,
            )
            try:
                logger.info(
                    "DM readiness budget: %.1fs",
                    self.dm_readiness_timeout_ms / 1000,
                )
                readiness = await self.wait_for_dm_readiness(current_page)
            except TikTokRendererUnresponsiveError:
                if attempt >= self.dm_load_attempts:
                    raise
                logger.warning("%s renderer unresponsive; recreating page", caller)
                context = getattr(current_page, "context", None)
                if callable(context):
                    context = context()
                if context is None or not callable(getattr(context, "new_page", None)):
                    raise RuntimeError(
                        "Cannot recreate the %s page because its browser context is unavailable."
                        % caller
                    )
                await current_page.close()
                current_page = await context.new_page()
                continue
            return current_page, readiness

        raise AssertionError("TikTok DM load attempts unexpectedly exhausted.")

    async def wait_for_dm_conversation_list(self, page: Any) -> tuple[Any, Any, bool]:
        """Wait only for the DM list after returning from an opened chat.

        The first visit to ``/messages`` uses :meth:`wait_for_dm_readiness`,
        which handles login, CAPTCHA, empty state, and the low-memory renderer
        stages.  A return from a chat is a narrower condition: wait for the
        already-known conversation-list selector with the existing bounded
        content-ready timeout instead of restarting that expensive lifecycle.
        """

        self._raise_for_login(page)
        conversation_list = page.locator(CONVERSATION_LIST_SELECTOR).first
        try:
            await conversation_list.wait_for(
                state="visible",
                timeout=self.content_ready_timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            self._raise_for_login(page)
            raise TikTokDMReadinessError(
                "Timed out waiting for the TikTok DM conversation list after returning from a chat."
            ) from exc
        return conversation_list, page.locator(CONVERSATION_ITEM_SELECTOR), False

    async def click_conversation_by_display_name(
        self,
        page: Any,
        display_name: str,
        *,
        max_scrolls: int = 40,
        scroll_distance: int = 600,
        after_click_wait_ms: int = 500,
        readiness: tuple[Any, Any, bool] | None = None,
        reject_ambiguous: bool = False,
    ) -> None:
        """Find and open a conversation by its current display name.

        Existing sender calls preserve the original first-exact-match behavior.
        Username resolution passes ``reject_ambiguous=True`` so duplicate
        display names become an explicit, safe failure rather than selecting an
        arbitrary chat.
        """

        if readiness is None:
            readiness = await self.wait_for_dm_readiness(page)
        conversation_list, conversation_items, is_empty = readiness
        if is_empty:
            raise ValueError("TikTok reports an empty DM conversation list.")

        normalized_display_name = display_name.casefold()
        if reject_ambiguous:
            await self._click_unique_conversation_by_display_name(
                page,
                conversation_list,
                conversation_items,
                display_name,
                normalized_display_name,
                max_scrolls=max_scrolls,
                scroll_distance=scroll_distance,
                after_click_wait_ms=after_click_wait_ms,
            )
            return

        for _ in range(max_scrolls):
            for index in range(await conversation_items.count()):
                item = conversation_items.nth(index)
                nickname = item.locator(CONVERSATION_NICKNAME_SELECTOR)
                nickname_text = (
                    await nickname.inner_text(timeout=self.action_timeout_ms)
                ).strip()
                if nickname_text.casefold() == normalized_display_name:
                    await item.click(timeout=self.action_timeout_ms)
                    await handle_screen_time_popup(
                        page,
                        action_timeout_ms=self.action_timeout_ms,
                        probe_timeout_ms=self.probe_timeout_ms,
                    )
                    if after_click_wait_ms:
                        await page.wait_for_timeout(after_click_wait_ms)
                    return

            await conversation_list.evaluate(
                f"(element) => element.scrollBy(0, {scroll_distance})",
                timeout=self.action_timeout_ms,
            )
            await page.wait_for_timeout(500)

        raise ValueError(f"Could not find conversation for display name: {display_name}")

    async def _click_unique_conversation_by_display_name(
        self,
        page: Any,
        conversation_list: Any,
        conversation_items: Any,
        display_name: str,
        normalized_display_name: str,
        *,
        max_scrolls: int,
        scroll_distance: int,
        after_click_wait_ms: int,
    ) -> None:
        """Open one exact row, never a raw global DM-item locator.

        The preliminary scan finds an exact nickname while the list can be
        virtualized.  Before clicking, it re-resolves that nickname as a
        row-scoped locator.  This avoids acting on an unfiltered global
        ``dm-new-conversation-item`` locator rendered as ``.first`` by
        Playwright when the matched index is zero.
        """

        started_at = self._monotonic()
        for _ in range(max_scrolls):
            matches: list[Any] = []
            total_item_count = await conversation_items.count()
            for index in range(total_item_count):
                item = conversation_items.nth(index)
                nickname = item.locator(CONVERSATION_NICKNAME_SELECTOR)
                nickname_text = (
                    await nickname.inner_text(timeout=self.action_timeout_ms)
                ).strip()
                if nickname_text.casefold() == normalized_display_name:
                    matches.append(item)

            if len(matches) > 1:
                raise AmbiguousConversationError(display_name, len(matches))
            if len(matches) == 1:
                matched_row = self._exact_conversation_row_locator(page, display_name)
                matched_item_count = await matched_row.count()
                if matched_item_count > 1:
                    raise AmbiguousConversationError(display_name, matched_item_count)
                if matched_item_count == 1:
                    matched_display_name = (
                        await matched_row.locator(CONVERSATION_NICKNAME_SELECTOR).inner_text(
                            timeout=self.action_timeout_ms
                        )
                    ).strip()
                    logger.info(
                        "Exact TikTok DM row matched in %.2fs for %r",
                        self._monotonic() - started_at,
                        display_name,
                    )
                    state = await self._log_matched_conversation_diagnostics(
                        matched_row,
                        display_name=display_name,
                        total_item_count=total_item_count,
                        matched_item_count=matched_item_count,
                        # This is read from the exact row that will be
                        # clicked, not from the last row visited while
                        # scanning. Do not read the row's full text: it can
                        # include a private message preview.
                        matched_display_name=matched_display_name,
                    )
                    await self._open_matched_conversation(
                        page,
                        matched_row,
                        state,
                        after_click_wait_ms=after_click_wait_ms,
                    )
                    return

                # A virtualized list can update after its nickname text is
                # read. Continue the bounded lookup rather than falling back
                # to the unscoped ``item`` locator.
                logger.warning(
                    "Exact TikTok DM row disappeared before click for %r; continuing lookup",
                    display_name,
                )

            await conversation_list.evaluate(
                f"(element) => element.scrollBy(0, {scroll_distance})",
                timeout=self.action_timeout_ms,
            )
            await page.wait_for_timeout(500)

        raise ValueError(f"Could not find conversation for display name: {display_name}")

    @staticmethod
    def _exact_conversation_row_locator(page: Any, display_name: str) -> Any:
        """Return rows containing precisely the requested display-name element."""

        exact_name = re.compile(rf"^{re.escape(display_name)}$", re.IGNORECASE)
        matching_nicknames = page.locator(CONVERSATION_NICKNAME_SELECTOR).filter(
            has_text=exact_name
        )
        return page.locator(CONVERSATION_ITEM_SELECTOR).filter(has=matching_nicknames)

    async def _log_matched_conversation_diagnostics(
        self,
        matched_row: Any,
        *,
        display_name: str,
        total_item_count: int,
        matched_item_count: int,
        matched_display_name: str,
    ) -> _MatchedConversationState:
        """Log only row/actionability data needed to diagnose a resolver click."""

        bounding_box: Any = "<unavailable>"
        is_visible: bool | str = "<unavailable>"
        aria_selected: str | None = None
        try:
            bounding_box = await matched_row.bounding_box(
                timeout=self.probe_timeout_ms
            )
        except Exception:
            pass
        try:
            is_visible = await matched_row.is_visible(timeout=self.probe_timeout_ms)
        except Exception:
            pass
        try:
            aria_selected = await matched_row.get_attribute(
                "aria-selected",
                timeout=self.probe_timeout_ms,
            )
        except Exception:
            pass

        already_selected = str(aria_selected or "").casefold() == "true"
        logger.info(
            "Matched TikTok DM row: target_display_name=%r total_dm_item_count=%d "
            "matched_item_count=%d matched_display_name=%r bounding_box=%s "
            "is_visible=%s aria_selected=%r already_selected=%s",
            display_name,
            total_item_count,
            matched_item_count,
            matched_display_name,
            bounding_box,
            is_visible,
            aria_selected,
            already_selected,
        )
        return _MatchedConversationState(
            aria_selected=aria_selected,
            already_selected=already_selected,
        )

    async def _open_matched_conversation(
        self,
        page: Any,
        matched_row: Any,
        state: _MatchedConversationState,
        *,
        after_click_wait_ms: int,
    ) -> None:
        """Open one exact row with a bounded DOM-click fallback if required."""

        click_timeout_ms = min(self.action_timeout_ms, RESOLUTION_CLICK_TIMEOUT_MS)
        if state.already_selected:
            logger.info("Matched TikTok DM row is already selected; skipping click")
        else:
            try:
                await matched_row.scroll_into_view_if_needed(timeout=click_timeout_ms)
            except PlaywrightTimeoutError:
                logger.warning("Matched TikTok DM row did not finish scrolling before click")

            click_started_at = self._monotonic()
            used_dom_click_fallback = False
            try:
                await matched_row.click(timeout=click_timeout_ms)
            except PlaywrightTimeoutError:
                used_dom_click_fallback = True
                logger.warning(
                    "Normal click timed out for matched TikTok DM row; using bounded DOM click fallback"
                )
                await matched_row.evaluate(
                    "(element) => element.click()",
                    timeout=click_timeout_ms,
                )
            logger.info(
                "Matched TikTok DM row click completed in %.2fs (dom_fallback=%s)",
                self._monotonic() - click_started_at,
                used_dom_click_fallback,
            )

        await handle_screen_time_popup(
            page,
            action_timeout_ms=self.action_timeout_ms,
            probe_timeout_ms=self.probe_timeout_ms,
        )
        await self._wait_for_matched_conversation_open(page, matched_row, state)
        if after_click_wait_ms:
            await page.wait_for_timeout(after_click_wait_ms)

    async def _wait_for_matched_conversation_open(
        self,
        page: Any,
        matched_row: Any,
        state: _MatchedConversationState,
    ) -> None:
        """Require row-selection/header evidence before extracting a username."""

        opened_started_at = self._monotonic()
        if not state.already_selected and state.aria_selected is not None:
            deadline = opened_started_at + (self.action_timeout_ms / 1000)
            while True:
                try:
                    current_value = await matched_row.get_attribute(
                        "aria-selected",
                        timeout=self.probe_timeout_ms,
                    )
                except Exception:
                    current_value = None
                if str(current_value or "").casefold() == "true":
                    break
                remaining_milliseconds = int((deadline - self._monotonic()) * 1000)
                if remaining_milliseconds <= 0:
                    raise ValueError(
                        "Matched TikTok DM row did not become the selected conversation."
                    )
                await page.wait_for_timeout(
                    min(self.probe_timeout_ms, max(1, remaining_milliseconds))
                )

        chat_username = page.locator(CHAT_USERNAME_SELECTOR).first
        await chat_username.wait_for(state="visible", timeout=self.action_timeout_ms)
        elapsed_seconds = self._monotonic() - opened_started_at
        logger.info("TikTok DM chat opened in %.2fs", elapsed_seconds)
        logger.info("TikTok chat unique-id appeared in %.2fs", elapsed_seconds)

    async def extract_username_from_chat(self, page: Any) -> str:
        """Read the unique TikTok username shown in an opened chat."""

        element = page.locator(CHAT_USERNAME_SELECTOR).first
        try:
            await element.wait_for(state="visible", timeout=self.action_timeout_ms)
        except PlaywrightTimeoutError:
            logger.warning("Timed out waiting for chat unique id")
            return ""

        username_text = await element.inner_text(timeout=self.action_timeout_ms)
        return username_text.strip().lstrip("@")

    async def extract_username_from_item(self, item: Any, fallback: str = "") -> str:
        """Extract a username from profile links within a conversation item."""

        hrefs = await item.locator("a[href^='/@'], a[href*='tiktok.com/@']").evaluate_all(
            """(links) => links.map((link) => link.getAttribute("href"))"""
        )
        for href in hrefs:
            username = username_from_href(href)
            if username:
                return username
        return fallback

    async def select_recipient_for_username(
        self,
        page: Any,
        username: str,
        notifier: Any,
        *,
        configured_display_name: str | None = None,
    ) -> str:
        """Open a recipient DM while retaining the legacy string-only API."""

        _, display_name = await self.select_recipient_for_username_with_page(
            page,
            username,
            notifier,
            configured_display_name=configured_display_name,
        )
        return display_name

    async def select_recipient_for_username_with_page(
        self,
        page: Any,
        username: str,
        notifier: Any,
        *,
        configured_display_name: str | None = None,
    ) -> tuple[Any, str]:
        """Open the DM conversation that corresponds to a known username.

        A configured display name is already the DM-list key TikTok exposes.
        Prefer it when available so sending does not need a separate profile
        navigation merely to rediscover the same value.  Profile lookup stays
        as the compatibility fallback for older recipient records without a
        display name.
        """

        display_name = (configured_display_name or "").strip()
        if display_name:
            logger.info(
                "Selecting recipient %s using configured display name",
                display_name,
            )
            return await self._select_recipient_by_display_name(
                page,
                display_name,
                username,
                notifier,
                reject_ambiguous=True,
            )

        logger.info("Display name unavailable; falling back to TikTok profile lookup")

        normalized_username = username.lstrip("@")
        await navigate_to_profile(
            page,
            normalized_username,
            navigation_timeout_ms=self.navigation_timeout_ms,
        )
        await self._handle_tiktok_interruptions(page, notifier)

        display_name_locator = page.locator("h1[data-e2e='user-title']").first
        if await display_name_locator.count() == 0:
            display_name_locator = page.locator("[data-e2e='user-title']").first
        await display_name_locator.wait_for(
            state="visible",
            timeout=self.action_timeout_ms,
        )
        display_name = (
            await display_name_locator.inner_text(timeout=self.action_timeout_ms)
        ).strip()
        logger.info(
            "Resolved TikTok username @%s to display name %s",
            normalized_username,
            display_name,
        )

        return await self._select_recipient_by_display_name(
            page,
            display_name,
            username,
            notifier,
            reject_ambiguous=False,
        )

    async def _select_recipient_by_display_name(
        self,
        page: Any,
        display_name: str,
        username: str,
        notifier: Any,
        *,
        reject_ambiguous: bool,
    ) -> tuple[Any, str]:
        """Open a selected recipient's DM and wait for its message composer."""

        page, readiness = await self._load_messages_with_renderer_recovery(
            page,
            caller="Send",
        )
        await self._handle_tiktok_interruptions(page, notifier)
        await self.click_conversation_by_display_name(
            page,
            display_name,
            max_scrolls=10,
            scroll_distance=400,
            after_click_wait_ms=0,
            reject_ambiguous=reject_ambiguous,
            readiness=readiness,
        )
        await handle_sleep_hours_popup(
            page,
            action_timeout_ms=self.action_timeout_ms,
            probe_timeout_ms=self.probe_timeout_ms,
        )

        editable_elements = await page.evaluate(
            """() => Array.from(
                document.querySelectorAll("[contenteditable], input, textarea")
            ).map((element) => ({
                tag: element.tagName.toLowerCase(),
                dataE2e: element.getAttribute("data-e2e"),
                role: element.getAttribute("role"),
                placeholder: element.getAttribute("placeholder"),
                ariaLabel: element.getAttribute("aria-label"),
                contenteditable: element.getAttribute("contenteditable")
            }))"""
        )
        logger.debug("Editable elements after selecting %s: %s", username, editable_elements)
        await self.wait_for_message_input(page)
        return page, display_name

    async def wait_for_message_input(self, page: Any) -> Any:
        """Wait for the composer visible after a recipient has been selected."""

        input_locator = page.locator(MESSAGE_INPUT_SELECTOR).first
        await input_locator.wait_for(state="visible", timeout=self.action_timeout_ms)
        return input_locator

    async def _handle_tiktok_interruptions(self, page: Any, notifier: Any) -> None:
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
            notifier,
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

    async def wait_for_dm_readiness(self, page: Any) -> tuple[Any, Any, bool]:
        """Use a soft DOM stage, then wait for a usable DM state after navigation."""

        initial_ready_state = await self._wait_for_dom_execution_context(page)
        if self.low_memory_mode and initial_ready_state == "loading":
            await self._stabilize_low_memory_dom(page)
        logger.info("Waiting for DM readiness")
        start_time = self._monotonic()
        readiness = await self._wait_for_dm_readiness(page)
        logger.info("DM readiness reached in %.2fs", self._monotonic() - start_time)
        return readiness

    async def _wait_for_dom_execution_context(self, page: Any) -> str | None:
        """Give a committed TikTok document a bounded chance to create its DOM context.

        A navigation commit can precede both ``DOMContentLoaded`` and a usable
        JavaScript execution context on an overloaded Chromium renderer.  This
        stage is intentionally soft: either signal lets the normal explicit DM
        readiness loop begin, while a bounded timeout on an active TikTok URL
        falls through to that loop unchanged.
        """

        logger.info("Waiting for TikTok DOM execution context")
        deadline = self._monotonic() + (self.action_timeout_ms / 1000)
        wait_for_load_state = getattr(page, "wait_for_load_state", None)
        load_state_task: asyncio.Task[object] | None = None
        if callable(wait_for_load_state):
            load_state_task = asyncio.create_task(
                wait_for_load_state("domcontentloaded", timeout=self.action_timeout_ms)
            )

        context_unavailable_logged = False
        try:
            while True:
                if load_state_task is not None and load_state_task.done():
                    try:
                        await load_state_task
                    except PlaywrightTimeoutError:
                        return self._continue_after_soft_dom_timeout(page)
                    except Exception as exc:
                        if self._is_transient_renderer_probe_error(exc):
                            return self._continue_after_soft_dom_timeout(page)
                        raise
                    logger.debug("TikTok DOMContentLoaded reached before DM readiness")
                    return None

                now = self._monotonic()
                if now >= deadline:
                    return self._continue_after_soft_dom_timeout(page)

                try:
                    ready_state = await self._take_dom_execution_context_probe(page)
                except _ReadinessProbeUnavailable:
                    if not context_unavailable_logged:
                        logger.info("TikTok DOM execution context not available yet")
                        context_unavailable_logged = True
                else:
                    logger.info(
                        "DOM execution context available (readyState=%s)",
                        ready_state,
                    )
                    return ready_state

                await self._sleep_until_next_soft_dom_probe(deadline)
        finally:
            if load_state_task is not None and not load_state_task.done():
                load_state_task.cancel()
                with suppress(asyncio.CancelledError):
                    await load_state_task

    @staticmethod
    def _continue_after_soft_dom_timeout(page: Any) -> None:
        """Fall through only when the committed document remains a TikTok URL."""

        if is_tiktok_http_url(getattr(page, "url", "")):
            logger.info(
                "TikTok DOM soft wait timed out; continuing to explicit DM readiness."
            )
            return None
        raise TikTokDMReadinessError(
            "TikTok messages navigation did not retain a valid TikTok page before readiness."
        )

    async def _stabilize_low_memory_dom(self, page: Any) -> None:
        """Let a newly-created low-memory renderer settle before DM polling begins."""

        logger.info(
            "Low-memory stabilization wait: %.1fs",
            self.low_memory_stabilization_seconds,
        )
        if self.low_memory_stabilization_seconds:
            await asyncio.sleep(self.low_memory_stabilization_seconds)

        try:
            ready_state = await self._take_dom_execution_context_probe(page)
        except _ReadinessProbeUnavailable:
            logger.info("DOM state after stabilization: <unavailable>")
        else:
            logger.info("DOM state after stabilization: %s", ready_state)

    async def _wait_for_dm_readiness(self, page: Any) -> tuple[Any, Any, bool]:
        """Poll one lightweight DOM snapshot until TikTok DM content is usable.

        The ARM renderer can time out while servicing several Playwright
        locator RPCs in a row.  Snapshot all passive readiness signals in one
        page-side evaluation, and reserve locators for real interactions only
        after a usable state has been found.
        """

        deadline = self._monotonic() + (self.dm_readiness_timeout_ms / 1000)
        probe_state = _ReadinessProbeState()
        while True:
            self._raise_for_login(page)
            now = self._monotonic()
            if now >= deadline:
                break

            try:
                snapshot = await self._take_dm_readiness_snapshot(page)
            except _ReadinessProbeUnavailable:
                self._record_readiness_probe_failure(probe_state, now)
                await self._sleep_until_next_readiness_probe(deadline)
                continue

            self._record_readiness_probe_success(probe_state, snapshot)
            if int(snapshot["login"]) > 0:
                raise TikTokLoginRequiredError(
                    "TikTok login is required before the DM conversation list can load."
                )
            if int(snapshot["captcha"]) > 0:
                raise TikTokChallengeRequiredError(
                    "TikTok CAPTCHA or security challenge detected before the DM list loaded."
                )

            # The snapshot itself is the cheap popup detector.  Only then
            # issue locator/keyboard actions to dismiss a popup that is known
            # to exist, rather than probing every popup selector on each poll.
            if bool(snapshot["screen_time"]):
                await handle_screen_time_popup(
                    page,
                    action_timeout_ms=self.action_timeout_ms,
                    probe_timeout_ms=self.probe_timeout_ms,
                )
            if bool(snapshot["sleep_hours"]):
                await handle_sleep_hours_popup(
                    page,
                    action_timeout_ms=self.action_timeout_ms,
                    probe_timeout_ms=self.probe_timeout_ms,
                )
            if bool(snapshot["screen_time"]) or bool(snapshot["sleep_hours"]):
                await self._sleep_until_next_readiness_probe(deadline)
                continue

            conversation_list_available = int(snapshot["dm_list"]) > 0
            conversation_items_available = int(snapshot["dm_items"]) > 0
            if conversation_list_available or conversation_items_available:
                return (
                    page.locator(CONVERSATION_LIST_SELECTOR).first,
                    page.locator(CONVERSATION_ITEM_SELECTOR),
                    False,
                )
            if int(snapshot["empty_state"]) > 0:
                return (
                    page.locator(CONVERSATION_LIST_SELECTOR).first,
                    page.locator(CONVERSATION_ITEM_SELECTOR),
                    True,
                )

            await self._sleep_until_next_readiness_probe(deadline)

        await self._log_dm_readiness_timeout(page, probe_state)
        if probe_state.consecutive_failures >= 2:
            duration = self._renderer_unresponsive_seconds(probe_state, self._monotonic())
            message = (
                "TikTok renderer did not respond to readiness probes for "
                f"{duration:.1f} seconds."
            )
            logger.warning(message)
            raise TikTokRendererUnresponsiveError(message)
        raise TikTokDMReadinessError(
            "Timed out waiting for TikTok DM conversations, an empty state, login, or CAPTCHA."
        )

    @staticmethod
    def _raise_for_login(page: Any) -> None:
        page_url = str(getattr(page, "url", ""))
        if "login" in page_url.casefold():
            raise TikTokLoginRequiredError(
                "TikTok login is required before the DM conversation list can load."
            )

    async def _take_dm_readiness_snapshot(self, page: Any) -> dict[str, int | bool]:
        """Collect every passive readiness signal in one bounded renderer RPC."""

        try:
            raw_snapshot = await asyncio.wait_for(
                page.evaluate(READINESS_SNAPSHOT_SCRIPT),
                timeout=self.probe_timeout_ms / 1000,
            )
        except (asyncio.TimeoutError, PlaywrightTimeoutError) as exc:
            raise _ReadinessProbeUnavailable from exc
        except Exception as exc:
            if self._is_transient_renderer_probe_error(exc):
                raise _ReadinessProbeUnavailable from exc
            logger.exception("Unexpected DM readiness snapshot failure")
            raise

        if not isinstance(raw_snapshot, Mapping):
            raise RuntimeError("TikTok DM readiness snapshot was not an object.")
        return {
            "dm_list": self._snapshot_count(raw_snapshot, "dmList"),
            "dm_items": self._snapshot_count(raw_snapshot, "dmItems"),
            "empty_state": self._snapshot_count(raw_snapshot, "emptyState"),
            "login": self._snapshot_count(raw_snapshot, "login"),
            "captcha": self._snapshot_count(raw_snapshot, "captcha"),
            "screen_time": bool(raw_snapshot.get("screenTime", False)),
            "sleep_hours": bool(raw_snapshot.get("sleepHours", False)),
        }

    async def _take_dom_execution_context_probe(self, page: Any) -> str:
        """Check only whether JavaScript can run before issuing a full DM snapshot."""

        try:
            ready_state = await asyncio.wait_for(
                page.evaluate(DOM_EXECUTION_CONTEXT_SCRIPT),
                timeout=self.probe_timeout_ms / 1000,
            )
        except (asyncio.TimeoutError, PlaywrightTimeoutError) as exc:
            raise _ReadinessProbeUnavailable from exc
        except Exception as exc:
            if self._is_transient_renderer_probe_error(exc):
                raise _ReadinessProbeUnavailable from exc
            logger.exception("Unexpected TikTok DOM execution-context probe failure")
            raise

        if not isinstance(ready_state, str):
            raise RuntimeError("TikTok DOM execution-context probe did not return readyState.")
        return ready_state

    @staticmethod
    def _snapshot_count(snapshot: Mapping[str, Any], key: str) -> int:
        try:
            return max(0, int(snapshot.get(key, 0)))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _is_transient_renderer_probe_error(error: Exception) -> bool:
        message = str(error).casefold()
        return any(marker in message for marker in TRANSIENT_RENDERER_PROBE_ERROR_MARKERS)

    async def _sleep_until_next_readiness_probe(self, deadline: float) -> None:
        remaining_seconds = deadline - self._monotonic()
        if remaining_seconds > 0:
            await asyncio.sleep(min(self.probe_timeout_ms / 1000, remaining_seconds))

    async def _sleep_until_next_soft_dom_probe(self, deadline: float) -> None:
        """Yield modestly while the renderer establishes its first document context."""

        await self._sleep_until_next_readiness_probe(deadline)

    def _record_readiness_probe_failure(
        self,
        probe_state: _ReadinessProbeState,
        now: float,
    ) -> None:
        probe_state.failed_probes += 1
        probe_state.consecutive_failures += 1
        if probe_state.consecutive_failure_started_at is None:
            probe_state.consecutive_failure_started_at = now
            logger.warning("DM readiness probe temporarily unavailable")

        unresponsive_seconds = self._renderer_unresponsive_seconds(probe_state, now)
        if (
            unresponsive_seconds >= 5
            and (
                probe_state.last_renderer_report_at is None
                or now - probe_state.last_renderer_report_at >= 5
            )
        ):
            logger.warning(
                "TikTok renderer did not respond to readiness probes for %.1f seconds",
                unresponsive_seconds,
            )
            probe_state.last_renderer_report_at = now

    @staticmethod
    def _record_readiness_probe_success(
        probe_state: _ReadinessProbeState,
        snapshot: dict[str, int | bool],
    ) -> None:
        probe_state.successful_probes += 1
        probe_state.consecutive_failures = 0
        probe_state.consecutive_failure_started_at = None
        probe_state.last_renderer_report_at = None
        probe_state.last_snapshot = snapshot

    @staticmethod
    def _renderer_unresponsive_seconds(
        probe_state: _ReadinessProbeState,
        now: float,
    ) -> float:
        if probe_state.consecutive_failure_started_at is None:
            return 0.0
        return max(0.0, now - probe_state.consecutive_failure_started_at)

    async def _log_dm_readiness_timeout(
        self,
        page: Any,
        probe_state: _ReadinessProbeState,
    ) -> None:
        """Log bounded, credential-free browser diagnostics for a readiness timeout."""

        url = self._sanitize_url(str(getattr(page, "url", "")))
        title, body_prefix = await asyncio.gather(
            self._read_readiness_title(page),
            self._read_readiness_body_prefix(page),
        )
        last_snapshot = probe_state.last_snapshot or {}
        renderer_unresponsive_seconds = self._renderer_unresponsive_seconds(
            probe_state,
            self._monotonic(),
        )
        counts = {
            "dm_list": last_snapshot.get("dm_list", "<unavailable>"),
            "dm_items": last_snapshot.get("dm_items", "<unavailable>"),
            "login": last_snapshot.get("login", "<unavailable>"),
            "captcha": last_snapshot.get("captcha", "<unavailable>"),
        }
        logger.warning(
            "TikTok DM readiness timed out: url=%s title=%r body_prefix=%r "
            "last_snapshot=%s successful_probes=%d failed_probes=%d "
            "renderer_unresponsive_seconds=%.1f last_counts=%s",
            url or "<unavailable>",
            title,
            body_prefix,
            last_snapshot or "<unavailable>",
            probe_state.successful_probes,
            probe_state.failed_probes,
            renderer_unresponsive_seconds,
            counts,
        )

    async def _read_readiness_title(self, page: Any) -> str:
        try:
            title = await asyncio.wait_for(
                page.title(),
                timeout=self.probe_timeout_ms / 1000,
            )
            return self._bounded_text(title)
        except Exception:
            return "<unavailable>"

    async def _read_readiness_body_prefix(self, page: Any) -> str:
        try:
            body_text = await asyncio.wait_for(
                page.evaluate(
                    "() => (document.body?.innerText || '').slice(0, 500)"
                ),
                timeout=self.probe_timeout_ms / 1000,
            )
            return self._bounded_text(body_text)
        except Exception:
            return "<unavailable>"

    @staticmethod
    def _sanitize_url(page_url: str) -> str:
        """Remove query/fragment data, which can contain session material."""

        try:
            parsed = urlsplit(page_url)
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        except ValueError:
            return "<unavailable>"

    @staticmethod
    def _bounded_text(value: Any) -> str:
        compact = " ".join(str(value or "").split())
        compact = SENSITIVE_DIAGNOSTIC_VALUE_PATTERN.sub(
            lambda match: f"{match.group(1)}=<redacted>",
            compact,
        )
        return compact[:READINESS_BODY_PREFIX_MAX_CHARS]
