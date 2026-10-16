"""Safe deployment and maintenance guards for Daily Streak Tool runbooks."""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
from typing import Any, Mapping

from daily_streak.config import load_config
from daily_streak.paths import get_data_dir


logger = logging.getLogger(__name__)


class GuardError(RuntimeError):
    """Raised when a deployment safety check fails."""


def probe_sender_lock(
    config_path: Path | str,
    *,
    data_dir_override: Path | None = None,
) -> dict[str, Any]:
    """Inspect send.lock using fail-closed semantics without acquiring or modifying the lock.

    Returns a status dict:
      {"safe": True, "reason": "UNLOCKED" | "STALE_INACTIVE", "metadata": ...}
    Raises GuardError on any active lock, hostname mismatch, corrupt metadata, or unverified PID.
    """
    config_file = Path(config_path).expanduser()
    try:
        config = load_config(config_file)
    except Exception as exc:
        raise GuardError(f"Cannot load config from {config_file}: {exc}") from exc

    data_dir = data_dir_override or get_data_dir(config)
    lock_path = Path(data_dir) / "send.lock"

    if not lock_path.exists():
        return {"safe": True, "reason": "UNLOCKED", "metadata": {}}

    # File exists -> Must parse metadata safely without acquire/release/unlink
    try:
        content = lock_path.read_text(encoding="utf-8")
        meta = json.loads(content)
    except Exception as exc:
        raise GuardError(
            f"send.lock exists at {lock_path} but JSON content is invalid or corrupt: {exc}"
        ) from exc

    if not isinstance(meta, dict):
        raise GuardError(f"send.lock at {lock_path} is not a valid JSON dictionary")

    # Hostname verification: Missing or mismatching hostname must FAIL CLOSED
    lock_hostname = meta.get("hostname")
    local_hostname = socket.gethostname()
    if not lock_hostname or not isinstance(lock_hostname, str):
        raise GuardError(
            f"send.lock at {lock_path} is missing 'hostname' field in metadata. Fail closed."
        )

    if lock_hostname.strip() != local_hostname.strip():
        raise GuardError(
            f"send.lock at {lock_path} belongs to host {lock_hostname!r}, differing from local host {local_hostname!r}. Fail closed."
        )

    # PID verification: Must be a positive integer
    pid = meta.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        raise GuardError(
            f"send.lock at {lock_path} contains invalid or missing PID: {pid!r}. Fail closed."
        )

    # Check process liveness
    try:
        os.kill(pid, 0)
        # If no error, process is alive
        raise GuardError(
            f"Active sender batch is running under PID {pid} on host {local_hostname}! Deployment must halt."
        )
    except ProcessLookupError:
        # Process is dead -> Stale lock from a dead process. Safe to proceed.
        return {
            "safe": True,
            "reason": "STALE_INACTIVE",
            "metadata": meta,
            "pid": pid,
        }
    except PermissionError:
        # Process exists but owned by another user -> Still alive! Fail closed.
        raise GuardError(
            f"Sender process PID {pid} exists (PermissionError). Cannot verify termination. Fail closed."
        )


def check_run_on_start(
    config_path: Path | str,
    *,
    allow_run_on_start: bool = False,
) -> dict[str, Any]:
    """Verify schedule.run_on_start in configuration with strict fail-closed behavior.

    - Only strict bool is accepted (e.g. True or False). Strings, ints, or other types FAIL CLOSED.
    - If schedule.run_on_start is True and allow_run_on_start is False, raises GuardError.
    """
    config_file = Path(config_path).expanduser()
    try:
        config = load_config(config_file)
    except Exception as exc:
        raise GuardError(f"Cannot load config from {config_file}: {exc}") from exc

    raw_schedule = config.get("schedule")
    if not isinstance(raw_schedule, Mapping):
        raise GuardError(f"Invalid configuration: 'schedule' section must be an object at {config_file}")

    if "run_on_start" in raw_schedule:
        run_on_start = raw_schedule["run_on_start"]
        # Strict boolean type check (in Python, bool is a subclass of int, so type(x) is bool)
        if type(run_on_start) is not bool:
            raise GuardError(
                f"Invalid configuration: 'schedule.run_on_start' must be a strict boolean (got {type(run_on_start).__name__}: {run_on_start!r}). Fail closed."
            )
    else:
        run_on_start = False

    if run_on_start is True and not allow_run_on_start:
        raise GuardError(
            f"GUARD BLOCKED: 'schedule.run_on_start' is set to True in {config_file}.\n"
            f"Starting the daemon service will trigger an immediate TikTok send batch.\n"
            f"To prevent unintended sends during deployment, edit config.json and set 'run_on_start': false.\n"
            f"If an immediate send is explicitly intended, pass --allow-run-on-start."
        )

    return {"run_on_start": run_on_start, "safe": True}


