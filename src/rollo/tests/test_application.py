from __future__ import annotations

import asyncio
import hashlib
import math
import sys
from pathlib import Path

import pytest

from rollo.application import (
    Application,
    ControlStore,
    canonical_json_bytes,
    full_sha256,
    params_digest,
    project_canonical_evidence,
)
from rollo.interactions import InteractionKind, InteractionRegistry, InteractionRequest
from rollo.project_context import ProjectContext


class _FakeAgent:
    instances: list["_FakeAgent"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.interaction_registry = InteractionRegistry()
        self.interaction_port = kwargs.get("interaction_port")
        self.aborted = False
        self.__class__.instances.append(self)

    def set_interaction_port(self, port):
        self.interaction_port = port

    def configure_runtime_store(self, store):
        # A caller-supplied agent (the CLI seam) receives the store here rather
        # than through the constructor, so it must accept it to be able to
        # record canonical evidence.
        self._runtime_store = store

    def configure_runtime_identity(self, *, session_id, run_id):
        # The Application supplies the run identity for each run; a reused agent
        # therefore learns it here, not from its constructor.
        self.runtime_session_id = session_id
        self.runtime_run_id = run_id

    async def chat(self, prompt: str):
        if prompt == "wait":
            request = InteractionRequest(
                request_id="request-1",
                kind=InteractionKind.APPROVAL,
                session_id=self.kwargs["runtime_session_id"],
                run_id=self.kwargs["runtime_run_id"],
                params_digest="digest",
                tool_call_id=None,
                tool_name=None,
            )
            self.interaction_registry.open(request)
            await self.interaction_port.request(request)
            return
        # The Application reports a run as ``succeeded`` only from canonical
        # evidence, so a double that expects success has to record the terminal
        # itself.  Recording nothing is the lost-evidence shape.
        store = self.kwargs.get("runtime_store") or getattr(self, "_runtime_store", None)
        session_id = self.kwargs.get("runtime_session_id") or getattr(self, "runtime_session_id", None)
        run_id = self.kwargs.get("runtime_run_id") or getattr(self, "runtime_run_id", None)
        if store is not None and session_id and run_id:
            _write_canonical_terminal(store, session_id, run_id)

    def abort(self):
        self.aborted = True

    async def aclose(self):
        return None


def _write_canonical_terminal(store, session_id: str, run_id: str) -> None:
    """Record an invocation open plus a completed run terminal.

    The store is re-opened per call: a reused agent outlives a single run, and
    the Application may have closed or replaced the handle it handed over.
    """

    from rollo.runtime_event import RuntimeEvent
    from rollo.runtime_store import SQLiteRuntimeStore

    if store is None:
        return
    database = getattr(store, "database", None)
    if database is not None:
        try:
            store = SQLiteRuntimeStore(database)
        except Exception:
            return

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


def test_application_workspace_session_and_idempotent_run(tmp_path: Path):
    async def scenario():
        context = ProjectContext.from_root(tmp_path, runtime_data_dir=tmp_path / "runtime")
        app = Application(context, agent_factory=_FakeAgent)
        session = app.session_create("session-a").session_id
        first = await app.run_start(session_id=session, prompt="hello", command_id="cmd-1")
        second = await app.run_start(session_id=session, prompt="hello", command_id="cmd-1")
        assert first.run_id == second.run_id
        assert (await app.wait_run(first.run_id)).status == "succeeded"
        assert len(app.session_list().data["sessions"]) == 1
        await app.shutdown()

    asyncio.run(scenario())


def test_application_reuses_existing_agent_across_repl_runs(tmp_path: Path):
    """A caller-supplied agent serves consecutive runs in one session.

    The runs are terminal but ``interrupted`` rather than ``succeeded``: this
    double cannot attribute its canonical events to the current run id, and the
    Application reports success only from evidence that names the run.  That is
    the intended conservatism -- a run is never reported successful without
    matching evidence -- so the assertion here is the honest terminal, not the
    optimistic one.
    """

    async def scenario():
        context = ProjectContext.from_root(tmp_path, runtime_data_dir=tmp_path / "runtime")
        agent = _FakeAgent(runtime_session_id="session-reuse", runtime_run_id="unused")
        app = Application(context, existing_agent=agent)
        session = app.session_create("session-reuse").session_id
        first = await app.run_start(session_id=session, prompt="one", command_id="cmd-reuse-1")
        first_status = (await app.wait_run(first.run_id)).status
        assert first_status == "interrupted"
        second = await app.run_start(session_id=session, prompt="two", command_id="cmd-reuse-2")
        second_status = (await app.wait_run(second.run_id)).status
        assert second_status == "interrupted"
        # Two distinct runs in the same session, both terminal, no replay.
        assert first.run_id != second.run_id
        assert len(app.control.runs_for_session(session)) == 2
        await app.shutdown()

    asyncio.run(scenario())


def test_run_start_control_commit_failure_never_dispatches(tmp_path: Path):
    async def scenario():
        context = ProjectContext.from_root(tmp_path, runtime_data_dir=tmp_path / "runtime")
        app = Application(context, agent_factory=_FakeAgent)
        session = app.session_create("session-fault").session_id
        original = app.control.insert_run

        def fail(*args, **kwargs):
            raise RuntimeError("injected commit failure")

        app.control.insert_run = fail  # type: ignore[method-assign]
        response = await app.run_start(session_id=session, prompt="never dispatch", command_id="cmd-fault")
        assert response.error_code == "control_commit_error"
        assert not app._tasks
        # The failure is still before the dispatch barrier, so the session
        # lease taken for this command must be handed back for a later retry.
        assert app._session_leases == set()
        assert app.control.session_lease(session) is None
        assert app.control.run_for_command(session, "cmd-fault") is None
        app.control.insert_run = original  # type: ignore[method-assign]
        await app.shutdown()

    asyncio.run(scenario())


def test_plan_approval_uses_application_identity_metadata(tmp_path: Path):
    async def scenario():
        from rollo.agent import Agent
        from rollo.interactions import InteractionReply

        context = ProjectContext.from_root(tmp_path, runtime_data_dir=tmp_path / "runtime")
        seen = {}

        class Approver:
            async def request(self, request):
                seen["request"] = request
                return InteractionReply(
                    request_id=request.request_id,
                    approved=True,
                    params_digest=request.params_digest,
                )

        agent = Agent(project_context=context, api_key="fixture-key", interaction_port=Approver())
        agent.configure_application_interactions(True)
        agent.permission_mode = "plan"
        result = await agent._execute_plan_mode_tool("exit_plan_mode")
        assert "approved" in result.lower()
        request = seen["request"]
        assert request.metadata == {"plan_approval": True}
        assert request.tool_name == "plan_approval"
        assert request.plan_digest and len(request.plan_digest) == 64
        await agent.aclose()

    asyncio.run(scenario())


def test_control_pending_redacts_sensitive_input(tmp_path: Path):
    store = ControlStore(tmp_path / "control.sqlite")
    request = InteractionRequest(
        request_id="secret-request",
        kind=InteractionKind.APPROVAL,
        session_id="s",
        run_id="r",
        params_digest="d",
        tool_input={"api_key": "secret-value", "nested": {"password": "pw"}},
    )
    store.insert_pending(request, workspace_id="w", process_id=1)
    raw = str(store.pending("secret-request")["tool_input_json"])
    assert "secret-value" not in raw and "pw" not in raw
    assert "[REDACTED]" in raw
    store.close()


def test_application_interaction_response_is_bound_and_resolves_once(tmp_path: Path):
    async def scenario():
        context = ProjectContext.from_root(tmp_path, runtime_data_dir=tmp_path / "runtime")
        
        class HoldingPort:
            async def request(self, request):
                await asyncio.Event().wait()

        app = Application(context, agent_factory=_FakeAgent, interaction_port=HoldingPort())
        session = app.session_create("session-b").session_id
        started = await app.run_start(session_id=session, prompt="wait", command_id="cmd-2")
        for _ in range(100):
            if app.control.pending("request-1") is not None:
                break
            await asyncio.sleep(0.005)
        reply = await app.interaction_respond(
            request_id="request-1",
            session_id=session,
            run_id=started.run_id,
            tool_call_id=None,
            tool_name=None,
            tool_input=None,
            plan_id=None,
            plan_digest=None,
            params_digest="digest",
            approved=True,
        )
        assert reply.status == "resolved"
        # The agent resumes only because the approval arrived, and it is the
        # agent -- not the Application -- that records the run terminal.
        _write_canonical_terminal(
            app._stores[session], session, started.run_id
        )
        assert (await app.wait_run(started.run_id)).status == "succeeded"
        late = await app.interaction_respond(
            request_id="request-1",
            session_id=session,
            run_id=started.run_id,
            tool_call_id=None,
            tool_name=None,
            tool_input=None,
            plan_id=None,
            plan_digest=None,
            params_digest="digest",
            approved=True,
        )
        assert late.error_code in {"interaction_interrupted", "interaction_binding_error"}
        await app.shutdown()

    asyncio.run(scenario())


def test_application_digest_is_full_sha256():
    digest = params_digest(
        session_id="s",
        run_id="r",
        request_id="q",
        tool_call_id=None,
        tool_name=None,
        tool_input=None,
        plan_id=None,
        plan_digest_value=None,
    )
    assert len(digest) == 64
    assert digest == digest.lower()


def test_canonical_json_bytes_is_plain_canonical_json():
    """v3 digests hash plain canonical JSON, not RFC 8785 JCS.

    Sorted keys, no insignificant whitespace and verbatim UTF-8 are the whole
    contract now; number spelling follows ``json.dumps`` (so ``1e20`` stays
    ``1e+20`` instead of being expanded to an integer literal).  Non-finite
    numbers stay rejected: emitting bare ``NaN``/``Infinity`` would write
    invalid JSON into the control records and let an unreproducible value into
    an approval digest.
    """

    assert canonical_json_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'
    assert canonical_json_bytes({"k": "中文"}) == '{"k":"中文"}'.encode("utf-8")
    assert canonical_json_bytes([1, {"z": [True, None]}, "a"]) == b'[1,{"z":[true,null]},"a"]'
    assert canonical_json_bytes(1e20) == b"1e+20"
    for value in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError):
            canonical_json_bytes(value)
    with pytest.raises(ValueError):
        canonical_json_bytes({"nested": [math.nan]})
    assert full_sha256({"a": 1}) == hashlib.sha256(b'{"a":1}').hexdigest()
    assert params_digest(
        session_id="s", run_id="r", request_id=None, tool_call_id=None,
        tool_name=None, tool_input=None, plan_id=None,
    ) == full_sha256({
        "session_id": "s", "run_id": "r", "request_id": None, "tool_call_id": None,
        "tool_name": None, "tool_input": None, "plan_id": None, "plan_digest": None,
    })


