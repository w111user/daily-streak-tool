"""First-class command-line interface for Daily Streak Tool v2."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys
from typing import Any, Callable, Mapping, Sequence, TextIO

from daily_streak.config import DEFAULT_CONFIG_PATH, load_config
from daily_streak.doctor import CheckStatus, DoctorReport, collect_doctor_report
from daily_streak.history import RunHistoryStore
from daily_streak.models import RunSummary
from daily_streak.paths import get_data_dir
from daily_streak.schedule import next_run, schedule_from_config
from daily_streak.state import DailyStateStore


logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_RECIPIENT_FAILURES = 1
EXIT_CONFIGURATION = 2
EXIT_RUNTIME = 3
EXIT_INTERRUPTED = 130

SenderFactory = Callable[[Mapping[str, Any]], Any]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="streak", description="Daily Streak Tool v2 command line")
    parser.add_argument("-a", "--account", default=None, help="Named account instance (e.g. account-1)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to config.json")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging on stderr")
    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser("status", help="Show local daily state without launching a browser")
    status.add_argument("--json", action="store_true", dest="json_output", help="Emit JSON only on stdout")

    send = commands.add_parser("send", help="Send configured daily links")
    send.add_argument("--force", action="store_true", help="Ignore today’s completed-recipient state")
    send.add_argument("--json", action="store_true", dest="json_output", help="Emit JSON only on stdout")

    doctor = commands.add_parser("doctor", help="Check local CLI/server setup without sending")
    doctor.add_argument("--json", action="store_true", dest="json_output", help="Emit JSON only on stdout")

    history = commands.add_parser("history", help="Show recent local run history")
    history.add_argument("--limit", type=_positive_int, default=10, help="Number of recent runs (default: 10)")
    history.add_argument("--json", action="store_true", dest="json_output", help="Emit JSON only on stdout")

    daemon = commands.add_parser("daemon", help="Run the scheduled server daemon")
    daemon.add_argument("--status", action="store_true", help="Check daemon status and exit")
    daemon.add_argument("--stop", action="store_true", help="Stop running background daemon")
    daemon.add_argument("--json", action="store_true", dest="json_output", help="Emit JSON only on stdout")

    accounts = commands.add_parser("accounts", help="Manage named account instances")
    account_commands = accounts.add_subparsers(dest="account_command", required=True)

    acc_list = account_commands.add_parser("list", help="List all configured accounts")
    acc_list.add_argument("--json", action="store_true", dest="json_output", help="Emit JSON only on stdout")

    acc_create = account_commands.add_parser("create", help="Create a new named account instance")
    acc_create.add_argument("name", help="Account name (alphanumeric, hyphens, underscores)")
    acc_create.add_argument("--copy-from", default=None, help="Account name or config path to copy settings from")
    acc_create.add_argument("--cookie-file", default=None, help="Path to cookies file for this account")
    acc_create.add_argument("--schedule-time", default=None, help="Daily run time (HH:MM)")
    acc_create.add_argument("--schedule-timezone", default=None, help="Schedule timezone (e.g. Asia/Ho_Chi_Minh)")
    acc_create.add_argument("--run-on-start", action="store_true", help="Run sender immediately on daemon start")
    acc_create.add_argument("--json", action="store_true", dest="json_output", help="Emit JSON only on stdout")

    acc_status = account_commands.add_parser("status", help="Show status of accounts and daemon instances")
    acc_status.add_argument("name", nargs="?", default=None, help="Account name (optional; shows all if omitted)")
    acc_status.add_argument("--json", action="store_true", dest="json_output", help="Emit JSON only on stdout")

    acc_start = account_commands.add_parser("start", help="Start background daemon for an account")
    acc_start.add_argument("name", help="Account name to start daemon for")
    acc_start.add_argument("--json", action="store_true", dest="json_output", help="Emit JSON only on stdout")

    acc_stop = account_commands.add_parser("stop", help="Stop background daemon for an account")
    acc_stop.add_argument("name", help="Account name to stop daemon for")
    acc_stop.add_argument("--timeout", type=float, default=10.0, help="Stop timeout seconds (default: 10)")
    acc_stop.add_argument("--json", action="store_true", dest="json_output", help="Emit JSON only on stdout")

    acc_logs = account_commands.add_parser("logs", help="Tail daemon logs for an account")
    acc_logs.add_argument("name", help="Account name")
    acc_logs.add_argument("-n", "--lines", type=_positive_int, default=50, help="Number of log lines to show (default: 50)")
    acc_logs.add_argument("--json", action="store_true", dest="json_output", help="Emit JSON only on stdout")

    commands.add_parser("tui", help="Run the optional interactive Textual frontend")
    commands.add_parser("telegram-controller", help="Run the independent Telegram remote controller")

    recipients = commands.add_parser("recipients", help="Inspect or discover DM recipients")
    recipient_commands = recipients.add_subparsers(dest="recipient_command", required=True)
    for name, help_text in (
        ("list", "List configured recipients without launching a browser"),
        ("scan", "Scan currently visible TikTok DM conversations"),
        ("resolve", "Resolve configured display names to TikTok usernames"),
        ("refresh-streaks", "Refresh streak status from TikTok Android UI for configured recipients (read-only)"),
    ):
        child = recipient_commands.add_parser(name, help=help_text)
        child.add_argument("--json", action="store_true", dest="json_output", help="Emit JSON only on stdout")
        if name == "refresh-streaks":
            child.add_argument("--device", dest="device_id", default=None, help="Target Android device serial/ID")
        if name == "list":
            child.add_argument("--show-streaks", action="store_true", help="Display cached TikTok streak status")


    videos = commands.add_parser("videos", help="Inspect configured video links")
    video_commands = videos.add_subparsers(dest="video_command", required=True)
    video_list = video_commands.add_parser("list", help="List configured videos")
    video_list.add_argument("--json", action="store_true", dest="json_output", help="Emit JSON only on stdout")
    return parser


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a documented process exit status."""

    return run_cli(argv)


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    sender_factory: SenderFactory | None = None,
) -> int:
    """Testable CLI dispatcher; JSON-producing commands never log to stdout."""

    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    parser = build_parser()
    arguments = parser.parse_args(argv)
    _configure_logging(bool(arguments.verbose), stderr)

    account_name = getattr(arguments, "account", None)
    if account_name:
        from daily_streak.accounts import AccountManager

        raw_config_path = Path(arguments.config)
        acc_mgr = AccountManager(base_dir=raw_config_path.parent, default_config_path=raw_config_path)
        if arguments.config == str(DEFAULT_CONFIG_PATH):
            try:
                acc = acc_mgr.get_account(account_name)
                config_path = acc.config_path
            except ValueError:
                config_path = acc_mgr.accounts_dir / account_name / "config.json"
        else:
            config_path = Path(arguments.config)
    else:
        config_path = Path(arguments.config)

    def _load_and_tag_config(path: Path) -> dict[str, Any]:
        cfg = load_config(path)
        if account_name and "account_name" not in cfg:
            cfg["account_name"] = account_name
        return cfg

    try:
        if arguments.command == "doctor":
            return _run_doctor(config_path, bool(arguments.json_output), stdout)
        if arguments.command == "status":
            config = _load_and_tag_config(config_path)
            payload = status_payload(config)
            _write_payload(payload, bool(arguments.json_output), stdout, _format_status)
            return EXIT_OK
        if arguments.command == "history":
            config = _load_and_tag_config(config_path)
            records = RunHistoryStore(get_data_dir(config, account_name=account_name)).load(arguments.limit)
            payload = {"history": records}
            _write_payload(payload, bool(arguments.json_output), stdout, _format_history)
            return EXIT_OK
        if arguments.command == "videos":
            config = _load_and_tag_config(config_path)
            videos = configured_videos(config)
            _write_payload({"videos": videos}, bool(arguments.json_output), stdout, _format_videos)
            return EXIT_OK
        if arguments.command == "recipients":
            config = _load_and_tag_config(config_path)
            show_streaks = bool(getattr(arguments, "show_streaks", False))
            if arguments.recipient_command == "list":
                recipients = configured_recipients(config)
            elif arguments.recipient_command == "refresh-streaks":
                device_id = getattr(arguments, "device_id", None)
                recipients = asyncio.run(_run_recipient_action(config, arguments.recipient_command, device_id=device_id))
                show_streaks = True
            else:
                recipients = asyncio.run(_run_recipient_action(config, arguments.recipient_command))

            if show_streaks and arguments.recipient_command != "refresh-streaks":
                from daily_streak.streak_reader import StreakCache, format_streak_display

                cache = StreakCache(get_data_dir(config, account_name=account_name))
                enriched: list[dict[str, Any]] = []
                for item in recipients:
                    row = dict(item)
                    username = str(row.get("username") or "")
                    record = cache.get(username) if username else None
                    row["streak"] = record.to_dict() if record else None
                    row["streak_display"] = format_streak_display(record)
                    enriched.append(row)
                recipients = enriched

            payload = {"recipients": recipients, "config_modified": False, "show_streaks": show_streaks}
            _write_payload(payload, bool(arguments.json_output), stdout, _format_recipients)
            return EXIT_OK

        if arguments.command == "send":
            config = _load_and_tag_config(config_path)
            summary = asyncio.run(_run_send(config, bool(arguments.force), sender_factory))
            _write_payload(summary_to_payload(summary), bool(arguments.json_output), stdout, _format_summary)
            if summary.setup_error:
                return EXIT_RUNTIME
            return EXIT_RECIPIENT_FAILURES if (summary.failed_count or summary.unconfirmed_count) else EXIT_OK
        if arguments.command == "daemon":
            if getattr(arguments, "status", False):
                config = _load_and_tag_config(config_path)
                from daily_streak.daemon import DaemonManager

                mgr = DaemonManager(config_path, account_name=account_name)
                status = mgr.get_status()
                payload = {
                    "status": status["status"],
                    "pid": status["pid"],
                    "schedule_time": status["schedule_time"],
                    "timezone": status["timezone"],
                    "next_run": status["next_run"],
                    "run_on_start": status["run_on_start"],
                    "last_run": status["last_run"],
                }
                if account_name:
                    payload["account_name"] = account_name
                _write_payload(payload, bool(arguments.json_output), stdout, _format_daemon_status)
                return EXIT_OK
            if getattr(arguments, "stop", False):
                from daily_streak.daemon import DaemonManager

                mgr = DaemonManager(config_path, account_name=account_name)
                stopped = mgr.stop()
                if stopped:
                    print("Daemon stopped.", file=stdout)
                    return EXIT_OK
                print("Stop signal sent; daemon may be finishing an in-progress batch.", file=stdout)
                return EXIT_OK
            return asyncio.run(_run_daemon(config_path, sender_factory, account_name=account_name))
        if arguments.command == "accounts":
            from daily_streak.accounts import AccountManager

            mgr = AccountManager(base_dir=config_path.parent, default_config_path=config_path)
            if arguments.account_command == "list":
                accounts = mgr.list_accounts(include_default=True)
                payload = {
                    "accounts": [
                        {
                            "name": acc.name,
                            "config_path": str(acc.config_path),
                            "data_dir": str(acc.data_dir),
                            "cookie_file": str(acc.cookie_file) if acc.cookie_file else None,
                            "schedule_time": acc.schedule_time,
                            "schedule_timezone": acc.schedule_timezone,
                            "enabled": acc.enabled,
                        }
                        for acc in accounts
                    ]
                }
                _write_payload(payload, bool(arguments.json_output), stdout, _format_accounts)
                return EXIT_OK
            if arguments.account_command == "create":
                acc = mgr.create_account(
                    arguments.name,
                    copy_from=arguments.copy_from,
                    cookie_file=arguments.cookie_file,
                    schedule_time=arguments.schedule_time,
                    schedule_timezone=arguments.schedule_timezone,
                    run_on_start=arguments.run_on_start,
                )
                payload = {
                    "created": True,
                    "account": {
                        "name": acc.name,
                        "config_path": str(acc.config_path),
                        "data_dir": str(acc.data_dir),
                        "cookie_file": str(acc.cookie_file) if acc.cookie_file else None,
                    },
                }
                if bool(arguments.json_output):
                    _write_payload(payload, True, stdout, lambda _: "")
                else:
                    print(f"Created account '{acc.name}' at {acc.config_path}", file=stdout)
                return EXIT_OK
            if arguments.account_command == "status":
                if arguments.name:
                    status = mgr.get_status(arguments.name)
                    statuses = [status]
                else:
                    statuses = mgr.get_all_statuses()
                conflicts = mgr.check_conflicts()
                payload = {"statuses": statuses, "conflicts": conflicts}
                _write_payload(payload, bool(arguments.json_output), stdout, _format_account_status)
                return EXIT_OK
            if arguments.account_command == "start":
                pid = mgr.start_daemon(arguments.name)
                payload = {"started": True, "account": arguments.name, "pid": pid}
                if bool(arguments.json_output):
                    _write_payload(payload, True, stdout, lambda _: "")
                else:
                    print(f"Daemon for account '{arguments.name}' started with PID {pid}.", file=stdout)
                return EXIT_OK
            if arguments.account_command == "stop":
                stopped = mgr.stop_daemon(arguments.name, timeout=getattr(arguments, "timeout", 10.0))
                payload = {"stopped": stopped, "account": arguments.name}
                if bool(arguments.json_output):
                    _write_payload(payload, True, stdout, lambda _: "")
                else:
                    if stopped:
                        print(f"Daemon for account '{arguments.name}' stopped.", file=stdout)
                    else:
                        print(f"Stop signal sent to account '{arguments.name}'; daemon may be finishing an in-progress batch.", file=stdout)
                return EXIT_OK
            if arguments.account_command == "logs":
                lines = mgr.read_logs(arguments.name, max_lines=arguments.lines)
                payload = {"account": arguments.name, "logs": lines}
                if bool(arguments.json_output):
                    _write_payload(payload, True, stdout, lambda _: "")
                else:
                    for line in lines:
                        print(line, file=stdout)
                return EXIT_OK
        if arguments.command == "tui":
            # Validate configuration before starting terminal takeover, but
            # defer importing the optional TUI package until this command.
            load_config(config_path)
            from daily_streak.tui import TuiUnavailableError, run_tui

            try:
                if account_name:
                    return run_tui(config_path, account_name=account_name)
                return run_tui(config_path)
            except TuiUnavailableError as exc:
                _write_error(stderr, str(exc))
                return EXIT_RUNTIME
        if arguments.command == "telegram-controller":
            return asyncio.run(_run_telegram_controller(config_path, stderr, sender_factory))
    except FileNotFoundError as exc:
        _write_error(stderr, f"Configuration error: {exc}")
        return EXIT_CONFIGURATION
    except ValueError as exc:
        _write_error(stderr, f"Configuration error: {exc}")
        return EXIT_CONFIGURATION
    except (KeyboardInterrupt, asyncio.CancelledError):
        _write_error(stderr, "Interrupted.")
        return EXIT_INTERRUPTED
    except Exception as exc:
        from daily_streak.android_streak_reader import (
            AdbDeviceNotFoundError,
            AdbMultipleDevicesError,
            AdbUnavailableError,
        )
        from daily_streak.api_streak_reader import (
            ApiStreakAuthError,
            ApiStreakError,
            ApiStreakNetworkError,
            ApiStreakParseError,
        )

        if isinstance(exc, ApiStreakAuthError):
            _write_error(
                stderr,
                f"Lỗi xác thực TikTok API: {exc}\n"
                "Vui lòng kiểm tra file cookies.json hoặc làm mới cookie phiên đăng nhập TikTok.",
            )
            return EXIT_RUNTIME
        if isinstance(exc, ApiStreakNetworkError):
            _write_error(
                stderr,
                f"Lỗi mạng TikTok API: {exc}\n"
                "Không thể kết nối đến máy chủ TikTok streak. Vui lòng kiểm tra kết nối mạng.",
            )
            return EXIT_RUNTIME
        if isinstance(exc, ApiStreakParseError):
            _write_error(
                stderr,
                f"Lỗi phân tích TikTok API: {exc}\n"
                "Phản hồi từ máy chủ TikTok không hợp lệ hoặc cấu trúc dữ liệu đã thay đổi.",
            )
            return EXIT_RUNTIME
        if isinstance(exc, ApiStreakError):
            _write_error(stderr, f"Lỗi TikTok API streak reader: {exc}")
            return EXIT_RUNTIME

        if isinstance(exc, AdbUnavailableError):
            _write_error(
                stderr,
                f"Lỗi ADB: {exc}\n"
                "Vui lòng cài đặt Android Debug Bridge ('adb') và đảm bảo lệnh có trong PATH.",
            )
            return EXIT_RUNTIME
        if isinstance(exc, AdbDeviceNotFoundError):
            _write_error(
                stderr,
                f"Lỗi thiết bị Android: {exc}\n"
                "Vui lòng cắm điện thoại qua USB, bật USB Debugging, hoặc cấu hình 'android.device_id'.",
            )
            return EXIT_RUNTIME
        if isinstance(exc, AdbMultipleDevicesError):
            _write_error(
                stderr,
                f"Nhiều thiết bị Android: {exc}\n"
                "Vui lòng truyền cờ '--device <device_id>' hoặc cấu hình 'android.device_id' trong config.json.",
            )
            return EXIT_RUNTIME

        logger.exception("Command failed")
        _write_error(stderr, f"Runtime error: {exc}")
        return EXIT_RUNTIME
    raise AssertionError("Unhandled CLI command")


