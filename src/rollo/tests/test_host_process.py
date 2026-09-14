"""Host wire integration tests (L1: real server loop, no child process).

The host is driven through its real read loop with a scripted stdin and a
captured protocol stream, so framing, dispatch, error mapping and the
subscription pump are exercised as production code.

``FakeStdin`` **blocks** once the scripted chunks are exhausted, exactly as a
real pipe does: a reader that returned EOF immediately would let ``serve`` finish
before the subscription pump ever ran, which is a property of the test rig and
not of the host.  The test signals end-of-input explicitly.

Nothing here writes canonical facts through the host: the slice under test is
observation only, and the events that prove incremental delivery are appended by
the client path.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from rollo.application import Application
from rollo.host.server import HostServer
from rollo.project_context import ProjectContext
from rollo.runtime_event import RuntimeEvent
from rollo.runtime_store import SQLiteRuntimeStore
from rollo.session import runtime_store_path


class FakeStdin:
    """Scripted byte source that blocks after its chunks, like a real pipe."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []
        self._lock = threading.Lock()
        self._has_data = threading.Event()
        self._eof = threading.Event()

    def feed(self, data: bytes) -> None:
        with self._lock:
            self._chunks.append(data)
        self._has_data.set()

    def eof(self) -> None:
        self._eof.set()
        self._has_data.set()

    def read1(self, _size: int) -> bytes:
        while True:
            with self._lock:
                if self._chunks:
                    return self._chunks.pop(0)
            if self._eof.is_set():
                return b""
            if not self._has_data.is_set():
                self._has_data.wait(timeout=0.05)
                continue
            # Data was consumed and no EOF is pending: wait for more.
            self._has_data.clear()


class Harness:
    """A HostServer wired to scripted input and a captured protocol stream."""

    def __init__(self, tmp_path: Path) -> None:
        self.context = ProjectContext.from_root(
            tmp_path, runtime_data_dir=tmp_path / "runtime"
        )
        self.application = Application(self.context)
        self.stdin = FakeStdin()
        self.server = HostServer(
            self.context,
            application=self.application,
            stdin=self.stdin,
            stdout=self,
            stderr=self,
            auto_shutdown=False,
        )
        self.frames: list[dict[str, Any]] = []
        self.diagnostics: list[str] = []
        self.next_id = 0

    # The server writes bytes to stdout and text to stderr; both land here.
    def write(self, data: bytes) -> None:
        if isinstance(data, bytes):
            for line in data.decode("utf-8").splitlines():
                if line.strip():
                    self.frames.append(json.loads(line))
        else:
            self.diagnostics.append(str(data))

    def flush(self) -> None:
        return None

    def frame(self, method: str, params: dict[str, Any] | None = None, *, notify: bool = False) -> int:
        self.next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            payload["id"] = self.next_id
        self.stdin.feed((json.dumps(payload, ensure_ascii=False) + "\n").encode())
        return self.next_id

    def raw(self, data: bytes) -> None:
        self.stdin.feed(data)

    def reply(self, frame_id: int) -> dict[str, Any]:
        for frame in self.frames:
            if frame.get("id") == frame_id:
                return frame
        raise AssertionError(f"no reply for id={frame_id}; frames={self.frames}")

    def events(self, kind: str | None = None) -> list[dict[str, Any]]:
        found = [
            frame["params"] for frame in self.frames if frame.get("method") == "events.event"
        ]
        if kind is not None:
            found = [item for item in found if item["kind"] == kind]
        return found

    def error_code(self, frame_id: int) -> str | None:
        error = self.reply(frame_id).get("error") or {}
        return (error.get("data") or {}).get("code")

    async def run_until(self, predicate, *, timeout: float = 5.0) -> None:
        """Let the host work until ``predicate`` holds.

        This deliberately does NOT signal end-of-input: closing stdin stops the
        read loop, which closes the subscriptions, so a helper that closed it
        would make every later phase of a multi-step scenario unreachable.
        """

        async def _wait() -> None:
            while not predicate():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(_wait(), timeout=timeout)

    async def started(self) -> "Harness":
        """Start the read loop as a background task."""

        self.task = asyncio.create_task(self.server.serve())
        return self

    async def wait_reply(self, frame_id: int, *, timeout: float = 5.0) -> dict[str, Any]:
        async def _wait() -> None:
            while True:
                for frame in self.frames:
                    if frame.get("id") == frame_id:
                        return
                await asyncio.sleep(0.01)

        try:
            await asyncio.wait_for(_wait(), timeout=timeout)
        except TimeoutError:
            # Signal end of input before raising: the read loop is otherwise
            # still waiting, and the event loop would wait for it forever while
            # the failure is reported.
            self.stdin.eof()
            raise
        return self.reply(frame_id)

    async def stop(self) -> None:
        self.stdin.eof()
        await self.task
        await self.application.shutdown()


