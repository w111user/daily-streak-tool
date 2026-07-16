import asyncio
import json
import logging
import platform
import random
import time
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page, async_playwright

from captcha import handle_screen_time_popup, handle_sleep_hours_popup, wait_for_captcha_if_present
from notifier import Notifier
from video_pool import VideoPool


logger = logging.getLogger(__name__)


def get_chrome_path() -> str:
    system = platform.system()
    if system == "Windows":
        return r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    if system == "Darwin":
        return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    return "/usr/bin/google-chrome"


def get_default_user_data_dir() -> str:
    system = platform.system()
    if system == "Windows":
        return str(Path.home() / "AppData/Local/chrome-debug")
    if system == "Darwin":
        return str(Path.home() / "Library/Application Support/chrome-debug")
    return str(Path.home() / ".config/chrome-debug")


def get_user_data_dir_key() -> str:
    system = platform.system()
    if system == "Windows":
        return "user_data_dir_windows"
    if system == "Darwin":
        return "user_data_dir_macos"
    return "user_data_dir_linux"


def check_cookies_valid(cookie_file: str | Path) -> bool:
    cookie_path = Path(cookie_file)
    if not cookie_path.exists():
        return False

    try:
        with cookie_path.open("r", encoding="utf-8") as file:
            cookies = json.load(file)
    except Exception:
        logger.exception("Failed to load cookies from %s", cookie_path)
        return False

    for cookie in cookies:
        if cookie.get("name") != "sessionid":
            continue

        expires = cookie.get("expires", cookie.get("expirationDate"))
        if expires is None:
            return False

        try:
            return time.time() < float(expires)
        except (TypeError, ValueError):
            logger.warning("Invalid sessionid expiry timestamp in %s: %r", cookie_path, expires)
            return False

    return False


def show_cookie_warning_if_ui_running() -> None:
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


def log_duration(step: str, start_time: float) -> None:
    logger.info("%s took %.2fs", step, time.time() - start_time)


async def timed_await(step: str, awaitable: Any) -> Any:
    start_time = time.time()
    try:
        result = await awaitable
    except Exception:
        logger.exception("%s failed after %.2fs", step, time.time() - start_time)
        raise
    logger.info("%s took %.2fs", step, time.time() - start_time)
    return result


