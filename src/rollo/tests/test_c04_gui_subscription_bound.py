"""C04 subscription bound contracts (layer L1: injected store).

The overflow bound is a property of the service's own backlog accounting, so it
is tested here with a store whose suffix reads the test controls exactly.  That
removes every timing guess: a "tick" is an explicit ``await``, and whether the
subscriber progressed is an explicit read or an explicit non-read.

The real-store delivery path (boundary, pagination, resume, drafts, read-only)
is covered in ``test_c04_gui_subscriptions.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from rollo.projections import subscriptions as subs
from rollo.projections.subscriptions import GuiMessage, SubscriptionService
from rollo.projections.subscriptions import _CLOSE as _CLOSE_SENTINEL
from rollo.runtime_event import RuntimeEvent

SESSION = "session-c04-bound"


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


def _text_event(index: int, *, session_id: str = SESSION) -> RuntimeEvent:
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
            "content": {"kind": "text", "text": f"m{index}"},
            "metadata": {"lifecycle": "model_final"},
        }
    )


def _terminal_event(*, session_id: str = SESSION) -> RuntimeEvent:
    return RuntimeEvent.from_dict(
        {
            "schema_version": 2,
            "id": f"terminal-{session_id}",
            "session_id": session_id,
            "run_id": f"run-{session_id}",
            "invocation_id": f"inv-{session_id}",
            "turn_id": "turn-c04",
            "ts": 9_000,
            "partial": False,
            "role": "model",
            "author": "agent",
            "status": "completed",
            "actions": {"run_terminal": {"status": "completed"}},
        }
    )


class ScriptedStore:
    """Store whose ledger only moves when the test says so.

    Ordinals are assigned globally, exactly like the real store's, so the
    strictly-greater-than semantics of ``after_ordinal`` are exercised.
    """

    def __init__(self) -> None:
        self.ledger: list[RuntimeEvent] = []
        self.ordinals: dict[str, int] = {}
        self._next = 1

    def append(self, event: RuntimeEvent) -> int:
        ordinal = self._next
        self._next += 1
        self.ledger.append(event)
        self.ordinals[event.id] = ordinal
        return ordinal

    def high_water(self, *, session_id: str | None = None, run_id: str | None = None) -> int:
        del run_id
        values = [
            self.ordinals[event.id]
            for event in self.ledger
            if session_id is None or event.session_id == session_id
        ]
        return max(values, default=0)

    def read_event_records(self, **kwargs):
        session_id = kwargs.get("session_id")
        high_water = kwargs.get("high_water")
        after = kwargs.get("after_ordinal")
        pairs = []
        for event in self.ledger:
            ordinal = self.ordinals[event.id]
            if session_id is not None and event.session_id != session_id:
                continue
            if high_water is not None and ordinal > high_water:
                continue
            if after is not None and ordinal <= after:
                continue
            pairs.append((ordinal, event))
        return sorted(pairs, key=lambda pair: pair[0])

    def read_runtime_stream_partials(self, **kwargs):
        del kwargs
        return []

    def read_event(self, event_id: str):
        return next((event for event in self.ledger if event.id == event_id), None)


async def _tick(service: SubscriptionService, *, times: int = 1) -> None:
    """Advance the pull loop by an exact number of poll ticks."""

    for _ in range(times):
        await asyncio.sleep(subs.POLL_INTERVAL_SECONDS * 3)


async def _next(stream, kind: str, *, timeout: float = 5.0) -> GuiMessage:
    async def _take() -> GuiMessage:
        async for message in stream:
            if message.kind == kind:
                return message
        raise AssertionError(f"stream ended before a {kind!r} message arrived")

    return await asyncio.wait_for(_take(), timeout=timeout)


async def _drain(stream, *, limit: int = 64, timeout: float = 5.0) -> list[GuiMessage]:
    collected: list[GuiMessage] = []

    async def _take() -> None:
        async for message in stream:
            collected.append(message)
            if len(collected) >= limit:
                return

    try:
        await asyncio.wait_for(_take(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    return collected


def terminal_message(event: RuntimeEvent) -> GuiMessage:
    return GuiMessage(
        kind="event",
        key=event.id,
        prefix_boundary_exempt=False,
        ordinal=9_000,
        payload=event.to_dict(),
    )


def test_stalled_subscriber_is_closed_once_with_the_backlog_intact(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(subs, "SUBSCRIPTION_BUFFER_LIMIT", 3)
    # Only the grace period is shortened: the clock itself is under test.
    monkeypatch.setattr(subs, "SUBSCRIPTION_STALL_GRACE_SECONDS", 0.0)

    async def scenario():
        store = ScriptedStore()
        store.append(_open_event())
        store.append(_text_event(1))
        store.append(_text_event(2))
        service = SubscriptionService(store=store)
        stalled = await service.subscribe(SESSION)
        await _next(stalled, "snapshot")

        for index in range(3, 12):
            store.append(_text_event(index))

        # Nobody reads: the backlog never shrinks, so the service closes it.
        for _ in range(200):
            if stalled.closed:
                break
            await _tick(service)
        assert stalled.closed is True
        assert stalled.terminal_kind is None  # nothing read yet

        delivered = await _drain(stalled)
        kinds = [message.kind for message in delivered]
        assert kinds.count("resync_required") == 1
        assert kinds[-1] == "resync_required"
        # A subscriber this far behind cannot be served without loss: the bound
        # keeps the newest entries (the newest of which is delivered last).  What
        # is guaranteed is that the loss is bounded, announced exactly once, and
        # never silent -- and that ordinals never go backwards.
        ordinals = [message.ordinal for message in delivered if message.kind == "event"]
        assert ordinals == sorted(ordinals)
        assert ordinals and ordinals[-1] == 12
        assert len(ordinals) <= subs.SUBSCRIPTION_BUFFER_LIMIT + 1
        assert stalled.terminal_kind == "resync_required"
        assert delivered[-1].payload["buffer_limit"] == 3

        # Re-running the check must not emit a second notice.
        await _tick(service, times=3)
        assert stalled.closed is True
        assert (await _drain(stalled, limit=4, timeout=1.0)) == []
        await service.aclose()

    asyncio.run(scenario())


def test_reading_subscriber_is_never_closed_by_a_burst(tmp_path: Path, monkeypatch):
    """The bound measures a stalled subscriber, not a fast producer."""

    monkeypatch.setattr(subs, "SUBSCRIPTION_BUFFER_LIMIT", 3)

    async def scenario():
        store = ScriptedStore()
        store.append(_open_event())
        service = SubscriptionService(store=store)
        consumer = await service.subscribe(SESSION)
        await _next(consumer, "snapshot")

        for index in range(1, 25):
            store.append(_text_event(index))

        seen: list[int] = []
        async for message in consumer:
            assert message.kind == "event"
            seen.append(message.ordinal)
            if len(seen) >= 24:
                break
        assert seen == list(range(2, 26))
        assert consumer.closed is False
        assert consumer.terminal_kind is None

        # The consumer has now stopped reading; only then may the service close
        # the subscription, and only once the backlog passes the bound.
        for index in range(90, 100):
            store.append(_text_event(index))
        for _ in range(200):
            if consumer.closed:
                break
            await _tick(service)
        assert consumer.closed is True
        assert consumer.terminal_kind is None  # nothing read after the burst
        await service.aclose()

    asyncio.run(scenario())


def test_terminal_and_interaction_survive_a_full_backlog(tmp_path: Path, monkeypatch):
    """At the bound, a terminal and an interaction are queued, never dropped."""

    monkeypatch.setattr(subs, "SUBSCRIPTION_BUFFER_LIMIT", 3)

    async def scenario():
        store = ScriptedStore()
        store.append(_open_event())
        service = SubscriptionService(store=store)
        stream = await service.subscribe(SESSION)
        await _next(stream, "snapshot")
        state = service._states[stream.subscription_id]

        # A backlog of exactly the declared bound, delivered to the queue.
        for ordinal in (2, 3, 4):
            state.queue.put_nowait(
                GuiMessage(
                    kind="event",
                    key=f"queued-{ordinal}",
                    prefix_boundary_exempt=False,
                    ordinal=ordinal,
                    payload={},
                )
            )
        assert service._pending(state) == subs.SUBSCRIPTION_BUFFER_LIMIT

        terminal = _terminal_event()
        interaction = GuiMessage(
            kind="interaction",
            key="request-1",
            prefix_boundary_exempt=True,
            request_id="request-1",
            run_id="run-1",
            payload={"request_id": "request-1"},
        )
        # Both arrive while the backlog is saturated.  The service keeps the
        # newest entries: a terminal and an interaction are never merged away.
        await service._enqueue(state, terminal_message(terminal))
        await service._enqueue(state, interaction)
        await service._overflow(state)
        assert state.closed is True

        delivered = await _drain(stream, limit=20)
        await service.aclose()

        keys = [message.key for message in delivered if message.kind == "event"]
        assert terminal.id in keys
        assert [message.request_id for message in delivered if message.kind == "interaction"] == [
            "request-1"
        ]
        assert delivered[-1].kind == "resync_required"

    asyncio.run(scenario())


def test_stall_clock_uses_the_declared_grace_period(tmp_path: Path, monkeypatch):
    """The default grace period is what decides, not merely a patched zero."""

    async def scenario():
        store = ScriptedStore()
        store.append(_open_event())
        service = SubscriptionService(store=store)
        stream = await service.subscribe(SESSION)
        await _next(stream, "snapshot")
        state = service._states[stream.subscription_id]
        # Stop the pull loop: this case is about the clock, not about timing.
        await service._cancel_task(state)

        # Under the bound: nothing is stalled no matter how long it sits.
        assert service._at_bound(state) is False
        state.stalled_at = None
        assert service._stall_expired(state) is False

        # At the bound the clock starts, and only the declared grace period
        # turns it into an expiry.
        state.queue.put_nowait(
            GuiMessage(
                kind="event",
                key="queued",
                prefix_boundary_exempt=False,
                ordinal=2,
                payload={},
            )
        )
        monkeypatch.setattr(subs, "SUBSCRIPTION_BUFFER_LIMIT", 1)
        assert service._pending(state) >= subs.SUBSCRIPTION_BUFFER_LIMIT
        assert service._at_bound(state) is True
        assert state.stalled_at is not None
        assert service._stall_expired(state) is False

        state.stalled_at = subs._monotonic() - (
            subs.SUBSCRIPTION_STALL_GRACE_SECONDS + 0.01
        )
        assert service._stall_expired(state) is True

        await service.aclose()

    asyncio.run(scenario())


def test_overflow_leaves_other_subscriptions_open(tmp_path: Path, monkeypatch):
    """One stalled subscriber must not take its siblings down with it."""

    monkeypatch.setattr(subs, "SUBSCRIPTION_BUFFER_LIMIT", 3)
    monkeypatch.setattr(subs, "SUBSCRIPTION_STALL_GRACE_SECONDS", 0.0)

    async def scenario():
        store = ScriptedStore()
        store.append(_open_event())
        service = SubscriptionService(store=store)
        stalled = await service.subscribe(SESSION)
        sibling = await service.subscribe(SESSION)
        await _next(stalled, "snapshot")
        assert (await _next(sibling, "snapshot")).kind == "snapshot"
        assert sibling.messages_delivered == 1

        for index in range(1, 8):
            store.append(_text_event(index))

        # The sibling keeps consuming while the other subscription stalls.
        delivered: list[int] = []

        async def _consume() -> None:
            async for message in sibling:
                delivered.append(message.ordinal)
                if len(delivered) >= 7:
                    return

        async def _stall() -> None:
            for _ in range(400):
                if stalled.closed:
                    return
                await _tick(service)

        await asyncio.wait_for(asyncio.gather(_consume(), _stall()), timeout=10.0)

        assert stalled.closed is True
        assert service._states[stalled.subscription_id].overflowed is True
        sibling_state = service._states[sibling.subscription_id]
        assert sibling.closed is False
        assert sibling_state.overflowed is False
        assert sibling.terminal_kind is None
        assert delivered == list(range(2, 9))
        await service.aclose()

    asyncio.run(scenario())


def test_draft_revision_at_the_bound_replaces_instead_of_appending(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(subs, "SUBSCRIPTION_BUFFER_LIMIT", 3)

    async def scenario():
        store = ScriptedStore()
        store.append(_open_event())
        service = SubscriptionService(store=store)
        stream = await service.subscribe(SESSION)
        await _next(stream, "snapshot")
        state = service._states[stream.subscription_id]
        assert service._pending(state) == 0

        def draft(revision: int) -> subs.GuiDraft:
            return subs.GuiDraft(
                stream_key="partial:inv:text",
                revision=revision,
                partial_seq=revision,
                stream_kind="text",
                tool_call_id=None,
                size=revision,
                fragment_count=revision,
                payload={"content": {"kind": "text", "text": "x" * revision}},
            )

        for revision in (1, 2, 3):
            await service._enqueue(
                state,
                GuiMessage(
                    kind="draft",
                    key="partial:inv:text",
                    prefix_boundary_exempt=True,
                    payload=draft(revision),
                ),
            )
        assert service._pending(state) == 3
        assert state.closed is False

        # A fourth revision has nowhere to go, but the same stream merges.
        await service._enqueue(
            state,
            GuiMessage(
                kind="draft",
                key="partial:inv:text",
                prefix_boundary_exempt=True,
                payload=draft(4),
            ),
        )
        assert service._pending(state) == 3
        assert state.closed is False
        assert [item.payload.revision for item in state.buffer] == [1, 2, 4]

        delivered = await _drain(stream, limit=10, timeout=1.0)
        await service.aclose()
        revisions = [
            message.payload.revision for message in delivered if message.kind == "draft"
        ]
        assert revisions == [1, 2, 4]

    asyncio.run(scenario())


async def _collect_matching(stream, predicate, *, timeout: float = 1.0) -> list[GuiMessage]:
    """Collect every message matching ``predicate`` for a bounded time."""

    collected: list[GuiMessage] = []

    async def _run() -> None:
        async for message in stream:
            if predicate(message):
                collected.append(message)

    try:
        await asyncio.wait_for(_run(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    return collected


def test_partial_canonical_record_is_never_delivered(tmp_path: Path, monkeypatch):
    """A ``partial`` record on the suffix read must never become an event.

    The real store routes streaming observations into the mutable draft table
    and never into ``runtime_events`` -- its validation rejects exactly the
    shape that would leak -- so this guard is defensive rather than hot.  It is
    still load bearing: the pull loop is the only thing standing between a store
    that hands back a partial record and a GUI that would treat a draft fragment
    as a committed canonical event and drag its boundary along with it.  The
    double returns such a record on purpose, because a real store cannot be made
    to produce one.
    """

    monkeypatch.setattr(subs, "SUBSCRIPTION_BUFFER_LIMIT", 3)

    class PartialLeakingStore(ScriptedStore):
        """Adds a partial record the loop's own filter is there to stop."""

        def read_event_records(self, **kwargs):
            pairs = list(super().read_event_records(**kwargs))
            pairs.append((LEAKED_ORDINAL, _leaked_partial_event()))
            return sorted(pairs, key=lambda pair: pair[0])

    async def scenario():
        store = PartialLeakingStore()
        store.append(_open_event())
        store.append(_text_event(1))
        service = SubscriptionService(store=store)
        stream = await service.subscribe(SESSION)
        await _next(stream, "snapshot")
        state = service._states[stream.subscription_id]

        # The record is genuinely reachable on the suffix read...
        seen = store.read_event_records(session_id=SESSION, after_ordinal=0)
        assert any(event.partial for _ordinal, event in seen)

        delivered = await _collect_matching(stream, lambda message: True, timeout=1.0)
        await service.aclose()

        # ...yet it never surfaces as a canonical event and never moves the
        # live boundary.
        delivered_ids = [
            message.payload["id"] for message in delivered if message.kind == "event"
        ]
        assert "leaked-partial" not in delivered_ids
        assert state.high_water == 2

    asyncio.run(scenario())


