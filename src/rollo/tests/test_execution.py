from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path

from rollo.execution import ManagedExecutionHandle
from rollo.project_context import ProjectContext
from rollo.tools import execute_managed_shell, execute_tool_value


def test_managed_shell_drains_both_streams_and_records_spill(tmp_path: Path):
    async def scenario():
        context = ProjectContext.from_root(tmp_path, runtime_data_dir=tmp_path / "runtime")
        script = "import sys; sys.stdout.write('o'*4096); sys.stderr.write('e'*4096)"
        handle = ManagedExecutionHandle(
            owner_id="owner-1",
            execution_id="exec-1",
            spill_dir=tmp_path / "spill",
            max_memory_bytes=32,
        )
        await handle.start([sys.executable, "-c", script], cwd=context.tool_cwd)
        snapshot = await handle.wait()
        assert snapshot.returncode == 0
        assert snapshot.stdout is not None and snapshot.stderr is not None
        assert snapshot.stdout.byte_count == 4096
        assert snapshot.stderr.byte_count == 4096
        assert snapshot.stdout.sha256 == hashlib.sha256(b"o" * 4096).hexdigest()
        assert snapshot.stderr.sha256 == hashlib.sha256(b"e" * 4096).hexdigest()
        assert snapshot.stdout.spill_path and Path(snapshot.stdout.spill_path).exists()
        assert snapshot.stderr.spill_path and Path(snapshot.stderr.spill_path).exists()

        value = await execute_tool_value(
            "run_shell",
            {"command": [sys.executable, "-c", "print('managed')"]},
            context=context,
        )
        assert "managed" in value

    asyncio.run(scenario())


def test_managed_shell_timeout_keeps_evidence(tmp_path: Path):
    async def scenario():
        context = ProjectContext.from_root(tmp_path, runtime_data_dir=tmp_path / "runtime")
        snapshot, output = await execute_managed_shell(
            [sys.executable, "-c", "import time; print('before', flush=True); time.sleep(2)"],
            context=context,
            owner_id="owner-2",
            execution_id="exec-2",
            timeout=0.2,
        )
        assert snapshot.requested_cancel is True
        assert snapshot.returncode is not None
        assert "before" in output

    asyncio.run(scenario())
