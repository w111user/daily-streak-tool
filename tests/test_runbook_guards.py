from __future__ import annotations

from io import StringIO
import json
import os
from pathlib import Path
import socket
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from daily_streak.config import save_config
from daily_streak.runbook_guards import (
    GuardError,
    check_run_on_start,
    main as guard_main,
    probe_sender_lock,
    verify_daemon_stopped,
)


def sample_test_config(data_dir: Path, *, run_on_start: Any = False) -> dict[str, object]:
    return {
        "data_dir": str(data_dir),
        "schedule": {
            "time": "09:00",
            "timezone": "Asia/Ho_Chi_Minh",
            "run_on_start": run_on_start,
        },
        "tiktok": {"headless": True},
        "cookie_file": str(data_dir / "cookies.json"),
        "recipients": [],
        "videos": [],
        "telegram": {"enabled": False},
        "daemon": {"service_backend": "process"},
    }


class RunbookGuardsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "config.json"
        self.data_dir = self.root / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.send_lock = self.data_dir / "send.lock"
        self.daemon_lock = self.data_dir / "daemon.lock"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # 1. No lock file -> Safe UNLOCKED
    def test_probe_sender_lock_unlocked_when_no_file(self) -> None:
        cfg = sample_test_config(self.data_dir)
        save_config(cfg, self.config_path)

        result = probe_sender_lock(self.config_path)
        self.assertTrue(result["safe"])
        self.assertEqual(result["reason"], "UNLOCKED")

    # 2. Active sender -> GuardError (fail closed)
    def test_probe_sender_lock_active_sender_fails_closed(self) -> None:
        cfg = sample_test_config(self.data_dir)
        save_config(cfg, self.config_path)

        meta = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "created_at": 1000.0,
            "token": "test-token",
        }
        self.send_lock.write_text(json.dumps(meta), encoding="utf-8")

        with self.assertRaises(GuardError) as ctx:
            probe_sender_lock(self.config_path)
        self.assertIn("Active sender batch is running", str(ctx.exception))

    # 3. Stale lock (PID dead) -> Safe STALE_INACTIVE
    def test_probe_sender_lock_stale_lock_safe(self) -> None:
        cfg = sample_test_config(self.data_dir)
        save_config(cfg, self.config_path)

        dead_pid = 9999999
        meta = {
            "pid": dead_pid,
            "hostname": socket.gethostname(),
            "created_at": 1000.0,
            "token": "dead-token",
        }
        self.send_lock.write_text(json.dumps(meta), encoding="utf-8")

        result = probe_sender_lock(self.config_path)
        self.assertTrue(result["safe"])
        self.assertEqual(result["reason"], "STALE_INACTIVE")
        self.assertEqual(result["pid"], dead_pid)

    # 4. Hostname mismatch -> GuardError (fail closed)
    def test_probe_sender_lock_hostname_mismatch_fails_closed(self) -> None:
        cfg = sample_test_config(self.data_dir)
        save_config(cfg, self.config_path)

        meta = {
            "pid": 12345,
            "hostname": "some-other-host.net",
            "created_at": 1000.0,
            "token": "other-token",
        }
        self.send_lock.write_text(json.dumps(meta), encoding="utf-8")

        with self.assertRaises(GuardError) as ctx:
            probe_sender_lock(self.config_path)
        self.assertIn("belongs to host 'some-other-host.net'", str(ctx.exception))

    # 5. Hostname missing -> GuardError (fail closed)
    def test_probe_sender_lock_hostname_missing_fails_closed(self) -> None:
        cfg = sample_test_config(self.data_dir)
        save_config(cfg, self.config_path)

        meta = {
            "pid": 12345,
            "created_at": 1000.0,
            "token": "no-host-token",
        }
        self.send_lock.write_text(json.dumps(meta), encoding="utf-8")

        with self.assertRaises(GuardError) as ctx:
            probe_sender_lock(self.config_path)
        self.assertIn("missing 'hostname' field", str(ctx.exception))

    # 6. PID missing or invalid -> GuardError (fail closed)
    def test_probe_sender_lock_pid_invalid_fails_closed(self) -> None:
        cfg = sample_test_config(self.data_dir)
        save_config(cfg, self.config_path)

        meta = {
            "pid": "invalid-pid",
            "hostname": socket.gethostname(),
            "created_at": 1000.0,
        }
        self.send_lock.write_text(json.dumps(meta), encoding="utf-8")

        with self.assertRaises(GuardError) as ctx:
            probe_sender_lock(self.config_path)
        self.assertIn("invalid or missing PID", str(ctx.exception))

    # 7. Corrupt lock JSON -> GuardError (fail closed)
    def test_probe_sender_lock_corrupt_json_fails_closed(self) -> None:
        cfg = sample_test_config(self.data_dir)
        save_config(cfg, self.config_path)

        self.send_lock.write_text("corrupted-not-json{{{", encoding="utf-8")

        with self.assertRaises(GuardError) as ctx:
            probe_sender_lock(self.config_path)
        self.assertIn("invalid or corrupt", str(ctx.exception))

    # 8. check_run_on_start = true -> GuardError when allow_run_on_start=False
    def test_check_run_on_start_true_blocks_by_default(self) -> None:
        cfg = sample_test_config(self.data_dir, run_on_start=True)
        save_config(cfg, self.config_path)

        with self.assertRaises(GuardError) as ctx:
            check_run_on_start(self.config_path, allow_run_on_start=False)
        self.assertIn("BLOCKED: 'schedule.run_on_start' is set to True", str(ctx.exception))

    # 9. check_run_on_start = true -> Passes when allow_run_on_start=True
    def test_check_run_on_start_true_allowed_explicitly(self) -> None:
        cfg = sample_test_config(self.data_dir, run_on_start=True)
        save_config(cfg, self.config_path)

        result = check_run_on_start(self.config_path, allow_run_on_start=True)
        self.assertTrue(result["safe"])
        self.assertTrue(result["run_on_start"])

    # 10. check_run_on_start = false -> Passes normally
    def test_check_run_on_start_false_passes(self) -> None:
        cfg = sample_test_config(self.data_dir, run_on_start=False)
        save_config(cfg, self.config_path)

        result = check_run_on_start(self.config_path)
        self.assertTrue(result["safe"])
        self.assertFalse(result["run_on_start"])

    # 11. check_run_on_start with non-bool types fails closed
    def test_check_run_on_start_non_bool_types_fail_closed(self) -> None:
        # String "true"
        cfg_str = sample_test_config(self.data_dir, run_on_start="true")
        save_config(cfg_str, self.config_path)
        with self.assertRaises(GuardError) as ctx:
            check_run_on_start(self.config_path)
        self.assertIn("must be a strict boolean (got str", str(ctx.exception))

        # Int 1
        cfg_int = sample_test_config(self.data_dir, run_on_start=1)
        save_config(cfg_int, self.config_path)
        with self.assertRaises(GuardError) as ctx:
            check_run_on_start(self.config_path)
        self.assertIn("must be a strict boolean (got int", str(ctx.exception))

    # 12. Invalid / missing config -> GuardError
    def test_check_run_on_start_missing_config_fails_closed(self) -> None:
        missing_path = self.root / "does_not_exist.json"
        with self.assertRaises(GuardError):
            check_run_on_start(missing_path)

    # 13. verify_daemon_stopped: Daemon actively running -> GuardError
    def test_verify_daemon_stopped_running_fails(self) -> None:
        cfg = sample_test_config(self.data_dir)
        save_config(cfg, self.config_path)

        mock_runner = MagicMock()
        mock_runner.return_value = subprocess.CompletedProcess(
            args=["streak", "daemon", "--status", "--json"],
            returncode=0,
            stdout=json.dumps({"status": "Running", "pid": os.getpid()}),
            stderr="",
        )

        with self.assertRaises(GuardError) as ctx:
            verify_daemon_stopped(self.config_path, runner=mock_runner)
        self.assertIn("expected 'Stopped'", str(ctx.exception))

    # 14. verify_daemon_stopped: Daemon cleanly stopped -> Passes
    def test_verify_daemon_stopped_cleanly_stopped(self) -> None:
        cfg = sample_test_config(self.data_dir)
        save_config(cfg, self.config_path)

        mock_runner = MagicMock()
        mock_runner.return_value = subprocess.CompletedProcess(
            args=["streak", "daemon", "--status", "--json"],
            returncode=0,
            stdout=json.dumps({"status": "Stopped", "pid": None}),
            stderr="",
        )

        result = verify_daemon_stopped(self.config_path, runner=mock_runner)
        self.assertTrue(result["stopped"])
        self.assertEqual(result["cli_status"], "Stopped")
        self.assertIsNone(result["cli_pid"])

    # 15. verify_daemon_stopped: Conflict (CLI stopped but PID alive) -> GuardError
    def test_verify_daemon_stopped_conflicting_pid_fails(self) -> None:
        cfg = sample_test_config(self.data_dir)
        save_config(cfg, self.config_path)

        self.daemon_lock.write_text(
            json.dumps({"pid": os.getpid(), "hostname": socket.gethostname()}),
            encoding="utf-8",
        )

        mock_runner = MagicMock()
        mock_runner.return_value = subprocess.CompletedProcess(
            args=["streak", "daemon", "--status", "--json"],
            returncode=0,
            stdout=json.dumps({"status": "Stopped", "pid": None}),
            stderr="",
        )

        with self.assertRaises(GuardError) as ctx:
            verify_daemon_stopped(self.config_path, runner=mock_runner)
        self.assertIn("Lingering live daemon process detected", str(ctx.exception))

    # 16. verify_daemon_stopped: Corrupt daemon.lock -> GuardError (fail closed)
    def test_verify_daemon_stopped_corrupt_daemon_lock_fails(self) -> None:
        cfg = sample_test_config(self.data_dir)
        save_config(cfg, self.config_path)

        self.daemon_lock.write_text("corrupt-daemon-lock{{{", encoding="utf-8")

        mock_runner = MagicMock()
        mock_runner.return_value = subprocess.CompletedProcess(
            args=["streak", "daemon", "--status", "--json"],
            returncode=0,
            stdout=json.dumps({"status": "Stopped", "pid": None}),
            stderr="",
        )

        with self.assertRaises(GuardError) as ctx:
            verify_daemon_stopped(self.config_path, runner=mock_runner)
        self.assertIn("corrupt or unreadable", str(ctx.exception))

    # 17. verify_daemon_stopped: daemon.lock hostname mismatch -> GuardError (fail closed)
    def test_verify_daemon_stopped_daemon_lock_hostname_mismatch_fails(self) -> None:
        cfg = sample_test_config(self.data_dir)
        save_config(cfg, self.config_path)

        self.daemon_lock.write_text(
            json.dumps({"pid": 9999999, "hostname": "remote-machine"}),
            encoding="utf-8",
        )

        mock_runner = MagicMock()
        mock_runner.return_value = subprocess.CompletedProcess(
            args=["streak", "daemon", "--status", "--json"],
            returncode=0,
            stdout=json.dumps({"status": "Stopped", "pid": None}),
            stderr="",
        )

        with self.assertRaises(GuardError) as ctx:
            verify_daemon_stopped(self.config_path, runner=mock_runner)
        self.assertIn("belongs to host 'remote-machine'", str(ctx.exception))

    # 18. CLI entrypoint main() tests
    def test_main_cli_commands(self) -> None:
        cfg = sample_test_config(self.data_dir, run_on_start=False)
        save_config(cfg, self.config_path)

        # probe-sender-lock CLI
        code = guard_main(["probe-sender-lock", "--config", str(self.config_path)])
        self.assertEqual(code, 0)

        # check-run-on-start CLI (safe false)
        code = guard_main(["check-run-on-start", "--config", str(self.config_path)])
        self.assertEqual(code, 0)

        # check-run-on-start CLI (blocked true)
        cfg_true = sample_test_config(self.data_dir, run_on_start=True)
        save_config(cfg_true, self.config_path)
        with patch("sys.stderr", new_callable=StringIO):
            code = guard_main(["check-run-on-start", "--config", str(self.config_path)])
        self.assertEqual(code, 1)

        # check-run-on-start CLI (allowed true)
        code = guard_main(["check-run-on-start", "--config", str(self.config_path), "--allow-run-on-start"])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