async def _host(tmp_path: Path) -> tuple[Harness, dict[str, Any]]:
    """Start a host and complete the handshake."""

    harness = await Harness(tmp_path).started()
    frame_id = harness.frame("host.initialize", {"version": 1})
    reply = await harness.wait_reply(frame_id)
    assert "result" in reply, reply
    return harness, reply["result"]


def _open_event(session_id: str) -> RuntimeEvent:
    """The first canonical record of an invocation, as the store requires."""

    return RuntimeEvent.from_dict(
        {
            "schema_version": 2,
            "id": f"open-{session_id}",
            "session_id": session_id,
            "run_id": "run-host",
            "invocation_id": "inv-host",
            "turn_id": "turn-host",
            "ts": 0,
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
        }
    )


def _event(event_id: str, ordinal_hint: int, session_id: str) -> RuntimeEvent:
    return RuntimeEvent.from_dict(
        {
            "schema_version": 2,
            "id": event_id,
            "session_id": session_id,
            "run_id": "run-host",
            "invocation_id": "inv-host",
            "turn_id": "turn-host",
            "ts": ordinal_hint,
            "partial": False,
            "role": "model",
            "author": "agent",
            "content": {"kind": "text", "text": f"m{ordinal_hint}"},
            "metadata": {"lifecycle": "model_final"},
        }
    )


def test_initialize_reports_version_epoch_and_capabilities(tmp_path: Path):
    async def scenario():
        harness, result = await _host(tmp_path)
        await harness.stop()

        assert result["protocol_version"] == 1
        assert result["host_epoch"] == harness.server.host_epoch
        assert result["workspace_id"] == harness.context.workspace_id
        assert result["capabilities"]["batch"] is False
        assert "events.subscribe" in result["capabilities"]["observation"]
        assert result["capabilities"]["control"] == []
        assert result["capabilities"]["declared_not_implemented"]
        assert result["limits"]["frame_bytes"] == 1024 * 1024
        # Missing settings are reported by NAME so the GUI can tell the user what
        # to configure; the value must never reach the wire.
        assert isinstance(result["settings"]["missing"], list)
        rendered = json.dumps(result["settings"])
        for value in (os.environ.get("ANTHROPIC_API_KEY"), os.environ.get("OPENAI_API_KEY")):
            if value:
                assert value not in rendered

    asyncio.run(scenario())


def test_an_unsupported_version_is_refused_without_guessing(tmp_path: Path):
    async def scenario():
        harness = await Harness(tmp_path).started()
        frame_id = harness.frame("host.initialize", {"version": 2})
        reply = await harness.wait_reply(frame_id)
        await harness.stop()

        assert (reply["error"]["data"] or {})["code"] == "unsupported_version"
        assert reply["error"]["data"]["supported"] == 1
        assert harness.server.initialized is False

    asyncio.run(scenario())


def test_commands_before_initialize_are_refused(tmp_path: Path):
    async def scenario():
        harness = await Harness(tmp_path).started()
        frame_id = harness.frame("session.list")
        reply = await harness.wait_reply(frame_id)
        await harness.stop()

        assert (reply["error"]["data"] or {})["code"] == "not_initialized"

    asyncio.run(scenario())


