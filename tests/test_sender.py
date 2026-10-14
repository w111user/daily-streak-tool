from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock

from daily_streak.history import RunHistoryStore
from daily_streak.models import DeliveryStatus, RecipientResult, RecipientStatus
from daily_streak.sender import (
    DeliveryConfirmedNotSentError,
    DeliveryUnconfirmedError,
    TikTokSender,
    extract_video_identifier,
)
from daily_streak.state import DailyStateStore


class FakeNotifier:
    def desktop(self, title: str, message: str) -> None:
        pass

    async def telegram(self, message: str) -> None:
        pass

    async def captcha_alert(self, page_url: str) -> None:
        pass


class RecordingVideoPool:
    def __init__(self) -> None:
        self.calls = 0

    def random_video(self) -> str:
        self.calls += 1
        return "https://video.from.pool/example"


class ControlledSender(TikTokSender):
    def __init__(
        self,
        config: dict[str, object],
        outcomes: dict[str, list[Exception | None]],
        data_dir: Path,
    ) -> None:
        self.recording_pool = RecordingVideoPool()
        super().__init__(
            config,
            FakeNotifier(),
            self.recording_pool,  # type: ignore[arg-type]
            state_store=DailyStateStore(data_dir),
            history_store=RunHistoryStore(data_dir),
            now_provider=lambda: datetime(2026, 10, 11, tzinfo=timezone.utc),
        )
        self.outcomes = defaultdict(list, outcomes)
        self.send_calls: list[tuple[str, str]] = []

    async def _send_recipient_once(
        self,
        context: object,
        recipient: dict[str, object],
        selected_video: str,
    ) -> None:
        username = str(recipient["username"])
        self.send_calls.append((username, selected_video))
        outcome = self.outcomes[username].pop(0)
        if outcome:
            raise outcome

    async def _sleep_between_messages(self) -> None:
        pass


class RecipientSelectionRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str, object, str | None]] = []

    async def select_recipient_for_username_with_page(
        self,
        page: object,
        username: str,
        notifier: object,
        *,
        configured_display_name: str | None = None,
    ) -> tuple[object, str]:
        self.calls.append((page, username, notifier, configured_display_name))
        return page, configured_display_name or ""


class SenderPage:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class SenderContext:
    def __init__(self) -> None:
        self.page = SenderPage()

    async def new_page(self) -> SenderPage:
        return self.page