def status_payload(config: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, object]:
    """Build the browser-free status representation used by human and JSON output."""

    schedule = schedule_from_config(config)
    data_dir = get_data_dir(config)
    current = now or datetime.now(timezone.utc)
    local_now = current.astimezone(schedule.timezone) if current.tzinfo else current.replace(tzinfo=timezone.utc).astimezone(schedule.timezone)
    daily_state = DailyStateStore(data_dir).load(local_now.date().isoformat(), str(schedule.timezone))
    recipients = configured_recipients(config)
    completed = sum(
        1
        for recipient in recipients
        if isinstance(recipient.get("username"), str)
        and daily_state.is_completed(recipient["username"])
    )
    if not completed:
        state = "idle"
    elif recipients and completed >= len(recipients):
        state = "complete"
    else:
        state = "in progress"
    history = RunHistoryStore(data_dir).load(limit=1)
    raw_tiktok = config.get("tiktok", {})
    tiktok_config = raw_tiktok if isinstance(raw_tiktok, Mapping) else {}

    from daily_streak.daemon import DaemonManager
    daemon_mgr = DaemonManager(config_path=DEFAULT_CONFIG_PATH, config_loader=lambda _: dict(config))
    daemon_status = daemon_mgr.get_status()

    return {
        "name": "Daily Streak Tool v2",
        "state": state,
        "recipients": len(recipients),
        "completed_today": completed,
        "videos": len(configured_videos(config)),
        "last_run": history[-1] if history else None,
        "schedule": {
            "time": schedule.time_text,
            "timezone": str(schedule.timezone),
            "next_run": next_run(schedule, local_now).isoformat(),
        },
        "daemon": {"status": daemon_status["status"], "pid": daemon_status["pid"]},
        "browser": {"headless": bool(tiktok_config.get("headless", False))},
        "data_dir": str(data_dir),
    }


