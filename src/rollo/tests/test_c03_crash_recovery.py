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

import asyncio
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
        if prompt == "hold":
            # Hold a live run (and therefore its session lease) until the test
            # releases it, so a second process can observe the refusal.
            release = Path(os.environ["C03_RELEASE_FILE"])
            deadline = time.monotonic() + 120
            while not release.exists():
                if time.monotonic() > deadline:
                    return
                await asyncio.sleep(0.02)

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

    from rollo.application import Application, _process_alive

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
        # v3 evidence: the crashed holder left a lease row behind, it is stale
        # (its process is gone), and the recovery scan took nothing over - it
        # only classified the run.
        lease = app.control.session_lease(session_id)
        payload["lease"] = (
            {
                "owner_id": lease["owner_id"],
                "pid": int(lease["pid"]),
                "pid_alive": _process_alive(int(lease["pid"])),
            }
            if lease is not None
            else None
        )
        payload["lease_held_by_recovery_process"] = app._own_session(session_id)
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


def _worker_reclaim_after_crash() -> None:
    """A crashed session is immediately reusable; there is no reconcile step."""

    import asyncio

    from rollo.application import Application

    factory_calls: list[str] = []

    def factory(**kwargs: Any) -> Any:
        factory_calls.append("created")
        return _RecordingAgent(**kwargs)

    async def main() -> None:
        context = _context(Path(os.environ["C03_ROOT"]))
        app = Application(context, agent_factory=factory)
        payload: dict[str, Any] = {"scenario": "reclaim-after-crash"}
        session_id = os.environ["C03_SESSION_ID"]
        try:
            lease = app.control.session_lease(session_id)
            assert lease is not None, "the crashed holder's lease must still be readable"
            payload["lease_owner_id"] = lease["owner_id"]
            payload["lease_pid"] = int(lease["pid"])
            payload["factory_calls_before"] = len(factory_calls)

            # No operator action: the stale lease of a dead process is adopted.
            accepted = await app.run_start(
                session_id=session_id,
                prompt="side-effect",
                command_id="after-crash-command",
            )
            payload["accepted_status"] = accepted.status
            payload["accepted_error_code"] = accepted.error_code
            if accepted.run_id is not None:
                await app.wait_run(accepted.run_id)
                payload["final_status"] = app.run_status(accepted.run_id).status
            payload["lease_released"] = app.control.session_lease(session_id) is None
            payload["factory_calls"] = len(factory_calls)
        finally:
            _emit(payload)
            await app.shutdown()

    asyncio.run(main())


def _worker_session_hold() -> None:
    """Hold one session's lease with a live run until the release flag appears."""

    import asyncio

    from rollo.application import Application

    async def main() -> None:
        context = _context(Path(os.environ["C03_ROOT"]))
        app = Application(context, agent_factory=_RecordingAgent)
        try:
            session = app.session_create("lease-holder").session_id
            response = await app.run_start(
                session_id=session, prompt="hold", command_id="holder-command"
            )
            _write_file(os.environ["C03_READY_FILE"], session)
            _emit({
                "scenario": "session-hold",
                "session_id": session,
                "run_id": response.run_id,
                "status": response.status,
                "error_code": response.error_code,
                "pid": os.getpid(),
            })
            await app.wait_run(response.run_id)
            _emit({
                "scenario": "session-hold",
                "released": True,
                "run_id": response.run_id,
                "status": app.run_status(response.run_id).status,
            })
        finally:
            await app.shutdown()

    asyncio.run(main())


def _worker_session_probe() -> None:
    """Ask the same workspace for the held session and for a fresh one."""

    import asyncio

    from rollo.application import Application

    factory_calls: list[str] = []

    def factory(**kwargs: Any) -> Any:
        factory_calls.append("created")
        return _RecordingAgent(**kwargs)

    async def main() -> None:
        context = _context(Path(os.environ["C03_ROOT"]))
        app = Application(context, agent_factory=factory)
        payload: dict[str, Any] = {"scenario": "session-probe"}
        try:
            busy = await app.run_start(
                session_id=os.environ["C03_SESSION_ID"],
                prompt="side-effect",
                command_id="probe-busy-command",
            )
            payload["busy_status"] = busy.status
            payload["busy_error_code"] = busy.error_code
            payload["busy_holder"] = (busy.data or {}).get("holder_owner_id")

            other = await app.run_start(
                session_id=os.environ["C03_OTHER_SESSION"],
                prompt="side-effect",
                command_id="probe-other-command",
            )
            payload["other_status"] = other.status
            payload["other_error_code"] = other.error_code
            if other.run_id is not None:
                await app.wait_run(other.run_id)
                payload["other_final_status"] = app.run_status(other.run_id).status
        finally:
            payload["factory_calls"] = len(factory_calls)
            _emit(payload)
            await app.shutdown()

    asyncio.run(main())


