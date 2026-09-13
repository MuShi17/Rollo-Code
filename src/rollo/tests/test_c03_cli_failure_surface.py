"""C03 — the real CLI boundary must surface identity and failure.

Two contract points are pinned here against the *real* CLI entry point:

1. a session held by another live process must not silently report success — the
   rejected ``run.start`` never reaches the provider and the process exits
   non-zero;
2. a successful one-shot must actually run *through* the Application control
   plane, leaving session/run identity in ``control.sqlite``.

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


def _runtime_context(root: Path):
    from rollo.project_context import ProjectContext

    return ProjectContext.from_root(root, runtime_data_dir=root / "runtime")


def _control_store(root: Path):
    from rollo.application import ControlStore

    context = _runtime_context(root)
    return ControlStore(
        context.runtime_data_dir / "application" / context.workspace_id / "control.sqlite"
    )


def _bootstrap_session(root: Path) -> str:
    """Run one real one-shot so the CLI has a canonical session to resume."""

    with _ProviderStub(root / "bootstrap-requests.json", reply=True) as stub:
        result = _run_cli(root, f"http://127.0.0.1:{stub.port}", "--no-thinking", "bootstrap")
    assert result.returncode == 0, result.stdout + result.stderr
    session_dirs = sorted(
        path.parent.name for path in (root / "runtime" / "sessions").glob("*/runtime.sqlite")
    )
    assert len(session_dirs) == 1, session_dirs
    return session_dirs[0]


def _seed_live_session_lease(root: Path, session_id: str, pid: int) -> None:
    """Seed the durable state of "another live process holds this session"."""

    context = _runtime_context(root)
    store = _control_store(root)
    now = "2026-01-01T00:00:00.000Z"
    try:
        with store.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO session_leases(session_id,workspace_id,owner_id,pid,acquired_at,updated_at) VALUES(?,?,?,?,?,?)",
                (session_id, context.workspace_id, "owner-live-holder", pid, now, now),
            )
    finally:
        store.close()


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
def test_cli_reports_failure_instead_of_silently_succeeding_on_a_busy_session(tmp_path: Path):
    """v3: the session is the unit of exclusion, and the CLI must surface it."""

    session_id = _bootstrap_session(tmp_path)
    request_log = tmp_path / "provider-requests.json"
    # A process that is certainly alive, so the refusal comes from a real
    # liveness check rather than from reaping order.
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        _seed_live_session_lease(tmp_path, session_id, holder.pid)
        with _ProviderStub(request_log, reply=True) as stub:
            result = _run_cli(
                tmp_path, f"http://127.0.0.1:{stub.port}", "--no-thinking", "--resume", "hello"
            )
            requests = list(stub.requests)
    finally:
        holder.kill()
        holder.wait(timeout=30)

    # Fact 1: the refused session must not reach the provider at all.
    assert requests == [], f"a busy session dispatched a provider request: {requests!r}"
    # Fact 2: and the refusal must be visible to the caller.
    assert result.returncode != 0, (
        "a busy session must not exit 0: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    assert "session_busy" in combined, combined

    # A rejected run may neither clear the holder's lease nor leave a run row.
    store = _control_store(tmp_path)
    try:
        lease = store.session_lease(session_id)
        assert lease is not None, "the live holder's lease must survive the refusal"
        assert lease["owner_id"] == "owner-live-holder", dict(lease)
        assert int(lease["pid"]) == holder.pid, dict(lease)
        runs = [dict(row) for row in store.runs_for_session(session_id)]
        assert len(runs) == 1, runs  # only the bootstrap run
        assert runs[0]["status"] == "succeeded", runs
    finally:
        store.close()


@pytest.mark.timeout(300)
def test_cli_read_only_inspection_still_works_while_a_session_is_busy(tmp_path: Path):
    """Only new runs are refused; inspection must stay available."""

    session_id = _bootstrap_session(tmp_path)
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        _seed_live_session_lease(tmp_path, session_id, holder.pid)
        with _ProviderStub(tmp_path / "provider-requests.json") as stub:
            result = _run_cli(tmp_path, f"http://127.0.0.1:{stub.port}", "--list")
    finally:
        holder.kill()
        holder.wait(timeout=30)

    assert result.returncode == 0, result.stdout + result.stderr
    assert session_id in result.stdout, result.stdout


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
        # run went through the Application control plane, not a bare chat().
        assert runs[0]["command_id"].startswith(f"cli-{session_id}-"), runs
        # v3: the run row itself is the idempotency carrier.  It records which
        # process ran it and stores the reply a retry converges on.
        assert int(runs[0]["owner_pid"]) > 0, runs
        assert runs[0]["prompt_digest"], runs
        assert runs[0]["response_json"], runs
        # The run must actually *execute* inside the control plane and reach the
        # provider-backed terminal.  Asserting only "a run row exists" would pass
        # even if the CLI ran agent.chat() outside the Application, because
        # run.start would still have committed its bookkeeping row first.
        assert runs[0]["status"] == "succeeded", runs
        assert runs[0]["error_code"] is None, runs
    finally:
        store.close()