async def _run_send(
    config: Mapping[str, Any],
    force: bool,
    sender_factory: SenderFactory | None,
) -> RunSummary:
    if sender_factory is None:
        from daily_streak.daemon import create_sender

        sender_factory = create_sender
    sender = sender_factory(config)
    return await sender.send_daily_links(force=force)


async def _run_daemon(
    config_path: Path,
    sender_factory: SenderFactory | None,
    *,
    account_name: str | None = None,
) -> int:
    from daily_streak.daemon import DaemonRunner
    from daily_streak.lock import DaemonLockedError

    runner = DaemonRunner(
        config_path,
        account_name=account_name,
        sender_factory=sender_factory or _default_sender_factory(),
    )
    try:
        await runner.run_forever()
    except DaemonLockedError as exc:
        logger.error(str(exc))
        return EXIT_RUNTIME
    return EXIT_OK


async def _run_telegram_controller(
    config_path: Path,
    stderr: TextIO,
    sender_factory: SenderFactory | None,
) -> int:
    config = load_config(config_path)
    raw_telegram = config.get("telegram", {})
    telegram_cfg: Mapping[str, Any] = (
        raw_telegram if isinstance(raw_telegram, Mapping) else {}
    )

    if not bool(telegram_cfg.get("enabled")):
        _write_error(
            stderr,
            "Configuration error: 'telegram.enabled' is false in config.json.\n"
            "Set 'telegram.enabled: true' to run the Telegram remote controller.",
        )
        return EXIT_CONFIGURATION

    if not telegram_cfg.get("bot_token") or not telegram_cfg.get("chat_id"):
        _write_error(
            stderr,
            "Configuration error: 'telegram.bot_token' and 'telegram.chat_id' must be configured in config.json.",
        )
        return EXIT_CONFIGURATION

    if not bool(telegram_cfg.get("commands_enabled")):
        _write_error(
            stderr,
            "Configuration error: Telegram remote commands are disabled.\n"
            "Set 'telegram.commands_enabled: true' in config.json to enable remote control.",
        )
        return EXIT_CONFIGURATION

    try:
        import telegram
        import telegram.ext
    except ImportError:
        _write_error(
            stderr,
            "Error: python-telegram-bot is required to run the Telegram remote controller.\n"
            "Install the telegram extra using:\n"
            "  pip install 'daily-streak-tool[telegram]'\n"
            "or (from wheel):\n"
            "  pip install '<wheel-path>[telegram]'",
        )
        return EXIT_RUNTIME

    from daily_streak.telegram_controller import TelegramController

    controller = TelegramController(
        config_path,
        sender_factory=sender_factory or _default_sender_factory(),
    )
    await controller.run_forever()
    return EXIT_OK


