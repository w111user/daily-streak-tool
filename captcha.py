import logging

from playwright.async_api import Page

from notifier import Notifier


logger = logging.getLogger(__name__)

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


async def is_captcha_present(page: Page) -> bool:
    for selector in CAPTCHA_SELECTOR_MARKERS:
        try:
            if await page.locator(selector).first.is_visible():
                return True
        except Exception:
            logger.debug("CAPTCHA selector check failed: %s", selector, exc_info=True)

    try:
        text = (await page.locator("body").inner_text(timeout=3000)).lower()
    except Exception:
        return False

    return any(marker.lower() in text for marker in CAPTCHA_TEXT_MARKERS)


async def wait_for_captcha_if_present(
    page: Page,
    notifier: Notifier,
    poll_seconds: int = 5,
) -> None:
    if not await is_captcha_present(page):
        return

    await notifier.captcha_alert(page.url)
    logger.warning("CAPTCHA detected. Waiting for user to solve it.")

    while True:
        await page.wait_for_timeout(poll_seconds * 1000)
        if not await is_captcha_present(page):
            logger.info("CAPTCHA solved, continuing.")
            break
        logger.info("CAPTCHA still present, checking again in %ss...", poll_seconds)



async def handle_screen_time_popup(page: Page) -> None:
    try:
        title = page.locator("h1:has-text('Ready to close TikTok?')")
        await title.wait_for(state="visible", timeout=2000)
        await page.keyboard.type("1234")

        return_button = page.locator("button:has-text('Return to TikTok')").first
        await return_button.wait_for(state="visible", timeout=5000)
        await return_button.click()

        await page.wait_for_timeout(1000)
        logger.info("Dismissed Screen Time popup.")
    except Exception:
        pass


async def handle_sleep_hours_popup(page: Page) -> None:
    try:
        btn = page.locator(
            "button:has-text('Return for now'),"
            "button:has-text('Quay lại')"
        ).first
        await btn.wait_for(state="visible", timeout=2000)
        await btn.click()
        await page.wait_for_timeout(1000)
        logger.info("Dismissed Sleep Hours popup.")
    except Exception:
        pass
