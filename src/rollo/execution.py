"""Managed local subprocess execution used by the Application supervisor."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shlex
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

__all__ = ["StreamCapture", "ManagedExecutionSnapshot", "ManagedExecutionHandle"]


@dataclass(frozen=True, slots=True)
class StreamCapture:
    stream: str
    byte_count: int
    sha256: str
    data: bytes
    spill_path: str | None = None


@dataclass(frozen=True, slots=True)
class ManagedExecutionSnapshot:
    requested_cancel: bool
    graceful_exit: bool
    forced_exit: bool
    returncode: int | None
    pid: int | None
    pid_alive: bool
    descendant_alive: bool
    stdout: StreamCapture | None
    stderr: StreamCapture | None


class ManagedExecutionHandle:
    """Subprocess handle with independent stdout/stderr drains and evidence."""

    def __init__(
        self,
        *,
        owner_id: str,
        root_owner_id: str | None = None,
        execution_id: str,
        spill_dir: str | Path | None = None,
        max_memory_bytes: int = 256 * 1024,
    ) -> None:
        self.owner_id = owner_id
        self.root_owner_id = root_owner_id or owner_id
        self.execution_id = execution_id
        self.spill_dir = Path(spill_dir) if spill_dir is not None else None
        self.max_memory_bytes = max(1, int(max_memory_bytes))
        self.process: asyncio.subprocess.Process | None = None
        self.requested_cancel = False
        self.graceful_exit = False
        self.forced_exit = False
        self._drain_tasks: dict[str, asyncio.Task[StreamCapture]] = {}
        self._captures: dict[str, StreamCapture] = {}

    async def start(
        self,
        command: str | Sequence[str],
        *,
        cwd: str | Path,
        shell: bool = False,
    ) -> "ManagedExecutionHandle":
        kwargs: dict[str, object] = {
            "cwd": os.fspath(cwd),
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(__import__("subprocess"), "CREATE_NEW_PROCESS_GROUP", 0)
        else:  # put the process and descendants in a killable group
            kwargs["start_new_session"] = True
        if shell and isinstance(command, str):
            self.process = await asyncio.create_subprocess_shell(command, **kwargs)
        else:
            argv = _argv(command)
            self.process = await asyncio.create_subprocess_exec(*argv, **kwargs)
        assert self.process.stdout is not None and self.process.stderr is not None
        self._drain_tasks["stdout"] = asyncio.create_task(self._drain("stdout", self.process.stdout))
        self._drain_tasks["stderr"] = asyncio.create_task(self._drain("stderr", self.process.stderr))
        return self

    async def wait(self, *, timeout: float | None = None) -> ManagedExecutionSnapshot:
        if self.process is None:
            raise RuntimeError("execution has not started")
        try:
            await asyncio.wait_for(self.process.wait(), timeout=timeout) if timeout is not None else await self.process.wait()
            self.graceful_exit = not self.forced_exit
        except asyncio.TimeoutError:
            await self.cancel(grace_timeout=0.2)
        await self._finish_drains()
        return self.snapshot()

    async def cancel(self, *, grace_timeout: float = 1.0) -> ManagedExecutionSnapshot:
        if self.process is None or self.process.returncode is not None:
            return self.snapshot()
        self.requested_cancel = True
        try:
            if os.name == "nt":
                self.process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(self.process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            with contextlib_suppress():
                self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=max(0.0, grace_timeout))
            self.graceful_exit = True
        except asyncio.TimeoutError:
            self.forced_exit = True
            try:
                if os.name != "nt":
                    os.killpg(self.process.pid, signal.SIGKILL)
                else:
                    self.process.kill()
            except (OSError, ProcessLookupError):
                pass
            await self.process.wait()
        await self._finish_drains()
        return self.snapshot()

    async def _finish_drains(self) -> None:
        if self._drain_tasks:
            results = await asyncio.gather(*self._drain_tasks.values(), return_exceptions=True)
            for stream, result in zip(self._drain_tasks, results):
                if isinstance(result, StreamCapture):
                    self._captures[stream] = result

    async def _drain(self, stream: str, reader: asyncio.StreamReader) -> StreamCapture:
        digest = hashlib.sha256()
        memory = bytearray()
        spill_path: Path | None = None
        spill = None
        count = 0
        try:
            while True:
                chunk = await reader.read(64 * 1024)
                if not chunk:
                    break
                count += len(chunk)
                digest.update(chunk)
                if len(memory) < self.max_memory_bytes and spill is None:
                    take = min(len(chunk), self.max_memory_bytes - len(memory))
                    memory.extend(chunk[:take])
                    remainder = chunk[take:]
                else:
                    remainder = chunk
                if remainder:
                    if spill is None:
                        if self.spill_dir is None:
                            self.spill_dir = Path.cwd() / ".rollo" / "execution-spill"
                        self.spill_dir.mkdir(parents=True, exist_ok=True)
                        spill_path = self.spill_dir / f"{self.execution_id}.{stream}.bin"
                        spill = open(spill_path, "wb")
                    spill.write(remainder)
            if spill is not None:
                spill.flush()
                os.fsync(spill.fileno())
                spill.close()
        finally:
            if spill is not None and not spill.closed:
                spill.close()
        return StreamCapture(stream, count, digest.hexdigest(), bytes(memory), str(spill_path) if spill_path else None)

    def snapshot(self) -> ManagedExecutionSnapshot:
        process = self.process
        pid = process.pid if process is not None else None
        alive = bool(process is not None and process.returncode is None)
        return ManagedExecutionSnapshot(
            requested_cancel=self.requested_cancel,
            graceful_exit=self.graceful_exit,
            forced_exit=self.forced_exit,
            returncode=process.returncode if process is not None else None,
            pid=pid,
            pid_alive=alive,
            descendant_alive=_descendant_alive(pid) if pid is not None else False,
            stdout=self._captures.get("stdout"),
            stderr=self._captures.get("stderr"),
        )


def _argv(command: str | Sequence[str]) -> list[str]:
    if isinstance(command, str):
        return shlex.split(command, posix=os.name != "nt")
    return [os.fspath(item) for item in command]


def _descendant_alive(root_pid: int | None) -> bool:
    """Best-effort descendant evidence after the root process exits.

    POSIX process groups give a kernel-level answer.  Windows uses an optional
    psutil walk when available; the Job Object backend can refine this without
    changing the public snapshot contract.
    """

    if root_pid is None:
        return False
    if os.name != "nt":
        try:
            os.killpg(root_pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False
    try:  # psutil is optional in the core package
        import psutil  # type: ignore

        ppid_by_pid: dict[int, int] = {}
        for process in psutil.process_iter(["pid", "ppid"]):
            info = process.info
            if info.get("pid") is not None and info.get("ppid") is not None:
                ppid_by_pid[int(info["pid"])] = int(info["ppid"])
        for candidate in tuple(ppid_by_pid):
            current = candidate
            seen: set[int] = set()
            while current not in seen and current in ppid_by_pid:
                if ppid_by_pid[current] == root_pid:
                    return True
                seen.add(current)
                current = ppid_by_pid[current]
    except Exception:
        return False
    return False


class contextlib_suppress:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return True