def _default_sender_factory() -> SenderFactory:
    from daily_streak.daemon import create_sender

    return create_sender


async def _run_recipient_action(
    config: Mapping[str, Any],
    action: str,
    *,
    device_id: str | None = None,
) -> list[dict[str, str]]:
    """Delegate recipient browser or Android work to the shared adapter."""

    from daily_streak.recipient_actions import refresh_streaks, resolve_recipients, scan_recipients

    if action == "scan":
        return await scan_recipients(config)
    if action == "resolve":
        return await resolve_recipients(config, configured_recipients(config))
    if action == "refresh-streaks":
        return await refresh_streaks(config, configured_recipients(config), force=True, device_id=device_id)
    raise ValueError(f"Unknown recipient operation: {action}")



def _run_doctor(config_path: Path, json_output: bool, stdout: TextIO) -> int:
    report, _ = collect_doctor_report(config_path)
    _write_payload(report.to_dict(), json_output, stdout, _format_doctor)
    if any(
        check.status is CheckStatus.FAIL and check.name in {"config", "schedule"}
        for check in report.checks
    ):
        return EXIT_CONFIGURATION
    return EXIT_RUNTIME if report.fail_count else EXIT_OK


def configured_recipients(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_recipients = config.get("recipients", [])
    if not isinstance(raw_recipients, list):
        return []
    recipients: list[dict[str, Any]] = []
    for item in raw_recipients:
        if not isinstance(item, Mapping):
            continue
        entry = dict(item)
        entry["name"] = str(item.get("name") or "")
        entry["username"] = str(item.get("username") or "")
        recipients.append(entry)
    return recipients


def configured_videos(config: Mapping[str, Any]) -> list[str]:
    raw_videos = config.get("videos", [])
    return [video.strip() for video in raw_videos if isinstance(video, str) and video.strip()] if isinstance(raw_videos, list) else []


def summary_to_payload(summary: RunSummary) -> dict[str, object]:
    return {
        "started_at": summary.started_at.isoformat(),
        "finished_at": summary.finished_at.isoformat() if summary.finished_at else None,
        "elapsed_seconds": summary.elapsed_seconds,
        "success": summary.success_count,
        "failed": summary.failed_count,
        "unconfirmed": summary.unconfirmed_count,
        "skipped": summary.skipped_count,
        "setup_error": summary.setup_error,
        "results": [result.to_dict() for result in summary.results],
    }


def _write_payload(
    payload: Mapping[str, object],
    json_output: bool,
    stdout: TextIO,
    formatter: Callable[[Mapping[str, object]], str],
) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False), file=stdout)
    else:
        print(formatter(payload), file=stdout)


