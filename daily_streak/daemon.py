"""Foreground scheduler for server and supervisor-managed deployments."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Mapping

from daily_streak.config import DEFAULT_CONFIG_PATH, load_config
from daily_streak.history import RunHistoryStore
from daily_streak.lock import DaemonLock, DaemonLockedError, _process_is_alive
from daily_streak.models import RunSummary
from daily_streak.paths import get_data_dir
from daily_streak.schedule import Schedule, next_run, schedule_from_config


logger = logging.getLogger(__name__)

SenderFactory = Callable[[Mapping[str, Any]], Any]
ConfigLoader = Callable[[str | Path], dict[str, Any]]

DAEMON_LOG_FILENAME = "daemon.log"
DAEMON_STARTING_FILENAME = "daemon.starting"
DEFAULT_MAX_LOG_BYTES = 5 * 1024 * 1024  # 5 MB
DEFAULT_LOG_BACKUP_COUNT = 3


class DaemonProcessError(RuntimeError):
    """Raised when daemon process control operations fail."""


def create_sender(
    config: Mapping[str, Any],
    *,
    on_recipient_result: Callable[[Any, RunSummary], None] | None = None,
    **kwargs: Any,
) -> Any:
    """Construct the standard shared sender without importing GUI code."""

    from daily_streak.notifications import Notifier
    from daily_streak.sender import TikTokSender
    from daily_streak.videos import VideoPool

    return TikTokSender(
        config,
        Notifier(config),
        VideoPool(config),
        on_recipient_result=on_recipient_result,
        **kwargs,
    )


def _default_notifier_factory(config: Mapping[str, Any]) -> Any:
    from daily_streak.notifications import Notifier

    return Notifier(config)


class DaemonRunner:
    """Run scheduled sender batches in the foreground with fresh configuration."""

    def __init__(
        self,
        config_path: str | Path = DEFAULT_CONFIG_PATH,
        *,
        config_loader: ConfigLoader = load_config,
        sender_factory: SenderFactory = create_sender,
        lock: DaemonLock | None = None,
        notifier_factory: Callable[[Mapping[str, Any]], Any] | None = None,
        pause_store: Any | None = None,
    ) -> None:
        self.config_path = Path(config_path)
        self.config_loader = config_loader
        self.sender_factory = sender_factory
        self.lock = lock
        self.notifier_factory = notifier_factory
        self.pause_store = pause_store
        self._run_guard = asyncio.Lock()

    async def run_once(self, *, check_pause: bool = True) -> RunSummary | None:
        """Load fresh configuration and run one batch unless a batch is active."""

        if self._run_guard.locked():
            logger.warning("Skipping scheduled run because a prior run is still active.")
            return None

        async with self._run_guard:
            config = self.config_loader(self.config_path)
            if check_pause:
                data_dir = get_data_dir(config)
                store = self.pause_store
                if store is None:
                    from daily_streak.state import PauseStore
                    store = PauseStore(data_dir)
                if store.is_paused():
                    info = store.get_pause_info()
                    logger.info(
                        "Skipping scheduled run because daemon scheduler is paused (paused_at: %s, reason: %s)",
                        info.get("paused_at"),
                        info.get("reason"),
                    )
                    return None

            sender = self.sender_factory(config)
            summary = await sender.send_daily_links()
            logger.info(
                "Sender run completed: %d success, %d failed, %d unconfirmed, %d skipped",
                summary.success_count,
                summary.failed_count,
                summary.unconfirmed_count,
                summary.skipped_count,
            )

            if summary is not None:
                notifier = None
                if self.notifier_factory is not None:
                    notifier = self.notifier_factory(config)
                elif hasattr(sender, "notifier") and hasattr(sender.notifier, "batch_completion"):
                    notifier = sender.notifier
                else:
                    notifier = _default_notifier_factory(config)

                if notifier is not None and hasattr(notifier, "batch_completion"):
                    try:
                        await notifier.batch_completion(summary)
                    except Exception:
                        logger.exception("Failed to send batch completion notification")
            return summary

    async def run_forever(self, *, stop_event: asyncio.Event | None = None) -> None:
        """Schedule runs until SIGINT/SIGTERM or a supplied stop event occurs."""

        # APScheduler is intentionally imported only for daemon mode.  Offline
        # CLI inspection commands do not need it installed.
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger

        initial_config = self.config_loader(self.config_path)
        data_dir = get_data_dir(initial_config)
        daemon_lock = self.lock or DaemonLock(data_dir, config_path=self.config_path)
        daemon_lock.acquire()

        schedule = schedule_from_config(initial_config)
        scheduler = AsyncIOScheduler(timezone=schedule.timezone)
        job = scheduler.add_job(
            self.run_once,
            CronTrigger(
                hour=schedule.hour,
                minute=schedule.minute,
                timezone=schedule.timezone,
            ),
            id="daily_tiktok_streak_sender",
            replace_existing=True,
            max_instances=1,
            misfire_grace_time=None,
            coalesce=True,
        )
        scheduler.start()
        logger.info("Daemon started; scheduled daily run for %s %s", schedule.time_text, schedule.timezone)
        logger.info("Next scheduled run: %s", job.next_run_time)

        stop_event = stop_event or asyncio.Event()
        remove_handlers = _install_stop_handlers(stop_event)
        try:
            raw_schedule = initial_config.get("schedule", {})
            run_on_start = isinstance(raw_schedule, Mapping) and bool(
                raw_schedule.get("run_on_start")
            )
            if run_on_start:
                logger.info("Running sender immediately because schedule.run_on_start is enabled.")
                try:
                    await self.run_once()
                except Exception:
                    # An individual run must not take down a service that can
                    # recover on the next scheduled execution.
                    logger.exception("Initial sender run failed")
            await stop_event.wait()
            if self._run_guard.locked():
                logger.info("Waiting for the active sender run to finish before shutdown.")
                async with self._run_guard:
                    pass
        finally:
            scheduler.shutdown(wait=False)
            remove_handlers()
            daemon_lock.release()
            logger.info("Daemon stopped")


class DaemonManager:
    """Manage lifecycle, status, and logs of the detached background daemon."""

    _start_lock = threading.Lock()

    def __init__(
        self,
        config_path: str | Path = DEFAULT_CONFIG_PATH,
        *,
        config_loader: ConfigLoader = load_config,
        lock: DaemonLock | None = None,
        process_is_daemon: Callable[[int, str], bool] | None = None,
        now_provider: Callable[[], datetime] | None = None,
        popen_factory: Callable[..., Any] | None = None,
        startup_timeout: float = 3.0,
    ) -> None:
        self.config_path = Path(config_path)
        self.config_loader = config_loader
        self._lock = lock
        self._process_is_daemon = process_is_daemon
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._popen_factory = popen_factory or subprocess.Popen
        self.startup_timeout = max(0.1, float(startup_timeout))
        self._process: Any = None

    def _get_lock(self, data_dir: Path) -> DaemonLock:
        return self._lock or DaemonLock(
            data_dir,
            config_path=self.config_path,
            process_is_daemon=self._process_is_daemon,
        )

    def get_status(self) -> dict[str, Any]:
        """Return structured daemon status without blocking."""
        config = self.config_loader(self.config_path)
        data_dir = get_data_dir(config)
        lock = self._get_lock(data_dir)
        metadata = lock.read_metadata()
        pid = metadata.get("pid")
        is_running = False
        if isinstance(pid, int) and pid > 0:
            if lock.is_active():
                is_running = True
            else:
                pid = None

        schedule = schedule_from_config(config)
        now_dt = self._now_provider()
        local_now = (
            now_dt.astimezone(schedule.timezone)
            if now_dt.tzinfo
            else now_dt.replace(tzinfo=timezone.utc).astimezone(schedule.timezone)
        )
        next_run_dt = next_run(schedule, local_now)
        next_run_text = next_run_dt.strftime("%Y-%m-%d %H:%M:%S %Z")

        raw_schedule = config.get("schedule", {})
        run_on_start = isinstance(raw_schedule, Mapping) and bool(raw_schedule.get("run_on_start"))

        history_store = RunHistoryStore(data_dir)
        records = history_store.load(limit=1)
        last_run = records[0] if records else None

        log_path = data_dir / DAEMON_LOG_FILENAME

        from daily_streak.state import PauseStore
        pause_store = PauseStore(data_dir)
        pause_info = pause_store.get_pause_info()
        is_paused = bool(pause_info.get("paused"))

        status_text = "Running" if is_running else "Stopped"
        if is_running and is_paused:
            status_text = "Running (Paused)"
        elif not is_running and is_paused:
            status_text = "Stopped (Paused)"

        return {
            "status": status_text,
            "is_running": is_running,
            "is_paused": is_paused,
            "pause_info": pause_info,
            "pid": pid if is_running else None,
            "schedule_time": schedule.time_text,
            "timezone": str(schedule.timezone),
            "next_run": next_run_text,
            "run_on_start": run_on_start,
            "last_run": last_run,
            "log_path": log_path,
        }

    def start(self, startup_timeout: float | None = None) -> int:
        """Start the background daemon process detached, returning its PID."""
        if not self._start_lock.acquire(blocking=False):
            raise DaemonProcessError("Daemon startup is already in progress.")

        starting_file: Path | None = None
        try:
            status = self.get_status()
            if status["is_running"]:
                raise DaemonProcessError(f"Daemon is already running with PID {status['pid']}.")

            config = self.config_loader(self.config_path)
            data_dir = get_data_dir(config)
            data_dir.mkdir(parents=True, exist_ok=True)
            log_path = data_dir / DAEMON_LOG_FILENAME

            # Check cross-process startup sentinel
            starting_file = data_dir / DAEMON_STARTING_FILENAME
            if starting_file.exists():
                try:
                    starting_info = json.loads(starting_file.read_text(encoding="utf-8"))
                    starting_pid = starting_info.get("pid")
                    starting_time = float(starting_info.get("time", 0))
                    if (
                        time.time() - starting_time < 5.0
                        and isinstance(starting_pid, int)
                        and _process_is_alive(starting_pid)
                    ):
                        raise DaemonProcessError(
                            f"Daemon startup is already in progress (PID {starting_pid})."
                        )
                except DaemonProcessError:
                    raise
                except Exception:
                    pass
                starting_file.unlink(missing_ok=True)

            self.rotate_logs_if_needed(log_path)

            # Use absolute path to preserve venv symlink instead of resolving to system python
            sys_executable = Path(sys.executable).absolute()
            abs_config_path = self.config_path.resolve()
            project_root = Path(__file__).resolve().parent.parent

            cmd = [
                str(sys_executable),
                "-m",
                "daily_streak.cli",
                "--config",
                str(abs_config_path),
                "daemon",
            ]

            env = dict(os.environ)
            env["PYTHONUNBUFFERED"] = "1"
            pythonpath = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = (
                f"{project_root}:{pythonpath}" if pythonpath else str(project_root)
            )

            kwargs: dict[str, Any] = {
                "stdin": subprocess.DEVNULL,
                "cwd": str(project_root),
                "env": env,
            }
            if os.name == "posix":
                kwargs["start_new_session"] = True
            elif os.name == "nt":
                kwargs["creationflags"] = 0x00000008  # DETACHED_PROCESS

            logger.info(
                "Spawning detached daemon: executable=%s, args=%s, cwd=%s",
                sys_executable,
                cmd,
                project_root,
            )

            log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                kwargs["stdout"] = log_fd
                kwargs["stderr"] = subprocess.STDOUT
                process = self._popen_factory(cmd, **kwargs)
                self._process = process
            except Exception as exc:
                raise DaemonProcessError(f"Failed to spawn daemon process: {exc}") from exc
            finally:
                os.close(log_fd)

            pid = int(process.pid)
            try:
                starting_file.write_text(
                    json.dumps({"pid": pid, "time": time.time()}),
                    encoding="utf-8",
                )
            except Exception:
                pass

            timeout = self.startup_timeout if startup_timeout is None else startup_timeout
            poll = getattr(process, "poll", None)

            # If an injected popen_factory was provided (e.g. in unit tests), check poll immediately
            if self._popen_factory != subprocess.Popen:
                if callable(poll):
                    exit_code = poll()
                    if exit_code is not None:
                        recent_logs = self.read_logs(max_lines=15)
                        err_snippet = (
                            "\n".join(recent_logs).strip()
                            if recent_logs
                            else f"exited with code {exit_code}"
                        )
                        raise DaemonProcessError(f"Daemon failed to start: {err_snippet}")
                logger.info("Daemon process started with PID %d", pid)
                return pid

            # For real subprocesses, monitor startup window for immediate exit or verified lock acquisition
            deadline = time.time() + timeout
            started_ok = False
            lock = self._get_lock(data_dir)
            while time.time() < deadline:
                if callable(poll):
                    exit_code = poll()
                    if exit_code is not None:
                        recent_logs = self.read_logs(max_lines=15)
                        err_snippet = (
                            "\n".join(recent_logs).strip()
                            if recent_logs
                            else f"exited with code {exit_code}"
                        )
                        logger.error(
                            "Daemon process PID %d exited during startup with code %d: %s",
                            pid,
                            exit_code,
                            err_snippet,
                        )
                        raise DaemonProcessError(f"Daemon failed to start: {err_snippet}")

                if lock.is_active():
                    metadata = lock.read_metadata()
                    if metadata.get("pid") == pid:
                        started_ok = True
                        break
                time.sleep(0.05)

            if not started_ok:
                if callable(poll) and poll() is not None:
                    exit_code = poll()
                    recent_logs = self.read_logs(max_lines=15)
                    err_snippet = (
                        "\n".join(recent_logs).strip()
                        if recent_logs
                        else f"exited with code {exit_code}"
                    )
                    raise DaemonProcessError(f"Daemon failed to start: {err_snippet}")
                if not lock.is_active():
                    logger.warning(
                        "Daemon process PID %d is running but lock file was not observed within %.1fs",
                        pid,
                        timeout,
                    )

            logger.info("Daemon process started with PID %d", pid)
            return pid
        finally:
            if starting_file is not None:
                starting_file.unlink(missing_ok=True)
            self._start_lock.release()

    def stop(self, timeout: float = 10.0) -> bool:
        """Gracefully stop the running daemon without force-killing."""
        config = self.config_loader(self.config_path)
        data_dir = get_data_dir(config)
        lock = self._get_lock(data_dir)
        metadata = lock.read_metadata()
        pid = metadata.get("pid")

        if not isinstance(pid, int) or pid <= 0 or not lock.is_active():
            logger.info("Daemon is not running.")
            return True

        # Send SIGTERM to the daemon
        try:
            os.kill(pid, signal.SIGTERM)
            logger.info("Sent SIGTERM to daemon PID %d", pid)
        except ProcessLookupError:
            logger.info("Daemon PID %d had already exited", pid)
            lock.force_remove_if_inactive()
            return True
        except PermissionError as exc:
            raise DaemonProcessError(f"Permission denied stopping daemon PID {pid}: {exc}") from exc

        # Wait up to timeout for process to exit gracefully
        deadline = time.monotonic() + max(0.1, timeout)
        while time.monotonic() < deadline:
            if not lock.is_active():
                logger.info("Daemon PID %d stopped successfully.", pid)
                if self._process is not None and hasattr(self._process, "wait"):
                    try:
                        self._process.wait(timeout=0.5)
                    except Exception:
                        pass
                    self._process = None
                return True
            time.sleep(0.2)

        # Do NOT force-kill!
        logger.warning(
            "Daemon PID %d did not terminate within %.1fs; it may be completing an active batch.",
            pid,
            timeout,
        )
        return False

    def read_logs(self, max_lines: int = 100) -> list[str]:
        """Read a bounded tail of the daemon log file."""
        config = self.config_loader(self.config_path)
        data_dir = get_data_dir(config)
        log_path = data_dir / DAEMON_LOG_FILENAME
        if not log_path.exists():
            return []
        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
                return [line.rstrip("\r\n") for line in lines[-max_lines:]]
        except Exception as exc:
            logger.warning("Could not read daemon logs from %s: %s", log_path, exc)
            return [f"Error reading log file: {exc}"]

    def rotate_logs_if_needed(
        self,
        log_path: Path,
        max_bytes: int = DEFAULT_MAX_LOG_BYTES,
        backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
    ) -> None:
        """Rotate daemon log file if it exceeds max_bytes."""
        if not log_path.exists():
            return
        try:
            if log_path.stat().st_size < max_bytes:
                return
            for i in range(backup_count - 1, 0, -1):
                sfn = log_path.with_name(f"{log_path.name}.{i}")
                dfn = log_path.with_name(f"{log_path.name}.{i + 1}")
                if sfn.exists():
                    sfn.replace(dfn)
            dfn = log_path.with_name(f"{log_path.name}.1")
            log_path.replace(dfn)
        except Exception as exc:
            logger.warning("Could not rotate daemon log: %s", exc)


def _install_stop_handlers(stop_event: asyncio.Event) -> Callable[[], None]:
    """Arrange graceful stop handling where the event loop supports signals."""

    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []

    def request_stop() -> None:
        logger.info("Stop signal received; waiting for the current sender operation to finish.")
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, request_stop)
        except (NotImplementedError, RuntimeError):
            # Windows' default event loop may not support add_signal_handler.
            # ``signal.signal`` still allows a foreground process to stop.
            try:
                signal.signal(signum, lambda *_: loop.call_soon_threadsafe(request_stop))
            except (ValueError, OSError):
                logger.debug("Could not install stop handler for %s", signum, exc_info=True)
        else:
            installed.append(signum)

    def remove_handlers() -> None:
        for signum in installed:
            loop.remove_signal_handler(signum)

    return remove_handlers
