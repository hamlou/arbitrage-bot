"""Single-instance guard for the bot.

Prevents two bot processes from running against the same storage directory
(the cause of two separate double-launch incidents: duplicated trading
cycles, doubled API load, and a 16 MB error log). It needs code, not
vigilance — a human checking Task Manager is how the duplicates slipped
through before.

Design: an OS-level advisory lock on ``storage/bot.lock``. On Windows that is
``msvcrt.locking``; on POSIX, ``fcntl.flock``. The OS releases the lock
automatically when the owning process dies — including a crash or kill — so
a stale lock file is impossible by construction; the file may remain, but
whoever holds the byte-range/file lock is by definition a live process.
We also write the owning PID into the file for diagnostics ("who is holding
this?").

    lock = SingleInstanceLock(Path("storage/bot.lock"))
    if not lock.acquire():
        sys.exit("Another instance is already running")
    try:
        ...
    finally:
        lock.release()
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import msvcrt  # Windows
    _HAVE_MSVCRT = True
except ImportError:  # pragma: no cover - POSIX
    _HAVE_MSVCRT = False

try:
    import fcntl  # POSIX
    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - Windows
    _HAVE_FCNTL = False


class SingleInstanceLock:
    """Exclusive lock file guarding one bot process per storage directory."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fd: Optional[int] = None

    def acquire(self) -> bool:
        """Try to take the lock.

        Returns True if this process now owns it (either freshly or by
        reclaiming a leftover file whose OS lock is free), False if another
        live process holds it.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            logger.error("Cannot open lock file %s", self.path)
            return False
        self._fd = fd
        # Ensure at least one byte exists so the byte-range lock is valid.
        try:
            os.lseek(fd, 0, os.SEEK_END)
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
        except OSError:
            pass
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if _HAVE_MSVCRT:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            elif _HAVE_FCNTL:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:  # pragma: no cover - unsupported platform
                logger.error("No lock primitive available on this platform")
                os.close(fd)
                self._fd = None
                return False
        except OSError:
            # Another live process holds the OS lock. The previous PID (if any)
            # is informational only — the OS lock is the authority.
            prev = self._read_pid()
            os.close(fd)
            self._fd = None
            logger.error(
                "Another bot instance is already running (lock %s held%s). "
                "Refusing to start — kill the existing process first.",
                self.path,
                f" by PID {prev}" if prev is not None else "",
            )
            return False
        # We own it. Record our PID for diagnostics.
        prev = self._read_pid()
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())
            os.fsync(fd)
        except OSError:
            pass
        if prev is not None:
            logger.info(
                "Reclaimed stale single-instance lock %s (leftover PID %s no longer holds it).",
                self.path,
                prev,
            )
        return True

    def held_pid(self) -> Optional[int]:
        """PID currently recorded in the lock file, read through our own
        handle. (Windows byte-range locks block OTHER handles from reading
        the locked bytes, so this must go through the locking fd.) Returns
        None when we don't hold the lock or no PID is recorded."""
        if self._fd is None:
            return None
        try:
            os.lseek(self._fd, 0, os.SEEK_SET)
            raw = os.read(self._fd, 64).strip().rstrip(b"\0")
            if not raw:
                return None
            return int(raw)
        except (OSError, ValueError):
            return None

    def _read_pid(self) -> Optional[int]:
        return self.held_pid()

    def release(self) -> None:
        """Release the lock and remove the lock file. Safe to call when the
        lock was never acquired or was already released."""
        if self._fd is None:
            return
        try:
            os.lseek(self._fd, 0, os.SEEK_SET)
            if _HAVE_MSVCRT:
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            elif _HAVE_FCNTL:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None
        try:
            self.path.unlink()
        except OSError:
            pass