def verify_daemon_stopped(
    config_path: Path | str,
    *,
    streak_bin: Path | str | None = None,
    data_dir_override: Path | None = None,
    runner: Any | None = None,
) -> dict[str, Any]:
    """Verify that the background daemon has cleanly stopped using multi-source cross-checking.

    Sources:
    1. CLI status JSON (`streak daemon --status --json`) called with shell=False
    2. daemon.lock metadata validation (JSON structure, hostname, PID)
    3. Process liveness check on all reported PIDs
    4. Confirms no conflicting states.
    """
    config_file = Path(config_path).expanduser()
    try:
        config = load_config(config_file)
    except Exception as exc:
        raise GuardError(f"Cannot load config from {config_file}: {exc}") from exc

    data_dir = data_dir_override or get_data_dir(config)
    resolved_bin = Path(streak_bin).expanduser() if streak_bin else Path(sys.executable).parent / "streak"

    # Source 1: Check CLI status via subprocess (shell=False)
    run_fn = runner or subprocess.run
    if runner is not None or resolved_bin.exists():
        proc = run_fn(
            [str(resolved_bin), "--config", str(config_file), "daemon", "--status", "--json"],
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )
        if proc.returncode != 0:
            raise GuardError(
                f"CLI command '{resolved_bin} daemon --status --json' failed (exit {proc.returncode}):\n{proc.stderr.strip()}"
            )
        try:
            cli_data = json.loads(proc.stdout)
        except Exception as exc:
            raise GuardError(
                f"CLI emitted invalid JSON on stdout: {exc}\nRaw output: {proc.stdout.strip()}"
            ) from exc
    else:
        # Fallback to direct DaemonManager if streak_bin is not directly runnable in dev/test
        from daily_streak.daemon import DaemonManager

        mgr = DaemonManager(config_file)
        cli_data = mgr.get_status()

    cli_status = cli_data.get("status")
    cli_pid = cli_data.get("pid")

    # Source 2: Check daemon.lock metadata directly with strict validation
    dlock_path = Path(data_dir) / "daemon.lock"
    dlock_pid = None
    if dlock_path.exists():
        try:
            content = dlock_path.read_text(encoding="utf-8")
            meta = json.loads(content)
        except Exception as exc:
            raise GuardError(
                f"daemon.lock exists at {dlock_path} but JSON is corrupt or unreadable: {exc}. Fail closed."
            ) from exc

        if not isinstance(meta, dict):
            raise GuardError(f"daemon.lock at {dlock_path} is not a valid JSON dictionary. Fail closed.")

        lock_hostname = meta.get("hostname")
        local_hostname = socket.gethostname()
        if not lock_hostname or not isinstance(lock_hostname, str):
            raise GuardError(f"daemon.lock at {dlock_path} is missing 'hostname' field. Fail closed.")

        if lock_hostname.strip() != local_hostname.strip():
            raise GuardError(
                f"daemon.lock at {dlock_path} belongs to host {lock_hostname!r}, differing from local host {local_hostname!r}. Fail closed."
            )

        dlock_pid = meta.get("pid")
        if not isinstance(dlock_pid, int) or dlock_pid <= 0:
            raise GuardError(
                f"daemon.lock at {dlock_path} contains invalid or missing PID: {dlock_pid!r}. Fail closed."
            )

    # Source 3: Process liveness of any reported PID
    live_pids = []
    for test_pid in {cli_pid, dlock_pid}:
        if isinstance(test_pid, int) and test_pid > 0:
            try:
                os.kill(test_pid, 0)
                live_pids.append(test_pid)
            except ProcessLookupError:
                pass
            except PermissionError:
                live_pids.append(test_pid)

    # Multi-source validation:
    # 1. cli_status must be "Stopped"
    # 2. cli_pid must be None
    # 3. No daemon PIDs may be alive
    if cli_status != "Stopped":
        raise GuardError(
            f"Daemon verification failed: CLI reports status {cli_status!r} (expected 'Stopped')"
        )

    if cli_pid is not None:
        raise GuardError(
            f"Daemon verification failed: CLI still reports active PID {cli_pid}"
        )

    if live_pids:
        raise GuardError(
            f"Daemon verification failed: Lingering live daemon process detected (PID {live_pids})! Process must terminate."
        )

    return {
        "stopped": True,
        "cli_status": cli_status,
        "cli_pid": cli_pid,
        "dlock_pid": dlock_pid,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for runbook guard operations."""
    parser = argparse.ArgumentParser(description="Daily Streak Tool Runbook Deployment Guards")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: probe-sender-lock
    probe_parser = subparsers.add_parser("probe-sender-lock", help="Probe send.lock with fail-closed semantics")
    probe_parser.add_argument("--config", required=True, help="Path to config.json")

    # Subcommand: check-run-on-start
    ros_parser = subparsers.add_parser("check-run-on-start", help="Check schedule.run_on_start")
    ros_parser.add_argument("--config", required=True, help="Path to config.json")
    ros_parser.add_argument("--allow-run-on-start", action="store_true", help="Explicitly allow run_on_start=True")

    # Subcommand: verify-daemon-stopped
    stop_parser = subparsers.add_parser("verify-daemon-stopped", help="Verify daemon has cleanly stopped")
    stop_parser.add_argument("--config", required=True, help="Path to config.json")
    stop_parser.add_argument("--streak-bin", default=None, help="Path to streak executable")

    args = parser.parse_args(argv)

    try:
        if args.command == "probe-sender-lock":
            res = probe_sender_lock(args.config)
            print(f"PROBE_RESULT: {res['reason']}")
            return 0

        if args.command == "check-run-on-start":
            res = check_run_on_start(args.config, allow_run_on_start=args.allow_run_on_start)
            print(f"RUN_ON_START_SAFE: schedule.run_on_start={res['run_on_start']}")
            return 0

        if args.command == "verify-daemon-stopped":
            res = verify_daemon_stopped(args.config, streak_bin=args.streak_bin)
            print("DAEMON_STOP_VERIFIED: Daemon cleanly stopped without lingering processes.")
            return 0

    except GuardError as exc:
        print(f"GUARD_FAILURE: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"UNEXPECTED_ERROR: {exc}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
