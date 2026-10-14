from __future__ import annotations

from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from daily_streak.cli import (
    EXIT_OK,
    EXIT_RECIPIENT_FAILURES,
    build_parser,
    run_cli,
)
from daily_streak.config import save_config
from daily_streak.history import RunHistoryStore
from daily_streak.models import RecipientResult, RecipientStatus, RunSummary


def test_config(data_dir: Path) -> dict[str, object]:
    return {
        "data_dir": str(data_dir),
        "schedule": {"time": "00:00", "timezone": "UTC", "run_on_start": False},
        "tiktok": {"headless": True},
        "cookie_file": str(data_dir / "missing-cookies.json"),
        "recipients": [{"name": "Alice", "username": "alice"}],
        "videos": ["https://www.tiktok.com/example"],
    }


class FakeSender:
    def __init__(self, summary: RunSummary) -> None:
        self.summary = summary
        self.force_values: list[bool] = []

    async def send_daily_links(self, *, force: bool = False) -> RunSummary:
        self.force_values.append(force)
        return self.summary


class CliTests(unittest.TestCase):
    def test_argument_parser_supports_recipient_list(self) -> None:
        arguments = build_parser().parse_args(["recipients", "list"])
        self.assertEqual(arguments.command, "recipients")
        self.assertEqual(arguments.recipient_command, "list")

    def test_status_human_output_is_browser_free(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = root / "config.json"
            save_config(test_config(root / "data"), config_path)
            stdout = StringIO()
            exit_code = run_cli(["--config", str(config_path), "status"], stdout=stdout, stderr=StringIO())

        self.assertEqual(exit_code, EXIT_OK)
        output = stdout.getvalue()
        self.assertIn("Daily Streak Tool v2", output)
        self.assertIn("Recipients    1", output)
        self.assertIn("Browser       headless", output)

    def test_status_json_output_contains_only_json(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = root / "config.json"
            save_config(test_config(root / "data"), config_path)
            stdout = StringIO()
            stderr = StringIO()
            exit_code = run_cli(
                ["--config", str(config_path), "status", "--json"],
                stdout=stdout,
                stderr=stderr,
            )

        self.assertEqual(exit_code, EXIT_OK)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["recipients"], 1)
        self.assertEqual(stderr.getvalue(), "")

    def test_send_uses_shared_sender_and_passes_force_flag(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = root / "config.json"
            save_config(test_config(root / "data"), config_path)
            summary = RunSummary().finish()
            sender = FakeSender(summary)

            normal_exit = run_cli(
                ["--config", str(config_path), "send"],
                stdout=StringIO(),
                stderr=StringIO(),
                sender_factory=lambda _: sender,
            )
            forced_exit = run_cli(
                ["--config", str(config_path), "send", "--force"],
                stdout=StringIO(),
                stderr=StringIO(),
                sender_factory=lambda _: sender,
            )

        self.assertEqual(normal_exit, EXIT_OK)
        self.assertEqual(forced_exit, EXIT_OK)
        self.assertEqual(sender.force_values, [False, True])

    def test_send_returns_failure_exit_code_for_failed_recipient(self) -> None:
        summary = RunSummary(
            results=[
                RecipientResult("Alice", RecipientStatus.FAILED, error_message="not found"),
            ]
        ).finish()
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = root / "config.json"
            save_config(test_config(root / "data"), config_path)
            exit_code = run_cli(
                ["--config", str(config_path), "send", "--json"],
                stdout=StringIO(),
                stderr=StringIO(),
                sender_factory=lambda _: FakeSender(summary),
            )

        self.assertEqual(exit_code, EXIT_RECIPIENT_FAILURES)

    def test_history_limit_and_json_output(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            data_dir = root / "data"
            config_path = root / "config.json"
            save_config(test_config(data_dir), config_path)
            history = RunHistoryStore(data_dir)
            history.append(RunSummary().finish())
            history.append(RunSummary().finish())
            stdout = StringIO()
            exit_code = run_cli(
                ["--config", str(config_path), "history", "--limit", "1", "--json"],
                stdout=stdout,
                stderr=StringIO(),
            )

            self.assertEqual(exit_code, EXIT_OK)
            self.assertEqual(len(json.loads(stdout.getvalue())["history"]), 1)

            # Test human-readable history formatting
            human_stdout = StringIO()
            run_cli(
                ["--config", str(config_path), "history"],
                stdout=human_stdout,
                stderr=StringIO(),
            )
            self.assertIn("0 success, 0 failed, 0 unconfirmed, 0 skipped", human_stdout.getvalue())

    def test_doctor_returns_structured_results(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = root / "config.json"
            save_config(test_config(root / "data"), config_path)
            stdout = StringIO()
            exit_code = run_cli(
                ["--config", str(config_path), "doctor", "--json"],
                stdout=stdout,
                stderr=StringIO(),
            )

        payload = json.loads(stdout.getvalue())
        self.assertIn("checks", payload)
        self.assertIn("summary", payload)
        self.assertGreater(len(payload["checks"]), 1)
        self.assertIn(exit_code, (EXIT_OK, 3))

    def test_status_includes_daemon_information(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = root / "config.json"
            save_config(test_config(root / "data"), config_path)
            stdout = StringIO()
            exit_code = run_cli(
                ["--config", str(config_path), "status"],
                stdout=stdout,
                stderr=StringIO(),
            )
            self.assertEqual(exit_code, EXIT_OK)
            self.assertIn("Daemon        Stopped", stdout.getvalue())

            json_stdout = StringIO()
            exit_code_json = run_cli(
                ["--config", str(config_path), "status", "--json"],
                stdout=json_stdout,
                stderr=StringIO(),
            )
            self.assertEqual(exit_code_json, EXIT_OK)
            payload = json.loads(json_stdout.getvalue())
            self.assertEqual(payload["daemon"], {"status": "Stopped", "pid": None})

    def test_daemon_status_cli_human_and_json(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = root / "config.json"
            save_config(test_config(root / "data"), config_path)

            stdout = StringIO()
            exit_code = run_cli(
                ["--config", str(config_path), "daemon", "--status"],
                stdout=stdout,
                stderr=StringIO(),
            )
            self.assertEqual(exit_code, EXIT_OK)
            output = stdout.getvalue()
            self.assertIn("Daily Streak Tool Daemon", output)
            self.assertIn("Status:       Stopped", output)
            self.assertIn("Schedule:     00:00 (UTC)", output)
            self.assertIn("Run on start: No", output)

            json_stdout = StringIO()
            exit_code_json = run_cli(
                ["--config", str(config_path), "daemon", "--status", "--json"],
                stdout=json_stdout,
                stderr=StringIO(),
            )
            self.assertEqual(exit_code_json, EXIT_OK)
            payload = json.loads(json_stdout.getvalue())
            self.assertEqual(payload["status"], "Stopped")
            self.assertIsNone(payload["pid"])
            self.assertFalse(payload["run_on_start"])

    def test_daemon_status_running_cli(self) -> None:
        from unittest.mock import patch

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = root / "config.json"
            save_config(test_config(root / "data"), config_path)

            mock_status = {
                "status": "Running",
                "is_running": True,
                "pid": 7777,
                "schedule_time": "00:00",
                "timezone": "UTC",
                "next_run": "2026-10-15 00:00:00 UTC",
                "run_on_start": True,
                "last_run": None,
                "log_path": root / "data" / "daemon.log",
            }
            with patch("daily_streak.daemon.DaemonManager.get_status", return_value=mock_status):
                stdout = StringIO()
                exit_code = run_cli(
                    ["--config", str(config_path), "daemon", "--status"],
                    stdout=stdout,
                    stderr=StringIO(),
                )
                self.assertEqual(exit_code, EXIT_OK)
                self.assertIn("Status:       Running (PID 7777)", stdout.getvalue())
                self.assertIn("Run on start: Yes", stdout.getvalue())

                json_stdout = StringIO()
                exit_code_json = run_cli(
                    ["--config", str(config_path), "daemon", "--status", "--json"],
                    stdout=json_stdout,
                    stderr=StringIO(),
                )
                self.assertEqual(exit_code_json, EXIT_OK)
                payload = json.loads(json_stdout.getvalue())
                self.assertEqual(payload["status"], "Running")
                self.assertEqual(payload["pid"], 7777)
                self.assertTrue(payload["run_on_start"])

    def test_daemon_stop_cli(self) -> None:
        from unittest.mock import patch

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = root / "config.json"
            save_config(test_config(root / "data"), config_path)

            with patch("daily_streak.daemon.DaemonManager.stop", return_value=True):
                stdout = StringIO()
                exit_code = run_cli(
                    ["--config", str(config_path), "daemon", "--stop"],
                    stdout=stdout,
                    stderr=StringIO(),
                )
                self.assertEqual(exit_code, EXIT_OK)
                self.assertIn("Daemon stopped.", stdout.getvalue())

            with patch("daily_streak.daemon.DaemonManager.stop", return_value=False):
                stdout = StringIO()
                exit_code = run_cli(
                    ["--config", str(config_path), "daemon", "--stop"],
                    stdout=stdout,
                    stderr=StringIO(),
                )
                self.assertEqual(exit_code, EXIT_OK)
                self.assertIn("Stop signal sent; daemon may be finishing an in-progress batch.", stdout.getvalue())

    def test_cli_import_does_not_load_tkinter_or_plyer(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import daily_streak.cli; print('tkinter' in sys.modules, 'plyer' in sys.modules)",
            ],
            capture_output=True,
            check=True,
            text=True,
        )
        self.assertEqual(result.stdout.strip(), "False False")
