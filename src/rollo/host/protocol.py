"""NDJSON framing and error mapping for the host wire (IPC protocol v1).

The wire is one JSON object per line, UTF-8, LF terminated.  A frame carries a
JSON-RPC ``id`` for response correlation and, for commands that change state, a
separate ``command_id`` used for business idempotency: the two are deliberately
independent, so a retried command keeps its identity while a retried *call*
picks a new one.

Only :meth:`HostProtocol.encode` writes to stdout; nothing else may.  A client
must tolerate half frames, merged frames and a UTF-8 sequence split across two
reads, so decoding is incremental and never assumes a read boundary is a frame
boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

#: Protocol version this host speaks.  A client asking for anything else gets an
#: explicit error: the version is negotiated, never guessed.
PROTOCOL_VERSION = 1

#: Default single-frame ceiling.  A larger frame closes the connection instead
#: of guessing at a framing recovery.
MAX_FRAME_BYTES = 1024 * 1024

#: Text-stream delta ceiling inside one event payload.
TEXT_DELTA_BYTES = 32 * 1024

#: Upper bound for a page of history or messages.
MAX_PAGE_ITEMS = 100

#: Total response body target for a snapshot/history page.
PAGE_BODY_TARGET_BYTES = 256 * 1024

#: Single display preview ceiling; anything larger goes through ``content.read``.
PREVIEW_BYTES = 16 * 1024

#: Body paging defaults and ceiling.
BODY_PAGE_DEFAULT_BYTES = 64 * 1024
BODY_PAGE_MAX_BYTES = 256 * 1024

# JSON-RPC 2.0 standard codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

#: Business error codes carried in ``error.data.code``, each mapping to an
#: action a GUI can offer.  These are stable identifiers, not messages.
BUSINESS_CODES = frozenset(
    {
        "session_busy",
        "scope_mismatch",
        "command_conflict",
        "request_expired",
        "run_terminal",
        "cursor_expired",
        "frame_too_large",
        "runtime_unavailable",
        "not_initialized",
        "unsupported_version",
        "not_implemented",
    }
)


class FrameTooLarge(Exception):
    """A frame exceeded :data:`MAX_FRAME_BYTES`; the connection must close."""


class ProtocolError(Exception):
    """An error carrying a JSON-RPC code and an optional business code."""

    def __init__(
        self,
        message: str,
        *,
        code: int = INVALID_REQUEST,
        business: str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.business = business
        self.data = dict(data or {})
        if business is not None and business not in BUSINESS_CODES:
            raise ValueError(f"undeclared business error code: {business!r}")

    def to_error(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.business is not None:
            payload["code"] = self.business
        payload.update(self.data)
        error: dict[str, Any] = {"code": self.code, "message": str(self)}
        if payload:
            error["data"] = payload
        return error


@dataclass(frozen=True, slots=True)
class Frame:
    """One decoded request or notification."""

    id: Any
    method: str
    params: dict[str, Any]
    is_notification: bool


class HostProtocol:
    """Incremental NDJSON decoder and encoder.

    The decoder is fed arbitrary byte chunks; only a newline completes a frame.
    ``stdout`` is the single protocol outlet -- diagnostics belong on stderr.
    """

    def __init__(self, *, max_frame_bytes: int = MAX_FRAME_BYTES) -> None:
        self.max_frame_bytes = int(max_frame_bytes)
        self._buffer = bytearray()
        self.frames_decoded = 0
        self.frames_rejected = 0

    def feed(self, chunk: bytes) -> list[Frame]:
        """Decode every complete frame in ``chunk``; keep the remainder buffered."""

        self._buffer.extend(chunk)
        frames: list[Frame] = []
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                if len(self._buffer) > self.max_frame_bytes:
                    self.frames_rejected += 1
                    raise FrameTooLarge(
                        f"frame exceeded {self.max_frame_bytes} bytes without a newline"
                    )
                return frames
            raw = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            if not raw.strip():
                # A blank line is not a frame; skipping it keeps a stray newline
                # from being reported as malformed JSON.
                continue
            if len(raw) > self.max_frame_bytes:
                self.frames_rejected += 1
                raise FrameTooLarge(f"frame of {len(raw)} bytes exceeds the limit")
            frames.append(self._decode(raw))

    def _decode(self, raw: bytes) -> Frame:
        try:
            value = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError as error:
            self.frames_rejected += 1
            raise ProtocolError(f"frame is not valid UTF-8: {error}", code=PARSE_ERROR) from error
        except json.JSONDecodeError as error:
            self.frames_rejected += 1
            raise ProtocolError(f"frame is not valid JSON: {error}", code=PARSE_ERROR) from error

        if not isinstance(value, dict):
            self.frames_rejected += 1
            raise ProtocolError("frame must be a JSON object", code=INVALID_REQUEST)
        if "method" not in value or not isinstance(value["method"], str):
            self.frames_rejected += 1
            raise ProtocolError("frame has no method name", code=INVALID_REQUEST)

        params = value.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            self.frames_rejected += 1
            raise ProtocolError("params must be an object", code=INVALID_PARAMS)

        return Frame(
            id=value.get("id"),
            method=value["method"],
            params=params,
            is_notification="id" not in value,
        )

    @staticmethod
    def encode(payload: Mapping[str, Any]) -> bytes:
        """Encode one frame.  This is the only writer to the protocol stream."""

        return (json.dumps(payload, ensure_ascii=False, sort_keys=False) + "\n").encode("utf-8")
