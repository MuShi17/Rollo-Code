"""C03 tasks 4.2 / 6.3 / 6.6 — real multi-process crash, restart and lock evidence.

Every scenario here runs the product code in a *separate real Python process*.
The workers live in this same file and are re-entered through
``python test_c03_crash_recovery.py <scenario>``, so the worker and its oracle
stay together without shipping a test-only module inside the package.

The oracle is external observation only — control.sqlite rows, canonical store
projections, on-disk side-effect markers and worker exit codes — never a
re-implementation of an Application branch.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC_ROOT = _REPO_ROOT / "src"
_WORKER_FILE = Path(__file__).resolve()

#: Marker exit code for a worker that dies without any cleanup.
CRASH_EXIT_CODE = 9


# --------------------------------------------------------------------------
# worker side — executed only inside the child process
# --------------------------------------------------------------------------


class _RecordingAgent:
    """Agent stand-in whose only job is to leave an observable side effect."""

    def __init__(self, **kwargs: Any) -> None:
        self.runtime_store = kwargs.get("runtime_store")
        self.session_id = kwargs.get("runtime_session_id")
        self.run_id = kwargs.get("runtime_run_id")
        marker_dir = Path(os.environ["C03_MARKER_DIR"])
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / f"agent-{os.getpid()}").write_text("constructed", encoding="utf-8")

    async def chat(self, prompt: str) -> None:
        if prompt in {"crash-after-open", "crash-after-dispatch"}:
            _crash_mid_dispatch(prompt, self.runtime_store, self.session_id, self.run_id)
        if prompt == "side-effect":
            marker_dir = Path(os.environ["C03_MARKER_DIR"])
            with (marker_dir / "side-effect.txt").open("a", encoding="utf-8") as stream:
                stream.write("side-effect\n")

    def abort(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


def _open_event(session_id: str, run_id: str) -> Any:
    from rollo.runtime_event import RuntimeEvent

    return RuntimeEvent.from_dict({
        "schema_version": 2,
        "id": "crash-invocation-opened",
        "session_id": session_id,
        "run_id": run_id,
        "invocation_id": "inv-crash",
        "turn_id": "turn-crash",
        "ts": 1,
        "partial": False,
        "role": "system",
        "author": "agent",
        "content": {
            "kind": "invocation_opened",
            "protocol": "invocation_opened_v1",
            "route": {"provider": "fixture", "model": "fixture-model"},
            "configuration": {"attempt": 1},
            "root": {"kind": "agent"},
            "source": {"kind": "fresh"},
        },
    })


def _dispatch_event(session_id: str, run_id: str) -> Any:
    from rollo.runtime_event import RuntimeEvent

    return RuntimeEvent.from_dict({
        "schema_version": 2,
        "id": "crash-tool-dispatch",
        "session_id": session_id,
        "run_id": run_id,
        "invocation_id": "inv-crash",
        "turn_id": "turn-crash",
        "ts": 2,
        "partial": False,
        "role": "model",
        "author": "agent",
        "actions": {
            "tool_dispatch": {
                "operation_id": "op-crash",
                "provider_tool_call_id": "tc-crash",
                "tool_name": "run_shell",
                "canonical_args_hash": "args-crash",
            }
        },
    })


def _crash_mid_dispatch(prompt: str, store: Any, session_id: str, run_id: str) -> None:
    """Append canonical dispatch evidence, announce the barrier, then die mid-dispatch."""

    store.append(_open_event(session_id, run_id))
    if prompt == "crash-after-dispatch":
        # A tool side effect was dispatched but its outcome was never observed.
        # This is the riskiest restart shape: replaying it duplicates the effect.
        store.append(_dispatch_event(session_id, run_id))
    barrier = "canonical-open" if prompt == "crash-after-open" else "canonical-dispatch"
    _write_file(os.environ["C03_CRASH_AFTER"], barrier)
    _emit({"scenario": "crash", "reached": barrier, "pid": os.getpid()})
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(CRASH_EXIT_CODE)


def _write_file(path: str | Path, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


def _context(root: Path):
    from rollo.project_context import ProjectContext

    return ProjectContext.from_root(root, runtime_data_dir=Path(os.environ["C03_RUNTIME"]))


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True, default=str), flush=True)


def _worker_run_start_then_crash() -> None:
    """Reach the requested crash barrier and die without any cleanup."""

    import asyncio

    from rollo.application import Application

    prompt = os.environ.get("C03_PROMPT", "crash-after-open")

    async def main() -> None:
        context = _context(Path(os.environ["C03_ROOT"]))
        app = Application(context, agent_factory=_RecordingAgent)
        session = app.session_create("crash-mid-dispatch").session_id
        response = await app.run_start(
            session_id=session, prompt=prompt, command_id="crash-command"
        )
        _emit({
            "scenario": "run-start-then-crash",
            "session_id": session,
            "run_id": response.run_id,
            "status": response.status,
            "error_code": response.error_code,
            "pid": os.getpid(),
        })
        # _RecordingAgent.chat terminates this process at the barrier above.
        for _ in range(1200):
            await asyncio.sleep(0.05)

    asyncio.run(main())


def _worker_recover() -> None:
    """Restart as a brand-new Application and report what the durable rows say."""

    import asyncio

    from rollo.application import Application

    factory_calls: list[str] = []

    def factory(**kwargs: Any) -> Any:
        factory_calls.append("created")
        raise AssertionError("recovery must never dispatch an agent")

    async def main() -> None:
        context = _context(Path(os.environ["C03_ROOT"]))
        app = Application(context, agent_factory=factory)
        payload: dict[str, Any] = {"scenario": "recover", "pid": os.getpid()}

        run_id = os.environ.get("C03_RUN_ID")
        if run_id:
            status = app.run_status(run_id)
            payload["run_id"] = run_id
            payload["status"] = status.status
            payload["error_code"] = status.error_code
            payload["result"] = (status.data or {}).get("result")
            payload["dispatch_intent"] = (status.data or {}).get("dispatch_intent")

        session_id = os.environ["C03_SESSION_ID"]
        payload["runs"] = [
            {
                "run_id": row["run_id"],
                "status": row["status"],
                "error_code": row["error_code"],
                "dispatch_intent": bool(row["dispatch_intent"]),
            }
            for row in app.control.runs_for_session(session_id)
        ]
        payload["owners"] = [
            {
                "owner_id": row["owner_id"],
                "status": row["status"],
                "quarantine": int(row["quarantine"]),
                "generation": int(row["generation"]),
            }
            for row in app.control.owner_rows(context.workspace_id)
        ]
        payload["agent_factory_calls"] = len(factory_calls)
        _emit(payload)
        await app.shutdown()

    asyncio.run(main())


def _worker_retry_same_command() -> None:
    """Response-loss barrier: the command row exists but its reply was never read."""

    import asyncio

    from rollo.application import Application

    factory_calls: list[str] = []

    def factory(**kwargs: Any) -> Any:
        factory_calls.append("created")
        raise AssertionError("an accepted command must never be dispatched twice")

    async def main() -> None:
        context = _context(Path(os.environ["C03_ROOT"]))
        app = Application(context, agent_factory=factory)
        response = await app.run_start(
            session_id=os.environ["C03_SESSION_ID"],
            prompt=os.environ["C03_PROMPT"],
            command_id="crash-command",
        )
        _emit({
            "scenario": "retry-same-command",
            "run_id": response.run_id,
            "status": response.status,
            "result": response.result,
            "error_code": response.error_code,
            "agent_factory_calls": len(factory_calls),
        })
        await app.shutdown()

    asyncio.run(main())


def _worker_quarantine_then_reconcile() -> None:
    """A crashed root frees the OS lock but must not silently hand over the workspace."""

    import asyncio

    from rollo.application import Application

    factory_calls: list[str] = []

    def factory(**kwargs: Any) -> Any:
        factory_calls.append("created")
        return _RecordingAgent(**kwargs)

    async def main() -> None:
        context = _context(Path(os.environ["C03_ROOT"]))
        app = Application(context, agent_factory=factory)
        payload: dict[str, Any] = {"scenario": "quarantine-then-reconcile"}

        # The restarting root must observe the quarantined dead owner and refuse
        # to acquire the workspace until that owner is explicitly reconciled.
        row = app.control.owner(os.environ["C03_OWNER_ID"])
        assert row is not None, "the crashed owner row must still be readable"
        payload["owner_status"] = row["status"]
        payload["owner_quarantine"] = int(row["quarantine"])

        blocked = await app.run_start(
            session_id=os.environ["C03_SESSION_ID"], prompt="adopt", command_id="adopt-command"
        )
        payload["blocked_status"] = blocked.status
        payload["blocked_error_code"] = blocked.error_code
        payload["factory_calls_before_quarantine_clear"] = len(factory_calls)

        reconciled = app.owner_reconcile(
            owner_id=os.environ["C03_OWNER_ID"],
            generation=int(row["generation"]),
            action="release",
            evidence={"reason": "operator-verified-crash"},
        )
        payload["reconcile_status"] = reconciled.status
        payload["reconcile_error_code"] = reconciled.error_code

        accepted = await app.run_start(
            session_id=os.environ["C03_SESSION_ID"],
            prompt="side-effect",
            command_id="after-reconcile-command",
        )
        payload["accepted_status"] = accepted.status
        payload["accepted_error_code"] = accepted.error_code
        if accepted.run_id is not None:
            await app.wait_run(accepted.run_id)
            payload["final_status"] = app.run_status(accepted.run_id).status
        payload["factory_calls"] = len(factory_calls)
        _emit(payload)
        await app.shutdown()

    asyncio.run(main())


def _worker_lock_hold() -> None:
    from rollo.workspace_lock import WorkspaceLock

    lock = WorkspaceLock(
        Path(os.environ["C03_LOCK_PATH"]),
        workspace_id=os.environ["C03_WORKSPACE_ID"],
        owner_id="holder",
    )
    with lock:
        _write_file(os.environ["C03_READY_FILE"], lock.key)
        deadline = time.monotonic() + 60
        while not Path(os.environ["C03_RELEASE_FILE"]).exists():
            if time.monotonic() > deadline:
                _emit({"scenario": "lock-hold", "held": True, "released": False})
                return
            time.sleep(0.02)
    _emit({"scenario": "lock-hold", "held": True, "released": True, "key": lock.key})


def _worker_lock_probe() -> None:
    from rollo.workspace_lock import WorkspaceLock, WorkspaceLockBusyError, workspace_lock_key

    workspace_id = os.environ["C03_WORKSPACE_ID"]
    lock = WorkspaceLock(
        Path(os.environ["C03_LOCK_PATH"]), workspace_id=workspace_id, owner_id="probe"
    )
    payload: dict[str, Any] = {
        "scenario": "lock-probe",
        "key": workspace_lock_key(workspace_id),
        "sibling_key": workspace_lock_key(os.environ.get("C03_SIBLING_WORKSPACE", "workspace-b")),
    }
    try:
        lock.acquire()
    except WorkspaceLockBusyError:
        payload["acquired"] = False
        payload["code"] = WorkspaceLockBusyError.code
    else:
        payload["acquired"] = True
        lock.release()
    _emit(payload)


_WORKERS = {
    "run-start-then-crash": _worker_run_start_then_crash,
    "recover": _worker_recover,
    "retry-same-command": _worker_retry_same_command,
    "quarantine-then-reconcile": _worker_quarantine_then_reconcile,
    "lock-hold": _worker_lock_hold,
    "lock-probe": _worker_lock_probe,
}


# --------------------------------------------------------------------------
# test side
# --------------------------------------------------------------------------


def _worker_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(_SRC_ROOT),
        # Never let a developer's .env inject real provider credentials.
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "C03_ROOT": str(tmp_path),
        "C03_RUNTIME": str(tmp_path / "runtime"),
        "C03_MARKER_DIR": str(tmp_path / "markers"),
        "C03_CRASH_AFTER": str(tmp_path / "crash.flag"),
        "C03_READY_FILE": str(tmp_path / "ready.flag"),
        "C03_RELEASE_FILE": str(tmp_path / "release.flag"),
    })
    env.update(extra)
    return env


def _spawn(tmp_path: Path, scenario: str, **extra: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(_WORKER_FILE), scenario],
        env=_worker_env(tmp_path, **extra),
        cwd=str(_REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _parse_emitted(stream: str) -> list[dict]:
    emitted = []
    for line in stream.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                emitted.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return emitted


def _collect(process: subprocess.Popen[str], timeout: float = 90.0) -> tuple[int, list[dict], str]:
    stdout, stderr = process.communicate(timeout=timeout)
    return process.returncode, _parse_emitted(stdout), stderr


def _wait_for(predicate, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _markers(tmp_path: Path, name: str) -> list[Path]:
    directory = tmp_path / "markers"
    return sorted(directory.glob(name)) if directory.exists() else []


def _crash_worker(tmp_path: Path, prompt: str = "crash-after-open") -> tuple[dict, str]:
    """Run the crashing worker to its barrier and return its handshake payload."""

    holder = _spawn(tmp_path, "run-start-then-crash", C03_PROMPT=prompt)
    try:
        assert _wait_for(lambda: bool(_markers(tmp_path, "agent-*"))), (
            "worker never constructed its agent"
        )
        assert _wait_for(lambda: (tmp_path / "crash.flag").exists()), (
            "worker never reached the canonical crash barrier"
        )
        holder.wait(timeout=30)
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=10)
        stdout = holder.stdout.read() if holder.stdout is not None else ""
        stderr = holder.stderr.read() if holder.stderr is not None else ""
    assert holder.returncode == CRASH_EXIT_CODE, stderr
    emitted = _parse_emitted(stdout)
    handshake = next(
        (item for item in emitted if item.get("scenario") == "run-start-then-crash"), None
    )
    assert handshake is not None, f"crashing worker produced no handshake: {stdout!r} {stderr!r}"
    return handshake, stderr


def _crashed_owner_id(tmp_path: Path) -> str:
    """Read the single owner row straight from the durable control store."""

    from rollo.application import ControlStore
    from rollo.project_context import ProjectContext

    context = ProjectContext.from_root(tmp_path, runtime_data_dir=tmp_path / "runtime")
    store = ControlStore(
        context.runtime_data_dir / "application" / context.workspace_id / "control.sqlite"
    )
    try:
        rows = store.owner_rows(context.workspace_id)
        assert len(rows) == 1, f"expected exactly one owner row, found {len(rows)}"
        return str(rows[0]["owner_id"])
    finally:
        store.close()


@pytest.mark.timeout(240)
def test_restart_after_open_without_terminal_never_reports_success(tmp_path: Path):
    """Crash barrier #2/#3 — canonical evidence exists, no terminal was committed.

    The restarting Application must read the canonical ledger, refuse to report
    success, quarantine the dead owner and never construct another agent.  The
    expected classification is the D14 row for "terminal not observed"
    (``interrupted`` / ``run_dispatch_not_observed``); the invocation identity is
    still recovered into the correlation block.
    """

    handshake, _ = _crash_worker(tmp_path, "crash-after-open")

    recovery = _spawn(
        tmp_path,
        "recover",
        C03_RUN_ID=handshake["run_id"],
        C03_SESSION_ID=handshake["session_id"],
    )
    exit_code, recovered, stderr = _collect(recovery)
    assert exit_code == 0, stderr
    payload = next(item for item in recovered if item["scenario"] == "recover")

    assert payload["status"] == "interrupted"
    assert payload["error_code"] == "run_dispatch_not_observed"
    assert payload["dispatch_intent"] is True
    assert payload["result"]["side_effect_count"] == 0
    assert payload["result"]["canonical_correlation"] == {
        "version": 1,
        "session_id": handshake["session_id"],
        "run_id": handshake["run_id"],
        "invocation_ids": ["inv-crash"],
        "turn_ids": ["turn-crash"],
        "tool_operations": [],
    }
    assert payload["agent_factory_calls"] == 0
    assert any(item["quarantine"] == 1 for item in payload["owners"])
    # Exactly one agent was ever constructed: the crashed one.  A second marker
    # would prove the restart replayed the dispatch.
    assert len(_markers(tmp_path, "agent-*")) == 1


@pytest.mark.timeout(240)
def test_restart_after_unpaired_tool_dispatch_is_uncertain_and_never_replayed(tmp_path: Path):
    """Crash barrier #3 — a tool was dispatched but its outcome was never observed.

    This is the duplication-risk shape: the run must project ``uncertain`` with
    ``tool_outcome_uncertain``, keep the side-effect count at zero, and the
    restart must not re-execute the tool.
    """

    handshake, _ = _crash_worker(tmp_path, "crash-after-dispatch")

    recovery = _spawn(
        tmp_path,
        "recover",
        C03_RUN_ID=handshake["run_id"],
        C03_SESSION_ID=handshake["session_id"],
    )
    exit_code, recovered, stderr = _collect(recovery)
    assert exit_code == 0, stderr
    payload = next(item for item in recovered if item["scenario"] == "recover")

    assert payload["status"] == "uncertain"
    assert payload["error_code"] == "tool_outcome_uncertain"
    assert payload["dispatch_intent"] is True
    assert payload["result"]["side_effect_count"] == 0
    assert payload["result"]["canonical_correlation"]["tool_operations"] == [
        {
            "operation_id": "op-crash",
            "provider_tool_call_id": "tc-crash",
            "tool_name": "run_shell",
            "canonical_args_hash": "args-crash",
        }
    ]
    assert payload["agent_factory_calls"] == 0
    assert any(item["quarantine"] == 1 for item in payload["owners"])
    assert len(_markers(tmp_path, "agent-*")) == 1


@pytest.mark.timeout(240)
def test_retry_of_accepted_command_converges_without_second_dispatch(tmp_path: Path):
    """Crash barrier #4 — the reply was lost after the command row committed.

    A retry of the same ``command_id`` must return the original run identity and
    create no second root dispatch, because the command row is the idempotency key.
    """

    handshake, _ = _crash_worker(tmp_path, "crash-after-dispatch")
    assert handshake["status"] == "queued", "the command must commit before dispatch"

    retry = _spawn(
        tmp_path,
        "retry-same-command",
        C03_SESSION_ID=handshake["session_id"],
        C03_PROMPT="crash-after-dispatch",
    )
    exit_code, retried, stderr = _collect(retry)
    assert exit_code == 0, stderr
    payload = next(item for item in retried if item["scenario"] == "retry-same-command")

    assert payload["run_id"] == handshake["run_id"]
    assert payload["agent_factory_calls"] == 0
    assert payload["error_code"] is None
    assert len(_markers(tmp_path, "agent-*")) == 1


@pytest.mark.timeout(240)
def test_crashed_root_releases_os_lock_but_quarantine_refuses_new_root(tmp_path: Path):
    """tasks 2.8 / 6.3 — physical lock release is not logical ownership release."""

    handshake, _ = _crash_worker(tmp_path, "crash-after-dispatch")
    owner_id = _crashed_owner_id(tmp_path)

    recovery = _spawn(
        tmp_path,
        "quarantine-then-reconcile",
        C03_SESSION_ID=handshake["session_id"],
        C03_OWNER_ID=owner_id,
    )
    exit_code, recovered, stderr = _collect(recovery)
    assert exit_code == 0, stderr
    payload = next(item for item in recovered if item["scenario"] == "quarantine-then-reconcile")

    assert payload["blocked_status"] == "rejected"
    assert payload["blocked_error_code"] == "owner_quarantine"
    assert payload["factory_calls_before_quarantine_clear"] == 0
    assert payload["owner_quarantine"] == 1
    assert payload["reconcile_status"] == "released"
    assert payload["reconcile_error_code"] is None
    assert payload["accepted_status"] == "queued"
    assert payload["accepted_error_code"] is None
    assert payload["final_status"] == "succeeded"
    assert payload["factory_calls"] == 1
    # Explicit reconcile is the only path to a new root, and the new root runs
    # its own command instead of replaying the crashed one.
    assert (tmp_path / "markers" / "side-effect.txt").read_text(encoding="utf-8").splitlines() == [
        "side-effect"
    ]


@pytest.mark.timeout(180)
def test_cross_process_workspace_lock_is_mutually_exclusive(tmp_path: Path):
    """tasks 3.1 / 6.3 — a second process must observe a busy owner."""

    lock_path = tmp_path / "locks" / "workspace.lock"
    ready = tmp_path / "ready.flag"
    hold = _spawn(
        tmp_path, "lock-hold", C03_LOCK_PATH=str(lock_path), C03_WORKSPACE_ID="workspace-a"
    )
    try:
        assert _wait_for(ready.exists), "holder never reported the acquired lock"
        holder_key = ready.read_text(encoding="utf-8")

        probe = _spawn(
            tmp_path,
            "lock-probe",
            C03_LOCK_PATH=str(lock_path),
            C03_WORKSPACE_ID="workspace-a",
            C03_SIBLING_WORKSPACE="workspace-b",
        )
        exit_code, probed, stderr = _collect(probe)
        assert exit_code == 0, stderr
        payload = next(item for item in probed if item["scenario"] == "lock-probe")
        assert payload["acquired"] is False
        assert payload["code"] == "owner_conflict"
        assert payload["key"] == holder_key, "the lock key must be recomputable cross-process"
        assert payload["sibling_key"] != holder_key, "distinct workspaces must not share a key"
    finally:
        (tmp_path / "release.flag").write_text("release", encoding="utf-8")

    exit_code, held, stderr = _collect(hold)
    assert exit_code == 0, stderr
    assert any(item.get("released") for item in held), f"holder output: {held!r} {stderr!r}"

    after = _spawn(
        tmp_path, "lock-probe", C03_LOCK_PATH=str(lock_path), C03_WORKSPACE_ID="workspace-a"
    )
    exit_code, probed, stderr = _collect(after)
    assert exit_code == 0, stderr
    final = next(item for item in probed if item["scenario"] == "lock-probe")
    assert final["acquired"] is True, "a released lock must be acquirable again"


if __name__ == "__main__":  # pragma: no cover - worker entry point
    _WORKERS[sys.argv[1]]()
