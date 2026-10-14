from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from daily_streak.navigation import TIKTOK_MESSAGES_URL, navigate_to_messages
from daily_streak.recipients import (
    CONVERSATION_ITEM_SELECTOR,
    CONVERSATION_LIST_SELECTOR,
    DOM_EXECUTION_CONTEXT_SCRIPT,
    EMPTY_CONVERSATION_SELECTOR,
    RecipientService,
)


class TikTokNavigationTests(unittest.TestCase):
    def test_messages_navigation_uses_commit_not_domcontentloaded(self) -> None:
        class Page:
            url = TIKTOK_MESSAGES_URL

            def __init__(self) -> None:
                self.goto_calls: list[tuple[str, str, int]] = []

            async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
                self.goto_calls.append((url, wait_until, timeout))

        page = Page()
        asyncio.run(navigate_to_messages(page, navigation_timeout_ms=60_000))

        self.assertEqual(page.goto_calls, [(TIKTOK_MESSAGES_URL, "commit", 60_000)])

    def test_scan_invokes_readiness_after_messages_navigation_commit(self) -> None:
        events: list[str] = []

        class Page:
            url = TIKTOK_MESSAGES_URL

            async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
                events.append(f"goto:{wait_until}")

        class Service(RecipientService):
            async def wait_for_dm_readiness(self, page: object) -> tuple[object, object, bool]:
                events.append("readiness")
                return object(), object(), True

        service = Service()
        with patch("daily_streak.recipients.handle_screen_time_popup", new=AsyncMock()), patch(
            "daily_streak.recipients.handle_sleep_hours_popup", new=AsyncMock()
        ):
            recipients = asyncio.run(service.scan_dm_list(Page()))

        self.assertEqual(recipients, [])
        self.assertEqual(events, ["goto:commit", "readiness"])

    def test_slow_dm_dom_readiness_succeeds_after_fast_navigation_commit(self) -> None:
        class Locator:
            first = None

            def __init__(self) -> None:
                self.first = self

        class Page:
            url = TIKTOK_MESSAGES_URL

            def __init__(self) -> None:
                self.goto_calls: list[tuple[str, str, int]] = []
                self.evaluate_calls = 0
                self.conversation_list = Locator()
                self.conversation_items = Locator()
                self.empty_state = Locator()

            async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
                self.goto_calls.append((url, wait_until, timeout))

            async def evaluate(self, script: str) -> object:
                self.evaluate_calls += 1
                await asyncio.sleep(0.01)
                if script == DOM_EXECUTION_CONTEXT_SCRIPT:
                    return "loading"
                return {
                    "dmList": 1,
                    "dmItems": 0,
                    "emptyState": 0,
                    "login": 0,
                    "captcha": 0,
                    "screenTime": False,
                    "sleepHours": False,
                }

            def locator(self, selector: str) -> Locator:
                if selector == CONVERSATION_LIST_SELECTOR:
                    return self.conversation_list
                if selector == CONVERSATION_ITEM_SELECTOR:
                    return self.conversation_items
                if selector == EMPTY_CONVERSATION_SELECTOR:
                    return self.empty_state
                return Locator()

        async def navigate_then_wait() -> tuple[object, object, bool]:
            await navigate_to_messages(page, navigation_timeout_ms=60_000)
            return await service.wait_for_dm_readiness(page)

        page = Page()
        service = RecipientService(
            action_timeout_ms=10_000,
            content_ready_timeout_ms=45_000,
            probe_timeout_ms=500,
        )
        with patch("daily_streak.recipients.handle_screen_time_popup", new=AsyncMock()), patch(
            "daily_streak.recipients.handle_sleep_hours_popup", new=AsyncMock()
        ):
            _, _, is_empty = asyncio.run(navigate_then_wait())

        self.assertFalse(is_empty)
        self.assertEqual(page.goto_calls, [(TIKTOK_MESSAGES_URL, "commit", 60_000)])
        self.assertEqual(page.evaluate_calls, 2)

    def test_commit_can_be_followed_by_delayed_execution_context_availability(self) -> None:
        class Locator:
            first = None

            def __init__(self) -> None:
                self.first = self

        class Page:
            url = TIKTOK_MESSAGES_URL

            def __init__(self) -> None:
                self.goto_calls: list[tuple[str, str, int]] = []
                self.execution_context_checks = 0
                self.snapshot_checks = 0

            async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
                self.goto_calls.append((url, wait_until, timeout))

            async def wait_for_load_state(self, state: str, *, timeout: int) -> None:
                await asyncio.Future()

            async def evaluate(self, script: str) -> object:
                if script == DOM_EXECUTION_CONTEXT_SCRIPT:
                    self.execution_context_checks += 1
                    if self.execution_context_checks == 1:
                        raise RuntimeError("Execution context is not available yet")
                    return "loading"
                self.snapshot_checks += 1
                return {
                    "dmList": 0,
                    "dmItems": 1,
                    "emptyState": 0,
                    "login": 0,
                    "captcha": 0,
                    "screenTime": False,
                    "sleepHours": False,
                }

            def locator(self, selector: str) -> Locator:
                return Locator()

        async def navigate_then_wait() -> tuple[object, object, bool]:
            await navigate_to_messages(page, navigation_timeout_ms=60_000)
            return await service.wait_for_dm_readiness(page)

        async def yield_to_soft_stage(_: float) -> None:
            await asyncio.sleep(0)

        page = Page()
        service = RecipientService(action_timeout_ms=10_000, probe_timeout_ms=500)
        with patch.object(
            service, "_sleep_until_next_soft_dom_probe", new=yield_to_soft_stage
        ):
            _, _, is_empty = asyncio.run(navigate_then_wait())

        self.assertFalse(is_empty)
        self.assertEqual(page.goto_calls, [(TIKTOK_MESSAGES_URL, "commit", 60_000)])
        self.assertEqual(page.execution_context_checks, 2)
        self.assertEqual(page.snapshot_checks, 1)

    def test_precommit_navigation_timeout_propagates(self) -> None:
        class Page:
            url = "about:blank"

            async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
                raise PlaywrightTimeoutError("navigation did not commit")

        with self.assertRaisesRegex(PlaywrightTimeoutError, "did not commit"):
            asyncio.run(navigate_to_messages(Page(), navigation_timeout_ms=60_000))

    def test_timeout_with_active_tiktok_document_can_continue_to_readiness(self) -> None:
        class Page:
            url = TIKTOK_MESSAGES_URL

            async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
                raise PlaywrightTimeoutError("late navigation timeout")

        with self.assertLogs("daily_streak.navigation", level="WARNING") as logs:
            asyncio.run(navigate_to_messages(Page(), navigation_timeout_ms=60_000))

        self.assertIn("continuing to explicit readiness", "\n".join(logs.output))