def test_declared_control_methods_answer_not_implemented(tmp_path: Path):
    """A client must be able to tell "not built" from "wrong call"."""

    async def scenario():
        harness, _ = await _host(tmp_path)
        frame_id = harness.frame("run.start", {"session_id": "s", "input": "hi"})
        reply = await harness.wait_reply(frame_id)
        await harness.stop()

        assert (reply["error"]["data"] or {})["code"] == "not_implemented"
        assert reply["error"]["code"] == -32601

    asyncio.run(scenario())


def test_an_unknown_method_is_reported_as_method_not_found(tmp_path: Path):
    async def scenario():
        harness, _ = await _host(tmp_path)
        frame_id = harness.frame("session.explode")
        reply = await harness.wait_reply(frame_id)
        await harness.stop()

        assert reply["error"]["code"] == -32601
        # An unknown method is not a business error, so no business code is set.
        assert "code" not in (reply["error"].get("data") or {})

    asyncio.run(scenario())


def test_malformed_input_does_not_kill_the_connection(tmp_path: Path):
    """One bad frame is an error reply; the next frame still works."""

    async def scenario():
        harness, _ = await _host(tmp_path)
        harness.raw(b"{not json}\n")
        frame_id = harness.frame("session.list")
        reply = await harness.wait_reply(frame_id)
        await harness.stop()

        assert reply["result"]["sessions"] == []
        assert harness.server.protocol.frames_rejected == 1
        parse_errors = [
            frame for frame in harness.frames if (frame.get("error") or {}).get("code") == -32700
        ]
        assert parse_errors, harness.frames

    asyncio.run(scenario())


def test_an_oversized_frame_closes_with_a_diagnostic(tmp_path: Path):
    async def scenario():
        harness, _ = await _host(tmp_path)
        harness.server.protocol.max_frame_bytes = 64
        harness.raw(b"x" * 200 + b"\n")
        harness.stdin.eof()
        await harness.task
        await harness.application.shutdown()

        errors = [frame for frame in harness.frames if "error" in frame]
        assert errors[-1]["error"]["data"]["code"] == "frame_too_large"
        assert any("closing connection" in line for line in harness.diagnostics)

    asyncio.run(scenario())


def test_session_list_is_scoped_and_hides_filesystem_paths(tmp_path: Path):
    async def scenario():
        harness, _ = await _host(tmp_path)
        created = harness.application.session_create("session-host")
        frame_id = harness.frame("session.list")
        reply = await harness.wait_reply(frame_id)
        await harness.stop()

        result = reply["result"]
        assert result["workspace_id"] == harness.context.workspace_id
        summaries = [row for row in result["sessions"] if row["session_id"] == created.session_id]
        assert summaries, result
        assert "canonical_path" not in summaries[0]

    asyncio.run(scenario())


def test_session_list_pages_and_reports_a_cursor(tmp_path: Path):
    async def scenario():
        harness, _ = await _host(tmp_path)
        for index in range(3):
            harness.application.session_create(f"session-page-{index}")
        first_id = harness.frame("session.list", {"limit": 2})
        first = (await harness.wait_reply(first_id))["result"]
        second_id = harness.frame(
            "session.list", {"limit": 2, "page_cursor": first["next_page_cursor"]}
        )
        second = (await harness.wait_reply(second_id))["result"]
        await harness.stop()

        assert len(first["sessions"]) == 2
        assert first["next_page_cursor"] == "2"
        assert len(second["sessions"]) == 1
        assert second["next_page_cursor"] is None

    asyncio.run(scenario())


