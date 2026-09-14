"""Stdio host: exposes the Application read surface as IPC protocol v1.

The host is a transport adapter and nothing more.  State-changing commands are
forwarded to :meth:`Application.dispatch` unchanged, so the host owns no policy,
no permissions and no canonical facts; observation goes through the C04
subscription service, which already provides the atomic snapshot and the
resumable incremental stream.  If a behaviour is not in those two places, the
host must not invent it.

Scope of this build: the *observation* slice.  A GUI can attach to a session,
take an atomic snapshot and follow it live.  ``run.start`` / ``run.cancel`` /
``interaction.respond`` are declared on the wire but answer with
``not_implemented`` -- they are control paths with their own acceptance rules
and are not claimed here.

Only :meth:`HostProtocol.encode` may write to stdout.  Everything this process
prints for humans goes to stderr, and :class:`_ProtocolOutlet` enforces that by
pointing the process-wide ``sys.stdout`` at stderr for the host's lifetime.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..application import Application, ApplicationResponse
from ..project_context import ProjectContext
from ..projections.subscriptions import (
    GuiCursor,
    GuiStream,
    SubscriptionError,
    SubscriptionService,
)
from ..runtime_store import SQLiteRuntimeStore
from ..session import runtime_store_path
from .protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    PROTOCOL_VERSION,
    Frame,
    FrameTooLarge,
    HostProtocol,
    ProtocolError,
)

#: Methods this host answers.  Anything else is a stable ``METHOD_NOT_FOUND``.
OBSERVATION_METHODS = (
    "host.initialize",
    "session.list",
    "session.snapshot",
    "events.subscribe",
    "events.detach",
    "events.unsubscribe",
    "host.shutdown",
)

#: Declared by the frozen method table but not implemented in this slice.  They
#: answer with an explicit business error instead of a generic failure, so a
#: client can tell "not built yet" from "wrong call".
CONTROL_METHODS = ("run.start", "run.cancel", "interaction.respond", "content.read")


class _ProtocolOutlet:
    """Route process-wide stdout to stderr so only the wire owns stdout.

    A stray ``print`` anywhere in the process would otherwise interleave with
    frames and corrupt the protocol.  Redirecting the Python-level ``sys.stdout``
    covers that; the encoded frames are written to the *saved* stream.
    """

    def __init__(self, wire_stdout: Any, diagnostics: Any) -> None:
        self._wire = wire_stdout
        self._diagnostics = diagnostics
        self._previous: Any = None

    def __enter__(self) -> "_ProtocolOutlet":
        self._previous = sys.stdout
        sys.stdout = self._diagnostics
        return self

    def __exit__(self, *exc: object) -> None:
        sys.stdout = self._previous

    def write(self, data: bytes) -> None:
        self._wire.write(data)
        self._wire.flush()


@dataclass(slots=True)
class _Subscription:
    subscription_id: str
    session_id: str
    stream: GuiStream
    task: asyncio.Task[None] | None = None


class HostServer:
    """One stdio host serving one workspace."""

    def __init__(
        self,
        context: ProjectContext,
        *,
        application: Application | None = None,
        protocol: HostProtocol | None = None,
        stdin: Any = None,
        stdout: Any = None,
        stderr: Any = None,
        host_epoch: str | None = None,
        auto_shutdown: bool = True,
    ) -> None:
        self.context = context
        self.application = application if application is not None else Application(context)
        self.protocol = protocol or HostProtocol()
        self.stdin = stdin if stdin is not None else sys.stdin.buffer
        self.stdout = stdout if stdout is not None else sys.stdout.buffer
        self.stderr = stderr if stderr is not None else sys.stderr
        self.host_epoch = host_epoch or uuid.uuid4().hex
        #: One process serves one run of the loop and then closes the
        #: Application.  Embedders that drive ``serve`` repeatedly (tests) turn
        #: this off and own the Application's lifetime themselves.
        self.auto_shutdown = auto_shutdown
        self.subscriptions: dict[str, _Subscription] = {}
        self.initialized = False
        self.shutdown_requested = False
        self.transport_seq = 0
        self._write_lock = asyncio.Lock()
        self._subscriptions = SubscriptionService(
            store_factory=self._store_for,
            control_store=self.application.control,
        )
        self._outlet = _ProtocolOutlet(self.stdout, self.stderr)

    # ---- plumbing -------------------------------------------------------

    def _store_for(self, session_id: str) -> Any:
        """Return the canonical store for one session without side effects.

        Opening the database is a read: it creates no session, runs no recovery
        and writes no control row, which is what lets a read-only client attach
        to a session it does not own.

        A session the workspace never issued is refused rather than opened.
        Opening a *new* path would have the database driver create the file, so
        a subscription to a mistyped or never-issued id would leave a database
        behind -- the opposite of a read.
        """

        store = self.application._stores.get(session_id)
        if store is not None:
            return store

        if not self._session_exists(session_id):
            raise SubscriptionError(
                f"session {session_id!r} does not exist in this workspace"
            )

        store = SQLiteRuntimeStore(runtime_store_path(session_id, context=self.context))
        self.application._stores[session_id] = store
        return store

    def _session_exists(self, session_id: str) -> bool:
        """Whether this workspace already knows the session.

        Either the control store registered it, or its database is already on
        disk (a session this host is only inspecting).  Both are checks against
        existing state; neither creates anything.
        """

        try:
            registered = {
                str(row["session_id"])
                for row in self.application.control.list_sessions(self.workspace_id)
            }
            if session_id in registered:
                return True
        except Exception:  # noqa: BLE001 - a control-store read must not be fatal
            pass
        try:
            return runtime_store_path(session_id, context=self.context).exists()
        except ValueError:
            # A session id that cannot even form a path names nothing.
            return False

    @property
    def workspace_id(self) -> str:
        return self.context.workspace_id

    async def _write(self, payload: Mapping[str, Any]) -> None:
        async with self._write_lock:
            self._outlet.write(self.protocol.encode(payload))

    def _log(self, message: str) -> None:
        self.stderr.write(f"[host] {message}\n")
        with contextlib.suppress(Exception):
            self.stderr.flush()

    async def _reply(self, frame: Frame, result: Any) -> None:
        if frame.is_notification:
            return
        await self._write({"jsonrpc": "2.0", "id": frame.id, "result": result})

    async def _fail(self, frame: Frame | None, error: ProtocolError) -> None:
        if frame is not None and frame.is_notification:
            # A notification is never used to carry a required confirmation, so
            # a rejection is logged rather than answered.
            self._log(f"notification {frame.method!r} rejected: {error}")
            return
        await self._write(
            {
                "jsonrpc": "2.0",
                "id": frame.id if frame is not None else None,
                "error": error.to_error(),
            }
        )

    # ---- request handling ----------------------------------------------

    async def _dispatch(self, frame: Frame) -> None:
        handler = getattr(self, f"_rpc_{frame.method.replace('.', '_')}", None)
        if handler is None:
            if frame.method in CONTROL_METHODS:
                await self._fail(
                    frame,
                    ProtocolError(
                        f"{frame.method} belongs to the control slice and is not implemented "
                        "in this host build",
                        code=METHOD_NOT_FOUND,
                        business="not_implemented",
                    ),
                )
                return
            await self._fail(
                frame,
                ProtocolError(f"unknown method {frame.method!r}", code=METHOD_NOT_FOUND),
            )
            return
        if frame.method != "host.initialize" and not self.initialized:
            await self._fail(
                frame,
                ProtocolError(
                    "host.initialize must be the first call",
                    business="not_initialized",
                ),
            )
            return
        try:
            result = await handler(frame.params)
        except ProtocolError as error:
            await self._fail(frame, error)
            return
        except asyncio.CancelledError:
            raise
        except SubscriptionError as error:
            # The subscription service refuses a request it cannot serve.  That
            # is a caller-side problem, so it must not be reported as
            # `runtime_unavailable`: a client that cannot tell "your request is
            # wrong" from "the runtime is broken" cannot decide what to do.
            await self._fail(frame, self._subscription_error(error))
            return
        except Exception as error:  # noqa: BLE001 - surfaced as runtime_unavailable
            self._log(f"{frame.method} failed: {type(error).__name__}: {error}")
            await self._fail(
                frame,
                ProtocolError(
                    f"{frame.method} failed",
                    business="runtime_unavailable",
                    data={"detail": f"{type(error).__name__}: {error}"},
                ),
            )
            return
        await self._reply(frame, result)

    @staticmethod
    def _subscription_error(error: SubscriptionError) -> ProtocolError:
        """Map a refusal onto the declared business code that fits it."""

        if "does not exist" in str(error) or "no control store" in str(error):
            return ProtocolError(
                str(error),
                code=INVALID_PARAMS,
                business="scope_mismatch",
            )
        return ProtocolError(str(error), business="runtime_unavailable", code=INTERNAL_ERROR)

    # ---- methods --------------------------------------------------------

    async def _rpc_host_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        version = params.get("version", params.get("protocol_version"))
        if version != PROTOCOL_VERSION:
            raise ProtocolError(
                f"protocol version {version!r} is not supported",
                business="unsupported_version",
                data={"supported": PROTOCOL_VERSION},
            )
        requested = params.get("workspace_id")
        if requested is not None and requested != self.workspace_id:
            raise ProtocolError(
                "requested workspace does not match this host",
                business="scope_mismatch",
                data={"workspace_id": self.workspace_id},
            )
        self.initialized = True
        return {
            "protocol_version": PROTOCOL_VERSION,
            "host_epoch": self.host_epoch,
            "workspace_id": self.workspace_id,
            "capabilities": {
                "observation": list(OBSERVATION_METHODS),
                "control": [],
                "declared_not_implemented": list(CONTROL_METHODS),
                "batch": False,
            },
            "limits": {
                "frame_bytes": self.protocol.max_frame_bytes,
                "page_items": 100,
            },
            "settings": self._settings_state(),
        }

    def _settings_state(self) -> dict[str, Any]:
        """Report which provider settings are missing, never their values."""

        import os

        missing = [name for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY") if not os.environ.get(name)]
        model = os.environ.get("ROLLO_MODEL") or None
        return {
            "ready": not missing,
            "missing": missing,
            "model": model,
        }

    async def _rpc_session_list(self, params: dict[str, Any]) -> dict[str, Any]:
        response = self.application.session_list()
        sessions = list(response.data.get("sessions", ()))
        limit = self._limit(params, default=100, maximum=100)
        cursor = params.get("page_cursor")
        offset = self._cursor_offset(cursor)
        page = sessions[offset : offset + limit]
        next_offset = offset + len(page)
        return {
            "workspace_id": self.workspace_id,
            "sessions": [self._session_summary(row) for row in page],
            "next_page_cursor": str(next_offset) if next_offset < len(sessions) else None,
        }

    @staticmethod
    def _session_summary(row: Mapping[str, Any]) -> dict[str, Any]:
        """Withhold the canonical filesystem path from the wire.

        The renderer has no filesystem entry point, so the absolute database
        path is not its business; only the identity it can act on.
        """

        return {
            "session_id": row.get("session_id"),
            "workspace_id": row.get("workspace_id"),
            "status": row.get("status"),
        }

    async def _rpc_session_snapshot(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = self._require_session(params)
        result = await self._subscriptions.snapshot(session_id)
        return {
            "session_id": session_id,
            "host_epoch": self.host_epoch,
            "high_water": result.snapshot.high_water,
            "source_digest": result.snapshot.source_digest,
            "projection_version": result.cursor.projection_version,
            "snapshot": result.snapshot.to_dict(),
        }

    async def _rpc_events_subscribe(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = self._require_session(params)
        cursor = self._decode_cursor(params.get("cursor"))
        if cursor is None:
            stream = await self._subscriptions.subscribe(session_id)
            note = "subscribed"
        else:
            resumed = await self._subscriptions.resume(cursor)
            if resumed.status != "resumed" or resumed.stream is None:
                raise ProtocolError(
                    f"cursor cannot be resumed: {resumed.error_code}",
                    business="cursor_expired",
                    data={
                        "error_code": resumed.error_code,
                        "current_high_water": resumed.current_high_water,
                    },
                )
            stream = resumed.stream
            note = "resumed"
        subscription = _Subscription(
            subscription_id=stream.subscription_id,
            session_id=session_id,
            stream=stream,
        )
        self.subscriptions[subscription.subscription_id] = subscription
        subscription.task = asyncio.create_task(self._pump(subscription))
        return {
            "subscription_id": subscription.subscription_id,
            "session_id": session_id,
            # The resume token, exactly as the subscription service defines it.
            # Without it on the wire the declared resume path is unreachable: a
            # client cannot invent `service_epoch`, and the service refuses a
            # cursor that does not carry it.
            "cursor": stream.cursor.to_dict(),
            "host_epoch": self.host_epoch,
            "status": note,
        }

    async def _rpc_events_unsubscribe(self, params: dict[str, Any]) -> dict[str, Any]:
        subscription_id = self._require_session_field(params, "subscription_id")
        result = await self._subscriptions.unsubscribe(subscription_id)
        return {"subscription_id": subscription_id, "status": result.status}

    async def _rpc_events_detach(self, params: dict[str, Any]) -> dict[str, Any]:
        """Stop consuming without ending the subscription.

        ``events.unsubscribe`` closes the subscription, which invalidates its
        cursor by design.  A client that wants to reconnect later needs this
        instead: the buffer survives, so the issued cursor stays resumable.
        Without it the declared resume path has no way to be reached.
        """

        subscription_id = self._require_session_field(params, "subscription_id")
        result = await self._subscriptions.detach(subscription_id)
        if result.status == "unknown_subscription":
            raise ProtocolError(
                f"unknown subscription {subscription_id!r}",
                code=INVALID_PARAMS,
                business="scope_mismatch",
            )
        return {"subscription_id": subscription_id, "status": result.status}

    async def _rpc_host_shutdown(self, params: dict[str, Any]) -> dict[str, Any]:
        self.shutdown_requested = True
        await self._stop_subscriptions()
        return {"status": "shutting_down", "host_epoch": self.host_epoch}

    # ---- subscription pumping ------------------------------------------

    async def _pump(self, subscription: _Subscription) -> None:
        """Forward one subscription's messages as wire events until it ends."""

        try:
            async for message in subscription.stream:
                self.transport_seq += 1
                await self._write(
                    {
                        "jsonrpc": "2.0",
                        "method": "events.event",
                        "params": {
                            "subscription_id": subscription.subscription_id,
                            "session_id": subscription.session_id,
                            "host_epoch": self.host_epoch,
                            "transport_seq": self.transport_seq,
                            "kind": message.kind,
                            "ordinal": message.ordinal,
                            "key": message.key,
                            "prefix_boundary_exempt": message.prefix_boundary_exempt,
                            "payload": self._payload(message),
                        },
                    }
                )
                if message.kind in {"resync_required", "cursor_expired", "stream_closed"}:
                    return
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - reported on the wire, not raised
            self._log(f"subscription {subscription.subscription_id} failed: {error}")
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "method": "events.event",
                    "params": {
                        "subscription_id": subscription.subscription_id,
                        "session_id": subscription.session_id,
                        "host_epoch": self.host_epoch,
                        "transport_seq": self.transport_seq,
                        "kind": "cursor_expired",
                        "payload": {"reason": "transport_failure"},
                    },
                }
            )

    @staticmethod
    def _payload(message: Any) -> Any:
        """Render one delivery item as JSON."""

        payload = message.payload
        if hasattr(payload, "to_dict"):
            return payload.to_dict()
        if isinstance(payload, tuple):
            return [item.to_dict() if hasattr(item, "to_dict") else item for item in payload]
        return payload

    # ---- helpers --------------------------------------------------------

    def _require_session(self, params: Mapping[str, Any]) -> str:
        session_id = params.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ProtocolError("session_id is required", code=INVALID_PARAMS)
        return session_id

    @staticmethod
    def _require_session_field(params: Mapping[str, Any], field: str) -> str:
        """Read a required non-empty string parameter."""

        value = params.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ProtocolError(f"{field} is required", code=INVALID_PARAMS)
        return value

    def _limit(self, params: Mapping[str, Any], *, default: int, maximum: int) -> int:
        value = params.get("limit", default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProtocolError("limit must be an integer", code=INVALID_PARAMS)
        if value < 1:
            raise ProtocolError("limit must be positive", code=INVALID_PARAMS)
        return min(value, maximum)

    @staticmethod
    def _cursor_offset(cursor: Any) -> int:
        if cursor in (None, ""):
            return 0
        try:
            offset = int(cursor)
        except (TypeError, ValueError) as error:
            raise ProtocolError("page_cursor is not a valid cursor", code=INVALID_PARAMS) from error
        if offset < 0:
            raise ProtocolError("page_cursor is not a valid cursor", code=INVALID_PARAMS)
        return offset

    def _decode_cursor(self, raw: Any) -> GuiCursor | None:
        if raw in (None, ""):
            return None
        if not isinstance(raw, Mapping):
            raise ProtocolError("cursor must be an object", code=INVALID_PARAMS)
        try:
            return GuiCursor(
                subscription_id=str(raw["subscription_id"]),
                session_id=str(raw["session_id"]),
                high_water=int(raw["high_water"]),
                projection_version=str(raw["projection_version"]),
                service_epoch=str(raw.get("service_epoch", "")),
                partial_versions=tuple(
                    (str(key), int(value))
                    for key, value in dict(raw.get("partial_versions") or {}).items()
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ProtocolError(f"cursor is malformed: {error}", code=INVALID_PARAMS) from error

    # ---- lifecycle ------------------------------------------------------

    async def _stop_subscriptions(self) -> None:
        for subscription in list(self.subscriptions.values()):
            task = subscription.task
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self.subscriptions.clear()
        with contextlib.suppress(SubscriptionError, Exception):
            await self._subscriptions.aclose()

    async def serve(self) -> int:
        """Read frames until stdin closes or a shutdown is requested."""

        with self._outlet:
            try:
                await self._read_loop()
            finally:
                await self._stop_subscriptions()
                if self.auto_shutdown:
                    with contextlib.suppress(Exception):
                        await self.application.shutdown()
        return 0

    async def _read_loop(self) -> None:
        while not self.shutdown_requested:
            chunk = await self._read_chunk()
            if not chunk:
                self._log("stdin closed")
                return
            try:
                frames = self.protocol.feed(chunk)
            except FrameTooLarge as error:
                # The frame boundary is no longer trustworthy, so the contract is
                # to close with a diagnostic rather than guess.
                await self._fail(
                    None,
                    ProtocolError(str(error), business="frame_too_large"),
                )
                self._log(f"closing connection: {error}")
                return
            except ProtocolError as error:
                await self._fail(None, error)
                continue
            for frame in frames:
                await self._dispatch(frame)
                if self.shutdown_requested:
                    return

    async def _read_chunk(self) -> bytes:
        """Read one chunk without blocking the event loop.

        The blocking read runs in a worker thread, so subscriptions keep being
        served while the host waits for input -- the whole point of a duplex
        protocol.  The caller checks between reads whether a shutdown arrived, so
        a close does not have to wait for the peer to send something.
        """

        return await asyncio.to_thread(self.stdin.read1, 65536)


def build_context(workspace: str, runtime_dir: str | None = None) -> ProjectContext:
    """Resolve the workspace once at start-up, honouring explicit arguments."""

    root = Path(workspace).expanduser().resolve()
    return ProjectContext.from_root(
        root,
        runtime_data_dir=Path(runtime_dir).expanduser().resolve() if runtime_dir else None,
    )


__all__ = ["HostServer", "build_context", "OBSERVATION_METHODS", "CONTROL_METHODS"]
