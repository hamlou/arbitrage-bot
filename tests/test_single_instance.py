"""Tests for the single-instance guard (engine/single_instance.py).

Covers the two failure modes that caused real double-launch incidents:
  (1) a second bot instance must refuse to start while the first holds the
      lock;
  (2) a leftover lock file from a dead process must be reclaimed, not block
      the next launch forever.
"""
import os

import pytest

from engine.single_instance import SingleInstanceLock


def test_second_instance_is_refused(tmp_path):
    lock_path = tmp_path / "bot.lock"
    first = SingleInstanceLock(lock_path)
    second = SingleInstanceLock(lock_path)

    assert first.acquire() is True
    # A second handle to the same path must NOT get the lock — even within
    # this same process, because the OS lock is per open-file-description.
    assert second.acquire() is False

    # The lock file records the owning PID for diagnostics. Read through the
    # owning handle (Windows byte-range locks block other handles from
    # reading the locked bytes).
    assert first.held_pid() == os.getpid()

    first.release()
    # Once the first instance releases, the lock is free again.
    assert second.acquire() is True
    second.release()
    assert lock_path.exists() is False


def test_stale_lock_file_is_reclaimed(tmp_path):
    """A leftover lock file whose OS lock is free (owner died, crashed, or
    was killed) must be taken over, not block the next launch."""
    lock_path = tmp_path / "bot.lock"
    # Simulate a dead process's leftover: PID that is almost certainly not
    # running (and even if the PID existed, the OS lock is what matters).
    lock_path.write_text("999999999\0")

    lock = SingleInstanceLock(lock_path)
    assert lock.acquire() is True
    assert lock.held_pid() == os.getpid()
    lock.release()


def test_release_twice_is_harmless(tmp_path):
    lock = SingleInstanceLock(tmp_path / "bot.lock")
    assert lock.acquire() is True
    lock.release()
    lock.release()  # no exception


def test_acquire_after_release_restarts_clean(tmp_path):
    lock_path = tmp_path / "bot.lock"
    lock = SingleInstanceLock(lock_path)
    assert lock.acquire() is True
    lock.release()
    # Fresh acquire on a fresh instance works and re-creates the file.
    again = SingleInstanceLock(lock_path)
    assert again.acquire() is True
    assert lock_path.exists()
    again.release()
