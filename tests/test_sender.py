from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from daily_streak.history import RunHistoryStore
from daily_streak.models import DeliveryStatus, RecipientResult, RecipientStatus, RunSummary
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

    async def extract_username_from_chat(self, page: object) -> str | None:
        return None


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
            sender._share_video_card = AsyncMock()
            sender._wait_for_delivery_evidence = AsyncMock()
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


class MockShareElement:
    def __init__(self, text: str = "", count_val: int = 1, visible: bool = True) -> None:
        self.text = text
        self.count_val = count_val
        self.visible = visible
        self.clicked = False
        self.typed_values: list[str] = []
        self.filled_values: list[str] = []

    async def count(self) -> int:
        return self.count_val

    async def is_visible(self) -> bool:
        return self.visible

    async def wait_for(self, state: str = "visible", timeout: int | None = None) -> None:
        pass

    async def click(self, timeout: int | None = None) -> None:
        self.clicked = True

    async def type(self, value: str) -> None:
        self.typed_values.append(value)

    async def fill(self, value: str) -> None:
        self.filled_values.append(value)

    async def inner_text(self, timeout: int | None = None) -> str:
        return self.text

    @property
    def first(self) -> "MockShareElement":
        return self


class MockVideoPage:
    def __init__(self, friend_names: list[str] | None = None) -> None:
        self.goto_calls: list[str] = []
        self.share_btn = MockShareElement()
        self.search_nav_btn = MockShareElement()
        self.search_input = MockShareElement()
        self.send_btn = MockShareElement()
        self.friend_items = [
            MockShareElement(text=name) for name in (friend_names or ["Khoa Đức @khoa_memezlol"])
        ]
        self.avatar_btns: dict[str, MockShareElement] = {}

    async def goto(self, url: str, wait_until: str = "commit", timeout: int | None = None) -> None:
        self.goto_calls.append(url)

    def locator(self, selector: str) -> object:
        if "share-icon" in selector:
            return self.share_btn
        if ".TUXModalNavBar-leading button" in selector or "flip-rtl" in selector:
            return self.search_nav_btn
        if "TUXTextInputCore-input" in selector or "Search" in selector:
            return self.search_input
        if "DivUserItemContainer" in selector:
            containers = MagicMock()
            containers.count = AsyncMock(return_value=len(self.friend_items))
            containers.nth = MagicMock(side_effect=lambda idx: self.friend_items[idx])
            return containers
        if "StyledSendButton" in selector or "Send" in selector:
            return self.send_btn
        if "share-" in selector:
            for k, btn in self.avatar_btns.items():
                if f"share-{k}" in selector:
                    return btn
            return MockShareElement(count_val=0, visible=False)
        return MockShareElement(count_val=0, visible=False)


