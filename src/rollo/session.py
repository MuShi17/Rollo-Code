"""Session 管理 — JSON 文件持久化会话历史记录。"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

def runtime_data_dir() -> Path:
    """Return the directory for canonical runtime data.

    Harbor can point this at its mounted agent-log directory so canonical
    events and their derived snapshots survive an outer agent timeout.
    """

    configured = os.environ.get("ROLLO_RUNTIME_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".rollo"


SESSION_DIR = runtime_data_dir() / "sessions"
SESSION_SNAPSHOT_VERSION = 2


class CanonicalRecoveryError(RuntimeError):
    """A canonical session exists but cannot be safely opened or projected."""

    code = "canonical_recovery_error"
    classification = "corrupt"

    def __init__(self, message: str, *, path: Path | None = None) -> None:
        self.path = path
        suffix = f" ({path})" if path is not None else ""
        super().__init__(message + suffix)


def _ensure_dir() -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)


def _session_dir(session_id: str, *, context: Any | None = None, runtime_root: Path | None = None) -> Path:
    if context is not None:
        base = Path(context.runtime_data_dir) / "sessions"
    elif runtime_root is not None:
        base = Path(runtime_root) / "sessions"
    else:
        base = SESSION_DIR
    return base / session_id


def runtime_store_path(
    session_id: str,
    *,
    context: Any | None = None,
    runtime_root: Path | None = None,
) -> Path:
    """Return the canonical store path isolated to one session directory."""

    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id must be a non-empty string")
    if Path(session_id).name != session_id or session_id in {".", ".."}:
        raise ValueError("session_id must be a single safe path component")
    return _session_dir(session_id, context=context, runtime_root=runtime_root) / "runtime.sqlite"


def list_runtime_store_paths(*, context: Any | None = None, runtime_root: Path | None = None) -> list[Path]:
    """List only session-scoped canonical stores.

    A root-level database may still exist from an older installation, but it
    is deliberately outside the application's discovery boundary.  Files from
    older formats remain untouched and are not candidates for list/latest/resume.
    """

    session_root = _session_dir("_placeholder", context=context, runtime_root=runtime_root).parent
    paths = [
        path for path in session_root.glob("*/runtime.sqlite") if path.is_file()
    ] if session_root.exists() else []
    return sorted(
        paths,
        key=lambda path: (path.stat().st_mtime_ns, str(path)),
        reverse=True,
    )


def _session_v2_path(session_id: str, *, context: Any | None = None, runtime_root: Path | None = None) -> Path:
    return _session_dir(session_id, context=context, runtime_root=runtime_root) / "session.v2.json"


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write a derived snapshot without damaging the prior snapshot."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    encoded = json.dumps(data, indent=2, ensure_ascii=False, default=str).encode("utf-8")
    try:
        with open(temporary, "xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def build_session_v2(
    session_id: str,
    runtime_store: Any,
    *,
    high_water: int | None = None,
    context: Any | None = None,
) -> dict[str, Any] | None:
    """Build a canonical-derived v2 snapshot from one immutable prefix."""

    from .projections.metrics_projection import CanonicalMetricsProjection
    from .projections.session_projection import SessionProjection

    projection = SessionProjection().build(
        runtime_store,
        session_id=session_id,
        high_water=high_water,
    )
    if projection.high_water == 0 and not projection.messages and not projection.runs:
        return None
    records = runtime_store.read_event_records(session_id=session_id, high_water=projection.high_water)
    metrics = CanonicalMetricsProjection().build(records, high_water=projection.high_water)
    ordinals = [ordinal for ordinal, _ in records]
    turns = sorted({event.turn_id for _, event in records})
    return {
        "schemaVersion": SESSION_SNAPSHOT_VERSION,
        "metadata": {
            "id": session_id,
            "source": "canonical",
            "snapshotVersion": SESSION_SNAPSHOT_VERSION,
            "projectionVersion": projection.projection_version,
            "highWater": projection.high_water,
            "sourceDigest": projection.source_digest,
            "projectionDigest": projection.digest,
            "messageCount": len(projection.messages),
        },
        "coverage": {
            "sessionId": session_id,
            "fromOrdinal": min(ordinals) if ordinals else 0,
            "toOrdinal": projection.high_water,
            "turnIds": turns,
        },
        "canonicalMessages": list(projection.messages),
        "runs": list(projection.runs),
        "errors": list(projection.errors),
        "terminals": list(projection.terminals),
        "partialCount": projection.partial_count,
        "diagnostics": [item.to_dict() for item in projection.diagnostics],
        "metrics": metrics.to_dict(),
    }


def save_session_v2(
    session_id: str,
    runtime_store: Any,
    *,
    high_water: int | None = None,
    context: Any | None = None,
) -> dict[str, Any] | None:
    snapshot = build_session_v2(session_id, runtime_store, high_water=high_water, context=context)
    if snapshot is not None:
        _ensure_dir()
        _atomic_write_json(_session_v2_path(session_id, context=context), snapshot)
    return snapshot


def load_canonical_session(session_id: str, runtime_store: Any) -> dict[str, Any] | None:
    """Read a session projection from canonical events without touching JSONL."""

    try:
        from .projections.session_projection import SessionProjection

        return build_session_v2(session_id, runtime_store)
    except Exception as error:
        # A damaged canonical source must be surfaced to the caller; it is
        # never interpreted as an empty session.
        raise CanonicalRecoveryError(
            f"canonical session projection failed: {error}",
            path=getattr(runtime_store, "database", None),
        ) from error


def load_session(
    session_id: str,
    *,
    runtime_store: Any | None = None,
    context: Any | None = None,
) -> dict[str, Any] | None:
    """Load a canonical-derived session view, rebuilding its cache as needed."""
    owned_store = None
    if runtime_store is None:
        database = runtime_store_path(session_id, context=context)
        if not database.is_file():
            return None
        from .runtime_store import SQLiteRuntimeStore

        try:
            owned_store = SQLiteRuntimeStore(database)
            runtime_store = owned_store
        except Exception as error:
            raise CanonicalRecoveryError(
                f"canonical session store failed to open: {error}", path=database
            ) from error

    try:
        canonical = load_canonical_session(session_id, runtime_store)
        if canonical is None:
            return None
        # The JSON file is a disposable display cache.  Rewriting it is safe
        # because the source boundary and digest are derived from the ledger.
        save_session_v2(session_id, runtime_store)
        return canonical
    finally:
        if owned_store is not None:
            owned_store.close()


def list_sessions(runtime_store: Any | None = None, *, context: Any | None = None) -> list[dict[str, Any]]:
    """List sessions represented by canonical SQLite ledgers only."""

    if runtime_store is not None:
        return list_canonical_sessions(runtime_store)
    return list_canonical_runtime_sessions(context=context)


def list_canonical_sessions(runtime_store: Any) -> list[dict[str, Any]]:
    """List sessions represented by canonical events only."""

    result: list[dict[str, Any]] = []
    for session_id in getattr(runtime_store, "list_session_ids", lambda: [])():
        snapshot = load_canonical_session(session_id, runtime_store)
        if snapshot is not None:
            result.append(dict(snapshot.get("metadata") or {}))
    return result


def list_canonical_runtime_sessions(*, context: Any | None = None) -> list[dict[str, Any]]:
    """Inspect all session stores for CLI list/latest without mutating them."""

    from .runtime_store import SQLiteRuntimeStore

    result: list[dict[str, Any]] = []
    for database in list_runtime_store_paths(context=context):
        store: Any | None = None
        try:
            store = SQLiteRuntimeStore(database)
            result.extend(list_canonical_sessions(store))
        except Exception as error:
            raise CanonicalRecoveryError(
                f"canonical session listing failed: {error}", path=database
            ) from error
        finally:
            if store is not None:
                store.close()
    return result


def get_latest_session_id(runtime_store: Any | None = None) -> str | None:
    sessions = list_canonical_sessions(runtime_store) if runtime_store is not None else list_sessions()
    if not sessions:
        return None
    sessions.sort(
        key=lambda s: (int(s.get("highWater", 0) or 0), s.get("startTime", ""), s.get("id", "")),
        reverse=True,
    )
    return sessions[0].get("id")


__all__ = [
    "SESSION_DIR",
    "SESSION_SNAPSHOT_VERSION",
    "CanonicalRecoveryError",
    "build_session_v2",
    "get_latest_session_id",
    "list_canonical_sessions",
    "list_canonical_runtime_sessions",
    "list_sessions",
    "list_runtime_store_paths",
    "load_canonical_session",
    "load_session",
    "save_session_v2",
    "runtime_data_dir",
    "runtime_store_path",
]
