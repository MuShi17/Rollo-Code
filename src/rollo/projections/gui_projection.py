"""Bounded, read-only session view for non-terminal consumers.

This projection is deliberately *boundary-explicit*.  Everything bounded by a
canonical ordinal (messages, runs, terminals, errors) is derived from the
immutable prefix ``ordinal <= high_water`` of one session.  Facts that carry no
ordinal at all -- streaming drafts from ``runtime_stream_partials`` and pending
interactions from the C03 control store -- are assembled from the control plane
and are marked with ``prefix_boundary_exempt`` so a consumer can never mistake
them for part of that prefix.

The projection reads and never writes: it never appends a canonical event and
never calls a model or a tool.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from ..runtime_event import RuntimeEvent
from .base import EventRecord, iter_event_records, source_digest
from .session_projection import SessionProjection

#: Own projection namespace.  It is intentionally *not* the shared
#: ``projection-v1``: this DTO shape can change without the replay projections
#: changing, so the compatibility predicate must be able to move on its own.
GUI_PROJECTION_VERSION = "gui-projection-v1"

#: Maximum number of message references returned in one snapshot page.
SNAPSHOT_MESSAGE_PAGE_LIMIT = 200

#: Maximum number of characters kept in a message ``summary``.
SUMMARY_CHAR_LIMIT = 160

#: Maximum number of body characters returned by one ``read_body`` call.
BODY_PAGE_SIZE = 4096

#: The only field names that are bounded by ``high_water``.
PREFIX_FIELDS = ("messages", "terminals", "errors", "source_digest")

#: Field names that carry no ordinal boundary and are read at call time.
#: ``runs`` belongs here: run state lives in the C03 control store, which has no
#: ordinal at all, so it cannot be derived from ``ordinal <= high_water``.  It
#: used to be listed as a prefix field, which made one snapshot claim both
#: classifications at once -- two snapshots at the same ``high_water`` with the
#: same ``source_digest`` could report different run states.
NON_PREFIX_FIELDS = ("runs", "drafts", "pending_interactions")


def _body_size(value: Any) -> int:
    """Return the UTF-8 byte size of a canonical-JSON encoded value."""

    return len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))


@dataclass(frozen=True, slots=True)
class GuiMessageRef:
    """A message identity plus a bounded summary; the body stays out of band."""

    message_id: str
    ordinal: int
    role: str
    kind: str
    summary: str
    size: int
    body_ref: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "ordinal": self.ordinal,
            "role": self.role,
            "kind": self.kind,
            "summary": self.summary,
            "size": self.size,
            "body_ref": self.body_ref,
        }


@dataclass(frozen=True, slots=True)
class GuiMessagePage:
    refs: tuple[GuiMessageRef, ...]
    offset: int
    limit: int
    total: int
    has_more: bool
    next_page_token: str | None


@dataclass(frozen=True, slots=True)
class GuiBodyPage:
    message_id: str
    high_water: int
    offset: int
    page_size: int
    size: int
    text: str
    has_more: bool
    next_page_token: str | None
    source_digest: str


@dataclass(frozen=True, slots=True)
class GuiDraft:
    """A mutable streaming observation.  ``revision`` is the store's
    ``last_partial_seq``; the table has no separate revision column."""

    stream_key: str
    revision: int
    partial_seq: int
    stream_kind: str
    tool_call_id: str | None
    size: int
    fragment_count: int
    payload: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "stream_key": self.stream_key,
            "revision": self.revision,
            "partial_seq": self.partial_seq,
            "stream_kind": self.stream_kind,
            "tool_call_id": self.tool_call_id,
            "size": self.size,
            "fragment_count": self.fragment_count,
            "payload": self.payload,
            "prefix_boundary_exempt": True,
        }


@dataclass(frozen=True, slots=True)
class GuiPendingInteraction:
    request_id: str
    run_id: str
    status: str
    tool_name: str | None
    updated_at: str | None
    expires_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "run_id": self.run_id,
            "status": self.status,
            "tool_name": self.tool_name,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "prefix_boundary_exempt": True,
        }


@dataclass(frozen=True, slots=True)
class GuiSnapshot:
    """A bounded view of one session at one explicit boundary.

    ``messages``/``runs``/``terminals``/``errors``/``source_digest`` come from
    the immutable prefix ``ordinal <= high_water`` of ``session_id``.
    ``drafts`` and ``pending_interactions`` carry no ordinal and are read at
    call time; they are *not* part of that prefix.
    """

    session_id: str
    high_water: int
    projection_version: str
    source_digest: str
    message_page: GuiMessagePage
    runs: tuple[dict[str, Any], ...]
    terminals: tuple[dict[str, Any], ...]
    errors: tuple[dict[str, Any], ...]
    drafts: tuple[GuiDraft, ...]
    pending_interactions: tuple[GuiPendingInteraction, ...]
    last_partial_seq: int
    read_at_ms: int
    prefix_boundary_exempt: bool = False

    @property
    def messages(self) -> tuple[GuiMessageRef, ...]:
        return self.message_page.refs

    @property
    def has_more_messages(self) -> bool:
        return self.message_page.has_more

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "high_water": self.high_water,
            "projection_version": self.projection_version,
            "source_digest": self.source_digest,
            "prefix_boundary_exempt": self.prefix_boundary_exempt,
            "prefix_fields": list(PREFIX_FIELDS),
            "non_prefix_fields": list(NON_PREFIX_FIELDS),
            "messages": [item.to_dict() for item in self.message_page.refs],
            "message_page": {
                "offset": self.message_page.offset,
                "limit": self.message_page.limit,
                "total": self.message_page.total,
                "has_more": self.message_page.has_more,
                "next_page_token": self.message_page.next_page_token,
            },
            "runs": [dict(item) for item in self.runs],
            "terminals": [dict(item) for item in self.terminals],
            "errors": [dict(item) for item in self.errors],
            "drafts": [item.to_dict() for item in self.drafts],
            "pending_interactions": [item.to_dict() for item in self.pending_interactions],
            "last_partial_seq": self.last_partial_seq,
            "read_at_ms": self.read_at_ms,
        }


def _summarise(text: str) -> str:
    if len(text) <= SUMMARY_CHAR_LIMIT:
        return text
    return text[:SUMMARY_CHAR_LIMIT]


def _text_of(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def event_body(event: RuntimeEvent) -> str:
    """Return the full in-band body of one canonical message event.

    This is exactly the value whose canonical-JSON UTF-8 byte length is
    reported as ``GuiMessageRef.size``.
    """

    content = event.content or {}
    if isinstance(content, Mapping):
        kind = content.get("kind")
        if kind == "text":
            return _text_of(content.get("text", ""))
        if kind == "context":
            return _text_of(content.get("text", ""))
        if kind == "function_call":
            return _text_of(content.get("args"))
        if kind == "function_response":
            return _text_of(content.get("result"))
    return _text_of(dict(content))


def _role_and_kind(record: EventRecord) -> tuple[str, str]:
    event = record.event
    content = event.content or {}
    kind = str(event.kind or "")
    if kind == "text":
        role = {"model": "assistant", "user": "user", "tool": "tool"}.get(
            event.role, event.role
        )
        return role, "text"
    if kind == "context":
        return "context", "context"
    if kind == "function_call":
        return "assistant", "function_call"
    if kind == "function_response":
        return "tool", "function_response"
    if kind == "error":
        return "system", "error"
    return str(event.role), kind or "unknown"


def _is_message(record: EventRecord) -> bool:
    event = record.event
    if event.partial or event.model_visibility == "hidden":
        return False
    kind = event.kind
    if kind == "text":
        return bool((event.content or {}).get("text"))
    if kind == "context":
        return bool((event.content or {}).get("text"))
    return kind in {"function_call", "function_response", "error"}


class GuiProjection:
    """Fold one session's immutable prefix into a bounded GUI view."""

    projection_version = GUI_PROJECTION_VERSION

    def __init__(self, *, message_page_limit: int | None = None) -> None:
        if message_page_limit is not None:
            self.message_page_limit = int(message_page_limit)
        else:
            self.message_page_limit = SNAPSHOT_MESSAGE_PAGE_LIMIT

    # ---- snapshot ------------------------------------------------------

    def build(
        self,
        store: Any,
        *,
        session_id: str,
        high_water: int,
        page_offset: int = 0,
        drafts: tuple[GuiDraft, ...] | list[GuiDraft] = (),
        pending_interactions: tuple[GuiPendingInteraction, ...]
        | list[GuiPendingInteraction] = (),
        runs: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]] = (),
        last_partial_seq: int = 0,
    ) -> GuiSnapshot:
        offset = max(0, int(page_offset))
        high_water = int(high_water)
        records = self._prefix_records(store, session_id=session_id, high_water=high_water)
        digest = source_digest(records)
        refs = self._message_refs(records, session_id, high_water, digest)
        limit = self.message_page_limit
        page = tuple(refs[offset : offset + limit])
        has_more = offset + len(page) < len(refs)
        token = (
            _encode_page_token(session_id, high_water, digest, offset + len(page), limit)
            if has_more
            else None
        )
        base = SessionProjection().project(
            records, session_id=session_id, high_water=high_water
        )
        return GuiSnapshot(
            session_id=session_id,
            high_water=high_water,
            projection_version=self.projection_version,
            source_digest=digest,
            message_page=GuiMessagePage(
                refs=page,
                offset=offset,
                limit=limit,
                total=len(refs),
                has_more=has_more,
                next_page_token=token,
            ),
            runs=tuple(dict(item) for item in (runs or base.runs)),
            terminals=base.terminals,
            errors=base.errors,
            drafts=tuple(drafts),
            pending_interactions=tuple(pending_interactions),
            last_partial_seq=int(last_partial_seq),
            read_at_ms=_now_ms(),
        )

    build_page = build

    # ---- pagination ----------------------------------------------------

    def read_page(
        self,
        store: Any,
        *,
        session_id: str,
        high_water: int,
        page_offset: int = 0,
    ) -> GuiSnapshot:
        """Read one page pinned to an explicit boundary.

        Every page must be requested with the *same* ``high_water`` and produce
        the same ``source_digest`` as the page that issued the token.  Offset
        pagination against a moving boundary can repeat or skip entries, so the
        boundary is carried inside ``GuiMessagePage.next_page_token`` rather
        than being an ambient default.
        """

        return self.build(
            store, session_id=session_id, high_water=high_water, page_offset=page_offset
        )

    def read_page_by_token(self, store: Any, page_token: str) -> GuiSnapshot:
        """Continue a page sequence from the boundary the token carries."""

        session_id, high_water, digest, offset, _limit = _decode_page_token(page_token)
        records = self._prefix_records(
            store, session_id=session_id, high_water=high_water
        )
        if source_digest(records) != digest:
            raise GuiPageTokenError(
                f"prefix <= {high_water} of {session_id!r} no longer matches the page token"
            )
        return self.build(
            store, session_id=session_id, high_water=high_water, page_offset=offset
        )

    def read_body(self, store: Any, page_token: str) -> GuiBodyPage:
        """Read one bounded chunk of one message body by page token.

        The token pins the target to the immutable prefix it was produced at:
        the prefix is re-read and its digest must match the token verbatim.
        """

        session_id, high_water, digest, event_id, body_offset, page_size = (
            _decode_body_token(page_token)
        )
        records = self._prefix_records(
            store, session_id=session_id, high_water=high_water
        )
        actual = source_digest(records)
        if actual != digest:
            raise GuiPageTokenError(
                f"prefix <= {high_water} of {session_id!r} no longer matches the body token"
            )
        event = next(
            (record.event for record in records if record.event.id == event_id), None
        )
        if event is None:
            raise GuiPageTokenError(
                f"body target {event_id!r} is not in prefix <= {high_water} of {session_id!r}"
            )
        body = event_body(event)
        chunk = body[body_offset : body_offset + page_size]
        next_offset = body_offset + len(chunk)
        has_more = next_offset < len(body)
        return GuiBodyPage(
            message_id=event_id,
            high_water=high_water,
            offset=body_offset,
            page_size=page_size,
            size=_body_size(body),
            text=chunk,
            has_more=has_more,
            next_page_token=(
                _encode_body_token(
                    session_id, high_water, actual, event_id, next_offset, page_size
                )
                if has_more
                else None
            ),
            source_digest=actual,
        )

    # ---- internals -----------------------------------------------------

    def _prefix_records(
        self, store: Any, *, session_id: str, high_water: int
    ) -> list[EventRecord]:
        return [
            record
            for record in iter_event_records(store, high_water=high_water)
            if record.event.session_id == session_id
            and record.ordinal <= high_water
        ]

    def _message_refs(
        self,
        records: list[EventRecord],
        session_id: str,
        high_water: int,
        digest: str,
    ) -> list[GuiMessageRef]:
        refs: list[GuiMessageRef] = []
        for record in records:
            if not _is_message(record):
                continue
            role, kind = _role_and_kind(record)
            body = event_body(record.event)
            refs.append(
                GuiMessageRef(
                    message_id=record.event.id,
                    ordinal=record.ordinal,
                    role=role,
                    kind=kind,
                    summary=_summarise(body),
                    size=_body_size(body),
                    body_ref=_encode_body_token(
                        session_id, high_water, digest, record.event.id, 0, BODY_PAGE_SIZE
                    ),
                )
            )
        return refs


