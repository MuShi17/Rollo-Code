"""C03 — platform and schema oracles that the first review round found unguarded.

Two mutants survived the whole suite before this file existed:

* ``_windows_process_alive`` replaced by ``return False`` — every existing case
  short-circuits on ``pid == os.getpid()`` and never reaches the Windows API;
* dropping the durable idempotency/exclusion keys — no test ever inspected the
  control schema.

Both are pinned here.  Under v3 the schema keys are ``runs(session_id,
command_id)`` (command idempotency) and ``session_leases.session_id`` (the only
execution mutex); the v2 ``owners``/``commands``/``tool_operations`` tables must
not come back.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from rollo.application import Application, ControlStore, _process_alive
from rollo.project_context import ProjectContext


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific liveness backend")
def test_windows_process_alive_sees_a_terminated_process_as_dead():
    """A dead pid must be reported dead even while our handle keeps it open.

    This is exactly the shape the pre-fix implementation got wrong:
    ``os.kill(pid, 0)`` does not probe on Windows (``signal.CTRL_C_EVENT == 0``),
    so it reported a terminated process as alive and its owner row was never
    quarantined.
    """

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        # The handle is deliberately kept open: that is what made the old
        # implementation report a terminated process as alive.
        assert _process_alive(child.pid) is True
        child.wait(timeout=60)
        assert child.returncode == 0
        assert _process_alive(child.pid) is False, (
            "a terminated process must not be reported alive"
        )
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)

    assert _process_alive(os.getpid()) is True
    for bogus in (0, -1, 999_999_999):
        assert _process_alive(bogus) is False


def test_control_schema_enforces_run_and_lease_uniqueness(tmp_path: Path):
    """The durable idempotency keys must exist, not just the application logic."""

    import sqlite3

    context = ProjectContext.from_root(tmp_path, runtime_data_dir=tmp_path / "runtime")
    store = ControlStore(
        context.runtime_data_dir / "application" / context.workspace_id / "control.sqlite"
    )
    now = "2026-01-01T00:00:00.000Z"
    try:
        tables = {
            str(row["name"])
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"owners", "commands", "tool_operations"}.isdisjoint(tables), tables

        lease_rows = list(store.connection.execute("PRAGMA table_info(session_leases)"))
        lease_columns = {str(row["name"]) for row in lease_rows}
        assert "pid" in lease_columns, lease_columns
        assert "process_id" not in lease_columns, lease_columns
        assert [str(row["name"]) for row in lease_rows if int(row["pk"]) == 1] == ["session_id"]

        # The columns below are asserted by behaviour rather than by matching the
        # DDL text: a semantic-preserving rewrite of the constraint (for example
        # swapping the column order, which uniqueness does not care about) must
        # not be reported as a regression.

        # And the constraints must actually bite: a second run row for the same
        # (session_id, command_id) can never be stored.
        store.connection.execute(
            "INSERT INTO runs(run_id,session_id,workspace_id,command_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("run-1", "s", "w", "c", "queued", now, now),
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.connection.execute(
                "INSERT INTO runs(run_id,session_id,workspace_id,command_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                ("run-2", "s", "w", "c", "queued", now, now),
            )
        # ... while a different command id in the same session is still allowed,
        # so the constraint is on the pair and not on either column alone.
        store.connection.execute(
            "INSERT INTO runs(run_id,session_id,workspace_id,command_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("run-3", "s", "w", "c-other", "queued", now, now),
        )

        store.connection.execute(
            "INSERT INTO session_leases(session_id,workspace_id,owner_id,pid,acquired_at,updated_at) VALUES(?,?,?,?,?,?)",
            ("s", "w", "owner-1", 1, now, now),
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.connection.execute(
                "INSERT INTO session_leases(session_id,workspace_id,owner_id,pid,acquired_at,updated_at) VALUES(?,?,?,?,?,?)",
                ("s", "w", "owner-2", 2, now, now),
            )
    finally:
        store.close()


def test_session_lease_blocks_a_second_holder_but_not_other_sessions(tmp_path: Path):
    """The unit of execution exclusion is the session, never the workspace.

    A lease held by a *live* foreign process refuses; a different session of the
    same workspace is untouched; a lease whose holder is gone is adopted; and a
    stale release can never delete a newer holder's row.
    """

    context = ProjectContext.from_root(tmp_path, runtime_data_dir=tmp_path / "runtime")
    store = ControlStore(
        context.runtime_data_dir / "application" / context.workspace_id / "control.sqlite"
    )
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    now = "2026-01-01T00:00:00.000Z"
    try:
        assert store.acquire_session_lease(
            session_id="session-a", workspace_id=context.workspace_id, owner_id="owner-self"
        ) is None
        assert int(store.session_lease("session-a")["pid"]) == os.getpid()

        # A live foreign holder refuses and its identity is reported back.
        store.connection.execute(
            "UPDATE session_leases SET owner_id=?,pid=? WHERE session_id=?",
            ("owner-foreign", holder.pid, "session-a"),
        )
        assert store.acquire_session_lease(
            session_id="session-a", workspace_id=context.workspace_id, owner_id="owner-b"
        ) == "owner-foreign"
        assert store.session_lease("session-a")["owner_id"] == "owner-foreign"

        # A different session of the same workspace is not blocked.
        assert store.acquire_session_lease(
            session_id="session-b", workspace_id=context.workspace_id, owner_id="owner-b"
        ) is None
        assert store.session_lease("session-b")["owner_id"] == "owner-b"

        # A release from a non-holder must not evict the live holder.
        store.release_session_lease("session-a")
        assert store.session_lease("session-a")["owner_id"] == "owner-foreign"

        # Once the holder is gone the lease is stale evidence, not a lock.
        holder.kill()
        holder.wait(timeout=30)
        assert store.acquire_session_lease(
            session_id="session-a", workspace_id=context.workspace_id, owner_id="owner-b"
        ) is None
        assert store.session_lease("session-a")["owner_id"] == "owner-b"

        # And the holder can always release its own lease.
        store.release_session_lease("session-a")
        assert store.session_lease("session-a") is None
        assert store.session_lease("session-b") is not None
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=30)
        store.close()

