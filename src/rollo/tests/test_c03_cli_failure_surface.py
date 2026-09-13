"""C03 — the real CLI boundary must surface identity and failure.

Two contract points are pinned here against the *real* CLI entry point:

1. a quarantined workspace must not silently report success — the rejected
   ``run.start`` never reaches the provider and the process exits non-zero;
2. a successful one-shot must actually run *through* the Application control
   plane, leaving a session/run/command identity in ``control.sqlite``.

Point 2 is what makes "wire the CLI back to a direct ``agent.chat``" fail: that
regression is invisible to any provider-level assertion, which is exactly how it
survived the first review round.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC_ROOT = _REPO_ROOT / "src"


def _seed_quarantined_workspace(root: Path) -> str:
    """Create the durable state a real crashed root leaves behind."""

    from rollo.application import ControlStore
    from rollo.project_context import ProjectContext

    context = ProjectContext.from_root(root, runtime_data_dir=root / "runtime")
    store = ControlStore(
        context.runtime_data_dir / "application" / context.workspace_id / "control.sqlite"
    )
    try:
        store.insert_owner(
            owner_id="owner-crashed",
            workspace_id=context.workspace_id,
            generation=1,
            lock_key="lock-key",
        )
        # A process that is certainly gone, so the dead-owner branch is
        # reachable in any runner, regardless of process reaping order.
        store.connection.execute(
            "UPDATE owners SET process_id=?, status='active', quarantine=0 WHERE owner_id=?",
            (999_999_999, "owner-crashed"),
        )
    finally:
        store.close()
    return context.workspace_id


def _sse_event(event_type: str, payload: dict) -> bytes:
    return (
        f"event: {event_type}\n"
        f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
    ).encode("utf-8")


def _anthropic_stream_body(text: str = "ack") -> bytes:
    """A minimal Anthropic streaming response that finishes with end_turn."""

    return b"".join((
        _sse_event("message_start", {
            "type": "message_start",
            "message": {
                "id": "msg-fixture", "type": "message", "role": "assistant",
                "content": [], "model": "fixture-model",
                "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 11, "output_tokens": 0},
            },
        }),
        _sse_event("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""},
        }),
        _sse_event("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": text},
        }),
        _sse_event("content_block_stop", {"type": "content_block_stop", "index": 0}),
        _sse_event("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 1},
        }),
        _sse_event("message_stop", {"type": "message_stop"}),
    ))


class _ProviderStub:
    """A loopback endpoint that records every provider request."""

    def __init__(self, log: Path, *, reply: bool = False) -> None:
        self.log = log
        self.reply = reply
        self.requests: list[dict] = []
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8", "replace")
                recorder.requests.append({"path": self.path, "body": body})
                recorder.log.write_text(
                    json.dumps(recorder.requests, indent=2), encoding="utf-8"
                )
                if recorder.reply:
                    payload = _anthropic_stream_body()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                payload = json.dumps({
                    "type": "error",
                    "error": {"type": "api_error", "message": "stub provider"},
                }).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def __enter__(self) -> "_ProviderStub":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)


def _run_cli(root: Path, base_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(_SRC_ROOT),
        # Keep the child away from the developer's real credentials.
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "ANTHROPIC_API_KEY": "fixture-key",
        "ANTHROPIC_BASE_URL": base_url,
        "ROLLO_MODEL": "fixture-model",
        # The suite's conftest redirects this variable into a per-test isolated
        # runtime; the child must resolve the *seeded* workspace instead.
        "ROLLO_RUNTIME_DIR": str(root / "runtime"),
    })
    return subprocess.run(
        [sys.executable, "-m", "rollo", *args],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.mark.timeout(300)
def test_cli_reports_failure_instead_of_silently_succeeding_on_quarantine(tmp_path: Path):
    _seed_quarantined_workspace(tmp_path)
    request_log = tmp_path / "provider-requests.json"

    with _ProviderStub(request_log) as stub:
        result = _run_cli(tmp_path, f"http://127.0.0.1:{stub.port}", "--no-thinking", "hello")
        requests = list(stub.requests)

    # Fact 1: the quarantined workspace must not reach the provider at all.
    assert requests == [], f"a quarantined workspace dispatched a provider request: {requests!r}"
    # Fact 2: and the refusal must be visible to the caller.
    assert result.returncode != 0, (
        "a quarantined workspace must not exit 0: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    assert "owner_quarantine" in combined, combined

    # The quarantine must survive an ordinary CLI attempt: a rejected run may
    # not clear the very state that protects the crashed workspace.
    from rollo.application import ControlStore
    from rollo.project_context import ProjectContext

    context = ProjectContext.from_root(tmp_path, runtime_data_dir=tmp_path / "runtime")
    store = ControlStore(
        context.runtime_data_dir / "application" / context.workspace_id / "control.sqlite"
    )
    try:
        row = store.owner("owner-crashed")
        assert row is not None
        assert int(row["quarantine"]) == 1
        assert list(store.connection.execute("SELECT run_id FROM runs")) == []
    finally:
        store.close()


@pytest.mark.timeout(300)
def test_cli_read_only_inspection_still_works_in_a_quarantined_workspace(tmp_path: Path):
    """Only new runs are blocked; inspection must stay available."""

    _seed_quarantined_workspace(tmp_path)

    with _ProviderStub(tmp_path / "provider-requests.json") as stub:
        result = _run_cli(tmp_path, f"http://127.0.0.1:{stub.port}", "--list")

    assert result.returncode == 0, result.stdout + result.stderr


def _run_cli_stdin(
    root: Path, base_url: str, stdin_text: str, *args: str
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(_SRC_ROOT),
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "ANTHROPIC_API_KEY": "fixture-key",
        "ANTHROPIC_BASE_URL": base_url,
        "ROLLO_MODEL": "fixture-model",
        "ROLLO_RUNTIME_DIR": str(root / "runtime"),
    })
    return subprocess.run(
        [sys.executable, "-m", "rollo", *args],
        cwd=str(root),
        env=env,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.mark.timeout(300)
def test_repl_drives_the_application_control_plane(tmp_path: Path):
    """The interactive path must go through Application too (tasks 5.3).

    The pre-existing REPL test monkeypatches ``_run_repl_with_cleanup`` away and
    the other one calls ``run_repl(agent)`` without an Application, i.e. the
    legacy branch.  This runs the real entry point with piped stdin and asserts
    the durable control-plane identity, which is what a "wire the REPL straight
    to agent.chat()" regression would break.
    """

    from rollo.application import ControlStore
    from rollo.project_context import ProjectContext

    with _ProviderStub(tmp_path / "provider-requests.json", reply=True) as stub:
        result = _run_cli_stdin(
            tmp_path, f"http://127.0.0.1:{stub.port}", "hello\nexit\n"
        )
        assert stub.requests, "the stub provider was never called"

    assert result.returncode == 0, result.stdout + result.stderr

    session_dirs = sorted(
        path.parent.name
        for path in (tmp_path / "runtime" / "sessions").glob("*/runtime.sqlite")
    )
    assert len(session_dirs) == 1, session_dirs
    session_id = session_dirs[0]

    context = ProjectContext.from_root(tmp_path, runtime_data_dir=tmp_path / "runtime")
    store = ControlStore(
        context.runtime_data_dir / "application" / context.workspace_id / "control.sqlite"
    )
    try:
        runs = [dict(row) for row in store.runs_for_session(session_id)]
        assert len(runs) == 1, runs
        # REPL command ids are namespaced separately from the one-shot ones.
        assert runs[0]["command_id"].startswith(f"repl-{session_id}-"), runs
        assert runs[0]["status"] == "succeeded", runs
    finally:
        store.close()


@pytest.mark.timeout(300)
def test_cli_one_shot_records_application_session_and_run_identity(tmp_path: Path):
    """A successful one-shot must run through the Application control plane.

    Without this oracle, re-wiring the CLI back to a direct ``agent.chat`` keeps
    every provider-level assertion green while silently bypassing session/run
    ownership, idempotency and cancellation.
    """

    from rollo.application import ControlStore
    from rollo.project_context import ProjectContext

    with _ProviderStub(tmp_path / "provider-requests.json", reply=True) as stub:
        result = _run_cli(tmp_path, f"http://127.0.0.1:{stub.port}", "--no-thinking", "hello")
        assert stub.requests, "the stub provider was never called"

    assert result.returncode == 0, result.stdout + result.stderr

    # The canonical store directory names the session the CLI actually used.
    session_dirs = sorted(
        path.parent.name
        for path in (tmp_path / "runtime" / "sessions").glob("*/runtime.sqlite")
    )
    assert len(session_dirs) == 1, session_dirs
    session_id = session_dirs[0]

    context = ProjectContext.from_root(tmp_path, runtime_data_dir=tmp_path / "runtime")
    store = ControlStore(
        context.runtime_data_dir / "application" / context.workspace_id / "control.sqlite"
    )
    try:
        sessions = [dict(row) for row in store.list_sessions(context.workspace_id)]
        assert [row["session_id"] for row in sessions] == [session_id]

        runs = [dict(row) for row in store.runs_for_session(session_id)]
        assert len(runs) == 1, runs
        # The CLI issues a per-invocation command id; the durable row proves the
        # run went through the Application command envelope, not a bare chat().
        assert runs[0]["command_id"].startswith(f"cli-{session_id}-"), runs
        assert runs[0]["owner_id"], runs
        # The run must actually *execute* inside the control plane and reach the
        # provider-backed terminal.  Asserting only "a run row exists" would pass
        # even if the CLI ran agent.chat() outside the Application, because
        # run.start would still have committed its bookkeeping row first.
        assert runs[0]["status"] == "succeeded", runs
        assert runs[0]["error_code"] is None, runs

        commands = [
            dict(row)
            for row in store.connection.execute(
                "SELECT * FROM commands WHERE command_id=?", (runs[0]["command_id"],)
            )
        ]
        assert len(commands) == 1, commands
        assert commands[0]["operation"] == "run.start"
        assert commands[0]["params_digest"], commands
    finally:
        store.close()