class NativeVideoCardSenderTests(unittest.TestCase):
    def test_default_message_format_and_fallback(self) -> None:
        config: dict[str, object] = {"recipients": []}
        with TemporaryDirectory() as tmp_dir:
            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
            )
            self.assertEqual(sender.message_format, "native_video_card")
            self.assertFalse(sender.allow_raw_url_fallback)

    def test_configured_message_format_and_fallback(self) -> None:
        config: dict[str, object] = {
            "tiktok": {
                "message_format": "raw_url",
                "allow_raw_url_fallback": True,
            },
            "recipients": [],
        }
        with TemporaryDirectory() as tmp_dir:
            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
            )
            self.assertEqual(sender.message_format, "raw_url")
            self.assertTrue(sender.allow_raw_url_fallback)

    def test_send_recipient_once_dispatches_to_native_card_flow(self) -> None:
        config: dict[str, object] = {
            "tiktok": {"message_format": "native_video_card"},
            "recipients": [{"name": "Khoa Đức", "username": "khoa_memezlol"}],
        }
        with TemporaryDirectory() as tmp_dir:
            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
            )
            sender._send_native_card_flow = AsyncMock()
            sender._send_raw_url_flow = AsyncMock()

            asyncio.run(
                sender._send_recipient_once(
                    object(),
                    config["recipients"][0],  # type: ignore[index]
                    "https://www.tiktok.com/@creator/video/123",
                )
            )

            sender._send_native_card_flow.assert_awaited_once()
            sender._send_raw_url_flow.assert_not_awaited()

    def test_send_recipient_once_dispatches_to_raw_url_flow(self) -> None:
        config: dict[str, object] = {
            "tiktok": {"message_format": "raw_url"},
            "recipients": [{"name": "Khoa Đức", "username": "khoa_memezlol"}],
        }
        with TemporaryDirectory() as tmp_dir:
            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
            )
            sender._send_native_card_flow = AsyncMock()
            sender._send_raw_url_flow = AsyncMock()

            asyncio.run(
                sender._send_recipient_once(
                    object(),
                    config["recipients"][0],  # type: ignore[index]
                    "https://www.tiktok.com/@creator/video/123",
                )
            )

            sender._send_raw_url_flow.assert_awaited_once()
            sender._send_native_card_flow.assert_not_awaited()

    def test_send_recipient_once_fallback_when_enabled(self) -> None:
        config: dict[str, object] = {
            "tiktok": {
                "message_format": "native_video_card",
                "allow_raw_url_fallback": True,
            },
            "recipients": [{"name": "Khoa Đức", "username": "khoa_memezlol"}],
        }
        with TemporaryDirectory() as tmp_dir:
            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
            )
            sender._send_native_card_flow = AsyncMock(
                side_effect=DeliveryConfirmedNotSentError("recipient missing in modal")
            )
            sender._send_raw_url_flow = AsyncMock()

            with self.assertLogs("daily_streak.sender", level="WARNING"):
                asyncio.run(
                    sender._send_recipient_once(
                        object(),
                        config["recipients"][0],  # type: ignore[index]
                        "https://www.tiktok.com/@creator/video/123",
                    )
                )

            sender._send_native_card_flow.assert_awaited_once()
            sender._send_raw_url_flow.assert_awaited_once()

    def test_send_recipient_once_no_fallback_when_disabled(self) -> None:
        config: dict[str, object] = {
            "tiktok": {
                "message_format": "native_video_card",
                "allow_raw_url_fallback": False,
            },
            "recipients": [{"name": "Khoa Đức", "username": "khoa_memezlol"}],
        }
        with TemporaryDirectory() as tmp_dir:
            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
            )
            sender._send_native_card_flow = AsyncMock(
                side_effect=DeliveryConfirmedNotSentError("recipient missing in modal")
            )
            sender._send_raw_url_flow = AsyncMock()

            with self.assertRaises(DeliveryConfirmedNotSentError):
                asyncio.run(
                    sender._send_recipient_once(
                        object(),
                        config["recipients"][0],  # type: ignore[index]
                        "https://www.tiktok.com/@creator/video/123",
                    )
                )

            sender._send_native_card_flow.assert_awaited_once()
            sender._send_raw_url_flow.assert_not_awaited()

    def test_share_video_card_success(self) -> None:
        config: dict[str, object] = {"recipients": []}
        with TemporaryDirectory() as tmp_dir:
            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
            )
            sender._handle_tiktok_interruptions = AsyncMock()
            page = MockVideoPage(["Khoa Đức @khoa_memezlol"])

            with patch("asyncio.sleep", new_callable=AsyncMock):
                asyncio.run(
                    sender._share_video_card(
                        page,
                        {"name": "Khoa Đức", "username": "khoa_memezlol"},
                        "https://www.tiktok.com/@user/video/7692053355551526162",
                        "7692053355551526162",
                    )
                )

            self.assertEqual(
                page.goto_calls,
                ["https://www.tiktok.com/@user/video/7692053355551526162"],
            )
            self.assertTrue(page.share_btn.clicked)
            self.assertTrue(page.search_nav_btn.clicked)
            self.assertIn("khoa_memezlol", page.search_input.typed_values)
            self.assertTrue(page.friend_items[0].clicked)
            self.assertTrue(page.send_btn.clicked)

    def test_share_video_card_friend_not_found_raises_not_sent(self) -> None:
        config: dict[str, object] = {"recipients": []}
        with TemporaryDirectory() as tmp_dir:
            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
            )
            sender._handle_tiktok_interruptions = AsyncMock()
            page = MockVideoPage(["Someone Else @someone_else"])

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(DeliveryConfirmedNotSentError):
                    asyncio.run(
                        sender._share_video_card(
                            page,
                            {"name": "Khoa Đức", "username": "khoa_memezlol"},
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            "7692053355551526162",
                        )
                    )

            self.assertTrue(page.share_btn.clicked)
            self.assertFalse(page.send_btn.clicked)

    def test_share_video_card_share_button_failure_raises_not_sent(self) -> None:
        config: dict[str, object] = {"recipients": []}
        with TemporaryDirectory() as tmp_dir:
            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
            )
            sender._handle_tiktok_interruptions = AsyncMock()
            page = MockVideoPage()
            page.share_btn.click = AsyncMock(side_effect=RuntimeError("share btn detached"))

            with self.assertRaises(DeliveryConfirmedNotSentError):
                asyncio.run(
                    sender._share_video_card(
                        page,
                        {"name": "Khoa Đức", "username": "khoa_memezlol"},
                        "https://www.tiktok.com/@user/video/7692053355551526162",
                        "7692053355551526162",
                    )
                )

            self.assertFalse(page.send_btn.clicked)

    def test_share_video_card_send_button_failure_raises_not_sent(self) -> None:
        config: dict[str, object] = {"recipients": []}
        with TemporaryDirectory() as tmp_dir:
            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
            )
            sender._handle_tiktok_interruptions = AsyncMock()
            page = MockVideoPage(["Khoa Đức @khoa_memezlol"])
            page.send_btn.click = AsyncMock(side_effect=RuntimeError("send btn detached"))

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(DeliveryConfirmedNotSentError):
                    asyncio.run(
                        sender._share_video_card(
                            page,
                            {"name": "Khoa Đức", "username": "khoa_memezlol"},
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            "7692053355551526162",
                        )
                    )

    def test_wait_for_delivery_evidence_verified_on_native_card(self) -> None:
        config: dict[str, object] = {
            "tiktok": {"action_timeout_ms": 1000},
            "recipients": [],
        }
        with TemporaryDirectory() as tmp_dir:
            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
            )
            sender._handle_tiktok_interruptions = AsyncMock()
            page = MockDeliveryPage(
                evaluate_result={
                    "verified": True,
                    "matched_by": "native_video_card",
                    "matched_item_id": "7692053355551526162",
                }
            )

            # Should return immediately without exception
            asyncio.run(
                sender._wait_for_delivery_evidence(
                    page,
                    "https://www.tiktok.com/@user/video/7692053355551526162",
                    initial_item_count=0,
                    expected_username="khoa_memezlol",
                    expected_video_id="7692053355551526162",
                )
            )

    def test_wait_for_delivery_evidence_times_out_raises_unconfirmed(self) -> None:
        config: dict[str, object] = {
            "tiktok": {"action_timeout_ms": 100},
            "recipients": [],
        }
        with TemporaryDirectory() as tmp_dir:
            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
            )
            sender._handle_tiktok_interruptions = AsyncMock()
            page = MockDeliveryPage(
                evaluate_result={
                    "verified": False,
                    "current_count": 0,
                    "composer_cleared": False,
                }
            )

            with self.assertLogs("daily_streak.sender", level="WARNING"):
                with self.assertRaises(DeliveryUnconfirmedError):
                    asyncio.run(
                        sender._wait_for_delivery_evidence(
                            page,
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            initial_item_count=0,
                            expected_username="khoa_memezlol",
                            expected_video_id="7692053355551526162",
                        )
                    )