def _format_status(payload: Mapping[str, object]) -> str:
    schedule = payload["schedule"]
    assert isinstance(schedule, Mapping)
    browser = payload["browser"]
    assert isinstance(browser, Mapping)
    last_run = payload.get("last_run")
    if isinstance(last_run, Mapping):
        unconfirmed_text = f", {last_run.get('unconfirmed', 0)} unconfirmed" if last_run.get("unconfirmed") else ""
        last_text = f"{last_run.get('success', 0)} success, {last_run.get('failed', 0)} failed{unconfirmed_text}, {last_run.get('skipped', 0)} skipped"
    else:
        last_text = "none"
    browser_text = "headless" if browser.get("headless") else "headed"
    return "\n".join(
        (
            str(payload["name"]),
            "",
            f"State         {payload['state']}",
            f"Recipients    {payload['recipients']}",
            f"Completed     {payload['completed_today']} today",
            f"Videos        {payload['videos']}",
            f"Last run      {last_text}",
            f"Schedule      {schedule['time']} {schedule['timezone']}",
            f"Next run      {schedule['next_run']}",
            f"Daemon        {_daemon_text(payload.get('daemon'))}",
            f"Browser       {browser_text}",
            f"Data dir      {payload['data_dir']}",
        )
    )


def _daemon_text(daemon_info: object) -> str:
    if isinstance(daemon_info, Mapping):
        status = str(daemon_info.get("status", "Stopped"))
        pid = daemon_info.get("pid")
        return f"{status} (PID {pid})" if pid else status
    return "Stopped"


