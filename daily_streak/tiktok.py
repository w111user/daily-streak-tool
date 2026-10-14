"""TikTok page-interaction helpers that do not depend on a UI adapter."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from daily_streak.browser import DEFAULT_ACTION_TIMEOUT_MS, DEFAULT_PROBE_TIMEOUT_MS


logger = logging.getLogger(__name__)

CAPTCHA_PROBE_TIMEOUT_MS = DEFAULT_PROBE_TIMEOUT_MS
POPUP_PROBE_TIMEOUT_MS = DEFAULT_PROBE_TIMEOUT_MS

CAPTCHA_TEXT_MARKERS = (
    "captcha",
    "verify",
    "verification",
    "security check",
    "drag the slider",
    "please try again",
    "Snooze",
)

CAPTCHA_SELECTOR_MARKERS = (
    "iframe[src*='captcha']",
    "iframe[src*='verify']",
    "[class*='captcha' i]",
    "[id*='captcha' i]",
    "[class*='verify' i]",
    "[id*='verify' i]",
)


class CaptchaNotifier(Protocol):
    async def captcha_alert(self, page_url: str) -> None:
        """Notify the user that CAPTCHA intervention is required."""


async def is_captcha_present(
    page: Any,
    *,
    probe_timeout_ms: int = CAPTCHA_PROBE_TIMEOUT_MS,
) -> bool:
    """Return whether TikTok currently exposes a CAPTCHA/challenge indicator."""

    for selector in CAPTCHA_SELECTOR_MARKERS:
        try:
            if await page.locator(selector).first.is_visible(timeout=probe_timeout_ms):
                return True
        except Exception:
            logger.debug("CAPTCHA selector check failed: %s", selector, exc_info=True)

    try:
        text = (
            await page.locator("body").inner_text(timeout=probe_timeout_ms)
        ).lower()
    except Exception:
        return False

    return any(marker.lower() in text for marker in CAPTCHA_TEXT_MARKERS)


async def wait_for_captcha_if_present(
    page: Any,
    notifier: CaptchaNotifier,
    poll_seconds: int = 5,
    *,
    probe_timeout_ms: int = CAPTCHA_PROBE_TIMEOUT_MS,
) -> None:
    if not await is_captcha_present(page, probe_timeout_ms=probe_timeout_ms):
        return

    await notifier.captcha_alert(page.url)
    logger.warning("CAPTCHA detected. Waiting for user to solve it.")

    while True:
        await page.wait_for_timeout(poll_seconds * 1000)
        if not await is_captcha_present(page, probe_timeout_ms=probe_timeout_ms):
            logger.info("CAPTCHA solved, continuing.")
            break
        logger.info("CAPTCHA still present, checking again in %ss", poll_seconds)


async def handle_screen_time_popup(
    page: Any,
    *,
    action_timeout_ms: int = DEFAULT_ACTION_TIMEOUT_MS,
    probe_timeout_ms: int = POPUP_PROBE_TIMEOUT_MS,
) -> None:
    """Dismiss Screen Time after a bounded probe finds its dialog."""

    try:
        title = page.locator("h1:has-text('Ready to close TikTok?')")
        await title.wait_for(state="visible", timeout=probe_timeout_ms)
        await page.keyboard.type("1234")

        return_button = page.locator("button:has-text('Return to TikTok')").first
        await return_button.wait_for(state="visible", timeout=action_timeout_ms)
        await return_button.click(timeout=action_timeout_ms)

        await page.wait_for_timeout(1000)
        logger.info("Dismissed Screen Time popup.")
    except Exception:
        pass


async def handle_sleep_hours_popup(
    page: Any,
    *,
    action_timeout_ms: int = DEFAULT_ACTION_TIMEOUT_MS,
    probe_timeout_ms: int = POPUP_PROBE_TIMEOUT_MS,
) -> None:
    """Dismiss Sleep Hours when its short bounded probe finds the button."""

    try:
        button = page.locator(
            "button:has-text('Return for now'),"
            "button:has-text('Quay lại')"
        ).first
        await button.wait_for(state="visible", timeout=probe_timeout_ms)
        await button.click(timeout=action_timeout_ms)
        await page.wait_for_timeout(1000)
        logger.info("Dismissed Sleep Hours popup.")
    except Exception:
        pass