class SenderBatchTests(unittest.TestCase):
    def test_sender_passes_dm_readiness_budget_and_load_attempts_to_shared_service(self) -> None:
        config: dict[str, object] = {
            "tiktok": {
                "dm_readiness_timeout_ms": 120_000,
                "dm_load_attempts": 1,
                "recipient_retries": 0,
            },
            "recipients": [],
        }
        with TemporaryDirectory() as temporary_directory:
            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(temporary_directory)),
                history_store=RunHistoryStore(Path(temporary_directory)),
            )

        self.assertEqual(sender.recipient_service.dm_readiness_timeout_ms, 120_000)
        self.assertEqual(sender.recipient_service.dm_load_attempts, 1)
        self.assertEqual(sender._recipient_attempts_allowed(), 1)

    def test_sender_passes_configured_display_name_to_recipient_selection(self) -> None:
        config: dict[str, object] = {
            "recipients": [{"name": "Khoa Đức", "username": "khoa_memezlol"}],
        }
        recorder = RecipientSelectionRecorder()
        with TemporaryDirectory() as temporary_directory:
            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                recipient_service=recorder,  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(temporary_directory)),
                history_store=RunHistoryStore(Path(temporary_directory)),
            )
            sender._handle_tiktok_interruptions = AsyncMock()
            sender._send_message = AsyncMock()
            context = SenderContext()

            asyncio.run(
                sender._send_recipient_once(
                    context,
                    config["recipients"][0],  # type: ignore[index]
                    "https://video.from.pool/example",
                )
            )

        self.assertEqual(len(recorder.calls), 1)
        _, username, notifier, configured_display_name = recorder.calls[0]
        self.assertEqual(username, "khoa_memezlol")
        self.assertIsInstance(notifier, FakeNotifier)
        self.assertEqual(configured_display_name, "Khoa Đức")
        self.assertTrue(context.page.closed)

    def test_sender_reports_persisted_recipient_results_to_optional_progress_callback(self) -> None:
        config: dict[str, object] = {
            "tiktok": {"recipient_retries": 0},
            "recipients": [{"name": "Alice", "username": "alice"}],
        }
        with TemporaryDirectory() as temporary_directory:
            sender = ControlledSender(config, {"alice": [None]}, Path(temporary_directory))
            progress: list[tuple[RecipientStatus, int]] = []
            sender.on_recipient_result = lambda result, summary: progress.append(
                (result.status, summary.success_count)
            )
            summary = asyncio.run(sender.send_recipients(context=object()))

        self.assertEqual(summary.success_count, 1)
        self.assertEqual(progress, [(RecipientStatus.SUCCESS, 1)])

    def test_failed_recipient_does_not_stop_later_recipients(self) -> None:
        config: dict[str, object] = {
            "tiktok": {"recipient_retries": 0},
            "recipients": [
                {"name": "Alice", "username": "alice"},
                {"name": "Bob", "username": "bob"},
                {"name": "Carol", "username": "carol"},
            ],
        }
        with TemporaryDirectory() as temporary_directory:
            sender = ControlledSender(
                config,
                {
                    "alice": [None],
                    "bob": [RuntimeError("conversation missing")],
                    "carol": [None],
                },
                Path(temporary_directory),
            )

            with self.assertLogs("daily_streak.sender", level="ERROR"):
                summary = asyncio.run(sender.send_recipients(context=object()))

        self.assertEqual(
            [result.status for result in summary.results],
            [RecipientStatus.SUCCESS, RecipientStatus.FAILED, RecipientStatus.SUCCESS],
        )
        self.assertEqual(summary.success_count, 2)
        self.assertEqual(summary.failed_count, 1)
        self.assertEqual([username for username, _ in sender.send_calls], ["alice", "bob", "carol"])
        self.assertEqual(sender.recording_pool.calls, 3)
        self.assertTrue(
            all(video == "https://video.from.pool/example" for _, video in sender.send_calls)
        )

    def test_recipient_retry_reuses_the_video_selected_from_video_pool(self) -> None:
        config: dict[str, object] = {
            "tiktok": {"recipient_retries": 1},
            "recipients": [{"name": "Alice", "username": "alice"}],
        }
        with TemporaryDirectory() as temporary_directory:
            sender = ControlledSender(
                config,
                {"alice": [RuntimeError("temporary error"), None]},
                Path(temporary_directory),
            )

            with self.assertLogs("daily_streak.sender", level="ERROR"):
                summary = asyncio.run(sender.send_recipients(context=object()))

        result = summary.results[0]
        self.assertEqual(result.status, RecipientStatus.SUCCESS)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(sender.recording_pool.calls, 1)
        self.assertEqual(
            sender.send_calls,
            [
                ("alice", "https://video.from.pool/example"),
                ("alice", "https://video.from.pool/example"),
            ],
        )

    def test_extract_video_identifier(self) -> None:
        self.assertEqual(
            extract_video_identifier("https://www.tiktok.com/@user/video/7123456789012345678?is_from_webapp=1"),
            "7123456789012345678",
        )
        self.assertEqual(
            extract_video_identifier("https://vt.tiktok.com/ZSj12345/"),
            "ZSj12345",
        )
        self.assertEqual(
            extract_video_identifier("https://m.tiktok.com/v/7123456789012345678.html"),
            "7123456789012345678",
        )
        self.assertEqual(
            extract_video_identifier("https://other.domain/video"),
            "https://other.domain/video",
        )

    def test_send_message_verifies_delivery_when_outgoing_bubble_observed(self) -> None:
        config: dict[str, object] = {
            "tiktok": {"action_timeout_ms": 1000},
            "recipients": [],
        }
        with TemporaryDirectory() as temporary_directory:
            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(temporary_directory)),
                history_store=RunHistoryStore(Path(temporary_directory)),
            )
            sender._handle_tiktok_interruptions = AsyncMock()
            input_locator = MockInputLocator()
            sender.recipient_service.wait_for_message_input = AsyncMock(return_value=input_locator)
            sender.recipient_service.extract_username_from_chat = AsyncMock(return_value="alice")

            page = MockDeliveryPage(evaluate_result={"verified": True, "matched_by": "video_id"})
            asyncio.run(
                sender._send_message(
                    page,
                    "https://www.tiktok.com/@creator/video/12345",
                    username="alice",
                )
            )

            self.assertTrue(input_locator.clicked)
            self.assertEqual(page.keyboard.typed, ["https://www.tiktok.com/@creator/video/12345"])
            self.assertEqual(page.keyboard.pressed, ["Enter"])

    def test_send_message_fails_before_submission_when_composer_missing(self) -> None:
        config: dict[str, object] = {
            "tiktok": {"action_timeout_ms": 1000},
            "recipients": [],
        }
        with TemporaryDirectory() as temporary_directory:
            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(temporary_directory)),
                history_store=RunHistoryStore(Path(temporary_directory)),
            )
            sender._handle_tiktok_interruptions = AsyncMock()
            sender.recipient_service.wait_for_message_input = AsyncMock(
                side_effect=RuntimeError("composer missing")
            )

            page = MockDeliveryPage()
            with self.assertRaises(DeliveryConfirmedNotSentError):
                asyncio.run(
                    sender._send_message(
                        page,
                        "https://www.tiktok.com/@creator/video/12345",
                        username="alice",
                    )
                )

            self.assertEqual(page.keyboard.pressed, [])

    def test_send_message_raises_unconfirmed_when_enter_pressed_but_bubble_not_observed(self) -> None:
        config: dict[str, object] = {
            "tiktok": {"action_timeout_ms": 100},  # 100ms for fast test timeout
            "recipients": [],
        }
        with TemporaryDirectory() as temporary_directory:
            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(temporary_directory)),
                history_store=RunHistoryStore(Path(temporary_directory)),
            )
            sender._handle_tiktok_interruptions = AsyncMock()
            input_locator = MockInputLocator()
            sender.recipient_service.wait_for_message_input = AsyncMock(return_value=input_locator)
            sender.recipient_service.extract_username_from_chat = AsyncMock(return_value="alice")

            page = MockDeliveryPage(
                evaluate_result={"verified": False, "composer_cleared": True, "current_count": 0}
            )
            with self.assertLogs("daily_streak.sender", level="WARNING"):
                with self.assertRaises(DeliveryUnconfirmedError):
                    asyncio.run(
                        sender._send_message(
                            page,
                            "https://www.tiktok.com/@creator/video/12345",
                            username="alice",
                        )
                    )

            self.assertEqual(page.keyboard.pressed, ["Enter"])
            self.assertTrue(len(page.evaluate_calls) > 0)
            self.assertEqual(page.evaluate_calls[0][1]["expectedUsername"], "alice")

    def test_probe_outgoing_message_via_locators_distinguishes_peer_from_outgoing(self) -> None:
        sender = TikTokSender(
            {},
            FakeNotifier(),
            RecordingVideoPool(),  # type: ignore[arg-type]
        )

        class FakeChatItems:
            def __init__(self, count: int, item: Any) -> None:
                self._count = count
                self._item = item

            async def count(self) -> int:
                return self._count

            def nth(self, idx: int) -> Any:
                return self._item

        class FakeChatItem:
            def __init__(self, inner_text: str, has_peer_link: bool) -> None:
                self._inner_text = inner_text
                self._has_peer_link = has_peer_link

            async def inner_text(self) -> str:
                return self._inner_text

            def locator(self, selector: str) -> Any:
                if selector.startswith("a[href*='/@bob']"):
                    count = 1 if self._has_peer_link else 0
                    return MockLocator(count)
                if selector == "[data-e2e='dm-new-shared-video']":
                    return MockLocator(0)
                return MockLocator(0)

        class FakePage:
            def __init__(self, item: Any) -> None:
                self._item = item

            def locator(self, selector: str) -> Any:
                if selector == "[data-e2e='dm-new-chat-item']":
                    return FakeChatItems(1, self._item)
                return MockLocator(0)

        # 1. Incoming message from peer (@bob) containing video link: should NOT be verified
        peer_item = FakeChatItem("https://vt.tiktok.com/ZSCaGfTRS/", has_peer_link=True)
        peer_page = FakePage(peer_item)
        res_peer = asyncio.run(
            sender._probe_outgoing_message_via_locators(
                peer_page,
                expected_video_id="ZSCaGfTRS",
                expected_url="https://vt.tiktok.com/ZSCaGfTRS",
                initial_count=0,
                expected_username="bob",
            )
        )
        self.assertFalse(res_peer["verified"])

        # 2. Outgoing message (no link to peer @bob) containing video link: SHOULD be verified
        outgoing_item = FakeChatItem("https://vt.tiktok.com/ZSCaGfTRS/", has_peer_link=False)
        outgoing_page = FakePage(outgoing_item)
        res_out = asyncio.run(
            sender._probe_outgoing_message_via_locators(
                outgoing_page,
                expected_video_id="ZSCaGfTRS",
                expected_url="https://vt.tiktok.com/ZSCaGfTRS",
                initial_count=0,
                expected_username="bob",
            )
        )
        self.assertTrue(res_out["verified"])
        self.assertEqual(res_out["matched_by"], "text")

    def test_ambiguous_delivery_does_not_trigger_duplicate_send(self) -> None:
        config: dict[str, object] = {
            "tiktok": {"recipient_retries": 2},
            "recipients": [{"name": "Alice", "username": "alice"}],
        }
        with TemporaryDirectory() as temporary_directory:
            sender = ControlledSender(
                config,
                {"alice": [DeliveryUnconfirmedError("timeout verification")]},
                Path(temporary_directory),
            )

            with self.assertLogs("daily_streak.sender", level="WARNING"):
                summary = asyncio.run(sender.send_recipients(context=object()))

            self.assertEqual(summary.unconfirmed_count, 1)
            self.assertEqual(summary.success_count, 0)
            self.assertEqual(summary.failed_count, 0)

            result = summary.results[0]
            self.assertEqual(result.status, RecipientStatus.UNCONFIRMED)
            self.assertEqual(result.delivery_status, DeliveryStatus.UNCONFIRMED)
            self.assertEqual(result.attempts, 1)
            # Crucial: exactly 1 send attempt was performed, no retries triggered
            self.assertEqual(len(sender.send_calls), 1)

    def test_state_and_history_record_success_only_when_delivery_is_verified(self) -> None:
        config: dict[str, object] = {
            "tiktok": {"recipient_retries": 0},
            "recipients": [
                {"name": "Alice", "username": "alice"},
                {"name": "Bob", "username": "bob"},
                {"name": "Carol", "username": "carol"},
            ],
        }
        with TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory)
            sender = ControlledSender(
                config,
                {
                    "alice": [None],  # Verified
                    "bob": [DeliveryUnconfirmedError("ambiguous timeout")],
                    "carol": [DeliveryConfirmedNotSentError("submission failed")],
                },
                data_dir,
            )

            with self.assertLogs("daily_streak.sender", level="WARNING"):
                summary = asyncio.run(sender.send_recipients(context=object()))

            self.assertEqual(summary.success_count, 1)
            self.assertEqual(summary.unconfirmed_count, 1)
            self.assertEqual(summary.failed_count, 1)

            state = sender.state_store.load("2026-10-11", "Asia/Ho_Chi_Minh")
            self.assertTrue(state.is_completed("alice"))
            self.assertFalse(state.is_completed("bob"))
            self.assertFalse(state.is_completed("carol"))

            records = sender.history_store.load()
            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record["success"], 1)
            self.assertEqual(record["unconfirmed"], 1)
            self.assertEqual(record["failed"], 1)

    def test_recipient_retry_continues_for_safe_pre_submission_failures(self) -> None:
        config: dict[str, object] = {
            "tiktok": {"recipient_retries": 1},
            "recipients": [{"name": "Alice", "username": "alice"}],
        }
        with TemporaryDirectory() as temporary_directory:
            sender = ControlledSender(
                config,
                {"alice": [DeliveryConfirmedNotSentError("composer click error"), None]},
                Path(temporary_directory),
            )

            with self.assertLogs("daily_streak.sender", level="ERROR"):
                summary = asyncio.run(sender.send_recipients(context=object()))

            result = summary.results[0]
            self.assertEqual(result.status, RecipientStatus.SUCCESS)
            self.assertEqual(result.delivery_status, DeliveryStatus.VERIFIED)
            self.assertEqual(result.attempts, 2)
            self.assertEqual(len(sender.send_calls), 2)


