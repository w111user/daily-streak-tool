from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import signal
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import ANY, AsyncMock, MagicMock, patch

from daily_streak.daemon import DaemonRunner
from daily_streak.models import RunSummary


def config_with_marker(marker: int) -> dict[str, object]:
    return {
        "marker": marker,
        "schedule": {"time": "00:00", "timezone": "UTC", "run_on_start": False},
    }


class RecordingSender:
    def __init__(self, marker: int, started: asyncio.Event | None = None, release: asyncio.Event | None = None) -> None:
        self.marker = marker
        self.started = started
        self.release = release

    async def send_daily_links(self) -> RunSummary:
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        return RunSummary(started_at=datetime(2026, 10, 11, tzinfo=timezone.utc)).finish(
            datetime(2026, 10, 11, 0, 0, 1, tzinfo=timezone.utc)
        )


class MockProcess:
    def __init__(self, pid: int = 12345, returncode: int | None = None) -> None:
        self.pid = pid
        self._returncode = returncode

    def poll(self) -> int | None:
        return self._returncode


class DaemonTests(unittest.TestCase):
    def test_run_once_reloads_config_for_each_scheduled_execution(self) -> None:
        loaded = [config_with_marker(1), config_with_marker(2)]
        seen_markers: list[int] = []

        def loader(_: str | Path) -> dict[str, object]:
            return loaded.pop(0)

        def factory(config: dict[str, object]) -> RecordingSender:
            marker = int(config["marker"])
            seen_markers.append(marker)
            return RecordingSender(marker)

        runner = DaemonRunner("unused.json", config_loader=loader, sender_factory=factory)
        asyncio.run(runner.run_once())
        asyncio.run(runner.run_once())

        self.assertEqual(seen_markers, [1, 2])

    def test_run_once_skips_when_an_in_process_run_is_active(self) -> None:
        async def scenario() -> tuple[object, int]:
            started = asyncio.Event()
            release = asyncio.Event()
            factory_calls = 0

            def loader(_: str | Path) -> dict[str, object]:
                return config_with_marker(1)

            def factory(_: dict[str, object]) -> RecordingSender:
                nonlocal factory_calls
                factory_calls += 1
                return RecordingSender(1, started, release)

            runner = DaemonRunner("unused.json", config_loader=loader, sender_factory=factory)
            first = asyncio.create_task(runner.run_once())
            await started.wait()
            second = await runner.run_once()
            release.set()
            await first
            return second, factory_calls

        skipped, factory_calls = asyncio.run(scenario())
        self.assertIsNone(skipped)
        self.assertEqual(factory_calls, 1)

    def test_run_once_sends_exactly_one_telegram_summary_when_enabled(self) -> None:
        from daily_streak.models import RecipientResult, RecipientStatus

        summary = RunSummary(
            results=[
                RecipientResult("Alice", RecipientStatus.SUCCESS),
                RecipientResult("Bob", RecipientStatus.SUCCESS),
                RecipientResult("Charlie", RecipientStatus.SUCCESS),
                RecipientResult("David", RecipientStatus.FAILED),
                RecipientResult("Eve", RecipientStatus.UNCONFIRMED),
                RecipientResult("Frank", RecipientStatus.SKIPPED),
                RecipientResult("Grace", RecipientStatus.SKIPPED),
            ]
        ).finish()

        config = {
            "telegram": {
                "enabled": True,
                "bot_token": "token_abc_123",
                "chat_id": "chat_xyz_789",
            }
        }

        class FakeSenderWithSummary:
            async def send_daily_links(self) -> RunSummary:
                return summary

        mock_telegram = MagicMock()
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()
        mock_telegram.Bot.return_value = mock_bot

        runner = DaemonRunner(
            "unused.json",
            config_loader=lambda _: config,
            sender_factory=lambda _: FakeSenderWithSummary(),
        )

        with patch.dict("sys.modules", {"telegram": mock_telegram}):
            result_summary = asyncio.run(runner.run_once())

        self.assertIs(result_summary, summary)
        mock_telegram.Bot.assert_called_once_with(token="token_abc_123")
        mock_bot.send_message.assert_awaited_once_with(
            chat_id="chat_xyz_789",
            text=(
                "Daily Streak Tool \u2014 batch completed\n"
                "Success: 3\n"
                "Failed: 1\n"
                "Unconfirmed: 1\n"
                "Skipped: 2"
            ),
        )

    def test_run_once_does_not_break_when_telegram_disabled_or_unconfigured(self) -> None:
        for config in (
            {"telegram": {"enabled": False}},
            {},
            {"telegram": {"enabled": True, "bot_token": "", "chat_id": ""}},
        ):
            summary = RunSummary().finish()

            class FakeSender:
                async def send_daily_links(self) -> RunSummary:
                    return summary

            runner = DaemonRunner(
                "unused.json",
                config_loader=lambda _: config,
                sender_factory=lambda _: FakeSender(),
            )
            result = asyncio.run(runner.run_once())
            self.assertIs(result, summary)

    def test_run_once_telegram_failure_does_not_fail_successful_batch(self) -> None:
        from daily_streak.models import RecipientResult, RecipientStatus

        summary = RunSummary(
            results=[RecipientResult("Alice", RecipientStatus.SUCCESS)]
        ).finish()

        config = {
            "telegram": {
                "enabled": True,
                "bot_token": "token",
                "chat_id": "chat",
            }
        }

        class FakeSender:
            async def send_daily_links(self) -> RunSummary:
                return summary

        mock_telegram = MagicMock()
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(side_effect=RuntimeError("Network is down"))
        mock_telegram.Bot.return_value = mock_bot

        runner = DaemonRunner(
            "unused.json",
            config_loader=lambda _: config,
            sender_factory=lambda _: FakeSender(),
        )

        with patch.dict("sys.modules", {"telegram": mock_telegram}):
            result = asyncio.run(runner.run_once())

        self.assertIs(result, summary)
        self.assertEqual(result.success_count, 1)

    def test_run_once_skipped_overlapping_run_does_not_send_telegram_summary(self) -> None:
        mock_notifier = MagicMock()
        mock_notifier.batch_completion = AsyncMock()

        async def scenario() -> tuple[object, object]:
            started = asyncio.Event()
            release = asyncio.Event()

            runner = DaemonRunner(
                "unused.json",
                config_loader=lambda _: {},
                sender_factory=lambda _: RecordingSender(1, started, release),
                notifier_factory=lambda _: mock_notifier,
            )
            first = asyncio.create_task(runner.run_once())
            await started.wait()
            second = await runner.run_once()
            release.set()
            first_result = await first
            return first_result, second

        first_summary, second_summary = asyncio.run(scenario())
        self.assertIsNotNone(first_summary)
        self.assertIsNone(second_summary)
        # exactly one completion notification was sent (for the completed run, not the skipped one)
        mock_notifier.batch_completion.assert_awaited_once_with(first_summary)

    def test_daemon_lock_acquire_and_release(self) -> None:
        from daily_streak.lock import DaemonLock

        with TemporaryDirectory() as temp_dir:
            lock = DaemonLock(temp_dir, pid=11111, process_is_daemon=lambda pid, host: True)
            self.assertFalse(lock.is_active())
            lock.acquire()
            self.assertTrue(lock.is_active())
            metadata = lock.read_metadata()
            self.assertEqual(metadata["pid"], 11111)
            lock.release()
            self.assertFalse(lock.is_active())

    def test_daemon_lock_refuses_duplicate_start_when_active(self) -> None:
        from daily_streak.lock import DaemonLock, DaemonLockedError

        with TemporaryDirectory() as temp_dir:
            lock1 = DaemonLock(temp_dir, pid=11111, process_is_daemon=lambda pid, host: True)
            lock1.acquire()

            lock2 = DaemonLock(temp_dir, pid=22222, process_is_daemon=lambda pid, host: True)
            with self.assertRaises(DaemonLockedError) as ctx:
                lock2.acquire()
            self.assertIn("11111", str(ctx.exception))

            lock1.release()

    def test_daemon_lock_recovers_stale_pid_without_signalling_unrelated_process(self) -> None:
        import json
        from daily_streak.lock import DaemonLock

        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            lock_path = data_dir / "daemon.lock"
            # Write a stale lock file with unrelated PID 99999
            lock_path.write_text(
                json.dumps({"pid": 99999, "hostname": "localhost", "token": "old", "created_at": 100.0}),
                encoding="utf-8",
            )

            signals_sent: list[tuple[int, int]] = []

            def fake_kill(pid: int, sig: int) -> None:
                signals_sent.append((pid, sig))

            # process_is_daemon returns False (unrelated process / dead process)
            lock = DaemonLock(
                data_dir,
                pid=33333,
                process_is_daemon=lambda pid, host: False,
            )
            self.assertFalse(lock.is_active())

            # Acquiring should recover stale lock cleanly without sending any signal to 99999
            with patch("os.kill", side_effect=fake_kill):
                lock.acquire()

            self.assertEqual(signals_sent, [])
            self.assertEqual(lock.read_metadata()["pid"], 33333)
            lock.release()

    def test_daemon_manager_status_stopped_and_running(self) -> None:
        from daily_streak.daemon import DaemonManager

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.json"
            config = {
                "data_dir": str(root / "data"),
                "schedule": {"time": "09:30", "timezone": "Asia/Ho_Chi_Minh", "run_on_start": True},
            }
            manager = DaemonManager(config_path, config_loader=lambda _: config, process_is_daemon=lambda pid, host: True)

            # Initially stopped
            status = manager.get_status()
            self.assertEqual(status["status"], "Stopped")
            self.assertFalse(status["is_running"])
            self.assertIsNone(status["pid"])
            self.assertEqual(status["schedule_time"], "09:30")
            self.assertEqual(status["timezone"], "Asia/Ho_Chi_Minh")
            self.assertTrue(status["run_on_start"])

            # Now acquire lock to simulate running daemon
            from daily_streak.lock import DaemonLock
            lock = DaemonLock(root / "data", pid=44444, process_is_daemon=lambda pid, host: True)
            lock.acquire()

            status_running = manager.get_status()
            self.assertEqual(status_running["status"], "Running")
            self.assertTrue(status_running["is_running"])
            self.assertEqual(status_running["pid"], 44444)
            lock.release()

    def test_daemon_manager_start_launches_detached_process(self) -> None:
        from daily_streak.daemon import DaemonManager

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.json"
            config = {"data_dir": str(root / "data"), "schedule": {"time": "12:00", "timezone": "UTC"}}

            spawned_cmd: list[str] = []
            spawned_kwargs: dict[str, object] = {}

            def fake_popen(cmd: list[str], **kwargs: object) -> MockProcess:
                spawned_cmd.extend(cmd)
                spawned_kwargs.update(kwargs)
                return MockProcess(pid=55555)

            manager = DaemonManager(
                config_path,
                config_loader=lambda _: config,
                popen_factory=fake_popen,
            )

            pid = manager.start()
            self.assertEqual(pid, 55555)
            self.assertIn("daemon", spawned_cmd)
            self.assertIn("--config", spawned_cmd)
            # Ensure it is detached with its own session on POSIX
            if sys.platform != "win32":
                self.assertTrue(spawned_kwargs.get("start_new_session"))
            self.assertEqual(spawned_kwargs.get("stdin"), ANY)

    def test_daemon_manager_refuses_duplicate_start_when_running(self) -> None:
        from daily_streak.daemon import DaemonManager, DaemonProcessError
        from daily_streak.lock import DaemonLock

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.json"
            config = {"data_dir": str(root / "data")}

            lock = DaemonLock(root / "data", pid=66666, process_is_daemon=lambda pid, host: True)
            lock.acquire()

            manager = DaemonManager(config_path, config_loader=lambda _: config, process_is_daemon=lambda pid, host: True)
            with self.assertRaises(DaemonProcessError) as ctx:
                manager.start()
            self.assertIn("already running with PID 66666", str(ctx.exception))
            lock.release()

    def test_daemon_manager_graceful_stop_and_does_not_force_kill(self) -> None:
        from daily_streak.daemon import DaemonManager
        from daily_streak.lock import DaemonLock

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.json"
            config = {"data_dir": str(root / "data")}

            signals: list[tuple[int, int]] = []

            def fake_kill(pid: int, sig: int) -> None:
                signals.append((pid, sig))

            lock = DaemonLock(root / "data", pid=77777, process_is_daemon=lambda pid, host: True)
            lock.acquire()

            manager = DaemonManager(config_path, config_loader=lambda _: config, process_is_daemon=lambda pid, host: True)

            # Stopping sends SIGTERM
            with patch("os.kill", side_effect=fake_kill):
                # We release lock after kill is simulated so stop succeeds
                def fake_kill_and_release(pid: int, sig: int) -> None:
                    signals.append((pid, sig))
                    lock.release()

                with patch("os.kill", side_effect=fake_kill_and_release):
                    stopped = manager.stop(timeout=1.0)

            self.assertTrue(stopped)
            self.assertEqual(signals, [(77777, signal.SIGTERM)])

    def test_daemon_manager_log_tail_and_bounded_rotation(self) -> None:
        from daily_streak.daemon import DaemonManager

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.json"
            data_dir = root / "data"
            data_dir.mkdir(parents=True)
            config = {"data_dir": str(data_dir)}

            log_file = data_dir / "daemon.log"
            log_file.write_text("\n".join(f"Line {i}" for i in range(1, 101)) + "\n", encoding="utf-8")

            manager = DaemonManager(config_path, config_loader=lambda _: config)

            # Read bounded tail
            tail = manager.read_logs(max_lines=10)
            self.assertEqual(len(tail), 10)
            self.assertEqual(tail[-1], "Line 100")
            self.assertEqual(tail[0], "Line 91")

            # Rotation when exceeding max_bytes
            manager.rotate_logs_if_needed(log_file, max_bytes=50, backup_count=2)
            self.assertTrue((data_dir / "daemon.log.1").exists())
            self.assertFalse(log_file.exists())

    def test_daemon_manager_child_exits_immediately_reports_error(self) -> None:
        from daily_streak.daemon import DaemonManager, DaemonProcessError

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.json"
            data_dir = root / "data"
            data_dir.mkdir(parents=True)
            config = {"data_dir": str(data_dir)}

            log_file = data_dir / "daemon.log"
            log_file.write_text("SyntaxError: invalid syntax in config\n", encoding="utf-8")

            def failing_popen(cmd: list[str], **kwargs: object) -> MockProcess:
                return MockProcess(pid=88888, returncode=1)

            manager = DaemonManager(
                config_path,
                config_loader=lambda _: config,
                popen_factory=failing_popen,
            )

            with self.assertRaises(DaemonProcessError) as ctx:
                manager.start()

            self.assertIn("SyntaxError: invalid syntax in config", str(ctx.exception))

    def test_daemon_manager_repeated_start_during_startup_rejected(self) -> None:
        import json
        import os
        from daily_streak.daemon import DaemonManager, DaemonProcessError, DAEMON_STARTING_FILENAME

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.json"
            data_dir = root / "data"
            data_dir.mkdir(parents=True)
            config = {"data_dir": str(data_dir)}

            # Write active startup sentinel
            sentinel = data_dir / DAEMON_STARTING_FILENAME
            sentinel.write_text(json.dumps({"pid": os.getpid(), "time": time.time()}), encoding="utf-8")

            manager = DaemonManager(config_path, config_loader=lambda _: config)
            with self.assertRaises(DaemonProcessError) as ctx:
                manager.start()

            self.assertIn("startup is already in progress", str(ctx.exception))

    def test_daemon_manager_in_memory_lock_prevents_concurrent_spawn(self) -> None:
        from daily_streak.daemon import DaemonManager, DaemonProcessError

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.json"
            data_dir = root / "data"
            data_dir.mkdir(parents=True)
            config = {"data_dir": str(data_dir)}

            manager = DaemonManager(config_path, config_loader=lambda _: config)
            # Acquire class-level start lock
            manager._start_lock.acquire()
            try:
                with self.assertRaises(DaemonProcessError) as ctx:
                    manager.start()
                self.assertIn("startup is already in progress", str(ctx.exception))
            finally:
                manager._start_lock.release()

    def test_is_daemon_process_alive_distinguishes_zombie(self) -> None:
        from daily_streak.lock import is_daemon_process_alive

        with patch("daily_streak.lock._process_is_alive", return_value=True):
            # Zombie process in /proc/{pid}/status
            with patch("pathlib.Path.exists", return_value=True):
                with patch("pathlib.Path.read_text", return_value="Name:\tpython\nState:\tZ (zombie)\n"):
                    self.assertFalse(is_daemon_process_alive(12345))

                with patch("pathlib.Path.read_text", return_value="Name:\tpython\nState:\tS (sleeping)\n"):
                    with patch("pathlib.Path.read_bytes", return_value=b"/usr/bin/python3 -m daily_streak daemon"):
                        self.assertTrue(is_daemon_process_alive(12345))

                    with patch("pathlib.Path.read_bytes", return_value=b"/usr/bin/python3 -m something_else"):
                        self.assertFalse(is_daemon_process_alive(12345))

    def test_detached_start_and_status_with_real_subprocess(self) -> None:
        try:
            import apscheduler
        except ImportError:
            self.skipTest("apscheduler is not installed in the running python environment")

        from daily_streak.config import save_config
        from daily_streak.daemon import DaemonManager

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.json"
            data_dir = root / "data"
            config = {
                "data_dir": str(data_dir),
                "schedule": {"time": "00:00", "timezone": "UTC", "run_on_start": False},
                "tiktok": {"headless": True},
                "recipients": [],
                "videos": [],
            }
            save_config(config, config_path)

            manager = DaemonManager(config_path)
            self.assertEqual(manager.get_status()["status"], "Stopped")

            pid = manager.start()
            try:
                status = manager.get_status()
                self.assertEqual(status["status"], "Running")
                self.assertEqual(status["pid"], pid)
                self.assertTrue(status["is_running"])

                # Verify log captures startup info
                log_lines = manager.read_logs()
                self.assertTrue(any("Daemon started" in line for line in log_lines))
            finally:
                stopped = manager.stop()
                self.assertTrue(stopped)
                try:
                    os.waitpid(pid, 0)
                except (ChildProcessError, ProcessLookupError):
                    pass

            status_after = manager.get_status()
            self.assertEqual(status_after["status"], "Stopped")
            self.assertIsNone(status_after["pid"])

    def test_supports_kwarg_behavior(self) -> None:
        """Verify supports_kwarg correctly detects explicit parameter vs **kwargs vs unsupported."""
        from daily_streak.daemon import supports_kwarg

        def f_with_explicit(config: dict, config_path: Path | None = None) -> None:
            pass

        def f_with_kwargs(config: dict, **kwargs: object) -> None:
            pass

        def f_legacy(config: dict) -> None:
            pass

        self.assertTrue(supports_kwarg(f_with_explicit, "config_path"))
        self.assertTrue(supports_kwarg(f_with_kwargs, "config_path"))
        self.assertFalse(supports_kwarg(f_legacy, "config_path"))
        self.assertFalse(supports_kwarg("not_callable", "config_path"))

    def test_daemon_runner_factory_does_not_mask_internal_type_error(self) -> None:
        """P1: An internal TypeError inside factory must NOT cause fallback/retry calling factory twice."""
        from daily_streak.daemon import DaemonRunner

        call_count = 0

        def buggy_factory(config: dict, config_path: Path | None = None) -> object:
            nonlocal call_count
            call_count += 1
            # Simulate internal bug inside factory body
            raise TypeError("internal bug inside factory")

        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            runner = DaemonRunner(config_path, sender_factory=buggy_factory, config_loader=lambda p: {})

            with self.assertRaises(TypeError) as cm:
                asyncio.run(runner.run_once(check_pause=False))

            self.assertIn("internal bug inside factory", str(cm.exception))
            # Must only have been called ONCE; never retried without config_path!
            self.assertEqual(call_count, 1)