def _format_daemon_status(payload: Mapping[str, object]) -> str:
    status_text = f"Status:       {payload.get('status')}"
    if payload.get("pid"):
        status_text += f" (PID {payload.get('pid')})"
    lines = [
        "Daily Streak Tool Daemon",
        "",
        status_text,
        f"Schedule:     {payload.get('schedule_time')} ({payload.get('timezone')})",
        f"Next run:     {payload.get('next_run')}",
        f"Run on start: {'Yes' if payload.get('run_on_start') else 'No'}",
    ]
    last_run = payload.get("last_run")
    if isinstance(last_run, Mapping):
        lines.append(
            f"Last batch:   {last_run.get('success', 0)} success, "
            f"{last_run.get('failed', 0)} failed, {last_run.get('unconfirmed', 0)} unconfirmed, "
            f"{last_run.get('skipped', 0)} skipped"
        )
    return "\n".join(lines)


def _format_summary(payload: Mapping[str, object]) -> str:
    elapsed = payload.get("elapsed_seconds")
    elapsed_text = f"{float(elapsed):.1f}s" if elapsed is not None else "unknown duration"
    lines = [
        f"Finished in {elapsed_text}",
        f"Success    : {payload['success']}",
        f"Failed     : {payload['failed']}",
        f"Unconfirmed: {payload.get('unconfirmed', 0)}",
        f"Skipped    : {payload['skipped']}",
    ]
    if payload.get("setup_error"):
        lines.append(f"Setup error: {payload['setup_error']}")
    return "\n".join(lines)