# ---- page tokens -------------------------------------------------------
#
# A page token always carries the snapshot boundary it was produced at: the
# session, the high-water ordinal and the digest of that immutable prefix.
# ``read_body`` re-reads the same prefix and refuses a target that is not
# inside it, so a stale token fails loudly instead of silently reading a newer
# value.

_PAGE_TOKEN_PREFIX = "gui-page-v1"
_BODY_TOKEN_PREFIX = "gui-body-v1"


class GuiPageTokenError(ValueError):
    """A page token is malformed or no longer valid at its own boundary."""


def _encode_page_token(
    session_id: str, high_water: int, digest: str, offset: int, limit: int
) -> str:
    return "\t".join(
        (
            _PAGE_TOKEN_PREFIX,
            session_id,
            str(int(high_water)),
            digest,
            str(int(offset)),
            str(int(limit)),
        )
    )


def _decode_page_token(token: str) -> tuple[str, int, str, int, int]:
    parts = str(token).split("\t")
    if len(parts) != 6 or parts[0] != _PAGE_TOKEN_PREFIX:
        raise GuiPageTokenError("malformed page token")
    try:
        return parts[1], int(parts[2]), parts[3], int(parts[4]), int(parts[5])
    except ValueError as error:
        raise GuiPageTokenError("malformed page token") from error


