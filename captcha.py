"""Legacy compatibility imports for shared TikTok interaction helpers."""

from daily_streak.tiktok import (
    CAPTCHA_SELECTOR_MARKERS,
    CAPTCHA_TEXT_MARKERS,
    handle_screen_time_popup,
    handle_sleep_hours_popup,
    is_captcha_present,
    wait_for_captcha_if_present,
)


__all__ = [
    "CAPTCHA_SELECTOR_MARKERS",
    "CAPTCHA_TEXT_MARKERS",
    "handle_screen_time_popup",
    "handle_sleep_hours_popup",
    "is_captcha_present",
    "wait_for_captcha_if_present",
]
