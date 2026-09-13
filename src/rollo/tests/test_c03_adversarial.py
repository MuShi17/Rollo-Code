from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

from rollo.application import Application, ControlStore
from rollo.interactions import InteractionKind, InteractionReply, InteractionRequest
from rollo.project_context import ProjectContext
from rollo.runtime_event import RuntimeEvent
from rollo.runtime_store import SQLiteRuntimeStore
from rollo.session import runtime_store_path


class _BlockingAgent:
    gate: asyncio.Event | None = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.runtime_store = kwargs.get("runtime_store")

    async def chat(self, prompt: str):
        if prompt == "block":
            assert self.__class__.gate is not None
            await self.__class__.gate.wait()

    def abort(self):
        # Cross-Application cancellation cannot call this instance directly;
        # the durable cancel generation is the source of truth.
        return None

    async def aclose(self):
        return None


class _CanonicalAgent:
    def __init__(self, **kwargs):
        self.store = kwargs["runtime_store"]
        self.session_id = kwargs["runtime_session_id"]
        self.run_id = kwargs["runtime_run_id"]

    async def chat(self, prompt: str):
        common = {
            "schema_version": 2,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "invocation_id": "inv-1",
            "turn_id": "turn-1",
            "ts": 1,
            "partial": False,
            "role": "model",
            "author": "agent",
        }
        self.store.append(RuntimeEvent.from_dict({
            **common,
            "id": "event-open",
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
        dispatch = {
            "operation_id": "op-1",
            "provider_tool_call_id": "tc-1",
            "tool_name": "read_file",
            "canonical_args_hash": "args-1",
        }
        self.store.append(RuntimeEvent.from_dict({
            **common,
            "id": "event-dispatch",
            "actions": {"tool_dispatch": dispatch},
        }))
        self.store.append(RuntimeEvent.from_dict({
            **common,
            "id": "event-outcome",
            "status": "completed",
            "actions": {"tool_outcome": {**dispatch, "success": True, "executed": True}},
        }))

    async def aclose(self):
        return None


def _context(tmp_path: Path) -> ProjectContext:
    return ProjectContext.from_root(tmp_path, runtime_data_dir=tmp_path / "runtime")


def test_cross_application_cancel_is_observed_before_success(tmp_path: Path):
    async def scenario():
        context = _context(tmp_path)
        _BlockingAgent.gate = asyncio.Event()
        app1 = Application(context, agent_factory=_BlockingAgent)
        session = app1.session_create("cross-cancel").session_id
        started = await app1.run_start(session_id=session, prompt="block", command_id="cross-cancel-command")
        await asyncio.sleep(0.02)
        app2 = Application(
            context,
            agent_factory=_BlockingAgent,
            control_store=ControlStore(context.runtime_data_dir / "application" / context.workspace_id / "control.sqlite"),
        )
        cancelled = await app2.run_cancel(run_id=started.run_id, command_id="cross-cancel-request")
        assert cancelled.status == "cancelling"
        _BlockingAgent.gate.set()
        final = await app1.wait_run(started.run_id)
        assert final.status == "cancelled"
        assert final.error_code == "cancelled"
        await app2.shutdown()
        await app1.shutdown()

    asyncio.run(scenario())


def test_foreign_owner_cannot_release_live_capability(tmp_path: Path):
    async def scenario():
        context = _context(tmp_path)
        _BlockingAgent.gate = asyncio.Event()
        app1 = Application(context, agent_factory=_BlockingAgent)
        session = app1.session_create("foreign-owner").session_id
        started = await app1.run_start(session_id=session, prompt="block", command_id="foreign-owner-command")
        await asyncio.sleep(0.02)
        control_path = context.runtime_data_dir / "application" / context.workspace_id / "control.sqlite"
        app2 = Application(context, agent_factory=_BlockingAgent, control_store=ControlStore(control_path))
        row = app2.control.owner(app1.owner_id)
        assert row is not None
        response = app2.owner_reconcile(
            owner_id=app1.owner_id,
            generation=int(row["generation"]),
            action="release",
            evidence={"pid": row["process_id"], "reason": "not-authoritative"},
        )
        assert response.error_code == "owner_foreign_active"
        assert app2.control.owner(app1.owner_id)["status"] == "active"
        _BlockingAgent.gate.set()
        await app1.wait_run(started.run_id)
        await app2.shutdown()
        await app1.shutdown()

    asyncio.run(scenario())


def test_inspect_only_session_is_never_implicitly_adopted(tmp_path: Path):
    async def scenario():
        context = _context(tmp_path)
        session_id = "legacy-session"
        path = runtime_store_path(session_id, context=context)
        path.parent.mkdir(parents=True, exist_ok=True)
        with SQLiteRuntimeStore(path):
            pass
        calls = []

        def factory(**kwargs):
            calls.append(kwargs)
            raise AssertionError("inspect-only session must not dispatch an agent")

        app = Application(context, agent_factory=factory)
        listed = app.session_list().data["sessions"]
        assert any(item["session_id"] == session_id and item["status"] == "inspect_only" for item in listed)
        rejected = await app.run_start(session_id=session_id, prompt="adopt", command_id="inspect-only-command")
        assert rejected.error_code == "inspect_only"
        assert not calls
        await app.shutdown()

    asyncio.run(scenario())


def test_canonical_projection_records_tool_operation_in_control_ledger(tmp_path: Path):
    async def scenario():
        context = _context(tmp_path)
        app = Application(context, agent_factory=_CanonicalAgent)
        session = app.session_create("tool-ledger").session_id
        started = await app.run_start(session_id=session, prompt="canonical", command_id="tool-ledger-command")
        final = await app.wait_run(started.run_id)
        assert final.status == "succeeded"
        assert final.data["canonical_correlation"]["tool_operations"][0]["operation_id"] == "op-1"
        assert len(app.control.tool_operations(started.run_id)) == 1
        assert final.data["result"]["completed"] is True
        await app.shutdown()

    asyncio.run(scenario())


def test_shutdown_incomplete_retains_owner_until_run_finishes(tmp_path: Path):
    async def scenario():
        context = _context(tmp_path)
        _BlockingAgent.gate = asyncio.Event()
        app = Application(context, agent_factory=_BlockingAgent)
        session = app.session_create("shutdown-retain").session_id
        started = await app.run_start(session_id=session, prompt="block", command_id="shutdown-retain-command")
        await asyncio.sleep(0.02)
        owner_id = app.owner_id
        incomplete = await app.shutdown(timeout=0.001)
        assert incomplete.status == "shutdown_incomplete"
        assert app.owner_id == owner_id
        _BlockingAgent.gate.set()
        await app.wait_run(started.run_id)
        complete = await app.shutdown(timeout=1)
        assert complete.status == "shutdown_complete"

    asyncio.run(scenario())


def test_restart_recovery_does_not_replay_dispatch_intent(tmp_path: Path):
    async def scenario():
        context = _context(tmp_path)
        _BlockingAgent.gate = asyncio.Event()
        app1 = Application(context, agent_factory=_BlockingAgent)
        session = app1.session_create("recovery-no-replay").session_id
        started = await app1.run_start(session_id=session, prompt="block", command_id="recovery-no-replay-command")
        await asyncio.sleep(0.02)
        control_path = context.runtime_data_dir / "application" / context.workspace_id / "control.sqlite"
        owner_id = app1.owner_id
        with app1.control.transaction() as db:
            db.execute("UPDATE owners SET process_id=? WHERE owner_id=?", (999999999, owner_id))
        app2 = Application(context, agent_factory=lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not replay")), control_store=ControlStore(control_path))
        status = app2.run_status(started.run_id)
        assert status.status == "interrupted"
        assert status.error_code == "run_dispatch_not_observed"
        _BlockingAgent.gate.set()
        await app1.wait_run(started.run_id)
        await app2.shutdown()
        await app1.shutdown()

    asyncio.run(scenario())


def test_control_reply_persists_redacted_sensitive_tool_input(tmp_path: Path):
    store = ControlStore(tmp_path / "control.sqlite")
    request = InteractionRequest(
        request_id="reply-secret",
        kind=InteractionKind.APPROVAL,
        session_id="s",
        run_id="r",
        params_digest="digest",
        tool_input={"api_key": "secret-value"},
    )
    store.insert_pending(request, workspace_id="w", process_id=1)
    store.complete_pending(
        request.request_id,
        status="resolved",
        reply=InteractionReply(
            request_id=request.request_id,
            approved=True,
            params_digest="digest",
            tool_input={"api_key": "secret-value"},
        ),
    )
    raw = str(store.pending(request.request_id)["reply_json"])
    assert "secret-value" not in raw
    assert "[REDACTED]" in raw
    store.close()


def test_provider_only_multiple_turns_are_ambiguous():
    events = [
        {"session_id": "s", "run_id": "r", "invocation_id": "same", "turn_id": "t1", "status": "completed"},
        {"session_id": "s", "run_id": "r", "invocation_id": "same", "turn_id": "t2", "status": "completed"},
    ]
    from rollo.application import project_canonical_evidence

    projection = project_canonical_evidence(events, session_id="s", run_id="r")
    assert projection.error_code == "canonical_identity_ambiguous"


def test_cross_process_command_idempotency_has_one_run_and_one_side_effect(tmp_path: Path):
    worker = textwrap.dedent(
        """
        import asyncio, json, os, sys
        from pathlib import Path
        from rollo.application import Application
        from rollo.project_context import ProjectContext

        class Agent:
            def __init__(self, **kwargs):
                self.marker = Path(os.environ['C03_MARKER'])
            async def chat(self, prompt):
                with self.marker.open('a', encoding='utf-8') as stream:
                    stream.write('side-effect\\n')
                await asyncio.sleep(0.5)
            async def aclose(self):
                return None

        async def main():
            root = Path(os.environ['C03_ROOT'])
            context = ProjectContext.from_root(root, runtime_data_dir=Path(os.environ['C03_RUNTIME']))
            app = Application(context, agent_factory=Agent)
            if os.environ.get('C03_SEED'):
                app.session_create('process-idempotency')
            response = await app.run_start(
                session_id='process-idempotency', prompt='same', command_id='same-command'
            )
            print(json.dumps({'status': response.status, 'result': response.result, 'run_id': response.run_id}), flush=True)
            if os.environ.get('C03_SEED'):
                await app.wait_run(response.run_id)
            await app.shutdown()

        asyncio.run(main())
        """
    )
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(Path(__file__).parents[2]),
        "C03_ROOT": str(tmp_path),
        "C03_RUNTIME": str(tmp_path / "runtime"),
        "C03_MARKER": str(tmp_path / "marker.txt"),
        "PYTHON_DOTENV_DISABLED": "1",
    })
    first_env = dict(env, C03_SEED="1")
    first = subprocess.Popen([sys.executable, "-c", worker], env=first_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        # Let the first process cross the control commit barrier before the
        # second process submits the same command id.
        import time
        time.sleep(0.15)
        second = subprocess.run([sys.executable, "-c", worker], env=env, capture_output=True, text=True, timeout=30)
        stdout1, stderr1 = first.communicate(timeout=30)
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=5)
    assert first.returncode == 0, stderr1
    assert second.returncode == 0, second.stderr + second.stdout
    first_response = json.loads(stdout1.strip().splitlines()[-1])
    second_response = json.loads(second.stdout.strip().splitlines()[-1])
    assert first_response["run_id"] == second_response["run_id"]
    assert (tmp_path / "marker.txt").read_text(encoding="utf-8").splitlines() == ["side-effect"]
