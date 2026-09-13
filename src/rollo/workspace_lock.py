"""Cross-process workspace ownership primitives.

The lock is deliberately small and boring: the operating system owns the
actual exclusion, while the Application control database stores only
diagnostic owner metadata.  A stale lock file therefore never becomes a
silent lease.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

__all__ = [
    "WorkspaceLockError",
    "WorkspaceLockBusyError",
    "workspace_lock_key",
    "WorkspaceLock",
]


class WorkspaceLockError(RuntimeError):
    code = "workspace_lock_error"


class WorkspaceLockBusyError(WorkspaceLockError):
    code = "owner_conflict"


def workspace_lock_key(workspace_id: str) -> str:
    """Return the stable, platform-independent lock namespace key."""

    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise ValueError("workspace_id must be a non-empty string")
    return hashlib.sha256(
        f"rollo-workspace-lock-v1:{workspace_id}".encode("utf-8")
    ).hexdigest()[:32]


class WorkspaceLock:
    """An OS-backed advisory lock held for the lifetime of this object."""

    def __init__(self, path: str | Path, *, workspace_id: str, owner_id: str) -> None:
        self.path = Path(path)
        self.workspace_id = workspace_id
        self.owner_id = owner_id
        self.key = workspace_lock_key(workspace_id)
        self._handle: Any | None = None
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._handle is not None or self._fd is not None

    def acquire(self) -> "WorkspaceLock":
        if self.held:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            self._acquire_windows()
        else:  # pragma: no cover - exercised on Linux CI
            self._acquire_posix()
        return self

    def _acquire_windows(self) -> None:
        import msvcrt

        flags = os.O_RDWR | os.O_CREAT
        fd = os.open(self.path, flags)
        try:
            # msvcrt.locking locks bytes from the current file position.  Keep
            # a real byte in the file so another process observes the same
            # range even when the lock file was newly created.
            if os.path.getsize(self.path) == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise WorkspaceLockBusyError(
                    f"workspace owner is held: {self.workspace_id}"
                ) from exc
            self._fd = fd
        except Exception:
            os.close(fd)
            raise

    def _acquire_posix(self) -> None:  # pragma: no cover - platform branch
        import fcntl

        handle = open(self.path, "a+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise WorkspaceLockBusyError(
                f"workspace owner is held: {self.workspace_id}"
            ) from exc
        self._handle = handle

    def release(self) -> None:
        if os.name == "nt":
            if self._fd is None:
                return
            import msvcrt

            fd, self._fd = self._fd, None
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            finally:
                os.close(fd)
            return
        if self._handle is not None:  # pragma: no cover - platform branch
            import fcntl

            handle, self._handle = self._handle, None
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def __enter__(self) -> "WorkspaceLock":
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()

    def __del__(self) -> None:  # pragma: no cover - best-effort crash cleanup
        try:
            self.release()
        except Exception:
            pass
