from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from daily_streak.history import RunHistoryStore
from daily_streak.models import DeliveryStatus, RecipientResult, RecipientStatus, RunSummary
from daily_streak.sender import (
    DeliveryConfirmedNotSentError,
    DeliveryUnconfirmedError,
    RowSelectionState,
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
        return self.calls[-1][1] if self.calls else None


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
    def __init__(
        self,
        text: str = "",
        count_val: int = 1,
        visible: bool = True,
        attributes: dict[str, str] | None = None,
        profile_links: list[str] | None = None,
        sub_elements: dict[str, list["MockShareElement"]] | None = None,
        auto_check_on_click: bool = True,
    ) -> None:
        self.text = text
        self.count_val = count_val
        self.visible = visible
        self.clicked = False
        self.typed_values: list[str] = []
        self.filled_values: list[str] = []
        self.attributes: dict[str, str] = dict(attributes or {})
        self.auto_check_on_click = auto_check_on_click
        self.sub_elements: dict[str, list[MockShareElement]] = dict(sub_elements or {})

        if profile_links is not None:
            self.profile_links = list(profile_links)
        else:
            self.profile_links = []
            if text:
                m = re.search(r"@([A-Za-z0-9_.-]+)", text)
                if m:
                    self.profile_links.append(f"/@{m.group(1)}")

    async def count(self) -> int:
        return self.count_val

    async def is_visible(self) -> bool:
        return self.visible

    async def wait_for(self, state: str = "visible", timeout: int | None = None) -> None:
        pass

    async def click(self, timeout: int | None = None) -> None:
        self.clicked = True
        if self.auto_check_on_click:
            self.attributes["aria-checked"] = "true"

    async def type(self, value: str) -> None:
        self.typed_values.append(value)

    async def fill(self, value: str) -> None:
        self.filled_values.append(value)

    async def inner_text(self, timeout: int | None = None) -> str:
        return self.text

    async def get_attribute(self, attr: str) -> str | None:
        return self.attributes.get(attr)

    @property
    def first(self) -> "MockShareElement":
        return self

    def nth(self, idx: int) -> "MockShareElement":
        return self

    def locator(self, selector: str) -> Any:
        for sel_key, elements in self.sub_elements.items():
            if sel_key in selector:
                if elements:
                    container = MagicMock()
                    container.count = AsyncMock(return_value=len(elements))
                    container.nth = MagicMock(side_effect=lambda idx: elements[idx])
                    container.first = elements[0]
                    return container
                return MockShareElement(count_val=0, visible=False)

        if "a[href" in selector:
            if self.profile_links:
                link_els = [
                    MockShareElement(attributes={"href": href})
                    for href in self.profile_links
                ]
                container = MagicMock()
                container.count = AsyncMock(return_value=len(link_els))
                container.nth = MagicMock(side_effect=lambda idx: link_els[idx])
                container.first = link_els[0]
                container.evaluate_all = AsyncMock(return_value=self.profile_links)
                return container
            return MockShareElement(count_val=0, visible=False)

        if any(k in selector for k in ("unique-id", "UniqueId", "UserSubTitle", "handle")):
            if self.text and "@" in self.text:
                m = re.search(r"@([A-Za-z0-9_.-]+)", self.text)
                if m:
                    handle_el = MockShareElement(text=f"@{m.group(1)}")
                    container = MagicMock()
                    container.count = AsyncMock(return_value=1)
                    container.nth = MagicMock(return_value=handle_el)
                    container.first = handle_el
                    return container
            return MockShareElement(count_val=0, visible=False)

        return MockShareElement(count_val=0, visible=False)


class MockVideoPage:
    def __init__(
        self,
        friend_names: list[str] | None = None,
        friend_items: list[MockShareElement] | None = None,
        chips: list[MockShareElement] | None = None,
    ) -> None:
        self.goto_calls: list[str] = []
        self.share_btn = MockShareElement()
        self.search_nav_btn = MockShareElement()
        self.search_input = MockShareElement()
        self.send_btn = MockShareElement()
        if friend_items is not None:
            self.friend_items = friend_items
        else:
            self.friend_items = [
                MockShareElement(text=name, attributes={"aria-checked": "false"})
                for name in (friend_names or ["Khoa Đức @khoa_memezlol"])
            ]
        self.chips: list[MockShareElement] = chips or []
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
        if "DivUserItemContainer" in selector or "share-user-item" in selector:
            containers = MagicMock()
            containers.count = AsyncMock(return_value=len(self.friend_items))
            containers.nth = MagicMock(side_effect=lambda idx: self.friend_items[idx])
            return containers
        if "Chip" in selector or "Tag" in selector or "SelectedUser" in selector or "selected-user" in selector:
            chips_container = MagicMock()
            chips_container.count = AsyncMock(return_value=len(self.chips))
            chips_container.nth = MagicMock(side_effect=lambda idx: self.chips[idx])
            chips_container.first = self.chips[0] if self.chips else MockShareElement(count_val=0, visible=False)
            return chips_container
        if "aria-checked='true'" in selector or "aria-selected='true'" in selector:
            checked = [
                item for item in self.friend_items
                if item.attributes.get("aria-checked") in ("true", "True", True)
                or item.attributes.get("aria-selected") in ("true", "True", True)
            ]
            checked_container = MagicMock()
            checked_container.count = AsyncMock(return_value=len(checked))
            checked_container.nth = MagicMock(side_effect=lambda idx: checked[idx])
            return checked_container
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

    def test_share_video_card_send_button_wait_failure_raises_not_sent(self) -> None:
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
            page.send_btn.wait_for = AsyncMock(side_effect=RuntimeError("send btn not visible"))

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

    def test_chip_generic_data_e2e_is_not_accepted_as_username(self) -> None:
        config: dict[str, object] = {"recipients": []}
        sender = TikTokSender(config, FakeNotifier(), RecordingVideoPool())  # type: ignore[arg-type]
        chip = MockShareElement(attributes={"data-e2e": "selected-user-chip"})
        extracted = asyncio.run(sender._extract_chip_username(chip))
        self.assertIsNone(extracted)

    def test_chip_valid_data_e2e_prefix_accepted_as_identity_signal(self) -> None:
        config: dict[str, object] = {"recipients": []}
        sender = TikTokSender(config, FakeNotifier(), RecordingVideoPool())  # type: ignore[arg-type]
        chip_share = MockShareElement(attributes={"data-e2e": "share-alice"})
        self.assertEqual(asyncio.run(sender._extract_chip_username(chip_share)), "alice")

        chip_prefix = MockShareElement(attributes={"data-e2e": "chip-alice"})
        self.assertEqual(asyncio.run(sender._extract_chip_username(chip_prefix)), "alice")

        chip_at = MockShareElement(attributes={"data-e2e": "chip-@alice"})
        self.assertEqual(asyncio.run(sender._extract_chip_username(chip_at)), "alice")

        chip_empty_suffix = MockShareElement(attributes={"data-e2e": "share-"})
        self.assertIsNone(asyncio.run(sender._extract_chip_username(chip_empty_suffix)))

        chip_malformed_suffix = MockShareElement(attributes={"data-e2e": "chip-alice!bad"})
        self.assertIsNone(asyncio.run(sender._extract_chip_username(chip_malformed_suffix)))

    def test_chip_conflicting_profile_link_and_explicit_attribute_rejected(self) -> None:
        config: dict[str, object] = {"recipients": []}
        sender = TikTokSender(config, FakeNotifier(), RecordingVideoPool())  # type: ignore[arg-type]
        chip1 = MockShareElement(
            profile_links=["/@alice"],
            attributes={"data-username": "bob"},
        )
        self.assertIsNone(asyncio.run(sender._extract_chip_username(chip1)))

        chip2 = MockShareElement(
            profile_links=["/@alice"],
            attributes={"data-e2e": "chip-bob"},
        )
        self.assertIsNone(asyncio.run(sender._extract_chip_username(chip2)))

        chip3 = MockShareElement(
            attributes={"data-username": "alice", "data-e2e": "chip-bob"},
        )
        self.assertIsNone(asyncio.run(sender._extract_chip_username(chip3)))

    def test_chip_valid_unambiguous_selected_chip_passes(self) -> None:
        config: dict[str, object] = {"recipients": []}
        sender = TikTokSender(config, FakeNotifier(), RecordingVideoPool())  # type: ignore[arg-type]
        chip = MockShareElement(
            profile_links=["/@alice"],
            attributes={"data-username": "alice", "data-e2e": "chip-alice"},
        )
        self.assertEqual(asyncio.run(sender._extract_chip_username(chip)), "alice")

    def test_verify_share_recipient_selected_chip_generic_data_e2e_fails(self) -> None:
        """P1: A generic data-e2e attribute on chip does not identify the user and fails before Send."""
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
            row = MockShareElement(text="Alice @alice")
            chip = MockShareElement(attributes={"data-e2e": "selected-user-chip"})
            page = MockVideoPage(friend_items=[row], chips=[chip])

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(DeliveryConfirmedNotSentError):
                    asyncio.run(
                        sender._share_video_card(
                            page,
                            {"name": "Alice", "username": "alice"},
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            "7692053355551526162",
                        )
                    )
            self.assertFalse(page.send_btn.clicked)

    def test_verify_share_recipient_selected_chip_mismatched_target_fails(self) -> None:
        """P1: A chip identifying a different user fails verification before Send."""
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
            row = MockShareElement(text="Alice @alice")
            chip = MockShareElement(attributes={"data-e2e": "chip-bob"})
            page = MockVideoPage(friend_items=[row], chips=[chip])

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(DeliveryConfirmedNotSentError):
                    asyncio.run(
                        sender._share_video_card(
                            page,
                            {"name": "Alice", "username": "alice"},
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            "7692053355551526162",
                        )
                    )
            self.assertFalse(page.send_btn.clicked)

    def test_share_row_generic_data_e2e_user_item_not_accepted_as_username(self) -> None:
        """P1: data-e2e='share-user-item' is a structural marker, not a username."""
        config: dict[str, object] = {"recipients": []}
        sender = TikTokSender(config, FakeNotifier(), RecordingVideoPool())  # type: ignore[arg-type]
        row = MockShareElement(attributes={"data-e2e": "share-user-item"})
        extracted = asyncio.run(sender._extract_share_row_usernames(row))
        self.assertEqual(extracted, set())

    def test_share_video_card_generic_data_e2e_user_item_aborts_without_send(self) -> None:
        """P1: A row with only structural data-e2e='share-user-item' does not match and Send is not clicked."""
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
            row = MockShareElement(attributes={"data-e2e": "share-user-item"})
            page = MockVideoPage(friend_items=[row])

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(DeliveryConfirmedNotSentError):
                    asyncio.run(
                        sender._share_video_card(
                            page,
                            {"name": "User Item", "username": "user-item"},
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            "7692053355551526162",
                        )
                    )
            self.assertFalse(page.send_btn.clicked)

    def test_share_row_real_supported_share_avatar_identifier_accepted(self) -> None:
        """P1: A real, supported share-avatar identifier identifies the recipient and clicks Send."""
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
            avatar = MockShareElement(
                attributes={"data-e2e": "share-alice", "class": "avatar"},
            )
            row = MockShareElement(
                sub_elements={"avatar": [avatar]},
                attributes={"aria-checked": "false"},
                auto_check_on_click=True,
            )
            page = MockVideoPage(friend_items=[row])

            with patch("asyncio.sleep", new_callable=AsyncMock):
                asyncio.run(
                    sender._share_video_card(
                        page,
                        {"name": "Alice", "username": "alice"},
                        "https://www.tiktok.com/@user/video/7692053355551526162",
                        "7692053355551526162",
                    )
                )
            self.assertTrue(page.send_btn.clicked)

    def test_share_row_arbitrary_row_level_marker_cannot_impersonate_identity(self) -> None:
        """P1: Arbitrary row-level data-e2e marker cannot impersonate an identity."""
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
            row = MockShareElement(
                attributes={"data-e2e": "share-alice", "aria-checked": "false"},
                auto_check_on_click=True,
            )
            self.assertEqual(asyncio.run(sender._extract_share_row_usernames(row)), set())

            page = MockVideoPage(friend_items=[row])
            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(DeliveryConfirmedNotSentError):
                    asyncio.run(
                        sender._share_video_card(
                            page,
                            {"name": "Alice", "username": "alice"},
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            "7692053355551526162",
                        )
                    )
            self.assertFalse(page.send_btn.clicked)

    def test_share_row_generic_structural_marker_with_profile_link_does_not_conflict(self) -> None:
        """P1: Structural marker share-user-item does not conflict with profile link /@alice."""
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
            row = MockShareElement(
                profile_links=["/@alice"],
                attributes={"data-e2e": "share-user-item"},
            )
            page = MockVideoPage(friend_items=[row])

            # Should match alice and click Send
            with patch("asyncio.sleep", new_callable=AsyncMock):
                asyncio.run(
                    sender._share_video_card(
                        page,
                        {"name": "Alice", "username": "alice"},
                        "https://www.tiktok.com/@user/video/7692053355551526162",
                        "7692053355551526162",
                    )
                )
            self.assertTrue(page.send_btn.clicked)

            # But should NOT match user-item
            page2 = MockVideoPage(friend_items=[row])
            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(DeliveryConfirmedNotSentError):
                    asyncio.run(
                        sender._share_video_card(
                            page2,
                            {"name": "User Item", "username": "user-item"},
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            "7692053355551526162",
                        )
                    )
            self.assertFalse(page2.send_btn.clicked)

    def test_verify_share_recipient_selected_chip_valid_but_multiple_rows_selected_fails(self) -> None:
        """P1: Valid Alice chip present, but both Alice and Bob rows are marked selected; fails closed without clicking Send."""
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
            row_alice = MockShareElement(text="Alice @alice", attributes={"aria-checked": "true"})
            row_bob = MockShareElement(text="Bob @bob", attributes={"aria-checked": "true"})
            chip_alice = MockShareElement(attributes={"data-e2e": "chip-alice"})
            page = MockVideoPage(
                friend_items=[row_alice, row_bob],
                chips=[chip_alice],
            )

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(DeliveryConfirmedNotSentError):
                    asyncio.run(
                        sender._share_video_card(
                            page,
                            {"name": "Alice", "username": "alice"},
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            "7692053355551526162",
                        )
                    )
            self.assertFalse(page.send_btn.clicked)

    def test_verify_share_recipient_selected_valid_chip_with_zero_enumerable_rows_succeeds(self) -> None:
        """P1: Genuinely exposes chips but no enumerable rows; exact verified chip succeeds."""
        config: dict[str, object] = {"recipients": []}
        sender = TikTokSender(config, FakeNotifier(), RecordingVideoPool())  # type: ignore[arg-type]
        chip_alice = MockShareElement(attributes={"data-e2e": "chip-alice"})
        item = MockShareElement()
        page = MockVideoPage(friend_items=[], chips=[chip_alice])

        verified = asyncio.run(sender._verify_share_recipient_selected(item, page, "alice"))
        self.assertTrue(verified)

    def test_share_video_card_send_button_click_failure_raises_unconfirmed(self) -> None:
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
                with self.assertRaises(DeliveryUnconfirmedError):
                    asyncio.run(
                        sender._share_video_card(
                            page,
                            {"name": "Khoa Đức", "username": "khoa_memezlol"},
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            "7692053355551526162",
                        )
                    )

    def test_share_video_card_does_not_match_suffix_username(self) -> None:
        """P1: Target 'alice' must not match 'alice_backup'."""
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
            page = MockVideoPage(["Alice Backup @alice_backup"])

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(DeliveryConfirmedNotSentError) as cm:
                    asyncio.run(
                        sender._share_video_card(
                            page,
                            {"name": "Alice", "username": "alice"},
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            "7692053355551526162",
                        )
                    )
            self.assertIn("not found with exact verifiable username", str(cm.exception))
            self.assertFalse(page.send_btn.clicked)

    def test_share_video_card_does_not_match_prefix_username(self) -> None:
        """P1: Target 'alice' must not match 'malice'."""
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
            page = MockVideoPage(["Malice @malice"])

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(DeliveryConfirmedNotSentError) as cm:
                    asyncio.run(
                        sender._share_video_card(
                            page,
                            {"name": "Alice", "username": "alice"},
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            "7692053355551526162",
                        )
                    )
            self.assertIn("not found with exact verifiable username", str(cm.exception))
            self.assertFalse(page.send_btn.clicked)

    def test_share_video_card_duplicate_exact_usernames_aborts_without_send(self) -> None:
        """P1: Duplicate/ambiguous exact matches abort without clicking Send."""
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
            page = MockVideoPage(["Alice @alice", "Alice Twin @alice"])

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(DeliveryConfirmedNotSentError) as cm:
                    asyncio.run(
                        sender._share_video_card(
                            page,
                            {"name": "Alice", "username": "alice"},
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            "7692053355551526162",
                        )
                    )
            self.assertIn("Ambiguous recipient", str(cm.exception))
            self.assertFalse(page.send_btn.clicked)

    def test_share_video_card_unique_exact_username_matches_and_clicks_send(self) -> None:
        """P1: Unique exact username match proceeds and clicks Send."""
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
            page = MockVideoPage(["Alice Backup @alice_backup", "Alice @alice", "Malice @malice"])

            with patch("asyncio.sleep", new_callable=AsyncMock):
                asyncio.run(
                    sender._share_video_card(
                        page,
                        {"name": "Alice", "username": "alice"},
                        "https://www.tiktok.com/@user/video/7692053355551526162",
                        "7692053355551526162",
                    )
                )
            self.assertTrue(page.send_btn.clicked)

    def test_share_video_card_selection_verification_failure_aborts_without_send(self) -> None:
        """P1: If selection verification fails, Send is never clicked."""
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
            page = MockVideoPage(["Alice @alice"])
            # Model DOM where selection could not be established
            page.friend_items[0].auto_check_on_click = False
            page.friend_items[0].attributes["aria-checked"] = "false"

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(DeliveryConfirmedNotSentError) as cm:
                    asyncio.run(
                        sender._share_video_card(
                            page,
                            {"name": "Alice", "username": "alice"},
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            "7692053355551526162",
                        )
                    )
            self.assertIn("Could not verify selection state", str(cm.exception))
            self.assertFalse(page.send_btn.clicked)

    def test_share_row_authoritative_link_bob_with_conflicting_alice_field_rejected(self) -> None:
        """P1: Row whose profile link is 'bob' but another DOM/text field is 'alice' must be rejected for target 'alice'."""
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
            row = MockShareElement(
                text="Alice Cooper",
                profile_links=["/@bob"],
                sub_elements={"[data-e2e*='user-name']": [MockShareElement(text="alice")]},
            )
            page = MockVideoPage(friend_items=[row])

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(DeliveryConfirmedNotSentError) as cm:
                    asyncio.run(
                        sender._share_video_card(
                            page,
                            {"name": "Alice Cooper", "username": "alice"},
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            "7692053355551526162",
                        )
                    )
            self.assertIn("not found with exact verifiable username", str(cm.exception))
            self.assertFalse(page.send_btn.clicked)

    def test_share_row_authoritative_link_alice_with_conflicting_bob_field_rejected(self) -> None:
        """P1: Row whose profile link is 'alice' but unique-id field is 'bob' (reverse contradiction) must be rejected."""
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
            row = MockShareElement(
                text="Alice @bob",
                profile_links=["/@alice"],
                sub_elements={"[data-e2e*='unique-id']": [MockShareElement(text="@bob")]},
            )
            page = MockVideoPage(friend_items=[row])

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(DeliveryConfirmedNotSentError) as cm:
                    asyncio.run(
                        sender._share_video_card(
                            page,
                            {"name": "Alice", "username": "alice"},
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            "7692053355551526162",
                        )
                    )
            self.assertIn("not found with exact verifiable username", str(cm.exception))
            self.assertFalse(page.send_btn.clicked)

    def test_share_row_conflicting_profile_links_rejected(self) -> None:
        """P1: Row with conflicting profile links must be rejected."""
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
            row = MockShareElement(
                text="Alice",
                profile_links=["/@alice", "/@bob"],
            )
            page = MockVideoPage(friend_items=[row])

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(DeliveryConfirmedNotSentError) as cm:
                    asyncio.run(
                        sender._share_video_card(
                            page,
                            {"name": "Alice", "username": "alice"},
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            "7692053355551526162",
                        )
                    )
            self.assertIn("not found with exact verifiable username", str(cm.exception))
            self.assertFalse(page.send_btn.clicked)

    def test_share_row_missing_authoritative_identity_rejected(self) -> None:
        """P1: Row with missing authoritative identity evidence (only display name) must fail closed."""
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
            row = MockShareElement(
                text="Alice Cooper",
                profile_links=[],
                sub_elements={},
            )
            page = MockVideoPage(friend_items=[row])

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(DeliveryConfirmedNotSentError) as cm:
                    asyncio.run(
                        sender._share_video_card(
                            page,
                            {"name": "Alice Cooper", "username": "alice"},
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            "7692053355551526162",
                        )
                    )
            self.assertIn("not found with exact verifiable username", str(cm.exception))
            self.assertFalse(page.send_btn.clicked)

    def test_share_row_consistent_exact_username_matches_and_clicks_send(self) -> None:
        """P1: Row with consistent exact username evidence matches and clicks Send."""
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
            row = MockShareElement(
                text="Alice Cooper @alice",
                profile_links=["/@alice"],
                sub_elements={"[data-e2e*='unique-id']": [MockShareElement(text="@alice")]},
            )
            page = MockVideoPage(friend_items=[row])

            with patch("asyncio.sleep", new_callable=AsyncMock):
                asyncio.run(
                    sender._share_video_card(
                        page,
                        {"name": "Alice Cooper", "username": "alice"},
                        "https://www.tiktok.com/@user/video/7692053355551526162",
                        "7692053355551526162",
                    )
                )
            self.assertTrue(page.send_btn.clicked)

    def test_verify_share_recipient_selected_chip_suffix_fails(self) -> None:
        """P1: Target 'alice' with selected chip '@alice_backup' must fail without clicking Send."""
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
            page = MockVideoPage(
                friend_names=["Alice @alice"],
                chips=[MockShareElement(text="@alice_backup")],
            )

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(DeliveryConfirmedNotSentError) as cm:
                    asyncio.run(
                        sender._share_video_card(
                            page,
                            {"name": "Alice", "username": "alice"},
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            "7692053355551526162",
                        )
                    )
            self.assertIn("Could not verify selection state", str(cm.exception))
            self.assertFalse(page.send_btn.clicked)

    def test_verify_share_recipient_selected_chip_display_name_substring_fails(self) -> None:
        """P1: Target 'alice' with chip containing display name as substring must fail."""
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
            page = MockVideoPage(
                friend_names=["Alice @alice"],
                chips=[MockShareElement(text="Alice Cooper")],  # display name, no verified username
            )

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(DeliveryConfirmedNotSentError) as cm:
                    asyncio.run(
                        sender._share_video_card(
                            page,
                            {"name": "Alice Cooper", "username": "alice"},
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            "7692053355551526162",
                        )
                    )
            self.assertIn("Could not verify selection state", str(cm.exception))
            self.assertFalse(page.send_btn.clicked)

    def test_verify_share_recipient_selected_multiple_chips_fails(self) -> None:
        """P1: Target 'alice' with multiple recipients selected in chips must fail."""
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
            page = MockVideoPage(
                friend_names=["Alice @alice"],
                chips=[MockShareElement(text="@alice"), MockShareElement(text="@bob")],
            )

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(DeliveryConfirmedNotSentError) as cm:
                    asyncio.run(
                        sender._share_video_card(
                            page,
                            {"name": "Alice", "username": "alice"},
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            "7692053355551526162",
                        )
                    )
            self.assertIn("Could not verify selection state", str(cm.exception))
            self.assertFalse(page.send_btn.clicked)

    def test_verify_share_recipient_selected_exact_single_chip_succeeds(self) -> None:
        """P1: Target 'alice' with exact single chip '@alice' succeeds and clicks Send."""
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
            page = MockVideoPage(
                friend_names=["Alice @alice"],
                chips=[MockShareElement(text="@alice")],
            )

            with patch("asyncio.sleep", new_callable=AsyncMock):
                asyncio.run(
                    sender._share_video_card(
                        page,
                        {"name": "Alice", "username": "alice"},
                        "https://www.tiktok.com/@user/video/7692053355551526162",
                        "7692053355551526162",
                    )
                )
            self.assertTrue(page.send_btn.clicked)

    def test_verify_share_recipient_selected_missing_or_ambiguous_state_fails(self) -> None:
        """P1: Missing or ambiguous selection state fails closed before Send."""
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
            row = MockShareElement(
                text="Alice @alice",
                auto_check_on_click=False,
                attributes={},
            )
            page = MockVideoPage(friend_items=[row])

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(DeliveryConfirmedNotSentError) as cm:
                    asyncio.run(
                        sender._share_video_card(
                            page,
                            {"name": "Alice", "username": "alice"},
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            "7692053355551526162",
                        )
                    )
            self.assertIn("Could not verify selection state", str(cm.exception))
            self.assertFalse(page.send_btn.clicked)

    def test_verify_share_recipient_selected_two_selected_rows_fails(self) -> None:
        """P1: Two selected recipient rows are rejected before Send is clicked."""
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
            row_alice = MockShareElement(
                text="Alice @alice",
                attributes={"aria-checked": "true"},
            )
            row_bob = MockShareElement(
                text="Bob @bob",
                attributes={"aria-checked": "true"},
            )
            page = MockVideoPage(friend_items=[row_alice, row_bob])

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(DeliveryConfirmedNotSentError) as cm:
                    asyncio.run(
                        sender._share_video_card(
                            page,
                            {"name": "Alice", "username": "alice"},
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            "7692053355551526162",
                        )
                    )
            self.assertIn("Could not verify selection state", str(cm.exception))
            self.assertFalse(page.send_btn.clicked)

    def test_verify_share_recipient_selected_unselected_class_alone_fails(self) -> None:
        """P1: An 'unselected' class alone does not count as selected and fails before Send."""
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
            row = MockShareElement(
                text="Alice @alice",
                auto_check_on_click=False,
                attributes={"class": "user-item unselected"},
            )
            page = MockVideoPage(friend_items=[row])

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(DeliveryConfirmedNotSentError) as cm:
                    asyncio.run(
                        sender._share_video_card(
                            page,
                            {"name": "Alice", "username": "alice"},
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            "7692053355551526162",
                        )
                    )
            self.assertIn("Could not verify selection state", str(cm.exception))
            self.assertFalse(page.send_btn.clicked)

    def test_verify_share_recipient_selected_exception_during_inspection_fails_closed(self) -> None:
        """P1: An exception while inspecting other selected recipients fails closed without clicking Send."""
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
            row_alice = MockShareElement(
                text="Alice @alice",
                attributes={"aria-checked": "true"},
            )
            row_broken = MockShareElement(
                text="Bob @bob",
            )
            row_broken.get_attribute = AsyncMock(side_effect=RuntimeError("DOM node detached during inspection"))
            page = MockVideoPage(friend_items=[row_alice, row_broken])

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(DeliveryConfirmedNotSentError) as cm:
                    asyncio.run(
                        sender._share_video_card(
                            page,
                            {"name": "Alice", "username": "alice"},
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            "7692053355551526162",
                        )
                    )
            self.assertIn("Could not verify selection state", str(cm.exception))
            self.assertFalse(page.send_btn.clicked)

    def test_verify_share_recipient_selected_chip_with_conflicting_identities_fails(self) -> None:
        """P1: A selected chip with conflicting authoritative usernames is rejected before Send."""
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
            conflicting_chip = MockShareElement(
                text="Alice @alice",
                profile_links=["/@alice", "/@bob"],
            )
            page = MockVideoPage(
                friend_names=["Alice @alice"],
                chips=[conflicting_chip],
            )

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(DeliveryConfirmedNotSentError) as cm:
                    asyncio.run(
                        sender._share_video_card(
                            page,
                            {"name": "Alice", "username": "alice"},
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            "7692053355551526162",
                        )
                    )
            self.assertIn("Could not verify selection state", str(cm.exception))
            self.assertFalse(page.send_btn.clicked)

    def test_verify_share_recipient_selected_valid_exact_recipient_succeeds(self) -> None:
        """P1: A valid, exactly selected recipient row succeeds and clicks Send."""
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
            row_alice = MockShareElement(
                text="Alice @alice",
                auto_check_on_click=True,
                attributes={"aria-checked": "false"},
            )
            row_bob = MockShareElement(
                text="Bob @bob",
                auto_check_on_click=False,
                attributes={"aria-checked": "false"},
            )
            page = MockVideoPage(friend_items=[row_alice, row_bob])

            with patch("asyncio.sleep", new_callable=AsyncMock):
                asyncio.run(
                    sender._share_video_card(
                        page,
                        {"name": "Alice", "username": "alice"},
                        "https://www.tiktok.com/@user/video/7692053355551526162",
                        "7692053355551526162",
                    )
                )
            self.assertTrue(page.send_btn.clicked)

    def test_verify_share_recipient_selected_unknown_row_state_fails_before_send(self) -> None:
        """P1: Alice is explicitly selected, but Bob has unknown selection state -> fails before Send."""
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
            row_alice = MockShareElement(
                text="Alice @alice",
                auto_check_on_click=True,
                attributes={"aria-checked": "false"},
            )
            # Bob has no readable selection state (empty attributes)
            row_bob = MockShareElement(
                text="Bob @bob",
                auto_check_on_click=False,
                attributes={},
            )
            page = MockVideoPage(friend_items=[row_alice, row_bob])

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(DeliveryConfirmedNotSentError) as cm:
                    asyncio.run(
                        sender._share_video_card(
                            page,
                            {"name": "Alice", "username": "alice"},
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            "7692053355551526162",
                        )
                    )
            self.assertIn("Could not verify selection state", str(cm.exception))
            self.assertFalse(page.send_btn.clicked)

    def test_verify_share_recipient_selected_alice_selected_and_bob_explicitly_unselected_succeeds(self) -> None:
        """P1: Alice is selected, and Bob is explicitly unselected -> succeeds and clicks Send."""
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
            row_alice = MockShareElement(
                text="Alice @alice",
                auto_check_on_click=True,
                attributes={"aria-checked": "false"},
            )
            row_bob = MockShareElement(
                text="Bob @bob",
                auto_check_on_click=False,
                attributes={"aria-checked": "false"},
            )
            page = MockVideoPage(friend_items=[row_alice, row_bob])

            with patch("asyncio.sleep", new_callable=AsyncMock):
                asyncio.run(
                    sender._share_video_card(
                        page,
                        {"name": "Alice", "username": "alice"},
                        "https://www.tiktok.com/@user/video/7692053355551526162",
                        "7692053355551526162",
                    )
                )
            self.assertTrue(page.send_btn.clicked)

    def test_is_row_element_selected_tri_state_behavior(self) -> None:
        """P1: _is_row_element_selected returns SELECTED, UNSELECTED, or UNKNOWN, and raises on conflict."""
        config: dict[str, object] = {"recipients": []}
        sender = TikTokSender(config, FakeNotifier(), RecordingVideoPool())  # type: ignore[arg-type]

        # Selected
        el_sel = MockShareElement(attributes={"aria-checked": "true"})
        self.assertEqual(
            asyncio.run(sender._is_row_element_selected(el_sel)),
            RowSelectionState.SELECTED,
        )

        # Explicitly unselected
        el_unsel = MockShareElement(attributes={"aria-checked": "false"})
        self.assertEqual(
            asyncio.run(sender._is_row_element_selected(el_unsel)),
            RowSelectionState.UNSELECTED,
        )

        # Negative class
        el_neg_class = MockShareElement(attributes={"class": "item unselected"})
        self.assertEqual(
            asyncio.run(sender._is_row_element_selected(el_neg_class)),
            RowSelectionState.UNSELECTED,
        )

        # Unknown (no readable state)
        el_unknown = MockShareElement(attributes={})
        self.assertEqual(
            asyncio.run(sender._is_row_element_selected(el_unknown)),
            RowSelectionState.UNKNOWN,
        )

        # Contradictory signals raise ValueError
        el_conflict = MockShareElement(attributes={"aria-checked": "true", "aria-selected": "false"})
        with self.assertRaises(ValueError):
            asyncio.run(sender._is_row_element_selected(el_conflict))

    def test_selection_conflict_aria_checked_true_and_aria_selected_false_fails_closed(self) -> None:
        """P1: aria-checked='true' conflicting with aria-selected='false' must fail closed without Send click."""
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
            row = MockShareElement(
                text="Alice @alice",
                auto_check_on_click=False,
                attributes={"aria-checked": "true", "aria-selected": "false"},
            )
            page = MockVideoPage(friend_items=[row])

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(DeliveryConfirmedNotSentError) as cm:
                    asyncio.run(
                        sender._share_video_card(
                            page,
                            {"name": "Alice", "username": "alice"},
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            "7692053355551526162",
                        )
                    )
            self.assertIn("Could not verify selection state", str(cm.exception))
            self.assertFalse(page.send_btn.clicked)

    def test_selection_conflict_explicit_false_and_selected_class_fails_closed(self) -> None:
        """P1: Explicit false attribute conflicting with a 'selected' class must fail closed without Send click."""
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
            row = MockShareElement(
                text="Alice @alice",
                auto_check_on_click=False,
                attributes={"aria-checked": "false", "class": "user-item selected"},
            )
            page = MockVideoPage(friend_items=[row])

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(DeliveryConfirmedNotSentError) as cm:
                    asyncio.run(
                        sender._share_video_card(
                            page,
                            {"name": "Alice", "username": "alice"},
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            "7692053355551526162",
                        )
                    )
            self.assertIn("Could not verify selection state", str(cm.exception))
            self.assertFalse(page.send_btn.clicked)

    def test_identity_extraction_profile_link_inspection_fails_while_text_handle_available(self) -> None:
        """P1: Failure reading authoritative profile link fails closed even when text handle is present."""
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
            row = MockShareElement(text="Alice @alice")
            orig_locator = row.locator

            def broken_link_locator(sel: str) -> Any:
                if "a[href" in sel:
                    broken_links = MagicMock()
                    broken_links.evaluate_all = AsyncMock(side_effect=RuntimeError("Profile link inspection error"))
                    broken_links.count = AsyncMock(side_effect=RuntimeError("Profile link inspection error"))
                    return broken_links
                return orig_locator(sel)

            row.locator = broken_link_locator  # type: ignore[method-assign]
            page = MockVideoPage(friend_items=[row])

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(DeliveryConfirmedNotSentError) as cm:
                    asyncio.run(
                        sender._share_video_card(
                            page,
                            {"name": "Alice", "username": "alice"},
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            "7692053355551526162",
                        )
                    )
            self.assertIn("not found with exact verifiable username", str(cm.exception))
            self.assertFalse(page.send_btn.clicked)

    def test_identity_extraction_row_signal_fails_while_another_signal_matches_target(self) -> None:
        """P1: Failure reading one identity signal fails closed even if another signal appears to match."""
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
            row = MockShareElement(
                text="Alice @alice",
                profile_links=["/@alice"],
            )
            orig_locator = row.locator

            def broken_unique_id_locator(sel: str) -> Any:
                if "unique-id" in sel:
                    broken_el = MagicMock()
                    broken_el.count = AsyncMock(side_effect=RuntimeError("Unique-id DOM inspection failure"))
                    return broken_el
                return orig_locator(sel)

            row.locator = broken_unique_id_locator  # type: ignore[method-assign]
            page = MockVideoPage(friend_items=[row])

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(DeliveryConfirmedNotSentError) as cm:
                    asyncio.run(
                        sender._share_video_card(
                            page,
                            {"name": "Alice", "username": "alice"},
                            "https://www.tiktok.com/@user/video/7692053355551526162",
                            "7692053355551526162",
                        )
                    )
            self.assertIn("not found with exact verifiable username", str(cm.exception))
            self.assertFalse(page.send_btn.clicked)

    def test_valid_unambiguous_recipient_still_succeeds(self) -> None:
        """P1: Valid, unambiguous recipient with consistent identity and selection succeeds."""
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
            row = MockShareElement(
                text="Alice @alice",
                profile_links=["/@alice"],
                auto_check_on_click=True,
                attributes={"aria-checked": "false"},
            )
            page = MockVideoPage(friend_items=[row])

            with patch("asyncio.sleep", new_callable=AsyncMock):
                asyncio.run(
                    sender._share_video_card(
                        page,
                        {"name": "Alice", "username": "alice"},
                        "https://www.tiktok.com/@user/video/7692053355551526162",
                        "7692053355551526162",
                    )
                )
            self.assertTrue(page.send_btn.clicked)

    def test_post_click_exception_in_share_video_card_returns_unconfirmed_without_fallback_or_retry(self) -> None:
        """P1: Non-cancellation exception after Send click in _share_video_card() must result in UNCONFIRMED, no retry, no raw-URL fallback."""
        config: dict[str, Any] = {
            "tiktok": {"recipient_retries": 2, "message_delay_seconds": [0, 0]},
            "recipients": [{"name": "Alice", "username": "alice"}],
        }
        with TemporaryDirectory() as tmp_dir:
            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
            )
            sender.allow_raw_url_fallback = True  # Explicitly enable fallback
            sender._send_raw_url_flow = AsyncMock()

            video_page = MockVideoPage(["Alice @alice"])

            dm_page = MockDeliveryPage(evaluate_result={"verified": True})
            sender.recipient_service.select_recipient_for_username_with_page = AsyncMock(return_value=(dm_page, "Alice"))
            sender._active_dm_page = dm_page
            sender._handle_tiktok_interruptions = AsyncMock()
            sender._verify_chat_header_identity = AsyncMock()
            sender._count_chat_items = AsyncMock(return_value=0)
            sender._wait_for_delivery_evidence = AsyncMock()

            context = MagicMock()
            context.is_closed = lambda: False
            context.new_page = AsyncMock(return_value=video_page)

            orig_sleep = asyncio.sleep

            async def failing_sleep(duration: float) -> None:
                if video_page.send_btn.clicked:
                    raise RuntimeError("Post-click crash in _share_video_card")
                await orig_sleep(0)

            with patch("asyncio.sleep", side_effect=failing_sleep):
                result = asyncio.run(
                    sender._send_recipient_with_retries(context, config["recipients"][0])
                )

            self.assertTrue(video_page.send_btn.clicked)
            self.assertEqual(result.status, RecipientStatus.UNCONFIRMED)
            self.assertEqual(result.delivery_status, DeliveryStatus.UNCONFIRMED)
            self.assertEqual(result.attempts, 1)  # Did NOT retry
            sender._send_raw_url_flow.assert_not_called()  # Did NOT fallback

    def test_video_page_close_failure_after_send_does_not_trigger_resend(self) -> None:
        """P1: video_page.close() failure after Send clicked does not cause retry."""
        config: dict[str, Any] = {
            "tiktok": {"recipient_retries": 1, "message_delay_seconds": [0, 0]},
            "recipients": [{"name": "Alice", "username": "alice"}],
        }
        with TemporaryDirectory() as tmp_dir:
            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
            )
            dm_page = MockDeliveryPage(evaluate_result={"verified": True})
            sender.recipient_service.select_recipient_for_username_with_page = AsyncMock(return_value=(dm_page, "Alice"))
            sender._active_dm_page = dm_page
            sender._handle_tiktok_interruptions = AsyncMock()
            sender._verify_chat_header_identity = AsyncMock()
            sender._count_chat_items = AsyncMock(return_value=0)
            sender._wait_for_delivery_evidence = AsyncMock()

            # Mock context
            video_page = MockVideoPage(["Alice @alice"])
            video_page.close = AsyncMock(side_effect=RuntimeError("close failed on video page"))

            async def mock_new_page() -> Any:
                return video_page

            ctx = MagicMock()
            ctx.is_closed = lambda: False
            ctx.new_page = mock_new_page

            with patch("asyncio.sleep", new_callable=AsyncMock):
                res = asyncio.run(sender._send_recipient_with_retries(ctx, config["recipients"][0]))

            # Send button was clicked once
            self.assertTrue(video_page.send_btn.clicked)
            # Result is verified SUCCESS, attempts == 1 (no duplicate attempt!)
            self.assertEqual(res.status, RecipientStatus.SUCCESS)
            self.assertEqual(res.delivery_status, DeliveryStatus.VERIFIED)
            self.assertEqual(res.attempts, 1)

    def test_post_verification_popup_handling_failure_does_not_resend(self) -> None:
        """P1: A post-verification popup handling exception does not resend a verified message."""
        config: dict[str, Any] = {
            "tiktok": {"recipient_retries": 1, "message_delay_seconds": [0, 0]},
            "recipients": [{"name": "Alice", "username": "alice"}],
        }
        with TemporaryDirectory() as tmp_dir:
            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
            )
            async def mock_popup_handler(p: Any) -> None:
                if sender._wait_for_delivery_evidence.await_count > 0:
                    raise RuntimeError("popup handler failed after verification")

            dm_page = MockDeliveryPage(evaluate_result={"verified": True})
            sender.recipient_service.select_recipient_for_username_with_page = AsyncMock(return_value=(dm_page, "Alice"))
            sender._active_dm_page = dm_page
            sender._handle_tiktok_interruptions = mock_popup_handler  # type: ignore[method-assign]
            sender._verify_chat_header_identity = AsyncMock()
            sender._count_chat_items = AsyncMock(return_value=0)
            sender._wait_for_delivery_evidence = AsyncMock()

            video_page = MockVideoPage(["Alice @alice"])
            video_page.close = AsyncMock()

            ctx = MagicMock()
            ctx.is_closed = lambda: False
            ctx.new_page = AsyncMock(return_value=video_page)

            with patch("asyncio.sleep", new_callable=AsyncMock):
                res = asyncio.run(sender._send_recipient_with_retries(ctx, config["recipients"][0]))

            self.assertEqual(res.status, RecipientStatus.SUCCESS)
            self.assertEqual(res.delivery_status, DeliveryStatus.VERIFIED)
            self.assertEqual(res.attempts, 1)

    def test_submission_failure_returns_unconfirmed_without_fallback(self) -> None:
        """P1: A failure during Send button click returns UNCONFIRMED and does not invoke fallback."""
        config: dict[str, Any] = {
            "tiktok": {
                "message_format": "native_video_card",
                "allow_raw_url_fallback": True,
                "recipient_retries": 1,
            },
            "recipients": [{"name": "Alice", "username": "alice"}],
        }
        with TemporaryDirectory() as tmp_dir:
            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
            )
            dm_page = MockDeliveryPage(evaluate_result={"verified": True})
            sender.recipient_service.select_recipient_for_username_with_page = AsyncMock(return_value=(dm_page, "Alice"))
            sender._active_dm_page = dm_page
            sender._handle_tiktok_interruptions = AsyncMock()
            sender._verify_chat_header_identity = AsyncMock()
            sender._count_chat_items = AsyncMock(return_value=0)
            sender._send_raw_url_flow = AsyncMock()  # type: ignore[method-assign]

            video_page = MockVideoPage(["Alice @alice"])
            video_page.send_btn.click = AsyncMock(side_effect=RuntimeError("connection dropped during send click"))
            video_page.close = AsyncMock()

            ctx = MagicMock()
            ctx.is_closed = lambda: False
            ctx.new_page = AsyncMock(return_value=video_page)

            with patch("asyncio.sleep", new_callable=AsyncMock):
                res = asyncio.run(sender._send_recipient_with_retries(ctx, config["recipients"][0]))

            self.assertEqual(res.status, RecipientStatus.UNCONFIRMED)
            self.assertEqual(res.delivery_status, DeliveryStatus.UNCONFIRMED)
            self.assertEqual(res.attempts, 1)
            # Raw-URL fallback must NOT be called after possible submission!
            sender._send_raw_url_flow.assert_not_awaited()

    def test_verified_send_remains_successful_when_post_send_cleanup_fails(self) -> None:
        """P1: A verified send remains successful even when post-send page close fails."""
        config: dict[str, Any] = {
            "tiktok": {"recipient_retries": 1, "message_delay_seconds": [0, 0]},
            "recipients": [{"name": "Alice", "username": "alice"}],
        }
        with TemporaryDirectory() as tmp_dir:
            sender = TikTokSender(
                config,
                FakeNotifier(),
                RecordingVideoPool(),  # type: ignore[arg-type]
                state_store=DailyStateStore(Path(tmp_dir)),
                history_store=RunHistoryStore(Path(tmp_dir)),
            )
            dm_page = MockDeliveryPage(evaluate_result={"verified": True})
            sender.recipient_service.select_recipient_for_username_with_page = AsyncMock(return_value=(dm_page, "Alice"))
            sender._active_dm_page = dm_page
            sender._handle_tiktok_interruptions = AsyncMock()
            sender._verify_chat_header_identity = AsyncMock()
            sender._count_chat_items = AsyncMock(return_value=0)
            sender._wait_for_delivery_evidence = AsyncMock()

            video_page = MockVideoPage(["Alice @alice"])
            video_page.close = AsyncMock(side_effect=RuntimeError("browser crashed during close"))

            ctx = MagicMock()
            ctx.is_closed = lambda: False
            ctx.new_page = AsyncMock(return_value=video_page)

            with patch("asyncio.sleep", new_callable=AsyncMock):
                res = asyncio.run(sender._send_recipient_with_retries(ctx, config["recipients"][0]))

            self.assertEqual(res.status, RecipientStatus.SUCCESS)
            self.assertEqual(res.delivery_status, DeliveryStatus.VERIFIED)

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
                def __init__(self) -> None:
                    self.current_username: str | None = None

                async def select_recipient_for_username_with_page(
                    self,
                    page: Any,
                    username: str,
                    notifier: Any,
                    *,
                    configured_display_name: str | None = None,
                    reuse_existing_page: bool = False,
                ) -> tuple[Any, str]:
                    self.current_username = username
                    selection_calls.append(page)
                    pages_closed_during_selection.append(page.is_closed())
                    # Simulate page closing right after first recipient completes
                    if username == "alice":
                        page.closed = True
                    return page, configured_display_name or username

                async def extract_username_from_chat(self, page: Any) -> str | None:
                    return self.current_username

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
                def __init__(self) -> None:
                    self.current_username: str | None = None

                async def select_recipient_for_username_with_page(
                    self,
                    page: Any,
                    username: str,
                    notifier: Any,
                    *,
                    configured_display_name: str | None = None,
                    reuse_existing_page: bool = False,
                ) -> tuple[Any, str]:
                    self.current_username = username
                    selection_calls.append(page)
                    return page, configured_display_name or username

                async def extract_username_from_chat(self, page: Any) -> str | None:
                    return self.current_username

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
