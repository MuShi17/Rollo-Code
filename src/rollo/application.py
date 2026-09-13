"""The process-local Application control plane for C03.

This module intentionally sits above the C02 canonical event store.  SQLite
control rows provide command idempotency, ownership and recovery evidence;
``Agent`` and ``SQLiteRuntimeStore`` continue to own provider-neutral runtime
facts.  No public method consults Agent private lifecycle fields.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol

from .interactions import (
    InteractionError,
    InteractionReply,
    InteractionRequest,
    InteractionState,
)
from .project_context import ProjectContext, require_context
from .runtime_ports import NullOutputPort, OutputPort
from .runtime_store import SQLiteRuntimeStore
from .session import runtime_store_path
from .workspace_lock import WorkspaceLock, WorkspaceLockBusyError, workspace_lock_key

__all__ = [
    "APPLICATION_SCHEMA_VERSION",
    "canonical_json_bytes",
    "full_sha256",
    "plan_digest",
    "params_digest",
    "write_ready_barrier",
    "CanonicalProjection",
    "project_canonical_evidence",
    "ApplicationError",
    "CommandConflictError",
    "OwnerConflictError",
    "InteractionBindingError",
    "RecoveryRequiredError",
    "ApplicationClosedError",
    "ApplicationResponse",
    "CommandEnvelope",
    "ControlStore",
    "ApplicationInteractionPort",
    "Application",
]

APPLICATION_SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical_json_bytes(value: Any) -> bytes:
    """Canonical JSON v1 used by public Application digests.

    The runtime accepts only finite JSON values.  Sorting keys, preserving list
    order and removing insignificant whitespace gives a stable byte stream for
    the identity fields used by C03.  The implementation deliberately does not
    use ``default=str``: silently stringifying an input would make approval
    binding weaker.
    """

    return _jcs_encode(value).encode("utf-8")


def _jcs_encode(value: Any) -> str:
    """Encode the JSON data model using RFC 8785's compact ordering rules."""

    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, int) and not isinstance(value, bool):
        # RFC 8785 is defined over ECMAScript Number values.  Refusing an
        # integer outside the exact IEEE-754 safe range is preferable to
        # silently emitting Python's arbitrary-precision spelling, which a
        # JavaScript verifier would round to a different digest.
        if abs(value) > 2**53 - 1:
            raise ValueError("JCS integer exceeds the IEEE-754 safe range")
        return str(value)
    if isinstance(value, float):
        return _jcs_float(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_jcs_encode(item) for item in value) + "]"
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("JCS object keys must be strings")
        # RFC 8785 sorts the UTF-16 code units, rather than Unicode scalar
        # values (the distinction matters for supplementary-plane keys).
        ordered = sorted(value.items(), key=lambda item: item[0].encode("utf-16-be", "surrogatepass"))
        return "{" + ",".join(
            json.dumps(key, ensure_ascii=False, separators=(",", ":")) + ":" + _jcs_encode(item)
            for key, item in ordered
        ) + "}"
    raise TypeError(f"value is not JSON serializable for JCS: {type(value).__name__}")


