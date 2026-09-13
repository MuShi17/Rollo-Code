"""Ordinal pull-based subscription service for the bounded GUI projection.

Design boundary (delivery side)
-------------------------------

The subscription service derives *all* of its facts by reading:

* ``store.high_water(session_id=...)`` -- the boundary of the immutable prefix;
* ``store.read_event_records(session_id=..., after_ordinal=last)`` -- the
  ordinal suffix (the same warm-replay path ``Agent`` already uses);
* ``store.read_runtime_stream_partials(session_id=...)`` -- mutable drafts,
  which advance no ordinal and therefore can never be seen by the suffix read;
* the C03 control store -- run status and pending interactions, which carry no
  ordinal either.

There is deliberately **no** write-side hook.  ``SQLiteRuntimeStore.append`` is
reached through ``RuntimeEventEmitter.emit``, a pure forwarder that does not
know this service exists, so there is nothing to register a listener with.
``OutputPort`` is a wake-up hint only (no ordinal, not exhaustive, and
``emit_safely`` swallows exceptions); it is never a source of truth here.

Gap freedom therefore comes from ordinal monotonicity plus the left-open
``after_ordinal`` interval, *not* from a lock.  ``asyncio.Lock`` is used only to
serialise this service's own registration/trim/publish state.

Scope: single process, single event loop.  The delivery path performs no
``await`` between completing a series of reads and publishing them, so the
observed high-water and the published records are one consistent step.  Cross
process linearisation belongs to C05.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Mapping

from ..runtime_store import SQLiteRuntimeStore
from .base import EventRecord, json_value
from .gui_projection import (
    GUI_PROJECTION_VERSION,
    BODY_PAGE_SIZE,
    GuiDraft,
    GuiMessageRef,
    GuiPendingInteraction,
    GuiProjection,
    GuiSnapshot,
    event_body,
)

#: Maximum number of messages that may wait for one subscriber.  A subscription
#: whose backlog stays at this bound for longer than the grace period without
#: consuming is closed with ``resync_required``.
SUBSCRIPTION_BUFFER_LIMIT = 256

#: How long a subscriber may hold a backlog at or above the bound without
#: consuming anything before it is considered stalled.  A client that keeps
#: reading (even slowly) resets the clock on every message it takes.
SUBSCRIPTION_STALL_GRACE_SECONDS = 0.25

#: Poll interval of the pull loop, in seconds.
POLL_INTERVAL_SECONDS = 0.01

_CLOSE = object()

#: Draft stream-key derivation, mirroring
#: ``rollo.runtime_lifecycle.ModelCallRecorder._event_stream_key`` verbatim.
#: This is a contract mirror, not a shared helper: if that private method
#: drifts, ``test_c04_gui_subscriptions.py`` fails in both directions.
def derive_stream_key(
    *, invocation_id: str, metadata: Mapping[str, Any] | None, content: Mapping[str, Any] | None
) -> str:
    """Mirror of ``ModelCallRecorder._event_stream_key``."""

    metadata = metadata or {}
    value = metadata.get("partial_stream_key")
    if isinstance(value, str) and value.strip():
        return value
    content = content or {}
    call_id = content.get("id") if content.get("kind") == "function_call" else ""
    attempt_id = metadata.get("attempt_id") or ""
    return ":".join(
        (
            "partial",
            str(invocation_id),
            str(attempt_id),
            str(content.get("kind") or ""),
            str(call_id or ""),
        )
    )


def _monotonic() -> float:
    return time.monotonic()


#: Message kinds a slow subscriber may lose without losing a state transition.
#: An interaction signals that the run is blocked waiting for an answer, and the
#: three closing notices are the subscriber's only warning that its view is no
#: longer continuous -- none of them can be recovered by re-reading the prefix.
#: Dropping one would strand a GUI, which is what "MUST NOT 静默消失" forbids.
_PROTECTED_KINDS = frozenset(
    {"interaction", "resync_required", "cursor_expired", "stream_closed"}
)


def _is_disposable(message: GuiMessage) -> bool:
    """Whether a queued message may be displaced to keep the backlog bounded."""

    if message.kind in _PROTECTED_KINDS:
        return False
    # A run terminal is NOT its own kind: the delivery path frames every
    # canonical record as ``kind == "event"``, so the terminal is recognised by
    # its payload.  A ``kind == "terminal"`` branch would be dead code -- the
    # service never produces that kind.
    if message.kind == "event" and isinstance(message.payload, Mapping):
        actions = message.payload.get("actions") or {}
        if isinstance(actions, Mapping) and actions.get("run_terminal"):
            return False
    return True


def _storage_key(partial: Any) -> str:
    """The authoritative key of a persisted draft.

    ``SQLiteRuntimeStore`` stores a draft under ``metadata.partial_stream_key``
    when present, otherwise under the same derived fallback; its own
    ``stream_key`` column is that stored primary key.  The derived mirror is
    kept as a second candidate so a row whose metadata disagrees with its
    primary key still collapses onto one GUI draft.
    """

    payload = partial.payload
    if isinstance(payload, Mapping):
        metadata = payload.get("metadata") or {}
        content = payload.get("content") or {}
        invocation_id = str(payload.get("invocation_id") or "")
    else:
        metadata = getattr(payload, "metadata", None) or {}
        content = getattr(payload, "content", None) or {}
        invocation_id = str(getattr(payload, "invocation_id", "") or "")
    if invocation_id:
        candidate = derive_stream_key(
            invocation_id=invocation_id, metadata=metadata, content=content
        )
        if candidate:
            return candidate
    return str(partial.stream_key)


class SubscriptionError(RuntimeError):
    """The subscription request cannot be served."""

    code = "subscription_error"


@dataclass(frozen=True, slots=True)
class GuiCursor:
    """Process-local resume token.

    ``subscription_id`` is an in-process identity; its wire encoding is C05's
    concern.  ``service_epoch`` is the identity of the ``SubscriptionService``
    instance that issued the cursor, so a replaced instance can never silently
    continue someone else's buffer.
    """

    subscription_id: str
    session_id: str
    high_water: int
    projection_version: str
    partial_versions: tuple[tuple[str, int], ...] = ()
    service_epoch: str = ""

    def partial_version(self, stream_key: str) -> int | None:
        return dict(self.partial_versions).get(stream_key)

    def to_dict(self) -> dict[str, Any]:
        """The cursor as a client must be able to hand it back.

        ``service_epoch`` is part of the cursor's identity and the resume path
        validates it, so a token that omits it can never be resumed: the service
        would compare an empty epoch against its own and report the buffer as
        invalid.  A transport that mirrors this dict verbatim is therefore
        resumable; one that drops a field is not.
        """

        return {
            "subscription_id": self.subscription_id,
            "session_id": self.session_id,
            "high_water": self.high_water,
            "projection_version": self.projection_version,
            "partial_versions": dict(self.partial_versions),
            "service_epoch": self.service_epoch,
        }


@dataclass(frozen=True, slots=True)
class GuiMessage:
    """One delivery item; the shape C05 will frame on the wire."""

    kind: str
    key: str | None
    prefix_boundary_exempt: bool
    payload: Any
    ordinal: int | None = None
    body_ref: str | None = None
    run_id: str | None = None
    request_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "key": self.key,
            "prefix_boundary_exempt": self.prefix_boundary_exempt,
            "ordinal": self.ordinal,
            "body_ref": self.body_ref,
            "run_id": self.run_id,
            "request_id": self.request_id,
            "payload": self.payload,
        }


@dataclass(frozen=True, slots=True)
class GuiSnapshotResult:
    status: str
    cursor: GuiCursor
    snapshot: GuiSnapshot
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class GuiResumeResult:
    status: str
    cursor: GuiCursor | None
    stream: "GuiStream | None" = None
    error_code: str | None = None
    current_high_water: int = 0


@dataclass(frozen=True, slots=True)
class GuiUnsubscribeResult:
    status: str
    subscription_id: str
    error_code: str | None = None


@dataclass(slots=True)
class _SubscriptionState:
    subscription_id: str
    session_id: str
    projection_version: str
    service_epoch: str
    last_ordinal: int
    high_water: int
    buffer: deque[GuiMessage] = field(default_factory=deque)
    drafts: dict[str, GuiDraft] = field(default_factory=dict)
    seen_runs: dict[str, str] = field(default_factory=dict)
    seen_requests: set[str] = field(default_factory=set)
    closed: bool = False
    overflowed: bool = False
    expired: bool = False
    subscribed: bool = True
    resumable_high_water: int | None = None
    stalled_at: float | None = None
    queue: "asyncio.Queue[Any]" = field(default_factory=asyncio.Queue)
    task: "asyncio.Task[None] | None" = None


class GuiStream:
    """Async-iterator delivery for one subscription.

    Delivery is **at-least-once**: the service never emits the same canonical
    ordinal twice, but a caller that has reconnected must still de-duplicate by
    ``ordinal`` because a message may have been queued before the cursor was
    taken.
    """

    def __init__(self, state: _SubscriptionState, service: "SubscriptionService") -> None:
        self._state = state
        self._service = service
        self._messages = 0
        self._terminal_kind: str | None = None

    @property
    def subscription_id(self) -> str:
        return self._state.subscription_id

    @property
    def session_id(self) -> str:
        return self._state.session_id

    @property
    def high_water(self) -> int:
        return self._state.high_water

    @property
    def messages_delivered(self) -> int:
        return self._messages

    @property
    def closed(self) -> bool:
        return self._state.closed

    @property
    def terminal_kind(self) -> str | None:
        return self._terminal_kind

    def __aiter__(self) -> "GuiStream":
        return self

    async def __anext__(self) -> GuiMessage:
        # The producer is the only writer: a closed subscription is always
        # followed by a close sentinel, so a blocking ``get`` can never hang.
        while True:
            item = await self._state.queue.get()
            if item is _CLOSE:
                raise StopAsyncIteration
            self._state.stalled_at = None
            self._messages += 1
            if item.kind in {"resync_required", "cursor_expired", "stream_closed"}:
                self._terminal_kind = item.kind
            return item

    async def aclose(self) -> str:
        """Detach from the live buffer; the cursor can resume it later."""

        await self._service.detach(self._state.subscription_id)
        return self._state.subscription_id

    @property
    def cursor(self) -> GuiCursor:
        """The resume token for this subscription at its current boundary."""

        return GuiCursor(
            subscription_id=self._state.subscription_id,
            session_id=self._state.session_id,
            high_water=self._state.high_water,
            projection_version=self._state.projection_version,
            partial_versions=tuple(
                (key, draft.revision)
                for key, draft in sorted(self._state.drafts.items())
            ),
            service_epoch=self._state.service_epoch,
        )


class SubscriptionService:
    """Atomic snapshot plus resumable incremental delivery for one GUI client."""

    def __init__(
        self,
        *,
        store: Any | None = None,
        store_factory: Any | None = None,
        control_store: Any | None = None,
        projection: GuiProjection | None = None,
        service_epoch: str | None = None,
    ) -> None:
        """Own no storage.

        The runtime store handle (or a factory returning one per session) and
        the C03 control store are injected by the host.  Opening a second
        connection to ``runtime.sqlite`` is not offered: that database is not in
        WAL mode and its connections use ``busy_timeout = 2000``, so a
        concurrent reader can raise ``database is locked`` while a write
        transaction is open.
        """

        if store is None and store_factory is None:
            raise SubscriptionError(
                "SubscriptionService requires an injected runtime store or store_factory"
            )
        self._injected_store = store
        self._store_factory = store_factory
        self._control_store = control_store
        self._projection = projection or GuiProjection()
        self._service_epoch = service_epoch or uuid.uuid4().hex
        self._lock = asyncio.Lock()
        self._streams: dict[str, GuiStream] = {}
        self._states: dict[str, _SubscriptionState] = {}
        self._closed = False

    # ---- store plumbing -------------------------------------------------

    @property
    def service_epoch(self) -> str:
        return self._service_epoch

    @property
    def projection_version(self) -> str:
        return self._projection.projection_version

    def stream_for(self, subscription_id: str) -> GuiStream | None:
        return self._streams.get(subscription_id)

    def store(self, session_id: str) -> Any:
        if self._injected_store is not None:
            return self._injected_store
        return self._store_factory(session_id)

    def control(self) -> Any:
        """Return the injected C03 control store, read-only.

        C04 depends on C03 in the read direction only: it calls public read
        methods and never changes their semantics.
        """

        if self._control_store is None:
            raise SubscriptionError("no control store injected into this service")
        return self._control_store

    # ---- snapshot / subscribe ------------------------------------------

    async def snapshot(self, session_id: str, *, projection_version: str | None = None) -> GuiSnapshotResult:
        """Take an atomic snapshot at the session's current high-water ordinal."""

        self._ensure_open()
        if projection_version is not None and projection_version != self.projection_version:
            raise SubscriptionError(
                f"unsupported projection version {projection_version!r}"
            )
        async with self._lock:
            store = self.store(session_id)
            high_water = int(store.high_water(session_id=session_id))
            drafts, last_partial_seq = self._read_drafts(store, session_id)
            pending = self._read_pending(session_id)
            runs = self._read_runs(session_id)
            snapshot = self._projection.build(
                store,
                session_id=session_id,
                high_water=high_water,
                drafts=drafts,
                pending_interactions=pending,
                runs=runs,
                last_partial_seq=last_partial_seq,
            )
            cursor = GuiCursor(
                subscription_id=f"snapshot-{uuid.uuid4().hex}",
                session_id=session_id,
                high_water=high_water,
                projection_version=self.projection_version,
                partial_versions=tuple(
                    (draft.stream_key, draft.revision) for draft in drafts
                ),
                service_epoch=self._service_epoch,
            )
            return GuiSnapshotResult(status="ok", cursor=cursor, snapshot=snapshot)

    async def subscribe(
        self,
        session_id: str,
        *,
        cursor: GuiCursor | None = None,
        projection_version: str | None = None,
    ) -> GuiStream:
        """Register a subscription and emit its snapshot as the first message.

        A subscription that starts from a cursor must go through
        :meth:`resume`; this entry point always establishes a *new* boundary so
        the snapshot is never ambiguous.
        """

        self._ensure_open()
        if cursor is not None:
            raise SubscriptionError(
                "subscribe() establishes a new boundary; use resume(cursor) to continue one"
            )
        if projection_version is not None and projection_version != self.projection_version:
            raise SubscriptionError(
                f"unsupported projection version {projection_version!r}"
            )
        async with self._lock:
            store = self.store(session_id)
            high_water = int(store.high_water(session_id=session_id))
            drafts, last_partial_seq = self._read_drafts(store, session_id)
            pending = self._read_pending(session_id)
            runs = self._read_runs(session_id)
            # The snapshot is built *at* high_water and the first suffix read is
            # strictly after it; the boundary is carried by the ordinal, not by
            # the order in which this method happened to run.
            snapshot = self._projection.build(
                store,
                session_id=session_id,
                high_water=high_water,
                drafts=drafts,
                pending_interactions=pending,
                runs=runs,
                last_partial_seq=last_partial_seq,
            )
            state = _SubscriptionState(
                subscription_id=uuid.uuid4().hex,
                session_id=session_id,
                projection_version=self.projection_version,
                service_epoch=self._service_epoch,
                last_ordinal=high_water,
                high_water=high_water,
            )
            stream = GuiStream(state, self)
            self._states[state.subscription_id] = state
            self._streams[state.subscription_id] = stream
            await self._enqueue(
                state,
                GuiMessage(
                    kind="snapshot",
                    key=None,
                    prefix_boundary_exempt=False,
                    ordinal=high_water,
                    payload=snapshot,
                ),
            )
            for request in snapshot.pending_interactions:
                state.seen_requests.add(request.request_id)
            for run in snapshot.runs:
                state.seen_runs[str(run.get("run_id"))] = str(run.get("status"))
            for draft in snapshot.drafts:
                state.drafts[draft.stream_key] = draft
            state.task = asyncio.create_task(self._pump(state))
            return stream

    async def resume(self, cursor: GuiCursor) -> GuiResumeResult:
        """Continue a subscription, or report an explicit ``cursor_expired``."""

        self._ensure_open()
        reason = self._expiry_reason(cursor)
        if reason is not None:
            return GuiResumeResult(
                status="cursor_expired",
                cursor=None,
                error_code=reason,
                current_high_water=self._safe_high_water(cursor.session_id),
            )
        async with self._lock:
            state = self._states.get(cursor.subscription_id)
            if state is None or state.closed:
                return GuiResumeResult(
                    status="cursor_expired",
                    cursor=None,
                    error_code="subscription_not_resumable",
                    current_high_water=self._safe_high_water(cursor.session_id),
                )
            stream = self._streams[cursor.subscription_id]
            if state.subscribed:
                return GuiResumeResult(
                    status="cursor_expired",
                    cursor=None,
                    error_code="subscription_active",
                    current_high_water=state.high_water,
                )
            state.subscribed = True
            if state.resumable_high_water is None:
                state.resumable_high_water = state.high_water
            if cursor.high_water < state.resumable_high_water:
                # Older than the oldest retained boundary: the gap cannot be
                # replayed from this buffer, so expiring is the only honest
                # answer.
                return GuiResumeResult(
                    status="cursor_expired",
                    cursor=None,
                    error_code="buffer_does_not_cover",
                    current_high_water=state.high_water,
                )
            if state.task is None or state.task.done():
                state.task = asyncio.create_task(self._pump(state))
            return GuiResumeResult(
                status="resumed",
                cursor=replace(cursor, service_epoch=self._service_epoch),
                stream=stream,
                current_high_water=state.high_water,
            )

    async def detach(self, subscription_id: str) -> GuiUnsubscribeResult:
        """Stop consuming status for one subscription and keep it resumable.

        ``GuiStream.aclose()`` routes here so the cancellation of the pump task
        happens under the service lock.  This is the counterpart of
        :meth:`unsubscribe`: detaching keeps the buffer (and therefore the
        cursor) valid, closing does not, so a transport that exposes only
        ``unsubscribe`` cannot offer resumption at all.
        """

        async with self._lock:
            state = self._states.get(subscription_id)
            if state is None:
                return GuiUnsubscribeResult(
                    status="unknown_subscription",
                    subscription_id=subscription_id,
                    error_code="subscription_not_found",
                )
            await self._detach_locked(state)
            return GuiUnsubscribeResult(status="detached", subscription_id=subscription_id)

    async def unsubscribe(self, subscription_id: str) -> GuiUnsubscribeResult:
        """Close one subscription.  The run it observes keeps executing."""

        async with self._lock:
            state = self._states.get(subscription_id)
            if state is None:
                return GuiUnsubscribeResult(
                    status="unknown_subscription",
                    subscription_id=subscription_id,
                    error_code="subscription_not_found",
                )
            await self._retire(state)
            return GuiUnsubscribeResult(
                status="unsubscribed", subscription_id=subscription_id
            )

    async def aclose(self) -> None:
        self._closed = True
        async with self._lock:
            for state in list(self._states.values()):
                await self._retire(state)

    # ---- internals ------------------------------------------------------

    async def _detach_locked(self, state: _SubscriptionState) -> None:
        """Stop delivering to the caller while the buffer stays resumable.

        The pull loop keeps running so facts that arrive during the gap are
        buffered rather than lost; ``resume`` re-attaches the same cursor.
        """

        if not state.subscribed:
            return
        state.subscribed = False
        if state.resumable_high_water is None:
            state.resumable_high_water = state.high_water

    async def _cancel_task(self, state: _SubscriptionState) -> None:
        task = state.task
        state.task = None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task

    def _ensure_open(self) -> None:
        if self._closed:
            raise SubscriptionError("subscription service is closed")
        if self._closed:
            raise SubscriptionError("subscription service is closed")

    def _expiry_reason(self, cursor: GuiCursor) -> str | None:
        if cursor.service_epoch != self._service_epoch:
            return "service_instance_replaced"
        if cursor.projection_version != self.projection_version:
            return "projection_version_mismatch"
        if cursor.subscription_id not in self._streams:
            return "subscription_unknown"
        state = self._states[cursor.subscription_id]
        if state.session_id != cursor.session_id:
            return "session_mismatch"
        if state.closed and not state.overflowed:
            return "subscription_closed"
        if state.expired:
            return "source_expired"
        if cursor.high_water > state.high_water:
            return "cursor_ahead_of_source"
        return None

    def _safe_high_water(self, session_id: str) -> int:
        try:
            return int(self.store(session_id).high_water(session_id=session_id))
        except Exception:
            return 0

    def _read_drafts(self, store: Any, session_id: str) -> tuple[tuple[GuiDraft, ...], int]:
        partials = store.read_runtime_stream_partials(session_id=session_id)
        drafts: dict[str, GuiDraft] = {}
        for partial in partials:
            key = _storage_key(partial)
            revision = int(getattr(partial, "last_partial_seq", 0) or 0)
            previous = drafts.get(key)
            if previous is not None and previous.revision >= revision:
                continue
            drafts[key] = GuiDraft(
                stream_key=key,
                revision=revision,
                partial_seq=revision,
                stream_kind=str(getattr(partial, "stream_kind", "") or ""),
                tool_call_id=getattr(partial, "tool_call_id", None),
                size=int(getattr(partial, "size_bytes", 0) or 0),
                fragment_count=int(getattr(partial, "fragment_count", 0) or 0),
                payload=json_value(getattr(partial, "payload", None)),
            )
        values = tuple(sorted(drafts.values(), key=lambda item: item.stream_key))
        return values, max((item.revision for item in values), default=0)

    def _read_pending(self, session_id: str) -> tuple[GuiPendingInteraction, ...]:
        control = self._control_store
        if control is None:
            return ()
        pending: list[GuiPendingInteraction] = []
        for row in control.runs_for_session(session_id):
            for item in control.pending_for_run(str(row["run_id"])):
                if str(item["status"]) != "pending":
                    continue
                pending.append(
                    GuiPendingInteraction(
                        request_id=str(item["request_id"]),
                        run_id=str(item["run_id"]),
                        status=str(item["status"]),
                        tool_name=item["tool_name"],
                        updated_at=item["updated_at"],
                        expires_at=item["expires_at"],
                    )
                )
        pending.sort(key=lambda item: item.request_id)
        return tuple(pending)

    def _read_runs(self, session_id: str) -> tuple[dict[str, Any], ...]:
        control = self._control_store
        if control is None:
            return ()
        runs: list[dict[str, Any]] = []
        for row in control.runs_for_session(session_id):
            keys = set(row.keys())
            runs.append(
                {
                    key: (row[key] if key in keys else None)
                    for key in ("run_id", "session_id", "status", "error_code", "created_at", "updated_at")
                }
            )
            # Run state lives in the control plane and carries no canonical
            # ordinal, so it is never part of the ``ordinal <= high_water``
            # prefix.  The flag has to ride on the DTO or a consumer cannot tell
            # a bounded prefix fact from a live control-plane fact.
            runs[-1]["prefix_boundary_exempt"] = True
        runs.sort(key=lambda item: (str(item["created_at"]), str(item["run_id"])))
        return tuple(runs)

    def _pending(self, state: _SubscriptionState) -> int:
        """Messages the subscriber has not consumed yet.

        The bound counts the whole outstanding backlog (queue plus buffer), not
        just one of them: a subscriber that never reads makes the queue grow,
        and a subscriber that reads slowly makes the buffer grow.
        """

        return state.queue.qsize() + len(state.buffer)

    def _at_bound(self, state: _SubscriptionState) -> bool:
        """Whether the outstanding backlog has reached the declared bound."""

        if self._pending(state) >= SUBSCRIPTION_BUFFER_LIMIT:
            if state.stalled_at is None:
                state.stalled_at = _monotonic()
            return True
        state.stalled_at = None
        return False

    def _stall_expired(self, state: _SubscriptionState) -> bool:
        """Whether a saturated backlog has failed to shrink for the grace period."""

        if not self._at_bound(state) or state.stalled_at is None:
            return False
        return _monotonic() - state.stalled_at >= SUBSCRIPTION_STALL_GRACE_SECONDS

    def _trim_front(self, state: _SubscriptionState, count: int) -> None:
        """Drop the stalest *disposable* entries to keep the backlog bounded.

        A terminal or an interaction is a state transition the consumer cannot
        recover by reading the prefix again -- losing one silently would leave a
        GUI waiting forever on a run that already finished.  They are therefore
        never displaced; only ordinary events are.  The stalest such entry goes
        first, and the bound is a soft target when protected entries are all that
        remains.
        """

        for _ in range(max(0, count)):
            for index, queued in enumerate(state.queue._queue):
                if queued is not _CLOSE and _is_disposable(queued):
                    del state.queue._queue[index]
                    state.queue._unfinished_tasks = max(
                        0, state.queue._unfinished_tasks - 1
                    )
                    break
            else:
                return

    async def _enqueue(self, state: _SubscriptionState, message: GuiMessage) -> None:
        if state.closed:
            return
        if message.kind == "draft" and message.key is not None:
            await self._enqueue_draft(state, message)
            return
        if self._at_bound(state):
            # The bound is saturated, so this message displaces the stalest
            # disposable queued entry instead of growing the backlog without
            # limit.  Protected entries may make the trim a no-op.
            self._trim_front(state, 1)
        state.buffer.append(message)

    async def _enqueue_draft(self, state: _SubscriptionState, message: GuiMessage) -> None:
        draft = message.payload
        assert isinstance(draft, GuiDraft)
        previous = state.drafts.get(draft.stream_key)
        if previous is not None and draft.revision <= previous.revision:
            return
        state.drafts[draft.stream_key] = draft
        if not self._at_bound(state):
            state.buffer.append(message)
            return
        # At the bound a newer revision of the *same* stream replaces the one
        # already waiting instead of consuming another slot, so a chatty draft
        # stream alone can never close a subscription.
        for index in range(len(state.buffer) - 1, -1, -1):
            existing = state.buffer[index]
            if existing.kind == "draft" and existing.key == draft.stream_key:
                state.buffer[index] = replace(message, ordinal=existing.ordinal)
                return
        self._trim_front(state, 1)
        state.buffer.append(message)

    async def _overflow(self, state: _SubscriptionState) -> None:
        if state.overflowed:
            return
        state.overflowed = True
        state.closed = True
        # Buffer contents stay in front of the terminal notice so a client that
        # drains the stream still sees everything that was queued.  Nothing is
        # removed from the buffer here: silently dropping a terminal, an
        # interaction or an error is exactly the failure this bound exists to
        # make loud.
        state.buffer.append(
            GuiMessage(
                kind="resync_required",
                key=None,
                prefix_boundary_exempt=False,
                payload={
                    "reason": "subscription_buffer_overflow",
                    "buffer_limit": SUBSCRIPTION_BUFFER_LIMIT,
                    "session_id": state.session_id,
                    "high_water": state.high_water,
                },
            )
        )
        await self._drain(state)
        self._finish(state)

    def _finish(self, state: _SubscriptionState) -> None:
        state.queue.put_nowait(_CLOSE)

    async def _retire(self, state: _SubscriptionState) -> None:
        state.closed = True
        await self._cancel_task(state)
        self._finish(state)

    # ---- the pull loop --------------------------------------------------

    async def _pump(self, state: _SubscriptionState) -> None:
        try:
            await self._drain(state)
            while not state.closed:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                await self._poll_events(state)
                if state.closed:
                    break
                await self._poll_drafts(state)
                if state.closed:
                    break
                await self._poll_control(state)
                if (
                    not state.closed
                    and self._at_bound(state)
                    and self._stall_expired(state)
                ):
                    # The backlog has stayed saturated for the whole grace
                    # period.  Everything already queued is still delivered,
                    # followed by the resync notice; nothing is dropped.
                    await self._overflow(state)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - surfaced as a closed stream
            state.expired = True
            state.closed = True
            with suppress(Exception):
                state.queue.put_nowait(
                    GuiMessage(
                        kind="cursor_expired",
                        key=None,
                        prefix_boundary_exempt=False,
                        payload={
                            "reason": "source_read_failed",
                            "error": f"{type(error).__name__}: {error}",
                            "session_id": state.session_id,
                        },
                    )
                )
        finally:
            self._finish(state)

    async def _drain(self, state: _SubscriptionState) -> None:
        while state.buffer and not state.overflowed:
            state.queue.put_nowait(state.buffer.popleft())
        if state.overflowed:
            # The overflow notice is the last thing this subscription emits.
            while state.buffer:
                state.queue.put_nowait(state.buffer.popleft())
        elif state.queue.qsize() > SUBSCRIPTION_BUFFER_LIMIT:
            # A subscriber that is not reading lets the queue grow past the
            # bound.  The newest entries are worth keeping, so the stalest
            # *disposable* ones are dropped -- terminals and interactions are
            # never among them.  The subscriber is told to re-snapshot by the
            # stall timer, which fires a ``resync_required``.
            self._trim_front(state, state.queue.qsize() - SUBSCRIPTION_BUFFER_LIMIT)
        if state.queue.qsize():
            # Let a subscriber that is ready to read actually read before the
            # next tick judges whether the backlog is shrinking.
            await asyncio.sleep(0)

    async def _poll_events(self, state: _SubscriptionState) -> None:
        store = self.store(state.session_id)
        records = store.read_event_records(
            session_id=state.session_id, after_ordinal=state.last_ordinal
        )
        for ordinal, event in records:
            if state.overflowed:
                # The subscription is closed; stop advancing its cursor so the
                # handover to a fresh snapshot is unambiguous.
                return
            # ``last_ordinal`` is the de-duplication watermark and must advance
            # past every record read, partial or not, or the same record would
            # be re-read forever.  ``high_water`` is the delivered boundary and
            # must not: a partial record is not part of the visible prefix, so
            # letting it move the boundary would break the draft contract that
            # streaming observations never change ``high_water``.
            state.last_ordinal = max(state.last_ordinal, int(ordinal))
            if event.partial:
                # Partial canonical events are not part of the visible prefix;
                # the mutable draft table is their delivery path.
                continue
            state.high_water = max(state.high_water, int(ordinal))
            await self._enqueue(
                state,
                GuiMessage(
                    kind="event",
                    key=event.id,
                    prefix_boundary_exempt=False,
                    ordinal=int(ordinal),
                    payload=event.to_dict(),
                ),
            )
            # Hand each record to the subscriber queue before reading the next
            # one, so the bound measures the backlog the subscriber is actually
            # behind on rather than the size of one batch.
            await self._drain(state)

    async def _poll_drafts(self, state: _SubscriptionState) -> None:
        # A draft advances no ordinal and never moves the high-water mark, so a
        # pull-based subscriber can only observe it by reading the mutable table
        # on every tick.
        store = self.store(state.session_id)
        drafts, _last = self._read_drafts(store, state.session_id)
        for draft in drafts:
            previous = state.drafts.get(draft.stream_key)
            if previous is not None and draft.revision <= previous.revision:
                continue
            await self._enqueue(
                state,
                GuiMessage(
                    kind="draft",
                    key=draft.stream_key,
                    prefix_boundary_exempt=True,
                    payload=draft,
                ),
            )
        await self._drain(state)

    async def _poll_control(self, state: _SubscriptionState) -> None:
        for run in self._read_runs(state.session_id):
            run_id = str(run["run_id"])
            status = str(run["status"])
            if state.seen_runs.get(run_id) == status:
                continue
            state.seen_runs[run_id] = status
            await self._enqueue(
                state,
                GuiMessage(
                    kind="run",
                    key=run_id,
                    prefix_boundary_exempt=True,
                    run_id=run_id,
                    payload=run,
                ),
            )
        for item in self._read_pending(state.session_id):
            if item.request_id in state.seen_requests:
                continue
            state.seen_requests.add(item.request_id)
            await self._enqueue(
                state,
                GuiMessage(
                    kind="interaction",
                    key=item.request_id,
                    prefix_boundary_exempt=True,
                    request_id=item.request_id,
                    run_id=item.run_id,
                    payload=item.to_dict(),
                ),
            )
        await self._drain(state)


__all__ = [
    "GUI_PROJECTION_VERSION",
    "POLL_INTERVAL_SECONDS",
    "SUBSCRIPTION_STALL_GRACE_SECONDS",
    "SUBSCRIPTION_BUFFER_LIMIT",
    "GuiCursor",
    "GuiMessage",
    "GuiMessageRef",
    "GuiResumeResult",
    "GuiSnapshotResult",
    "GuiStream",
    "GuiUnsubscribeResult",
    "SubscriptionError",
    "SubscriptionService",
    "derive_stream_key",
]
