"""Regression tests for TikTok recipient recovery by verified user ID."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import AsyncMock, MagicMock

from daily_streak.api_streak_reader import ImUserProfile
from daily_streak.history import RunHistoryStore
from daily_streak.models import DeliveryStatus, RecipientStatus
from daily_streak.recipients import (
    AmbiguousConversationError,
    RecipientIdentityMismatchError,
    RecipientService,
    normalize_name_for_comparison,
)
from daily_streak.sender import DeliveryConfirmedNotSentError, TikTokSender
from daily_streak.state import DailyStateStore
from daily_streak.streak_reader import StreakCache, StreakRecord, StreakStatus


class MockConversationItem:
    def __init__(self, nickname: str) -> None:
        self.nickname = nickname

    def locator(self, selector: str) -> Any:
        m = MagicMock()
        m.inner_text = AsyncMock(return_value=self.nickname)
        return m

    async def click(self, **kwargs: Any) -> None:
        pass


class MockConversationList:
    def __init__(self, items: list[str]) -> None:
        self.items = [MockConversationItem(name) for name in items]

    async def count(self) -> int:
        return len(self.items)

    def nth(self, index: int) -> Any:
        return self.items[index]

    async def evaluate(self, script: str, **kwargs: Any) -> None:
        pass


class MockPageWithConversations:
    def __init__(self, nicknames: list[str]) -> None:
        self.nicknames = nicknames
        self.url = "https://www.tiktok.com/messages"

    def is_closed(self) -> bool:
        return False

    def locator(self, selector: str) -> Any:
        loc = MagicMock()
        loc.first = loc
        loc.filter = MagicMock(return_value=loc)
        loc.count = AsyncMock(return_value=1)
        loc.locator = MagicMock(return_value=loc)
        loc.inner_text = AsyncMock(return_value=self.nicknames[0] if self.nicknames else "")
        loc.bounding_box = AsyncMock(return_value={"x": 10, "y": 10})
        loc.is_visible = AsyncMock(return_value=True)
        loc.get_attribute = AsyncMock(return_value="true")
        loc.scroll_into_view_if_needed = AsyncMock()
        loc.click = AsyncMock()
        loc.evaluate = AsyncMock(return_value=None)
        loc.wait_for = AsyncMock()
        return loc

    async def evaluate(self, script: str, **kwargs: Any) -> Any:
        return []

    async def wait_for_timeout(self, ms: int) -> None:
        pass


class RecipientRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_normalize_name_for_comparison(self) -> None:
        # NFC vs NFD
        nfc = "còii"
        nfd = "co\u0300ii"
        self.assertEqual(normalize_name_for_comparison(nfc), normalize_name_for_comparison(nfd))

        # Unusual whitespace, non-breaking space, zero-width space
        spaces = " Alice \u00a0 \u200b  Bob "
        expected = "alice bob"
        self.assertEqual(normalize_name_for_comparison(spaces), expected)

        # Emoji and case folding
        with_emoji = "  Ngọc Trâm ✨ 🍅 "
        self.assertEqual(normalize_name_for_comparison(with_emoji), "ngọc trâm ✨ 🍅")

    async def test_normal_display_name_match_does_not_invoke_profile_resolver(self) -> None:
        service = RecipientService()
        mock_page = MockPageWithConversations(["Alice"])

        conv_list = MockConversationList(["Alice"])
        service._load_messages_with_renderer_recovery = AsyncMock(  # type: ignore[method-assign]
            return_value=(mock_page, (conv_list, conv_list, False))
        )
        service._handle_tiktok_interruptions = AsyncMock()  # type: ignore[method-assign]
        service.wait_for_message_input = AsyncMock()  # type: ignore[method-assign]

        resolver_called = False

        def fake_resolver(uid: int) -> Any:
            nonlocal resolver_called
            resolver_called = True
            return None

        page, matched_name = await service.select_recipient_for_username_with_page(
            mock_page,
            "alice",
            MagicMock(),
            configured_display_name="Alice",
            user_id=12345,
            profile_resolver=fake_resolver,
        )

        self.assertEqual(matched_name, "Alice")
        self.assertFalse(resolver_called)

    async def test_stale_display_name_recovers_via_verified_user_id(self) -> None:
        service = RecipientService()
        mock_page = MockPageWithConversations(["Alice Updated", "Bob"])

        # List contains the updated name "Alice Updated"
        conv_list = MockConversationList(["Alice Updated", "Bob"])
        service._load_messages_with_renderer_recovery = AsyncMock(  # type: ignore[method-assign]
            return_value=(mock_page, (conv_list, conv_list, False))
        )
        service._handle_tiktok_interruptions = AsyncMock()  # type: ignore[method-assign]
        service.wait_for_message_input = AsyncMock()  # type: ignore[method-assign]

        recovered_records: list[tuple[str, str, int]] = []

        def on_recovered(new_name: str, new_user: str, uid: int) -> None:
            recovered_records.append((new_name, new_user, uid))

        def fake_resolver(uid: int) -> ImUserProfile:
            self.assertEqual(uid, 98765)
            return ImUserProfile(user_id=98765, unique_id="alice", nick_name="Alice Updated")

        # Configured with stale name "Alice Old"
        page, matched_name = await service.select_recipient_for_username_with_page(
            mock_page,
            "alice",
            MagicMock(),
            configured_display_name="Alice Old",
            user_id=98765,
            profile_resolver=fake_resolver,
            on_recipient_recovered=on_recovered,
        )

        self.assertEqual(matched_name, "Alice Updated")
        self.assertEqual(recovered_records, [("Alice Updated", "alice", 98765)])

    async def test_refreshed_profile_with_unicode_and_emojis(self) -> None:
        service = RecipientService()
        emoji_name = "còii cte🍅"
        mock_page = MockPageWithConversations([emoji_name])

        conv_list = MockConversationList([emoji_name])
        service._load_messages_with_renderer_recovery = AsyncMock(  # type: ignore[method-assign]
            return_value=(mock_page, (conv_list, conv_list, False))
        )
        service._handle_tiktok_interruptions = AsyncMock()  # type: ignore[method-assign]
        service.wait_for_message_input = AsyncMock()  # type: ignore[method-assign]

        def fake_resolver(uid: int) -> ImUserProfile:
            return ImUserProfile(user_id=111, unique_id="coii", nick_name=emoji_name)

        page, matched_name = await service.select_recipient_for_username_with_page(
            mock_page,
            "coii",
            MagicMock(),
            configured_display_name="coii old",
            user_id=111,
            profile_resolver=fake_resolver,
        )

        self.assertEqual(matched_name, emoji_name)

    async def test_profile_user_id_mismatch_is_rejected_and_raises(self) -> None:
        service = RecipientService()
        mock_page = MockPageWithConversations(["Other Person"])

        conv_list = MockConversationList(["Other Person"])
        service._load_messages_with_renderer_recovery = AsyncMock(  # type: ignore[method-assign]
            return_value=(mock_page, (conv_list, conv_list, False))
        )
        service._handle_tiktok_interruptions = AsyncMock()  # type: ignore[method-assign]
        service.wait_for_message_input = AsyncMock()  # type: ignore[method-assign]

        callback_called = False

        def on_recovered(*args: Any) -> None:
            nonlocal callback_called
            callback_called = True

        # Resolver returns a profile for a DIFFERENT UID
        def mismatched_resolver(uid: int) -> ImUserProfile:
            return ImUserProfile(user_id=999999, unique_id="wrong_user", nick_name="Other Person")

        with self.assertRaises(ValueError) as cm:
            await service.select_recipient_for_username_with_page(
                mock_page,
                "target_user",
                MagicMock(),
                configured_display_name="Old Target Name",
                user_id=12345,
                profile_resolver=mismatched_resolver,
                on_recipient_recovered=on_recovered,
            )

        self.assertIn("Could not find conversation for display name", str(cm.exception))
        self.assertFalse(callback_called, "Mismatched UID must never trigger metadata update callback!")

    async def test_profile_resolver_failure_or_none_raises_safely(self) -> None:
        service = RecipientService()
        mock_page = MockPageWithConversations(["Random Person"])

        conv_list = MockConversationList(["Random Person"])
        service._load_messages_with_renderer_recovery = AsyncMock(  # type: ignore[method-assign]
            return_value=(mock_page, (conv_list, conv_list, False))
        )
        service._handle_tiktok_interruptions = AsyncMock()  # type: ignore[method-assign]

        # Case 1: Resolver returns None
        with self.assertRaises(ValueError):
            await service.select_recipient_for_username_with_page(
                mock_page,
                "user",
                MagicMock(),
                configured_display_name="Stale Name",
                user_id=12345,
                profile_resolver=lambda uid: None,
            )

        # Case 2: Resolver raises network exception
        def failing_resolver(uid: int) -> Any:
            raise RuntimeError("TikTok API 500 error")

        with self.assertRaises(ValueError):
            await service.select_recipient_for_username_with_page(
                mock_page,
                "user",
                MagicMock(),
                configured_display_name="Stale Name",
                user_id=12345,
                profile_resolver=failing_resolver,
            )

    async def test_recipient_without_stored_user_id_raises_without_speculative_requests(self) -> None:
        service = RecipientService()
        mock_page = MockPageWithConversations(["Anyone"])

        conv_list = MockConversationList(["Anyone"])
        service._load_messages_with_renderer_recovery = AsyncMock(  # type: ignore[method-assign]
            return_value=(mock_page, (conv_list, conv_list, False))
        )
        service._handle_tiktok_interruptions = AsyncMock()  # type: ignore[method-assign]

        resolver_called = False

        def probe_resolver(uid: int) -> Any:
            nonlocal resolver_called
            resolver_called = True
            return None

        # user_id is None
        with self.assertRaises(ValueError):
            await service.select_recipient_for_username_with_page(
                mock_page,
                "user",
                MagicMock(),
                configured_display_name="Missing Display Name",
                user_id=None,
                profile_resolver=probe_resolver,
            )

        self.assertFalse(resolver_called, "Must not invoke resolver when user_id is None")

    async def test_ambiguous_display_names_raise_and_never_send_to_wrong_recipient(self) -> None:
        service = RecipientService()
        mock_page = MockPageWithConversations(["Dupe", "Dupe"])

        # Two duplicate rows with the same display name "Dupe"
        conv_list = MockConversationList(["Dupe", "Dupe"])
        service._load_messages_with_renderer_recovery = AsyncMock(  # type: ignore[method-assign]
            return_value=(mock_page, (conv_list, conv_list, False))
        )
        service._handle_tiktok_interruptions = AsyncMock()  # type: ignore[method-assign]

        # Resolver returns "Dupe" for UID
        def fake_resolver(uid: int) -> ImUserProfile:
            return ImUserProfile(user_id=12345, unique_id="user", nick_name="Dupe")

        # Ambiguous names must raise AmbiguousConversationError rather than choosing arbitrary chat
        with self.assertRaises(AmbiguousConversationError):
            await service.select_recipient_for_username_with_page(
                mock_page,
                "user",
                MagicMock(),
                configured_display_name="Dupe",
                user_id=12345,
                profile_resolver=fake_resolver,
            )

    async def test_missing_display_name_with_ambiguous_conversations_raises_and_never_sends(self) -> None:
        """When configured display name is empty, UID recovery must reject ambiguous conversations."""
        service = RecipientService()
        mock_page = MockPageWithConversations(["SharedNick", "SharedNick"])

        conv_list = MockConversationList(["SharedNick", "SharedNick"])
        service._load_messages_with_renderer_recovery = AsyncMock(  # type: ignore[method-assign]
            return_value=(mock_page, (conv_list, conv_list, False))
        )
        service._handle_tiktok_interruptions = AsyncMock()  # type: ignore[method-assign]

        def fake_resolver(uid: int) -> ImUserProfile:
            return ImUserProfile(user_id=888, unique_id="user888", nick_name="SharedNick")

        # Configured display name is None/empty -> UID resolves to "SharedNick" -> must reject ambiguous!
        with self.assertRaises(AmbiguousConversationError):
            await service.select_recipient_for_username_with_page(
                mock_page,
                "user888",
                MagicMock(),
                configured_display_name=None,
                user_id=888,
                profile_resolver=fake_resolver,
            )

    async def test_strict_config_matching_priority_shared_display_name(self) -> None:
        """When two config entries share a display name, only the entry matching the verified UID is updated."""
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            cfg_path = tmp_path / "config.json"
            initial_config = {
                "recipients": [
                    {"name": "Friend", "username": "user_one", "user_id": 111},
                    {"name": "Friend", "username": "user_two", "user_id": 222},
                ]
            }
            cfg_path.write_text(json.dumps(initial_config, indent=2))

            sender = TikTokSender(
                initial_config,
                MagicMock(),
                MagicMock(),
                config_path=cfg_path,
            )

            # Recover user 111
            sender._on_recipient_recovered(
                initial_config["recipients"][0],
                "Friend Updated ✨",
                "user_one_new",
                111,
            )

            saved_cfg = json.loads(cfg_path.read_text())
            # Recipient 1 must be updated
            self.assertEqual(saved_cfg["recipients"][0]["name"], "Friend Updated ✨")
            self.assertEqual(saved_cfg["recipients"][0]["username"], "user_one_new")
            self.assertEqual(saved_cfg["recipients"][0]["user_id"], 111)

            # Recipient 2 must remain completely untouched
            self.assertEqual(saved_cfg["recipients"][1]["name"], "Friend")
            self.assertEqual(saved_cfg["recipients"][1]["username"], "user_two")
            self.assertEqual(saved_cfg["recipients"][1]["user_id"], 222)

    async def test_strict_config_matching_ambiguous_username_skips_config_update(self) -> None:
        """When multiple config entries match by username and UID is absent, config is not updated."""
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            cfg_path = tmp_path / "config.json"
            initial_config = {
                "recipients": [
                    {"name": "Alice 1", "username": "dupe_handle"},
                    {"name": "Alice 2", "username": "dupe_handle"},
                ]
            }
            cfg_path.write_text(json.dumps(initial_config, indent=2))

            sender = TikTokSender(
                initial_config,
                MagicMock(),
                MagicMock(),
                config_path=cfg_path,
            )

            # Attempt recovery without a matching UID
            sender._on_recipient_recovered(
                {"name": "Alice 1", "username": "dupe_handle"},
                "Alice New",
                "dupe_new",
                999,
            )

            saved_cfg = json.loads(cfg_path.read_text())
            # Neither entry should have been corrupted
            self.assertEqual(saved_cfg["recipients"][0]["username"], "dupe_handle")
            self.assertEqual(saved_cfg["recipients"][1]["username"], "dupe_handle")
            self.assertNotIn("user_id", saved_cfg["recipients"][0])

    async def test_strict_config_matching_no_match_skips_config_update(self) -> None:
        """When no matching entry exists in config, config file is not modified."""
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            cfg_path = tmp_path / "config.json"
            initial_config = {
                "recipients": [
                    {"name": "Alice", "username": "alice", "user_id": 111},
                ]
            }
            original_content = json.dumps(initial_config, indent=2)
            cfg_path.write_text(original_content)

            sender = TikTokSender(
                initial_config,
                MagicMock(),
                MagicMock(),
                config_path=cfg_path,
            )

            # Recover an unknown recipient
            sender._on_recipient_recovered(
                {"name": "Unknown", "username": "unknown"},
                "Unknown Updated",
                "unknown_new",
                9999,
            )

            # File should not be modified
            saved_cfg = json.loads(cfg_path.read_text())
            self.assertEqual(saved_cfg["recipients"][0]["name"], "Alice")
            self.assertEqual(saved_cfg["recipients"][0]["username"], "alice")

    async def test_default_profile_resolver_wiring_and_cookie_resolution(self) -> None:
        """Verify resolve_profile_by_user_id builds ApiStreakReader with correct account cookie file."""
        from daily_streak.recipients import resolve_profile_by_user_id

        with TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            acc_dir = base_dir / "accounts" / "acc1"
            acc_dir.mkdir(parents=True)
            cfg_path = acc_dir / "config.json"
            cookie_path = acc_dir / "my_cookies.json"
            cookie_path.write_text(json.dumps([{"name": "sessionid", "value": "test_sess_123"}]))

            config = {
                "_config_path": str(cfg_path),
                "cookie_file": "my_cookies.json",  # Relative to cfg_path.parent
            }

            def fake_http_client(url: str, headers: dict[str, str], timeout: float) -> tuple[int, dict[str, str], bytes]:
                # Verify cookie header contains the sessionid from my_cookies.json
                self.assertIn("sessionid=test_sess_123", headers.get("Cookie", ""))
                self.assertIn("user_ids=%5B4567%5D", url)
                body = json.dumps({
                    "status_code": 0,
                    "users": [
                        {
                            "im_user_profile": {
                                "user_id": 4567,
                                "unique_id": "resolved_user",
                                "nick_name": "Resolved Name",
                            }
                        }
                    ],
                }).encode("utf-8")
                return 200, {}, body

            profile = resolve_profile_by_user_id(
                4567,
                config=config,
                http_client=fake_http_client,
            )

            self.assertIsNotNone(profile)
            self.assertEqual(profile.user_id, 4567)
            self.assertEqual(profile.unique_id, "resolved_user")
            self.assertEqual(profile.nick_name, "Resolved Name")

    async def test_profile_resolver_supports_sync_and_async_callables(self) -> None:
        """Verify profile_resolver accepts both async coroutine functions and sync callables."""
        service = RecipientService()
        mock_page = MockPageWithConversations(["Target Name"])
        conv_list = MockConversationList(["Target Name"])
        service._load_messages_with_renderer_recovery = AsyncMock(  # type: ignore[method-assign]
            return_value=(mock_page, (conv_list, conv_list, False))
        )
        service._handle_tiktok_interruptions = AsyncMock()  # type: ignore[method-assign]
        service.wait_for_message_input = AsyncMock()  # type: ignore[method-assign]

        # 1. Async resolver
        async def async_resolver(uid: int) -> ImUserProfile:
            await asyncio.sleep(0.001)
            return ImUserProfile(user_id=123, unique_id="user123", nick_name="Target Name")

        page, name = await service.select_recipient_for_username_with_page(
            mock_page,
            "user123",
            MagicMock(),
            configured_display_name="Old Name",
            user_id=123,
            profile_resolver=async_resolver,
        )
        self.assertEqual(name, "Target Name")

        # 2. Sync resolver
        def sync_resolver(uid: int) -> ImUserProfile:
            return ImUserProfile(user_id=123, unique_id="user123", nick_name="Target Name")

        page2, name2 = await service.select_recipient_for_username_with_page(
            mock_page,
            "user123",
            MagicMock(),
            configured_display_name="Old Name",
            user_id=123,
            profile_resolver=sync_resolver,
        )
        self.assertEqual(name2, "Target Name")

    async def test_sender_full_recovery_persists_config_and_cache(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            cfg_path = tmp_path / "config.json"
            initial_config: dict[str, Any] = {
                "cookie_file": str(tmp_path / "cookies.json"),
                "tiktok": {"recipient_retries": 0, "message_delay_seconds": [0, 0]},
                "recipients": [
                    {"name": "Old Nickname", "username": "old_user", "user_id": 55555},
                ],
                "custom_unrelated_field": "preserved_value",
            }
            cfg_path.write_text(json.dumps(initial_config, indent=2))

            cache = StreakCache(tmp_path)
            cache.set(StreakRecord(username="old_user", status=StreakStatus.AVAILABLE, count=10, user_id=55555))

            shared_page = MockPageWithConversations(["New Nickname ✨"])

            def fake_resolver(uid: int) -> ImUserProfile:
                return ImUserProfile(user_id=55555, unique_id="new_user", nick_name="New Nickname ✨")

            # Mock recipient service to trigger recovery
            custom_service = RecipientService()
            conv_list = MockConversationList(["New Nickname ✨"])
            custom_service._load_messages_with_renderer_recovery = AsyncMock(  # type: ignore[method-assign]
                return_value=(shared_page, (conv_list, conv_list, False))
            )
            custom_service._handle_tiktok_interruptions = AsyncMock()  # type: ignore[method-assign]
            custom_service.wait_for_message_input = AsyncMock()  # type: ignore[method-assign]
            custom_service.extract_username_from_chat = AsyncMock(return_value="new_user")  # type: ignore[method-assign]

            fake_pool = MagicMock()
            fake_pool.random_video = lambda: "https://www.tiktok.com/@test/video/123"

            sender = TikTokSender(
                initial_config,
                MagicMock(),
                fake_pool,
                recipient_service=custom_service,
                state_store=DailyStateStore(tmp_path),
                history_store=RunHistoryStore(tmp_path),
                config_path=cfg_path,
                profile_resolver=fake_resolver,
                streak_cache=cache,
            )
            sender._handle_tiktok_interruptions = AsyncMock()  # type: ignore[method-assign]
            sender._share_video_card = AsyncMock()  # type: ignore[method-assign]
            sender._wait_for_delivery_evidence = AsyncMock()  # type: ignore[method-assign]
            sender._sleep_between_messages = AsyncMock()  # type: ignore[method-assign]

            context = MagicMock()
            context.is_closed = lambda: False
            context.new_page = AsyncMock(return_value=shared_page)

            summary = await sender.send_recipients(context, dm_page=shared_page)

            self.assertEqual(summary.success_count, 1)

            # Check config.json was persisted with updated name & username while preserving custom fields
            saved_cfg = json.loads(cfg_path.read_text())
            rec = saved_cfg["recipients"][0]
            self.assertEqual(rec["name"], "New Nickname ✨")
            self.assertEqual(rec["username"], "new_user")
            self.assertEqual(rec["user_id"], 55555)
            self.assertEqual(saved_cfg["custom_unrelated_field"], "preserved_value")

            # Check streak cache was updated
            rec_cache = cache.get("new_user")
            self.assertIsNotNone(rec_cache)
            self.assertEqual(rec_cache.user_id, 55555)

    async def test_multi_account_recovery_isolation(self) -> None:
        """Verify recovery in Account A modifies only Account A's config and state, never Account B."""
        with TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)

            # Account A setup
            acc_a_dir = base_dir / "accounts" / "account-a"
            acc_a_dir.mkdir(parents=True)
            acc_a_cfg_path = acc_a_dir / "config.json"
            acc_a_config = {
                "account_name": "account-a",
                "cookie_file": str(acc_a_dir / "cookies.json"),
                "recipients": [{"name": "A Old", "username": "a_user", "user_id": 101}],
            }
            acc_a_cfg_path.write_text(json.dumps(acc_a_config))

            # Account B setup
            acc_b_dir = base_dir / "accounts" / "account-b"
            acc_b_dir.mkdir(parents=True)
            acc_b_cfg_path = acc_b_dir / "config.json"
            acc_b_config = {
                "account_name": "account-b",
                "cookie_file": str(acc_b_dir / "cookies.json"),
                "recipients": [{"name": "B Name", "username": "b_user", "user_id": 202}],
            }
            acc_b_cfg_path.write_text(json.dumps(acc_b_config))

            # Resolver only knows about Account A's UID
            def resolver(uid: int) -> ImUserProfile:
                if uid == 101:
                    return ImUserProfile(user_id=101, unique_id="a_user_new", nick_name="A New ✨")
                raise ValueError(f"Unknown UID {uid}")

            sender_a = TikTokSender(
                acc_a_config,
                MagicMock(),
                MagicMock(),
                config_path=acc_a_cfg_path,
                profile_resolver=resolver,
            )

            # Trigger recovery for Account A
            sender_a._on_recipient_recovered(
                acc_a_config["recipients"][0],
                "A New ✨",
                "a_user_new",
                101,
            )

            # Verify Account A updated
            updated_a = json.loads(acc_a_cfg_path.read_text())
            self.assertEqual(updated_a["recipients"][0]["name"], "A New ✨")
            self.assertEqual(updated_a["recipients"][0]["username"], "a_user_new")

            # Verify Account B remained completely unchanged
            untouched_b = json.loads(acc_b_cfg_path.read_text())
            self.assertEqual(untouched_b["recipients"][0]["name"], "B Name")
            self.assertEqual(untouched_b["recipients"][0]["username"], "b_user")

    async def test_uid_recovery_chat_header_mismatch_prevents_sending_native_card(self) -> None:
        """Blocker 1: When UID resolves to expected profile and DM list matches nickname,

        if the opened chat header exposes a different username, fail closed with
        DeliveryConfirmedNotSentError and NEVER execute video-sharing operation.
        """
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            cfg: dict[str, Any] = {
                "tiktok": {"recipient_retries": 0, "message_format": "native_video_card"},
                "recipients": [{"name": "Alice Old", "username": "alice_real", "user_id": 12345}],
            }
            # Profile resolver returns expected profile with new nickname
            def resolver(uid: int) -> ImUserProfile:
                return ImUserProfile(user_id=12345, unique_id="alice_real", nick_name="Alice Recovered")

            shared_page = MockPageWithConversations(["Alice Recovered"])
            conv_list = MockConversationList(["Alice Recovered"])

            custom_service = RecipientService()
            custom_service._load_messages_with_renderer_recovery = AsyncMock(  # type: ignore[method-assign]
                return_value=(shared_page, (conv_list, conv_list, False))
            )
            custom_service._handle_tiktok_interruptions = AsyncMock()  # type: ignore[method-assign]
            custom_service.wait_for_message_input = AsyncMock()  # type: ignore[method-assign]
            # Mismatch: opened chat header exposes a DIFFERENT username!
            custom_service.extract_username_from_chat = AsyncMock(return_value="imposter_user")  # type: ignore[method-assign]

            fake_pool = MagicMock()
            fake_pool.random_video = lambda: "https://www.tiktok.com/@test/video/123"

            sender = TikTokSender(
                cfg,
                MagicMock(),
                fake_pool,
                recipient_service=custom_service,
                state_store=DailyStateStore(tmp_path),
                history_store=RunHistoryStore(tmp_path),
                profile_resolver=resolver,
            )
            sender._handle_tiktok_interruptions = AsyncMock()  # type: ignore[method-assign]
            sender._share_video_card = AsyncMock()  # type: ignore[method-assign]

            ctx = MagicMock()
            ctx.is_closed = lambda: False
            ctx.new_page = AsyncMock(return_value=shared_page)

            with self.assertRaises(DeliveryConfirmedNotSentError) as cm:
                await sender._send_recipient_once(ctx, cfg["recipients"][0], "https://www.tiktok.com/@test/video/123", dm_page=shared_page)

            self.assertIn("does not match expected @alice_real", str(cm.exception))
            # Critical requirement: video sharing was asserted NOT to run!
            sender._share_video_card.assert_not_awaited()

    async def test_uid_recovery_chat_header_mismatch_prevents_sending_raw_url(self) -> None:
        """Blocker 1: Raw URL flow fails closed and never sends message on chat header mismatch."""
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            cfg: dict[str, Any] = {
                "tiktok": {"recipient_retries": 0, "message_format": "raw_url"},
                "recipients": [{"name": "Alice Old", "username": "alice_real", "user_id": 12345}],
            }
            def resolver(uid: int) -> ImUserProfile:
                return ImUserProfile(user_id=12345, unique_id="alice_real", nick_name="Alice Recovered")

            shared_page = MockPageWithConversations(["Alice Recovered"])
            conv_list = MockConversationList(["Alice Recovered"])

            custom_service = RecipientService()
            custom_service._load_messages_with_renderer_recovery = AsyncMock(  # type: ignore[method-assign]
                return_value=(shared_page, (conv_list, conv_list, False))
            )
            custom_service._handle_tiktok_interruptions = AsyncMock()  # type: ignore[method-assign]
            custom_service.wait_for_message_input = AsyncMock()  # type: ignore[method-assign]
            # Mismatch in chat header!
            custom_service.extract_username_from_chat = AsyncMock(return_value="imposter_user")  # type: ignore[method-assign]

            sender = TikTokSender(
                cfg,
                MagicMock(),
                MagicMock(),
                recipient_service=custom_service,
                state_store=DailyStateStore(tmp_path),
                history_store=RunHistoryStore(tmp_path),
                profile_resolver=resolver,
            )
            sender._handle_tiktok_interruptions = AsyncMock()  # type: ignore[method-assign]
            sender._send_message = AsyncMock()  # type: ignore[method-assign]

            ctx = MagicMock()
            ctx.is_closed = lambda: False
            ctx.new_page = AsyncMock(return_value=shared_page)

            with self.assertRaises(DeliveryConfirmedNotSentError) as cm:
                await sender._send_recipient_once(ctx, cfg["recipients"][0], "https://www.tiktok.com/@test/video/123", dm_page=shared_page)

            self.assertIn("does not match expected @alice_real", str(cm.exception))
            # Critical requirement: message sending was asserted NOT to run!
            sender._send_message.assert_not_awaited()

    async def test_uid_recovery_chat_header_missing_prevents_sending(self) -> None:
        """Blocker 1: Missing or unreadable chat header fails closed and never sends."""
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            cfg: dict[str, Any] = {
                "tiktok": {"recipient_retries": 0, "message_format": "native_video_card"},
                "recipients": [{"name": "Alice Old", "username": "alice_real", "user_id": 12345}],
            }
            def resolver(uid: int) -> ImUserProfile:
                return ImUserProfile(user_id=12345, unique_id="alice_real", nick_name="Alice Recovered")

            shared_page = MockPageWithConversations(["Alice Recovered"])
            conv_list = MockConversationList(["Alice Recovered"])

            custom_service = RecipientService()
            custom_service._load_messages_with_renderer_recovery = AsyncMock(  # type: ignore[method-assign]
                return_value=(shared_page, (conv_list, conv_list, False))
            )
            custom_service._handle_tiktok_interruptions = AsyncMock()  # type: ignore[method-assign]
            custom_service.wait_for_message_input = AsyncMock()  # type: ignore[method-assign]
            # Header username cannot be read / returns empty string
            custom_service.extract_username_from_chat = AsyncMock(return_value="")  # type: ignore[method-assign]

            fake_pool = MagicMock()
            fake_pool.random_video = lambda: "https://www.tiktok.com/@test/video/123"

            sender = TikTokSender(
                cfg,
                MagicMock(),
                fake_pool,
                recipient_service=custom_service,
                state_store=DailyStateStore(tmp_path),
                history_store=RunHistoryStore(tmp_path),
                profile_resolver=resolver,
            )
            sender._handle_tiktok_interruptions = AsyncMock()  # type: ignore[method-assign]
            sender._share_video_card = AsyncMock()  # type: ignore[method-assign]

            ctx = MagicMock()
            ctx.is_closed = lambda: False
            ctx.new_page = AsyncMock(return_value=shared_page)

            with self.assertRaises(DeliveryConfirmedNotSentError) as cm:
                await sender._send_recipient_once(ctx, cfg["recipients"][0], "https://www.tiktok.com/@test/video/123", dm_page=shared_page)

            self.assertIn("missing or empty", str(cm.exception))
            sender._share_video_card.assert_not_awaited()

    async def test_uid_recovery_chat_header_valid_allows_sending(self) -> None:
        """Blocker 1: Valid matching chat header (with consistent normalization) allows sending."""
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            cfg: dict[str, Any] = {
                "tiktok": {"recipient_retries": 0, "message_format": "native_video_card"},
                "recipients": [{"name": "Alice Old", "username": "alice_real", "user_id": 12345}],
            }
            def resolver(uid: int) -> ImUserProfile:
                return ImUserProfile(user_id=12345, unique_id="alice_real", nick_name="Alice Recovered")

            shared_page = MockPageWithConversations(["Alice Recovered"])
            conv_list = MockConversationList(["Alice Recovered"])

            custom_service = RecipientService()
            custom_service._load_messages_with_renderer_recovery = AsyncMock(  # type: ignore[method-assign]
                return_value=(shared_page, (conv_list, conv_list, False))
            )
            custom_service._handle_tiktok_interruptions = AsyncMock()  # type: ignore[method-assign]
            custom_service.wait_for_message_input = AsyncMock()  # type: ignore[method-assign]
            # Valid match with leading @ and uppercase casing to test normalization
            custom_service.extract_username_from_chat = AsyncMock(return_value=" @ALICE_REAL ")  # type: ignore[method-assign]

            fake_pool = MagicMock()
            fake_pool.random_video = lambda: "https://www.tiktok.com/@test/video/123"

            sender = TikTokSender(
                cfg,
                MagicMock(),
                fake_pool,
                recipient_service=custom_service,
                state_store=DailyStateStore(tmp_path),
                history_store=RunHistoryStore(tmp_path),
                profile_resolver=resolver,
            )
            sender._handle_tiktok_interruptions = AsyncMock()  # type: ignore[method-assign]
            sender._share_video_card = AsyncMock()  # type: ignore[method-assign]
            sender._count_chat_items = AsyncMock(return_value=0)  # type: ignore[method-assign]
            sender._wait_for_delivery_evidence = AsyncMock()  # type: ignore[method-assign]

            ctx = MagicMock()
            ctx.is_closed = lambda: False
            ctx.new_page = AsyncMock(return_value=shared_page)

            await sender._send_recipient_once(ctx, cfg["recipients"][0], "https://www.tiktok.com/@test/video/123", dm_page=shared_page)

            # Share video card was called successfully!
            sender._share_video_card.assert_awaited_once()

    async def test_recipient_service_verify_identity_rejects_mismatch(self) -> None:
        """Blocker 1: RecipientService._select_recipient_by_display_name rejects mismatched header when verify_identity=True."""
        service = RecipientService()
        mock_page = MockPageWithConversations(["Alice"])
        conv_list = MockConversationList(["Alice"])
        service._load_messages_with_renderer_recovery = AsyncMock(  # type: ignore[method-assign]
            return_value=(mock_page, (conv_list, conv_list, False))
        )
        service._handle_tiktok_interruptions = AsyncMock()  # type: ignore[method-assign]
        service.wait_for_message_input = AsyncMock()  # type: ignore[method-assign]
        service.extract_username_from_chat = AsyncMock(return_value="different_user")  # type: ignore[method-assign]

        with self.assertRaises(RecipientIdentityMismatchError) as cm:
            await service.select_recipient_for_username_with_page(
                mock_page,
                "alice",
                MagicMock(),
                configured_display_name="Alice",
                verify_identity=True,
            )
        self.assertIn("does not match expected @alice", str(cm.exception))

    async def test_sender_construction_account_config_and_relative_cookie_isolation(self) -> None:
        """P1: Verify multi-account sender construction isolates cookies and config persistence."""
        from daily_streak.config import save_config, load_config
        from daily_streak.daemon import create_sender

        with TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            acc1_dir = base / "accounts" / "acc1"
            acc2_dir = base / "accounts" / "acc2"
            acc1_dir.mkdir(parents=True)
            acc2_dir.mkdir(parents=True)

            acc1_cfg_path = acc1_dir / "config.json"
            acc2_cfg_path = acc2_dir / "config.json"

            cfg1 = {
                "account_name": "acc1",
                "cookie_file": "cookies.json",
                "recipients": [{"name": "Old Name 1", "username": "user1"}],
            }
            cfg2 = {
                "account_name": "acc2",
                "cookie_file": "cookies.json",
                "recipients": [{"name": "Old Name 2", "username": "user2"}],
            }
            save_config(cfg1, acc1_cfg_path)
            save_config(cfg2, acc2_cfg_path)

            loaded1 = load_config(acc1_cfg_path)
            loaded2 = load_config(acc2_cfg_path)

            sender1 = create_sender(loaded1, config_path=acc1_cfg_path)
            sender2 = create_sender(loaded2, config_path=acc2_cfg_path)

            # 1. Verify relative cookie files resolve against their respective account config dirs
            self.assertEqual(sender1._resolve_cookie_file(), (acc1_dir / "cookies.json").resolve())
            self.assertEqual(sender2._resolve_cookie_file(), (acc2_dir / "cookies.json").resolve())
            self.assertNotEqual(sender1._resolve_cookie_file(), sender2._resolve_cookie_file())

            # 2. Recover recipient metadata in account 1
            sender1._on_recipient_recovered(
                loaded1["recipients"][0],
                "New Name 1 ✨",
                "user1",
                111222333,
            )

            # 3. Assert account 1 config on disk is updated with recovered metadata
            raw1 = json.loads(acc1_cfg_path.read_text(encoding="utf-8"))
            self.assertEqual(raw1["recipients"][0]["name"], "New Name 1 ✨")
            self.assertEqual(raw1["recipients"][0]["user_id"], 111222333)
            # Internal _config_path must never be written to disk
            self.assertNotIn("_config_path", raw1)

            # 4. Assert account 2 config on disk is completely untouched
            raw2 = json.loads(acc2_cfg_path.read_text(encoding="utf-8"))
            self.assertEqual(raw2["recipients"][0]["name"], "Old Name 2")
            self.assertNotIn("user_id", raw2["recipients"][0])


if __name__ == "__main__":
    unittest.main()
