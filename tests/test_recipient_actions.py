from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from daily_streak import recipient_actions
from daily_streak.browser import BrowserSession


class RecipientActionLifecycleTests(unittest.TestCase):
    def test_resolve_recipients_uses_one_shared_page_operation_for_multiple_targets(self) -> None:
        page = object()
        run_calls: list[object] = []
        service_instances: list[object] = []

        class FakeService:
            def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
                self.pages: list[object] = []
                self.kwargs = kwargs
                service_instances.append(self)

            async def resolve_usernames(self, received_page, recipients, **kwargs):  # type: ignore[no-untyped-def]
                self.pages.append(received_page)
                self.recipients = list(recipients)
                return [
                    {"name": "Alice", "username": "alice"},
                    {"name": "Bob", "username": "bob"},
                ]

        async def run_with_one_page(config, operation, **kwargs):  # type: ignore[no-untyped-def]
            run_calls.append(config)
            return await operation(page)

        with patch.object(
            recipient_actions,
            "_run_with_recipient_page",
            new=run_with_one_page,
        ), patch("daily_streak.recipients.RecipientService", FakeService):
            resolved = asyncio.run(
                recipient_actions.resolve_recipients(
                    {
                        "tiktok": {
                            "dm_readiness_timeout_ms": 120_000,
                            "dm_load_attempts": 1,
                        }
                    },
                    [
                        {"name": "Alice", "username": ""},
                        {"name": "Bob", "username": ""},
                    ],
                )
            )

        self.assertEqual(len(run_calls), 1)
        self.assertEqual(len(service_instances), 1)
        self.assertEqual(service_instances[0].kwargs["dm_readiness_timeout_ms"], 120_000)  # type: ignore[attr-defined]
        self.assertEqual(service_instances[0].kwargs["dm_load_attempts"], 1)  # type: ignore[attr-defined]
        self.assertEqual(service_instances[0].pages, [page])  # type: ignore[attr-defined]
        self.assertEqual(
            resolved,
            [
                {"name": "Alice", "username": "alice"},
                {"name": "Bob", "username": "bob"},
            ],
        )

    def test_context_closes_when_tui_recipient_operation_fails(self) -> None:
        class Page:
            def __init__(self) -> None:
                self.closed = False

            async def close(self) -> None:
                self.closed = True

        class Context:
            def __init__(self) -> None:
                self.pages = [Page()]
                self.closed = False

            async def close(self) -> None:
                self.closed = True

        class PlaywrightContext:
            def __init__(self, playwright: object) -> None:
                self.playwright = playwright

            async def __aenter__(self) -> object:
                return self.playwright

            async def __aexit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
                return None

        context = Context()
        original_launch = recipient_actions.launch_browser_session

        async def launch(playwright, config):  # type: ignore[no-untyped-def]
            return BrowserSession(
                context=context,
                browser=None,
                persistent_context=True,
                page=context.pages[0],
            )

        async def fail(page):  # type: ignore[no-untyped-def]
            raise RuntimeError("TikTok operation failed")

        recipient_actions.launch_browser_session = launch  # type: ignore[assignment]
        try:
            with self.assertRaisesRegex(RuntimeError, "TikTok operation failed"):
                asyncio.run(
                    recipient_actions._run_with_recipient_page(
                        {"cookie_file": "test-missing-cookie-export.json"},
                        fail,
                        playwright_factory=lambda: PlaywrightContext(object()),
                    )
                )
        finally:
            recipient_actions.launch_browser_session = original_launch  # type: ignore[assignment]

        self.assertTrue(context.pages[0].closed)
        self.assertTrue(context.closed)

    def test_refresh_streaks_with_api_reader_and_factory(self) -> None:
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from daily_streak.streak_reader import StreakRecord, StreakStatus

        with TemporaryDirectory() as tmp_dir:
            config = {
                "data_dir": tmp_dir,
                "recipients": [{"name": "Test User", "username": "testuser"}],
            }

            class MockReader:
                def __init__(self, cache, target_device=None):
                    self.cache = cache
                    self.target_device = target_device

                def refresh_recipients(self, recipients, **kwargs):
                    return [
                        StreakRecord(
                            username="testuser",
                            status=StreakStatus.AVAILABLE,
                            count=42,
                        )
                    ]

            res = asyncio.run(
                recipient_actions.refresh_streaks(
                    config,
                    [{"name": "Test User", "username": "testuser"}],
                    reader_factory=lambda cache, dev: MockReader(cache, dev),
                )
            )

            self.assertEqual(len(res), 1)
            self.assertEqual(res[0]["username"], "testuser")
            self.assertEqual(res[0]["streak"]["count"], 42)
            self.assertIn("42 ngày", res[0]["streak_display"])

    def test_refresh_streaks_empty_recipients_returns_empty(self) -> None:
        res = asyncio.run(recipient_actions.refresh_streaks({}, []))
        self.assertEqual(res, [])
