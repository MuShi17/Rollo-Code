"""The process-local Application control plane for C03.

This module intentionally sits above the C02 canonical event store.  SQLite
control rows provide command idempotency, session exclusion and recovery
evidence; ``Agent`` and ``SQLiteRuntimeStore`` continue to own provider-neutral
runtime facts.  No public method consults Agent private lifecycle fields.

Execution exclusion is per *session*, never per workspace: each session owns its
own canonical store, so two sessions of one workspace share no write target.
``session_leases`` is the only mutex, ``runs(session_id, command_id)`` is the
command-level idempotency key, and ``runs.owner_pid`` is crash-recovery
attribution only - never a gate.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
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
    "InteractionBindingError",
    "RecoveryRequiredError",
    "ApplicationClosedError",
    "ApplicationResponse",
    "ControlStore",
    "ApplicationInteractionPort",
    "Application",
]

APPLICATION_SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical_json_bytes(value: Any) -> bytes:
    """Canonical JSON used by public Application digests.

    Sorting keys, preserving list order and removing insignificant whitespace
    gives a stable byte stream for the identity fields used by C03.  The
    implementation deliberately does not use ``default=str``: silently
    stringifying an input would make approval binding weaker.  ``allow_nan``
    stays off for the same reason: the v2 implementation rejected non-finite
    numbers outright, and emitting bare ``NaN``/``Infinity`` tokens would both
    produce invalid JSON in the control records and let a non-finite value into
    a digest that is supposed to be reproducible.
    """

    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


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


class InteractionBindingError(ApplicationError):
    code = "interaction_binding_error"


class RecoveryRequiredError(ApplicationError):
    code = "recovery_required"


class ApplicationClosedError(ApplicationError):
    code = "application_closed"


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

    def run_for_command(self, session_id: str, command_id: str) -> sqlite3.Row | None:
        """Read the durable row that makes ``(session_id, command_id)`` idempotent."""

        return self.connection.execute(
            "SELECT * FROM runs WHERE session_id=? AND command_id=?",
            (session_id, command_id),
        ).fetchone()

    def non_terminal_runs(self) -> list[sqlite3.Row]:
        """Runs a crashed process may have left mid-flight (recovery input only)."""

        return self.connection.execute(
            "SELECT * FROM runs WHERE status IN ('queued','running','waiting_interaction','cancelling') "
            "ORDER BY created_at,run_id",
        ).fetchall()

    def session_lease(self, session_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM session_leases WHERE session_id=?", (session_id,)
        ).fetchone()

    def acquire_session_lease(self, *, session_id: str, workspace_id: str, owner_id: str) -> str | None:
        """Take the session mutex; return the live holder's id when refused.

        A lease whose recorded process is gone is stale evidence rather than a
        lock, so the next process adopts it without operator help.  Only a lease
        held by a *live* foreign process refuses.
        """

        now = _utc_now()
        with self.transaction() as db:
            row = db.execute(
                "SELECT owner_id,pid FROM session_leases WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if row is not None:
                pid = int(row["pid"])
                if pid != os.getpid() and _process_alive(pid):
                    return str(row["owner_id"])
            db.execute(
                "INSERT INTO session_leases(session_id,workspace_id,owner_id,pid,acquired_at,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET workspace_id=excluded.workspace_id,"
                "owner_id=excluded.owner_id,pid=excluded.pid,acquired_at=excluded.acquired_at,"
                "updated_at=excluded.updated_at",
                (session_id, workspace_id, owner_id, os.getpid(), now, now),
            )
        return None

    def release_session_lease(self, session_id: str) -> None:
        """Release only a lease this process still owns.

        The ``pid`` guard keeps a stale release from deleting a newer holder's
        row after the session changed hands.
        """

        with self.transaction() as db:
            db.execute(
                "DELETE FROM session_leases WHERE session_id=? AND pid=?",
                (session_id, os.getpid()),
            )

    def bind_pending_command(self, request_id: str, command_id: str) -> None:
        """Bind the answering ``interaction.respond`` command to its row."""

        with self.transaction() as db:
            db.execute(
                "UPDATE pending_interactions SET command_id=?,updated_at=? WHERE request_id=?",
                (command_id, _utc_now(), request_id),
            )

    def insert_run(
        self,
        *,
        session_id: str,
        workspace_id: str,
        command_id: str,
        prompt_digest: str,
        run_id: str,
        response_json: str,
        parent_run_id: str | None = None,
        decision_generation: int = 0,
        owner_pid: int | None = None,
    ) -> sqlite3.Row:
        """Commit the run row that doubles as the command-level idempotency key.

        ``UNIQUE(session_id, command_id)`` is the durable gate: a retry of an
        already-committed command returns the existing row instead of raising or
        dispatching a second run.  ``response_json`` is the reply that retry
        converges on; it is written in the same transaction, so a committed row
        always has one.
        """

        now = _utc_now()
        with self.transaction() as db:
            existing = db.execute(
                "SELECT * FROM runs WHERE session_id=? AND command_id=?",
                (session_id, command_id),
            ).fetchone()
            if existing is not None:
                return existing
            db.execute(
                "INSERT INTO runs(run_id,session_id,workspace_id,command_id,owner_pid,parent_run_id,decision_generation,status,error_code,prompt_digest,response_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, session_id, workspace_id, command_id, os.getpid() if owner_pid is None else int(owner_pid), parent_run_id, decision_generation, "queued", None, prompt_digest, response_json, now, now),
            )
            created = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return created

    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        error_code: str | None = None,
        result: Mapping[str, Any] | None = None,
        dispatch_intent: bool | None = None,
        response_json: str | None = None,
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
        if response_json is not None:
            fields["response_json"] = response_json
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

    def insert_pending(self, request: InteractionRequest, *, workspace_id: str, process_id: int) -> None:
        now = _utc_now()
        tool_input = getattr(request, "tool_input", None)
        metadata = getattr(request, "metadata", None)
        with self.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO pending_interactions(request_id,workspace_id,session_id,run_id,tool_call_id,tool_name,tool_input_json,plan_id,plan_digest,metadata_json,params_digest,command_id,prompt,expires_at,process_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (request.request_id, workspace_id, request.session_id, request.run_id, request.tool_call_id, request.tool_name, json.dumps(_redact_control_value(tool_input), ensure_ascii=False, sort_keys=True) if tool_input is not None else None, getattr(request, "plan_id", None), getattr(request, "plan_digest", None), json.dumps(_redact_control_value(metadata), ensure_ascii=False, sort_keys=True) if metadata is not None else None, request.params_digest, getattr(request, "command_id", None), request.prompt, getattr(request, "expires_at_utc", None) or _expires_at_iso(request.expires_at), process_id, "pending", now, now),
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
        # Sessions this process currently holds a lease for, plus the reverse
        # run -> session map used to release only after the last run is done.
        self._session_leases: set[str] = set()
        self._run_sessions: dict[str, str] = {}
        self._lease_owner_id = f"owner-{uuid.uuid4().hex}"
        self._closed = False
        self._shutting_down = False
        self.control.mark_old_pending_interrupted()
        self._recover_orphaned_runs()

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
        prompt_digest = full_sha256({"prompt": prompt})
        run_id = requested_run_id or f"run-{uuid.uuid4().hex}"
        # 1. Command-level idempotency: a retry of a committed command returns
        #    the durable reply instead of dispatching a second run.
        existing = self.control.run_for_command(session_id, command_id)
        if existing is not None:
            return self._stored_run_response(existing, command_id=command_id, prompt_digest=prompt_digest)
        # 2. The session lease is the only execution mutex.  A live foreign
        #    holder is refused; a lease left behind by a dead process is adopted.
        if not self._own_session(session_id):
            holder = self.control.acquire_session_lease(
                session_id=session_id,
                workspace_id=self.context.workspace_id,
                owner_id=self._lease_owner_id,
            )
            if holder is not None:
                return ApplicationResponse(
                    "run.start", "rejected", "error",
                    command_id=command_id, session_id=session_id,
                    error_code="session_busy",
                    data={"session_id": session_id, "holder_owner_id": holder},
                )
            self._session_leases.add(session_id)
        queued = ApplicationResponse(
            "run.start", "queued", "accepted",
            command_id=command_id, session_id=session_id, run_id=run_id,
            data={"owner_pid": os.getpid(), "dispatch_intent": False},
        )
        # 3. Commit before dispatch.  Nothing that can construct an Agent or
        #    call a provider happens before this transaction succeeds.
        try:
            row = self.control.insert_run(
                session_id=session_id,
                workspace_id=self.context.workspace_id,
                command_id=command_id,
                prompt_digest=prompt_digest,
                run_id=run_id,
                response_json=_response_json(queued),
                parent_run_id=parent_run_id,
                decision_generation=decision_generation,
            )
        except Exception as exc:
            # A failed control commit is still before the dispatch barrier;
            # release the lease so a later attempt can retry safely.
            if not self._session_has_live_run(session_id):
                self._release_session_lease(session_id)
            return ApplicationResponse(
                "run.start", "rejected", "error",
                command_id=command_id, session_id=session_id,
                error_code="control_commit_error",
                data={"error_type": type(exc).__name__},
            )
        if str(row["run_id"]) != run_id:
            # Another client committed this command between the read above and
            # this insert; converge on its run instead of dispatching twice.
            # Dispatching nothing here also means the lease taken above must not
            # be kept when this process has no live run of that session.
            if not self._session_has_live_run(session_id):
                self._release_session_lease(session_id)
            return self._stored_run_response(row, command_id=command_id, prompt_digest=prompt_digest)
        execution = self._execute_run(run_id, session_id, prompt)
        try:
            task = asyncio.create_task(execution)
        except Exception as exc:
            execution.close()
            failed = ApplicationResponse(
                "run.start", "interrupted", "error", command_id=command_id,
                session_id=session_id, run_id=run_id,
                error_code="run_dispatch_not_observed",
                data={"error_type": type(exc).__name__},
            )
            self.control.update_run(
                run_id,
                status="interrupted",
                error_code="run_dispatch_not_observed",
                response_json=_response_json(failed),
            )
            if not self._session_has_live_run(session_id):
                self._release_session_lease(session_id)
            return failed
        self._tasks[run_id] = task
        self._run_sessions[run_id] = session_id
        return queued

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
            "owner_pid": int(row["owner_pid"]),
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
        generation, created, existing = self.control.create_cancel(run_id, command_id)
        if not created:
            # A repeated cancel of the same run is idempotent: the durable
            # cancel generation, not a command ledger, is the dedup key.
            status = existing["status"] if existing is not None else "cancelling"
            return ApplicationResponse("run.cancel", status, "ok", command_id=command_id, session_id=row["session_id"], run_id=run_id, data={"cancel_generation": generation})
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
        return ApplicationResponse("run.cancel", "cancelling", "accepted", command_id=command_id, session_id=row["session_id"], run_id=run_id, data={"cancel_generation": generation})

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

    async def interaction_respond(
        self,
        reply: InteractionReply | None = None,
        command_id: str | None = None,
        **fields: Any,
    ) -> ApplicationResponse:
        self._ensure_open()
        if command_id is None:
            command_id = fields.pop("command_id", None)
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
        if command_id is not None and request_row["command_id"] == command_id and request_row["status"] == "resolved":
            # 幂等：同一 command_id 的重放返回既有回复，不二次 resolve。
            # 行绑定校验仍然先行：重放载荷与持久行不符时一律按绑定错误拒绝。
            if not self._reply_matches_row(reply, request_row):
                return ApplicationResponse("interaction.respond", "rejected", "error", request_id=reply.request_id, run_id=request_row["run_id"], error_code="interaction_binding_error")
            return self._resolved_interaction_response(request_row, command_id)
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
        if command_id is not None:
            # Bind the answering command so a transport-level retry converges
            # instead of resolving the request a second time.
            self.control.bind_pending_command(reply.request_id, command_id)
        return ApplicationResponse("interaction.respond", "resolved", "ok", command_id=command_id, request_id=reply.request_id, session_id=request_row["session_id"], run_id=request_row["run_id"], data={"approved": bool(resolved.approved)})

    # ---- shutdown ----------------------------------------------------

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
        for session_id in list(self._session_leases):
            self._release_session_lease(session_id)
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

    def _own_session(self, session_id: str) -> bool:
        return session_id in self._session_leases

    def _release_session_lease(self, session_id: str) -> None:
        self._session_leases.discard(session_id)
        with contextlib.suppress(Exception):
            self.control.release_session_lease(session_id)

    def _session_has_live_run(self, session_id: str) -> bool:
        for run_id, run_session in self._run_sessions.items():
            if run_session != session_id:
                continue
            task = self._tasks.get(run_id)
            if task is not None and not task.done():
                return True
        return False

    @staticmethod
    def _stored_run_response(row: sqlite3.Row, *, command_id: str, prompt_digest: str) -> ApplicationResponse:
        """Return the committed reply for an already-committed command.

        The command identity is the idempotency key, so a replay returns the
        first reply even if the prompt text differs: v3 dropped the v2 rule that
        treated a differing parameter digest as a conflict, which is why there is
        no second command ledger to compare against.  ``prompt_digest`` is kept
        on the row as a diagnostic only.
        """

        stored = _response_from_json(row["response_json"])
        if stored is not None:
            return stored
        return ApplicationResponse(
            "run.start", str(row["status"]), "ok",
            command_id=command_id, session_id=row["session_id"], run_id=row["run_id"],
            error_code=row["error_code"],
            data={"dispatch_intent": bool(row["dispatch_intent"])},
        )

    @staticmethod
    def _resolved_interaction_response(row: sqlite3.Row, command_id: str) -> ApplicationResponse:
        approved = False
        if row["reply_json"]:
            with contextlib.suppress(json.JSONDecodeError):
                approved = bool(json.loads(row["reply_json"]).get("approved"))
        return ApplicationResponse(
            "interaction.respond", "resolved", "ok",
            command_id=command_id, request_id=row["request_id"],
            session_id=row["session_id"], run_id=row["run_id"],
            data={"approved": approved, "replayed": True},
        )

    def _recover_orphaned_runs(self) -> None:
        """Classify runs a crashed process left non-terminal.

        This is diagnosis, never a gate: nothing here refuses a new request or
        makes the workspace unavailable.  Rows owned by this process are
        skipped, which also removes any dependence on pid reuse.
        """

        for row in self.control.non_terminal_runs():
            owner_pid = int(row["owner_pid"])
            if owner_pid == os.getpid():
                continue
            if owner_pid and _process_alive(owner_pid):
                continue
            if not row["dispatch_intent"]:
                self.control.update_run(
                    str(row["run_id"]),
                    status="interrupted",
                    error_code="run_interrupted_before_dispatch",
                )
                continue
            projection = self._project_recovered_run(row)
            self.control.update_run(
                str(row["run_id"]),
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
            return projection
        except Exception:
            return CanonicalProjection(
                "uncertain",
                "control_canonical_conflict",
                None,
                {"session_id": run["session_id"], "run_id": run["run_id"], "tool_operations": []},
                0,
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
            # The recovery scan may have classified this run while this task
            # was still unwinding.  Never let the stale task overwrite an
            # ``interrupted``/``uncertain`` decision with a locally inferred
            # success.
            if control_row is not None and control_row["status"] in {"interrupted", "uncertain"}:
                return
            canonical_events = store.read_events(session_id=session_id, run_id=run_id)
            if canonical_events:
                # The canonical ledger is the only side-effect record now; the
                # control store keeps no parallel tool-operation tally.
                projection = project_canonical_evidence(
                    canonical_events, session_id=session_id, run_id=run_id,
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
                # ``dispatch_intent`` was already observed when this task
                # started, yet the canonical ledger holds nothing about the run.
                # D14 maps exactly that state to
                # ``interrupted``/``run_dispatch_not_observed`` — and the
                # recovery path in this module already classifies it that way,
                # so claiming success here would both contradict recovery and
                # report a run as successful with no evidence for it.
                self.control.finalize_run(
                    run_id,
                    status="interrupted",
                    error_code="run_dispatch_not_observed",
                    result={"side_effect_count": 0},
                )
        except asyncio.CancelledError:
            # Nothing in the runtime cancels a run task: ``run_cancel`` signals
            # the agent and lets the task finish, and the resulting terminal is
            # resolved from the ledger above.  This branch therefore only fires
            # when the surrounding event loop is torn down mid-run, so the task
            # is interrupted rather than cancelled -- reporting ``cancelled``
            # here would claim a cancellation that was never requested.
            self.control.update_run(
                run_id,
                status="interrupted",
                error_code="run_interrupted_before_dispatch",
            )
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
                self.control.update_run(run_id, response_json=_response_json(response))
            self._tasks.pop(run_id, None)
            self._run_sessions.pop(run_id, None)
            # Only this run's own session may be released here; other sessions
            # keep their own leases (a GUI holds several at once).
            if not self._session_has_live_run(session_id):
                self._release_session_lease(session_id)

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
