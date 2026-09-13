"""C03 — an observed dispatch with no canonical evidence must not report success.

Round-3 review finding (P0): ``Application._execute_run`` finalised every run
whose canonical ledger was empty as ``succeeded``, *bypassing*
``project_canonical_evidence`` entirely.  With ``dispatch_intent`` already set,
the frozen D14 mapping is ``interrupted``/``run_dispatch_not_observed`` — and the
recovery path in the same module already classifies it that way, so the live and
recovery paths disagreed about identical evidence.

The v3 convergence removed the owner/quarantine gate entirely: a dead process
holds nothing, so a crash is classified (``_recover_orphaned_runs``) and never
blocks the workspace.  The former ``owner_reconcile`` oracles are replaced by
oracles for that property.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from rollo.application import Application, ControlStore
from rollo.project_context import ProjectContext
from rollo.runtime_event import RuntimeEvent
from rollo.runtime_store import SQLiteRuntimeStore
from rollo.session import runtime_store_path


class _NonCanonicalAgent:
    """An agent double that owns a canonical ledger but never records to it.

    Owning the ledger is the point: the Application must then treat the ledger as
    authoritative, so an empty ledger after an observed dispatch is lost evidence
    rather than "no canonical layer at all".
    """

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self._runtime_store = kwargs.get("runtime_store")
        if self._runtime_store is None:
            self._owned_path = runtime_store_path(
                kwargs["runtime_session_id"], context=kwargs["project_context"]
            )
            self._runtime_store = SQLiteRuntimeStore(self._owned_path)

    def configure_runtime_store(self, store) -> None:
        if self._runtime_store is None:
            self._runtime_store = store

    def configure_runtime_identity(self, **kwargs) -> None:
        return None

    def configure_application_interactions(self, flag) -> None:
        return None

    def set_interaction_port(self, port) -> None:
        return None

    async def chat(self, prompt: str) -> None:
        # ``canonical-ok`` records the run terminal the way the shipped Agent
        # does.  Anything else stays silent, which is the lost-evidence shape.
        if prompt == "canonical-ok":
            _write_canonical_terminal(
                self._runtime_store, self.kwargs["runtime_session_id"], self.kwargs["runtime_run_id"]
            )
        return None

    def abort(self) -> None:
        return None

    async def aclose(self) -> None:
        store, self._runtime_store = self._runtime_store, None
        if store is not None:
            store.close()


def _write_canonical_terminal(store: SQLiteRuntimeStore, session_id: str, run_id: str) -> None:
    """Record an invocation open plus a completed run terminal.

    The canonical ledger is the only evidence the Application accepts, so a
    double that wants to be reported as ``succeeded`` has to record it here
    rather than have the Application assume it.
    """

    common = {
        "schema_version": 2,
        "session_id": session_id,
        "run_id": run_id,
        "invocation_id": "inv-fixture",
        "turn_id": "turn-fixture",
        "ts": 1,
        "partial": False,
        "author": "agent",
    }
    store.append(RuntimeEvent.from_dict({
        **common,
        "id": "fixture-invocation-opened",
        "role": "system",
        "content": {
            "kind": "invocation_opened",
            "protocol": "invocation_opened_v1",
            "route": {"provider": "fixture", "model": "fixture-model"},
            "configuration": {"attempt": 1},
            "root": {"kind": "agent"},
            "source": {"kind": "fresh"},
        },
    }))
    store.append(RuntimeEvent.from_dict({
        **common,
        "id": "fixture-run-terminal",
        "role": "model",
        "status": "completed",
        "actions": {"run_terminal": {"status": "completed"}},
    }))


def _invocation_opened(session_id: str, run_id: str) -> RuntimeEvent:
    return RuntimeEvent.from_dict({
        "schema_version": 2,
        "id": "e-open",
        "session_id": session_id,
        "run_id": run_id,
        "invocation_id": "inv-1",
        "turn_id": "turn-1",
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


def _run_terminal(session_id: str, run_id: str) -> RuntimeEvent:
    return RuntimeEvent.from_dict({
        "schema_version": 2,
        "id": "e-terminal",
        "session_id": session_id,
        "run_id": run_id,
        "invocation_id": "inv-1",
        "turn_id": "turn-1",
        "ts": 2,
        "partial": False,
        "role": "model",
        "author": "agent",
        "status": "completed",
        "actions": {"run_terminal": {"status": "completed"}},
    })


def test_empty_ledger_never_reports_success(tmp_path: Path):
    """dispatch_intent + zero canonical evidence -> interrupted, not succeeded.

    Regression oracle for the round-3 P0: the live path used to finalise an
    empty ledger as ``succeeded`` while this module's recovery path classified
    identical evidence as ``interrupted``/``run_dispatch_not_observed`` (frozen
    D14, barrier B2).  Success is never reported without evidence naming the run.
    """

    async def scenario():
        context = ProjectContext.from_root(tmp_path, runtime_data_dir=tmp_path / "runtime")
        app = Application(context, agent_factory=_NonCanonicalAgent)
        try:
            session = app.session_create("session-no-evidence").session_id
            store = SQLiteRuntimeStore(runtime_store_path(session, context=context))
            store.close()

            started = await app.run_start(
                session_id=session, prompt="hi", command_id="no-evidence-cmd"
            )
            final = await app.wait_run(started.run_id)
            assert final.status == "interrupted", final
            assert final.error_code == "run_dispatch_not_observed", final
            assert final.data["dispatch_intent"] is True, final
            assert final.data.get("result", {}).get("completed") is None, final
        finally:
            await app.shutdown()

    asyncio.run(scenario())


def test_ledger_evidence_is_still_respected(tmp_path: Path):
    """Positive control: when the ledger records a terminal, it is honoured."""

    async def scenario():
        context = ProjectContext.from_root(tmp_path, runtime_data_dir=tmp_path / "runtime")
        app = Application(context, agent_factory=_NonCanonicalAgent)
        try:
            session = app.session_create("session-with-evidence").session_id
            store = SQLiteRuntimeStore(runtime_store_path(session, context=context))
            store.close()

            started = await app.run_start(
                session_id=session, prompt="hi", command_id="with-evidence-cmd"
            )
            writer = SQLiteRuntimeStore(runtime_store_path(session, context=context))
            writer.append(_invocation_opened(session, started.run_id))
            writer.append(_run_terminal(session, started.run_id))
            writer.close()

            final = await app.wait_run(started.run_id)
            assert final.status == "succeeded", final
            assert final.error_code is None, final
        finally:
            await app.shutdown()

    asyncio.run(scenario())


def _seed_crashed_run(
    context: ProjectContext,
    *,
    session_id: str,
    run_id: str,
    dispatch_intent: int,
) -> ControlStore:
    """Create the durable state a process that died mid-run leaves behind."""

    store = ControlStore(
        context.runtime_data_dir / "application" / context.workspace_id / "control.sqlite"
    )
    now = "2026-01-01T00:00:00.000Z"
    store.connection.execute(
        "INSERT INTO runs(run_id,session_id,workspace_id,command_id,owner_pid,decision_generation,"
        "status,error_code,prompt_digest,dispatch_intent,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id, session_id, context.workspace_id, f"cmd-{run_id}", 999_999_999, 0,
            "running", None, "digest", dispatch_intent, now, now,
        ),
    )
    return store


def test_crashed_process_never_blocks_the_workspace(tmp_path: Path):
    """Crash recovery only classifies; the workspace stays immediately usable.

    This replaces the removed owner-quarantine oracles.  A dead process holds
    nothing, so the only durable effect of its crash is the D14 classification
    of the run it left behind - and no new request may be refused because of it.
    """

    context = ProjectContext.from_root(tmp_path, runtime_data_dir=tmp_path / "runtime")
    store = _seed_crashed_run(
        context, session_id="session-crashed", run_id="run-crashed", dispatch_intent=0
    )

    async def scenario():
        app = Application(context, agent_factory=_NonCanonicalAgent)
        try:
            # 1. D14: no dispatch intent -> interrupted/run_interrupted_before_dispatch.
            row = app.control.run("run-crashed")
            assert row["status"] == "interrupted", dict(row)
            assert row["error_code"] == "run_interrupted_before_dispatch", dict(row)
            # 2. The classification refuses nothing: the next run of this
            #    workspace is accepted and executed without any operator action.
            session = app.session_create("session-after-crash").session_id
            started = await app.run_start(
                session_id=session, prompt="canonical-ok", command_id="after-crash-cmd"
            )
            assert started.error_code is None, started
            assert (await app.wait_run(started.run_id)).status == "succeeded"
        finally:
            await app.shutdown()

    try:
        asyncio.run(scenario())
    finally:
        store.close()


def test_stale_session_lease_is_reclaimed_without_operator_action(tmp_path: Path):
    """A lease whose holder is gone is stale evidence, not a permanent lock.

    v2 made a crashed owner quarantine the workspace with no automatic expiry
    and no reachable clearing path outside its CLI/TUI -- the defect this
    convergence removes.
    """

    context = ProjectContext.from_root(tmp_path, runtime_data_dir=tmp_path / "runtime")
    session_id = "session-stale-lease"
    store = ControlStore(
        context.runtime_data_dir / "application" / context.workspace_id / "control.sqlite"
    )
    now = "2026-01-01T00:00:00.000Z"
    with store.transaction() as db:
        db.execute(
            "INSERT INTO session_leases(session_id,workspace_id,owner_id,pid,acquired_at,updated_at) VALUES(?,?,?,?,?,?)",
            (session_id, context.workspace_id, "owner-dead", 999_999_999, now, now),
        )

    async def scenario():
        app = Application(context, agent_factory=_NonCanonicalAgent, control_store=store)
        try:
            session = app.session_create(session_id).session_id
            started = await app.run_start(session_id=session, prompt="canonical-ok", command_id="stale-cmd")
            assert started.error_code is None, started
            assert (await app.wait_run(started.run_id)).status == "succeeded"
            # The lease was adopted and then released by its new holder.
            assert app.control.session_lease(session) is None
        finally:
            await app.shutdown()

    try:
        asyncio.run(scenario())
    finally:
        store.close()