class AutoRefreshStreaksTests(unittest.TestCase):
    def test_auto_refresh_streaks_called_when_batch_has_attempted_sends(self) -> None:
        config: dict[str, Any] = {
            "recipients": [{"name": "User 1", "username": "user1"}],
        }
        with TemporaryDirectory() as tmp_dir:
            refresher = AsyncMock()
            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
                streak_refresher=refresher,
            )

            # Case: at least one attempted send (e.g. SUCCESS)
            summary = RunSummary()
            summary.add_result(RecipientResult(recipient_name="User 1", username="user1", status=RecipientStatus.SUCCESS))

            asyncio.run(sender._auto_refresh_streaks(summary))
            refresher.assert_called_once_with(config, sender.recipients)

    def test_auto_refresh_streaks_skipped_when_all_skipped(self) -> None:
        config: dict[str, Any] = {
            "recipients": [{"name": "User 1", "username": "user1"}],
        }
        with TemporaryDirectory() as tmp_dir:
            refresher = AsyncMock()
            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
                streak_refresher=refresher,
            )

            # Case: all recipients skipped (e.g. already completed today)
            summary = RunSummary()
            summary.add_result(RecipientResult(recipient_name="User 1", username="user1", status=RecipientStatus.SKIPPED))

            asyncio.run(sender._auto_refresh_streaks(summary))
            refresher.assert_not_called()

    def test_auto_refresh_streaks_skipped_on_setup_error(self) -> None:
        config: dict[str, Any] = {
            "recipients": [{"name": "User 1", "username": "user1"}],
        }
        with TemporaryDirectory() as tmp_dir:
            refresher = AsyncMock()
            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
                streak_refresher=refresher,
            )

            # Case: setup error occurred before sending
            summary = RunSummary(setup_error="Failed to launch browser")
            summary.add_result(RecipientResult(recipient_name="User 1", username="user1", status=RecipientStatus.FAILED))

            asyncio.run(sender._auto_refresh_streaks(summary))
            refresher.assert_not_called()

    def test_auto_refresh_streaks_failure_is_non_fatal(self) -> None:
        config: dict[str, Any] = {
            "recipients": [{"name": "User 1", "username": "user1"}],
        }
        with TemporaryDirectory() as tmp_dir:
            refresher = AsyncMock(side_effect=RuntimeError("Streak API unreachable"))
            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
                streak_refresher=refresher,
            )

            summary = RunSummary()
            summary.add_result(RecipientResult(recipient_name="User 1", username="user1", status=RecipientStatus.SUCCESS))

            # Must not raise an exception
            asyncio.run(sender._auto_refresh_streaks(summary))
            refresher.assert_called_once()
            # Summary is unchanged and not failed
            self.assertIsNone(summary.setup_error)
            self.assertEqual(summary.success_count, 1)

    def test_send_daily_links_invokes_auto_refresh_after_session_close(self) -> None:
        config: dict[str, Any] = {
            "recipients": [{"name": "User 1", "username": "user1"}],
        }
        with TemporaryDirectory() as tmp_dir:
            refresher = AsyncMock()
            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
                streak_refresher=refresher,
            )

            events = []

            class FakeSession:
                def __init__(self):
                    self.context = MagicMock()
                async def load_cookies(self, cookie_file):
                    return True
                async def get_page(self):
                    return MagicMock()
                async def close(self):
                    events.append("session_close")

            class FakePlaywrightContext:
                async def __aenter__(self):
                    return MagicMock()
                async def __aexit__(self, *args):
                    pass

            async def fake_send_recipients(context, summary, **kwargs):
                events.append("send_recipients")
                summary.add_result(RecipientResult(recipient_name="User 1", username="user1", status=RecipientStatus.SUCCESS))
                return summary

            async def fake_auto_refresh(summary):
                events.append("auto_refresh")
                await refresher(config, sender.recipients)

            sender.send_recipients = fake_send_recipients
            sender._auto_refresh_streaks = fake_auto_refresh
            sender._ensure_logged_in = AsyncMock()

            with patch("daily_streak.sender.check_cookies_valid", return_value=True), \
                 patch("playwright.async_api.async_playwright", FakePlaywrightContext), \
                 patch("daily_streak.sender.launch_browser_session", AsyncMock(return_value=FakeSession())):
                summary = asyncio.run(sender.send_daily_links())

            self.assertEqual(events, ["send_recipients", "session_close", "auto_refresh"])
            self.assertEqual(summary.success_count, 1)
            refresher.assert_called_once()


