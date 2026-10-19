from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from daily_streak.recipients import (
    AmbiguousConversationError,
    CHAT_USERNAME_SELECTOR,
    CONVERSATION_ITEM_SELECTOR,
    CONVERSATION_LIST_SELECTOR,
    DOM_EXECUTION_CONTEXT_SCRIPT,
    EMPTY_CONVERSATION_SELECTOR,
    READINESS_SNAPSHOT_SCRIPT,
    RecipientService,
    TikTokChallengeRequiredError,
    TikTokDMReadinessError,
    TikTokLoginRequiredError,
    TikTokRendererUnresponsiveError,
    _ReadinessProbeState,
    username_from_href,
)


def readiness_snapshot(**overrides: object) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "dmList": 0,
        "dmItems": 0,
        "emptyState": 0,
        "login": 0,
        "captcha": 0,
        "screenTime": False,
        "sleepHours": False,
    }
    snapshot.update(overrides)
    return snapshot


class SnapshotLocator:
    """A locator that fails the test if passive readiness tries to use it."""

    def __init__(self) -> None:
        self.first = self

    async def is_visible(self, *, timeout: int) -> bool:
        raise AssertionError("readiness must use page.evaluate instead of locator.is_visible")

    async def wait_for(self, *, state: str, timeout: int) -> None:
        raise AssertionError("readiness must not issue locator waits")


class SnapshotPage:
    url = "https://www.tiktok.com/messages"

    def __init__(
        self,
        snapshots: list[object],
        *,
        execution_contexts: list[object] | None = None,
        domcontentloaded_error: Exception | None = None,
    ) -> None:
        self.snapshots = list(snapshots)
        self.execution_contexts = list(execution_contexts or ["loading"])
        self.domcontentloaded_error = domcontentloaded_error
        self.evaluate_calls: list[str] = []
        self.locator_calls: list[str] = []

    async def evaluate(self, script: str) -> object:
        self.evaluate_calls.append(script)
        if script == DOM_EXECUTION_CONTEXT_SCRIPT:
            if not self.execution_contexts:
                return "loading"
            outcome = self.execution_contexts.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        if "slice(0, 500)" in script:
            return "TikTok messages access_token=must-not-log are still loading"
        if not self.snapshots:
            return readiness_snapshot()
        outcome = self.snapshots.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def wait_for_load_state(self, state: str, *, timeout: int) -> None:
        if self.domcontentloaded_error is not None:
            raise self.domcontentloaded_error
        await asyncio.Future()

    async def title(self) -> str:
        return "TikTok Messages"

    def locator(self, selector: str) -> SnapshotLocator:
        self.locator_calls.append(selector)
        return SnapshotLocator()


class ConversationNickname:
    def __init__(self, value: str) -> None:
        self.value = value

    async def inner_text(self, *, timeout: int) -> str:
        return self.value


class ConversationItem:
    def __init__(
        self,
        display_name: str,
        *,
        aria_selected: str | None = None,
        click_error: Exception | None = None,
    ) -> None:
        self.display_name = display_name
        self.aria_selected = aria_selected
        self.click_error = click_error
        self.clicked = False
        self.click_timeouts: list[int] = []
        self.scroll_timeouts: list[int] = []
        self.dom_clicks = 0

    def locator(self, selector: str) -> ConversationNickname:
        if selector != "[data-e2e='dm-new-conversation-nickname']":
            raise AssertionError(f"Unexpected conversation-item selector: {selector}")
        return ConversationNickname(self.display_name)

    async def click(self, *, timeout: int) -> None:
        self.click_timeouts.append(timeout)
        if self.click_error is not None:
            raise self.click_error
        self.clicked = True

    async def count(self) -> int:
        return 1

    async def bounding_box(self, *, timeout: int) -> dict[str, int]:
        return {"x": 10, "y": 20, "width": 100, "height": 40}

    async def is_visible(self, *, timeout: int) -> bool:
        return True

    async def get_attribute(self, name: str, *, timeout: int) -> str | None:
        if name != "aria-selected":
            raise AssertionError(f"Unexpected attribute: {name}")
        return self.aria_selected

    async def scroll_into_view_if_needed(self, *, timeout: int) -> None:
        self.scroll_timeouts.append(timeout)

    async def evaluate(self, script: str, *, timeout: int) -> None:
        if script != "(element) => element.click()":
            raise AssertionError(f"Unexpected row evaluation: {script}")
        self.dom_clicks += 1
        self.clicked = True
        self.aria_selected = "true"


class ConversationItems:
    def __init__(self, *items: ConversationItem) -> None:
        self.items = list(items)

    async def count(self) -> int:
        return len(self.items)

    def nth(self, index: int) -> ConversationItem:
        return self.items[index]


class ConversationList:
    first = None

    def __init__(self) -> None:
        self.first = self
        self.scrolls: list[tuple[str, int]] = []
        self.wait_timeouts: list[int] = []

    async def evaluate(self, script: str, *, timeout: int) -> None:
        self.scrolls.append((script, timeout))

    async def wait_for(self, *, state: str, timeout: int) -> None:
        self.wait_timeouts.append(timeout)


class ChatUsername:
    first = None

    def __init__(self, value: str) -> None:
        self.first = self
        self.value = value
        self.wait_timeouts: list[int] = []

    async def wait_for(self, *, state: str, timeout: int) -> None:
        self.wait_timeouts.append(timeout)

    async def inner_text(self, *, timeout: int) -> str:
        return self.value


class ConversationPage:
    url = "https://www.tiktok.com/messages"

    def __init__(self, username: str) -> None:
        self.chat_username = ChatUsername(username)
        self.waits: list[int] = []

    def locator(self, selector: str) -> ChatUsername:
        if selector != CHAT_USERNAME_SELECTOR:
            raise AssertionError(f"Unexpected page selector: {selector}")
        return self.chat_username

    async def wait_for_timeout(self, timeout: int) -> None:
        self.waits.append(timeout)


class ResolveRecoveryContext:
    def __init__(self) -> None:
        self.created_pages: list[ResolveRecoveryPage] = []

    async def new_page(self) -> "ResolveRecoveryPage":
        page = ResolveRecoveryPage(self, f"fresh-{len(self.created_pages) + 1}")
        self.created_pages.append(page)
        return page


