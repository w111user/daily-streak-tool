from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from daily_streak import browser


class FakeContext:
    def __init__(self) -> None:
        self.cookies: list[dict[str, object]] | None = None

    async def add_cookies(self, cookies: list[dict[str, object]]) -> None:
        self.cookies = cookies


class FakePage:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        self.events.append("page")


class FakePersistentContext:
    def __init__(self, events: list[str] | None = None) -> None:
        self.default_timeout: int | None = None
        self.default_navigation_timeout: int | None = None
        self.routes: list[tuple[str, object]] = []
        self.closed = False
        self.cookies: list[dict[str, object]] | None = None
        self.events = events if events is not None else []
        self.pages: list[FakePage] = []

    def set_default_timeout(self, timeout: int) -> None:
        self.default_timeout = timeout

    def set_default_navigation_timeout(self, timeout: int) -> None:
        self.default_navigation_timeout = timeout

    async def route(self, pattern: str, handler: object) -> None:
        self.routes.append((pattern, handler))

    async def add_cookies(self, cookies: list[dict[str, object]]) -> None:
        self.cookies = cookies

    async def new_page(self) -> FakePage:
        page = FakePage(self.events)
        self.pages.append(page)
        return page

    async def close(self) -> None:
        self.closed = True
        self.events.append("context")


class FakeBrowser:
    def __init__(self, context: FakePersistentContext, events: list[str]) -> None:
        self.context = context
        self.events = events
        self.new_context_calls = 0
        self.closed = False

    async def new_context(self) -> FakePersistentContext:
        self.new_context_calls += 1
        return self.context

    async def close(self) -> None:
        self.closed = True
        self.events.append("browser")


class FakeChromium:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.context = FakePersistentContext(self.events)
        self.browser = FakeBrowser(self.context, self.events)
        self.persistent_launch_kwargs: dict[str, object] | None = None
        self.launch_kwargs: dict[str, object] | None = None

    async def launch_persistent_context(self, **kwargs: object) -> FakePersistentContext:
        self.persistent_launch_kwargs = kwargs
        return self.context

    async def launch(self, **kwargs: object) -> FakeBrowser:
        self.launch_kwargs = kwargs
        return self.browser


class FakePlaywright:
    def __init__(self) -> None:
        self.chromium = FakeChromium()