def _encode_body_token(
    session_id: str,
    high_water: int,
    digest: str,
    event_id: str,
    offset: int,
    page_size: int,
) -> str:
    return "\t".join(
        (
            _BODY_TOKEN_PREFIX,
            session_id,
            str(int(high_water)),
            digest,
            event_id,
            str(int(offset)),
            str(int(page_size)),
        )
    )


def _decode_body_token(token: str) -> tuple[str, int, str, str, int, int]:
    parts = str(token).split("\t")
    if len(parts) != 7 or parts[0] != _BODY_TOKEN_PREFIX:
        raise GuiPageTokenError("malformed body token")
    try:
        return parts[1], int(parts[2]), parts[3], parts[4], int(parts[5]), int(parts[6])
    except ValueError as error:
        raise GuiPageTokenError("malformed body token") from error


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


__all__ = [
    "BODY_PAGE_SIZE",
    "GUI_PROJECTION_VERSION",
    "NON_PREFIX_FIELDS",
    "PREFIX_FIELDS",
    "SNAPSHOT_MESSAGE_PAGE_LIMIT",
    "SUMMARY_CHAR_LIMIT",
    "GuiBodyPage",
    "GuiDraft",
    "GuiMessagePage",
    "GuiMessageRef",
    "GuiPageTokenError",
    "GuiPendingInteraction",
    "GuiProjection",
    "GuiSnapshot",
    "event_body",
]