class MockReusablePage:
    def __init__(self, name: str = "dm_page") -> None:
        self.name = name
        self.closed = False
        self.url = "https://www.tiktok.com/messages"

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.closed = True

    def locator(self, selector: str) -> MockLocator:
        return MockLocator(0, "")

    async def evaluate(self, script: object, args: object = None) -> object:
        return {"verified": True, "matched_by": "native_video_card"}


class MockReusableContext:
    def __init__(self) -> None:
        self.pages_created: list[MockReusablePage] = []

    async def new_page(self) -> MockReusablePage:
        page = MockReusablePage(f"page_{len(self.pages_created)}")
        self.pages_created.append(page)
        return page


class PageReuseAndPacingTests(unittest.TestCase):
    def test_send_recipients_reuses_dm_page_across_multiple_recipients(self) -> None:
        config: dict[str, Any] = {
            "tiktok": {"recipient_retries": 0, "message_delay_seconds": [0, 0]},
            "recipients": [
                {"name": "Alice", "username": "alice"},
                {"name": "Bob", "username": "bob"},
            ],
        }
        with TemporaryDirectory() as tmp_dir:
            shared_page = MockReusablePage("shared_dm_page")
            selection_calls: list[dict[str, Any]] = []

            class CustomRecipientService:
                async def select_recipient_for_username_with_page(
                    self,
                    page: Any,
                    username: str,
                    notifier: Any,
                    *,
                    configured_display_name: str | None = None,
                    reuse_existing_page: bool = False,
                ) -> tuple[Any, str]:
                    selection_calls.append({
                        "page": page,
                        "username": username,
                        "reuse_existing_page": reuse_existing_page,
                    })
                    return page, configured_display_name or username

                async def extract_username_from_chat(self, page: Any) -> str | None:
                    # Return expected username to verify chat header matches
                    return selection_calls[-1]["username"]

            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                recipient_service=CustomRecipientService(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
            )
            sender._handle_tiktok_interruptions = AsyncMock()
            sender._share_video_card = AsyncMock()
            sender._wait_for_delivery_evidence = AsyncMock()
            sender._sleep_between_messages = AsyncMock()

            context = MockReusableContext()
            summary = asyncio.run(
                sender.send_recipients(context, dm_page=shared_page)
            )

            self.assertEqual(summary.success_count, 2)
            self.assertEqual(len(selection_calls), 2)
            # Both recipients used the shared page
            self.assertIs(selection_calls[0]["page"], shared_page)
            self.assertIs(selection_calls[1]["page"], shared_page)
            # Second recipient reuses the existing page
            self.assertTrue(selection_calls[1]["reuse_existing_page"])
            # Page was NOT closed between recipients
            self.assertFalse(shared_page.closed)

    def test_send_recipients_recovers_when_retained_page_is_closed(self) -> None:
        config: dict[str, Any] = {
            "tiktok": {"recipient_retries": 0, "message_delay_seconds": [0, 0]},
            "recipients": [
                {"name": "Alice", "username": "alice"},
                {"name": "Bob", "username": "bob"},
            ],
        }
        with TemporaryDirectory() as tmp_dir:
            shared_page = MockReusablePage("shared_dm_page")
            selection_calls: list[Any] = []
            pages_closed_during_selection: list[bool] = []

            class CustomRecipientService:
                async def select_recipient_for_username_with_page(
                    self,
                    page: Any,
                    username: str,
                    notifier: Any,
                    *,
                    configured_display_name: str | None = None,
                    reuse_existing_page: bool = False,
                ) -> tuple[Any, str]:
                    selection_calls.append(page)
                    pages_closed_during_selection.append(page.is_closed())
                    # Simulate page closing right after first recipient completes
                    if username == "alice":
                        page.closed = True
                    return page, configured_display_name or username

                async def extract_username_from_chat(self, page: Any) -> str | None:
                    return None

            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                recipient_service=CustomRecipientService(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
            )
            sender._handle_tiktok_interruptions = AsyncMock()
            sender._share_video_card = AsyncMock()
            sender._wait_for_delivery_evidence = AsyncMock()
            sender._sleep_between_messages = AsyncMock()

            context = MockReusableContext()
            summary = asyncio.run(
                sender.send_recipients(context, dm_page=shared_page)
            )

            self.assertEqual(summary.success_count, 2)
            self.assertEqual(len(selection_calls), 2)
            # First recipient used shared_page
            self.assertIs(selection_calls[0], shared_page)
            # Second recipient received a fresh page because shared_page was closed
            self.assertIsNot(selection_calls[1], shared_page)
            # Neither page was closed when handed to recipient selection
            self.assertEqual(pages_closed_during_selection, [False, False])

    def test_send_recipients_resets_page_on_send_failure(self) -> None:
        config: dict[str, Any] = {
            "tiktok": {"recipient_retries": 0, "message_delay_seconds": [0, 0]},
            "recipients": [
                {"name": "Alice", "username": "alice"},
                {"name": "Bob", "username": "bob"},
            ],
        }
        with TemporaryDirectory() as tmp_dir:
            shared_page = MockReusablePage("shared_dm_page")
            selection_calls: list[Any] = []

            class CustomRecipientService:
                async def select_recipient_for_username_with_page(
                    self,
                    page: Any,
                    username: str,
                    notifier: Any,
                    *,
                    configured_display_name: str | None = None,
                    reuse_existing_page: bool = False,
                ) -> tuple[Any, str]:
                    selection_calls.append(page)
                    return page, configured_display_name or username

                async def extract_username_from_chat(self, page: Any) -> str | None:
                    return None

            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                recipient_service=CustomRecipientService(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
            )
            sender._handle_tiktok_interruptions = AsyncMock()
            sender._share_video_card = AsyncMock(
                side_effect=[RuntimeError("video share failed"), None]
            )
            sender._wait_for_delivery_evidence = AsyncMock()
            sender._sleep_between_messages = AsyncMock()

            context = MockReusableContext()
            with self.assertLogs("daily_streak.sender", level="ERROR"):
                summary = asyncio.run(
                    sender.send_recipients(context, dm_page=shared_page)
                )

            self.assertEqual(summary.failed_count, 1)
            self.assertEqual(summary.success_count, 1)
            # Initial shared page was closed upon Alice's failure to prevent dirty state
            self.assertTrue(shared_page.closed)
            # Bob received a fresh page
            self.assertIsNot(selection_calls[1], shared_page)

    def test_chat_header_mismatch_prevents_sending_to_wrong_recipient(self) -> None:
        config: dict[str, Any] = {
            "tiktok": {"recipient_retries": 0},
            "recipients": [{"name": "Alice", "username": "alice"}],
        }
        with TemporaryDirectory() as tmp_dir:
            page = MockReusablePage()

            class MismatchedHeaderService:
                async def select_recipient_for_username_with_page(
                    self,
                    page: Any,
                    username: str,
                    notifier: Any,
                    **kwargs: Any,
                ) -> tuple[Any, str]:
                    return page, "Alice"

                async def extract_username_from_chat(self, page: Any) -> str:
                    # TikTok chat remained on 'carol' instead of 'alice'!
                    return "carol"

            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                recipient_service=MismatchedHeaderService(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
            )
            sender._handle_tiktok_interruptions = AsyncMock()
            sender._share_video_card = AsyncMock()

            context = MockReusableContext()
            with self.assertRaises(DeliveryConfirmedNotSentError) as ctx:
                asyncio.run(
                    sender._send_native_card_flow(
                        context,
                        config["recipients"][0],
                        "https://www.tiktok.com/@creator/video/123",
                        "alice",
                        dm_page=page,
                    )
                )

            self.assertIn("does not match expected @alice", str(ctx.exception))
            # Share card was never attempted!
            sender._share_video_card.assert_not_awaited()

    def test_timing_logs_emitted_during_send(self) -> None:
        config: dict[str, Any] = {
            "tiktok": {"recipient_retries": 0, "message_delay_seconds": [1, 1]},
            "recipients": [{"name": "Alice", "username": "alice"}],
        }
        with TemporaryDirectory() as tmp_dir:
            page = MockReusablePage()

            class FastRecipientService:
                async def select_recipient_for_username_with_page(
                    self, page: Any, username: str, notifier: Any, **kwargs: Any
                ) -> tuple[Any, str]:
                    return page, "Alice"

                async def extract_username_from_chat(self, page: Any) -> str:
                    return "alice"

            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                recipient_service=FastRecipientService(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
            )
            sender._handle_tiktok_interruptions = AsyncMock()
            sender._share_video_card = AsyncMock()
            sender._wait_for_delivery_evidence = AsyncMock()

            context = MockReusableContext()
            with self.assertLogs("daily_streak.sender", level="INFO") as logs:
                asyncio.run(
                    sender.send_recipients(context, dm_page=page)
                )

            log_output = "\n".join(logs.output)
            self.assertIn("Timing: Conversation selection and composer readiness took", log_output)
            self.assertIn("Timing: Video share operation took", log_output)
            self.assertIn("Timing: Inter-message delay slept for", log_output)