class BrowserTests(unittest.TestCase):
    def test_platform_profile_keys_and_defaults(self) -> None:
        home = Path("/temporary/home")
        self.assertEqual(browser.get_user_data_dir_key("Windows"), "user_data_dir_windows")
        self.assertEqual(browser.get_user_data_dir_key("Darwin"), "user_data_dir_macos")
        self.assertEqual(browser.get_user_data_dir_key("Linux"), "user_data_dir_linux")
        self.assertEqual(
            browser.get_default_user_data_dir("Linux", home),
            "/temporary/home/.config/chrome-debug",
        )
        self.assertEqual(
            browser.get_default_user_data_dir("Darwin", home),
            "/temporary/home/Library/Application Support/chrome-debug",
        )

    def test_configured_profile_and_headless_launch_options(self) -> None:
        original_discovery = browser.discover_browser_executable
        browser.discover_browser_executable = lambda preferred: "/browser/chrome"  # type: ignore[assignment]
        try:
            launch = browser.get_browser_launch_config(
                {
                    "tiktok": {
                        "headless": True,
                        "user_data_dir_linux": "/profiles/tiktok",
                        "navigation_timeout_ms": 1234,
                        "action_timeout_ms": 567,
                        "content_ready_timeout_ms": 3456,
                        "probe_timeout_ms": 800,
                        "browser_args": ["--window-size=1280,800"],
                    }
                }
            )
        finally:
            browser.discover_browser_executable = original_discovery  # type: ignore[assignment]

        self.assertEqual(launch.user_data_dir, Path("/profiles/tiktok"))
        self.assertTrue(launch.headless)
        self.assertEqual(launch.executable_path, "/browser/chrome")
        self.assertEqual(launch.navigation_timeout_ms, 1234)
        self.assertEqual(launch.action_timeout_ms, 567)
        self.assertEqual(launch.content_ready_timeout_ms, 3456)
        self.assertEqual(launch.probe_timeout_ms, 800)
        self.assertIn("--restore-last-session", launch.args)
        self.assertIn("--window-size=1280,800", launch.args)
        self.assertFalse(launch.low_memory_mode)
        self.assertNotIn("--disable-gpu", launch.args)

    def test_discovery_falls_back_to_chromium_after_google_chrome(self) -> None:
        found = browser.discover_browser_executable(
            system="Linux",
            which=lambda _: None,
            is_file=lambda candidate: str(candidate) == "/usr/bin/chromium",
        )
        self.assertEqual(found, "/usr/bin/chromium")

    def test_launch_uses_playwright_when_no_system_browser_is_found(self) -> None:
        original_discovery = browser.discover_browser_executable
        browser.discover_browser_executable = lambda preferred: None  # type: ignore[assignment]
        try:
            playwright = FakePlaywright()
            context = asyncio.run(
                browser.launch_persistent_context(
                    playwright,
                    {
                        "tiktok": {
                            "headless": False,
                            "navigation_timeout_ms": 2468,
                            "action_timeout_ms": 1357,
                        }
                    },
                )
            )
        finally:
            browser.discover_browser_executable = original_discovery  # type: ignore[assignment]

        self.assertIs(context, playwright.chromium.context)
        self.assertIsNotNone(playwright.chromium.persistent_launch_kwargs)
        self.assertFalse(playwright.chromium.persistent_launch_kwargs["headless"])
        self.assertNotIn("executable_path", playwright.chromium.persistent_launch_kwargs)
        self.assertEqual(context.default_navigation_timeout, 2468)
        self.assertEqual(context.default_timeout, 1357)
        self.assertEqual(context.routes, [])

    def test_legacy_config_uses_split_navigation_and_action_defaults(self) -> None:
        original_discovery = browser.discover_browser_executable
        browser.discover_browser_executable = lambda preferred: None  # type: ignore[assignment]
        try:
            launch = browser.get_browser_launch_config({"tiktok": {}})
        finally:
            browser.discover_browser_executable = original_discovery  # type: ignore[assignment]

        self.assertEqual(launch.navigation_timeout_ms, 60000)
        self.assertEqual(browser.get_navigation_timeout_ms({"tiktok": {}}), 60000)
        self.assertEqual(launch.action_timeout_ms, 10000)
        self.assertEqual(launch.content_ready_timeout_ms, 45000)
        self.assertEqual(browser.get_dm_readiness_timeout_ms({"tiktok": {}}), 45000)
        self.assertEqual(browser.get_dm_load_attempts({"tiktok": {}}), 2)
        self.assertEqual(launch.probe_timeout_ms, 750)
        self.assertFalse(launch.low_memory_mode)
        self.assertEqual(browser.get_low_memory_stabilization_seconds({"tiktok": {}}), 4.0)
        self.assertTrue(launch.persistent_context)

    def test_dm_readiness_budget_uses_legacy_content_timeout_or_new_override(self) -> None:
        self.assertEqual(
            browser.get_dm_readiness_timeout_ms(
                {"tiktok": {"content_ready_timeout_ms": 60_000}}
            ),
            60_000,
        )
        config = {
            "tiktok": {
                "content_ready_timeout_ms": 45_000,
                "dm_readiness_timeout_ms": 120_000,
                "dm_load_attempts": 1,
            }
        }
        self.assertEqual(browser.get_dm_readiness_timeout_ms(config), 120_000)
        self.assertEqual(browser.get_dm_load_attempts(config), 1)

    def test_invalid_dm_load_attempt_count_uses_the_default(self) -> None:
        with self.assertLogs("daily_streak.browser", level="WARNING"):
            attempts = browser.get_dm_load_attempts({"tiktok": {"dm_load_attempts": 0}})

        self.assertEqual(attempts, 2)

    def test_session_factory_keeps_persistent_context_as_the_default(self) -> None:
        original_discovery = browser.discover_browser_executable
        browser.discover_browser_executable = lambda preferred: None  # type: ignore[assignment]
        try:
            playwright = FakePlaywright()
            session = asyncio.run(
                browser.launch_browser_session(playwright, {"tiktok": {}})
            )
        finally:
            browser.discover_browser_executable = original_discovery  # type: ignore[assignment]

        self.assertTrue(session.persistent_context)
        self.assertIs(session.context, playwright.chromium.context)
        self.assertIsNone(session.browser)
        self.assertIsNotNone(playwright.chromium.persistent_launch_kwargs)
        self.assertIsNone(playwright.chromium.launch_kwargs)

    def test_persistent_profile_lock_files_only_emit_a_warning(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            profile_dir = Path(temporary_directory)
            (profile_dir / "SingletonLock").touch()
            playwright = FakePlaywright()
            with self.assertLogs("daily_streak.browser", level="WARNING") as logs:
                session = asyncio.run(
                    browser.launch_browser_session(
                        playwright,
                        {
                            "tiktok": {
                                "persistent_context": True,
                                "user_data_dir_linux": str(profile_dir),
                            }
                        },
                    )
                )
            asyncio.run(session.close())

        output = "\n".join(logs.output)
        self.assertIn("SingletonLock", output)
        self.assertIn("were not removed", output)

    def test_ephemeral_session_launches_without_a_user_data_directory(self) -> None:
        original_discovery = browser.discover_browser_executable
        browser.discover_browser_executable = lambda preferred: "/browser/chromium"  # type: ignore[assignment]
        try:
            config = {
                "tiktok": {
                    "headless": True,
                    "low_memory_mode": True,
                    "persistent_context": False,
                }
            }
            launch_config = browser.get_browser_launch_config(config)
            playwright = FakePlaywright()
            session = asyncio.run(browser.launch_browser_session(playwright, config))
        finally:
            browser.discover_browser_executable = original_discovery  # type: ignore[assignment]

        self.assertFalse(launch_config.persistent_context)
        self.assertIsNone(launch_config.user_data_dir)
        self.assertFalse(session.persistent_context)
        self.assertIs(session.browser, playwright.chromium.browser)
        self.assertIsNone(playwright.chromium.persistent_launch_kwargs)
        self.assertIsNotNone(playwright.chromium.launch_kwargs)
        self.assertNotIn("user_data_dir", playwright.chromium.launch_kwargs)
        self.assertEqual(playwright.chromium.launch_kwargs["executable_path"], "/browser/chromium")
        self.assertTrue(
            set(browser.LOW_MEMORY_BROWSER_ARGS).issubset(
                set(playwright.chromium.launch_kwargs["args"])
            )
        )
        self.assertEqual(playwright.chromium.browser.new_context_calls, 1)
        self.assertEqual(session.context.default_navigation_timeout, 60_000)
        self.assertEqual(session.context.default_timeout, 10_000)
        self.assertEqual(len(session.context.routes), 1)

        class Request:
            resource_type = "image"

        class Route:
            request = Request()

            def __init__(self) -> None:
                self.action = ""

            async def abort(self) -> None:
                self.action = "abort"

            async def continue_(self) -> None:
                self.action = "continue"

        route = Route()
        handler = session.context.routes[0][1]
        asyncio.run(handler(route))  # type: ignore[misc]
        self.assertEqual(route.action, "abort")

    def test_browser_session_loads_cookies_and_closes_ephemeral_resources_in_order(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            cookie_path = Path(temporary_directory) / "cookies.json"
            cookie_path.write_text(
                json.dumps(
                    [
                        {
                            "name": "sessionid",
                            "value": "value",
                            "domain": ".tiktok.com",
                            "expires": time.time() + 300,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            playwright = FakePlaywright()
            session = asyncio.run(
                browser.launch_browser_session(
                    playwright,
                    {"tiktok": {"persistent_context": False}},
                )
            )
            loaded = asyncio.run(session.load_cookies(cookie_path))
            page = asyncio.run(session.get_page())
            asyncio.run(session.close())

        self.assertTrue(loaded)
        self.assertIsNotNone(session.context.cookies)
        self.assertTrue(page.closed)
        self.assertTrue(session.context.closed)
        self.assertTrue(playwright.chromium.browser.closed)
        self.assertEqual(playwright.chromium.events, ["page", "context", "browser"])

    def test_browser_session_loads_cookies_for_persistent_and_ephemeral_modes(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            cookie_path = Path(temporary_directory) / "cookies.json"
            cookie_path.write_text(
                json.dumps(
                    [
                        {
                            "name": "sessionid",
                            "value": "value",
                            "domain": ".tiktok.com",
                            "expires": time.time() + 300,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            for persistent_context in (True, False):
                playwright = FakePlaywright()
                session = asyncio.run(
                    browser.launch_browser_session(
                        playwright,
                        {"tiktok": {"persistent_context": persistent_context}},
                    )
                )
                loaded = asyncio.run(session.load_cookies(cookie_path))
                asyncio.run(session.close())

                self.assertTrue(loaded)
                self.assertIsNotNone(session.context.cookies)
                self.assertEqual(session.persistent_context, persistent_context)

    def test_low_memory_mode_uses_conservative_flags_and_resource_filter(self) -> None:
        original_discovery = browser.discover_browser_executable
        browser.discover_browser_executable = lambda preferred: None  # type: ignore[assignment]
        try:
            config = {"tiktok": {"low_memory_mode": True}}
            launch = browser.get_browser_launch_config(config)
            playwright = FakePlaywright()
            context = asyncio.run(browser.launch_persistent_context(playwright, config))
        finally:
            browser.discover_browser_executable = original_discovery  # type: ignore[assignment]

        self.assertTrue(launch.low_memory_mode)
        self.assertTrue(browser.get_low_memory_mode(config))
        self.assertTrue(
            set(browser.LOW_MEMORY_BROWSER_ARGS).issubset(set(launch.args))
        )
        self.assertEqual(len(context.routes), 1)
        self.assertEqual(context.routes[0][0], "**/*")

        class Request:
            def __init__(self, resource_type: str) -> None:
                self.resource_type = resource_type

        class Route:
            def __init__(self, resource_type: str) -> None:
                self.request = Request(resource_type)
                self.action = ""

            async def abort(self) -> None:
                self.action = "abort"

            async def continue_(self) -> None:
                self.action = "continue"

        handler = context.routes[0][1]
        for resource_type in ("image", "media", "font"):
            route = Route(resource_type)
            asyncio.run(handler(route))  # type: ignore[misc]
            self.assertEqual(route.action, "abort")
        for resource_type in ("script", "xhr", "fetch", "stylesheet"):
            route = Route(resource_type)
            asyncio.run(handler(route))  # type: ignore[misc]
            self.assertEqual(route.action, "continue")

    def test_edit_this_cookie_conversion_skips_session_cookies(self) -> None:
        cookies, cookie_format = browser.convert_cookies(
            [
                {
                    "name": "sessionid",
                    "value": "persistent",
                    "domain": ".tiktok.com",
                    "expirationDate": 2000000000.3,
                    "sameSite": "lax",
                },
                {
                    "name": "temporary",
                    "value": "skip",
                    "domain": ".tiktok.com",
                    "session": True,
                    "expirationDate": 2000000000,
                },
            ]
        )

        self.assertEqual(cookie_format, "EditThisCookie")
        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookies[0]["expires"], 2000000000)
        self.assertEqual(cookies[0]["sameSite"], "Lax")

    def test_cookie_validity_and_async_loading(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "cookies.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "name": "sessionid",
                            "value": "value",
                            "domain": ".tiktok.com",
                            "expirationDate": time.time() + 300,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            context = FakeContext()
            loaded = asyncio.run(browser.load_cookies(context, path))
            valid = browser.check_cookies_valid(path)

        self.assertTrue(loaded)
        self.assertTrue(valid)
        self.assertIsNotNone(context.cookies)
