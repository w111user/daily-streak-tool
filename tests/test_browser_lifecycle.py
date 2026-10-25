"""Regression tests for browser context and page lifecycle safety."""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import AsyncMock, MagicMock

from daily_streak.browser import BrowserSession, is_context_closed
from daily_streak.history import RunHistoryStore
from daily_streak.models import DeliveryStatus, RecipientStatus
from daily_streak.recipients import (
    RecipientService,
    TikTokDMReadinessError,
    TikTokRendererUnresponsiveError,
)
from daily_streak.sender import (
    DeliveryConfirmedNotSentError,
    DeliveryUnconfirmedError,
    TikTokSender,
)
from daily_streak.state import DailyStateStore


class MockContext:
    def __init__(self, *, is_closed: bool = False, browser_connected: bool = True) -> None:
        self._is_closed = is_closed
        self._browser_connected = browser_connected
        self.pages: list[Any] = []

    def is_closed(self) -> bool:
        return self._is_closed

    @property
    def browser(self) -> Any:
        m = MagicMock()
        m.is_connected = lambda: self._browser_connected
        return m

    async def new_page(self) -> Any:
        if self._is_closed or not self._browser_connected:
            raise RuntimeError("BrowserContext.new_page: Target page, context or browser has been closed")
        p = MockPage(self)
        self.pages.append(p)
        return p


class MockPage:
    def __init__(self, context: Any = None) -> None:
        self._is_closed = False
        self._context = context
        self.url = "https://www.tiktok.com/messages"

    def is_closed(self) -> bool:
        return self._is_closed

    def context(self) -> Any:
        return self._context

    async def close(self) -> None:
        self._is_closed = True
        if self._context and hasattr(self._context, "pages"):
            if self in self._context.pages:
                self._context.pages.remove(self)

    async def goto(self, url: str, **kwargs: Any) -> None:
        if self._is_closed:
            raise RuntimeError("Target page, context or browser has been closed")
        self.url = url

    def locator(self, *args: Any, **kwargs: Any) -> Any:
        loc = MagicMock()
        loc.first = loc
        loc.count = AsyncMock(return_value=0)
        loc.is_visible = AsyncMock(return_value=False)
        loc.wait_for = AsyncMock()
        return loc


class BrowserLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def test_is_context_closed_helper(self) -> None:
        self.assertTrue(is_context_closed(None))

        # Context explicitly reporting closed
        ctx = MockContext(is_closed=True)
        self.assertTrue(is_context_closed(ctx))

        # Context reporting open but browser disconnected
        ctx_disconnected = MockContext(is_closed=False, browser_connected=False)
        self.assertTrue(is_context_closed(ctx_disconnected))

        # Healthy open context
        ctx_open = MockContext(is_closed=False, browser_connected=True)
        self.assertFalse(is_context_closed(ctx_open))

    async def test_session_get_page_recreates_closed_page_when_context_alive(self) -> None:
        ctx = MockContext(is_closed=False)
        session = BrowserSession(browser=ctx.browser, context=ctx, persistent_context=True)

        page1 = await session.get_page()
        self.assertFalse(page1.is_closed())

        # Close the page while context remains alive
        await page1.close()
        self.assertTrue(page1.is_closed())

        # Getting page again creates a fresh alive page
        page2 = await session.get_page()
        self.assertFalse(page2.is_closed())
        self.assertIsNot(page1, page2)

    async def test_session_get_page_raises_when_context_is_closed(self) -> None:
        ctx = MockContext(is_closed=True)
        session = BrowserSession(browser=ctx.browser, context=ctx, persistent_context=True)

        with self.assertRaises(RuntimeError) as cm:
            await session.get_page()
        self.assertIn("Browser context is closed", str(cm.exception))

    async def test_send_recipients_aborts_cleanly_if_context_closed_at_start(self) -> None:
        config: dict[str, Any] = {
            "tiktok": {"recipient_retries": 0},
            "recipients": [{"name": "User 1", "username": "user1"}],
        }
        with TemporaryDirectory() as tmp_dir:
            fake_pool = MagicMock()
            fake_pool.random_video = lambda: "https://www.tiktok.com/@test/video/111"
            sender = TikTokSender(
                config,
                MagicMock(),
                fake_pool,
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
            )
            closed_ctx = MockContext(is_closed=True)
            summary = await sender.send_recipients(closed_ctx)

            self.assertIsNotNone(summary.setup_error)
            self.assertIn("Browser context was closed", summary.setup_error or "")
            self.assertEqual(summary.success_count, 0)
            self.assertEqual(summary.failed_count, 0)

    async def test_send_recipients_records_failure_and_aborts_if_context_closes_during_run(self) -> None:
        config: dict[str, Any] = {
            "tiktok": {"recipient_retries": 0, "message_delay_seconds": [0, 0]},
            "recipients": [
                {"name": "User 1", "username": "user1"},
                {"name": "User 2", "username": "user2"},
            ],
        }
        with TemporaryDirectory() as tmp_dir:
            fake_pool = MagicMock()
            fake_pool.random_video = lambda: "https://www.tiktok.com/@test/video/111"
            sender = TikTokSender(
                config,
                MagicMock(),
                fake_pool,
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
            )

            ctx = MockContext(is_closed=False)
            initial_page = await ctx.new_page()

            # Simulate browser crash/context closure when first recipient starts
            async def fake_send_once(*args: Any, **kwargs: Any) -> None:
                ctx._is_closed = True
                await initial_page.close()
                raise RuntimeError("browser closed")

            sender._send_recipient_once = fake_send_once  # type: ignore[method-assign]

            summary = await sender.send_recipients(ctx, dm_page=initial_page)

            # Did not raise uncaught BrowserContext.new_page TargetClosedError!
            self.assertEqual(summary.success_count, 0)
            self.assertEqual(summary.failed_count, 2)
            # Second recipient failed cleanly because context was closed
            self.assertIn("closed", summary.results[1].error_message or "")

    async def test_renderer_recovery_raises_when_context_closed(self) -> None:
        service = RecipientService()
        ctx = MockContext(is_closed=True)
        page = MockPage(ctx)
        page._is_closed = True

        with self.assertRaises(TikTokDMReadinessError) as cm:
            await service._load_messages_with_renderer_recovery(page, caller="Test")
        self.assertIn("closed", str(cm.exception).lower())

    async def test_native_card_flow_checks_context_before_video_page(self) -> None:
        config: dict[str, Any] = {
            "tiktok": {"recipient_retries": 0},
            "recipients": [{"name": "User 1", "username": "user1"}],
        }
        with TemporaryDirectory() as tmp_dir:
            sender = TikTokSender(
                config,
                MagicMock(),
                MagicMock(),
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
            )

            ctx = MockContext(is_closed=False)
            dm_page = await ctx.new_page()

            sender.recipient_service.select_recipient_for_username_with_page = AsyncMock(  # type: ignore[method-assign]
                return_value=(dm_page, "User 1")
            )
            sender._handle_tiktok_interruptions = AsyncMock()  # type: ignore[method-assign]
            sender._count_chat_items = AsyncMock(return_value=0)  # type: ignore[method-assign]

            # Context closes right after recipient selection
            ctx._is_closed = True

            with self.assertRaises(DeliveryConfirmedNotSentError) as cm:
                await sender._send_native_card_flow(
                    ctx,
                    {"name": "User 1", "username": "user1"},
                    "https://www.tiktok.com/@test/video/1234567890",
                    "user1",
                    dm_page=dm_page,
                )
            self.assertIn("Browser context was closed", str(cm.exception))

    async def test_persistent_context_single_page_closure_aborts_batch_safely_without_crash(self) -> None:
        """Simulate real Chromium persistent context: closing the last open page terminates context.

        Verify sender safely detects closed context, does not attempt uncaught new_page,
        and records CONFIRMED_NOT_SENT without retrying sent messages or crashing.
        """
        config: dict[str, Any] = {
            "tiktok": {"recipient_retries": 1, "message_delay_seconds": [0, 0]},
            "recipients": [
                {"name": "User 1", "username": "user1"},
                {"name": "User 2", "username": "user2"},
            ],
        }
        with TemporaryDirectory() as tmp_dir:
            fake_pool = MagicMock()
            fake_pool.random_video = lambda: "https://www.tiktok.com/@test/video/111"

            sender = TikTokSender(
                config,
                MagicMock(),
                fake_pool,
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
            )

            # Context that closes itself when its last page is closed (mimicking Chromium)
            class PersistentChromiumContext(MockContext):
                def on_page_closed(self) -> None:
                    alive = [p for p in self.pages if not p.is_closed()]
                    if not alive:
                        self._is_closed = True

            ctx = PersistentChromiumContext(is_closed=False)
            initial_page = await ctx.new_page()

            # Wire up page close hook
            orig_close = initial_page.close

            async def hooked_close() -> None:
                await orig_close()
                ctx.on_page_closed()

            initial_page.close = hooked_close  # type: ignore[method-assign]

            # Recipient 1 simulates target/page closure during send
            async def failing_send_once(*args: Any, **kwargs: Any) -> None:
                await initial_page.close()
                raise RuntimeError("target closed")

            sender._send_recipient_once = failing_send_once  # type: ignore[method-assign]

            summary = await sender.send_recipients(ctx, dm_page=initial_page)

            # Entire batch completes deterministically without unhandled TargetClosedError
            self.assertEqual(summary.success_count, 0)
            self.assertEqual(summary.failed_count, 2)

            # Both recipients have delivery_status == CONFIRMED_NOT_SENT
            self.assertEqual(summary.results[0].delivery_status, DeliveryStatus.CONFIRMED_NOT_SENT)
            self.assertEqual(summary.results[1].delivery_status, DeliveryStatus.CONFIRMED_NOT_SENT)
            self.assertIn("closed", summary.results[1].error_message or "")

    async def test_transient_failure_keeps_context_alive_and_next_recipient_proceeds(self) -> None:
        """Verify that a transient recipient failure does not close the shared last page,

        allowing the persistent context to remain alive so subsequent recipients proceed and succeed.
        """
        config: dict[str, Any] = {
            "tiktok": {"recipient_retries": 1, "message_delay_seconds": [0, 0]},
            "recipients": [
                {"name": "User 1", "username": "user1"},
                {"name": "User 2", "username": "user2"},
            ],
        }
        with TemporaryDirectory() as tmp_dir:
            fake_pool = MagicMock()
            fake_pool.random_video = lambda: "https://www.tiktok.com/@test/video/111"

            sender = TikTokSender(
                config,
                MagicMock(),
                fake_pool,
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
            )

            # Context that closes if all pages are closed
            class PersistentChromiumContext(MockContext):
                def on_page_closed(self) -> None:
                    alive = [p for p in self.pages if not p.is_closed()]
                    if not alive:
                        self._is_closed = True

            ctx = PersistentChromiumContext(is_closed=False)
            initial_page = await ctx.new_page()

            # Wire up hook
            orig_close = initial_page.close

            async def hooked_close() -> None:
                await orig_close()
                ctx.on_page_closed()

            initial_page.close = hooked_close  # type: ignore[method-assign]

            attempts_count: dict[str, int] = {"user1": 0, "user2": 0}

            async def mock_send_once(context: Any, recipient: Mapping[str, Any], video: str) -> None:
                u = recipient["username"]
                attempts_count[u] += 1
                if u == "user1":
                    # Transient error (e.g. selection timeout or DOM error)
                    raise RuntimeError("transient selection failure")
                # user2 succeeds!

            sender._send_recipient_once = mock_send_once  # type: ignore[method-assign]

            summary = await sender.send_recipients(ctx, dm_page=initial_page)

            # Context must still be alive!
            self.assertFalse(ctx.is_closed())
            self.assertFalse(initial_page.is_closed())

            # User 1 failed after 2 attempts (retries did not destroy context)
            self.assertEqual(attempts_count["user1"], 2)
            # User 2 proceeded and succeeded!
            self.assertEqual(attempts_count["user2"], 1)

            self.assertEqual(summary.failed_count, 1)
            self.assertEqual(summary.success_count, 1)
            self.assertEqual(summary.results[0].status, RecipientStatus.FAILED)
            self.assertEqual(summary.results[0].delivery_status, DeliveryStatus.CONFIRMED_NOT_SENT)
            self.assertEqual(summary.results[1].status, RecipientStatus.SUCCESS)
            self.assertEqual(summary.results[1].delivery_status, DeliveryStatus.VERIFIED)

    async def test_dead_context_accounts_for_all_remaining_recipients_in_summary(self) -> None:
        """Verify that when context dies mid-run, ALL remaining recipients are marked aborted in summary."""
        config: dict[str, Any] = {
            "tiktok": {"recipient_retries": 0, "message_delay_seconds": [0, 0]},
            "recipients": [
                {"name": "User 1", "username": "user1"},
                {"name": "User 2", "username": "user2"},
                {"name": "User 3", "username": "user3"},
                {"name": "User 4", "username": "user4"},
            ],
        }
        with TemporaryDirectory() as tmp_dir:
            fake_pool = MagicMock()
            fake_pool.random_video = lambda: "https://www.tiktok.com/@test/video/111"

            sender = TikTokSender(
                config,
                MagicMock(),
                fake_pool,
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
            )

            ctx = MockContext(is_closed=False)
            initial_page = await ctx.new_page()

            async def mock_send_once(context: Any, recipient: Mapping[str, Any], video: str) -> None:
                u = recipient["username"]
                if u == "user1":
                    return  # success
                if u == "user2":
                    ctx._is_closed = True
                    raise RuntimeError("browser crash")

            sender._send_recipient_once = mock_send_once  # type: ignore[method-assign]

            summary = await sender.send_recipients(ctx, dm_page=initial_page)

            # All 4 recipients must be accounted for in summary
            self.assertEqual(len(summary.results), 4)
            self.assertEqual(summary.success_count, 1)
            self.assertEqual(summary.failed_count, 3)

            # User 1: success
            self.assertEqual(summary.results[0].username, "user1")
            self.assertEqual(summary.results[0].status, RecipientStatus.SUCCESS)

            # User 2: failed due to crash
            self.assertEqual(summary.results[1].username, "user2")
            self.assertEqual(summary.results[1].status, RecipientStatus.FAILED)
            self.assertEqual(summary.results[1].delivery_status, DeliveryStatus.CONFIRMED_NOT_SENT)

            # Users 3 and 4: accounted for as aborted
            for res in summary.results[2:]:
                self.assertEqual(res.status, RecipientStatus.FAILED)
                self.assertEqual(res.delivery_status, DeliveryStatus.CONFIRMED_NOT_SENT)
                self.assertIn("Batch aborted", res.error_message or "")

    async def test_delivery_unconfirmed_does_not_close_shared_page_or_terminate_context(self) -> None:
        """P1: DeliveryUnconfirmedError must not close shared persistent page or abort whole batch."""
        config: dict[str, Any] = {
            "tiktok": {"recipient_retries": 1, "message_delay_seconds": [0, 0]},
            "recipients": [
                {"name": "User 1", "username": "user1"},
                {"name": "User 2", "username": "user2"},
            ],
        }
        with TemporaryDirectory() as tmp_dir:
            fake_pool = MagicMock()
            fake_pool.random_video = lambda: "https://www.tiktok.com/@test/video/111"

            sender = TikTokSender(
                config,
                MagicMock(),
                fake_pool,
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
            )

            # Persistent context that terminates if page count drops to 0
            class SinglePagePersistentContext(MockContext):
                def on_page_closed(self) -> None:
                    alive = [p for p in self.pages if not p.is_closed()]
                    if not alive:
                        self._is_closed = True

            ctx = SinglePagePersistentContext(is_closed=False)
            initial_page = await ctx.new_page()

            orig_close = initial_page.close

            async def hooked_close() -> None:
                await orig_close()
                ctx.on_page_closed()

            initial_page.close = hooked_close  # type: ignore[method-assign]

            send_attempts: dict[str, int] = {"user1": 0, "user2": 0}

            async def mock_send_once(context: Any, recipient: Any, video: str) -> None:
                u = recipient["username"]
                send_attempts[u] += 1
                if u == "user1":
                    # Ambiguous delivery: Enter/Submit was pressed, but confirmation timed out
                    raise DeliveryUnconfirmedError("Verification timed out after submit")
                # user2 succeeds cleanly

            sender._send_recipient_once = mock_send_once  # type: ignore[method-assign]

            summary = await sender.send_recipients(ctx, dm_page=initial_page)

            # Neither the page nor the persistent context was closed!
            self.assertFalse(initial_page.is_closed())
            self.assertFalse(ctx.is_closed())

            # user1 was NOT retried automatically (attempts == 1 despite retries=1 configured)
            self.assertEqual(send_attempts["user1"], 1)
            # user2 ran and succeeded on the same persistent context!
            self.assertEqual(send_attempts["user2"], 1)

            # Statuses in summary
            self.assertEqual(summary.unconfirmed_count, 1)
            self.assertEqual(summary.success_count, 1)
            self.assertEqual(summary.results[0].status, RecipientStatus.UNCONFIRMED)
            self.assertEqual(summary.results[0].delivery_status, DeliveryStatus.UNCONFIRMED)
            self.assertEqual(summary.results[1].status, RecipientStatus.SUCCESS)
            self.assertEqual(summary.results[1].delivery_status, DeliveryStatus.VERIFIED)

    async def test_renderer_recovery_creates_replacement_before_closing_old_page(self) -> None:
        """P1: Stalled renderer recovery creates replacement page BEFORE closing old page."""
        service = RecipientService(dm_load_attempts=2)

        class SinglePagePersistentContext(MockContext):
            def __init__(self) -> None:
                super().__init__(is_closed=False)
                self.min_pages_observed = 999

            async def new_page(self) -> Any:
                p = await super().new_page()
                orig_p_close = p.close

                async def p_close() -> None:
                    await orig_p_close()
                    alive = [x for x in self.pages if not x.is_closed()]
                    if not alive:
                        self._is_closed = True

                p.close = p_close  # type: ignore[method-assign]
                return p

        ctx = SinglePagePersistentContext()
        page1 = await ctx.new_page()

        call_count = 0

        async def mock_wait_for_dm_readiness(p: Any) -> tuple[Any, Any, bool]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First attempt fails with stalled renderer
                raise TikTokRendererUnresponsiveError("Renderer hung")
            # Second attempt succeeds on replacement page
            return (MagicMock(), MagicMock(), False)

        service.wait_for_dm_readiness = mock_wait_for_dm_readiness  # type: ignore[method-assign]

        final_page, readiness = await service._load_messages_with_renderer_recovery(page1, caller="Test")

        # Persistent context must NOT have died at any point
        self.assertFalse(ctx.is_closed())
        self.assertTrue(page1.is_closed())
        self.assertFalse(final_page.is_closed())
        self.assertIsNot(final_page, page1)
        self.assertEqual(call_count, 2)

    async def test_renderer_recovery_keeps_old_page_intact_if_replacement_creation_fails(self) -> None:
        """P1: If replacement page allocation fails, old page must not be closed."""
        service = RecipientService(dm_load_attempts=2)

        ctx = MockContext(is_closed=False)
        page1 = await ctx.new_page()

        async def fail_new_page() -> Any:
            raise RuntimeError("Out of memory while creating page")

        ctx.new_page = fail_new_page  # type: ignore[method-assign]

        async def mock_wait_for_dm_readiness(p: Any) -> tuple[Any, Any, bool]:
            raise TikTokRendererUnresponsiveError("Renderer hung")

        service.wait_for_dm_readiness = mock_wait_for_dm_readiness  # type: ignore[method-assign]

        with self.assertRaises(TikTokDMReadinessError):
            await service._load_messages_with_renderer_recovery(page1, caller="Test")

        # Original page must remain intact (not prematurely closed)
        self.assertFalse(page1.is_closed())


if __name__ == "__main__":
    unittest.main()
