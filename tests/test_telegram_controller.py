"""Unit tests for the independent Telegram Remote Controller."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from typing import Any
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

if "telegram" not in sys.modules:
    sys.modules["telegram"] = MagicMock()
if "telegram.ext" not in sys.modules:
    sys.modules["telegram.ext"] = MagicMock()

from daily_streak.cli import EXIT_CONFIGURATION, EXIT_RUNTIME, run_cli
from daily_streak.config import save_config
from daily_streak.daemon import DaemonRunner
from daily_streak.history import RunHistoryStore
from daily_streak.lock import DaemonLock, RunLock
from daily_streak.models import RunSummary
from daily_streak.state import PauseStore
from daily_streak.telegram_controller import (
    CONFIRMATION_EXPIRATION_SECONDS,
    DaemonService,
    ProcessDaemonManager,
    SystemdDaemonManager,
    TelegramController,
    create_daemon_service,
)


def sample_config(data_dir: Path) -> dict[str, Any]:
    return {
        "schedule": {"time": "09:30", "timezone": "Asia/Ho_Chi_Minh", "run_on_start": False},
        "data_dir": str(data_dir),
        "recipients": [{"name": "Test User", "username": "testuser"}],
        "videos": ["https://vt.tiktok.com/VIDEO1/"],
        "telegram": {
            "enabled": True,
            "bot_token": "123456:FAKE_TOKEN_XYZ",
            "chat_id": "123456789",
            "commands_enabled": True,
        },
    }


class FakeUpdate:
    def __init__(self, chat_id: str | int = "123456789", text: str = "") -> None:
        self.effective_chat = MagicMock()
        self.effective_chat.id = chat_id
        self.message = MagicMock()
        self.message.text = text
        self.message.chat_id = chat_id
        self.message.reply_text = AsyncMock()
        self.callback_query = None


class FakeCallbackQueryUpdate:
    def __init__(self, chat_id: str | int = "123456789", data: str = "") -> None:
        self.effective_chat = MagicMock()
        self.effective_chat.id = chat_id
        self.message = None
        self.callback_query = MagicMock()
        self.callback_query.data = data
        self.callback_query.message = MagicMock()
        self.callback_query.message.chat_id = chat_id
        self.callback_query.answer = AsyncMock()
        self.callback_query.edit_message_text = AsyncMock()


class TelegramControllerTests(unittest.TestCase):
    def test_pause_store_lifecycle(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir)
            store = PauseStore(data_dir)

            self.assertFalse(store.is_paused())
            self.assertEqual(store.get_pause_info(), {"paused": False})

            store.pause(reason="telegram")
            self.assertTrue(store.is_paused())
            info = store.get_pause_info()
            self.assertTrue(info["paused"])
            self.assertEqual(info["reason"], "telegram")
            self.assertIn("paused_at", info)

            # Persists to disk
            reloaded_store = PauseStore(data_dir)
            self.assertTrue(reloaded_store.is_paused())

            was_paused = store.resume()
            self.assertTrue(was_paused)
            self.assertFalse(store.is_paused())

            # Second resume returns False
            self.assertFalse(store.resume())

    def test_daemon_runner_skips_when_paused(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir)
            config_path = data_dir / "config.json"
            cfg = sample_config(data_dir)
            save_config(cfg, config_path)

            pause_store = PauseStore(data_dir)
            pause_store.pause()

            sender_called = False

            def fake_sender_factory(c: Any) -> Any:
                nonlocal sender_called
                sender_called = True
                m = MagicMock()
                m.send_daily_links = AsyncMock(return_value=RunSummary())
                m.notifier = MagicMock()
                m.notifier.batch_completion = AsyncMock()
                return m


            runner = DaemonRunner(
                config_path,
                config_loader=lambda _: cfg,
                sender_factory=fake_sender_factory,
                pause_store=pause_store,
            )

            # run_once with check_pause=True (default) skips
            result = asyncio.run(runner.run_once())
            self.assertIsNone(result)
            self.assertFalse(sender_called)

            # run_once with check_pause=False runs anyway
            result = asyncio.run(runner.run_once(check_pause=False))
            self.assertIsNotNone(result)
            self.assertTrue(sender_called)

    def test_process_daemon_manager_delegation(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir)
            config_path = data_dir / "config.json"
            cfg = sample_config(data_dir)
            save_config(cfg, config_path)

            mock_manager = MagicMock()
            mock_manager.get_status.return_value = {"status": "Stopped", "is_running": False, "pid": None}
            mock_manager.start.return_value = 1234
            mock_manager.stop.return_value = True

            manager = ProcessDaemonManager(config_path, config_loader=lambda _: cfg, manager=mock_manager)

            status = manager.get_status()
            self.assertEqual(status["status"], "Stopped")
            self.assertEqual(status["backend"], "process")

            pid = manager.start()
            self.assertEqual(pid, 1234)
            mock_manager.start.assert_called_once()

            self.assertTrue(manager.stop())
            mock_manager.stop.assert_called_once()

            manager.pause(reason="test")
            self.assertTrue(manager.is_paused())
            self.assertTrue(manager.resume())
            self.assertFalse(manager.is_paused())

    def test_systemd_daemon_manager(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir)
            config_path = data_dir / "config.json"
            cfg = sample_config(data_dir)
            save_config(cfg, config_path)

            executed_cmds: list[list[str]] = []

            def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
                executed_cmds.append(cmd)
                if "is-active" in cmd:
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="active\n")
                if "show" in cmd:
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="5678\n")
                if "start" in cmd or "stop" in cmd:
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="")
                return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="unknown")

            manager = SystemdDaemonManager(
                config_path,
                service_name="daily-streak-daemon.service",
                scope="user",
                config_loader=lambda _: cfg,
                run_command=fake_run,
            )

            status = manager.get_status()
            self.assertEqual(status["status"], "Running")
            self.assertTrue(status["is_running"])
            self.assertEqual(status["pid"], 5678)
            self.assertIn("systemd", status["backend"])

            # Test start when already active returns pid
            pid = manager.start()
            self.assertEqual(pid, 5678)

            # Test stop
            self.assertTrue(manager.stop())
            self.assertIn(["systemctl", "--user", "stop", "daily-streak-daemon.service"], executed_cmds)

    def test_create_daemon_service_factory(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir)
            config_path = data_dir / "config.json"

            # Default / process backend
            cfg_proc = sample_config(data_dir)
            save_config(cfg_proc, config_path)
            service_proc = create_daemon_service(cfg_proc, config_path)
            self.assertIsInstance(service_proc, ProcessDaemonManager)

            # Systemd backend explicitly specified
            cfg_sys = sample_config(data_dir)
            cfg_sys["daemon"] = {"service_backend": "systemd", "systemd_service": "my-daemon.service"}
            save_config(cfg_sys, config_path)
            service_sys = create_daemon_service(cfg_sys, config_path)
            self.assertIsInstance(service_sys, SystemdDaemonManager)
            self.assertEqual(service_sys.service_name, "my-daemon.service")

    def test_unauthorized_updates_are_ignored(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir)
            config_path = data_dir / "config.json"
            cfg = sample_config(data_dir)
            save_config(cfg, config_path)

            controller = TelegramController(config_path, config_loader=lambda _: cfg)

            unauthorized_update = FakeUpdate(chat_id="999999999")
            self.assertFalse(controller.is_authorized(unauthorized_update))

            asyncio.run(controller.handle_help(unauthorized_update, None))
            unauthorized_update.message.reply_text.assert_not_called()

            asyncio.run(controller.handle_status(unauthorized_update, None))
            unauthorized_update.message.reply_text.assert_not_called()

            asyncio.run(controller.handle_run(unauthorized_update, None))
            unauthorized_update.message.reply_text.assert_not_called()

    def test_handle_help_authorized(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir)
            config_path = data_dir / "config.json"
            cfg = sample_config(data_dir)
            save_config(cfg, config_path)

            controller = TelegramController(config_path, config_loader=lambda _: cfg)
            update = FakeUpdate(chat_id="123456789")

            asyncio.run(controller.handle_help(update, None))
            update.message.reply_text.assert_called_once()
            args, _ = update.message.reply_text.call_args
            self.assertIn("Daily Streak Tool", args[0])
            self.assertIn("/status", args[0])
            self.assertIn("/run", args[0])

    def test_handle_status_and_last_and_next(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir)
            config_path = data_dir / "config.json"
            cfg = sample_config(data_dir)
            save_config(cfg, config_path)

            # Record a run in history
            from daily_streak.models import RecipientResult, RecipientStatus

            history_store = RunHistoryStore(data_dir)
            summary = RunSummary()
            for _ in range(5):
                summary.add_result(RecipientResult(recipient_name="User", status=RecipientStatus.SUCCESS))
            summary.add_result(RecipientResult(recipient_name="User", status=RecipientStatus.FAILED))
            summary.started_at = datetime(2026, 10, 15, 9, 30, 0, tzinfo=timezone.utc)
            summary.finished_at = datetime(2026, 10, 15, 9, 30, 42, tzinfo=timezone.utc)
            history_store.append(summary)


            mock_service = MagicMock(spec=DaemonService)
            mock_service.get_status.return_value = {
                "status": "Stopped",
                "is_running": False,
                "pid": None,
                "backend": "process",
                "next_run": "2026-10-16 09:30:00 +07",
            }
            mock_service.is_paused.return_value = False

            controller = TelegramController(
                config_path,
                config_loader=lambda _: cfg,
                daemon_service=mock_service,
            )

            # /status
            status_update = FakeUpdate(chat_id="123456789")
            asyncio.run(controller.handle_status(status_update, None))
            status_text = status_update.message.reply_text.call_args[0][0]
            self.assertIn("Stopped", status_text)
            self.assertIn("Tiến độ hôm nay", status_text)

            # /last
            last_update = FakeUpdate(chat_id="123456789")
            asyncio.run(controller.handle_last(last_update, None))
            last_text = last_update.message.reply_text.call_args[0][0]
            self.assertIn("Thành công:* `5`", last_text)
            self.assertIn("Thất bại:* `1`", last_text)
            self.assertIn("42.0s", last_text)

            # /next
            next_update = FakeUpdate(chat_id="123456789")
            asyncio.run(controller.handle_next(next_update, None))
            next_text = next_update.message.reply_text.call_args[0][0]
            self.assertIn("Lịch gửi tự động kế tiếp", next_text)
            self.assertIn("Đếm ngược", next_text)

            # /streaks
            streaks_update = FakeUpdate(chat_id="123456789")
            asyncio.run(controller.handle_streaks(streaks_update, None))
            streaks_text = streaks_update.message.reply_text.call_args[0][0]
            self.assertIn("Trạng thái Streak TikTok", streaks_text)
            self.assertIn("Test User", streaks_text)
            self.assertIn("Chưa xác định", streaks_text)



    def test_handle_start_stop_pause_resume(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir)
            config_path = data_dir / "config.json"
            cfg = sample_config(data_dir)
            save_config(cfg, config_path)

            mock_service = MagicMock(spec=DaemonService)
            mock_service.get_status.side_effect = [
                {"status": "Stopped", "is_running": False, "pid": None},
                {"status": "Running", "is_running": True, "pid": 1111},
                {"status": "Running", "is_running": True, "pid": 1111},
                {"status": "Stopped", "is_running": False, "pid": None},
            ]
            mock_service.start.return_value = 1111
            mock_service.stop.return_value = True
            mock_service.resume.return_value = True

            controller = TelegramController(
                config_path,
                config_loader=lambda _: cfg,
                daemon_service=mock_service,
            )

            # /start
            start_update = FakeUpdate(chat_id="123456789")
            asyncio.run(controller.handle_start(start_update, None))
            mock_service.start.assert_called_once()
            self.assertIn("1111", start_update.message.reply_text.call_args[0][0])

            # /stop
            stop_update = FakeUpdate(chat_id="123456789")
            asyncio.run(controller.handle_stop(stop_update, None))
            mock_service.stop.assert_called_once()
            self.assertIn("Đã dừng daemon", stop_update.message.reply_text.call_args[0][0])

            # /pause
            pause_update = FakeUpdate(chat_id="123456789")
            asyncio.run(controller.handle_pause(pause_update, None))
            mock_service.pause.assert_called_once_with(reason="telegram")
            self.assertIn("tạm dừng", pause_update.message.reply_text.call_args[0][0])

            # /resume
            resume_update = FakeUpdate(chat_id="123456789")
            asyncio.run(controller.handle_resume(resume_update, None))
            mock_service.resume.assert_called_once()
            self.assertIn("tiếp tục", resume_update.message.reply_text.call_args[0][0])

    def test_handle_run_confirmation_and_rejection_on_lock(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir)
            config_path = data_dir / "config.json"
            cfg = sample_config(data_dir)
            save_config(cfg, config_path)

            controller = TelegramController(config_path, config_loader=lambda _: cfg)

            # Simulate active send lock
            import os
            lock = RunLock(data_dir, pid=os.getpid())
            lock.acquire()

            update = FakeUpdate(chat_id="123456789")
            asyncio.run(controller.handle_run(update, None))
            msg = update.message.reply_text.call_args[0][0]
            self.assertIn("send.lock", msg)
            self.assertEqual(len(controller._confirmations), 0)

            lock.release()

            # When unlocked, /run creates confirmation token with reply_markup
            update2 = FakeUpdate(chat_id="123456789")
            asyncio.run(controller.handle_run(update2, None))
            self.assertEqual(len(controller._confirmations), 1)
            token = list(controller._confirmations.keys())[0]
            _, kwargs = update2.message.reply_text.call_args
            self.assertIn("reply_markup", kwargs)

            # Test cancel callback
            cancel_update = FakeCallbackQueryUpdate(chat_id="123456789", data=f"run_cancel:{token}")
            asyncio.run(controller.handle_callback(cancel_update, None))
            cancel_update.callback_query.edit_message_text.assert_called_once()
            self.assertIn("Đã hủy", cancel_update.callback_query.edit_message_text.call_args[0][0])

            # Token consumed
            self.assertEqual(len(controller._confirmations), 0)

            # Duplicate callback on consumed/non-existent token
            cancel_update2 = FakeCallbackQueryUpdate(chat_id="123456789", data=f"run_cancel:{token}")
            asyncio.run(controller.handle_callback(cancel_update2, None))
            self.assertIn("hết hạn", cancel_update2.callback_query.edit_message_text.call_args[0][0])

    def test_handle_run_confirm_execution_and_summary(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir)
            config_path = data_dir / "config.json"
            cfg = sample_config(data_dir)
            save_config(cfg, config_path)

            from daily_streak.models import RecipientResult, RecipientStatus
            sent_summary = RunSummary()
            for _ in range(3):
                sent_summary.add_result(RecipientResult(recipient_name="U", status=RecipientStatus.SUCCESS))

            fake_sender = MagicMock()
            fake_sender.send_daily_links = AsyncMock(return_value=sent_summary)

            controller = TelegramController(
                config_path,
                config_loader=lambda _: cfg,
                sender_factory=lambda _: fake_sender,
            )

            # Issue /run
            run_update = FakeUpdate(chat_id="123456789")
            asyncio.run(controller.handle_run(run_update, None))
            token = list(controller._confirmations.keys())[0]

            confirm_update = FakeCallbackQueryUpdate(chat_id="123456789", data=f"run_confirm:{token}")

            async def run_and_drain() -> None:
                await controller.handle_callback(confirm_update, None)
                tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
                if tasks:
                    await asyncio.gather(*tasks)

            with patch("telegram.Bot") as MockBot:
                mock_bot_instance = MagicMock()
                mock_bot_instance.send_message = AsyncMock()
                MockBot.return_value = mock_bot_instance

                asyncio.run(run_and_drain())

                fake_sender.send_daily_links.assert_called_once()
                mock_bot_instance.send_message.assert_called_once()
                sent_msg = mock_bot_instance.send_message.call_args[1]["text"]
                self.assertIn("Thành công: `3`", sent_msg)
                self.assertIn("Thất bại: `0`", sent_msg)

    def test_run_does_not_emit_duplicate_batch_completion_summaries(self) -> None:
        """Assert that /run emits exactly one batch completion summary and avoids duplicate notifications."""
        with TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir)
            config_path = data_dir / "config.json"
            cfg = sample_config(data_dir)
            save_config(cfg, config_path)

            from daily_streak.models import RecipientResult, RecipientStatus

            sent_summary = RunSummary()
            sent_summary.add_result(RecipientResult(recipient_name="U", status=RecipientStatus.SUCCESS))

            fake_sender = MagicMock()
            fake_sender.send_daily_links = AsyncMock(return_value=sent_summary)
            mock_notifier = MagicMock()
            mock_notifier.batch_completion = AsyncMock()
            fake_sender.notifier = mock_notifier

            controller = TelegramController(
                config_path,
                config_loader=lambda _: cfg,
                sender_factory=lambda _: fake_sender,
            )

            run_update = FakeUpdate(chat_id="123456789")
            asyncio.run(controller.handle_run(run_update, None))
            token = list(controller._confirmations.keys())[0]

            confirm_update = FakeCallbackQueryUpdate(chat_id="123456789", data=f"run_confirm:{token}")

            async def run_and_drain() -> None:
                await controller.handle_callback(confirm_update, None)
                tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
                if tasks:
                    await asyncio.gather(*tasks)

            with patch("telegram.Bot") as MockBot:
                mock_bot_instance = MagicMock()
                mock_bot_instance.send_message = AsyncMock()
                MockBot.return_value = mock_bot_instance

                asyncio.run(run_and_drain())

                # Exactly one summary message sent by controller
                self.assertEqual(mock_bot_instance.send_message.await_count, 1)
                # sender.notifier.batch_completion was NOT invoked separately, preventing duplication
                mock_notifier.batch_completion.assert_not_called()



    def test_streaks_command_includes_refresh_button_and_status(self) -> None:
        from daily_streak.streak_reader import StreakCache, StreakRecord, StreakStatus

        with TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir)
            config_path = data_dir / "config.json"
            cfg = sample_config(data_dir)
            cfg["recipients"] = [
                {"name": "Alice", "username": "alice"},
                {"name": "Bob", "username": "bob"},
                {"name": "Charlie", "username": "charlie"},
            ]
            save_config(cfg, config_path)

            cache = StreakCache(data_dir)
            cache.set(StreakRecord(username="alice", status=StreakStatus.AVAILABLE, count=99, updated_at="2026-10-19T10:00:00+00:00"))
            cache.set(StreakRecord(username="bob", status=StreakStatus.NOT_ACTIVE, updated_at="2026-10-19T09:00:00+00:00"))
            # charlie is not in cache (unknown)

            controller = TelegramController(config_path, config_loader=lambda _: cfg)
            update = FakeUpdate(chat_id="123456789")

            asyncio.run(controller.handle_streaks(update, None))

            update.message.reply_text.assert_called_once()
            text = update.message.reply_text.call_args[0][0]
            kwargs = update.message.reply_text.call_args[1]

            self.assertIn("🔥 99 ngày", text)
            self.assertIn("Chưa kích hoạt", text)
            self.assertIn("Chưa xác định", text)
            self.assertIn("Lần làm mới gần nhất", text)
            self.assertIn("10:00:00 19/10/2026", text)

            reply_markup = kwargs.get("reply_markup")
            self.assertIsNotNone(reply_markup)
            import telegram
            if isinstance(telegram.InlineKeyboardButton, MagicMock):
                telegram.InlineKeyboardButton.assert_any_call("↻ Làm mới streak", callback_data="streaks_refresh")
            else:
                self.assertEqual(reply_markup.inline_keyboard[0][0].text, "↻ Làm mới streak")
                self.assertEqual(reply_markup.inline_keyboard[0][0].callback_data, "streaks_refresh")

    def test_streaks_refresh_callback_success_updates_message(self) -> None:
        from daily_streak.streak_reader import StreakCache, StreakRecord, StreakStatus

        with TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir)
            config_path = data_dir / "config.json"
            cfg = sample_config(data_dir)
            save_config(cfg, config_path)

            cache = StreakCache(data_dir)
            cache.set(StreakRecord(username="testuser", status=StreakStatus.AVAILABLE, count=10))

            refresh_called = []

            def fake_refresher(config, recipients, **kwargs):
                refresh_called.append((config, recipients))
                c = StreakCache(data_dir)
                c.set(StreakRecord(username="testuser", status=StreakStatus.AVAILABLE, count=11, updated_at="2026-10-19T12:00:00+00:00"))
                return []

            controller = TelegramController(
                config_path,
                config_loader=lambda _: cfg,
                streak_refresher=fake_refresher,
            )

            update = FakeCallbackQueryUpdate(chat_id="123456789", data="streaks_refresh")

            async def run_callback() -> None:
                await controller.handle_callback(update, None)
                tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
                if tasks:
                    await asyncio.gather(*tasks)

            asyncio.run(run_callback())

            self.assertEqual(len(refresh_called), 1)
            update.callback_query.answer.assert_called_with("Đang làm mới dữ liệu streak từ TikTok...")
            update.callback_query.edit_message_text.assert_called_once()
            edited_text = update.callback_query.edit_message_text.call_args[0][0]
            self.assertIn("🔥 11 ngày", edited_text)
            self.assertIn("12:00:00 19/10/2026", edited_text)

    def test_streaks_refresh_callback_unauthorized(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir)
            config_path = data_dir / "config.json"
            cfg = sample_config(data_dir)
            save_config(cfg, config_path)

            refresher = MagicMock()
            controller = TelegramController(
                config_path,
                config_loader=lambda _: cfg,
                streak_refresher=refresher,
            )

            # Different chat_id
            update = FakeCallbackQueryUpdate(chat_id="999999999", data="streaks_refresh")
            asyncio.run(controller.handle_callback(update, None))

            update.callback_query.answer.assert_called_with("Truy cập không được phép.", show_alert=True)
            refresher.assert_not_called()
            update.callback_query.edit_message_text.assert_not_called()

    def test_streaks_refresh_callback_locked_in_process_and_streak_lock(self) -> None:
        from daily_streak.lock import StreakLock

        with TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir)
            config_path = data_dir / "config.json"
            cfg = sample_config(data_dir)
            save_config(cfg, config_path)

            refresher = MagicMock()
            controller = TelegramController(
                config_path,
                config_loader=lambda _: cfg,
                streak_refresher=refresher,
            )

            # Case 1: StreakLock is active on the host
            lock = StreakLock(data_dir)
            lock.acquire()
            try:
                update1 = FakeCallbackQueryUpdate(chat_id="123456789", data="streaks_refresh")
                asyncio.run(controller.handle_callback(update1, None))
                update1.callback_query.answer.assert_called_with(
                    "Tiến trình làm mới streak khác đang chạy (`streak.lock`)!",
                    show_alert=True,
                )
                refresher.assert_not_called()
            finally:
                lock.release()

            # Case 2: In-process lock is locked
            async def test_locked():
                async with controller._streaks_refresh_lock:
                    update2 = FakeCallbackQueryUpdate(chat_id="123456789", data="streaks_refresh")
                    await controller.handle_callback(update2, None)
                    update2.callback_query.answer.assert_called_with(
                        "Đang trong quá trình làm mới streak, vui lòng đợi...",
                        show_alert=True,
                    )
                    refresher.assert_not_called()

            asyncio.run(test_locked())

    def test_streaks_refresh_callback_sanitized_errors_preserve_cache(self) -> None:
        from daily_streak.streak_reader import StreakCache, StreakRecord, StreakStatus

        with TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir)
            config_path = data_dir / "config.json"
            cfg = sample_config(data_dir)
            save_config(cfg, config_path)

            cache = StreakCache(data_dir)
            cache.set(StreakRecord(username="testuser", status=StreakStatus.AVAILABLE, count=55, updated_at="2026-10-19T08:00:00+00:00"))

            # Error scenario: sensitive cookie in exception message
            def failing_refresher(*args, **kwargs):
                raise RuntimeError("Failed to auth with cookie session_token=SECRET_COOKIE_12345")

            controller = TelegramController(
                config_path,
                config_loader=lambda _: cfg,
                streak_refresher=failing_refresher,
            )

            update = FakeCallbackQueryUpdate(chat_id="123456789", data="streaks_refresh")

            async def run_callback() -> None:
                await controller.handle_callback(update, None)
                tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
                if tasks:
                    await asyncio.gather(*tasks)

            asyncio.run(run_callback())

            update.callback_query.edit_message_text.assert_called_once()
            edited_text = update.callback_query.edit_message_text.call_args[0][0]

            # Secret is NOT leaked
            self.assertNotIn("SECRET_COOKIE_12345", edited_text)
            self.assertNotIn("session_token", edited_text)

            # Sanitized error is presented
            self.assertIn("Phiên đăng nhập TikTok (cookie) không hợp lệ hoặc đã hết hạn.", edited_text)

            # Previous cache is retained
            self.assertIn("🔥 55 ngày", edited_text)
            self.assertIn("bản cache gần nhất", edited_text)

    def test_run_cli_telegram_controller_validation(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir)
            config_path = data_dir / "config.json"

            # 1. telegram.enabled is false
            cfg_disabled = sample_config(data_dir)
            cfg_disabled["telegram"]["enabled"] = False
            save_config(cfg_disabled, config_path)
            stderr = MagicMock()
            code = run_cli(["--config", str(config_path), "telegram-controller"], stderr=stderr)
            self.assertEqual(code, EXIT_CONFIGURATION)

            # 2. telegram.commands_enabled is false
            cfg_no_cmds = sample_config(data_dir)
            cfg_no_cmds["telegram"]["commands_enabled"] = False
            save_config(cfg_no_cmds, config_path)
            code = run_cli(["--config", str(config_path), "telegram-controller"], stderr=stderr)
            self.assertEqual(code, EXIT_CONFIGURATION)

            # 3. Missing bot_token
            cfg_no_token = sample_config(data_dir)
            cfg_no_token["telegram"]["bot_token"] = ""
            save_config(cfg_no_token, config_path)
            code = run_cli(["--config", str(config_path), "telegram-controller"], stderr=stderr)
            self.assertEqual(code, EXIT_CONFIGURATION)


if __name__ == "__main__":
    unittest.main()