class MockKeyboard:
    def __init__(self) -> None:
        self.typed: list[str] = []
        self.pressed: list[str] = []
        self.type_error: Exception | None = None
        self.press_error: Exception | None = None

    async def type(self, text: str) -> None:
        if self.type_error:
            raise self.type_error
        self.typed.append(text)

    async def press(self, key: str) -> None:
        if self.press_error:
            raise self.press_error
        self.pressed.append(key)


class MockInputLocator:
    def __init__(self) -> None:
        self.clicked = False

    async def click(self) -> None:
        self.clicked = True


class MockDeliveryPage:
    def __init__(self, evaluate_result: dict[str, object] | None = None) -> None:
        self.keyboard = MockKeyboard()
        self.evaluate_result = evaluate_result or {}
        self.evaluate_calls: list[tuple[object, object]] = []

    def locator(self, selector: str) -> MockLocator:
        return MockLocator(0, "")

    async def evaluate(self, script: object, args: object = None) -> object:
        self.evaluate_calls.append((script, args))
        return self.evaluate_result


class MockLocator:
    def __init__(self, count_val: int = 0, inner_text_val: str = "") -> None:
        self.count_val = count_val
        self.inner_text_val = inner_text_val

    async def count(self) -> int:
        return self.count_val

    async def inner_text(self, timeout: int | None = None) -> str:
        return self.inner_text_val

    def locator(self, selector: str) -> "MockLocator":
        return MockLocator(0, "")

    def nth(self, index: int) -> "MockLocator":
        return MockLocator(0, "")
