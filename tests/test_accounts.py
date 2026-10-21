from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
import signal
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from daily_streak.accounts import (
    AccountInstance,
    AccountManager,
    validate_account_name,
)
from daily_streak.browser import get_browser_launch_config, get_user_data_dir
from daily_streak.cli import EXIT_OK, run_cli
from daily_streak.daemon import (
    DaemonManager,
    DaemonProcessError,
    ProcessDaemonManager,
    SystemdDaemonManager,
    create_daemon_service,
)
from daily_streak.paths import DATA_DIRECTORY_ENV_VAR, get_data_dir


def sample_config(data_dir: Path | None = None) -> dict[str, object]:
    cfg: dict[str, object] = {
        "schedule": {"time": "08:30", "timezone": "Asia/Ho_Chi_Minh", "run_on_start": False},
        "cookie_file": "cookies.json",
        "tiktok": {
            "headless": True,
            "message_format": "native_video_card",
            "allow_raw_url_fallback": False,
        },
        "recipients": [{"name": "Test User", "username": "testuser"}],
        "videos": ["https://www.tiktok.com/@test/video/123456789"],
    }
    if data_dir:
        cfg["data_dir"] = str(data_dir)
    return cfg


class AccountManagerTests(unittest.TestCase):
    def test_account_name_validation(self) -> None:
        self.assertEqual(validate_account_name("account-1"), "account-1")
        self.assertEqual(validate_account_name("acc_2.sub"), "acc_2.sub")

        with self.assertRaises(ValueError):
            validate_account_name("")
        with self.assertRaises(ValueError):
            validate_account_name("   ")
        with self.assertRaises(ValueError):
            validate_account_name("../evil")
        with self.assertRaises(ValueError):
            validate_account_name("slash/name")
        with self.assertRaises(ValueError):
            validate_account_name("-starts-with-hyphen")

    def test_create_account_strict_isolation(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            mgr = AccountManager(base_dir=base)

            acc1 = mgr.create_account(
                "acc1",
                schedule_time="09:00",
                schedule_timezone="UTC",
            )
            self.assertEqual(acc1.name, "acc1")
            self.assertTrue(acc1.config_path.exists())
            self.assertTrue(acc1.config_path.is_absolute())
            self.assertIn("accounts/acc1", str(acc1.config_path))

            # Cookie file is isolated and absolute
            self.assertIsNotNone(acc1.cookie_file)
            self.assertTrue(acc1.cookie_file.is_absolute())
            self.assertEqual(acc1.cookie_file, (base / "accounts/acc1/cookies.json").resolve())

            # Schedule time and defaults
            self.assertEqual(acc1.schedule_time, "09:00")
            self.assertEqual(acc1.schedule_timezone, "UTC")
            self.assertFalse(acc1.account_config["schedule"]["run_on_start"])
            self.assertEqual(acc1.account_config["tiktok"]["message_format"], "native_video_card")
            self.assertFalse(acc1.account_config["tiktok"]["allow_raw_url_fallback"])

            # Data directory isolation
            self.assertEqual(acc1.data_dir, get_data_dir(account_name="acc1"))
            self.assertTrue(str(acc1.data_dir).endswith("accounts/acc1"))

            # Cannot create duplicate account
            with self.assertRaises(ValueError):
                mgr.create_account("acc1")

    def test_data_dir_isolation_with_environment_override(self) -> None:
        with TemporaryDirectory() as temp_dir:
            env_data = Path(temp_dir) / "shared_data_dir"
            environ = {DATA_DIRECTORY_ENV_VAR: str(env_data)}

            # Single-account default
            dir_default = get_data_dir(environ=environ)
            self.assertEqual(dir_default, env_data)

            # Account 1
            dir_acc1 = get_data_dir(account_name="acc1", environ=environ)
            self.assertEqual(dir_acc1, env_data / "accounts" / "acc1")

            # Account 2
            dir_acc2 = get_data_dir(account_name="acc2", environ=environ)
            self.assertEqual(dir_acc2, env_data / "accounts" / "acc2")

            self.assertNotEqual(dir_acc1, dir_acc2)
            self.assertNotEqual(dir_acc1, dir_default)

    def test_browser_user_data_dir_isolation(self) -> None:
        tiktok_config = {
            "persistent_context": True,
            "user_data_dir_linux": "/tmp/test-chrome-profile",
        }
        profile_default = get_user_data_dir(tiktok_config, system="Linux")
        self.assertEqual(profile_default, Path("/tmp/test-chrome-profile"))

        profile_acc1 = get_user_data_dir(tiktok_config, system="Linux", account_name="account-1")
        self.assertEqual(profile_acc1, Path("/tmp/test-chrome-profile/accounts/account-1"))

        profile_acc2 = get_user_data_dir(tiktok_config, system="Linux", account_name="account-2")
        self.assertEqual(profile_acc2, Path("/tmp/test-chrome-profile/accounts/account-2"))

        self.assertNotEqual(profile_acc1, profile_acc2)

        # Full launch config includes isolated profile
        config1 = {"tiktok": dict(tiktok_config), "account_name": "account-1"}
        launch_cfg = get_browser_launch_config(config1)
        self.assertEqual(launch_cfg.user_data_dir, Path("/tmp/test-chrome-profile/accounts/account-1"))

    def test_conflict_detection_schedules_cookies_and_telegram(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            mgr = AccountManager(base_dir=base)

            # Create acc1
            mgr.create_account("acc1", schedule_time="00:00", schedule_timezone="Asia/Ho_Chi_Minh")

            # Create acc2 with identical schedule and shared cookie file
            cookie_shared = (base / "shared_cookies.json").resolve()
            acc2 = mgr.create_account(
                "acc2",
                schedule_time="00:00",
                schedule_timezone="Asia/Ho_Chi_Minh",
                cookie_file=cookie_shared,
            )

            # Reconfigure acc1 to also point to shared_cookies.json and same telegram token
            acc1 = mgr.get_account("acc1")
            acc1_cfg = dict(acc1.account_config)
            acc1_cfg["cookie_file"] = str(cookie_shared)
            acc1_cfg["telegram"] = {"enabled": True, "bot_token": "123456:SECRET_BOT_TOKEN"}
            mgr.config_saver(acc1_cfg, acc1.config_path)

            acc2_cfg = dict(acc2.account_config)
            acc2_cfg["telegram"] = {"enabled": True, "bot_token": "123456:SECRET_BOT_TOKEN"}
            mgr.config_saver(acc2_cfg, acc2.config_path)

            conflicts = mgr.check_conflicts()
            conflict_types = {c["type"] for c in conflicts}
            self.assertIn("schedule", conflict_types)
            self.assertIn("cookie", conflict_types)
            self.assertIn("telegram", conflict_types)

            # Secret token is masked in the message
            tg_conflict = next(c for c in conflicts if c["type"] == "telegram")
            self.assertNotIn("SECRET_BOT_TOKEN", tg_conflict["message"])
            self.assertIn("..._TOKEN", tg_conflict["message"])

    def test_duplicate_start_prevention_on_same_account(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            mgr = AccountManager(base_dir=base)
            mgr.create_account("acc1")

            d_mgr = mgr.get_daemon_manager("acc1")
            d_mgr._start_lock.acquire()
            try:
                with self.assertRaises(DaemonProcessError) as ctx:
                    d_mgr.start()
                self.assertIn("Daemon startup is already in progress", str(ctx.exception))
            finally:
                d_mgr._start_lock.release()

    def test_concurrent_start_different_accounts_allowed(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            mgr = AccountManager(base_dir=base)
            mgr.create_account("acc1")
            mgr.create_account("acc2")

            d_mgr1 = mgr.get_daemon_manager("acc1")
            d_mgr2 = mgr.get_daemon_manager("acc2")

            # Acquiring acc1's start lock does NOT block acc2!
            d_mgr1._start_lock.acquire()
            try:
                # acc2's start lock should be distinct and acquirable
                acquired2 = d_mgr2._start_lock.acquire(blocking=False)
                self.assertTrue(acquired2)
                d_mgr2._start_lock.release()
            finally:
                d_mgr1._start_lock.release()

    def test_systemd_daemon_template_unit_resolution(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            mgr = AccountManager(base_dir=base)
            acc = mgr.create_account("acc1")

            sys_mgr = SystemdDaemonManager(
                acc.config_path,
                account_name="acc1",
            )
            # Automatic template unit naming: daily-streak-daemon@acc1.service
            self.assertEqual(sys_mgr.service_name, "daily-streak-daemon@acc1.service")

        # Verify systemd/daily-streak-daemon@.service unit file exists and is valid
        project_root = Path(__file__).resolve().parent.parent
        template_file = project_root / "systemd" / "daily-streak-daemon@.service"
        self.assertTrue(template_file.exists(), "Template unit file systemd/daily-streak-daemon@.service must exist")
        content = template_file.read_text(encoding="utf-8")
        self.assertIn("[Unit]", content)
        self.assertIn("[Service]", content)
        self.assertIn("[Install]", content)
        self.assertIn("%i", content)
        self.assertIn("--account %i", content)
        self.assertIn("--config %h/.config/daily-streak-tool/accounts/%i/config.json", content)

    def test_systemd_analyze_verify(self) -> None:
        import shutil

        cmd = shutil.which("systemd-analyze")
        if not cmd:
            self.skipTest("systemd-analyze not installed or available on this system")

        project_root = Path(__file__).resolve().parent.parent
        template_file = project_root / "systemd" / "daily-streak-daemon@.service"
        daemon_file = project_root / "systemd" / "daily-streak-daemon.service"
        tg_file = project_root / "systemd" / "daily-streak-telegram-controller.service"

        res = subprocess.run(
            [cmd, "verify", str(template_file), str(daemon_file), str(tg_file)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            res.returncode,
            0,
            f"systemd-analyze verify failed (code {res.returncode}):\n{res.stderr}\n{res.stdout}",
        )

    def test_complete_cross_account_isolation_and_locks(self) -> None:
        """Confirm starting account A cannot stop, lock, or overwrite account B.

        Each account must have separate cookies, browser profiles, daily state,
        history, streak cache, logs, and daemon/send locks.
        """
        from daily_streak.history import RunHistoryStore
        from daily_streak.lock import DaemonLock, RunLock
        from daily_streak.models import RecipientResult, RecipientStatus, RunSummary
        from daily_streak.state import DailyRecipientState, DailyState, DailyStateStore
        from daily_streak.streak_reader import StreakCache, StreakRecord, StreakStatus

        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            test_data_dir = base / "state"
            with patch.dict(os.environ, {DATA_DIRECTORY_ENV_VAR: str(test_data_dir)}):
                mgr = AccountManager(base_dir=base)

                acc_a = mgr.create_account("acc-alpha", schedule_time="08:00")
                acc_b = mgr.create_account("acc-bravo", schedule_time="09:00")

                # 1. Path & directory isolation
                self.assertNotEqual(acc_a.data_dir, acc_b.data_dir)
                self.assertTrue(acc_a.data_dir.is_dir())
                self.assertTrue(acc_b.data_dir.is_dir())
                self.assertEqual(acc_a.data_dir, test_data_dir / "accounts/acc-alpha")
                self.assertEqual(acc_b.data_dir, test_data_dir / "accounts/acc-bravo")

                # 2. Cookie file isolation
                self.assertNotEqual(acc_a.cookie_file, acc_b.cookie_file)
                self.assertEqual(acc_a.cookie_file, (base / "accounts/acc-alpha/cookies.json").resolve())
                self.assertEqual(acc_b.cookie_file, (base / "accounts/acc-bravo/cookies.json").resolve())

                # 3. Browser profile isolation
                profile_a = get_user_data_dir(acc_a.account_config.get("tiktok", {}), account_name="acc-alpha")
                profile_b = get_user_data_dir(acc_b.account_config.get("tiktok", {}), account_name="acc-bravo")
                self.assertNotEqual(profile_a, profile_b)
                self.assertIn("accounts/acc-alpha", str(profile_a))
                self.assertIn("accounts/acc-bravo", str(profile_b))

                # 4. RunLock (send.lock) independent acquisition
                runlock_a = RunLock(acc_a.data_dir)
                runlock_b = RunLock(acc_b.data_dir)
                runlock_a.acquire()
                self.assertTrue(runlock_a.is_active())
                self.assertFalse(runlock_b.is_active())

                # Account B can acquire its own send.lock simultaneously without interference
                runlock_b.acquire()
                self.assertTrue(runlock_b.is_active())
                self.assertTrue(runlock_a.is_active())

                runlock_a.release()
                self.assertFalse(runlock_a.is_active())
                self.assertTrue(runlock_b.is_active())
                runlock_b.release()
                self.assertFalse(runlock_b.is_active())

                # 5. DaemonLock (daemon.lock) independent acquisition
                dlock_a = DaemonLock(
                    acc_a.data_dir,
                    config_path=acc_a.config_path,
                    process_is_daemon=lambda _pid, _h: True,
                )
                dlock_b = DaemonLock(
                    acc_b.data_dir,
                    config_path=acc_b.config_path,
                    process_is_daemon=lambda _pid, _h: True,
                )
                dlock_a.acquire()
                self.assertTrue(dlock_a.is_active())
                self.assertFalse(dlock_b.is_active())

                dlock_b.acquire()
                self.assertTrue(dlock_b.is_active())
                self.assertTrue(dlock_a.is_active())

                dlock_a.release()
                self.assertFalse(dlock_a.is_active())
                self.assertTrue(dlock_b.is_active())
                dlock_b.release()
                self.assertFalse(dlock_b.is_active())

                # 6. Daily state isolation
                state_store_a = DailyStateStore(acc_a.data_dir)
                state_store_b = DailyStateStore(acc_b.data_dir)

                state_a = state_store_a.load("2026-10-21", "Asia/Ho_Chi_Minh")
                state_store_a.record_success(
                    state_a,
                    username="recipient1",
                    video="https://www.tiktok.com/@u/video/1",
                    completed_at=datetime.now(timezone.utc),
                )

                state_b = state_store_b.load("2026-10-21", "Asia/Ho_Chi_Minh")
                self.assertNotIn("recipient1", state_b.recipients)

                # 7. Run history store isolation
                history_a = RunHistoryStore(acc_a.data_dir)
                history_b = RunHistoryStore(acc_b.data_dir)

                summary_a = RunSummary(
                    started_at=datetime.now(timezone.utc),
                    finished_at=datetime.now(timezone.utc),
                    results=[
                        RecipientResult(
                            recipient_name="user_a",
                            username="user_a",
                            status=RecipientStatus.SUCCESS,
                            selected_video="https://www.tiktok.com/@u/video/1",
                        )
                    ],
                )
                history_a.append(summary_a)
                self.assertEqual(len(history_a.load()), 1)
                self.assertEqual(len(history_b.load()), 0)

                # 8. Streak cache isolation
                cache_a = StreakCache(acc_a.data_dir)
                cache_b = StreakCache(acc_b.data_dir)
                cache_a.set(
                    StreakRecord(
                        username="user_a",
                        count=42,
                        status=StreakStatus.AVAILABLE,
                        updated_at="2026-10-21T00:00:00Z",
                    )
                )
                self.assertIsNotNone(cache_a.get("user_a"))
                self.assertIsNone(cache_b.get("user_a"))

                # 9. DaemonManager isolation (stopping account A does not affect account B)
                d_mgr_a = mgr.get_daemon_manager("acc-alpha")
                d_mgr_b = mgr.get_daemon_manager("acc-bravo")

                # Mock running status on A
                with patch.object(d_mgr_a, "get_status", return_value={"running": True, "pid": 11111}):
                    with patch.object(d_mgr_b, "get_status", return_value={"running": True, "pid": 22222}):
                        with patch("os.kill") as mock_kill:
                            # Stop account A
                            d_mgr_a.stop()
                            # Only PID 11111 is killed, never 22222
                            for call in mock_kill.call_args_list:
                                pid_called = call.args[0]
                                self.assertEqual(pid_called, 11111)
                                self.assertNotEqual(pid_called, 22222)


class AccountCliTests(unittest.TestCase):
    def test_cli_accounts_lifecycle(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            config_path = base / "config.json"
            config_path.write_text(json.dumps(sample_config()), encoding="utf-8")

            # 1. Create account-1 via CLI
            out_create = StringIO()
            exit_code = run_cli(
                [
                    "--config",
                    str(config_path),
                    "accounts",
                    "create",
                    "account-1",
                    "--schedule-time",
                    "07:15",
                ],
                stdout=out_create,
                stderr=StringIO(),
            )
            self.assertEqual(exit_code, EXIT_OK)
            self.assertIn("Created account 'account-1'", out_create.getvalue())

            # 2. List accounts via CLI --json
            out_list = StringIO()
            exit_code = run_cli(
                ["--config", str(config_path), "accounts", "list", "--json"],
                stdout=out_list,
                stderr=StringIO(),
            )
            self.assertEqual(exit_code, EXIT_OK)
            payload = json.loads(out_list.getvalue())
            acc_names = [a["name"] for a in payload["accounts"]]
            self.assertIn("account-1", acc_names)

            # 3. Status command via CLI
            out_status = StringIO()
            exit_code = run_cli(
                ["--config", str(config_path), "accounts", "status"],
                stdout=out_status,
                stderr=StringIO(),
            )
            self.assertEqual(exit_code, EXIT_OK)
            self.assertIn("Account: account-1", out_status.getvalue())
            self.assertIn("07:15", out_status.getvalue())


class AccountTuiTests(unittest.TestCase):
    def test_tui_controller_multi_account_switching(self) -> None:
        from daily_streak.tui import TuiController

        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            mgr = AccountManager(base_dir=base)
            mgr.create_account("alpha", schedule_time="05:00")
            mgr.create_account("beta", schedule_time="06:00")

            controller = TuiController(
                base / "accounts/alpha/config.json",
                account_name="alpha",
                account_manager=mgr,
            )
            self.assertEqual(controller.active_account_name, "alpha")
            status_alpha = controller.daemon_status()
            self.assertEqual(status_alpha["schedule_time"], "05:00")

            # Switch account to beta
            controller.select_account("beta")
            self.assertEqual(controller.active_account_name, "beta")
            status_beta = controller.daemon_status()
            self.assertEqual(status_beta["schedule_time"], "06:00")
            self.assertIn("accounts/beta", str(controller.config_path))

    def test_delete_account(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            mgr = AccountManager(base_dir=base)
            acc = mgr.create_account("to_delete")
            self.assertTrue(acc.config_path.exists())

            # Deleting named account removes its directory
            deleted = mgr.delete_account("to_delete")
            self.assertTrue(deleted)
            self.assertFalse(acc.config_path.exists())

            # Cannot delete default root account
            with self.assertRaises(ValueError):
                mgr.delete_account("default")

    def test_cli_global_account_option(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            mgr = AccountManager(base_dir=base)
            acc = mgr.create_account("my-brand", schedule_time="11:30")

            # status with --account
            out = StringIO()
            exit_code = run_cli(
                ["--config", str(acc.config_path), "--account", "my-brand", "status", "--json"],
                stdout=out,
                stderr=StringIO(),
            )
            self.assertEqual(exit_code, EXIT_OK)
            payload = json.loads(out.getvalue())
            self.assertIn("accounts/my-brand", payload["data_dir"])

            # daemon --status with --account
            out_daemon = StringIO()
            exit_code = run_cli(
                ["--config", str(acc.config_path), "--account", "my-brand", "daemon", "--status", "--json"],
                stdout=out_daemon,
                stderr=StringIO(),
            )
            self.assertEqual(exit_code, EXIT_OK)
            d_payload = json.loads(out_daemon.getvalue())
            self.assertEqual(d_payload["account_name"], "my-brand")
            self.assertEqual(d_payload["schedule_time"], "11:30")


try:
    import textual  # noqa: F401

    HAS_TEXTUAL = True
except ImportError:
    HAS_TEXTUAL = False


@unittest.skipUnless(HAS_TEXTUAL, "textual is not installed")
class MultiAccountTuiAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_tui_app_multi_account_rendering_and_table(self) -> None:
        from textual.widgets import DataTable, Static, TabbedContent
        from daily_streak.tui import TuiController, create_tui_app

        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            mgr = AccountManager(base_dir=base)
            mgr.create_account("brand-a", schedule_time="04:00")
            mgr.create_account("brand-b", schedule_time="05:00")

            controller = TuiController(
                base / "accounts/brand-a/config.json",
                account_name="brand-a",
                account_manager=mgr,
            )
            app = create_tui_app(
                base / "accounts/brand-a/config.json",
                controller=controller,
                account_name="brand-a",
            )
            async with app.run_test(size=(100, 40)) as pilot:
                tabbed = pilot.app.query_one(TabbedContent)
                tabbed.active = "daemon-tab"
                await pilot.pause()

                # Verify banner displays active account
                banner = pilot.app.query_one("#daemon-account-banner", Static).render()
                self.assertIn("Active Account: brand-a", str(banner))

                # Verify accounts table has rows for both brand-a and brand-b
                table = pilot.app.query_one("#daemon-accounts-table", DataTable)
                self.assertEqual(table.row_count, 2)
