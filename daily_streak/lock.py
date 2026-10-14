"""Small cross-process lock used to prevent overlapping sender batches."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import socket
import time
from typing import Any, Callable
from uuid import uuid4


logger = logging.getLogger(__name__)

RUN_LOCK_FILENAME = "send.lock"
DEFAULT_STALE_LOCK_SECONDS = 6 * 60 * 60


class RunLockedError(RuntimeError):
    """Raised when another sender batch is already active."""


class RunLock:
    """An exclusive lock file with conservative stale-lock recovery.

    A live process on the local hostname is always treated as active.  Locks
    from another host (or malformed locks) are only recovered after the
    configured stale period, which keeps a shared data directory from being
    unlocked immediately after a transient network issue.
    """

    def __init__(
        self,
        data_dir: str | Path,
        *,
        stale_seconds: float = DEFAULT_STALE_LOCK_SECONDS,
        now: Callable[[], float] = time.time,
        pid: int | None = None,
        hostname: str | None = None,
        process_is_alive: Callable[[int], bool] | None = None,
    ) -> None:
        self.path = Path(data_dir) / RUN_LOCK_FILENAME
        self.stale_seconds = max(1.0, float(stale_seconds))
        self._now = now
        self._pid = os.getpid() if pid is None else pid
        self._hostname = socket.gethostname() if hostname is None else hostname
        self._process_is_alive = process_is_alive or _process_is_alive
        self._token: str | None = None

    def acquire(self) -> None:
        """Acquire the lock, recovering only a demonstrably stale lock."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            token = uuid4().hex
            payload = {
                "pid": self._pid,
                "hostname": self._hostname,
                "created_at": self._now(),
                "token": token,
            }
            try:
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                if self._is_stale():
                    self._remove_stale_lock()
                    continue
                raise RunLockedError(
                    f"A sender batch is already running (lock: {self.path})."
                )

            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                    json.dump(payload, file, separators=(",", ":"))
                    file.write("\n")
                    file.flush()
                    os.fsync(file.fileno())
            except Exception:
                self.path.unlink(missing_ok=True)
                raise
            self._token = token
            logger.debug("Acquired sender run lock at %s", self.path)
            return

        raise RunLockedError(f"Could not recover stale sender lock: {self.path}")

    def release(self) -> None:
        """Release the lock only when this instance still owns it."""

        token = self._token
        self._token = None
        if not token:
            return
        try:
            metadata = self._read_metadata()
            if metadata.get("token") != token:
                logger.warning("Not removing sender lock with a different owner: %s", self.path)
                return
            self.path.unlink(missing_ok=True)
            logger.debug("Released sender run lock at %s", self.path)
        except Exception:
            logger.exception("Could not release sender run lock at %s", self.path)

    def __enter__(self) -> "RunLock":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()

    def _read_metadata(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    def _is_stale(self) -> bool:
        try:
            age = self._now() - self.path.stat().st_mtime
        except FileNotFoundError:
            return False
        metadata = self._read_metadata()
        hostname = metadata.get("hostname")
        pid = metadata.get("pid")
        if hostname == self._hostname and isinstance(pid, int) and pid > 0:
            return not self._process_is_alive(pid)
        return age >= self.stale_seconds

    def _remove_stale_lock(self) -> None:
        try:
            self.path.unlink()
            logger.warning("Recovered stale sender lock at %s", self.path)
        except FileNotFoundError:
            return


def _process_is_alive(pid: int) -> bool:
    """Check local process liveness without requiring a platform-specific API."""

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


DAEMON_LOCK_FILENAME = "daemon.lock"


class DaemonLockedError(RuntimeError):
    """Raised when another daemon process is already active."""


def is_daemon_process_alive(
    pid: int,
    hostname: str | None = None,
    *,
    expected_hostname: str | None = None,
) -> bool:
    """Safely verify if pid belongs to an active Daily Streak Tool daemon process."""
    if pid <= 0:
        return False
    local_host = expected_hostname or socket.gethostname()
    if hostname and hostname != local_host:
        return False
    if not _process_is_alive(pid):
        return False

    # On Linux, inspect /proc/{pid}/status and /proc/{pid}/cmdline to guard against zombie/recycled PIDs
    proc_status = Path(f"/proc/{pid}/status")
    if proc_status.exists():
        try:
            for line in proc_status.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("State:"):
                    state = line.split(":", 1)[1].strip()
                    # Z (zombie), T (stopped/traced), X (dead) are not active
                    if state.startswith(("Z", "T", "X")):
                        return False
                    break
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            return False

    proc_cmdline = Path(f"/proc/{pid}/cmdline")
    if proc_cmdline.exists():
        try:
            raw = proc_cmdline.read_bytes().decode("utf-8", errors="ignore")
            # Must contain daily_streak or streak and daemon
            if ("daily_streak" in raw or "streak" in raw) and "daemon" in raw:
                return True
            return False
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            return False

    return True


class DaemonLock:
    """Exclusive daemon-lifetime lock with safe stale-PID recovery."""

    def __init__(
        self,
        data_dir: str | Path,
        *,
        config_path: str | Path | None = None,
        pid: int | None = None,
        hostname: str | None = None,
        now: Callable[[], float] = time.time,
        process_is_daemon: Callable[[int, str | None], bool] | None = None,
    ) -> None:
        self.path = Path(data_dir) / DAEMON_LOCK_FILENAME
        self.config_path = str(config_path) if config_path else ""
        self._now = now
        self._pid = os.getpid() if pid is None else pid
        self._hostname = socket.gethostname() if hostname is None else hostname
        self._process_is_daemon = process_is_daemon or is_daemon_process_alive
        self._token: str | None = None

    def read_metadata(self) -> dict[str, Any]:
        """Read serialized metadata from lock file."""
        return self._read_metadata()

    def is_active(self) -> bool:
        """Check if lock file exists and belongs to a live daemon process."""
        metadata = self._read_metadata()
        if not metadata:
            return False
        pid = metadata.get("pid")
        hostname = metadata.get("hostname")
        if isinstance(pid, int) and pid > 0:
            return self._process_is_daemon(pid, hostname)
        return False

    def acquire(self) -> None:
        """Acquire the daemon lifetime lock, recovering stale locks safely."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            token = uuid4().hex
            payload = {
                "pid": self._pid,
                "hostname": self._hostname,
                "created_at": self._now(),
                "token": token,
                "config_path": self.config_path,
            }
            try:
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                if not self.is_active():
                    self._remove_stale_lock()
                    continue
                metadata = self._read_metadata()
                active_pid = metadata.get("pid")
                raise DaemonLockedError(
                    f"A daemon is already running (PID: {active_pid}, lock: {self.path})."
                )

            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                    json.dump(payload, file, separators=(",", ":"))
                    file.write("\n")
                    file.flush()
                    os.fsync(file.fileno())
            except Exception:
                self.path.unlink(missing_ok=True)
                raise
            self._token = token
            logger.debug("Acquired daemon lock at %s (PID %d)", self.path, self._pid)
            return

        raise DaemonLockedError(f"Could not acquire daemon lock: {self.path}")

    def release(self) -> None:
        """Release the daemon lock if owned by this instance."""
        token = self._token
        self._token = None
        if not token:
            return
        try:
            metadata = self._read_metadata()
            if metadata.get("token") != token:
                logger.warning("Not removing daemon lock with a different token: %s", self.path)
                return
            self.path.unlink(missing_ok=True)
            logger.debug("Released daemon lock at %s", self.path)
        except Exception:
            logger.exception("Could not release daemon lock at %s", self.path)

    def force_remove_if_inactive(self) -> bool:
        """Safely remove lock if process is not an active daemon."""
        if not self.is_active():
            self._remove_stale_lock()
            return True
        return False

    def _read_metadata(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    def _remove_stale_lock(self) -> None:
        try:
            self.path.unlink()
            logger.warning("Recovered stale daemon lock at %s", self.path)
        except FileNotFoundError:
            return