LEAKED_ORDINAL = 999


def _leaked_partial_event() -> RuntimeEvent:
    """A ``partial=True`` record shaped the way a suffix read would return it."""

    return RuntimeEvent.from_dict(
        {
            "schema_version": 2,
            "id": "leaked-partial",
            "session_id": SESSION,
            "run_id": f"run-{SESSION}",
            "invocation_id": f"inv-{SESSION}",
            "turn_id": "turn-c04",
            "ts": 5,
            "partial": True,
            "role": "model",
            "author": "agent",
            "content": {"kind": "text", "text": "frag"},
            "metadata": {"lifecycle": "stream_partial", "partial_seq": 1},
        }
    )


def test_a_pending_interaction_with_a_tool_name_is_delivered(tmp_path: Path, monkeypatch):
    """An interaction that appears *after* subscribe must still be delivered.

    Every pending interaction raised by the runtime names the tool awaiting
    approval, so filtering on ``tool_name`` would silently drop all of them.
    The control plane starts empty on purpose: an interaction already present at
    subscribe time is carried by the snapshot, so only an arrival after
    registration exercises the incremental ``_poll_control`` path.
    """

    monkeypatch.setattr(subs, "SUBSCRIPTION_BUFFER_LIMIT", 3)

    class ControlDouble:
        """Minimal control plane whose pending set the test mutates at will."""

        def __init__(self) -> None:
            self.pending: list[dict] = []

        def runs_for_session(self, session_id: str):
            del session_id
            return [_RUN_ROW]

        def pending_for_run(self, run_id: str):
            del run_id
            return tuple(dict(item) for item in self.pending)

    async def scenario():
        control = ControlDouble()
        store = ScriptedStore()
        store.append(_open_event())
        service = SubscriptionService(store=store, control_store=control)
        stream = await service.subscribe(SESSION)
        assert (await _next(stream, "snapshot")).payload.pending_interactions == ()

        control.pending.append(dict(_PENDING_ROW))
        delivered = await _take(
            stream, lambda message: message.kind == "interaction", timeout=5.0
        )
        await service.aclose()

        assert [item.request_id for item in delivered] == ["request-tool"]
        assert delivered[0].run_id == "run-control"
        assert delivered[0].key == "request-tool"
        assert delivered[0].payload["tool_name"] == "bash"

    asyncio.run(scenario())