def test_projection_pairs_multiple_tool_operations_and_detects_identity_faults():
    def event(actions=None, *, status=None, invocation="i", turn="t"):
        return {
            "session_id": "s",
            "run_id": "r",
            "invocation_id": invocation,
            "turn_id": turn,
            "actions": actions or {},
            "status": status,
        }

    dispatch_a = {"operation_id": "op-a", "provider_tool_call_id": "tc-a", "tool_name": "read_file", "canonical_args_hash": "ha"}
    dispatch_b = {"operation_id": "op-b", "provider_tool_call_id": "tc-b", "tool_name": "run_shell", "canonical_args_hash": "hb"}
    events = [
        event({"tool_dispatch": dispatch_a}),
        event({"tool_dispatch": dispatch_b}),
        event({"tool_outcome": {**dispatch_a, "success": True, "executed": True}}),
        event({"tool_outcome": {**dispatch_b, "success": True, "executed": True}}, status="completed"),
    ]
    projected = project_canonical_evidence(events, session_id="s", run_id="r", side_effect_count=2)
    assert projected.status == "succeeded"
    assert [item["operation_id"] for item in projected.correlation["tool_operations"]] == ["op-a", "op-b"]
    assert projected.side_effect_count == 2

    missing = project_canonical_evidence([event({"tool_dispatch": {"operation_id": "op"}})], session_id="s", run_id="r", side_effect_count=9)
    assert (missing.status, missing.error_code, missing.side_effect_count) == ("uncertain", "canonical_identity_missing", 0)
    conflict = project_canonical_evidence(
        [event({"tool_dispatch": dispatch_a}), event({"tool_dispatch": {**dispatch_a, "tool_name": "write_file"}})],
        session_id="s", run_id="r", side_effect_count=1,
    )
    assert (conflict.status, conflict.error_code, conflict.side_effect_count) == ("uncertain", "canonical_identity_conflict", 1)
