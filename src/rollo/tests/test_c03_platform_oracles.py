"""C03 — platform and schema oracles that the first review round found unguarded.

Two mutants survived the whole suite before this file existed:

* ``_windows_process_alive`` replaced by ``return False`` — every existing case
  short-circuits on ``pid == os.getpid()`` and never reaches the Windows API;
* dropping the ``commands`` primary key / ``runs`` uniqueness constraint — no
  test ever inspected the control schema.

Both are pinned here.
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


def test_control_schema_enforces_command_uniqueness(tmp_path: Path):
    """The durable idempotency keys must exist, not just the application logic."""

    context = ProjectContext.from_root(tmp_path, runtime_data_dir=tmp_path / "runtime")
    store = ControlStore(
        context.runtime_data_dir / "application" / context.workspace_id / "control.sqlite"
    )
    try:
        commands_sql = str(
            store.connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='commands'"
            ).fetchone()[0]
        ).lower().replace(" ", "")
        assert "primarykey(scope_type,scope_id,command_id)" in commands_sql, commands_sql

        runs_sql = str(
            store.connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='runs'"
            ).fetchone()[0]
        ).lower().replace(" ", "")
        assert "unique(session_id,command_id)" in runs_sql, runs_sql

        # And the constraint must actually bite: a duplicate command row with a
        # different digest can never be stored.
        import sqlite3

        now = "2026-01-01T00:00:00.000Z"
        store.connection.execute(
            "INSERT INTO commands(scope_type,scope_id,command_id,operation,params_digest,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("session", "s", "c", "run.start", "d1", "accepted", now, now),
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.connection.execute(
                "INSERT INTO commands(scope_type,scope_id,command_id,operation,params_digest,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                ("session", "s", "c", "run.start", "d2", "accepted", now, now),
            )
    finally:
        store.close()


def test_owner_capability_fast_path_rechecks_quarantine(tmp_path: Path):
    """P1-3: holding the capability must not skip the quarantine re-check.

    The branch is unreachable through ``run_start`` (a live task is refused with
    ``owner_conflict`` first, and ``_release_owner`` clears the lock once the
    last task finishes), so this drives ``_acquire_root_owner`` directly with the
    capability held.  It still has no mutation discrimination — that is a
    property of the branch being dead defensive code, not of the assertion — and
    the round-2 review confirmed the unreachability argument.
    """

    import asyncio

    from rollo.application import Application, WorkspaceLock, workspace_lock_key

    async def scenario():
        context = ProjectContext.from_root(tmp_path, runtime_data_dir=tmp_path / "runtime")
        app = Application(context, agent_factory=lambda **kwargs: None)
        try:
            # Simulate "this root already holds the workspace capability".
            lock_path = (
                context.runtime_data_dir
                / "application"
                / context.workspace_id
                / "locks"
                / f"{workspace_lock_key(context.workspace_id)}.lock"
            )
            app._owner_lock = WorkspaceLock(
                lock_path, workspace_id=context.workspace_id, owner_id="owner-self"
            ).acquire()
            app.owner_id = "owner-self"

            # Baseline: with no other owner row the fast path lets work through.
            assert app._acquire_root_owner() is None

            store = ControlStore(
                context.runtime_data_dir
                / "application"
                / context.workspace_id
                / "control.sqlite"
            )
            try:
                store.insert_owner(
                    owner_id="owner-stale",
                    workspace_id=context.workspace_id,
                    generation=99,
                    lock_key="stale",
                )
                store.update_owner("owner-stale", status="uncertain", quarantine=1)
            finally:
                store.close()

            blocked = app._acquire_root_owner()
            assert blocked is not None, "a quarantined owner must block a live root"
            assert blocked.error_code == "owner_quarantine", blocked
            assert blocked.data["owner_id"] == "owner-stale", blocked
        finally:
            app.owner_id = None
            if app._owner_lock is not None:
                app._owner_lock.release()
                app._owner_lock = None
            await app.shutdown()

    asyncio.run(scenario())