class TikTokSender:
    def __init__(self, config: dict[str, Any], notifier: Notifier, video_pool: VideoPool) -> None:
        self.config = config
        self.notifier = notifier
        self.video_pool = video_pool
        self.tiktok_config = config.get("tiktok", {})
        self.recipients = config.get("recipients", [])

    async def send_daily_links(self) -> None:
        if not self.recipients:
            logger.warning("No recipients configured.")
            return

        cookie_file = self.config.get("cookie_file", "cookies.json")
        if not check_cookies_valid(cookie_file):
            logger.error("Cookies expired or invalid. Please re-export from EditThisCookie.")
            show_cookie_warning_if_ui_running()
            return

        async with async_playwright() as playwright:
            key = get_user_data_dir_key()
            user_data_dir = Path(
                self.tiktok_config.get(key) or get_default_user_data_dir()
            ).expanduser()
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                executable_path=get_chrome_path(),
                headless=False,
                args=[
                    "--restore-last-session",
                    "--disable-session-crashed-bubble",
                    "--hide-crash-restore-bubble",
                ],
                ignore_default_args=["--enable-automation", "--no-first-run"],
            )
            context.set_default_timeout(int(self.tiktok_config.get("navigation_timeout_ms", 60000)))

            try:
                page = context.pages[0] if context.pages else await context.new_page()
                cookies_loaded = await self._load_cookies(context)
                await self._ensure_logged_in(page, cookies_loaded=cookies_loaded)

                for recipient in self.recipients:
                    await self._send_to_recipient(context, recipient)
                    await self._sleep_between_messages()
            finally:
                await context.close()

    async def _load_cookies(self, context: BrowserContext) -> bool:
        cookie_file = Path(self.config.get("cookie_file", "cookies.json"))
        if not cookie_file.exists():
            return False

        with cookie_file.open("r", encoding="utf-8") as file:
            cookies = json.load(file)

        if any("expirationDate" in cookie for cookie in cookies):
            same_site_map = {
                "unspecified": "None",
                "no_restriction": "None",
                "lax": "Lax",
                "strict": "Strict",
            }
            converted = []
            for cookie in cookies:
                if cookie.get("session", False):
                    continue
                converted.append(
                    {
                        "name": cookie["name"],
                        "value": cookie["value"],
                        "domain": cookie["domain"],
                        "path": cookie.get("path", "/"),
                        "expires": int(cookie["expirationDate"]),
                        "httpOnly": cookie.get("httpOnly", False),
                        "secure": cookie.get("secure", False),
                        "sameSite": same_site_map.get(
                            cookie.get("sameSite", "unspecified"),
                            "None",
                        ),
                    }
                )
            cookies = converted
            cookie_format = "EditThisCookie"
        elif any("expires" in cookie for cookie in cookies):
            cookie_format = "Playwright"
        else:
            cookie_format = "unknown"

        await context.add_cookies(cookies)
        logger.info(
            "Detected %s cookie format and loaded %d cookies from %s",
            cookie_format,
            len(cookies),
            cookie_file,
        )
        return True

    async def _ensure_logged_in(self, page: Page, cookies_loaded: bool = False) -> None:
        await page.goto("https://www.tiktok.com/messages", wait_until="domcontentloaded")
        await handle_screen_time_popup(page)
        await handle_sleep_hours_popup(page)
        await wait_for_captcha_if_present(page, self.notifier)
        await handle_screen_time_popup(page)
        await handle_sleep_hours_popup(page)

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
            await wait_for_captcha_if_present(page, self.notifier)
            await handle_screen_time_popup(page)
            await handle_sleep_hours_popup(page)

    async def _send_to_recipient(self, context: BrowserContext, recipient: dict[str, Any]) -> None:
        name = recipient.get("name", "recipient")
        username = recipient.get("username")
        if not username:
            logger.warning("Skipping %s because username is missing.", name)
            return

        videos = [
            video.strip()
            for video in self.config.get("videos", [])
            if isinstance(video, str) and video.strip()
        ]
        if not videos:
            raise ValueError("No videos configured. Add links to the 'videos' list in config.json.")
        video_link = random.choice(videos)
        logger.info("Selected video for %s (@%s): %s", name, username, video_link)
        page = await timed_await(f"new page for @{username}", context.new_page())
        try:
            total_start = time.time()
            logger.info("Resolving TikTok display name for %s (@%s)", name, username)
            await timed_await(f"select recipient for @{username}", self._select_recipient(page, username))
            await timed_await(
                f"post-select captcha wait for @{username}",
                wait_for_captcha_if_present(page, self.notifier),
            )
            await timed_await(
                f"post-select screen time popup check for @{username}",
                handle_screen_time_popup(page),
            )
            await timed_await(
                f"post-select sleep hours popup check for @{username}",
                handle_sleep_hours_popup(page),
            )
            send_start = time.time()
            await timed_await(f"message send call for @{username}", self._send_message(page, video_link))
            log_duration(f"message send for @{username}", send_start)
            await timed_await(
                f"post-send captcha wait for @{username}",
                wait_for_captcha_if_present(page, self.notifier),
            )
            await timed_await(
                f"post-send screen time popup check for @{username}",
                handle_screen_time_popup(page),
            )
            await timed_await(
                f"post-send sleep hours popup check for @{username}",
                handle_sleep_hours_popup(page),
            )
            logger.info("Sent video link to %s: %s", name, video_link)
            log_duration(f"total send flow for @{username}", total_start)
        finally:
            await timed_await(f"close page for @{username}", page.close())

    async def _select_recipient(self, page: Page, username: str) -> None:
        normalized_username = username.lstrip("@")
        profile_start = time.time()
        await timed_await(
            f"profile goto for @{normalized_username}",
            page.goto(f"https://www.tiktok.com/@{normalized_username}", wait_until="domcontentloaded"),
        )
        await timed_await(
            f"profile screen time popup check for @{normalized_username}",
            handle_screen_time_popup(page),
        )
        await timed_await(
            f"profile sleep hours popup check for @{normalized_username}",
            handle_sleep_hours_popup(page),
        )
        await timed_await(
            f"profile captcha wait for @{normalized_username}",
            wait_for_captcha_if_present(page, self.notifier),
        )
        await timed_await(
            f"profile post-captcha screen time popup check for @{normalized_username}",
            handle_screen_time_popup(page),
        )
        await timed_await(
            f"profile post-captcha sleep hours popup check for @{normalized_username}",
            handle_sleep_hours_popup(page),
        )
        log_duration(f"profile navigation for @{normalized_username}", profile_start)

        display_start = time.time()
        display_name_locator = page.locator("h1[data-e2e='user-title']").first
        if await timed_await(
            f"display name primary locator count for @{normalized_username}",
            display_name_locator.count(),
        ) == 0:
            display_name_locator = page.locator("[data-e2e='user-title']").first
        await timed_await(
            f"display name wait_for for @{normalized_username}",
            display_name_locator.wait_for(state="visible"),
        )
        display_name = (
            await timed_await(
                f"display name inner_text for @{normalized_username}",
                display_name_locator.inner_text(),
            )
        ).strip()
        log_duration(f"display name extraction for @{normalized_username}", display_start)

        logger.info(
            "Resolved TikTok username @%s to display name %s",
            normalized_username,
            display_name,
        )

        messages_start = time.time()
        await timed_await(
            f"messages goto for @{normalized_username}",
            page.goto("https://www.tiktok.com/messages", wait_until="domcontentloaded"),
        )
        await timed_await(
            f"messages screen time popup check for @{normalized_username}",
            handle_screen_time_popup(page),
        )
        await timed_await(
            f"messages sleep hours popup check for @{normalized_username}",
            handle_sleep_hours_popup(page),
        )
        await timed_await(
            f"messages captcha wait for @{normalized_username}",
            wait_for_captcha_if_present(page, self.notifier),
        )
        await timed_await(
            f"messages post-captcha screen time popup check for @{normalized_username}",
            handle_screen_time_popup(page),
        )
        await timed_await(
            f"messages post-captcha sleep hours popup check for @{normalized_username}",
            handle_sleep_hours_popup(page),
        )
        log_duration(f"messages navigation for @{normalized_username}", messages_start)

        conversation_list = page.locator("[data-e2e='dm-new-conversation-list']").first
        conversation_items = page.locator("[data-e2e='dm-new-conversation-item']")
        await timed_await(
            f"conversation items first wait_for for @{normalized_username}",
            conversation_items.first.wait_for(state="visible"),
        )

        normalized_display_name = display_name.casefold()
        loop_start = time.time()
        for scroll_index in range(10):
            scroll_iteration_start = time.time()
            item_count = await timed_await(
                f"conversation item count scroll {scroll_index + 1} for @{normalized_username}",
                conversation_items.count(),
            )
            for index in range(item_count):
                item = conversation_items.nth(index)
                nickname = item.locator("[data-e2e='dm-new-conversation-nickname']")
                nickname_text = (
                    await timed_await(
                        f"nickname inner_text item {index + 1} scroll {scroll_index + 1} for @{normalized_username}",
                        nickname.inner_text(),
                    )
                ).strip()
                normalized_nickname = nickname_text.casefold()

                if normalized_nickname == normalized_display_name:
                    await timed_await(
                        f"conversation item click for @{normalized_username}",
                        item.click(),
                    )
                    await timed_await(
                        f"conversation click screen time popup check for @{normalized_username}",
                        handle_screen_time_popup(page),
                    )
                    await timed_await(
                        f"conversation click sleep hours popup check for @{normalized_username}",
                        handle_sleep_hours_popup(page),
                    )
                    log_duration(f"conversation scroll/search loop for @{normalized_username}", loop_start)
                    editable_elements = await timed_await(
                        f"editable elements evaluate for @{normalized_username}",
                        page.evaluate(
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
                        ),
                    )
                    logger.debug("Editable elements after selecting %s: %s", username, editable_elements)

                    input_wait_start = time.time()
                    input_locator = page.locator(
                        ".public-DraftEditor-content, "
                        "div[contenteditable='true'][placeholder*='Send' i], "
                        "div[contenteditable='true'][placeholder*='message' i], "
                        "[contenteditable='true'][aria-label*='Send' i], "
                        "[contenteditable='true'][aria-label*='message' i]"
                    ).first
                    await timed_await(
                        f"chat input wait_for for @{normalized_username}",
                        input_locator.wait_for(state="visible", timeout=10000),
                    )
                    log_duration(f"chat input wait for @{normalized_username}", input_wait_start)
                    return

            await timed_await(
                f"conversation scroll evaluate iteration {scroll_index + 1} for @{normalized_username}",
                conversation_list.evaluate("(element) => element.scrollBy(0, 400)"),
            )
            await timed_await(
                f"conversation scroll wait iteration {scroll_index + 1} for @{normalized_username}",
                page.wait_for_timeout(500),
            )
            log_duration(
                f"conversation scroll iteration {scroll_index + 1} for @{normalized_username}",
                scroll_iteration_start,
            )

        log_duration(f"conversation scroll/search loop for @{normalized_username}", loop_start)
        raise ValueError(
            "Could not find TikTok conversation item for "
            f"@{normalized_username} using display name: {display_name}"
        )

    async def _send_message(self, page: Page, message: str) -> None:
        input_locator = page.locator(
            ".public-DraftEditor-content, "
            "div[contenteditable='true'][placeholder*='Send' i], "
            "div[contenteditable='true'][placeholder*='message' i], "
            "[contenteditable='true'][aria-label*='Send' i], "
            "[contenteditable='true'][aria-label*='message' i]"
        ).first

        await timed_await("message input wait_for", input_locator.wait_for(state="visible", timeout=10000))
        await timed_await("message input click", input_locator.click())
        await timed_await("message type", page.keyboard.type(message))
        await timed_await("Enter press", page.keyboard.press("Enter"))

    async def _sleep_between_messages(self) -> None:
        delay_range = self.tiktok_config.get("message_delay_seconds", [8, 18])
        if not isinstance(delay_range, list) or len(delay_range) != 2:
            delay_range = [8, 18]
        low, high = sorted(int(value) for value in delay_range)
        await asyncio.sleep(random.randint(low, high))
