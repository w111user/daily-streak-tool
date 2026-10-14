"""Shared, lightweight navigation semantics for TikTok routes."""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from daily_streak.browser import DEFAULT_NAVIGATION_TIMEOUT_MS


logger = logging.getLogger(__name__)

TIKTOK_MESSAGES_URL = "https://www.tiktok.com/messages"


async def navigate_to_messages(
    page: Any,
    *,
    navigation_timeout_ms: int = DEFAULT_NAVIGATION_TIMEOUT_MS,
) -> None:
    """Commit navigation to TikTok messages without waiting for page DOM readiness.

    Callers that need usable DM content must follow this with their explicit
    locator/readiness checks.  That separation avoids treating TikTok's slow
    long-running DOM work as a navigation failure on low-resource hosts.
    """

    await _navigate_to_tiktok_route(
        page,
        TIKTOK_MESSAGES_URL,
        route_name="messages",
        navigation_timeout_ms=navigation_timeout_ms,
    )


async def navigate_to_profile(
    page: Any,
    username: str,
    *,
    navigation_timeout_ms: int = DEFAULT_NAVIGATION_TIMEOUT_MS,
) -> None:
    """Commit navigation to a TikTok profile before caller-owned locator waits."""

    normalized_username = username.lstrip("@")
    await _navigate_to_tiktok_route(
        page,
        f"https://www.tiktok.com/@{normalized_username}",
        route_name="profile",
        navigation_timeout_ms=navigation_timeout_ms,
    )


async def _navigate_to_tiktok_route(
    page: Any,
    url: str,
    *,
    route_name: str,
    navigation_timeout_ms: int,
) -> None:
    """Navigate through the commit milestone while retaining finite failures."""

    timeout_ms = max(1, int(navigation_timeout_ms))
    start_time = time.monotonic()
    logger.info("Navigating to TikTok %s", route_name)
    try:
        await page.goto(url, wait_until="commit", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        elapsed_seconds = time.monotonic() - start_time
        if is_tiktok_http_url(getattr(page, "url", "")):
            # Playwright can time out while reporting a navigation even though
            # Chromium has already committed the TikTok document.  The caller's
            # locator-based readiness policy can now determine usable content.
            logger.warning(
                "TikTok %s navigation timed out after %.2fs, but a TikTok URL is active; "
                "continuing to explicit readiness.",
                route_name,
                elapsed_seconds,
            )
            return
        raise
    logger.info(
        "Navigation committed in %.2fs for TikTok %s",
        time.monotonic() - start_time,
        route_name,
    )


def is_tiktok_http_url(url: object) -> bool:
    """Return whether a page URL is a committed HTTP(S) TikTok document."""

    try:
        parsed = urlsplit(str(url))
    except ValueError:
        return False
    hostname = (parsed.hostname or "").casefold()
    return parsed.scheme in {"http", "https"} and (
        hostname == "tiktok.com" or hostname.endswith(".tiktok.com")
    )


def _is_tiktok_http_url(url: object) -> bool:
    """Compatibility alias for the former private helper name."""

    return is_tiktok_http_url(url)