def _jcs_float(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("JCS does not permit NaN or Infinity")
    if value == 0:
        return "0"
    raw = repr(value).lower()
    sign = ""
    if raw.startswith("-"):
        sign, raw = "-", raw[1:]
    if "e" in raw:
        mantissa, exponent_text = raw.split("e", 1)
        exponent = int(exponent_text)
    else:
        mantissa, exponent = raw, 0
    if "." in mantissa:
        whole, fraction = mantissa.split(".", 1)
        digits = whole + fraction
        decimal_position = len(whole) + exponent
    else:
        digits = mantissa
        decimal_position = len(mantissa) + exponent
    digits = digits.rstrip("0") or "0"
    # repr() can contain a decimal zero only when the value itself is zero;
    # for non-zero values stripping it preserves the shortest representation.
    if decimal_position > 0 and decimal_position <= 21:
        if decimal_position >= len(digits):
            number = digits + "0" * (decimal_position - len(digits))
        else:
            number = digits[:decimal_position] + "." + digits[decimal_position:]
    elif decimal_position <= 0 and decimal_position > -6:
        number = "0." + "0" * (-decimal_position) + digits
    else:
        exponent_value = decimal_position - 1
        number = digits[0]
        if len(digits) > 1:
            number += "." + digits[1:]
        number += "e" + ("+" if exponent_value >= 0 else "") + str(exponent_value)
    return sign + number


def full_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def plan_digest(plan_id: str | None, displayed_plan: str | None) -> str:
    return full_sha256({"plan_id": plan_id, "displayed_plan": displayed_plan})


def params_digest(
    *,
    session_id: str | None,
    run_id: str | None,
    request_id: str | None,
    tool_call_id: str | None,
    tool_name: str | None,
    tool_input: Any,
    plan_id: str | None,
    plan_digest_value: str | None = None,
    plan_digest: str | None = None,
) -> str:
    if plan_digest_value is not None and plan_digest is not None and plan_digest_value != plan_digest:
        raise ValueError("plan_digest and plan_digest_value disagree")
    effective_plan_digest = plan_digest_value if plan_digest_value is not None else plan_digest
    return full_sha256(
        {
            "session_id": session_id,
            "run_id": run_id,
            "request_id": request_id,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "plan_id": plan_id,
            "plan_digest": effective_plan_digest,
        }
    )


def write_ready_barrier(
    path: str | Path,
    *,
    barrier: str,
    workspace_id: str,
    command_id: str,
    marker_path: str | Path,
    expected_count: int = 0,
    operation: str | None = None,
) -> Path:
    """Write the deterministic worker handshake used by crash-barrier tests."""

    payload = {
        "schema_version": 1,
        "barrier": barrier,
        "operation": operation,
        "workspace_id": workspace_id,
        "command_id": command_id,
        "marker_path": str(marker_path),
        "expected_count": int(expected_count),
        "pid": os.getpid(),
        "created_at": _utc_now(),
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(canonical_json_bytes(payload))
    os.replace(temporary, target)
    return target


@dataclass(frozen=True, slots=True)
class CanonicalProjection:
    status: str
    error_code: str | None
    terminal: str | None
    correlation: Mapping[str, Any]
    side_effect_count: int


def project_canonical_evidence(
    events: list[Any] | tuple[Any, ...],
    *,
    session_id: str,
    run_id: str,
    side_effect_count: int = 0,
) -> CanonicalProjection:
    """Project C02 events into the deterministic D14 recovery oracle."""

    invocations: set[str] = set()
    turns: set[str] = set()
    terminal_invocations: set[str] = set()
    operations: dict[str, dict[str, Any]] = {}
    provider_calls: dict[str, str] = {}
    terminal_candidates: set[str] = set()
    outcome_records: list[dict[str, Any]] = []

    def value(event: Any, key: str, default: Any = None) -> Any:
        if isinstance(event, Mapping):
            return event.get(key, default)
        return getattr(event, key, default)

    def mapping(value_: Any) -> Mapping[str, Any]:
        return value_ if isinstance(value_, Mapping) else {}

    for event in events:
        if value(event, "session_id") != session_id or value(event, "run_id") != run_id:
            return CanonicalProjection("uncertain", "canonical_identity_conflict", None, {"events": list(events)}, side_effect_count)
        invocation = value(event, "invocation_id")
        turn = value(event, "turn_id")
        if invocation:
            invocations.add(str(invocation))
        if turn:
            turns.add(str(turn))
        actions = mapping(value(event, "actions"))
        dispatch = mapping(actions.get("tool_dispatch"))
        if dispatch:
            operation_id = dispatch.get("operation_id") or mapping(value(event, "refs")).get("operation_id")
            provider_call = dispatch.get("provider_tool_call_id") or mapping(value(event, "refs")).get("provider_tool_call_id")
            tool_name = dispatch.get("tool_name") or dispatch.get("name")
            args_hash = dispatch.get("canonical_args_hash") or dispatch.get("arguments_digest")
            if not all(isinstance(item, str) and item for item in (operation_id, provider_call, tool_name, args_hash)):
                return CanonicalProjection("uncertain", "canonical_identity_missing", None, {"events": list(events)}, 0)
            pair = {"operation_id": operation_id, "provider_tool_call_id": provider_call, "tool_name": tool_name, "canonical_args_hash": args_hash}
            prior = operations.get(operation_id)
            if prior is not None and prior != pair:
                return CanonicalProjection("uncertain", "canonical_identity_conflict", None, {"events": list(events)}, side_effect_count)
            prior_call = provider_calls.get(provider_call)
            if prior_call is not None and prior_call != operation_id:
                return CanonicalProjection("uncertain", "canonical_identity_conflict", None, {"events": list(events)}, side_effect_count)
            operations[operation_id] = pair
            provider_calls[provider_call] = operation_id
        event_status = value(event, "status")
        terminal_action = mapping(actions.get("run_terminal"))
        if event_status in {"completed", "failed", "cancelled", "budget_exceeded"}:
            terminal_candidates.add(str(event_status))
            if invocation:
                terminal_invocations.add(str(invocation))
        elif event_status == "aborted":
            terminal_candidates.add("aborted")
            if invocation:
                terminal_invocations.add(str(invocation))
        elif terminal_action.get("status") in {"completed", "failed", "cancelled", "budget_exceeded"}:
            terminal_candidates.add(str(terminal_action["status"]))
            if invocation:
                terminal_invocations.add(str(invocation))
        outcome = mapping(actions.get("tool_outcome"))
        if outcome:
            outcome_id = outcome.get("operation_id") or mapping(value(event, "refs")).get("operation_id")
            if not isinstance(outcome_id, str) or not outcome_id:
                return CanonicalProjection("uncertain", "canonical_identity_missing", None, {"events": list(events)}, 0)
            outcome_records.append({
                "operation_id": outcome_id,
                "provider_tool_call_id": outcome.get("provider_tool_call_id") or mapping(value(event, "refs")).get("tool_call_id"),
                "tool_name": outcome.get("tool_name") or outcome.get("name"),
            })

    correlation = {
        "version": 1,
        "session_id": session_id,
        "run_id": run_id,
        "invocation_ids": sorted(invocations),
        "turn_ids": sorted(turns),
        "tool_operations": [operations[key] for key in sorted(operations)],
    }
    if len(terminal_candidates) > 1:
        return CanonicalProjection("uncertain", "canonical_identity_ambiguous", None, correlation, side_effect_count)
    terminal = next(iter(terminal_candidates), None)
    # Provider-only terminal evidence is safe only when it names one candidate.
    # The candidates are the invocations that actually carry a terminal (or the
    # turns observed), never every invocation id present in the run: every run
    # opens a run-level invocation and a *distinct* invocation per model call
    # (agent.py:702), so counting all observed invocations would classify every
    # provider-only run as ambiguous — including a plainly successful one-turn
    # run, which design.md maps to ``succeeded/null``.  A run reusing one turn
    # across several model calls stays unambiguous; two different terminals, or
    # terminal evidence spread over several turns, still forces a decision.
    if (
        not operations
        and terminal in {"completed", "failed"}
        and (len(terminal_invocations) > 1 or len(turns) > 1)
    ):
        return CanonicalProjection("uncertain", "canonical_identity_ambiguous", None, correlation, side_effect_count)
    if operations:
        # A dispatch without a terminal/outcome event is evidence of an
        # unknown side effect.  The event model exposes outcomes as a matching
        # tool_outcome action carrying the same operation_id.
        outcome_ids = {record["operation_id"] for record in outcome_records}
        if any(operation_id not in outcome_ids for operation_id in operations):
            return CanonicalProjection("uncertain", "tool_outcome_uncertain", None, correlation, side_effect_count)
        for outcome in outcome_records:
            pair = operations.get(outcome["operation_id"])
            if pair is None:
                return CanonicalProjection("uncertain", "canonical_identity_conflict", None, correlation, side_effect_count)
            if outcome["provider_tool_call_id"] and outcome["provider_tool_call_id"] != pair["provider_tool_call_id"]:
                return CanonicalProjection("uncertain", "canonical_identity_conflict", None, correlation, side_effect_count)
            if outcome["tool_name"] and outcome["tool_name"] != pair["tool_name"]:
                return CanonicalProjection("uncertain", "canonical_identity_conflict", None, correlation, side_effect_count)
    mapping_result = {
        "completed": ("succeeded", None),
        "failed": ("failed", "provider_error"),
        "cancelled": ("cancelled", "cancelled"),
        "budget_exceeded": ("failed", "budget_exceeded"),
    }
    status, error_code = mapping_result.get(terminal or "", ("interrupted", "run_dispatch_not_observed"))
    return CanonicalProjection(status, error_code, terminal, correlation, side_effect_count)


class ApplicationError(RuntimeError):
    code = "application_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class CommandConflictError(ApplicationError):
    code = "digest_conflict"


class OwnerConflictError(ApplicationError):
    code = "owner_conflict"


class InteractionBindingError(ApplicationError):
    code = "interaction_binding_error"


class RecoveryRequiredError(ApplicationError):
    code = "recovery_required"


class ApplicationClosedError(ApplicationError):
    code = "application_closed"


@dataclass(frozen=True, slots=True)
class CommandEnvelope:
    command_id: str
    scope_type: str
    scope_id: str
    operation: str
    params_digest: str
    session_id: str | None = None
    run_id: str | None = None
    request_id: str | None = None
    schema_version: int = APPLICATION_SCHEMA_VERSION
    submitted_at: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        for name in ("command_id", "scope_type", "scope_id", "operation", "params_digest"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "operation": self.operation,
            "params_digest": self.params_digest,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "request_id": self.request_id,
            "schema_version": self.schema_version,
            "submitted_at": self.submitted_at,
        }


@dataclass(frozen=True, slots=True)
class ApplicationResponse:
    operation: str
    status: str
    result: str = "ok"
    command_id: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    request_id: str | None = None
    error_code: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error_code is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "status": self.status,
            "result": self.result,
            "command_id": self.command_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "request_id": self.request_id,
            "error_code": self.error_code,
            "data": dict(self.data),
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __await__(self):
        async def _ready() -> "ApplicationResponse":
            return self

        return _ready().__await__()


def _response_from_json(raw: str | bytes | None) -> ApplicationResponse | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
        return ApplicationResponse(
            operation=value["operation"],
            status=value["status"],
            result=value.get("result", "ok"),
            command_id=value.get("command_id"),
            session_id=value.get("session_id"),
            run_id=value.get("run_id"),
            request_id=value.get("request_id"),
            error_code=value.get("error_code"),
            data=value.get("data") or {},
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApplicationError(f"invalid persisted Application response: {exc}") from exc


def _response_json(response: ApplicationResponse) -> str:
    return json.dumps(response.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_SENSITIVE_INPUT_KEYS = frozenset({
    "api_key", "apikey", "authorization", "password", "passwd", "secret",
    "token", "access_token", "refresh_token", "provider_config",
})


def _redact_control_value(value: Any, *, key: str | None = None) -> Any:
    if key is not None and key.lower() in _SENSITIVE_INPUT_KEYS:
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(item_key): _redact_control_value(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_control_value(item) for item in value]
    return value


class ControlStore:
    """Versioned workspace-level control store, separate from canonical facts."""

    schema_version = 1

    def __init__(self, database: str | Path) -> None:
        self.database = Path(database)
        if not self.database.is_absolute():
            raise ValueError("control database path must be absolute")
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.database), timeout=5.0, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self._migrate()

    def _migrate(self) -> None:
        current = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if current > self.schema_version:
            raise ApplicationError("control database schema is newer", code="schema_version_error")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS control_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                canonical_path TEXT NOT NULL,
                create_command_id TEXT,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_workspace ON sessions(workspace_id, created_at);
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                command_id TEXT NOT NULL,
                owner_pid INTEGER NOT NULL DEFAULT 0,
                parent_run_id TEXT,
                decision_generation INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                error_code TEXT,
                prompt_digest TEXT,
                canonical_correlation_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT,
                response_json TEXT,
                dispatch_intent INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(session_id, command_id)
            );
            CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id, created_at);
            CREATE TABLE IF NOT EXISTS session_leases (
                session_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                pid INTEGER NOT NULL,
                acquired_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_interactions (
                request_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                tool_call_id TEXT,
                tool_name TEXT,
                tool_input_json TEXT,
                plan_id TEXT,
                plan_digest TEXT,
                metadata_json TEXT,
                params_digest TEXT NOT NULL,
                command_id TEXT,
                prompt TEXT NOT NULL DEFAULT '',
                expires_at TEXT,
                process_id INTEGER NOT NULL,
                decision_generation INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                reply_json TEXT,
                error_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pending_run ON pending_interactions(run_id, status);
            CREATE TABLE IF NOT EXISTS cancel_generations (
                run_id TEXT PRIMARY KEY,
                generation INTEGER NOT NULL,
                command_id TEXT NOT NULL,
                status TEXT NOT NULL,
                error_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        # Additive, in-place migration for control databases written by the v2
        # owner/command-ledger layout.  The legacy ``owners``/``commands``/
        # ``tool_operations`` tables are left untouched but are no longer read:
        # runs already carry the facts they duplicated.
        columns = {str(row["name"]) for row in self.connection.execute("PRAGMA table_info(runs)").fetchall()}
        if "owner_pid" not in columns:
            self.connection.execute("ALTER TABLE runs ADD COLUMN owner_pid INTEGER NOT NULL DEFAULT 0")
        if "response_json" not in columns:
            self.connection.execute("ALTER TABLE runs ADD COLUMN response_json TEXT")
        session_columns = {str(row["name"]) for row in self.connection.execute("PRAGMA table_info(sessions)").fetchall()}
        if "create_command_id" not in session_columns:
            self.connection.execute("ALTER TABLE sessions ADD COLUMN create_command_id TEXT")
        pending_columns = {str(row["name"]) for row in self.connection.execute("PRAGMA table_info(pending_interactions)").fetchall()}
        if "metadata_json" not in pending_columns:
            self.connection.execute("ALTER TABLE pending_interactions ADD COLUMN metadata_json TEXT")
        if "command_id" not in pending_columns:
            self.connection.execute("ALTER TABLE pending_interactions ADD COLUMN command_id TEXT")
        self.connection.execute(f"PRAGMA user_version = {self.schema_version}")
        self.connection.execute(
            "INSERT OR REPLACE INTO control_meta(key,value) VALUES('schema_version',?)",
            (str(self.schema_version),),
        )

    @contextlib.contextmanager
    def transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except Exception:
            if self.connection.in_transaction:
                self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def get_command(self, envelope: CommandEnvelope) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM commands WHERE scope_type=? AND scope_id=? AND command_id=?",
            (envelope.scope_type, envelope.scope_id, envelope.command_id),
        ).fetchone()

    def register_session(self, *, workspace_id: str, session_id: str, canonical_path: Path) -> None:
        now = _utc_now()
        with self.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO sessions(session_id,workspace_id,canonical_path,created_at) VALUES(?,?,?,?)",
                (session_id, workspace_id, str(canonical_path), now),
            )

    def list_sessions(self, workspace_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM sessions WHERE workspace_id=? ORDER BY created_at,session_id",
            (workspace_id,),
        ).fetchall()

    def insert_owner(
        self,
        *,
        owner_id: str,
        workspace_id: str,
        generation: int,
        lock_key: str,
        parent_owner_id: str | None = None,
    ) -> None:
        now = _utc_now()
        with self.transaction() as db:
            db.execute(
                "INSERT INTO owners(owner_id,workspace_id,root_owner_id,parent_owner_id,generation,process_id,lock_key,status,quarantine,evidence_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (owner_id, workspace_id, owner_id, parent_owner_id, generation, os.getpid(), lock_key, "active", 0, "{}", now, now),
            )

    def owner_rows(self, workspace_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM owners WHERE workspace_id=? ORDER BY created_at,owner_id",
            (workspace_id,),
        ).fetchall()

    def owner(self, owner_id: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM owners WHERE owner_id=?", (owner_id,)).fetchone()

    def update_owner(self, owner_id: str, **fields: Any) -> None:
        allowed = {"status", "quarantine", "evidence_json", "updated_at", "generation"}
        values = {key: value for key, value in fields.items() if key in allowed}
        if not values:
            return
        values.setdefault("updated_at", _utc_now())
        assignments = ",".join(f"{key}=?" for key in values)
        with self.transaction() as db:
            db.execute(
                f"UPDATE owners SET {assignments} WHERE owner_id=?",
                (*values.values(), owner_id),
            )

    def insert_run_and_command(
        self,
        envelope: CommandEnvelope,
        *,
        run_id: str,
        workspace_id: str,
        owner_id: str,
        prompt_digest: str,
        parent_run_id: str | None,
        decision_generation: int = 0,
    ) -> ApplicationResponse:
        now = _utc_now()
        response = ApplicationResponse(
            operation=envelope.operation,
            status="queued",
            result="accepted",
            command_id=envelope.command_id,
            session_id=envelope.session_id,
            run_id=run_id,
            data={"owner_id": owner_id, "dispatch_intent": False},
        )
        with self.transaction() as db:
            existing = db.execute(
                "SELECT * FROM commands WHERE scope_type=? AND scope_id=? AND command_id=?",
                (envelope.scope_type, envelope.scope_id, envelope.command_id),
            ).fetchone()
            if existing is not None:
                if existing["params_digest"] != envelope.params_digest:
                    raise CommandConflictError("command_id 已被不同参数摘要使用")
                stored = _response_from_json(existing["response_json"])
                if stored is None:
                    raise ApplicationError("accepted command has no response")
                return stored
            db.execute(
                "INSERT INTO commands(scope_type,scope_id,command_id,operation,params_digest,session_id,run_id,status,error_code,response_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (envelope.scope_type, envelope.scope_id, envelope.command_id, envelope.operation, envelope.params_digest, envelope.session_id, run_id, "accepted", None, _response_json(response), now, now),
            )
            db.execute(
                "INSERT INTO runs(run_id,session_id,workspace_id,command_id,owner_id,parent_run_id,decision_generation,status,error_code,prompt_digest,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, envelope.session_id, workspace_id, envelope.command_id, owner_id, parent_run_id, decision_generation, "queued", None, prompt_digest, now, now),
            )
        return response

    def record_command(self, envelope: CommandEnvelope, response: ApplicationResponse, *, status: str = "accepted") -> ApplicationResponse:
        """Persist a non-run command response under the same idempotency key."""

        now = _utc_now()
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM commands WHERE scope_type=? AND scope_id=? AND command_id=?",
                (envelope.scope_type, envelope.scope_id, envelope.command_id),
            ).fetchone()
            if row is not None:
                if row["params_digest"] != envelope.params_digest:
                    raise CommandConflictError("command_id 已被不同参数摘要使用")
                return _response_from_json(row["response_json"]) or response
            db.execute(
                "INSERT INTO commands(scope_type,scope_id,command_id,operation,params_digest,session_id,run_id,request_id,status,error_code,response_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (envelope.scope_type, envelope.scope_id, envelope.command_id, envelope.operation, envelope.params_digest, envelope.session_id, envelope.run_id, envelope.request_id, status, response.error_code, _response_json(response), now, now),
            )
        return response

    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        error_code: str | None = None,
        result: Mapping[str, Any] | None = None,
        dispatch_intent: bool | None = None,
    ) -> None:
        fields: dict[str, Any] = {"updated_at": _utc_now()}
        if status is not None:
            fields["status"] = status
        if error_code is not None or (status in {"succeeded", "failed", "cancelled", "interrupted", "uncertain"}):
            fields["error_code"] = error_code
        if result is not None:
            fields["result_json"] = json.dumps(result, ensure_ascii=False, sort_keys=True)
        if dispatch_intent is not None:
            fields["dispatch_intent"] = int(dispatch_intent)
        assignments = ",".join(f"{key}=?" for key in fields)
        with self.transaction() as db:
            db.execute(f"UPDATE runs SET {assignments} WHERE run_id=?", (*fields.values(), run_id))

    def finalize_run(
        self,
        run_id: str,
        *,
        status: str,
        error_code: str | None,
        result: Mapping[str, Any] | None = None,
    ) -> str:
        """Atomically commit a terminal observation against cancellation.

        A cross-Application cancel may arrive between a task's last read and
        its terminal write.  Performing the guard and update in one immediate
        transaction prevents a stale task from overwriting ``cancelling`` or
        ``cancelled`` with ``succeeded``.
        """

        now = _utc_now()
        with self.transaction() as db:
            row = db.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                return status
            current = str(row["status"])
            cancel = db.execute(
                "SELECT status FROM cancel_generations WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if current in {"interrupted", "uncertain"}:
                return current
            if current in {"cancelling", "cancelled"} or (cancel is not None and cancel["status"] in {"cancelling", "cancelled"}):
                db.execute(
                    "UPDATE runs SET status='cancelled',error_code='cancelled',result_json=?,updated_at=? WHERE run_id=?",
                    (json.dumps({"reason": "cancelled"}, ensure_ascii=False, sort_keys=True), now, run_id),
                )
                if cancel is not None:
                    db.execute(
                        "UPDATE cancel_generations SET status='cancelled',error_code='cancelled',updated_at=? WHERE run_id=?",
                        (now, run_id),
                    )
                return "cancelled"
            db.execute(
                "UPDATE runs SET status=?,error_code=?,result_json=?,updated_at=? WHERE run_id=?",
                (status, error_code, json.dumps(result, ensure_ascii=False, sort_keys=True) if result is not None else None, now, run_id),
            )
            return status

    def update_correlation(self, run_id: str, correlation: Mapping[str, Any]) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE runs SET canonical_correlation_json=?,updated_at=? WHERE run_id=?",
                (json.dumps(correlation, ensure_ascii=False, sort_keys=True, separators=(",", ":")), _utc_now(), run_id),
            )

    def run(self, run_id: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()

    def runs_for_session(self, session_id: str) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM runs WHERE session_id=? ORDER BY created_at,run_id", (session_id,)).fetchall()

    def runs_for_owner(self, owner_id: str) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM runs WHERE owner_id=? ORDER BY created_at,run_id", (owner_id,)).fetchall()

    def update_command_for_run(self, run_id: str, response: ApplicationResponse) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE commands SET status=?,error_code=?,response_json=?,updated_at=? WHERE run_id=?",
                (response.status, response.error_code, _response_json(response), _utc_now(), run_id),
            )

    def insert_pending(self, request: InteractionRequest, *, workspace_id: str, process_id: int) -> None:
        now = _utc_now()
        tool_input = getattr(request, "tool_input", None)
        metadata = getattr(request, "metadata", None)
        with self.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO pending_interactions(request_id,workspace_id,session_id,run_id,tool_call_id,tool_name,tool_input_json,plan_id,plan_digest,metadata_json,params_digest,prompt,expires_at,process_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (request.request_id, workspace_id, request.session_id, request.run_id, request.tool_call_id, request.tool_name, json.dumps(_redact_control_value(tool_input), ensure_ascii=False, sort_keys=True) if tool_input is not None else None, getattr(request, "plan_id", None), getattr(request, "plan_digest", None), json.dumps(_redact_control_value(metadata), ensure_ascii=False, sort_keys=True) if metadata is not None else None, request.params_digest, request.prompt, getattr(request, "expires_at_utc", None) or _expires_at_iso(request.expires_at), process_id, "pending", now, now),
            )

    def pending(self, request_id: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM pending_interactions WHERE request_id=?", (request_id,)).fetchone()

    def pending_for_run(self, run_id: str) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM pending_interactions WHERE run_id=? ORDER BY created_at", (run_id,)).fetchall()

    def complete_pending(self, request_id: str, *, status: str, reply: InteractionReply | None = None, error_code: str | None = None) -> None:
        now = _utc_now()
        reply_json = json.dumps(_reply_to_dict(reply), ensure_ascii=False, sort_keys=True) if reply is not None else None
        with self.transaction() as db:
            db.execute(
                "UPDATE pending_interactions SET status=?,reply_json=?,error_code=?,updated_at=? WHERE request_id=?",
                (status, reply_json, error_code, now, request_id),
            )

    def mark_old_pending_interrupted(self) -> list[str]:
        rows = self.connection.execute(
            "SELECT request_id,process_id,expires_at FROM pending_interactions WHERE status='pending'",
        ).fetchall()
        interrupted: list[str] = []
        expired: list[str] = []
        now = datetime.now(timezone.utc)
        for row in rows:
            expires_at = row["expires_at"]
            is_expired = False
            if expires_at:
                try:
                    text = str(expires_at)
                    if text.endswith("Z"):
                        text = text[:-1] + "+00:00"
                    is_expired = datetime.fromisoformat(text).astimezone(timezone.utc) <= now
                except (TypeError, ValueError):
                    # An invalid persisted deadline is not permission to wait
                    # forever; treat it as an interrupted old request.
                    is_expired = False
            if is_expired:
                expired.append(str(row["request_id"]))
            elif int(row["process_id"]) != os.getpid():
                interrupted.append(str(row["request_id"]))
        if interrupted or expired:
            with self.transaction() as db:
                if interrupted:
                    placeholders = ",".join("?" for _ in interrupted)
                    db.execute(
                        f"UPDATE pending_interactions SET status='interrupted',error_code='interaction_interrupted',updated_at=? WHERE request_id IN ({placeholders}) AND status='pending'",
                        (_utc_now(), *interrupted),
                    )
                if expired:
                    placeholders = ",".join("?" for _ in expired)
                    db.execute(
                        f"UPDATE pending_interactions SET status='expired',error_code='interaction_expired',updated_at=? WHERE request_id IN ({placeholders}) AND status='pending'",
                        (_utc_now(), *expired),
                    )
        return interrupted + expired

    def create_cancel(self, run_id: str, command_id: str) -> tuple[int, bool, sqlite3.Row | None]:
        now = _utc_now()
        with self.transaction() as db:
            row = db.execute("SELECT * FROM cancel_generations WHERE run_id=?", (run_id,)).fetchone()
            if row is not None:
                return int(row["generation"]), False, row
            db.execute(
                "INSERT INTO cancel_generations(run_id,generation,command_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (run_id, 1, command_id, "cancelling", now, now),
            )
            return 1, True, None

    def update_cancel(self, run_id: str, *, status: str, error_code: str | None = None) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE cancel_generations SET status=?,error_code=?,updated_at=? WHERE run_id=?",
                (status, error_code, _utc_now(), run_id),
            )

    def record_tool_operation(
        self,
        *,
        run_id: str,
        operation_id: str,
        provider_tool_call_id: str,
        tool_name: str,
        canonical_args_hash: str,
        invocation_id: str | None = None,
        turn_id: str | None = None,
        state: str = "dispatched",
    ) -> None:
        with self.transaction() as db:
            existing = db.execute(
                "SELECT * FROM tool_operations WHERE operation_id=? OR (run_id=? AND provider_tool_call_id=?)",
                (operation_id, run_id, provider_tool_call_id),
            ).fetchone()
            if existing is not None:
                same = (
                    existing["operation_id"] == operation_id
                    and existing["run_id"] == run_id
                    and existing["provider_tool_call_id"] == provider_tool_call_id
                    and existing["tool_name"] == tool_name
                    and existing["canonical_args_hash"] == canonical_args_hash
                    and existing["invocation_id"] == invocation_id
                    and existing["turn_id"] == turn_id
                )
                if same:
                    return
                raise ApplicationError("canonical tool identity conflict", code="control_canonical_conflict")
            db.execute(
                "INSERT INTO tool_operations(operation_id,run_id,invocation_id,turn_id,provider_tool_call_id,tool_name,canonical_args_hash,state) VALUES(?,?,?,?,?,?,?,?)",
                (operation_id, run_id, invocation_id, turn_id, provider_tool_call_id, tool_name, canonical_args_hash, state),
            )
            rows = db.execute(
                "SELECT operation_id,invocation_id,turn_id,provider_tool_call_id,tool_name,canonical_args_hash FROM tool_operations WHERE run_id=? ORDER BY operation_id",
                (run_id,),
            ).fetchall()
            run = db.execute("SELECT session_id,run_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
            operations = [dict(row) for row in rows]
            db.execute(
                "UPDATE runs SET canonical_correlation_json=?,updated_at=? WHERE run_id=?",
                (json.dumps({"version": 1, "session_id": run["session_id"] if run else None, "run_id": run_id, "tool_operations": operations, "invocation_ids": sorted({row["invocation_id"] for row in rows if row["invocation_id"]}), "turn_ids": sorted({row["turn_id"] for row in rows if row["turn_id"]})}, ensure_ascii=False, sort_keys=True), _utc_now(), run_id),
            )

    def tool_operations(self, run_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM tool_operations WHERE run_id=? ORDER BY operation_id",
            (run_id,),
        ).fetchall()


def _expires_at_iso(value: float | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _reply_to_dict(reply: InteractionReply | None) -> dict[str, Any] | None:
    if reply is None:
        return None
    return {
        "request_id": reply.request_id,
        "approved": reply.approved,
        "answer": reply.answer,
        "params_digest": reply.params_digest,
        "source": reply.source,
        "session_id": reply.session_id,
        "run_id": reply.run_id,
        "tool_call_id": reply.tool_call_id,
        "tool_name": reply.tool_name,
        # Replies may carry the sensitive value needed by the in-process
        # registry, but the durable control ledger follows the same redaction
        # policy as pending requests.
        "tool_input": _redact_control_value(getattr(reply, "tool_input", None)),
        "plan_id": getattr(reply, "plan_id", None),
        "plan_digest": getattr(reply, "plan_digest", None),
        "metadata": _redact_control_value(getattr(reply, "metadata", None)),
    }


class _AgentFactory(Protocol):
    def __call__(self, **kwargs: Any) -> Any: ...


class ApplicationInteractionPort:
    """Bridge C02's registry/Future to an Application control record."""

    name = "application"

    def __init__(self, application: "Application", agent: Any, delegate: Any) -> None:
        self.application = application
        self.agent = agent
        self.delegate = delegate
        self._futures: dict[str, asyncio.Future[InteractionReply]] = {}
        self._delegates: dict[str, asyncio.Task[Any]] = {}

    async def request(self, request: InteractionRequest) -> InteractionReply:
        self.application._register_pending(request, self.agent)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[InteractionReply] = loop.create_future()
        self._futures[request.request_id] = future
        delegate_task = asyncio.create_task(self.delegate.request(request))
        self._delegates[request.request_id] = delegate_task
        try:
            done, _ = await asyncio.wait(
                {future, delegate_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if future in done:
                return future.result()
            reply = delegate_task.result()
            # C02 ports are allowed to be legacy/minimal adapters.  Bind any
            # omitted identity fields to the immutable request before the
            # registry sees the reply; a public Application response remains
            # strict and must provide the complete envelope itself.
            reply = replace(
                reply,
                params_digest=reply.params_digest or request.params_digest,
                session_id=reply.session_id or request.session_id,
                run_id=reply.run_id or request.run_id,
                tool_call_id=reply.tool_call_id if reply.tool_call_id is not None else request.tool_call_id,
                tool_name=reply.tool_name if reply.tool_name is not None else request.tool_name,
                tool_input=reply.tool_input if reply.tool_input is not None else request.tool_input,
                plan_id=reply.plan_id if reply.plan_id is not None else request.plan_id,
                plan_digest=reply.plan_digest if reply.plan_digest is not None else request.plan_digest,
                metadata=reply.metadata if reply.metadata is not None else request.metadata,
            )
            resolved = self.agent.interaction_registry.resolve(reply)
            self.application._complete_pending(request.request_id, resolved)
            if not future.done():
                future.set_result(resolved)
            return resolved
        except asyncio.CancelledError:
            self.application._complete_pending(request.request_id, None, error_code="cancelled")
            raise
        finally:
            self._futures.pop(request.request_id, None)
            self._delegates.pop(request.request_id, None)

    def resolve_external(self, reply: InteractionReply) -> InteractionReply:
        future = self._futures.get(reply.request_id)
        if future is None:
            raise InteractionBindingError("interaction request is not waiting in this process", code="interaction_interrupted")
        resolved = self.agent.interaction_registry.resolve(reply)
        self.application._complete_pending(reply.request_id, resolved)
        if not future.done():
            future.set_result(resolved)
        task = self._delegates.get(reply.request_id)
        if task is not None and not task.done():
            task.cancel()
        return resolved

    def cancel_pending(self) -> list[str]:
        ids: list[str] = []
        for request_id, future in list(self._futures.items()):
            if not future.done():
                reply = InteractionReply(request_id=request_id, approved=False, source="cancelled")
                future.set_result(reply)
                self.application._complete_pending(request_id, reply, error_code="cancelled")
                ids.append(request_id)
        return ids


class Application:
    """Unified process-local lifecycle API."""

    def __init__(
        self,
        context: ProjectContext,
        *,
        agent_factory: _AgentFactory | None = None,
        output_port: OutputPort | None = None,
        interaction_port: Any | None = None,
        provider_client: Any | None = None,
        agent_options: Mapping[str, Any] | None = None,
        existing_agent: Any | None = None,
        control_store: ControlStore | None = None,
    ) -> None:
        self.context = require_context(context)
        self.control = control_store or ControlStore(
            self.context.runtime_data_dir
            / "application"
            / self.context.workspace_id
            / "control.sqlite"
        )
        self._owns_control = control_store is None
        self.agent_factory = agent_factory
        self.output_port = output_port or NullOutputPort()
        self.interaction_port = interaction_port or _DenyingPort()
        self.provider_client = provider_client
        self.agent_options = dict(agent_options or {})
        self._existing_agent = existing_agent
        self._existing_agent_used = False
        self._stores: dict[str, SQLiteRuntimeStore] = {}
        self._agents: dict[str, Any] = {}
        self._interaction_bridges: dict[str, ApplicationInteractionPort] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._cancel_requested: set[str] = set()
        self._owner_lock: WorkspaceLock | None = None
        self.owner_id: str | None = None
        self.owner_generation = 0
        self._closed = False
        self._shutting_down = False
        self.control.mark_old_pending_interrupted()
        self._quarantine_dead_owners()

    # ---- session API -------------------------------------------------

    def session_create(self, session_id: str | None = None) -> ApplicationResponse:
        self._ensure_open()
        session_id = session_id or uuid.uuid4().hex[:12]
        _validate_session_id(session_id)
        path = runtime_store_path(session_id, context=self.context)
        self.control.register_session(
            workspace_id=self.context.workspace_id,
            session_id=session_id,
            canonical_path=path,
        )
        return ApplicationResponse(
            operation="session.create",
            status="created",
            result="ok",
            session_id=session_id,
            data={"workspace_id": self.context.workspace_id, "canonical_path": str(path)},
        )

    def session_list(self) -> ApplicationResponse:
        self._ensure_open()
        rows = self.control.list_sessions(self.context.workspace_id)
        sessions = [dict(row) for row in rows]
        known = {str(row["session_id"]) for row in rows}
        session_root = self.context.runtime_data_dir / "sessions"
        if session_root.exists():
            for database in sorted(session_root.glob("*/runtime.sqlite")):
                session_id = database.parent.name
                if session_id not in known:
                    sessions.append(
                        {
                            "session_id": session_id,
                            "workspace_id": None,
                            "canonical_path": str(database),
                            "status": "inspect_only",
                        }
                    )
        return ApplicationResponse(
            operation="session.list",
            status="ok",
            result="ok",
            data={"workspace_id": self.context.workspace_id, "sessions": sessions},
        )

    async def dispatch(self, operation: str, **payload: Any) -> ApplicationResponse:
        """Dispatch a dot-named command without exposing Agent internals."""

        routes: dict[str, Callable[..., Any]] = {
            "session.create": self.session_create,
            "session.list": self.session_list,
            "run.start": self.run_start,
            "run.status": self.run_status,
            "run.cancel": self.run_cancel,
            "run.resume": self.run_resume,
            "interaction.respond": self.interaction_respond,
            "owner.reconcile": self.owner_reconcile,
            "shutdown": self.shutdown,
        }
        handler = routes.get(operation)
        if handler is None:
            return ApplicationResponse(operation, "rejected", "error", error_code="unknown_operation")
        result = handler(**payload)
        if hasattr(result, "__await__"):
            result = await result
        return result

    # ---- run API -----------------------------------------------------

    async def run_start(
        self,
        *,
        session_id: str,
        prompt: str,
        command_id: str | None = None,
        params_digest_value: str | None = None,
        run_id: str | None = None,
        parent_run_id: str | None = None,
        decision_generation: int = 0,
    ) -> ApplicationResponse:
        self._ensure_open()
        try:
            self._ensure_session(session_id)
        except ApplicationError as exc:
            return ApplicationResponse(
                "run.start", "rejected", "error", session_id=session_id,
                error_code=exc.code,
            )
        command_id = command_id or f"cmd-{uuid.uuid4().hex}"
        requested_run_id = run_id
        computed_digest = full_sha256({"session_id": session_id, "prompt": prompt})
        if params_digest_value is not None and params_digest_value != computed_digest:
            return ApplicationResponse(
                "run.start", "rejected", "error", command_id=command_id,
                session_id=session_id, error_code="digest_conflict",
                data={"expected_params_digest": computed_digest},
            )
        digest = computed_digest
        envelope = CommandEnvelope(
            command_id=command_id,
            scope_type="session",
            scope_id=session_id,
            operation="run.start",
            params_digest=digest,
            session_id=session_id,
            run_id=run_id,
        )
        existing = self.control.get_command(envelope)
        if existing is not None:
            if existing["params_digest"] != digest:
                return self._error_response(envelope, "digest_conflict")
            stored = _response_from_json(existing["response_json"])
            if stored is not None:
                return stored
        run_id = requested_run_id or f"run-{uuid.uuid4().hex}"
        owner_response = self._acquire_root_owner()
        if owner_response is not None:
            # A second process may observe the owner lock just before the
            # first process commits its command row.  Give the durable row a
            # short chance to appear so identical cross-process commands
            # converge on one result instead of spuriously returning a race.
            if owner_response.error_code == "owner_conflict":
                for _ in range(50):
                    await asyncio.sleep(0.01)
                    existing = self.control.get_command(envelope)
                    if existing is not None:
                        if existing["params_digest"] != digest:
                            return self._error_response(envelope, "digest_conflict")
                        stored = _response_from_json(existing["response_json"])
                        if stored is not None:
                            return stored
            return self._error_response(envelope, owner_response.error_code or "owner_conflict", data=owner_response.data)
        assert self.owner_id is not None
        try:
            response = self.control.insert_run_and_command(
                envelope,
                run_id=run_id,
                workspace_id=self.context.workspace_id,
                owner_id=self.owner_id,
                prompt_digest=full_sha256({"prompt": prompt}),
                parent_run_id=parent_run_id,
                decision_generation=decision_generation,
            )
        except CommandConflictError:
            self._release_owner(status="released")
            return self._error_response(envelope, "digest_conflict")
        except Exception as exc:
            # A failed control commit is still before the dispatch barrier;
            # release the capability so a later root can retry safely.
            self._release_owner(status="released")
            return self._error_response(
                envelope,
                "control_commit_error",
                data={"error_type": type(exc).__name__},
            )
        if response.result != "accepted":
            return response
        # The control commit above is the dispatch barrier.  Nothing that can
        # call a provider is constructed before it succeeds.
        execution = self._execute_run(response.run_id or run_id, session_id, prompt)
        try:
            task = asyncio.create_task(execution)
        except Exception as exc:
            execution.close()
            self.control.update_run(run_id, status="interrupted", error_code="run_dispatch_not_observed")
            self._release_owner(status="released")
            failed = ApplicationResponse(
                "run.start", "interrupted", "error", command_id=command_id,
                session_id=session_id, run_id=run_id,
                error_code="run_dispatch_not_observed",
                data={"error_type": type(exc).__name__},
            )
            self.control.update_command_for_run(run_id, failed)
            return failed
        self._tasks[run_id] = task
        return response

    async def wait_run(self, run_id: str) -> ApplicationResponse:
        task = self._tasks.get(run_id)
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        return self.run_status(run_id)

    def run_status(self, run_id: str) -> ApplicationResponse:
        self._ensure_open()
        row = self.control.run(run_id)
        if row is None:
            return ApplicationResponse("run.status", "missing", "error", run_id=run_id, error_code="run_not_found")
        data: dict[str, Any] = {
            "owner_id": row["owner_id"],
            "error_code": row["error_code"],
            "dispatch_intent": bool(row["dispatch_intent"]),
            "decision_generation": int(row["decision_generation"]),
            "pending_interactions": [dict(item) for item in self.control.pending_for_run(run_id)],
        }
        if row["result_json"]:
            with contextlib.suppress(json.JSONDecodeError):
                data["result"] = json.loads(row["result_json"])
        if row["canonical_correlation_json"]:
            with contextlib.suppress(json.JSONDecodeError):
                data["canonical_correlation"] = json.loads(row["canonical_correlation_json"])
        return ApplicationResponse(
            operation="run.status",
            status=row["status"],
            result="ok",
            session_id=row["session_id"],
            run_id=run_id,
            error_code=row["error_code"],
            data=data,
        )

    async def run_cancel(self, *, run_id: str, command_id: str | None = None) -> ApplicationResponse:
        self._ensure_open()
        command_id = command_id or f"cancel-{uuid.uuid4().hex}"
        row = self.control.run(run_id)
        if row is None:
            return ApplicationResponse("run.cancel", "missing", "error", run_id=run_id, error_code="run_not_found")
        if row["status"] in {"succeeded", "failed", "cancelled", "interrupted", "uncertain"}:
            return ApplicationResponse("run.cancel", row["status"], "ok", command_id=command_id, session_id=row["session_id"], run_id=run_id, error_code=row["error_code"], data={"already_terminal": True})
        cancel_envelope = CommandEnvelope(
            command_id=command_id,
            scope_type="run",
            scope_id=run_id,
            operation="run.cancel",
            params_digest=full_sha256({"run_id": run_id}),
            session_id=row["session_id"],
            run_id=run_id,
        )
        existing_command = self.control.get_command(cancel_envelope)
        if existing_command is not None:
            if existing_command["params_digest"] != cancel_envelope.params_digest:
                return self._error_response(cancel_envelope, "digest_conflict")
            stored = _response_from_json(existing_command["response_json"])
            if stored is not None:
                return stored
        generation, created, existing = self.control.create_cancel(run_id, command_id)
        if not created:
            status = existing["status"] if existing is not None else "cancelling"
            response = ApplicationResponse("run.cancel", status, "ok", command_id=command_id, session_id=row["session_id"], run_id=run_id, data={"cancel_generation": generation})
            return self.control.record_command(cancel_envelope, response)
        self._cancel_requested.add(run_id)
        self.control.update_run(run_id, status="cancelling", error_code="cancel_propagation_unconfirmed")
        agent = self._agents.get(run_id)
        if agent is not None:
            cancel_pending = getattr(agent, "cancel_pending_interactions", None)
            if callable(cancel_pending):
                cancel_pending()
            abort = getattr(agent, "abort", None)
            if callable(abort):
                abort()
        response = ApplicationResponse("run.cancel", "cancelling", "accepted", command_id=command_id, session_id=row["session_id"], run_id=run_id, data={"cancel_generation": generation})
        return self.control.record_command(cancel_envelope, response)

    async def run_resume(self, *, run_id: str, prompt: str, command_id: str | None = None) -> ApplicationResponse:
        self._ensure_open()
        row = self.control.run(run_id)
        if row is None:
            return ApplicationResponse("run.resume", "missing", "error", run_id=run_id, error_code="run_not_found")
        if row["status"] not in {"interrupted", "uncertain"}:
            return ApplicationResponse("run.resume", row["status"], "error", session_id=row["session_id"], run_id=run_id, error_code="resume_not_allowed")
        generation = int(row["decision_generation"]) + 1
        return await self.run_start(
            session_id=row["session_id"],
            prompt=prompt,
            command_id=command_id or f"resume-{uuid.uuid4().hex}",
            parent_run_id=run_id,
            decision_generation=generation,
        )

    # ---- interaction API --------------------------------------------

    async def interaction_respond(self, reply: InteractionReply | None = None, **fields: Any) -> ApplicationResponse:
        self._ensure_open()
        if reply is None:
            try:
                reply = InteractionReply(**fields)
            except (TypeError, ValueError) as exc:
                return ApplicationResponse(
                    "interaction.respond", "rejected", "error",
                    request_id=fields.get("request_id"),
                    error_code="interaction_binding_error",
                    data={"error_type": type(exc).__name__},
                )
        request_row = self.control.pending(reply.request_id)
        if request_row is None:
            return ApplicationResponse("interaction.respond", "rejected", "error", request_id=reply.request_id, error_code="interaction_interrupted")
        if request_row["status"] in {"interrupted", "expired", "cancelled", "resolved"}:
            return ApplicationResponse(
                "interaction.respond", "rejected", "error", request_id=reply.request_id,
                run_id=request_row["run_id"],
                error_code="interaction_expired" if request_row["status"] == "expired" else "interaction_interrupted",
            )
        if not self._reply_matches_row(reply, request_row):
            return ApplicationResponse("interaction.respond", "rejected", "error", request_id=reply.request_id, run_id=request_row["run_id"], error_code="interaction_binding_error")
        bridge = self._interaction_bridges.get(reply.request_id)
        if bridge is None:
            return ApplicationResponse("interaction.respond", "rejected", "error", request_id=reply.request_id, run_id=request_row["run_id"], error_code="interaction_interrupted")
        try:
            resolved = bridge.resolve_external(reply)
        except InteractionError:
            return ApplicationResponse("interaction.respond", "rejected", "error", request_id=reply.request_id, run_id=request_row["run_id"], error_code="interaction_binding_error")
        return ApplicationResponse("interaction.respond", "resolved", "ok", request_id=reply.request_id, session_id=request_row["session_id"], run_id=request_row["run_id"], data={"approved": bool(resolved.approved)})

    # ---- owner and shutdown -----------------------------------------

    def owner_reconcile(self, *, owner_id: str, generation: int, action: str, evidence: Mapping[str, Any]) -> ApplicationResponse:
        self._ensure_open()
        if action not in {"inspect", "terminate", "release"}:
            return ApplicationResponse("owner.reconcile", "rejected", "error", error_code="invalid_reconcile_action")
        row = self.control.owner(owner_id)
        if row is None or row["workspace_id"] != self.context.workspace_id or int(row["generation"]) != generation:
            return ApplicationResponse("owner.reconcile", "rejected", "error", error_code="owner_identity_conflict")
        if action == "inspect":
            return ApplicationResponse("owner.reconcile", "inspected", "ok", data={"owner": dict(row)})
        if not isinstance(evidence, Mapping) or not evidence:
            return ApplicationResponse("owner.reconcile", "rejected", "error", error_code="owner_evidence_required")
        # A live owner is a capability held by its owning Application.  A
        # different Application must not release or terminate it merely by
        # presenting arbitrary evidence; the physical workspace lock remains
        # held and the durable row must stay active.  Reconciliation is only
        # available once the recorded process has died (or the owner is
        # already quarantined).
        if (
            row["status"] == "active"
            and not int(row["quarantine"])
            and owner_id != self.owner_id
            and _process_alive(int(row["process_id"]))
        ):
            return ApplicationResponse("owner.reconcile", "rejected", "error", error_code="owner_foreign_active", data={"owner_id": owner_id})
        active_runs = [run for run in self.control.runs_for_owner(owner_id) if run["status"] in {"queued", "running", "waiting_interaction", "cancelling"}]
        if action == "terminate":
            for run in active_runs:
                self._cancel_requested.add(str(run["run_id"]))
                self.control.update_run(str(run["run_id"]), status="cancelling", error_code="cancel_propagation_unconfirmed")
                agent = self._agents.get(str(run["run_id"]))
                abort = getattr(agent, "abort", None) if agent is not None else None
                if callable(abort):
                    abort()
            if active_runs:
                return ApplicationResponse("owner.reconcile", "terminating", "ok", data={"owner_id": owner_id, "active_runs": [run["run_id"] for run in active_runs]})
        next_status = "terminated" if action == "terminate" else "released"
        self.control.update_owner(owner_id, status=next_status, quarantine=0, evidence_json=json.dumps(dict(evidence), sort_keys=True))
        if owner_id == self.owner_id and self._owner_lock is not None:
            self._owner_lock.release()
            self._owner_lock = None
            self.owner_id = None
        return ApplicationResponse("owner.reconcile", next_status, "ok", data={"owner_id": owner_id, "action": action})

    async def shutdown(self, *, timeout: float = 5.0) -> ApplicationResponse:
        if self._closed:
            return ApplicationResponse("shutdown", "shutdown_complete", "ok")
        self._shutting_down = True
        pending = [task for task in self._tasks.values() if not task.done()]
        for run_id in list(self._tasks):
            if not self._tasks[run_id].done():
                await self.run_cancel(run_id=run_id, command_id=f"shutdown-cancel-{uuid.uuid4().hex}")
        if pending:
            done, still = await asyncio.wait(pending, timeout=max(0.0, timeout))
            if still:
                return ApplicationResponse("shutdown", "shutdown_incomplete", "error", error_code="shutdown_incomplete", data={"active_runs": [run_id for run_id, task in self._tasks.items() if not task.done()]})
        agents = list(self._agents.values())
        if self._existing_agent is not None:
            agents.append(self._existing_agent)
        seen_agents: set[int] = set()
        for agent in agents:
            if id(agent) in seen_agents:
                continue
            seen_agents.add(id(agent))
            close = getattr(agent, "aclose", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()
        for store in list(self._stores.values()):
            with contextlib.suppress(Exception):
                store.close()
        self._stores.clear()
        self._release_owner(status="released")
        if self._owns_control:
            self.control.close()
        self._closed = True
        return ApplicationResponse("shutdown", "shutdown_complete", "ok")

    async def aclose(self) -> ApplicationResponse:
        return await self.shutdown()

    # ---- internals ---------------------------------------------------

    def _ensure_open(self) -> None:
        if self._closed:
            raise ApplicationClosedError("Application is closed")

    def _ensure_session(self, session_id: str) -> None:
        _validate_session_id(session_id)
        rows = self.control.list_sessions(self.context.workspace_id)
        if not any(row["session_id"] == session_id for row in rows):
            canonical_path = runtime_store_path(session_id, context=self.context)
            if canonical_path.exists():
                raise ApplicationError("session is inspect-only", code="inspect_only")
            self.control.register_session(
                workspace_id=self.context.workspace_id,
                session_id=session_id,
                canonical_path=canonical_path,
            )

    def _acquire_root_owner(self) -> ApplicationResponse | None:
        if self._owner_lock is not None and self._owner_lock.held:
            if any(not task.done() for task in self._tasks.values()):
                return ApplicationResponse("owner", "rejected", "error", error_code="owner_conflict", data={"owner_id": self.owner_id})
            # Holding this root's own capability is not enough to start more
            # work: a quarantined row still blocks the workspace, so re-check
            # rather than returning early.
            blocked = self._quarantine_response(exclude_owner=self.owner_id)
            if blocked is not None:
                return blocked
            return None
        blocked = self._quarantine_response()
        if blocked is not None:
            return blocked
        owner_id = f"owner-{uuid.uuid4().hex}"
        lock_path = self.context.runtime_data_dir / "application" / self.context.workspace_id / "locks" / f"{workspace_lock_key(self.context.workspace_id)}.lock"
        lock = WorkspaceLock(lock_path, workspace_id=self.context.workspace_id, owner_id=owner_id)
        try:
            lock.acquire()
        except WorkspaceLockBusyError:
            return ApplicationResponse("owner", "rejected", "error", error_code="owner_conflict")
        try:
            self.owner_generation += 1
            self.control.insert_owner(owner_id=owner_id, workspace_id=self.context.workspace_id, generation=self.owner_generation, lock_key=lock.key)
        except Exception:
            lock.release()
            raise
        self._owner_lock = lock
        self.owner_id = owner_id
        return None

    def _quarantine_response(self, *, exclude_owner: str | None = None) -> ApplicationResponse | None:
        """Return a refusal if any other owner row blocks this workspace.

        A quarantined owner is not adoptable by a later root regardless of the
        status label it carries: ``_quarantine_dead_owners`` moves a crashed root
        to ``uncertain``, and if that label alone released the workspace the
        crash would silently hand over ownership.  Only an explicit
        ``owner.reconcile`` clears the quarantine flag.
        """

        for stale in self.control.owner_rows(self.context.workspace_id):
            if exclude_owner is not None and stale["owner_id"] == exclude_owner:
                continue
            if int(stale["quarantine"]):
                return ApplicationResponse("owner", "rejected", "error", error_code="owner_quarantine", data={"owner_id": stale["owner_id"], "generation": stale["generation"]})
            if stale["status"] == "active" and _process_alive(int(stale["process_id"])):
                return ApplicationResponse("owner", "rejected", "error", error_code="owner_conflict", data={"owner_id": stale["owner_id"]})
            if stale["status"] == "active":
                self.control.update_owner(stale["owner_id"], status="uncertain", quarantine=1, evidence_json=json.dumps({"reason": "root_process_missing"}, sort_keys=True))
                return ApplicationResponse("owner", "rejected", "error", error_code="owner_quarantine", data={"owner_id": stale["owner_id"], "generation": stale["generation"]})
        return None

    def _release_owner(self, *, status: str) -> None:
        if self.owner_id is not None:
            with contextlib.suppress(Exception):
                self.control.update_owner(self.owner_id, status=status, quarantine=0)
        if self._owner_lock is not None:
            self._owner_lock.release()
            self._owner_lock = None
        self.owner_id = None

    def _quarantine_dead_owners(self) -> None:
        for row in self.control.owner_rows(self.context.workspace_id):
            if row["status"] == "active" and not _process_alive(int(row["process_id"])):
                self.control.update_owner(row["owner_id"], status="uncertain", quarantine=1, evidence_json=json.dumps({"reason": "root_process_missing"}, sort_keys=True))
                for run in self.control.runs_for_owner(row["owner_id"]):
                    if run["status"] in {"queued", "running", "waiting_interaction", "cancelling"}:
                        if not run["dispatch_intent"]:
                            self.control.update_run(run["run_id"], status="interrupted", error_code="run_interrupted_before_dispatch")
                        else:
                            projection = self._project_recovered_run(run)
                            self.control.update_run(
                                run["run_id"],
                                status=projection.status,
                                error_code=projection.error_code,
                                result={
                                    "canonical_correlation": dict(projection.correlation),
                                    "side_effect_count": projection.side_effect_count,
                                },
                            )

    def _project_recovered_run(self, run: sqlite3.Row) -> CanonicalProjection:
        """Read canonical facts only; never infer a dispatch from control intent."""

        path = runtime_store_path(str(run["session_id"]), context=self.context)
        if not path.exists():
            return project_canonical_evidence(
                [], session_id=str(run["session_id"]), run_id=str(run["run_id"])
            )
        try:
            store = SQLiteRuntimeStore(path)
            try:
                events = store.read_events(
                    session_id=str(run["session_id"]),
                    run_id=str(run["run_id"]),
                )
            finally:
                store.close()
            projection = project_canonical_evidence(
                events,
                session_id=str(run["session_id"]),
                run_id=str(run["run_id"]),
            )
            self._persist_projection_tool_operations(str(run["run_id"]), projection)
            return projection
        except Exception:
            return CanonicalProjection(
                "uncertain",
                "control_canonical_conflict",
                None,
                {"session_id": run["session_id"], "run_id": run["run_id"], "tool_operations": []},
                0,
            )

    def _persist_projection_tool_operations(self, run_id: str, projection: CanonicalProjection) -> None:
        """Materialize canonical tool identities in the C03 control ledger.

        The event store remains the source of truth.  This table is a durable
        correlation/index used for idempotency and recovery reporting, so an
        operation is recorded only after projection has validated its complete
        identity.  Replaying the same projection is idempotent.
        """

        for operation in projection.correlation.get("tool_operations", []):
            self.control.record_tool_operation(
                run_id=run_id,
                operation_id=str(operation["operation_id"]),
                provider_tool_call_id=str(operation["provider_tool_call_id"]),
                tool_name=str(operation["tool_name"]),
                canonical_args_hash=str(operation["canonical_args_hash"]),
                invocation_id=operation.get("invocation_id"),
                turn_id=operation.get("turn_id"),
                state="completed" if projection.status == "succeeded" else "dispatched",
            )

    async def _execute_run(self, run_id: str, session_id: str, prompt: str) -> None:
        self.control.update_run(run_id, status="running", dispatch_intent=True)
        store = self._stores.get(session_id)
        if store is None and self._existing_agent is not None and getattr(self._existing_agent, "_runtime_store", None) is not None:
            store = self._existing_agent._runtime_store
            self._stores[session_id] = store
        if store is None:
            store = SQLiteRuntimeStore(runtime_store_path(session_id, context=self.context))
            self._stores[session_id] = store
        bridge: ApplicationInteractionPort | None = None
        agent = None
        try:
            factory = self.agent_factory
            if self._existing_agent is not None:
                agent = self._existing_agent
                factory = None
            if factory is None:
                if agent is None:
                    from .agent import Agent

                    factory = Agent
            if agent is None:
                options = dict(self.agent_options)
                options.update(
                    project_context=self.context,
                    runtime_store=store,
                    runtime_session_id=session_id,
                    runtime_run_id=run_id,
                    output_port=self.output_port,
                    provider_client=self.provider_client,
                )
                # Agent creates its InteractionRegistry before the bridge is used.
                agent = factory(**options)
            configure_identity = getattr(agent, "configure_runtime_identity", None)
            if callable(configure_identity):
                configure_identity(session_id=session_id, run_id=run_id)
            configure_store = getattr(agent, "configure_runtime_store", None)
            if callable(configure_store) and store is not None and getattr(agent, "_runtime_store", None) is None:
                configure_store(store)
            bridge = ApplicationInteractionPort(self, agent, self.interaction_port)
            self._agents[run_id] = agent
            self._interaction_bridges_by_agent(agent, bridge)
            configure = getattr(agent, "configure_application_interactions", None)
            if callable(configure):
                configure(True)
            if hasattr(agent, "set_interaction_port"):
                agent.set_interaction_port(bridge)
            await agent.chat(prompt)
            control_row = self.control.run(run_id)
            # A restart/recovery owner may have classified this run while the
            # old task was still unwinding.  Never let that stale task
            # overwrite an ``interrupted``/``uncertain`` decision with a
            # locally inferred success.
            if control_row is not None and control_row["status"] in {"interrupted", "uncertain"}:
                return
            canonical_events = store.read_events(session_id=session_id, run_id=run_id)
            if canonical_events:
                projection = project_canonical_evidence(
                    canonical_events, session_id=session_id, run_id=run_id,
                    side_effect_count=len(self.control.tool_operations(run_id)),
                )
                try:
                    self._persist_projection_tool_operations(run_id, projection)
                except ApplicationError as exc:
                    projection = CanonicalProjection(
                        "uncertain", exc.code, None, projection.correlation,
                        max(projection.side_effect_count, len(self.control.tool_operations(run_id))),
                    )
                self.control.update_correlation(run_id, projection.correlation)
                self.control.finalize_run(
                    run_id,
                    status=projection.status,
                    error_code=projection.error_code,
                    result={
                        "side_effect_count": projection.side_effect_count,
                    } if projection.status == "uncertain" else {
                        "completed": projection.status == "succeeded",
                    },
                )
            else:
                self.control.finalize_run(
                    run_id,
                    status="succeeded",
                    error_code=None,
                    result={"completed": True},
                )
        except asyncio.CancelledError:
            self.control.update_run(run_id, status="cancelled" if run_id in self._cancel_requested else "interrupted", error_code="cancelled" if run_id in self._cancel_requested else "run_interrupted_before_dispatch")
            if run_id in self._cancel_requested:
                self.control.update_cancel(run_id, status="cancelled", error_code="cancelled")
            raise
        except Exception as exc:
            self.control.update_run(run_id, status="cancelled" if run_id in self._cancel_requested else "failed", error_code="cancelled" if run_id in self._cancel_requested else "provider_error", result={"error": type(exc).__name__})
        finally:
            if bridge is not None:
                for request_id in list(self._interaction_bridges):
                    if self._interaction_bridges[request_id] is bridge:
                        self._interaction_bridges.pop(request_id, None)
            self._agents.pop(run_id, None)
            if agent is not None and agent is not self._existing_agent:
                close = getattr(agent, "aclose", None)
                if close is not None:
                    with contextlib.suppress(Exception):
                        await close()
            status_row = self.control.run(run_id)
            if status_row is not None:
                response = ApplicationResponse("run.start", status_row["status"], "ok" if status_row["status"] in {"succeeded", "failed", "cancelled"} else "error", command_id=status_row["command_id"], session_id=session_id, run_id=run_id, error_code=status_row["error_code"], data={"dispatch_intent": bool(status_row["dispatch_intent"])})
                self.control.update_command_for_run(run_id, response)
            self._tasks.pop(run_id, None)
            if not any(not task.done() for task in self._tasks.values()):
                self._release_owner(status="released")

    def _interaction_bridges_by_agent(self, agent: Any, bridge: ApplicationInteractionPort) -> None:
        # A request is added lazily when C02 calls bridge.request.  Keeping a
        # weak reverse association is unnecessary; the application resolves by
        # request id in _interaction_bridges.
        setattr(bridge, "_agent", agent)

    def _register_pending(self, request: InteractionRequest, agent: Any) -> None:
        bridge = getattr(agent, "interaction_port", None)
        if isinstance(bridge, ApplicationInteractionPort):
            self._interaction_bridges[request.request_id] = bridge
        self.control.insert_pending(request, workspace_id=self.context.workspace_id, process_id=os.getpid())
        row = self.control.run(request.run_id)
        if row is not None:
            self.control.update_run(request.run_id, status="waiting_interaction")

    def _complete_pending(self, request_id: str, reply: InteractionReply | None, error_code: str | None = None) -> None:
        self.control.complete_pending(request_id, status="resolved" if reply is not None and error_code is None else "cancelled", reply=reply, error_code=error_code)
        row = self.control.pending(request_id)
        if row is not None:
            run = self.control.run(row["run_id"])
            if run is not None and run["status"] == "waiting_interaction":
                self.control.update_run(row["run_id"], status="running")

    @staticmethod
    def _reply_matches_row(reply: InteractionReply, row: sqlite3.Row) -> bool:
        for field in ("session_id", "run_id", "tool_call_id", "tool_name"):
            if getattr(reply, field, None) != row[field]:
                return False
        stored_input = json.loads(row["tool_input_json"]) if row["tool_input_json"] is not None else None
        if getattr(reply, "tool_input", None) != stored_input:
            # Sensitive fields are deliberately not persisted in control.sqlite;
            # the full params_digest remains the binding oracle for them.
            if not _contains_redaction(stored_input):
                return False
        if reply.params_digest != row["params_digest"]:
            return False
        if getattr(reply, "plan_id", None) != row["plan_id"]:
            return False
        if getattr(reply, "plan_digest", None) != row["plan_digest"]:
            return False
        stored_metadata = json.loads(row["metadata_json"]) if row["metadata_json"] is not None else None
        if getattr(reply, "metadata", None) != stored_metadata:
            return False
        return True
    @staticmethod
    def _error_response(envelope: CommandEnvelope, code: str, *, data: Mapping[str, Any] | None = None) -> ApplicationResponse:
        return ApplicationResponse(envelope.operation, "rejected", "error", command_id=envelope.command_id, session_id=envelope.session_id, run_id=envelope.run_id, error_code=code, data=data or {})


def _contains_redaction(value: Any) -> bool:
    if value == "[REDACTED]":
        return True
    if isinstance(value, Mapping):
        return any(_contains_redaction(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_redaction(item) for item in value)
    return False


class _DenyingPort:
    async def request(self, request: InteractionRequest) -> InteractionReply:
        return InteractionReply(
            request_id=request.request_id,
            approved=False,
            params_digest=request.params_digest,
            source="application-denying",
            session_id=request.session_id,
            run_id=request.run_id,
            tool_call_id=request.tool_call_id,
            tool_name=request.tool_name,
            plan_id=getattr(request, "plan_id", None),
            plan_digest=getattr(request, "plan_digest", None),
            metadata=getattr(request, "metadata", None),
        )


def _validate_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or not session_id or Path(session_id).name != session_id or session_id in {".", ".."}:
        raise ValueError("session_id must be a safe path component")


def _process_alive(pid: int) -> bool:
    """Report whether the recorded process is still running.

    ``os.kill(pid, 0)`` is not a liveness test on Windows.  CPython only
    special-cases ``sig == signal.CTRL_C_EVENT`` / ``CTRL_BREAK_EVENT``, and
    ``signal.CTRL_C_EVENT == 0``, so ``os.kill(pid, 0)`` does not probe at all —
    it calls ``GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)``.  That both fails
    to report a crashed root as dead (its owner row would never be quarantined)
    and sprays CTRL+C into the caller's console process group.  Query the exit
    code instead.
    """

    pid = int(pid)
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        return _windows_process_alive(pid)
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _windows_process_alive(pid: int) -> bool:
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)
