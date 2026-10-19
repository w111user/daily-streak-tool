"""Unit tests for TikTok streak count detection, caching, and display."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from daily_streak.cli import EXIT_OK, run_cli
from daily_streak.config import save_config
from daily_streak.streak_reader import (
    DEFAULT_STREAK_TTL_SECONDS,
    StreakCache,
    StreakReader,
    StreakRecord,
    StreakStatus,
    format_streak_display,
)
from daily_streak.tui import TuiController


class FakeLocator:
    def __init__(self, count_val: int = 1, text_val: str = "") -> None:
        self._count_val = count_val
        self._text_val = text_val

    @property
    def first(self) -> FakeLocator:
        return self

    async def count(self) -> int:
        return self._count_val

    async def inner_text(self, timeout: int = 1000) -> str:
        return self._text_val


class FakePage:
    def __init__(
        self,
        *,
        chat_username: str = "alice",
        eval_result: dict | None = None,
        eval_raises: Exception | None = None,
    ) -> None:
        self.chat_username = chat_username
        self.eval_result = eval_result
        self.eval_raises = eval_raises

    def locator(self, selector: str) -> FakeLocator:
        if "[data-e2e='chat-uniqueid']" in selector:
            return FakeLocator(count_val=1, text_val=f"@{self.chat_username}")
        return FakeLocator(count_val=0, text_val="")

    async def evaluate(self, script: str) -> dict:
        if self.eval_raises is not None:
            raise self.eval_raises
        return self.eval_result or {}


class StreakReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.cache = StreakCache(self.data_dir)
        self.reader = StreakReader(self.cache)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_read_streak_available_with_count(self) -> None:
        """Test 1: Available status when count is detected in DOM."""
        page = FakePage(
            chat_username="alice",
            eval_result={"found": True, "type": "available", "count": 125, "evidence": "🔥 125"},
        )
        record = asyncio.run(self.reader.probe_page_for_streak(page, "alice"))
        self.assertEqual(record.status, StreakStatus.AVAILABLE)
        self.assertEqual(record.count, 125)
        self.assertFalse(record.stale)
        self.assertEqual(format_streak_display(record), "🔥 125 ngày")

    def test_read_streak_not_visible_when_chat_cleanly_loaded(self) -> None:
        """Test 2: Not visible status when chat loaded but no streak in DOM (never 0)."""
        page = FakePage(
            chat_username="bob",
            eval_result={"found": False, "chatLoaded": True, "type": "none", "count": None},
        )
        record = asyncio.run(self.reader.probe_page_for_streak(page, "bob"))
        self.assertEqual(record.status, StreakStatus.NOT_VISIBLE)
        self.assertIsNone(record.count)
        self.assertEqual(format_streak_display(record), "Không hiển thị")
        # Ensure count is strictly None, never 0
        self.assertNotEqual(record.count, 0)
        self.assertNotIn("0", format_streak_display(record))

    def test_read_streak_not_active_when_explicitly_lost(self) -> None:
        """Test 3: Not active status when clear indicator shows lost/ended streak."""
        page = FakePage(
            chat_username="charlie",
            eval_result={"found": True, "type": "not_active", "count": None, "evidence": "Streak lost"},
        )
        record = asyncio.run(self.reader.probe_page_for_streak(page, "charlie"))
        self.assertEqual(record.status, StreakStatus.NOT_ACTIVE)
        self.assertIsNone(record.count)
        self.assertEqual(format_streak_display(record), "Chưa kích hoạt")

    def test_read_streak_unknown_on_script_failure_or_timeout(self) -> None:
        """Test 4: Unknown status on evaluation exception or unhandled state."""
        page = FakePage(
            chat_username="david",
            eval_raises=RuntimeError("Renderer execution context destroyed"),
        )
        record = asyncio.run(self.reader.probe_page_for_streak(page, "david"))
        self.assertEqual(record.status, StreakStatus.UNKNOWN)
        self.assertIsNone(record.count)
        self.assertEqual(format_streak_display(record), "Chưa xác định")

    def test_username_isolation_prevents_misattribution(self) -> None:
        """Test 5: Cross-recipient isolation prevents attributing wrong conversation."""
        # Page opened conversation with 'eve', but probe expects 'frank'
        page = FakePage(
            chat_username="eve",
            eval_result={"found": True, "type": "available", "count": 99, "evidence": "🔥 99"},
        )
        record = asyncio.run(self.reader.probe_page_for_streak(page, "frank"))
        self.assertEqual(record.status, StreakStatus.UNKNOWN)
        self.assertIsNone(record.count)
        self.assertIn("mismatch", str(record.raw_evidence).lower())

    def test_cache_hit_and_ttl_expiration(self) -> None:
        """Test 6: Cache hit returns cached record; expired TTL marks stale."""
        now = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
        cache = StreakCache(self.data_dir, ttl_seconds=3600, clock=lambda: now)

        rec = StreakRecord(
            username="grace",
            status=StreakStatus.AVAILABLE,
            count=42,
            updated_at=now.isoformat(),
        )
        cache.set(rec)

        # Fresh read
        cached = cache.get("grace")
        self.assertIsNotNone(cached)
        self.assertEqual(cached.count, 42)
        self.assertFalse(cached.stale)

        # Time passes beyond TTL (2 hours later)
        future_time = now + timedelta(hours=2)
        cache_future = StreakCache(self.data_dir, ttl_seconds=3600, clock=lambda: future_time)
        cached_future = cache_future.get("grace")
        self.assertIsNotNone(cached_future)
        self.assertTrue(cached_future.stale)
        self.assertEqual(format_streak_display(cached_future), "🔥 42 ngày (cũ)")

    def test_refresh_failure_retains_cached_record_marked_stale(self) -> None:
        """Test 7: Failed refresh retains prior cached record marked stale."""
        rec = StreakRecord(
            username="heidi",
            status=StreakStatus.AVAILABLE,
            count=10,
            updated_at="2026-10-18T10:00:00+00:00",
        )
        self.cache.set(rec)

        # Mock recipient service raising an error during refresh
        class FailingService:
            async def select_recipient_for_username_with_page(self, *args, **kwargs):
                raise RuntimeError("Network timeout while navigating")

        failing_reader = StreakReader(self.cache)
        page = FakePage(chat_username="heidi")

        result = asyncio.run(
            failing_reader.read_recipient_streak(
                page,
                "heidi",
                recipient_service=FailingService(),
                force_refresh=True,
            )
        )
        self.assertTrue(result.stale)
        self.assertEqual(result.count, 10)
        self.assertEqual(result.status, StreakStatus.AVAILABLE)
        self.assertEqual(format_streak_display(result), "🔥 10 ngày (cũ)")

    def test_ui_display_never_shows_zero_when_unknown_or_not_visible(self) -> None:
        """Test 8: UI formatting guarantees no fake '0' is rendered."""
        self.assertEqual(format_streak_display(None), "Chưa xác định")

        unknown_rec = StreakRecord(username="u", status=StreakStatus.UNKNOWN, count=None)
        self.assertEqual(format_streak_display(unknown_rec), "Chưa xác định")
        self.assertNotIn("0", format_streak_display(unknown_rec))

        not_vis_rec = StreakRecord(username="nv", status=StreakStatus.NOT_VISIBLE, count=None)
        self.assertEqual(format_streak_display(not_vis_rec), "Không hiển thị")
        self.assertNotIn("0", format_streak_display(not_vis_rec))

        not_act_rec = StreakRecord(username="na", status=StreakStatus.NOT_ACTIVE, count=None)
        self.assertEqual(format_streak_display(not_act_rec), "Chưa kích hoạt")
        self.assertNotIn("0", format_streak_display(not_act_rec))


class StreakCliAndTuiIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.data_dir = self.root / "data"
        self.config_path = self.root / "config.json"
        self.config = {
            "data_dir": str(self.data_dir),
            "recipients": [
                {"name": "Alice", "username": "alice"},
                {"name": "Bob", "username": "bob"},
            ],
            "videos": ["https://vt.tiktok.com/example/"],
        }
        save_config(self.config, self.config_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_cli_recipients_list_without_show_streaks_preserves_legacy_format(self) -> None:
        """Ensure CLI recipients list without --show-streaks does not change existing format."""
        stdout = StringIO()
        exit_code = run_cli(
            ["--config", str(self.config_path), "recipients", "list"],
            stdout=stdout,
            stderr=StringIO(),
        )
        self.assertEqual(exit_code, EXIT_OK)
        output = stdout.getvalue()
        self.assertIn("Alice  @alice", output)
        self.assertIn("Bob  @bob", output)
        self.assertNotIn("[Streak:", output)

    def test_cli_recipients_list_with_show_streaks_shows_status(self) -> None:
        """CLI with --show-streaks enriches output with cached or unknown streak status."""
        cache = StreakCache(self.data_dir)
        now_iso = datetime.now(timezone.utc).isoformat()
        cache.set(
            StreakRecord(
                username="alice",
                status=StreakStatus.AVAILABLE,
                count=77,
                updated_at=now_iso,
            )
        )
        cache.set(
            StreakRecord(
                username="bob",
                status=StreakStatus.NOT_VISIBLE,
                count=None,
                updated_at=now_iso,
            )
        )

        stdout = StringIO()
        exit_code = run_cli(
            ["--config", str(self.config_path), "recipients", "list", "--show-streaks"],
            stdout=stdout,
            stderr=StringIO(),
        )
        self.assertEqual(exit_code, EXIT_OK)
        output = stdout.getvalue()
        self.assertIn("Alice  @alice  [Streak: 🔥 77 ngày]", output)
        self.assertIn("Bob  @bob  [Streak: Không hiển thị]", output)

    def test_cli_recipients_list_with_show_streaks_json(self) -> None:
        """CLI with --show-streaks and --json provides structured streak object."""
        cache = StreakCache(self.data_dir)
        now_iso = datetime.now(timezone.utc).isoformat()
        cache.set(
            StreakRecord(
                username="alice",
                status=StreakStatus.AVAILABLE,
                count=77,
                updated_at=now_iso,
            )
        )
        stdout = StringIO()
        exit_code = run_cli(
            ["--config", str(self.config_path), "recipients", "list", "--show-streaks", "--json"],
            stdout=stdout,
            stderr=StringIO(),
        )
        self.assertEqual(exit_code, EXIT_OK)
        payload = json.loads(stdout.getvalue())
        recipients = payload["recipients"]
        self.assertEqual(len(recipients), 2)
        alice = next(r for r in recipients if r["username"] == "alice")
        self.assertEqual(alice["streak"]["status"], "available")
        self.assertEqual(alice["streak"]["count"], 77)
        self.assertEqual(alice["streak_display"], "🔥 77 ngày")

        bob = next(r for r in recipients if r["username"] == "bob")
        self.assertIsNone(bob["streak"])
        self.assertEqual(bob["streak_display"], "Chưa xác định")

    def test_tui_controller_renders_streak_column(self) -> None:
        """TuiController.recipient_rows includes streak string from cache."""
        cache = StreakCache(self.data_dir)
        now_iso = datetime.now(timezone.utc).isoformat()
        cache.set(
            StreakRecord(
                username="alice",
                status=StreakStatus.NOT_VISIBLE,
                count=None,
                updated_at=now_iso,
            )
        )
        controller = TuiController(
            self.config_path,
            config_loader=lambda _: self.config,
            config_saver=lambda *_: None,
        )
        rows = controller.recipient_rows()
        self.assertEqual(len(rows), 2)
        alice_row = next(r for r in rows if r["username"] == "alice")
        self.assertEqual(alice_row["streak"], "Không hiển thị")

        bob_row = next(r for r in rows if r["username"] == "bob")
        self.assertEqual(bob_row["streak"], "Chưa xác định")

    def test_cli_refresh_streaks_success(self) -> None:
        """CLI recipients refresh-streaks executes and outputs updated streaks."""
        with patch("daily_streak.recipient_actions.refresh_streaks") as mock_refresh:
            mock_refresh.return_value = [
                {
                    "name": "Alice",
                    "username": "alice",
                    "streak": {"status": "available", "count": 99},
                    "streak_display": "🔥 99 ngày",
                },
                {
                    "name": "Bob",
                    "username": "bob",
                    "streak": {"status": "not_visible", "count": None},
                    "streak_display": "Không hiển thị",
                },
            ]
            stdout = StringIO()
            exit_code = run_cli(
                ["--config", str(self.config_path), "recipients", "refresh-streaks"],
                stdout=stdout,
                stderr=StringIO(),
            )
            self.assertEqual(exit_code, EXIT_OK)
            output = stdout.getvalue()
            self.assertIn("Alice  @alice  [Streak: 🔥 99 ngày]", output)
            self.assertIn("Bob  @bob  [Streak: Không hiển thị]", output)

    def test_cli_refresh_streaks_device_not_found_handled_gracefully(self) -> None:
        """CLI recipients refresh-streaks prints clean error when device is not found."""
        from daily_streak.android_streak_reader import AdbDeviceNotFoundError

        with patch("daily_streak.recipient_actions.refresh_streaks", side_effect=AdbDeviceNotFoundError("No device attached")):
            stderr = StringIO()
            exit_code = run_cli(
                ["--config", str(self.config_path), "recipients", "refresh-streaks"],
                stdout=StringIO(),
                stderr=stderr,
            )
            self.assertNotEqual(exit_code, EXIT_OK)
            err_output = stderr.getvalue()
            self.assertIn("Lỗi thiết bị Android", err_output)
            self.assertIn("No device attached", err_output)

    def test_cli_refresh_streaks_adb_unavailable_handled_gracefully(self) -> None:
        """CLI recipients refresh-streaks prints clean error when adb is missing."""
        from daily_streak.android_streak_reader import AdbUnavailableError

        with patch("daily_streak.recipient_actions.refresh_streaks", side_effect=AdbUnavailableError("adb command not found")):
            stderr = StringIO()
            exit_code = run_cli(
                ["--config", str(self.config_path), "recipients", "refresh-streaks"],
                stdout=StringIO(),
                stderr=stderr,
            )
            self.assertNotEqual(exit_code, EXIT_OK)
            err_output = stderr.getvalue()
            self.assertIn("Lỗi ADB", err_output)

    def test_tui_controller_refresh_streaks_adb_error_raises_tui_input_error(self) -> None:
        """TuiController.refresh_streaks translates AdbError into user-friendly TuiInputError."""
        from daily_streak.android_streak_reader import AdbDeviceNotFoundError
        from daily_streak.tui import TuiInputError

        with patch("daily_streak.recipient_actions.refresh_streaks", side_effect=AdbDeviceNotFoundError("Device offline")):
            controller = TuiController(
                self.config_path,
                config_loader=lambda _: self.config,
                config_saver=lambda *_: None,
            )
            with self.assertRaises(TuiInputError) as ctx:
                asyncio.run(controller.refresh_streaks())
            self.assertIn("Không thể làm mới streak qua Android", str(ctx.exception))
            self.assertIn("Device offline", str(ctx.exception))
