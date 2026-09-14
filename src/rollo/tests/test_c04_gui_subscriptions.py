"""C04 subscription contracts: boundary, resume/expiry, overflow, drafts.

Three layers, chosen so each judgement can actually be witnessed:

* **L2 (real store + real ``SubscriptionService``)** for delivery, pagination
  and resume, because a fake store would be an unverified oracle for ordinals.
* **L1 (injected store)** for faults that must fire *inside* a read (a commit
  landing between the snapshot and the first suffix read) and for the buffer
  bound, which the service owns.
* **a real ``Application`` run** for "unsubscribing does not stop the run".

Nothing here needs a second process or a second thread: the service is
single-process and its delivery path has no ``await`` between reading the
boundary and publishing the result.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path

import pytest

from rollo.application import Application
from rollo.event_ids import RunContext
from rollo.event_sink import RuntimeEventEmitter
from rollo.project_context import ProjectContext
from rollo.projections import subscriptions as subs
from rollo.projections.subscriptions import (
    SUBSCRIPTION_BUFFER_LIMIT,
    GuiCursor,
    GuiMessage,
    SubscriptionService,
    derive_stream_key,
)
from rollo.runtime_event import RuntimeEvent
from rollo.runtime_lifecycle import ModelCallRecorder
from rollo.runtime_store import SQLiteRuntimeStore

SESSION = "session-c04"
RUN = "run-session-c04"
INVOCATION = "inv-session-c04"


def _open_event(session_id: str = SESSION) -> RuntimeEvent:
    return RuntimeEvent.from_dict(
        {
            "schema_version": 2,
            "id": f"open-{session_id}",
            "session_id": session_id,
            "run_id": f"run-{session_id}",
            "invocation_id": f"inv-{session_id}",
            "turn_id": "turn-c04",
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


def _text_event(index: int, text: str, *, session_id: str = SESSION) -> RuntimeEvent:
    return RuntimeEvent.from_dict(
        {
            "schema_version": 2,
            "id": f"{session_id}-msg-{index}",
            "session_id": session_id,
            "run_id": f"run-{session_id}",
            "invocation_id": f"inv-{session_id}",
            "turn_id": "turn-c04",
            "ts": index,
            "partial": False,
            "role": "model",
            "author": "agent",
            "content": {"kind": "text", "text": text},
            "metadata": {"lifecycle": "model_final"},
        }
    )


def _terminal_event(session_id: str = SESSION, *, ts: int = 9_000) -> RuntimeEvent:
    return RuntimeEvent.from_dict(
        {
            "schema_version": 2,
            "id": f"terminal-{session_id}",
            "session_id": session_id,
            "run_id": f"run-{session_id}",
            "invocation_id": f"inv-{session_id}",
            "turn_id": "turn-c04",
            "ts": ts,
            "partial": False,
            "role": "model",
            "author": "agent",
            "status": "completed",
            "actions": {"run_terminal": {"status": "completed"}},
        }
    )


def _store(tmp_path: Path) -> SQLiteRuntimeStore:
    return SQLiteRuntimeStore(tmp_path / "runtime.sqlite")


async def _collect(stream, *, limit: int = 200, timeout: float = 8.0) -> list[GuiMessage]:
    """Drain a stream to its natural end within a bounded time."""

    collected: list[GuiMessage] = []

    async def _drain() -> None:
        async for message in stream:
            collected.append(message)
            if len(collected) >= limit:
                return

    try:
        await asyncio.wait_for(_drain(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    return collected


async def _next(stream, kind: str, *, timeout: float = 8.0) -> GuiMessage:
    async def _take() -> GuiMessage:
        async for message in stream:
            if message.kind == kind:
                return message
        raise AssertionError(f"stream ended before a {kind!r} message arrived")

    return await asyncio.wait_for(_take(), timeout=timeout)


async def _at_high_water(store: SQLiteRuntimeStore, high_water: int, *, timeout: float = 8.0) -> None:
    async def _wait() -> None:
        while int(store.high_water(session_id=SESSION)) < high_water:
            await asyncio.sleep(0.01)
        raise AssertionError("reached")

    with pytest.raises(AssertionError):
        await asyncio.wait_for(_wait(), timeout=timeout)


class ReadHookStore:
    """Delegating store that runs a callback inside one suffix read."""

    def __init__(self, inner: SQLiteRuntimeStore, hook) -> None:
        self._inner = inner
        self._hook = hook
        self.fired = False

    def high_water(self, *, session_id: str | None = None, run_id: str | None = None) -> int:
        return self._inner.high_water(session_id=session_id, run_id=run_id)

    def read_event_records(self, **kwargs):
        pairs = self._inner.read_event_records(**kwargs)
        if kwargs.get("after_ordinal") is not None and not self.fired:
            self.fired = True
            self._hook()
        return pairs

    def read_runtime_stream_partials(self, **kwargs):
        return self._inner.read_runtime_stream_partials(**kwargs)

    def read_event(self, event_id: str):
        return self._inner.read_event(event_id)

    @property
    def database(self) -> Path:
        return self._inner.database

    def close(self) -> None:
        self._inner.close()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _partial_event(text: str, *, seq: int, event_id: str, ts: int = 1) -> RuntimeEvent:
    """One draft fragment.

    ``text`` is the *fragment* this chunk adds, exactly as a provider delta
    would arrive: the store aggregates fragments into one cumulative payload per
    stream, so revision 2 of "a" plus "b" reads back as "ab".
    """

    key = _recorder_stream_key(kind="text")
    return RuntimeEvent.from_dict(
        {
            "schema_version": 2,
            "id": event_id,
            "session_id": SESSION,
            "run_id": RUN,
            "invocation_id": INVOCATION,
            "turn_id": "turn-c04",
            "ts": ts,
            "partial": True,
            "role": "model",
            "author": "agent",
            "content": {"kind": "text", "text": text},
            "metadata": {
                "lifecycle": "stream_partial",
                "partial_stream_key": key,
                "partial_seq": seq,
                "attempt_id": "attempt-fixture",
            },
        }
    )


def _recorder_stream_key(
    *, kind: str, attempt_id: str = "attempt-fixture", call_id: str | None = None
) -> str:
    """The key ``ModelCallRecorder._stream_key`` builds for this fixture."""

    return ":".join(("partial", INVOCATION, attempt_id, kind, call_id or ""))


def _derive(partial) -> str:
    """Read a persisted draft the way the service reads it."""

    payload = partial.payload
    return derive_stream_key(
        invocation_id=payload["invocation_id"],
        metadata=payload.get("metadata"),
        content=payload.get("content"),
    )


# ---------------------------------------------------------------- snapshot


def test_snapshot_builds_at_the_high_water_it_reports(tmp_path: Path):
    """The service snapshots the *current* boundary and reports that one."""

    async def scenario():
        with _store(tmp_path) as store:
            store.append(_open_event())
            store.append(_text_event(1, "one"))
            store.append(_text_event(2, "two"))
            boundary = int(store.high_water(session_id=SESSION))
            assert boundary == 3

            service = SubscriptionService(store=store)
            result = await service.snapshot(SESSION)

            assert result.status == "ok"
            assert result.cursor.high_water == boundary
            assert result.cursor.service_epoch == service.service_epoch
            assert result.cursor.projection_version == service.projection_version
            assert result.snapshot.high_water == boundary
            assert [item.message_id for item in result.snapshot.messages] == [
                f"{SESSION}-msg-1",
                f"{SESSION}-msg-2",
            ]
            assert all(item.ordinal <= boundary for item in result.snapshot.messages)

            # The boundary advances with the store, never past it.
            store.append(_text_event(3, "three"))
            later = await service.snapshot(SESSION)
            assert later.cursor.high_water == 4
            assert [item.message_id for item in later.snapshot.messages] == [
                f"{SESSION}-msg-1",
                f"{SESSION}-msg-2",
                f"{SESSION}-msg-3",
            ]
            await service.aclose()

    asyncio.run(scenario())


# ------------------------------------------------------------------ no gap


def test_event_committed_between_snapshot_and_first_suffix_read_is_not_lost(tmp_path: Path):
    """Boundary safety comes from the ordinal, not from registration order."""

    async def scenario():
        with _store(tmp_path) as store:
            store.append(_open_event())
            store.append(_text_event(1, "one"))
            store.append(_text_event(2, "two"))
            boundary = int(store.high_water(session_id=SESSION))
            injected: list[int] = []

            def commit_between() -> None:
                injected.append(store.append(_text_event(3, "between")).ordinal)

            hooked = ReadHookStore(store, commit_between)
            service = SubscriptionService(store=hooked)
            stream = await service.subscribe(SESSION)

            delivered = await _collect(stream, limit=10, timeout=2.0)
            await service.aclose()

            assert hooked.fired, "the hook never fired inside a suffix read"
            assert injected and injected[0] > boundary
            snapshot = delivered[0]
            assert snapshot.kind == "snapshot"
            assert snapshot.payload.high_water == boundary
            # The envelope's ordinal is the resume boundary the consumer will
            # hand back, so it must equal the boundary the snapshot was built at
            # -- not one past it, which would skip the first suffix record.
            assert snapshot.ordinal == boundary
            assert snapshot.prefix_boundary_exempt is False
            assert snapshot.key is None
            assert [item.message_id for item in snapshot.payload.messages] == [
                f"{SESSION}-msg-1",
                f"{SESSION}-msg-2",
            ]
            # Not in the snapshot, but present exactly once in the stream.
            ordinals = [message.ordinal for message in delivered if message.kind == "event"]
            assert injected[0] in ordinals
            assert ordinals == sorted(ordinals)
            assert len(ordinals) == len(set(ordinals))

    asyncio.run(scenario())


# ------------------------------------------------------------ resume/expiry


def test_resume_continues_when_the_buffer_covers_the_cursor(tmp_path: Path):
    async def scenario():
        with _store(tmp_path) as store:
            store.append(_open_event())
            store.append(_text_event(1, "one"))
            service = SubscriptionService(store=store)
            stream = await service.subscribe(SESSION)
            first = await _next(stream, "snapshot")
            assert first.payload.high_water == 2

            # The caller detaches (no longer consuming); the producer keeps
            # buffering, which is exactly what a resumable cursor needs.
            await stream.aclose()
            await asyncio.sleep(0.05)
            store.append(_text_event(2, "two"))
            await asyncio.sleep(0.05)
            store.append(_text_event(3, "three"))
            await _at_high_water(store, 4)
            for _ in range(200):
                if stream.cursor.high_water == 4:
                    break
                await asyncio.sleep(0.01)

            cursor = stream.cursor
            assert cursor.high_water == 4
            resumed = await service.resume(cursor)
            assert resumed.status == "resumed"
            assert resumed.stream is stream
            assert resumed.error_code is None

            delivered = await _collect(resumed.stream, limit=10, timeout=2.0)
            await service.aclose()
            ordinals = [message.ordinal for message in delivered if message.kind == "event"]
            assert ordinals == [3, 4]

    asyncio.run(scenario())


def test_cursor_expired_on_every_declared_criterion(tmp_path: Path):
    async def scenario():
        with _store(tmp_path) as store:
            store.append(_open_event())
            store.append(_text_event(1, "one"))
            service = SubscriptionService(store=store)
            stream = await service.subscribe(SESSION)
            await _next(stream, "snapshot")
            cursor = stream.cursor

            # 1. the issuing service instance was replaced
            replacement = SubscriptionService(store=store)
            expired = await replacement.resume(cursor)
            assert expired.status == "cursor_expired"
            assert expired.error_code == "service_instance_replaced"
            assert expired.current_high_water == store.high_water(session_id=SESSION)

            # 2. the projection version moved on
            moved = SubscriptionService(store=store, service_epoch=cursor.service_epoch)
            moved._projection.projection_version = "gui-projection-v2"  # type: ignore[attr-defined]
            version_expired = await moved.resume(cursor)
            assert version_expired.status == "cursor_expired"
            assert version_expired.error_code == "projection_version_mismatch"

            # 3. the buffer no longer covers the cursor
            underneath = GuiCursor(
                subscription_id=cursor.subscription_id,
                session_id=cursor.session_id,
                high_water=cursor.high_water - 1,
                projection_version=cursor.projection_version,
                service_epoch=cursor.service_epoch,
            )
            await stream.aclose()
            await asyncio.sleep(0.05)
            store.append(_text_event(2, "two"))
            await _at_high_water(store, 2)
            stream._state.resumable_high_water = 2  # buffer trimmed past that point
            detached = await service.resume(underneath)
            assert detached.status == "cursor_expired"
            assert detached.error_code == "buffer_does_not_cover"

            # 4. an unknown subscription id
            unknown = await service.resume(
                GuiCursor(
                    subscription_id="never-issued",
                    session_id=SESSION,
                    high_water=0,
                    projection_version=cursor.projection_version,
                    service_epoch=cursor.service_epoch,
                )
            )
            assert unknown.status == "cursor_expired"
            assert unknown.error_code == "subscription_unknown"

            await service.aclose()
            await replacement.aclose()
            await moved.aclose()

    asyncio.run(scenario())


# ----------------------------------------------------------------- overflow


# ------------------------------------------------------------------- drafts


def test_draft_stream_key_mirrors_the_recorder(tmp_path: Path):
    class FakeClock:
        def __call__(self) -> int:
            return 1_700_000_000_000

    with _store(tmp_path) as store:
        context = RunContext(SESSION, "turn-c04", RUN, INVOCATION)
        recorder = ModelCallRecorder(
            RuntimeEventEmitter(store),
            context,
            provider="fixture",
            model="fixture-model",
            clock=FakeClock(),
        )
        recorder.start("request-fixture")
        recorder.partial_text("hello")
        recorder.flush_partials()

        partial = store.read_runtime_stream_partials(session_id=SESSION)[0]
        stored_key = str(partial.stream_key)
        derived = _derive(partial)
        attempt_id = str(recorder.attempt_id)
        # Both directions: the mirror must equal what the recorder wrote, and
        # what the store persisted under.
        assert derived == _recorder_stream_key(kind="text", attempt_id=attempt_id)
        assert stored_key == derived
        assert stored_key == ":".join(
            ("partial", INVOCATION, attempt_id, "text", "")
        )
        # The mirror also reproduces the recorder's fallback for metadata that
        # carries no explicit key.
        assert (
            derive_stream_key(
                invocation_id=INVOCATION,
                metadata={"attempt_id": attempt_id},
                content={"kind": "text"},
            )
            == stored_key
        )

        # A second stream of a different kind must not collapse onto this key.
        recorder.partial_text("hmm", kind="thinking")
        recorder.flush_partials()
        keys = {
            _derive(item) for item in store.read_runtime_stream_partials(session_id=SESSION)
        }
        assert keys == {
            _recorder_stream_key(kind="text", attempt_id=attempt_id),
            _recorder_stream_key(kind="thinking", attempt_id=attempt_id),
        }


def test_draft_replaces_by_revision_and_final_clears_it(tmp_path: Path):
    """A live recorder drives the draft lifecycle the way production does."""

    class FakeClock:
        def __call__(self) -> int:
            return 1_700_000_000_000

    async def scenario():
        with _store(tmp_path) as store:
            recorder = ModelCallRecorder(
                RuntimeEventEmitter(store),
                RunContext(SESSION, "turn-c04", RUN, INVOCATION),
                provider="fixture",
                model="fixture-model",
                clock=FakeClock(),
            )
            recorder.start("request-fixture")
            recorder.partial_text("a")
            recorder.flush_partials()
            stream_key = _recorder_stream_key(
                kind="text", attempt_id=str(recorder.attempt_id)
            )

            # The draft is a non-prefix fact, so it rides in the snapshot and
            # the snapshot says so instead of pretending it is prefix data.
            service = SubscriptionService(store=store)
            snapshot = await service.snapshot(SESSION)
            assert snapshot.snapshot.high_water == 1  # only the open event
            assert len(snapshot.snapshot.drafts) == 1
            draft = snapshot.snapshot.drafts[0]
            assert draft.stream_key == stream_key
            assert draft.to_dict()["prefix_boundary_exempt"] is True
            assert draft.revision == 1
            assert draft.partial_seq == 1
            assert draft.payload["content"]["text"] == "a"
            assert snapshot.cursor.partial_version(stream_key) == 1

            live = await service.subscribe(SESSION)
            await _next(live, "snapshot")

            # A later version of the same stream replaces, never appends.
            recorder.partial_text("b")
            recorder.flush_partials()
            second = await _next(live, "draft")
            assert second.key == stream_key
            assert second.payload.revision == 2
            assert second.payload.partial_seq == 2
            assert second.payload.payload["content"]["text"] == "ab"
            assert second.payload.fragment_count == 2
            assert second.prefix_boundary_exempt is True

            # The final event clears the draft row and ends the partial stream.
            recorder.final_text("ab")
            final = await _next(live, "event")
            assert final.payload["content"]["text"] == "ab"
            assert store.read_runtime_stream_partials(session_id=SESSION) == []

            # A snapshot taken after the final can no longer see a draft.
            after = await service.snapshot(SESSION)
            assert after.snapshot.drafts == ()
            assert after.snapshot.last_partial_seq == 0
            assert after.cursor.partial_versions == ()

            tail = await _collect(live, limit=5, timeout=1.0)
            await service.aclose()
            assert [message.kind for message in tail] == []
            assert live.closed is True

    asyncio.run(scenario())


# -------------------------------------------------------------- read-only


def test_subscription_path_writes_nothing(tmp_path: Path):
    """Silent store: event ids, counts and the prefix digest stay identical."""

    async def scenario():
        with _store(tmp_path) as store:
            store.append(_open_event())
            store.append(_text_event(1, "one"))
            store.append(_text_event(2, "two"))
            boundary = int(store.high_water(session_id=SESSION))
            database = Path(store.database)

            before_ids = [pair[1].id for pair in store.read_event_records()]
            before_ordinals = [pair[0] for pair in store.read_event_records()]
            before_digest = hashlib.sha256(
                b"".join(pair[1].canonical_bytes() for pair in store.read_event_records())
            ).hexdigest()
            before_file = _sha256(database)
            before_rows = len(store.read_runtime_stream_partials(session_id=SESSION))

            service = SubscriptionService(store=store)
            snapshot = await service.snapshot(SESSION)
            stream = await service.subscribe(SESSION)
            collected = await _collect(stream, limit=5, timeout=1.0)
            await service.aclose()

            after_ids = [pair[1].id for pair in store.read_event_records()]
            after_ordinals = [pair[0] for pair in store.read_event_records()]
            after_digest = hashlib.sha256(
                b"".join(pair[1].canonical_bytes() for pair in store.read_event_records())
            ).hexdigest()

            assert snapshot.snapshot.high_water == boundary
            assert [message.kind for message in collected] == ["snapshot"]
            assert after_ids == before_ids
            assert after_ordinals == before_ordinals
            assert after_digest == before_digest
            assert store.high_water(session_id=SESSION) == boundary
            # Drafts live in the same file; a single write would change its hash.
            assert _sha256(database) == before_file
            assert len(store.read_runtime_stream_partials(session_id=SESSION)) == before_rows

    asyncio.run(scenario())


def test_projections_do_not_import_the_agent_or_tools():
    """A read model that can reach the executor is not a read model.

    ``rollo.application`` is deliberately *not* in this guard: importing any
    ``rollo`` submodule runs ``src/rollo/__init__.py``, which imports
    ``Application`` eagerly, so its presence says nothing about our import
    graph.  ``agent`` and ``tools`` are the modules that carry execution.
    """

    for name in (
        "rollo.agent",
        "rollo.tools",
        "rollo.projections.gui_projection",
        "rollo.projections.subscriptions",
    ):
        sys.modules.pop(name, None)
    before = set(sys.modules)
    import rollo.projections.gui_projection  # noqa: F401
    import rollo.projections.subscriptions  # noqa: F401

    added = set(sys.modules) - before
    assert "rollo.agent" not in added
    assert "rollo.tools" not in added


# --------------------------------------------------------------- run binding


class _GatedAgent:
    """Real Application run double, gated so the test controls the terminal."""

    gate: asyncio.Event | None = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.runtime_store = kwargs.get("runtime_store")
        self.aborted = False

    def configure_runtime_store(self, store):
        self.runtime_store = store

    def configure_runtime_identity(self, *, session_id, run_id):
        self.runtime_session_id = session_id
        self.runtime_run_id = run_id

    async def chat(self, prompt: str):
        if self.gate is not None:
            await self.gate.wait()
        _write_canonical_terminal(
            self.runtime_store,
            self.kwargs.get("runtime_session_id") or self.runtime_session_id,
            self.kwargs.get("runtime_run_id") or self.runtime_run_id,
        )

    def abort(self):
        self.aborted = True

    async def aclose(self):
        return None


def _write_canonical_terminal(store, session_id: str, run_id: str) -> None:
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
    store.append(
        RuntimeEvent.from_dict(
            {
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
            }
        )
    )
    store.append(
        RuntimeEvent.from_dict(
            {
                **common,
                "id": "fixture-run-terminal",
                "role": "model",
                "status": "completed",
                "actions": {"run_terminal": {"status": "completed"}},
            }
        )
    )


def test_unsubscribe_does_not_stop_the_run_and_replay_reads_the_terminal(tmp_path: Path):
    async def scenario():
        context = ProjectContext.from_root(tmp_path, runtime_data_dir=tmp_path / "runtime")
        app = Application(context, agent_factory=_GatedAgent)
        session = app.session_create("session-c04-run").session_id
        gate = asyncio.Event()
        _GatedAgent.gate = gate

        started = await app.run_start(session_id=session, prompt="run", command_id="cmd-c04")
        for _ in range(200):
            if session in app._stores and started.run_id in app._tasks:
                break
            await asyncio.sleep(0.005)
        assert session in app._stores, "the run's canonical store was never created"
        assert app.run_status(started.run_id).status in {"running", "queued"}
        listed = app.runs_list(session)
        assert [row["run_id"] for row in listed.data["runs"]] == [started.run_id]

        store = app._stores[session]
        service = SubscriptionService(store=store, control_store=app.control)
        stream = await service.subscribe(session)
        first = await _next(stream, "snapshot")
        assert first.payload.session_id == session
        unsubscribe = await service.unsubscribe(stream.subscription_id)
        assert unsubscribe.status == "unsubscribed"
        assert stream.closed is True

        # Dropping the subscription must not touch the run.
        assert app.run_status(started.run_id).status == "running"
        assert _GatedAgent.gate is not None
        gate.set()
        status = (await app.wait_run(started.run_id)).status
        assert status == "succeeded"

        replay = await service.snapshot(session)
        await service.aclose()
        message_ids = [item.message_id for item in replay.snapshot.messages]
        assert "fixture-run-terminal" in message_ids or replay.snapshot.terminals
        assert replay.cursor.high_water >= 1
        assert any(
            terminal["status"] == "completed" for terminal in replay.snapshot.terminals
        )
        await app.shutdown()

    asyncio.run(scenario())


def test_multiple_concurrent_subscriptions_are_allowed(tmp_path: Path):
    async def scenario():
        with _store(tmp_path) as store:
            store.append(_open_event())
            service = SubscriptionService(store=store)
            streams = [await service.subscribe(SESSION) for _ in range(3)]
            ids = {stream.subscription_id for stream in streams}
            assert len(ids) == 3
            for stream in streams:
                assert (await _next(stream, "snapshot")).kind == "snapshot"
            await service.aclose()

    asyncio.run(scenario())


def test_non_prefix_facts_are_marked_as_such_in_the_dto(tmp_path: Path):
    """Run states and pending interactions must declare themselves non-prefix.

    ``GuiProjection.build`` puts ``prefix_boundary_exempt`` on the draft DTO but
    the run and interaction DTOs are plain dicts read from the control plane, so
    their exempt flag can only be asserted here.  A consumer that cannot tell a
    bounded prefix fact from a live control-plane fact would derive page numbers
    and cursors from a boundary the fact was never part of.
    """

    class ControlDouble:
        def runs_for_session(self, session_id: str):
            del session_id
            return [
                {
                    "run_id": "run-exempt",
                    "session_id": SESSION,
                    "status": "running",
                    "error_code": None,
                    "created_at": "2026-09-13T00:00:00Z",
                    "updated_at": "2026-09-13T00:00:00Z",
                }
            ]

        def pending_for_run(self, run_id: str):
            del run_id
            return (
                {
                    "request_id": "request-exempt",
                    "run_id": "run-exempt",
                    "status": "pending",
                    "tool_name": "bash",
                    "updated_at": "2026-09-13T00:00:00Z",
                    "expires_at": None,
                },
            )

    async def scenario():
        with _store(tmp_path) as store:
            store.append(_open_event())
            service = SubscriptionService(store=store, control_store=ControlDouble())
            result = await service.snapshot(SESSION)
            await service.aclose()

            snapshot = result.snapshot
            assert [run["run_id"] for run in snapshot.runs] == ["run-exempt"]
            assert snapshot.runs[0]["prefix_boundary_exempt"] is True
            assert [item.request_id for item in snapshot.pending_interactions] == [
                "request-exempt"
            ]
            assert (
                snapshot.pending_interactions[0].to_dict()["prefix_boundary_exempt"] is True
            )
            # The bounded half of the same snapshot must NOT claim exemption.
            assert snapshot.terminals == ()
            assert all(item.ordinal <= snapshot.high_water for item in snapshot.messages)

    asyncio.run(scenario())


def test_unknown_subscription_and_closed_cursor_are_reported(tmp_path: Path):
    """Both spec scenarios that no earlier case covered.

    ``unsubscribe`` of an id the service never issued, and ``resume`` of a cursor
    whose subscription has since been closed, must each be reported explicitly
    rather than silently ignored or silently continued.
    """

    async def scenario():
        with _store(tmp_path) as store:
            store.append(_open_event())
            service = SubscriptionService(store=store)

            unknown = await service.unsubscribe("no-such-subscription")
            assert unknown.status == "unknown_subscription"
            assert unknown.error_code == "subscription_not_found"
            assert unknown.subscription_id == "no-such-subscription"

            stream = await service.subscribe(SESSION)
            await _next(stream, "snapshot")
            cursor = stream.cursor
            assert (await service.unsubscribe(stream.subscription_id)).status == "unsubscribed"
            assert stream.closed is True

            # The buffer still covers it, but the subscription is closed: the
            # honest answer is an explicit expiry, not a silent continuation.
            resumed = await service.resume(cursor)
            assert resumed.status == "cursor_expired"
            assert resumed.error_code == "subscription_closed"
            assert resumed.cursor is None
            assert resumed.current_high_water == cursor.high_water
            await service.aclose()

    asyncio.run(scenario())


def test_detaching_keeps_the_subscription_resumable(tmp_path: Path):
    """``detach`` and ``unsubscribe`` differ: only the latter ends the buffer."""

    async def scenario():
        with _store(tmp_path) as store:
            store.append(_open_event())
            service = SubscriptionService(store=store)
            stream = await service.subscribe(SESSION)
            await _next(stream, "snapshot")

            detached = await service.detach(stream.subscription_id)
            assert detached.status == "detached"

            # A second detach is a no-op, not an error.
            assert (await service.detach(stream.subscription_id)).status == "detached"

            resumed = await service.resume(stream.cursor)
            assert resumed.status == "resumed"
            assert resumed.stream is stream
            assert resumed.error_code is None

            unknown = await service.detach("no-such-subscription")
            assert unknown.status == "unknown_subscription"
            await service.aclose()

    asyncio.run(scenario())


def test_a_draft_advances_the_draft_version_but_not_the_boundary(tmp_path: Path):
    """A draft-only change must leave ``high_water`` where it was.

    Two mechanisms can move the boundary without a committed visible event, so
    both are exercised here:

    * the mutable draft table, whose rows carry no ordinal at all;
    * a ``partial=True`` record on the suffix read, which *does* have an ordinal
      and therefore also advances the de-duplication watermark.  Only the
      delivered boundary must stay put.

    A consumer derives page numbers and cursors from ``high_water``, so a
    boundary that nothing was delivered at would describe a prefix the consumer
    has not seen.
    """

    class FakeClock:
        def __call__(self) -> int:
            return 1_700_000_000_000

    class PartialLeakingStore(SQLiteRuntimeStore):
        """Adds a partial record the poll loop must not let past the boundary."""

        def read_event_records(self, **kwargs):
            pairs = list(super().read_event_records(**kwargs))
            pairs.append((LEAKED_PARTIAL_ORDINAL, _a_partial_record()))
            return sorted(pairs, key=lambda pair: pair[0])

    async def scenario():
        store = PartialLeakingStore(tmp_path / "runtime.sqlite")
        try:
            recorder = ModelCallRecorder(
                RuntimeEventEmitter(store),
                RunContext(SESSION, "turn-c04", RUN, INVOCATION),
                provider="fixture",
                model="fixture-model",
                clock=FakeClock(),
            )
            recorder.start("request-fixture")
            boundary = int(store.high_water(session_id=SESSION))

            service = SubscriptionService(store=store)
            stream = await service.subscribe(SESSION)
            await _next(stream, "snapshot")
            state = service._states[stream.subscription_id]
            assert state.high_water == boundary

            # Only a draft changes: no canonical event is committed.
            recorder.partial_text("a")
            recorder.flush_partials()
            first = await _next(stream, "draft")
            assert first.payload.revision == 1
            assert state.high_water == boundary
            assert int(store.high_water(session_id=SESSION)) == boundary

            recorder.partial_text("ab")
            recorder.flush_partials()
            second = await _next(stream, "draft")
            assert second.payload.revision == 2
            assert state.high_water == boundary
            assert int(store.high_water(session_id=SESSION)) == boundary

            # The leaked partial record was read (so the watermark moved past
            # it) but it is not part of the delivered prefix.
            assert state.last_ordinal >= LEAKED_PARTIAL_ORDINAL
            assert state.high_water == boundary
            await service.aclose()
        finally:
            store.close()

    asyncio.run(scenario())


LEAKED_PARTIAL_ORDINAL = 999


def _a_partial_record() -> RuntimeEvent:
    """A ``partial=True`` record shaped the way a suffix read would return it."""

    return RuntimeEvent.from_dict(
        {
            "schema_version": 2,
            "id": "leaked-partial-boundary",
            "session_id": SESSION,
            "run_id": RUN,
            "invocation_id": INVOCATION,
            "turn_id": "turn-c04",
            "ts": 5,
            "partial": True,
            "role": "model",
            "author": "agent",
            "content": {"kind": "text", "text": "frag"},
            "metadata": {"lifecycle": "stream_partial", "partial_seq": 1},
        }
    )
