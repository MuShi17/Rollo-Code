"""C03 — negative oracle for ``interaction.respond`` binding.

The positive path was covered, but every *rejection* path had no oracle: a
mutant that deletes the row-binding gate, the bridge lookup or the C02 registry
delegation kept the whole suite green.  These cases pin each refusal to its own
error code so a tampered reply can never be mistaken for an accepted one.

The rule under test: an approval is authorised only when the reply reproduces
the complete immutable identity of the persisted pending request — session, run,
tool call, tool name, tool input and the full params/plan digests — and only the
bridge that actually owns the waiting Future may resolve it.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from rollo.application import Application, ControlStore
from rollo.interactions import InteractionKind, InteractionRequest
from rollo.project_context import ProjectContext

_DIGEST = "b" * 64
_PLAN_DIGEST = "c" * 64


class _WaitingAgent:
    """Opens one approval request through the injected interaction port."""

    def __init__(self, **kwargs: Any) -> None:
        from rollo.interactions import InteractionRegistry

        self.kwargs = kwargs
        self.interaction_registry = InteractionRegistry()
        self.interaction_port = kwargs.get("interaction_port")

    def set_interaction_port(self, port: Any) -> None:
        self.interaction_port = port

    async def chat(self, prompt: str) -> None:
        if prompt != "wait":
            return
        request = InteractionRequest(
            request_id="request-1",
            kind=InteractionKind.APPROVAL,
            session_id=self.kwargs["runtime_session_id"],
            run_id=self.kwargs["runtime_run_id"],
            params_digest=_DIGEST,
            tool_call_id="call-1",
            tool_name="write_file",
            tool_input={"path": "a.txt"},
            plan_id="plan-1",
            plan_digest=_PLAN_DIGEST,
        )
        self.interaction_registry.open(request)
        await self.interaction_port.request(request)

    def abort(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


class _HoldingPort:
    """Delegate that never answers, so the Future stays pending."""

    async def request(self, request: InteractionRequest) -> Any:
        await asyncio.Event().wait()


def _base_reply(session_id: str, run_id: str) -> dict[str, Any]:
    return {
        "request_id": "request-1",
        "session_id": session_id,
        "run_id": run_id,
        "tool_call_id": "call-1",
        "tool_name": "write_file",
        "tool_input": {"path": "a.txt"},
        "plan_id": "plan-1",
        "plan_digest": _PLAN_DIGEST,
        "params_digest": _DIGEST,
        "approved": True,
    }


async def _pending_context(tmp_path):
    context = ProjectContext.from_root(tmp_path, runtime_data_dir=tmp_path / "runtime")
    app = Application(context, agent_factory=_WaitingAgent, interaction_port=_HoldingPort())
    session = app.session_create("session-bind").session_id
    started = await app.run_start(session_id=session, prompt="wait", command_id="bind-cmd")
    for _ in range(200):
        if app.control.pending("request-1") is not None:
            break
        await asyncio.sleep(0.005)
    assert app.control.pending("request-1") is not None
    return app, session, started.run_id


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param({"params_digest": "d" * 64}, id="tampered-params-digest"),
        pytest.param({"plan_digest": "e" * 64}, id="tampered-plan-digest"),
        pytest.param({"plan_id": "plan-2"}, id="tampered-plan-id"),
        pytest.param({"tool_input": {"path": "b.txt"}}, id="tampered-tool-input"),
        pytest.param({"tool_name": "run_shell"}, id="tampered-tool-name"),
        pytest.param({"tool_call_id": "call-2"}, id="tampered-tool-call-id"),
        pytest.param({"session_id": "other-session"}, id="tampered-session"),
        pytest.param({"run_id": "other-run"}, id="tampered-run"),
        pytest.param({"metadata": {"plan_approval": True}}, id="tampered-metadata"),
    ],
)
def test_tampered_reply_is_rejected_and_the_run_stays_waiting(tmp_path, mutation):
    async def scenario():
        app, _session, run_id = await _pending_context(tmp_path)
        try:
            payload = _base_reply("unused", run_id)
            payload["session_id"] = app.control.pending("request-1")["session_id"]
            payload.update(mutation)
            response = await app.interaction_respond(**payload)
            assert response.status == "rejected", response
            assert response.error_code == "interaction_binding_error", response
            # The refusal must not consume the request: the tool stays unapproved.
            assert app.control.pending("request-1")["status"] == "pending"
            assert app.run_status(run_id).status != "succeeded"
        finally:
            await app.shutdown()

    asyncio.run(scenario())


def test_reply_omitting_tool_identity_is_rejected_by_the_row_binding(tmp_path):
    """A bare ``approved`` reply must not be accepted for a tool approval.

    This is the case only the row-level binding catches: a reply that omits
    ``tool_name``/``tool_input`` entirely would be filled in from the request by
    the C02 port's legacy-adapter path, so the registry alone would accept it.
    """

    async def scenario():
        app, _session, run_id = await _pending_context(tmp_path)
        try:
            response = await app.interaction_respond(
                request_id="request-1",
                session_id=app.control.pending("request-1")["session_id"],
                run_id=run_id,
                approved=True,
            )
            assert response.status == "rejected", response
            assert response.error_code == "interaction_binding_error", response
            assert app.control.pending("request-1")["status"] == "pending"
        finally:
            await app.shutdown()

    asyncio.run(scenario())


def test_reply_only_resolvable_by_the_bridge_that_owns_the_future(tmp_path):
    """A second Application sharing the control store must not answer for it."""

    async def scenario():
        app, session, run_id = await _pending_context(tmp_path)
        control = ControlStore(
            app.context.runtime_data_dir
            / "application"
            / app.context.workspace_id
            / "control.sqlite"
        )
        foreign = Application(
            app.context, agent_factory=_WaitingAgent, interaction_port=_HoldingPort(),
            control_store=control,
        )
        try:
            response = await foreign.interaction_respond(**_base_reply(session, run_id))
            assert response.status == "rejected", response
            assert response.error_code == "interaction_interrupted", response
            assert app.control.pending("request-1")["status"] == "pending"
        finally:
            await foreign.shutdown()
            await app.shutdown()

    asyncio.run(scenario())


def test_expired_persisted_pending_request_is_rejected_as_expired(tmp_path):
    """``interaction_expired`` had zero test hits (round-2 review finding).

    A persisted pending deadline that has already passed must not be revivable:
    the request is retired as ``expired`` and a late reply is told so, which
    keeps an old Future from waiting forever.
    """

    async def scenario():
        context = ProjectContext.from_root(tmp_path, runtime_data_dir=tmp_path / "runtime")
        app = Application(context, agent_factory=_WaitingAgent, interaction_port=_HoldingPort())
        try:
            session = app.session_create("session-expiry").session_id
            request = InteractionRequest(
                request_id="request-expired",
                kind=InteractionKind.APPROVAL,
                session_id=session,
                run_id="run-expired",
                params_digest="d" * 64,
                expires_at_utc="2020-01-01T00:00:00.000Z",
            )
            app.control.insert_pending(
                request, workspace_id=context.workspace_id, process_id=os.getpid()
            )
            # A restart re-runs the retirement pass over persisted requests.
            app.control.mark_old_pending_interrupted()
            assert app.control.pending("request-expired")["status"] == "expired"

            response = await app.interaction_respond(
                request_id="request-expired",
                session_id=session,
                run_id="run-expired",
                params_digest="d" * 64,
                approved=True,
            )
            assert response.status == "rejected", response
            assert response.error_code == "interaction_expired", response
        finally:
            await app.shutdown()

    asyncio.run(scenario())


def test_unknown_request_id_is_rejected_as_interrupted(tmp_path):
    async def scenario():
        app, _session, _run_id = await _pending_context(tmp_path)
        try:
            payload = _base_reply("unused", "unused")
            payload["request_id"] = "never-registered"
            response = await app.interaction_respond(**payload)
            assert response.status == "rejected", response
            assert response.error_code == "interaction_interrupted", response
        finally:
            await app.shutdown()

    asyncio.run(scenario())


def test_exact_reply_is_accepted_once_and_a_second_identical_reply_is_not_re_dispatched(tmp_path):
    """The positive control: the exact envelope resolves, tampering does not."""

    async def scenario():
        app, session, run_id = await _pending_context(tmp_path)
        try:
            response = await app.interaction_respond(**_base_reply(session, run_id))
            assert response.status == "resolved", response
            assert response.error_code is None, response
            assert app.control.pending("request-1")["status"] == "resolved"
            repeat = await app.interaction_respond(**_base_reply(session, run_id))
            assert repeat.status == "rejected", repeat
            assert repeat.error_code == "interaction_interrupted", repeat
            assert app.run_status(run_id).status in {"queued", "running", "waiting_interaction", "succeeded"}
        finally:
            await app.shutdown()

    asyncio.run(scenario())