def _format_history(payload: Mapping[str, object]) -> str:
    records = payload.get("history")
    if not isinstance(records, list) or not records:
        return "No run history."
    lines = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        lines.append(
            f"{record.get('finished_at') or record.get('started_at')}  "
            f"{record.get('success', 0)} success, {record.get('failed', 0)} failed, "
            f"{record.get('unconfirmed', 0)} unconfirmed, "
            f"{record.get('skipped', 0)} skipped"
        )
    return "\n".join(lines) or "No run history."


def _format_doctor(payload: Mapping[str, object]) -> str:
    checks = payload.get("checks")
    lines = ["Daily Streak Tool doctor", ""]
    if isinstance(checks, list):
        for check in checks:
            if isinstance(check, Mapping):
                lines.append(
                    f"{str(check.get('status', 'warn')).upper():4} "
                    f"{check.get('name')}: {check.get('message')}"
                )
    summary = payload.get("summary")
    if isinstance(summary, Mapping):
        lines.extend(
            (
                "",
                f"Summary: {summary.get('pass', 0)} pass, {summary.get('warn', 0)} warn, {summary.get('fail', 0)} fail",
            )
        )
    return "\n".join(lines)


def _format_recipients(payload: Mapping[str, object]) -> str:
    recipients = payload.get("recipients")
    if not isinstance(recipients, list) or not recipients:
        return "No recipients configured or found."
    lines = []
    show_streaks = bool(payload.get("show_streaks"))
    for recipient in recipients:
        if not isinstance(recipient, Mapping):
            continue
        name = recipient.get("name") or "(unnamed)"
        username = recipient.get("username") or "unresolved"
        base_line = f"{name}  @{username}" if username != "unresolved" else f"{name}  (unresolved)"
        if show_streaks:
            streak_disp = str(recipient.get("streak_display") or "Chưa xác định")
            lines.append(f"{base_line}  [Streak: {streak_disp}]")
        else:
            lines.append(base_line)
    lines.append("Configuration was not modified.")
    return "\n".join(lines)