def test_a_pending_interaction_is_also_carried_by_the_snapshot(tmp_path: Path, monkeypatch):
    """The same interaction, present before subscribe, rides in the snapshot."""

    monkeypatch.setattr(subs, "SUBSCRIPTION_BUFFER_LIMIT", 3)

    class ControlDouble:
        def runs_for_session(self, session_id: str):
            del session_id
            return [_RUN_ROW]

        def pending_for_run(self, run_id: str):
            del run_id
            return (dict(_PENDING_ROW),)

    async def scenario():
        store = ScriptedStore()
        store.append(_open_event())
        service = SubscriptionService(store=store, control_store=ControlDouble())
        stream = await service.subscribe(SESSION)
        snapshot = await _next(stream, "snapshot")
        assert [item.request_id for item in snapshot.payload.pending_interactions] == [
            "request-tool"
        ]
        # Already known, so the incremental path must not repeat it.
        delivered = await _take(
            stream, lambda message: message.kind == "interaction", timeout=1.0
        )
        await service.aclose()
        assert delivered == []

    asyncio.run(scenario())


_RUN_ROW = {
    "run_id": "run-control",
    "session_id": SESSION,
    "status": "running",
    "error_code": None,
    "created_at": "2026-09-13T00:00:00Z",
    "updated_at": "2026-09-13T00:00:00Z",
}