class ResolveRecoveryPage:
    def __init__(self, context: ResolveRecoveryContext, label: str = "initial") -> None:
        self.context = context
        self.label = label
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class SenderSelectionPage:
    async def evaluate(self, script: str) -> list[object]:
        return []


class ProfileTitleLocator:
    first = None

    def __init__(self, display_name: str) -> None:
        self.first = self
        self.display_name = display_name
        self.wait_timeouts: list[int] = []

    async def count(self) -> int:
        return 1

    async def wait_for(self, *, state: str, timeout: int) -> None:
        self.wait_timeouts.append(timeout)

    async def inner_text(self, *, timeout: int) -> str:
        return self.display_name


class ProfileSelectionPage:
    def __init__(self, display_name: str) -> None:
        self.title_locator = ProfileTitleLocator(display_name)

    def locator(self, selector: str) -> ProfileTitleLocator:
        if selector != "h1[data-e2e='user-title']":
            raise AssertionError(f"Unexpected profile selector: {selector}")
        return self.title_locator


class RecipientUtilityTests(unittest.TestCase):
    def test_username_from_relative_and_absolute_profile_links(self) -> None:
        self.assertEqual(username_from_href("/@alice"), "alice")
        self.assertEqual(
            username_from_href("https://www.tiktok.com/@alice/video/123?lang=en"),
            "alice",
        )

    def test_username_from_href_rejects_non_profile_urls(self) -> None:
        self.assertEqual(username_from_href(None), "")
        self.assertEqual(username_from_href("https://www.tiktok.com/messages"), "")

    def test_message_input_wait_uses_action_timeout_not_navigation_timeout(self) -> None:
        class Locator:
            first = None

            def __init__(self) -> None:
                self.first = self
                self.timeouts: list[int] = []

            async def wait_for(self, *, state: str, timeout: int) -> None:
                self.timeouts.append(timeout)

        class Page:
            def __init__(self) -> None:
                self.input = Locator()

            def locator(self, selector: str) -> Locator:
                return self.input

        page = Page()
        service = RecipientService(action_timeout_ms=10_000)
        asyncio.run(service.wait_for_message_input(page))

        self.assertEqual(page.input.timeouts, [10_000])

    def test_readiness_uses_one_dom_snapshot_per_polling_iteration(self) -> None:
        page = SnapshotPage(
            [
                readiness_snapshot(),
                readiness_snapshot(dmList=1),
            ]
        )
        service = RecipientService(content_ready_timeout_ms=45_000, probe_timeout_ms=500)
        with patch.object(service, "_sleep_until_next_readiness_probe", new=AsyncMock()):
            _, _, is_empty = asyncio.run(service._wait_for_dm_readiness(page))

        self.assertFalse(is_empty)
        self.assertEqual(len(page.evaluate_calls), 2)
        self.assertEqual(
            page.locator_calls,
            [CONVERSATION_LIST_SELECTOR, CONVERSATION_ITEM_SELECTOR],
        )

    def test_missing_selectors_do_not_trigger_sequential_locator_probe_delays(self) -> None:
        page = SnapshotPage([readiness_snapshot(dmItems=1)])
        service = RecipientService(content_ready_timeout_ms=45_000, probe_timeout_ms=500)
        _, _, is_empty = asyncio.run(service._wait_for_dm_readiness(page))

        self.assertFalse(is_empty)
        self.assertEqual(len(page.evaluate_calls), 1)
        self.assertEqual(
            page.locator_calls,
            [CONVERSATION_LIST_SELECTOR, CONVERSATION_ITEM_SELECTOR],
        )

    def test_transient_renderer_evaluation_failure_can_recover(self) -> None:
        page = SnapshotPage(
            [
                RuntimeError("Execution context was destroyed while probing"),
                readiness_snapshot(dmItems=1),
            ]
        )
        service = RecipientService(content_ready_timeout_ms=45_000, probe_timeout_ms=500)
        with patch.object(service, "_sleep_until_next_readiness_probe", new=AsyncMock()), self.assertLogs(
            "daily_streak.recipients", level="WARNING"
        ) as logs:
            _, _, is_empty = asyncio.run(service._wait_for_dm_readiness(page))

        self.assertFalse(is_empty)
        self.assertEqual(len(page.evaluate_calls), 2)
        output = "\n".join(logs.output)
        self.assertIn("DM readiness probe temporarily unavailable", output)
        self.assertNotIn("Traceback", output)

    def test_existing_dm_item_snapshot_still_reaches_readiness(self) -> None:
        page = SnapshotPage([readiness_snapshot(dmItems=1)])
        service = RecipientService(content_ready_timeout_ms=45_000, probe_timeout_ms=500)
        _, conversation_items, is_empty = asyncio.run(service._wait_for_dm_readiness(page))

        self.assertFalse(is_empty)
        self.assertIsInstance(conversation_items, SnapshotLocator)

    def test_repeated_renderer_probe_failures_report_a_distinct_diagnostic(self) -> None:
        page = SnapshotPage(
            [PlaywrightTimeoutError("renderer busy") for _ in range(3)]
        )
        monotonic_values = iter((0.0, 0.0, 1.0, 2.0, 11.0, 12.0, 13.0))
        service = RecipientService(
            content_ready_timeout_ms=10_000,
            probe_timeout_ms=500,
            monotonic=lambda: next(monotonic_values),
        )
        with patch.object(
            service, "_sleep_until_next_readiness_probe", new=AsyncMock()
        ), self.assertLogs("daily_streak.recipients", level="WARNING") as logs:
            with self.assertRaisesRegex(
                TikTokRendererUnresponsiveError,
                "renderer did not respond to readiness probes",
            ):
                asyncio.run(service._wait_for_dm_readiness(page))

        output = "\n".join(logs.output)
        self.assertIn("TikTok renderer did not respond to readiness probes", output)
        self.assertIn("failed_probes=", output)

    def test_readiness_timeout_logs_last_successful_snapshot_and_redacts_text(self) -> None:
        page = SnapshotPage([])
        state = _ReadinessProbeState(
            successful_probes=3,
            failed_probes=2,
            last_snapshot={
                "dm_list": 0,
                "dm_items": 0,
                "empty_state": 0,
                "login": 0,
                "captcha": 0,
                "screen_time": False,
                "sleep_hours": False,
            },
        )
        service = RecipientService(probe_timeout_ms=500)
        with self.assertLogs("daily_streak.recipients", level="WARNING") as logs:
            asyncio.run(service._log_dm_readiness_timeout(page, state))

        output = "\n".join(logs.output)
        self.assertIn("url=https://www.tiktok.com/messages", output)
        self.assertNotIn("access_token=must-not-log", output)
        self.assertIn("access_token=<redacted>", output)
        self.assertIn("successful_probes=3", output)
        self.assertIn("failed_probes=2", output)
        self.assertIn("renderer_unresponsive_seconds=0.0", output)
        self.assertIn("'dm_items': 0", output)

    def test_soft_dom_stage_recovers_after_delayed_execution_context(self) -> None:
        page = SnapshotPage(
            [readiness_snapshot(dmItems=1)],
            execution_contexts=[
                RuntimeError("Execution context is not available yet"),
                "loading",
            ],
        )
        service = RecipientService(action_timeout_ms=10_000, probe_timeout_ms=500)
        async def yield_to_load_state_waiter(_: float) -> None:
            await asyncio.sleep(0)

        with patch.object(
            service,
            "_sleep_until_next_soft_dom_probe",
            new=yield_to_load_state_waiter,
        ), self.assertLogs("daily_streak.recipients", level="INFO") as logs:
            _, _, is_empty = asyncio.run(service.wait_for_dm_readiness(page))

        self.assertFalse(is_empty)
        self.assertEqual(page.evaluate_calls.count(DOM_EXECUTION_CONTEXT_SCRIPT), 2)
        self.assertEqual(page.evaluate_calls.count(READINESS_SNAPSHOT_SCRIPT), 1)
        self.assertIn(
            "TikTok DOM execution context not available yet",
            "\n".join(logs.output),
        )

    def test_soft_dom_wait_timeout_still_proceeds_to_explicit_readiness(self) -> None:
        page = SnapshotPage(
            [readiness_snapshot(dmList=1)],
            execution_contexts=[RuntimeError("Execution context is not available yet")],
            domcontentloaded_error=PlaywrightTimeoutError("domcontentloaded delayed"),
        )
        service = RecipientService(action_timeout_ms=10_000, probe_timeout_ms=500)
        async def yield_to_load_state_waiter(_: float) -> None:
            await asyncio.sleep(0)

        with patch.object(
            service,
            "_sleep_until_next_soft_dom_probe",
            new=yield_to_load_state_waiter,
        ), self.assertLogs("daily_streak.recipients", level="INFO") as logs:
            _, _, is_empty = asyncio.run(service.wait_for_dm_readiness(page))

        self.assertFalse(is_empty)
        self.assertEqual(page.evaluate_calls.count(READINESS_SNAPSHOT_SCRIPT), 1)
        self.assertIn(
            "soft wait timed out; continuing to explicit DM readiness",
            "\n".join(logs.output),
        )

    def test_low_memory_loading_context_stabilizes_once_before_dm_readiness(self) -> None:
        page = SnapshotPage(
            [readiness_snapshot(dmItems=1)],
            execution_contexts=["loading", "interactive"],
        )
        service = RecipientService(
            low_memory_mode=True,
            low_memory_stabilization_seconds=4.0,
            probe_timeout_ms=500,
        )
        sleep_durations: list[float] = []

        async def stabilization_sleep(seconds: float) -> None:
            sleep_durations.append(seconds)
            self.assertEqual(page.evaluate_calls.count(DOM_EXECUTION_CONTEXT_SCRIPT), 1)
            self.assertEqual(page.evaluate_calls.count(READINESS_SNAPSHOT_SCRIPT), 0)

        with patch(
            "daily_streak.recipients.asyncio.sleep",
            new=stabilization_sleep,
        ), self.assertLogs("daily_streak.recipients", level="INFO") as logs:
            _, _, is_empty = asyncio.run(service.wait_for_dm_readiness(page))

        self.assertFalse(is_empty)
        self.assertEqual(sleep_durations, [4.0])
        self.assertEqual(page.evaluate_calls.count(DOM_EXECUTION_CONTEXT_SCRIPT), 2)
        self.assertEqual(page.evaluate_calls.count(READINESS_SNAPSHOT_SCRIPT), 1)
        output = "\n".join(logs.output)
        self.assertIn("DOM execution context available (readyState=loading)", output)
        self.assertIn("Low-memory stabilization wait: 4.0s", output)
        self.assertIn("DOM state after stabilization: interactive", output)

    def test_normal_mode_does_not_stabilize_a_loading_context(self) -> None:
        page = SnapshotPage(
            [readiness_snapshot(dmItems=1)],
            execution_contexts=["loading"],
        )
        service = RecipientService(
            low_memory_mode=False,
            low_memory_stabilization_seconds=4.0,
            probe_timeout_ms=500,
        )
        sleep = AsyncMock()

        with patch("daily_streak.recipients.asyncio.sleep", new=sleep):
            _, _, is_empty = asyncio.run(service.wait_for_dm_readiness(page))

        self.assertFalse(is_empty)
        sleep.assert_not_awaited()
        self.assertEqual(page.evaluate_calls.count(DOM_EXECUTION_CONTEXT_SCRIPT), 1)
        self.assertEqual(page.evaluate_calls.count(READINESS_SNAPSHOT_SCRIPT), 1)

    def test_low_memory_readiness_window_starts_after_stabilization(self) -> None:
        class Clock:
            value = 0.0

            def __call__(self) -> float:
                return self.value

        clock = Clock()
        page = SnapshotPage(
            [readiness_snapshot()],
            execution_contexts=["loading", "interactive"],
        )
        service = RecipientService(
            content_ready_timeout_ms=45_000,
            low_memory_mode=True,
            low_memory_stabilization_seconds=4.0,
            probe_timeout_ms=500,
            monotonic=clock,
        )
        readiness_deadlines: list[float] = []

        async def stabilization_sleep(seconds: float) -> None:
            clock.value += seconds

        async def advance_to_readiness_deadline(deadline: float) -> None:
            readiness_deadlines.append(deadline)
            clock.value = deadline

        with patch(
            "daily_streak.recipients.asyncio.sleep",
            new=stabilization_sleep,
        ), patch.object(
            service,
            "_sleep_until_next_readiness_probe",
            new=advance_to_readiness_deadline,
        ), self.assertLogs("daily_streak.recipients", level="WARNING"):
            with self.assertRaises(TikTokDMReadinessError):
                asyncio.run(service.wait_for_dm_readiness(page))

        self.assertEqual(service.content_ready_timeout_ms, 45_000)
        self.assertEqual(readiness_deadlines, [49.0])

    def test_configured_dm_readiness_budget_controls_the_full_polling_deadline(self) -> None:
        class Clock:
            value = 0.0

            def __call__(self) -> float:
                return self.value

        clock = Clock()
        page = SnapshotPage([readiness_snapshot()])
        service = RecipientService(
            content_ready_timeout_ms=45_000,
            dm_readiness_timeout_ms=120_000,
            probe_timeout_ms=500,
            monotonic=clock,
        )
        deadlines: list[float] = []

        async def advance_to_deadline(deadline: float) -> None:
            deadlines.append(deadline)
            clock.value = deadline

        with patch.object(
            service,
            "_sleep_until_next_readiness_probe",
            new=advance_to_deadline,
        ), self.assertLogs("daily_streak.recipients", level="WARNING"):
            with self.assertRaises(TikTokDMReadinessError):
                asyncio.run(service._wait_for_dm_readiness(page))

        self.assertEqual(deadlines, [120.0])

    def test_resolve_uses_dm_click_and_chat_uniqueid_without_profile_navigation(self) -> None:
        page = ConversationPage("@khoa_memezlol")
        conversation_list = ConversationList()
        item = ConversationItem("Đức Khoa")
        conversation_items = ConversationItems(item)
        service = RecipientService()
        service.wait_for_dm_readiness = AsyncMock(
            return_value=(conversation_list, conversation_items, False)
        )

        with patch(
            "daily_streak.recipients.navigate_to_messages",
            new=AsyncMock(),
        ) as navigate_messages, patch(
            "daily_streak.recipients.navigate_to_profile",
            new=AsyncMock(),
        ) as navigate_profile, patch(
            "daily_streak.recipients.handle_screen_time_popup",
            new=AsyncMock(),
        ), patch.object(
            service,
            "_exact_conversation_row_locator",
            return_value=item,
        ):
            resolved = asyncio.run(
                service.resolve_usernames(
                    page,
                    [{"name": "Đức Khoa", "username": ""}],
                )
            )

        self.assertEqual(resolved, [{"name": "Đức Khoa", "username": "khoa_memezlol"}])
        self.assertTrue(item.clicked)
        self.assertEqual(item.scroll_timeouts, [3_000])
        self.assertEqual(item.click_timeouts, [3_000])
        self.assertEqual(
            page.chat_username.wait_timeouts,
            [service.action_timeout_ms, service.action_timeout_ms],
        )
        navigate_messages.assert_awaited_once_with(
            page,
            navigation_timeout_ms=service.navigation_timeout_ms,
        )
        navigate_profile.assert_not_awaited()

    def test_scan_renderer_unresponsive_recreates_one_fresh_page_then_succeeds(self) -> None:
        context = ResolveRecoveryContext()
        initial_page = ResolveRecoveryPage(context)
        service = RecipientService()
        service.wait_for_dm_readiness = AsyncMock(
            side_effect=[
                TikTokRendererUnresponsiveError("renderer stopped responding"),
                (object(), object(), True),
            ]
        )

        with patch(
            "daily_streak.recipients.navigate_to_messages",
            new=AsyncMock(),
        ) as navigate_messages, self.assertLogs(
            "daily_streak.recipients", level="INFO"
        ) as logs:
            recipients = asyncio.run(service.scan_dm_list(initial_page))

        self.assertEqual(recipients, [])
        self.assertTrue(initial_page.closed)
        self.assertEqual(len(context.created_pages), 1)
        self.assertEqual(service.wait_for_dm_readiness.await_count, 2)
        self.assertEqual(navigate_messages.await_count, 2)
        output = "\n".join(logs.output)
        self.assertIn("Scan DM load attempt 1/2", output)
        self.assertIn("Scan renderer unresponsive; recreating page", output)
        self.assertIn("Scan DM load attempt 2/2", output)

    def test_scan_renderer_unresponsive_twice_fails_after_two_attempts(self) -> None:
        context = ResolveRecoveryContext()
        initial_page = ResolveRecoveryPage(context)
        service = RecipientService()
        service.wait_for_dm_readiness = AsyncMock(
            side_effect=[
                TikTokRendererUnresponsiveError("first renderer failure"),
                TikTokRendererUnresponsiveError("second renderer failure"),
            ]
        )

        with patch(
            "daily_streak.recipients.navigate_to_messages",
            new=AsyncMock(),
        ) as navigate_messages:
            with self.assertRaisesRegex(TikTokRendererUnresponsiveError, "second renderer failure"):
                asyncio.run(service.scan_dm_list(initial_page))

        self.assertTrue(initial_page.closed)
        self.assertEqual(len(context.created_pages), 1)
        self.assertFalse(context.created_pages[0].closed)
        self.assertEqual(service.wait_for_dm_readiness.await_count, 2)
        self.assertEqual(navigate_messages.await_count, 2)

    def test_send_dm_loader_honors_one_attempt_and_extended_readiness_budget(self) -> None:
        context = ResolveRecoveryContext()
        initial_page = ResolveRecoveryPage(context)
        service = RecipientService(
            dm_readiness_timeout_ms=120_000,
            dm_load_attempts=1,
        )
        service.wait_for_dm_readiness = AsyncMock(
            side_effect=TikTokRendererUnresponsiveError("renderer stopped responding")
        )

        with patch(
            "daily_streak.recipients.navigate_to_messages",
            new=AsyncMock(),
        ) as navigate_messages, self.assertLogs(
            "daily_streak.recipients", level="INFO"
        ) as logs:
            with self.assertRaisesRegex(TikTokRendererUnresponsiveError, "renderer stopped"):
                asyncio.run(
                    service._load_messages_with_renderer_recovery(
                        initial_page,
                        caller="Send",
                    )
                )

        self.assertFalse(initial_page.closed)
        self.assertEqual(context.created_pages, [])
        self.assertEqual(service.wait_for_dm_readiness.await_count, 1)
        navigate_messages.assert_awaited_once()
        output = "\n".join(logs.output)
        self.assertIn("Send DM load attempt 1/1", output)
        self.assertIn("DM readiness budget: 120.0s", output)

    def test_scan_non_renderer_readiness_outcomes_do_not_retry_or_create_a_page(self) -> None:
        for error in (
            TikTokLoginRequiredError("login required"),
            TikTokChallengeRequiredError("challenge required"),
        ):
            with self.subTest(error=type(error).__name__):
                context = ResolveRecoveryContext()
                page = ResolveRecoveryPage(context)
                service = RecipientService()
                service.wait_for_dm_readiness = AsyncMock(side_effect=error)

                with patch(
                    "daily_streak.recipients.navigate_to_messages",
                    new=AsyncMock(),
                ) as navigate_messages:
                    with self.assertRaises(type(error)):
                        asyncio.run(service.scan_dm_list(page))

                self.assertFalse(page.closed)
                self.assertEqual(context.created_pages, [])
                navigate_messages.assert_awaited_once()

        context = ResolveRecoveryContext()
        page = ResolveRecoveryPage(context)
        service = RecipientService()
        service.wait_for_dm_readiness = AsyncMock(return_value=(object(), object(), True))
        with patch(
            "daily_streak.recipients.navigate_to_messages",
            new=AsyncMock(),
        ) as navigate_messages:
            recipients = asyncio.run(service.scan_dm_list(page))

        self.assertEqual(recipients, [])
        self.assertFalse(page.closed)
        self.assertEqual(context.created_pages, [])
        navigate_messages.assert_awaited_once()

    def test_successful_first_scan_dm_load_does_not_create_another_page(self) -> None:
        context = ResolveRecoveryContext()
        initial_page = ResolveRecoveryPage(context)
        service = RecipientService()
        service.wait_for_dm_readiness = AsyncMock(return_value=(object(), object(), True))

        with patch(
            "daily_streak.recipients.navigate_to_messages",
            new=AsyncMock(),
        ) as navigate_messages:
            recipients = asyncio.run(service.scan_dm_list(initial_page))

        self.assertEqual(recipients, [])
        self.assertFalse(initial_page.closed)
        self.assertEqual(context.created_pages, [])
        navigate_messages.assert_awaited_once()

    def test_sender_selection_uses_configured_display_name_without_profile_lookup(self) -> None:
        page = SenderSelectionPage()
        notifier = object()
        service = RecipientService()
        readiness = (object(), object(), False)
        service._load_messages_with_renderer_recovery = AsyncMock(
            return_value=(page, readiness)
        )
        service._handle_tiktok_interruptions = AsyncMock()
        service.click_conversation_by_display_name = AsyncMock()
        service.wait_for_message_input = AsyncMock()

        with patch(
            "daily_streak.recipients.navigate_to_profile",
            new=AsyncMock(),
        ) as navigate_profile, patch(
            "daily_streak.recipients.handle_sleep_hours_popup",
            new=AsyncMock(),
        ), self.assertLogs("daily_streak.recipients", level="INFO") as logs:
            selected_name = asyncio.run(
                service.select_recipient_for_username(
                    page,
                    "@khoa_memezlol",
                    notifier,
                    configured_display_name="Khoa Đức",
                )
            )

        self.assertEqual(selected_name, "Khoa Đức")
        navigate_profile.assert_not_awaited()
        service._load_messages_with_renderer_recovery.assert_awaited_once_with(
            page,
            caller="Send",
        )
        service.click_conversation_by_display_name.assert_awaited_once_with(
            page,
            "Khoa Đức",
            max_scrolls=10,
            scroll_distance=400,
            after_click_wait_ms=0,
            reject_ambiguous=True,
            readiness=readiness,
        )
        self.assertIn(
            "Selecting recipient Khoa Đức using configured display name",
            "\n".join(logs.output),
        )

    def test_sender_selection_falls_back_to_profile_lookup_without_display_name(self) -> None:
        page = ProfileSelectionPage("Khoa Đức")
        notifier = object()
        service = RecipientService()
        service._handle_tiktok_interruptions = AsyncMock()
        service._select_recipient_by_display_name = AsyncMock(
            return_value=(page, "Khoa Đức")
        )

        with patch(
            "daily_streak.recipients.navigate_to_profile",
            new=AsyncMock(),
        ) as navigate_profile, self.assertLogs(
            "daily_streak.recipients", level="INFO"
        ) as logs:
            selected_name = asyncio.run(
                service.select_recipient_for_username(
                    page,
                    "@khoa_memezlol",
                    notifier,
                )
            )

        self.assertEqual(selected_name, "Khoa Đức")
        navigate_profile.assert_awaited_once_with(
            page,
            "khoa_memezlol",
            navigation_timeout_ms=service.navigation_timeout_ms,
        )
        service._select_recipient_by_display_name.assert_awaited_once_with(
            page,
            "Khoa Đức",
            "@khoa_memezlol",
            notifier,
            reject_ambiguous=False,
        )
        self.assertIn(
            "Display name unavailable; falling back to TikTok profile lookup",
            "\n".join(logs.output),
        )

    def test_sender_selection_uses_the_shared_dm_loader_and_returns_its_page(self) -> None:
        initial_page = SenderSelectionPage()
        recovered_page = SenderSelectionPage()
        notifier = object()
        readiness = (object(), object(), False)
        service = RecipientService()
        service._load_messages_with_renderer_recovery = AsyncMock(
            return_value=(recovered_page, readiness)
        )
        service._handle_tiktok_interruptions = AsyncMock()
        service.click_conversation_by_display_name = AsyncMock()
        service.wait_for_message_input = AsyncMock()

        with patch(
            "daily_streak.recipients.handle_sleep_hours_popup",
            new=AsyncMock(),
        ):
            selected_page, selected_name = asyncio.run(
                service.select_recipient_for_username_with_page(
                    initial_page,
                    "@khoa_memezlol",
                    notifier,
                    configured_display_name="Khoa Đức",
                )
            )

        self.assertIs(selected_page, recovered_page)
        self.assertEqual(selected_name, "Khoa Đức")
        service._load_messages_with_renderer_recovery.assert_awaited_once_with(
            initial_page,
            caller="Send",
        )
        service.click_conversation_by_display_name.assert_awaited_once_with(
            recovered_page,
            "Khoa Đức",
            max_scrolls=10,
            scroll_distance=400,
            after_click_wait_ms=0,
            reject_ambiguous=True,
            readiness=readiness,
        )

    def test_exact_row_locator_filters_rows_through_the_exact_nickname(self) -> None:
        class Locator:
            def __init__(self, label: str) -> None:
                self.label = label
                self.filters: list[dict[str, object]] = []
                self.next: Locator | None = None

            def filter(self, **kwargs):  # type: ignore[no-untyped-def]
                self.filters.append(kwargs)
                if self.next is None:
                    raise AssertionError("Unexpected extra locator filter")
                return self.next

        class Page:
            def __init__(self) -> None:
                self.nickname_locator = Locator("nickname")
                self.rows_locator = Locator("rows")
                self.filtered_nicknames = Locator("filtered nicknames")
                self.filtered_rows = Locator("filtered rows")
                self.nickname_locator.next = self.filtered_nicknames
                self.rows_locator.next = self.filtered_rows
                self.selectors: list[str] = []

            def locator(self, selector: str) -> Locator:
                self.selectors.append(selector)
                if selector == "[data-e2e='dm-new-conversation-nickname']":
                    return self.nickname_locator
                if selector == "[data-e2e='dm-new-conversation-item']":
                    return self.rows_locator
                raise AssertionError(f"Unexpected selector: {selector}")

        page = Page()
        result = RecipientService._exact_conversation_row_locator(page, "còii cte~")

        self.assertIs(result, page.filtered_rows)
        self.assertEqual(
            page.selectors,
            [
                "[data-e2e='dm-new-conversation-nickname']",
                "[data-e2e='dm-new-conversation-item']",
            ],
        )
        nickname_filter = page.nickname_locator.filters[0]
        self.assertIn("has_text", nickname_filter)
        self.assertTrue(nickname_filter["has_text"].fullmatch("còii cte~"))  # type: ignore[union-attr]
        self.assertEqual(page.rows_locator.filters, [{"has": page.filtered_nicknames}])

    def test_resolve_renderer_unresponsive_recreates_one_fresh_page_then_succeeds(self) -> None:
        context = ResolveRecoveryContext()
        initial_page = ResolveRecoveryPage(context)
        readiness = (object(), object(), False)
        service = RecipientService()
        service.wait_for_dm_readiness = AsyncMock(
            side_effect=[
                TikTokRendererUnresponsiveError("renderer stopped responding"),
                readiness,
            ]
        )

        with patch(
            "daily_streak.recipients.navigate_to_messages",
            new=AsyncMock(),
        ) as navigate_messages, self.assertLogs(
            "daily_streak.recipients", level="INFO"
        ) as logs:
            page, actual_readiness = asyncio.run(
                service._load_messages_for_resolution(initial_page)
            )

        self.assertEqual(actual_readiness, readiness)
        self.assertTrue(initial_page.closed)
        self.assertEqual(len(context.created_pages), 1)
        self.assertIs(page, context.created_pages[0])
        self.assertEqual(service.wait_for_dm_readiness.await_count, 2)
        self.assertEqual(navigate_messages.await_count, 2)
        output = "\n".join(logs.output)
        self.assertIn("Resolve DM load attempt 1/2", output)
        self.assertIn("Resolve renderer unresponsive; recreating page", output)
        self.assertIn("Resolve DM load attempt 2/2", output)

    def test_resolve_renderer_unresponsive_twice_fails_after_two_attempts(self) -> None:
        context = ResolveRecoveryContext()
        initial_page = ResolveRecoveryPage(context)
        service = RecipientService()
        service.wait_for_dm_readiness = AsyncMock(
            side_effect=[
                TikTokRendererUnresponsiveError("first renderer failure"),
                TikTokRendererUnresponsiveError("second renderer failure"),
            ]
        )

        with patch(
            "daily_streak.recipients.navigate_to_messages",
            new=AsyncMock(),
        ) as navigate_messages:
            with self.assertRaisesRegex(TikTokRendererUnresponsiveError, "second renderer failure"):
                asyncio.run(service._load_messages_for_resolution(initial_page))

        self.assertTrue(initial_page.closed)
        self.assertEqual(len(context.created_pages), 1)
        self.assertFalse(context.created_pages[0].closed)
        self.assertEqual(service.wait_for_dm_readiness.await_count, 2)
        self.assertEqual(navigate_messages.await_count, 2)

    def test_resolve_non_renderer_outcomes_do_not_recreate_a_page(self) -> None:
        for error in (
            TikTokLoginRequiredError("login required"),
            TikTokChallengeRequiredError("challenge required"),
        ):
            with self.subTest(error=type(error).__name__):
                context = ResolveRecoveryContext()
                page = ResolveRecoveryPage(context)
                service = RecipientService()
                service.wait_for_dm_readiness = AsyncMock(side_effect=error)
                with patch(
                    "daily_streak.recipients.navigate_to_messages",
                    new=AsyncMock(),
                ) as navigate_messages:
                    with self.assertRaises(type(error)):
                        asyncio.run(service._load_messages_for_resolution(page))
                self.assertFalse(page.closed)
                self.assertEqual(context.created_pages, [])
                navigate_messages.assert_awaited_once()

        context = ResolveRecoveryContext()
        page = ResolveRecoveryPage(context)
        service = RecipientService()
        service.wait_for_dm_readiness = AsyncMock(return_value=(object(), object(), True))
        with patch(
            "daily_streak.recipients.navigate_to_messages",
            new=AsyncMock(),
        ):
            resolved = asyncio.run(
                service.resolve_usernames(page, [{"name": "Empty", "username": ""}])
            )
        self.assertEqual(resolved, [])
        self.assertEqual(context.created_pages, [])

        context = ResolveRecoveryContext()
        page = ResolveRecoveryPage(context)
        service = RecipientService()
        service.wait_for_dm_readiness = AsyncMock(return_value=(object(), object(), False))
        service.click_conversation_by_display_name = AsyncMock(
            side_effect=ValueError("conversation not found")
        )
        with patch(
            "daily_streak.recipients.navigate_to_messages",
            new=AsyncMock(),
        ):
            resolved = asyncio.run(
                service.resolve_usernames(page, [{"name": "Missing", "username": ""}])
            )
        self.assertEqual(resolved, [])
        self.assertEqual(context.created_pages, [])

        context = ResolveRecoveryContext()
        page = ResolveRecoveryPage(context)
        service = RecipientService()
        service.wait_for_dm_readiness = AsyncMock(return_value=(object(), object(), False))
        service.click_conversation_by_display_name = AsyncMock(
            side_effect=PlaywrightTimeoutError("normal click timed out")
        )
        with patch(
            "daily_streak.recipients.navigate_to_messages",
            new=AsyncMock(),
        ):
            resolved = asyncio.run(
                service.resolve_usernames(page, [{"name": "Click failure", "username": ""}])
            )
        self.assertEqual(resolved, [])
        self.assertEqual(context.created_pages, [])

    def test_successful_first_resolve_dm_load_does_not_create_another_page(self) -> None:
        context = ResolveRecoveryContext()
        initial_page = ResolveRecoveryPage(context)
        readiness = (object(), object(), False)
        service = RecipientService()
        service.wait_for_dm_readiness = AsyncMock(return_value=readiness)

        with patch(
            "daily_streak.recipients.navigate_to_messages",
            new=AsyncMock(),
        ) as navigate_messages:
            page, actual_readiness = asyncio.run(
                service._load_messages_for_resolution(initial_page)
            )

        self.assertIs(page, initial_page)
        self.assertEqual(actual_readiness, readiness)
        self.assertFalse(initial_page.closed)
        self.assertEqual(context.created_pages, [])
        navigate_messages.assert_awaited_once()

    def test_resolver_clicks_only_the_exact_matched_row_and_logs_row_diagnostics(self) -> None:
        page = ConversationPage("@target_username")
        conversation_list = ConversationList()
        target_item = ConversationItem("Target person")
        unrelated_item = ConversationItem("Other person")
        conversation_items = ConversationItems(target_item, unrelated_item)
        service = RecipientService()

        with patch(
            "daily_streak.recipients.handle_screen_time_popup",
            new=AsyncMock(),
        ), patch.object(
            service,
            "_exact_conversation_row_locator",
            return_value=target_item,
        ), self.assertLogs("daily_streak.recipients", level="INFO") as logs:
            asyncio.run(
                service.click_conversation_by_display_name(
                    page,
                    "Target person",
                    readiness=(conversation_list, conversation_items, False),
                    reject_ambiguous=True,
                    after_click_wait_ms=0,
                )
            )

        self.assertFalse(unrelated_item.clicked)
        self.assertTrue(target_item.clicked)
        self.assertEqual(target_item.scroll_timeouts, [3_000])
        self.assertEqual(target_item.click_timeouts, [3_000])
        output = "\n".join(logs.output)
        self.assertIn("target_display_name='Target person'", output)
        self.assertIn("total_dm_item_count=2", output)
        self.assertIn("matched_item_count=1", output)
        self.assertIn("matched_display_name='Target person'", output)
        self.assertIn("bounding_box={'x': 10, 'y': 20, 'width': 100, 'height': 40}", output)
        self.assertIn("is_visible=True", output)
        self.assertIn("aria_selected=None", output)
        self.assertIn("already_selected=False", output)

    def test_resolver_uses_bounded_dom_click_fallback_only_for_the_matched_row(self) -> None:
        page = ConversationPage("@target_username")
        conversation_list = ConversationList()
        target_item = ConversationItem(
            "Target person",
            aria_selected="false",
            click_error=PlaywrightTimeoutError("row was unstable"),
        )
        service = RecipientService()

        with patch(
            "daily_streak.recipients.handle_screen_time_popup",
            new=AsyncMock(),
        ), patch.object(
            service,
            "_exact_conversation_row_locator",
            return_value=target_item,
        ), self.assertLogs("daily_streak.recipients", level="WARNING") as logs:
            asyncio.run(
                service.click_conversation_by_display_name(
                    page,
                    "Target person",
                    readiness=(conversation_list, ConversationItems(target_item), False),
                    reject_ambiguous=True,
                    after_click_wait_ms=0,
                )
            )

        self.assertEqual(target_item.click_timeouts, [3_000])
        self.assertEqual(target_item.dom_clicks, 1)
        self.assertTrue(target_item.clicked)
        self.assertEqual(target_item.aria_selected, "true")
        self.assertIn("using bounded DOM click fallback", "\n".join(logs.output))

    def test_resolver_does_not_click_an_already_selected_matched_row(self) -> None:
        page = ConversationPage("@target_username")
        conversation_list = ConversationList()
        target_item = ConversationItem("Target person", aria_selected="true")
        service = RecipientService()

        with patch(
            "daily_streak.recipients.handle_screen_time_popup",
            new=AsyncMock(),
        ), patch.object(
            service,
            "_exact_conversation_row_locator",
            return_value=target_item,
        ):
            asyncio.run(
                service.click_conversation_by_display_name(
                    page,
                    "Target person",
                    readiness=(conversation_list, ConversationItems(target_item), False),
                    reject_ambiguous=True,
                    after_click_wait_ms=0,
                )
            )

        self.assertEqual(target_item.click_timeouts, [])
        self.assertEqual(target_item.dom_clicks, 0)

    def test_resolve_skips_existing_usernames_without_browser_navigation(self) -> None:
        service = RecipientService()
        service.wait_for_dm_readiness = AsyncMock()
        service.click_conversation_by_display_name = AsyncMock()
        page = object()

        with patch(
            "daily_streak.recipients.navigate_to_messages",
            new=AsyncMock(),
        ) as navigate_messages:
            resolved = asyncio.run(
                service.resolve_usernames(
                    page,
                    [{"name": "Đức Khoa", "username": "@khoa_memezlol"}],
                )
            )

        self.assertEqual(resolved, [])
        navigate_messages.assert_not_awaited()
        service.wait_for_dm_readiness.assert_not_awaited()
        service.click_conversation_by_display_name.assert_not_awaited()

    def test_resolution_failure_does_not_abort_later_recipients_or_reuse_pages(self) -> None:
        service = RecipientService()
        page = object()
        readiness = (object(), object(), False)
        service.wait_for_dm_readiness = AsyncMock(return_value=readiness)
        service.wait_for_dm_conversation_list = AsyncMock(return_value=readiness)
        clicked: list[tuple[object, str]] = []
        current_name = ""

        async def click(page_argument, display_name, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal current_name
            clicked.append((page_argument, display_name))
            current_name = display_name
            if display_name == "Broken":
                raise ValueError("conversation is unavailable")

        async def extract(page_argument):  # type: ignore[no-untyped-def]
            self.assertIs(page_argument, page)
            return {"Alice": "alice", "Carol": "carol"}[current_name]

        service.click_conversation_by_display_name = click  # type: ignore[assignment]
        service.extract_username_from_chat = extract  # type: ignore[assignment]

        with patch(
            "daily_streak.recipients.navigate_to_messages",
            new=AsyncMock(),
        ) as navigate_messages, self.assertLogs(
            "daily_streak.recipients", level="WARNING"
        ) as logs:
            resolved = asyncio.run(
                service.resolve_usernames(
                    page,
                    [
                        {"name": "Alice", "username": ""},
                        {"name": "Broken", "username": ""},
                        {"name": "Carol", "username": ""},
                    ],
                )
            )

        self.assertEqual(
            resolved,
            [
                {"name": "Alice", "username": "alice"},
                {"name": "Carol", "username": "carol"},
            ],
        )
        self.assertEqual([name for _, name in clicked], ["Alice", "Broken", "Carol"])
        self.assertTrue(all(page_argument is page for page_argument, _ in clicked))
        self.assertEqual(navigate_messages.await_count, 3)
        self.assertEqual(service.wait_for_dm_readiness.await_count, 1)
        self.assertEqual(service.wait_for_dm_conversation_list.await_count, 2)
        self.assertIn("Could not resolve TikTok username for Broken", "\n".join(logs.output))

    def test_duplicate_display_names_are_not_clicked_or_silently_resolved(self) -> None:
        page = ConversationPage("@should-not-be-read")
        conversation_list = ConversationList()
        first = ConversationItem("Same Name")
        second = ConversationItem("Same Name")
        conversation_items = ConversationItems(first, second)
        service = RecipientService()

        with self.assertRaisesRegex(AmbiguousConversationError, "found 2 exact matches"):
            asyncio.run(
                service.click_conversation_by_display_name(
                    page,
                    "Same Name",
                    readiness=(conversation_list, conversation_items, False),
                    reject_ambiguous=True,
                    after_click_wait_ms=0,
                )
            )

        self.assertFalse(first.clicked)
        self.assertFalse(second.clicked)

    def test_is_page_dm_ready_validations(self) -> None:
        service = RecipientService()

        class MockPage:
            def __init__(self, url: str = "https://www.tiktok.com/messages", closed: bool = False, has_list: bool = True, has_captcha: bool = False) -> None:
                self.url = url
                self._closed = closed
                self.has_list = has_list
                self.has_captcha = has_captcha

            def is_closed(self) -> bool:
                return self._closed

            def locator(self, selector: str) -> Any:
                class Element:
                    def __init__(self, visible: bool) -> None:
                        self._visible = visible
                    async def count(self) -> int:
                        return 1 if self._visible else 0
                    async def is_visible(self) -> bool:
                        return self._visible
                    @property
                    def first(self) -> "Element":
                        return self

                if "captcha" in selector:
                    return Element(self.has_captcha)
                if "conversation-list" in selector:
                    return Element(self.has_list)
                return Element(False)

        # 1. Closed page -> False
        self.assertFalse(asyncio.run(service.is_page_dm_ready(MockPage(closed=True))))
        # 2. Non-TikTok url -> False
        self.assertFalse(asyncio.run(service.is_page_dm_ready(MockPage(url="https://google.com"))))
        # 3. Login url -> False
        self.assertFalse(asyncio.run(service.is_page_dm_ready(MockPage(url="https://www.tiktok.com/login"))))
        # 4. Captcha visible -> False
        self.assertFalse(asyncio.run(service.is_page_dm_ready(MockPage(has_captcha=True))))
        # 5. Conversation list missing -> False
        self.assertFalse(asyncio.run(service.is_page_dm_ready(MockPage(has_list=False))))
        # 6. Valid ready page -> True
        self.assertTrue(asyncio.run(service.is_page_dm_ready(MockPage())))

    def test_select_recipient_reuses_dm_list_when_page_is_ready(self) -> None:
        service = RecipientService()
        mock_page = MagicMock()
        mock_page.evaluate = AsyncMock()
        mock_list = MagicMock()
        mock_list.evaluate = AsyncMock()
        mock_page.locator.return_value.first = mock_list

        service.is_page_dm_ready = AsyncMock(return_value=True)
        readiness = (mock_list, MagicMock(), False)
        service.wait_for_dm_conversation_list = AsyncMock(return_value=readiness)
        service._load_messages_with_renderer_recovery = AsyncMock()
        service._handle_tiktok_interruptions = AsyncMock()
        service.click_conversation_by_display_name = AsyncMock()
        service.wait_for_message_input = AsyncMock()

        with patch("daily_streak.recipients.handle_sleep_hours_popup", new=AsyncMock()):
            page, name = asyncio.run(
                service.select_recipient_for_username_with_page(
                    mock_page,
                    "alice",
                    object(),
                    configured_display_name="Alice",
                    reuse_existing_page=True,
                )
            )

        self.assertEqual(name, "Alice")
        service.is_page_dm_ready.assert_awaited_once_with(mock_page)
        service.wait_for_dm_conversation_list.assert_awaited_once_with(mock_page)
        # Crucial: full messages reload was bypassed!
        service._load_messages_with_renderer_recovery.assert_not_awaited()
        service.click_conversation_by_display_name.assert_awaited_once()

    def test_select_recipient_recovers_via_full_load_when_retained_page_not_ready(self) -> None:
        service = RecipientService()
        mock_page = MagicMock()
        mock_page.evaluate = AsyncMock()
        service.is_page_dm_ready = AsyncMock(return_value=False)
        readiness = (MagicMock(), MagicMock(), False)
        service._load_messages_with_renderer_recovery = AsyncMock(return_value=(mock_page, readiness))
        service.wait_for_dm_conversation_list = AsyncMock()
        service._handle_tiktok_interruptions = AsyncMock()
        service.click_conversation_by_display_name = AsyncMock()
        service.wait_for_message_input = AsyncMock()

        with patch("daily_streak.recipients.handle_sleep_hours_popup", new=AsyncMock()):
            page, name = asyncio.run(
                service.select_recipient_for_username_with_page(
                    mock_page,
                    "alice",
                    object(),
                    configured_display_name="Alice",
                    reuse_existing_page=True,
                )
            )

        self.assertEqual(name, "Alice")
        service.is_page_dm_ready.assert_awaited_once_with(mock_page)
        # Fallback to full load was triggered
        service._load_messages_with_renderer_recovery.assert_awaited_once()