def _format_videos(payload: Mapping[str, object]) -> str:
    videos = payload.get("videos")
    if not isinstance(videos, list) or not videos:
        return "No videos configured."
    return "\n".join(f"{index}. {video}" for index, video in enumerate(videos, start=1))


def _format_accounts(payload: Mapping[str, object]) -> str:
    accounts = payload.get("accounts", [])
    if not isinstance(accounts, list) or not accounts:
        return "No accounts configured."
    lines = ["Configured Accounts:", ""]
    for acc in accounts:
        if isinstance(acc, Mapping):
            name = acc.get("name", "unknown")
            cfg = acc.get("config_path", "")
            cookie = acc.get("cookie_file") or "none"
            lines.append(f"- {name} (config: {cfg}, cookies: {cookie})")
    return "\n".join(lines)


def _format_account_status(payload: Mapping[str, object]) -> str:
    statuses = payload.get("statuses", [])
    conflicts = payload.get("conflicts", [])
    lines = ["Multi-Account Daemon Status:", ""]
    if isinstance(statuses, list):
        for st in statuses:
            if isinstance(st, Mapping):
                acc_name = st.get("account_name", "unknown")
                status = st.get("status", "Stopped")
                pid = st.get("pid")
                pid_text = f" (PID {pid})" if pid else ""
                sched = f"{st.get('schedule_time')} ({st.get('timezone')})"
                next_r = st.get("next_run", "N/A")
                lines.append(f"Account: {acc_name}")
                lines.append(f"  Status:    {status}{pid_text}")
                lines.append(f"  Schedule:  {sched}")
                lines.append(f"  Next run:  {next_r}")
                last_run = st.get("last_run")
                if isinstance(last_run, Mapping):
                    lines.append(
                        f"  Last run:  {last_run.get('success', 0)} success, "
                        f"{last_run.get('failed', 0)} failed, {last_run.get('unconfirmed', 0)} unconfirmed"
                    )
                lines.append("")
    if isinstance(conflicts, list) and conflicts:
        lines.append("Warnings / Conflicts:")
        for cf in conflicts:
            if isinstance(cf, Mapping):
                lines.append(f"  ! {cf.get('message')}")
    return "\n".join(lines).rstrip()


def _configure_logging(verbose: bool, stderr: TextIO) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=stderr,
        force=True,
    )


def _write_error(stderr: TextIO, message: str) -> None:
    print(message, file=stderr)


if __name__ == "__main__":
    sys.exit(main())