def test_snapshot_and_subscription_deliver_a_live_event(tmp_path: Path):
    """The observation loop: atomic snapshot, then incremental delivery."""

    async def scenario():
        harness, _ = await _host(tmp_path)
        created = harness.application.session_create("session-watch")
        session_id = created.session_id
        # ONE store, held open for the whole scenario: the host caches the
        # session's store, so closing it and reopening another handle would leave
        # the subscription reading through a closed connection.
        store = SQLiteRuntimeStore(runtime_store_path(session_id, context=harness.context))
        try:
            store.append(_open_event(session_id))

            frame_id = harness.frame("events.subscribe", {"session_id": session_id})
            subscribed = (await harness.wait_reply(frame_id))["result"]
            assert subscribed["status"] == "subscribed"
            assert subscribed["host_epoch"] == harness.server.host_epoch

            await harness.run_until(lambda: bool(harness.events("snapshot")))
            snapshot_events = harness.events("snapshot")
            payload = snapshot_events[0]["payload"]
            assert payload["session_id"] == session_id
            assert payload["high_water"] >= 1

            # A later canonical event reaches the subscriber incrementally.
            store.append(_event("later", 1, session_id))
            await harness.run_until(lambda: bool(harness.events("event")))
            delivered = harness.events("event")
            assert delivered[0]["session_id"] == session_id
            assert delivered[0]["payload"]["id"] == "later"
            assert delivered[0]["prefix_boundary_exempt"] is False
            assert delivered[0]["transport_seq"] >= 1
        finally:
            store.close()

        # Unsubscribing only stops observation.
        frame_id = harness.frame(
            "events.unsubscribe", {"subscription_id": subscribed["subscription_id"]}
        )
        await harness.wait_reply(frame_id)
        await harness.stop()
        assert harness.server.subscriptions == {}

    asyncio.run(scenario())


def test_the_snapshot_ordinal_is_the_resume_boundary(tmp_path: Path):
    """The snapshot envelope carries the boundary a client will hand back."""

    async def scenario():
        harness, _ = await _host(tmp_path)
        created = harness.application.session_create("session-cursor")
        session_id = created.session_id
        store = SQLiteRuntimeStore(runtime_store_path(session_id, context=harness.context))
        try:
            store.append(_open_event(session_id))
            boundary = int(store.high_water(session_id=session_id))
        finally:
            store.close()

        frame_id = harness.frame("events.subscribe", {"session_id": session_id})
        await harness.wait_reply(frame_id)
        await harness.run_until(lambda: bool(harness.events("snapshot")))
        await harness.stop()

        envelope = harness.events("snapshot")[0]
        assert envelope["ordinal"] == boundary
        assert envelope["payload"]["high_water"] == boundary

    asyncio.run(scenario())


def test_a_malformed_cursor_is_rejected(tmp_path: Path):
    async def scenario():
        harness, _ = await _host(tmp_path)
        frame_id = harness.frame("events.subscribe", {"session_id": "s", "cursor": {"nope": 1}})
        reply = await harness.wait_reply(frame_id)
        await harness.stop()

        assert reply["error"]["code"] == -32602

    asyncio.run(scenario())


def test_shutdown_stops_the_server_and_closes_subscriptions(tmp_path: Path):
    async def scenario():
        harness, _ = await _host(tmp_path)
        frame_id = harness.frame("host.shutdown", {"command_id": "cmd-1"})
        reply = await harness.wait_reply(frame_id)
        await harness.task
        await harness.application.shutdown()

        assert reply["result"]["status"] == "shutting_down"
        assert harness.server.shutdown_requested is True
        assert harness.server.subscriptions == {}

    asyncio.run(scenario())


def test_the_entry_point_serves_a_real_child_process(tmp_path: Path):
    """The mechanism Item 08 exists for: a real Python host over real pipes.

    Nothing is faked except the workspace: the child is a separate interpreter
    started exactly as the desktop main process would start it, and the frames
    cross an actual OS pipe.
    """

    import subprocess
    import sys

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "PYTHONIOENCODING": "utf-8",
    }
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "rollo.host",
            "--workspace",
            str(workspace),
            "--runtime-dir",
            str(tmp_path / "runtime"),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    def send(payload: dict[str, Any]) -> None:
        assert child.stdin is not None
        child.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        child.stdin.flush()

    def read_frame() -> dict[str, Any]:
        assert child.stdout is not None
        line = child.stdout.readline()
        assert line, f"the host closed the protocol stream: {child.stderr.read()!r}"
        return json.loads(line.decode("utf-8"))

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "host.initialize", "params": {"version": 1}})
        handshake = read_frame()
        assert handshake["id"] == 1
        assert handshake["result"]["protocol_version"] == 1
        assert handshake["result"]["capabilities"]["control"] == []

        send({"jsonrpc": "2.0", "id": 2, "method": "session.list", "params": {}})
        assert read_frame()["result"]["sessions"] == []

        # A frame split across two writes must still decode: the client is not
        # allowed to assume a write boundary is a frame boundary.
        assert child.stdin is not None
        whole = (json.dumps({"jsonrpc": "2.0", "id": 3, "method": "session.list"}) + "\n").encode()
        child.stdin.write(whole[:9])
        child.stdin.flush()
        child.stdin.write(whole[9:])
        child.stdin.flush()
        assert read_frame()["id"] == 3

        send({"jsonrpc": "2.0", "id": 4, "method": "host.shutdown", "params": {}})
        assert read_frame()["result"]["status"] == "shutting_down"
        assert child.wait(timeout=30) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=30)


