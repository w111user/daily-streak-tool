"""Legacy compatibility imports for the v2 sender core."""

from daily_streak.browser import (
    check_cookies_valid,
    get_chrome_path,
    get_default_user_data_dir,
    get_user_data_dir_key,
)
from daily_streak.sender import TikTokSender, show_cookie_warning_if_ui_running


__all__ = [
    "TikTokSender",
    "check_cookies_valid",
    "get_chrome_path",
    "get_default_user_data_dir",
    "get_user_data_dir_key",
    "show_cookie_warning_if_ui_running",
]
