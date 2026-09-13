"""C03 — an observed dispatch with no canonical evidence must not report success.

Round-3 review finding (P0): ``Application._execute_run`` finalised every run
whose canonical ledger was empty as ``succeeded``, *bypassing*
``project_canonical_evidence`` entirely.  With ``dispatch_intent`` already set,
the frozen D14 mapping is ``interrupted``/``run_dispatch_not_observed`` — and the
recovery path in the same module already classifies it that way, so the live and
recovery paths disagreed about identical evidence.

Round-3 also found the guard's ``owner_reconcile`` action dispatch had no oracle
at all: mutants that turned ``inspect`` into a release, or that dropped the
action whitelist, kept the whole suite green while a probe showed a *misspelled*
action clearing a crashed owner's quarantine and handing over the workspace.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

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
        return None

    def abort(self) -> None:
        return None

    async def aclose(self) -> None:
        store, self._runtime_store = self._runtime_store, None
        if store is not None:
            store.close()


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


@pytest.mark.skip(
    reason=(
        "OPEN P0 (round-3 review C03-ADV-MUTATION-REVIEW-20260913-03): with "
        "`dispatch_intent` observed and an empty canonical ledger, "
        "`Application._execute_run` finalises the run as `succeeded` and never "
        "consults `project_canonical_evidence`. The frozen D14 row "
        "(runtime-control-persistence/spec.md:89, barrier B2) requires "
        "`interrupted`/`run_dispatch_not_observed`, which is also what this same "
        "module's recovery path returns for identical evidence. Un-skip once the "
        "agent_factory/ledger contract is decided."
    )
)
def test_empty_ledger_never_reports_success(tmp_path: Path):
    """dispatch_intent + zero canonical evidence -> interrupted, not succeeded.

    Enable this as the regression oracle for the P0 above. It is skipped, not
    deleted, so the gap stays visible and the fix has a ready test.
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


def _seed_crashed_owner(context: ProjectContext) -> ControlStore:
    store = ControlStore(
        context.runtime_data_dir / "application" / context.workspace_id / "control.sqlite"
    )
    store.insert_owner(
        owner_id="owner-crashed", workspace_id=context.workspace_id, generation=1, lock_key="k"
    )
    store.connection.execute(
        "UPDATE owners SET process_id=999999999, status='uncertain', quarantine=1 "
        "WHERE owner_id='owner-crashed'"
    )
    return store


def test_owner_reconcile_rejects_unknown_action_without_touching_quarantine(tmp_path: Path):
    """A misspelled action must never clear the quarantine (round-3 M1)."""

    context = ProjectContext.from_root(tmp_path, runtime_data_dir=tmp_path / "runtime")
    app = Application(context, agent_factory=lambda **kwargs: None)
    store = _seed_crashed_owner(context)
    try:
        response = app.owner_reconcile(
            owner_id="owner-crashed", generation=1, action="relese", evidence={"why": "typo"}
        )
        assert response.status == "rejected", response
        assert response.error_code == "invalid_reconcile_action", response
        row = store.owner("owner-crashed")
        assert row["status"] == "uncertain", dict(row)
        assert int(row["quarantine"]) == 1, dict(row)
    finally:
        store.close()
        asyncio.run(app.shutdown())


def test_owner_reconcile_inspect_is_read_only(tmp_path: Path):
    """``inspect`` must never mutate the owner row (round-3 M9)."""

    context = ProjectContext.from_root(tmp_path, runtime_data_dir=tmp_path / "runtime")
    app = Application(context, agent_factory=lambda **kwargs: None)
    store = _seed_crashed_owner(context)
    try:
        response = app.owner_reconcile(
            owner_id="owner-crashed", generation=1, action="inspect", evidence={"why": "look"}
        )
        assert response.status == "inspected", response
        assert response.result == "ok", response
        row = store.owner("owner-crashed")
        assert row["status"] == "uncertain", dict(row)
        assert int(row["quarantine"]) == 1, dict(row)
    finally:
        store.close()
        asyncio.run(app.shutdown())


def test_owner_reconcile_requires_evidence_and_bound_generation(tmp_path: Path):
    """``owner_evidence_required`` and ``owner_identity_conflict`` had no oracle."""

    context = ProjectContext.from_root(tmp_path, runtime_data_dir=tmp_path / "runtime")
    app = Application(context, agent_factory=lambda **kwargs: None)
    store = _seed_crashed_owner(context)
    try:
        no_evidence = app.owner_reconcile(
            owner_id="owner-crashed", generation=1, action="release", evidence={}
        )
        assert no_evidence.error_code == "owner_evidence_required", no_evidence

        wrong_generation = app.owner_reconcile(
            owner_id="owner-crashed", generation=99, action="release", evidence={"why": "x"}
        )
        assert wrong_generation.error_code == "owner_identity_conflict", wrong_generation

        unknown_owner = app.owner_reconcile(
            owner_id="owner-unknown", generation=1, action="release", evidence={"why": "x"}
        )
        assert unknown_owner.error_code == "owner_identity_conflict", unknown_owner

        # None of the refusals may have released the workspace.
        row = store.owner("owner-crashed")
        assert row["status"] == "uncertain", dict(row)
        assert int(row["quarantine"]) == 1, dict(row)
    finally:
        store.close()
        asyncio.run(app.shutdown())
