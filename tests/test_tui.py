from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from daily_streak.cli import EXIT_OK, run_cli
from daily_streak.config import save_config
from daily_streak.history import RunHistoryStore
from daily_streak.models import RecipientResult, RecipientStatus, RunSummary
from daily_streak.recipients import TikTokRendererUnresponsiveError
from daily_streak.tui import TuiController, TuiInputError, _operation_error_status


def tui_config(data_dir: Path) -> dict[str, object]:
    return {
        "data_dir": str(data_dir),
        "schedule": {"time": "00:00", "timezone": "UTC"},
        "tiktok": {"headless": True},
        "future_setting": {"preserved": True},
        "recipients": [{"name": "Alice", "username": "alice"}],
        "videos": ["https://www.tiktok.com/example"],
    }


class FakeTuiSender:
    def __init__(self, callback) -> None:  # type: ignore[no-untyped-def]
        self.callback = callback
        self.force_values: list[bool] = []

    async def send_daily_links(self, *, force: bool = False) -> RunSummary:
        self.force_values.append(force)
        summary = RunSummary(started_at=datetime(2026, 10, 11, tzinfo=timezone.utc))
        result = RecipientResult(
            recipient_name="Alice",
            username="alice",
            selected_video="https://www.tiktok.com/example",
            status=RecipientStatus.SUCCESS,
            attempts=1,
        )
        summary.add_result(result)
        self.callback(result, summary)
        return summary.finish(datetime(2026, 10, 11, 0, 0, 1, tzinfo=timezone.utc))


