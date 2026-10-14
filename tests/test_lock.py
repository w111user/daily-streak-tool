from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from daily_streak.lock import RunLock, RunLockedError


class RunLockTests(unittest.TestCase):
    def test_acquire_blocks_another_owner_and_release_allows_a_new_owner(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory)
            first = RunLock(
                data_dir,
                pid=101,
                hostname="test-host",
                process_is_alive=lambda _: True,
            )
            second = RunLock(
                data_dir,
                pid=102,
                hostname="test-host",
                process_is_alive=lambda _: True,
            )
            first.acquire()
            self.assertTrue(first.path.exists())
            with self.assertRaises(RunLockedError):
                second.acquire()
            first.release()
            second.acquire()
            second.release()

        self.assertFalse((data_dir / "send.lock").exists())

    def test_dead_local_process_lock_is_recovered(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory)
            stale = RunLock(
                data_dir,
                pid=101,
                hostname="test-host",
                process_is_alive=lambda _: True,
            )
            stale.acquire()

            recovered = RunLock(
                data_dir,
                pid=202,
                hostname="test-host",
                process_is_alive=lambda pid: pid == 202,
            )
            recovered.acquire()
            self.assertTrue(recovered.path.exists())
            recovered.release()

    def test_context_manager_releases_after_exception(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            lock = RunLock(Path(temporary_directory))
            with self.assertRaises(RuntimeError):
                with lock:
                    raise RuntimeError("boom")
            self.assertFalse(lock.path.exists())