_PENDING_ROW = {
    "request_id": "request-tool",
    "run_id": "run-control",
    "status": "pending",
    "tool_name": "bash",
    "updated_at": "2026-09-13T00:00:00Z",
    "expires_at": None,
}


async def _take(stream, predicate, *, timeout: float = 5.0) -> list[GuiMessage]:
    """Collect the first message matching ``predicate``, or nothing on timeout."""

    collected: list[GuiMessage] = []

    async def _run() -> None:
        async for message in stream:
            if predicate(message):
                collected.append(message)
                return

    try:
        await asyncio.wait_for(_run(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    return collected


def test_a_new_entry_replaces_the_oldest_queued_one(tmp_path: Path, monkeypatch):
    """At the bound the *oldest* queued entry goes, not some other one.

    Queued count, last ordinal and the presence of a notice are identical under
    "drop the stalest", "drop the newest" and "keep everything", so only the
    surviving identities discriminate.  ``_enqueue`` is the path that owns the
    decision and it is called directly; the drain is neutralised so the
    background pump cannot move entries underneath the observation.
    """

    monkeypatch.setattr(subs, "SUBSCRIPTION_BUFFER_LIMIT", 3)

    async def scenario():
        store = ScriptedStore()
        store.append(_open_event())
        service = SubscriptionService(store=store)
        stream = await service.subscribe(SESSION)
        await _next(stream, "snapshot")
        state = service._states[stream.subscription_id]

        # Neutralise the drain *after* the snapshot has been delivered by it,
        # and stop the pump: otherwise either one moves entries between queue
        # and buffer underneath the measurement.
        monkeypatch.setattr(SubscriptionService, "_drain", _no_drain, raising=True)
        state.task = None

        def entry(ordinal: int) -> GuiMessage:
            return GuiMessage(
                kind="event",
                key=f"queued-{ordinal}",
                prefix_boundary_exempt=False,
                ordinal=ordinal,
                payload={},
            )

        for ordinal in (2, 3, 4):
            state.queue.put_nowait(entry(ordinal))
        assert state.queue.qsize() == subs.SUBSCRIPTION_BUFFER_LIMIT
        assert not state.buffer

        await service._enqueue(state, entry(5))
        await service.aclose()

        survivors = [
            message.ordinal
            for message in list(state.queue._queue)
            if message is not _CLOSE_SENTINEL
        ]
        # The stalest queued entry is gone, the newcomer waits behind the
        # survivors, and the backlog stays at the declared bound.
        assert survivors == [3, 4]
        assert [message.ordinal for message in state.buffer] == [5]

    asyncio.run(scenario())


def test_a_terminal_is_never_displaced_by_a_newcomer(tmp_path: Path, monkeypatch):
    """Terminals and interactions are not evictable, even when they are oldest.

    A terminal or an interaction reports a state transition the consumer cannot
    recover by re-reading the prefix, so displacing one would strand a GUI on a
    run that already finished.  The bound is therefore a soft target: when
    nothing disposable is left, the backlog may sit at ``LIMIT + 1`` rather than
    drop a protected entry.

    This complements ``test_terminal_and_interaction_survive_a_full_backlog``,
    which queues them *behind* ordinary entries and so never reached the
    eviction decision.
    """

    monkeypatch.setattr(subs, "SUBSCRIPTION_BUFFER_LIMIT", 3)

    async def scenario():
        store = ScriptedStore()
        store.append(_open_event())
        service = SubscriptionService(store=store)
        stream = await service.subscribe(SESSION)
        await _next(stream, "snapshot")
        state = service._states[stream.subscription_id]

        def entry(ordinal: int, *, terminal: bool = False) -> GuiMessage:
            payload = {"actions": {"run_terminal": {"status": "completed"}}} if terminal else {}
            return GuiMessage(
                kind="event",
                key=f"queued-{ordinal}",
                prefix_boundary_exempt=False,
                ordinal=ordinal,
                payload=payload,
            )

        # The terminal is the STALEST entry, so a naive trim would take it first.
        state.queue.put_nowait(entry(2, terminal=True))
        state.queue.put_nowait(entry(3))
        state.queue.put_nowait(entry(4))
        assert state.queue.qsize() == subs.SUBSCRIPTION_BUFFER_LIMIT

        await service._enqueue(state, entry(5))
        await service.aclose()

        survivors = [m.ordinal for m in list(state.queue._queue) if m is not _CLOSE_SENTINEL]
        assert survivors == [2, 4], "the terminal must survive; the stalest event goes"
        assert [m.ordinal for m in state.buffer] == [5]

    asyncio.run(scenario())


def test_an_interaction_in_the_middle_is_never_displaced(tmp_path: Path, monkeypatch):
    """A protected entry in the middle of the queue survives a saturated arrival.

    Complements the terminal case at the head: here the trim *does* have a
    disposable victim, so the only reason the interaction survives is the
    protection itself.
    """

    monkeypatch.setattr(subs, "SUBSCRIPTION_BUFFER_LIMIT", 2)

    async def scenario():
        store = ScriptedStore()
        store.append(_open_event())
        service = SubscriptionService(store=store)
        stream = await service.subscribe(SESSION)
        await _next(stream, "snapshot")
        state = service._states[stream.subscription_id]

        # The interaction is the ONLY unprotected entry: the other slot holds a
        # run terminal.  A trim that ignored the interaction's protection would
        # take it, because it is the only candidate left.
        state.queue.put_nowait(
            GuiMessage(
                kind="event",
                key="terminal-2",
                prefix_boundary_exempt=False,
                ordinal=2,
                payload={"actions": {"run_terminal": {"status": "completed"}}},
            )
        )
        state.queue.put_nowait(
            GuiMessage(
                kind="interaction",
                key="request-protected",
                prefix_boundary_exempt=True,
                request_id="request-protected",
                run_id="run-protected",
                payload={"request_id": "request-protected"},
            )
        )
        assert state.queue.qsize() == subs.SUBSCRIPTION_BUFFER_LIMIT

        await service._enqueue(
            state,
            GuiMessage(
                kind="event",
                key="queued-3",
                prefix_boundary_exempt=False,
                ordinal=3,
                payload={},
            ),
        )
        await service.aclose()

        # Nothing disposable existed, so nothing was evicted: the bound went soft
        # rather than lose the interaction.
        queued = [m for m in list(state.queue._queue) if m is not _CLOSE_SENTINEL]
        assert [m.key for m in queued] == ["terminal-2", "request-protected"]
        assert [m.key for m in state.buffer] == ["queued-3"]

    asyncio.run(scenario())


async def _no_drain(state) -> None:
    """Stand-in for ``SubscriptionService._drain`` that moves nothing."""

    del state
