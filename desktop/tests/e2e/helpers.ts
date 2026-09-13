/**
 * Shared setup for the Electron smoke tests.
 *
 * Both specs need the same thing: a temporary workspace holding one session with
 * one canonical event, and an Electron launch configured to find the host
 * interpreter.  Keeping it here means the two specs cannot drift into testing
 * slightly different environments.
 */

import { spawnSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { _electron as electron, type ElectronApplication } from '@playwright/test';

// The package is CommonJS, so Playwright loads specs and their imports as
// CommonJS too; `import.meta` is not available here.
const here = join(__dirname, '..', '..');
export const desktopRoot = here;
export const repoRoot = join(here, '..');

/** The interpreter that runs the host, or null when the machine has none. */
export function resolvePython(): string | null {
  const candidates = [
    process.env.ROLLO_PYTHON,
    join(repoRoot, '.venv', 'Scripts', 'python.exe'),
    'D:/Anaconda/envs/py313/python.exe',
  ].filter((value): value is string => Boolean(value));
  for (const candidate of candidates) {
    // Probe the entry point the host is actually started with, using the same
    // import path: `import rollo` alone accepts an interpreter that has the
    // package installed but not the host module this change adds.
    const probe = spawnSync(candidate, ['-c', 'import rollo.host'], {
      env: { ...process.env, PYTHONPATH: join(repoRoot, 'src') },
      encoding: 'utf8',
    });
    if (probe.status === 0) return candidate;
  }
  return null;
}

export interface SeededWorkspace {
  root: string;
  sessionId: string;
}

const SEED_SCRIPT = `
import sys
from pathlib import Path
from rollo.runtime_store import SQLiteRuntimeStore
from rollo.runtime_event import RuntimeEvent

root = Path(sys.argv[1]); session_id = sys.argv[2]
store = SQLiteRuntimeStore(root / "runtime" / "sessions" / session_id / "runtime.sqlite")
store.append(RuntimeEvent.from_dict({
    "schema_version": 2, "id": f"open-{session_id}", "session_id": session_id,
    "run_id": "run-smoke", "invocation_id": "inv-smoke", "turn_id": "turn-smoke",
    "ts": 0, "partial": False, "role": "system", "author": "agent",
    "content": {"kind": "invocation_opened", "protocol": "invocation_opened_v1",
                "route": {"provider": "fixture", "model": "fixture-model"},
                "configuration": {"attempt": 1}, "root": {"kind": "agent"},
                "source": {"kind": "fresh"}},
}))
for index, text in enumerate(("first observation", "second observation"), start=1):
    store.append(RuntimeEvent.from_dict({
        "schema_version": 2, "id": f"{session_id}-msg-{index}", "session_id": session_id,
        "run_id": "run-smoke", "invocation_id": "inv-smoke", "turn_id": "turn-smoke",
        "ts": index, "partial": False, "role": "model", "author": "agent",
        "content": {"kind": "text", "text": text},
        "metadata": {"lifecycle": "model_final"},
    }))
store.close()
print("seeded")
`;

export function seedWorkspace(python: string, name = 'shell'): SeededWorkspace {
  const root = mkdtempSync(join(tmpdir(), `rollo-${name}-`));
  const sessionId = `session-${name}`;
  mkdirSync(join(root, 'runtime', 'sessions', sessionId), { recursive: true });
  const scriptPath = join(root, 'seed.py');
  writeFileSync(scriptPath, SEED_SCRIPT, 'utf8');
  const seeded = spawnSync(python, [scriptPath, root, sessionId], {
    env: { ...process.env, PYTHONPATH: join(repoRoot, 'src') },
    encoding: 'utf8',
  });
  if (seeded.status !== 0) {
    throw new Error(`seeding failed: ${seeded.stderr || seeded.stdout}`);
  }
  return { root, sessionId };
}

/**
 * Launch the shell against a workspace.
 *
 * `ROLLO_RUNTIME_DIR` is set explicitly: without it the runtime defaults to
 * `~/.rollo` and the host would read the developer's real sessions instead of
 * the temporary one -- which is both a correctness and a privacy problem.
 */
export async function launchShell(
  python: string,
  workspace: string,
): Promise<ElectronApplication> {
  return electron.launch({
    args: [desktopRoot],
    cwd: desktopRoot,
    env: {
      ...process.env,
      ROLLO_AUTO_WORKSPACE: workspace,
      ROLLO_PYTHON: python,
      ROLLO_PYTHONPATH: join(repoRoot, 'src'),
      ROLLO_RUNTIME_DIR: join(workspace, 'runtime'),
    },
  });
}