def test_an_unissued_session_is_refused_without_creating_anything(tmp_path: Path):
    """Observation must not bring a session into existence.

    Opening a *new* database path has the driver create the file, so a
    subscription to a mistyped or never-issued id used to leave a 160 KiB
    database behind -- and answer ok while doing it.  A read that creates its
    own subject is not a read.
    """

    async def scenario():
        harness, _ = await _host(tmp_path)
        session_id = "never-issued-session"
        database = runtime_store_path(session_id, context=harness.context)
        assert not database.exists()

        for method in ("events.subscribe", "session.snapshot"):
            frame_id = harness.frame(method, {"session_id": session_id})
            reply = await harness.wait_reply(frame_id)
            assert "error" in reply, f"{method} accepted a session that does not exist"
            assert reply["error"]["data"]["code"] == "scope_mismatch", reply["error"]
            assert reply["error"]["code"] == -32602, reply["error"]
            assert not database.exists(), f"{method} created {database}"

        await harness.stop()

    asyncio.run(scenario())


def test_the_issued_cursor_can_actually_be_resumed(tmp_path: Path):
    """The resume path must be reachable from the wire, not merely declared.

    The subscription service validates `service_epoch` and `partial_versions`,
    neither of which a client can invent.  If the subscribe response does not
    carry them, every resume is refused -- and the refusal even blames a
    replaced service instance.  This drives the full round trip: subscribe,
    detach, resume with exactly what the host handed out.
    """

    async def scenario():
        harness, _ = await _host(tmp_path)
        created = harness.application.session_create("session-resume")
        session_id = created.session_id
        store = SQLiteRuntimeStore(runtime_store_path(session_id, context=harness.context))
        try:
            store.append(_open_event(session_id))

            first = harness.frame("events.subscribe", {"session_id": session_id})
            issued = (await harness.wait_reply(first))["result"]
            cursor = issued["cursor"]
            # Every field the service validates has to be present.
            assert set(cursor) >= {
                "subscription_id",
                "session_id",
                "high_water",
                "projection_version",
                "service_epoch",
                "partial_versions",
            }, cursor

            # Detach rather than unsubscribe: closing the subscription
            # invalidates the cursor by design, so only a detach leaves the
            # buffer (and the cursor) valid.
            detached = harness.frame(
                "events.detach", {"subscription_id": issued["subscription_id"]}
            )
            await harness.wait_reply(detached)
            await harness.run_until(lambda: bool(harness.events("snapshot")))

            second = harness.frame(
                "events.subscribe", {"session_id": session_id, "cursor": cursor}
            )
            reply = await harness.wait_reply(second)
            assert "error" not in reply, reply
            assert reply["result"]["status"] == "resumed", reply["result"]
            await harness.stop()
        finally:
            store.close()

    # A bound so a regression fails loudly instead of hanging the suite.
    asyncio.run(asyncio.wait_for(scenario(), timeout=30))


def test_stdout_carries_only_protocol_frames(tmp_path: Path, capsys):
    """Nothing but encoded frames may reach stdout, even a stray print."""

    async def scenario():
        harness, _ = await _host(tmp_path)
        with harness.server._outlet:
            print("this would corrupt the wire")
        frame_id = harness.frame("session.list")
        await harness.wait_reply(frame_id)
        await harness.stop()

        for frame in harness.frames:
            assert "jsonrpc" in frame
        assert any("this would corrupt" in line for line in harness.diagnostics)

    asyncio.run(scenario())
    captured = capsys.readouterr()
    assert "would corrupt" not in captured.out