class TuiControllerTests(unittest.TestCase):
    def test_renderer_readiness_error_does_not_request_a_tui_traceback(self) -> None:
        error = TikTokRendererUnresponsiveError(
            "TikTok renderer did not respond to readiness probes for 45.0 seconds."
        )

        self.assertEqual(
            _operation_error_status(error),
            (str(error), False),
        )

    def test_one_configured_recipient_renders_as_one_selected_row_at_startup(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = tui_config(root / "data")
            config["recipients"] = [{"name": "Đức Khoa", "username": "khoa_memezlol"}]
            controller = TuiController(
                root / "config.json",
                config_loader=lambda _: config,
                config_saver=lambda *_: None,
            )

        self.assertEqual(
            controller.recipient_rows(),
            [
                {
                    "key": "username:khoa_memezlol",
                    "selected": True,
                    "name": "Đức Khoa",
                    "username": "khoa_memezlol",
                    "streak": "Chưa xác định",
                }
            ],
        )


    def test_configured_recipients_initialize_as_selected_tui_targets(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = tui_config(root / "data")
            config["recipients"] = [
                {"name": "Alice", "username": "alice"},
                {"name": "Bob", "username": "bob"},
            ]
            controller = TuiController(
                root / "config.json",
                config_loader=lambda _: config,
                config_saver=lambda *_: None,
            )

        self.assertEqual(controller.selected_recipient_count, 2)
        self.assertTrue(controller.has_selected_recipients)
        self.assertTrue(all(row["selected"] for row in controller.recipient_rows()))
        self.assertEqual(
            [row["name"] for row in controller.recipient_rows()],
            ["Alice", "Bob"],
        )

    def test_failed_scan_preserves_configured_recipient_rows_and_selection(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = tui_config(root / "data")

            async def scan_action(received_config):  # type: ignore[no-untyped-def]
                raise RuntimeError("TikTok DM list did not become ready")

            controller = TuiController(
                root / "config.json",
                config_loader=lambda _: config,
                config_saver=lambda *_: None,
                scan_action=scan_action,
            )
            initial_rows = controller.recipient_rows()
            with self.assertRaisesRegex(RuntimeError, "did not become ready"):
                asyncio.run(controller.scan())

        self.assertEqual(controller.recipient_rows(), initial_rows)
        self.assertTrue(controller.has_selected_recipients)

    def test_successful_scan_merges_without_losing_configured_username_or_selection(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = tui_config(root / "data")
            config["recipients"] = [{"name": "Đức Khoa", "username": "khoa_memezlol"}]

            async def scan_action(received_config):  # type: ignore[no-untyped-def]
                self.assertEqual(received_config["recipients"], config["recipients"])
                return [
                    {"name": "Đức Khoa", "username": ""},
                    {"name": "New discovery", "username": "new_discovery"},
                ]

            controller = TuiController(
                root / "config.json",
                config_loader=lambda _: config,
                config_saver=lambda *_: None,
                scan_action=scan_action,
            )
            rows = asyncio.run(controller.scan())

        configured = next(row for row in rows if row["name"] == "Đức Khoa")
        discovered = next(row for row in rows if row["name"] == "New discovery")
        self.assertEqual(configured["username"], "khoa_memezlol")
        self.assertTrue(configured["selected"])
        self.assertFalse(discovered["selected"])

    def test_resolve_with_no_selection_does_not_call_browser_action_or_log_traceback(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = tui_config(root / "data")
            calls: list[object] = []

            async def resolve_action(*args):  # type: ignore[no-untyped-def]
                calls.append(args)
                return []

            controller = TuiController(
                root / "config.json",
                config_loader=lambda _: config,
                config_saver=lambda *_: None,
                resolve_action=resolve_action,
            )
            controller.deselect_all()
            with self.assertNoLogs("daily_streak.tui", level="ERROR"):
                with self.assertRaisesRegex(
                    TuiInputError,
                    "Select at least one recipient before resolving usernames.",
                ):
                    asyncio.run(controller.resolve())

        self.assertEqual(calls, [])
        self.assertEqual(
            _operation_error_status(
                TuiInputError("Select at least one recipient before resolving usernames.")
            ),
            ("Select at least one recipient before resolving usernames.", False),
        )

    def test_selection_preserves_unrelated_config_and_scanned_catalog(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            saved: list[dict[str, object]] = []
            config = tui_config(root / "data")
            controller = TuiController(
                root / "config.json",
                config_loader=lambda _: config,
                config_saver=lambda value, _: saved.append(deepcopy(dict(value))),
            )

            controller.deselect_all()
            controller._merge_recipients([{"name": "Bob", "username": ""}])
            controller.select_all()

        self.assertEqual(saved[0]["recipients"], [])
        self.assertEqual(saved[-1]["future_setting"], {"preserved": True})
        self.assertEqual(
            [recipient["name"] for recipient in saved[-1]["recipients"]],  # type: ignore[index]
            ["Alice", "Bob"],
        )

    def test_scan_and_resolve_use_injected_shared_actions_without_browser(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = tui_config(root / "data")
            scanned = []
            resolved = []

            async def scan_action(received_config):  # type: ignore[no-untyped-def]
                scanned.append(received_config)
                return [{"name": "Bob", "username": ""}]

            async def resolve_action(received_config, recipients):  # type: ignore[no-untyped-def]
                resolved.append((received_config, recipients))
                return [{"name": "Bob", "username": "bob"}]

            controller = TuiController(
                root / "config.json",
                config_loader=lambda _: config,
                config_saver=lambda *_: None,
                scan_action=scan_action,
                resolve_action=resolve_action,
            )
            asyncio.run(controller.scan())
            bob = next(row for row in controller.recipient_rows() if row["name"] == "Bob")
            controller.toggle_recipient(str(bob["key"]))
            rows = asyncio.run(controller.resolve())

        self.assertEqual(len(scanned), 1)
        self.assertEqual(len(resolved), 1)
        self.assertEqual([row["username"] for row in rows if row["name"] == "Bob"], ["bob"])

    def test_send_forwards_typed_recipient_progress_and_force_choice(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = tui_config(root / "data")
            progress: list[RecipientResult] = []
            fake_senders: list[FakeTuiSender] = []

            def factory(_, callback):  # type: ignore[no-untyped-def]
                sender = FakeTuiSender(callback)
                fake_senders.append(sender)
                return sender

            controller = TuiController(
                root / "config.json",
                config_loader=lambda _: config,
                config_saver=lambda *_: None,
                sender_factory=factory,
            )
            controller.set_progress_callback(lambda result, _: progress.append(result))
            summary = asyncio.run(controller.send(force=True))

        self.assertEqual(fake_senders[0].force_values, [True])
        self.assertEqual(summary.success_count, 1)
        self.assertEqual([result.status for result in progress], [RecipientStatus.SUCCESS])
        self.assertEqual(controller.current_results[0].recipient_name, "Alice")

    def test_send_uses_current_tui_selection(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = tui_config(root / "data")
            config["recipients"] = [
                {"name": "Alice", "username": "alice"},
                {"name": "Bob", "username": "bob"},
            ]
            received_configs: list[dict[str, object]] = []

            def factory(received_config, callback):  # type: ignore[no-untyped-def]
                received_configs.append(deepcopy(dict(received_config)))
                return FakeTuiSender(callback)

            controller = TuiController(
                root / "config.json",
                config_loader=lambda _: config,
                config_saver=lambda *_: None,
                sender_factory=factory,
            )
            bob = next(row for row in controller.recipient_rows() if row["name"] == "Bob")
            controller.toggle_recipient(str(bob["key"]))
            asyncio.run(controller.send())

        recipients = received_configs[0]["recipients"]
        self.assertEqual(recipients, [{"name": "Alice", "username": "alice"}])

    def test_history_rows_includes_unconfirmed_and_handles_legacy_records(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = tui_config(root / "data")
            history_store = RunHistoryStore(root / "data")
            # Write a modern record with unconfirmed
            history_store.append(
                RunSummary(
                    results=[
                        RecipientResult("Alice", RecipientStatus.SUCCESS, attempts=1),
                        RecipientResult("Bob", RecipientStatus.UNCONFIRMED, attempts=1),
                    ]
                ).finish()
            )
            # Write a legacy-style raw record without "unconfirmed" key
            raw_legacy = {
                "started_at": "2026-10-10T10:00:00+00:00",
                "finished_at": "2026-10-10T10:01:00+00:00",
                "elapsed_seconds": 60.0,
                "success": 1,
                "failed": 0,
                "skipped": 0,
                "setup_error": None,
                "results": [],
            }
            with (root / "data" / "history.jsonl").open("a", encoding="utf-8") as f:
                import json
                f.write(json.dumps(raw_legacy) + "\n")

            controller = TuiController(
                root / "config.json",
                config_loader=lambda _: config,
                config_saver=lambda *_: None,
            )
            rows = controller.history_rows()

        self.assertEqual(len(rows), 2)
        # Latest record (modern): 1 success, 0 failed, 1 unconfirmed, 0 skipped
        self.assertEqual(rows[0]["success"], 1)
        self.assertEqual(rows[0]["unconfirmed"], 1)
        self.assertEqual(rows[0]["failed"], 0)
        self.assertEqual(rows[0]["skipped"], 0)

        # Legacy record: 1 success, 0 failed, 0 unconfirmed, 0 skipped
        self.assertEqual(rows[1]["success"], 1)
        self.assertEqual(rows[1]["unconfirmed"], 0)
        self.assertEqual(rows[1]["failed"], 0)
        self.assertEqual(rows[1]["skipped"], 0)

    def test_daemon_status_and_logs(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            log_path = data_dir / "daemon.log"
            log_path.write_text("line 1\nline 2\n", encoding="utf-8")
            config = tui_config(data_dir)
            controller = TuiController(
                root / "config.json",
                config_loader=lambda _: config,
                config_saver=lambda *_: None,
            )
            status = controller.daemon_status()
            logs = controller.daemon_logs()

        self.assertEqual(status["status"], "Stopped")
        self.assertFalse(status["is_running"])
        self.assertIsNone(status["pid"])
        self.assertEqual(logs, ["line 1", "line 2"])

    def test_daemon_controller_start_and_stop_delegation(self) -> None:
        from unittest.mock import MagicMock

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = tui_config(root / "data")
            controller = TuiController(
                root / "config.json",
                config_loader=lambda _: config,
                config_saver=lambda *_: None,
            )
            mock_manager = MagicMock()
            mock_manager.start.return_value = 4242
            mock_manager.stop.return_value = True
            controller.daemon_manager = mock_manager

            pid = controller.start_daemon()
            stopped = controller.stop_daemon(timeout=5.0)

        self.assertEqual(pid, 4242)
        mock_manager.start.assert_called_once()
        self.assertTrue(stopped)
        mock_manager.stop.assert_called_once_with(timeout=5.0)


try:
    import textual
    HAS_TEXTUAL = True
except ImportError:
    HAS_TEXTUAL = False


@unittest.skipUnless(HAS_TEXTUAL, "textual is not installed")
class TuiAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_daemon_tab_composition_and_rendering(self) -> None:
        from textual.widgets import RichLog, Static, TabbedContent
        from daily_streak.tui import create_tui_app

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = tui_config(root / "data")
            controller = TuiController(
                root / "config.json",
                config_loader=lambda _: config,
                config_saver=lambda *_: None,
            )
            app = create_tui_app(root / "config.json", controller=controller)
            async with app.run_test(size=(100, 40)) as pilot:
                tabbed = pilot.app.query_one(TabbedContent)
                tabbed.active = "daemon-tab"
                await pilot.pause()

                info = pilot.app.query_one("#daemon-info", Static).render()
                self.assertIn("Daemon Status:       Stopped", str(info))
                self.assertIn("Configured Schedule: 00:00 (UTC)", str(info))
                self.assertIn("Run On Start:        Disabled", str(info))

                logs = pilot.app.query_one("#daemon-logs", RichLog)
                self.assertIsNotNone(logs)

    async def test_daemon_button_actions_and_status_updates(self) -> None:
        from unittest.mock import MagicMock
        from textual.widgets import Button, TabbedContent
        from daily_streak.tui import create_tui_app

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = tui_config(root / "data")
            controller = TuiController(
                root / "config.json",
                config_loader=lambda _: config,
                config_saver=lambda *_: None,
            )
            controller.start_daemon = MagicMock(return_value=12345)
            controller.stop_daemon = MagicMock(return_value=True)
            app = create_tui_app(root / "config.json", controller=controller)

            async with app.run_test(size=(100, 40)) as pilot:
                tabbed = pilot.app.query_one(TabbedContent)
                tabbed.active = "daemon-tab"
                await pilot.pause()

                btn_start = pilot.app.query_one("#btn-start-daemon", Button)
                await pilot.click(btn_start)
                while pilot.app._operation_task is not None and not pilot.app._operation_task.done():
                    await pilot.pause()
                controller.start_daemon.assert_called_once()
                self.assertIn("Daemon started successfully (PID 12345)", pilot.app._run_status)

                btn_stop = pilot.app.query_one("#btn-stop-daemon", Button)
                await pilot.click(btn_stop)
                while pilot.app._operation_task is not None and not pilot.app._operation_task.done():
                    await pilot.pause()
                controller.stop_daemon.assert_called_once()
                self.assertIn("Daemon stopped successfully", pilot.app._run_status)

                btn_refresh = pilot.app.query_one("#btn-refresh-daemon", Button)
                await pilot.click(btn_refresh)
                await pilot.pause()
                self.assertIn("Refreshed daemon status", pilot.app._run_status)

                btn_logs = pilot.app.query_one("#btn-refresh-daemon-logs", Button)
                await pilot.click(btn_logs)
                await pilot.pause()
                self.assertIn("Refreshed daemon logs", pilot.app._run_status)

    async def test_daemon_keybindings_and_error_handling(self) -> None:
        from unittest.mock import MagicMock
        from daily_streak.daemon import DaemonProcessError
        from daily_streak.tui import create_tui_app

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = tui_config(root / "data")
            controller = TuiController(
                root / "config.json",
                config_loader=lambda _: config,
                config_saver=lambda *_: None,
            )
            controller.start_daemon = MagicMock(return_value=54321)
            controller.stop_daemon = MagicMock(return_value=True)
            app = create_tui_app(root / "config.json", controller=controller)

            async with app.run_test(size=(100, 40)) as pilot:
                # Keybinding 'u' starts daemon
                await pilot.press("u")
                while pilot.app._operation_task is not None and not pilot.app._operation_task.done():
                    await pilot.pause()
                controller.start_daemon.assert_called_once()
                self.assertIn("Daemon started successfully (PID 54321)", pilot.app._run_status)

                # Keybinding 'k' stops daemon
                await pilot.press("k")
                while pilot.app._operation_task is not None and not pilot.app._operation_task.done():
                    await pilot.pause()
                controller.stop_daemon.assert_called_once()
                self.assertIn("Daemon stopped successfully", pilot.app._run_status)

                # Error handling during start does not crash the TUI
                controller.start_daemon.side_effect = DaemonProcessError("Daemon is already running with PID 54321.")
                await pilot.press("u")
                while pilot.app._operation_task is not None and not pilot.app._operation_task.done():
                    await pilot.pause()
                self.assertIn("Could not start daemon: Daemon is already running with PID 54321.", pilot.app._run_status)

    async def test_action_send_rejects_when_no_recipients(self) -> None:
        from daily_streak.tui import create_tui_app

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = tui_config(root / "data")
            mock_notifier = MagicMock()
            mock_notifier.batch_completion = AsyncMock()

            controller = TuiController(
                root / "config.json",
                config_loader=lambda _: config,
                config_saver=lambda *_: None,
                notifier_factory=lambda _: mock_notifier,
            )
            controller.deselect_all()
            app = create_tui_app(root / "config.json", controller=controller)

            async with app.run_test(size=(100, 40)) as pilot:
                # Keybinding 's' calls action_send
                await pilot.press("s")
                await pilot.pause()
                self.assertIn("Select at least one recipient before sending", pilot.app._run_status)
                mock_notifier.batch_completion.assert_not_called()

                # Keybinding 'f' calls action_force_send
                await pilot.press("f")
                await pilot.pause()
                self.assertIn("Select at least one recipient before force-sending", pilot.app._run_status)
                mock_notifier.batch_completion.assert_not_called()


class TuiCliTests(unittest.TestCase):
    def test_tui_command_dispatches_lazily_after_config_validation(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = root / "config.json"
            save_config(tui_config(root / "data"), config_path)
            with patch("daily_streak.tui.run_tui", return_value=EXIT_OK) as run_tui:
                exit_code = run_cli(
                    ["--config", str(config_path), "tui"],
                    stdout=StringIO(),
                    stderr=StringIO(),
                )

        self.assertEqual(exit_code, EXIT_OK)
        run_tui.assert_called_once_with(config_path)

    def test_importing_tui_does_not_import_textual(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import daily_streak.tui; print('textual' in sys.modules)",
            ],
            capture_output=True,
            check=True,
            text=True,
        )
        self.assertEqual(result.stdout.strip(), "False")


class TuiTelegramNotificationTests(unittest.TestCase):
    def test_manual_send_dispatches_exact_telegram_completion_summary(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = tui_config(root / "data")
            config["telegram"] = {
                "enabled": True,
                "bot_token": "secret_bot_token",
                "chat_id": "target_chat_123",
            }
            fake_sender = FakeTuiSender(lambda *_: None)

            mock_telegram = MagicMock()
            mock_bot = MagicMock()
            mock_bot.send_message = AsyncMock()
            mock_telegram.Bot.return_value = mock_bot

            controller = TuiController(
                root / "config.json",
                config_loader=lambda _: config,
                config_saver=lambda *_: None,
                sender_factory=lambda *_: fake_sender,
            )

            with patch.dict("sys.modules", {"telegram": mock_telegram}):
                summary = asyncio.run(controller.send(force=False))

            self.assertEqual(fake_sender.force_values, [False])
            self.assertEqual(summary.success_count, 1)
            mock_telegram.Bot.assert_called_once_with(token="secret_bot_token")
            mock_bot.send_message.assert_awaited_once_with(
                chat_id="target_chat_123",
                text=(
                    "Daily Streak Tool \u2014 batch completed\n"
                    "Success: 1\n"
                    "Failed: 0\n"
                    "Unconfirmed: 0\n"
                    "Skipped: 0"
                ),
            )

    def test_force_send_dispatches_exact_telegram_completion_summary(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = tui_config(root / "data")
            config["telegram"] = {
                "enabled": True,
                "bot_token": "secret_bot_token",
                "chat_id": "target_chat_123",
            }

            class CustomSummarySender:
                def __init__(self) -> None:
                    self.force_values: list[bool] = []

                async def send_daily_links(self, *, force: bool = False) -> RunSummary:
                    self.force_values.append(force)
                    return RunSummary(
                        results=[
                            RecipientResult("Alice", RecipientStatus.SUCCESS),
                            RecipientResult("Bob", RecipientStatus.FAILED),
                            RecipientResult("Charlie", RecipientStatus.UNCONFIRMED),
                            RecipientResult("David", RecipientStatus.SKIPPED),
                        ]
                    ).finish()

            sender = CustomSummarySender()
            mock_telegram = MagicMock()
            mock_bot = MagicMock()
            mock_bot.send_message = AsyncMock()
            mock_telegram.Bot.return_value = mock_bot

            controller = TuiController(
                root / "config.json",
                config_loader=lambda _: config,
                config_saver=lambda *_: None,
                sender_factory=lambda *_: sender,
            )

            with patch.dict("sys.modules", {"telegram": mock_telegram}):
                summary = asyncio.run(controller.send(force=True))

            self.assertEqual(sender.force_values, [True])
            self.assertEqual(summary.success_count, 1)
            self.assertEqual(summary.failed_count, 1)
            self.assertEqual(summary.unconfirmed_count, 1)
            self.assertEqual(summary.skipped_count, 1)
            mock_bot.send_message.assert_awaited_once_with(
                chat_id="target_chat_123",
                text=(
                    "Daily Streak Tool \u2014 batch completed\n"
                    "Success: 1\n"
                    "Failed: 1\n"
                    "Unconfirmed: 1\n"
                    "Skipped: 1"
                ),
            )

    def test_send_with_telegram_disabled_sends_no_summary(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = tui_config(root / "data")
            config["telegram"] = {
                "enabled": False,
                "bot_token": "secret_bot_token",
                "chat_id": "target_chat_123",
            }
            fake_sender = FakeTuiSender(lambda *_: None)

            mock_telegram = MagicMock()
            controller = TuiController(
                root / "config.json",
                config_loader=lambda _: config,
                config_saver=lambda *_: None,
                sender_factory=lambda *_: fake_sender,
            )

            with patch.dict("sys.modules", {"telegram": mock_telegram}):
                summary = asyncio.run(controller.send(force=False))

            self.assertEqual(summary.success_count, 1)
            mock_telegram.Bot.assert_not_called()

    def test_send_telegram_failure_does_not_alter_send_result(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = tui_config(root / "data")
            config["telegram"] = {
                "enabled": True,
                "bot_token": "token",
                "chat_id": "chat",
            }
            fake_sender = FakeTuiSender(lambda *_: None)

            mock_telegram = MagicMock()
            mock_bot = MagicMock()
            mock_bot.send_message = AsyncMock(side_effect=RuntimeError("Telegram network timeout"))
            mock_telegram.Bot.return_value = mock_bot

            controller = TuiController(
                root / "config.json",
                config_loader=lambda _: config,
                config_saver=lambda *_: None,
                sender_factory=lambda *_: fake_sender,
            )

            with patch.dict("sys.modules", {"telegram": mock_telegram}):
                summary = asyncio.run(controller.send(force=False))

            self.assertEqual(summary.success_count, 1)
            self.assertIsNotNone(controller.last_summary)
            self.assertEqual(controller.last_summary.success_count, 1)

    def test_rejected_send_no_selected_recipients_does_not_send_summary(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = tui_config(root / "data")
            config["telegram"] = {
                "enabled": True,
                "bot_token": "token",
                "chat_id": "chat",
            }
            mock_notifier = MagicMock()
            mock_notifier.batch_completion = AsyncMock()

            controller = TuiController(
                root / "config.json",
                config_loader=lambda _: config,
                config_saver=lambda *_: None,
                notifier_factory=lambda _: mock_notifier,
            )
            controller.deselect_all()
            self.assertFalse(controller.has_selected_recipients)

            with self.assertRaisesRegex(TuiInputError, "Select at least one recipient before sending."):
                asyncio.run(controller.send())

            mock_notifier.batch_completion.assert_not_called()

    def test_rejected_send_on_lock_collision_does_not_send_summary(self) -> None:
        from daily_streak.lock import RunLockedError

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = tui_config(root / "data")
            config["telegram"] = {
                "enabled": True,
                "bot_token": "token",
                "chat_id": "chat",
            }

            class LockedSender:
                async def send_daily_links(self, *, force: bool = False) -> RunSummary:
                    raise RunLockedError("Another sender instance holds the run lock.")

            mock_notifier = MagicMock()
            mock_notifier.batch_completion = AsyncMock()

            controller = TuiController(
                root / "config.json",
                config_loader=lambda _: config,
                config_saver=lambda *_: None,
                sender_factory=lambda *_: LockedSender(),
                notifier_factory=lambda _: mock_notifier,
            )

            with self.assertRaises(RunLockedError):
                asyncio.run(controller.send())

            mock_notifier.batch_completion.assert_not_called()

    def test_duplicate_notification_prevention_daemon_and_tui(self) -> None:
        from daily_streak.daemon import DaemonRunner

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = tui_config(root / "data")
            config["telegram"] = {
                "enabled": True,
                "bot_token": "token",
                "chat_id": "chat",
            }
            summary = RunSummary(results=[RecipientResult("Alice", RecipientStatus.SUCCESS)]).finish()

            sender_notifier = MagicMock()
            sender_notifier.batch_completion = AsyncMock()

            class MockSenderWithNotifier:
                def __init__(self) -> None:
                    self.notifier = sender_notifier

                async def send_daily_links(self, *, force: bool = False) -> RunSummary:
                    return summary

            daemon_runner = DaemonRunner(
                root / "config.json",
                config_loader=lambda _: config,
                sender_factory=lambda _: MockSenderWithNotifier(),
            )
            asyncio.run(daemon_runner.run_once())
            self.assertEqual(sender_notifier.batch_completion.await_count, 1)

            sender_notifier.batch_completion.reset_mock()

            tui_controller = TuiController(
                root / "config.json",
                config_loader=lambda _: config,
                config_saver=lambda *_: None,
                sender_factory=lambda *_: MockSenderWithNotifier(),
            )
            asyncio.run(tui_controller.send(force=False))
            self.assertEqual(sender_notifier.batch_completion.await_count, 1)

            sender_notifier.batch_completion.reset_mock()

            asyncio.run(tui_controller.send(force=True))
            self.assertEqual(sender_notifier.batch_completion.await_count, 1)