_WORKERS = {
    "run-start-then-crash": _worker_run_start_then_crash,
    "recover": _worker_recover,
    "retry-same-command": _worker_retry_same_command,
    "reclaim-after-crash": _worker_reclaim_after_crash,
    "session-hold": _worker_session_hold,
    "session-probe": _worker_session_probe,
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


@pytest.mark.timeout(240)
def test_restart_after_open_without_terminal_never_reports_success(tmp_path: Path):
    """Crash barrier #2/#3 — canonical evidence exists, no terminal was committed.

    The restarting Application must read the canonical ledger, refuse to report
    success, classify the dead process' run and never construct another agent.
    The expected classification is the D14 row for "terminal not observed"
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
    # The crashed holder's lease is stale evidence: its process is gone and the
    # recovery scan classified the run without taking the session over.
    assert payload["lease"] is not None
    assert payload["lease"]["pid_alive"] is False, payload["lease"]
    assert payload["lease_held_by_recovery_process"] is False
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
    assert payload["lease"] is not None
    assert payload["lease"]["pid_alive"] is False, payload["lease"]
    assert payload["lease_held_by_recovery_process"] is False
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
def test_crashed_session_is_reclaimed_without_reconcile(tmp_path: Path):
    """v3: a crash blocks nothing — no quarantine, no manual reconcile.

    The second process finds the dead holder's lease row, adopts it in the same
    ``run.start`` and runs its own command instead of replaying the crashed one.
    """

    handshake, _ = _crash_worker(tmp_path, "crash-after-dispatch")

    recovery = _spawn(
        tmp_path,
        "reclaim-after-crash",
        C03_SESSION_ID=handshake["session_id"],
    )
    exit_code, recovered, stderr = _collect(recovery)
    assert exit_code == 0, stderr
    payload = next(item for item in recovered if item["scenario"] == "reclaim-after-crash")

    # The stale lease is visible (evidence) but not a gate: its recorded pid is
    # the crashed holder's, which is exactly what makes it adoptable.
    assert payload["lease_owner_id"], payload
    assert payload["lease_pid"] == handshake["pid"], payload
    assert payload["factory_calls_before"] == 0
    assert payload["accepted_status"] == "queued", payload
    assert payload["accepted_error_code"] is None, payload
    assert payload["final_status"] == "succeeded", payload
    assert payload["factory_calls"] == 1, payload
    assert payload["lease_released"] is True, payload
    # The new process ran its own command exactly once; the crashed dispatch was
    # never replayed.
    assert (tmp_path / "markers" / "side-effect.txt").read_text(encoding="utf-8").splitlines() == [
        "side-effect"
    ]


@pytest.mark.timeout(240)
def test_cross_process_session_lease_blocks_only_that_session(tmp_path: Path):
    """v3: the only execution mutex is the session lease.

    Two real processes on one workspace: the second is refused with
    ``session_busy`` for the session the first one holds and is accepted for a
    different session of that same workspace (workspace-level exclusion no
    longer exists).
    """

    ready = tmp_path / "ready.flag"
    holder = _spawn(tmp_path, "session-hold")
    try:
        assert _wait_for(ready.exists), "holder never acquired its session lease"
        held_session = ready.read_text(encoding="utf-8").strip()
        assert held_session, "holder never reported its session id"

        probe = _spawn(
            tmp_path,
            "session-probe",
            C03_SESSION_ID=held_session,
            C03_OTHER_SESSION="probe-other-session",
        )
        exit_code, probed, stderr = _collect(probe)
        assert exit_code == 0, stderr
        payload = next(item for item in probed if item["scenario"] == "session-probe")

        assert payload["busy_status"] == "rejected", payload
        assert payload["busy_error_code"] == "session_busy", payload
        assert payload["busy_holder"], payload
        assert payload["other_status"] == "queued", payload
        assert payload["other_error_code"] is None, payload
        assert payload["other_final_status"] == "succeeded", payload
        assert payload["factory_calls"] == 1, payload
        # Only the accepted session produced a side effect.
        assert (tmp_path / "markers" / "side-effect.txt").read_text(encoding="utf-8").splitlines() == [
            "side-effect"
        ]
    finally:
        (tmp_path / "release.flag").write_text("release", encoding="utf-8")

    exit_code, held, stderr = _collect(holder)
    assert exit_code == 0, stderr
    assert any(item.get("released") for item in held), f"holder output: {held!r} {stderr!r}"


if __name__ == "__main__":  # pragma: no cover - worker entry point
    _WORKERS[sys.argv[1]]()
