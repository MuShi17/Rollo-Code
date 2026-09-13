/**
 * Cross-process integration: the desktop client against a real Python host.
 *
 * This is the test that proves the shell actually consumes IPC protocol v1 --
 * real interpreter, real pipes, real NDJSON.  Electron is not involved; Electron
 * only adds a window, and that is covered by the Playwright smoke.
 *
 * Skipped (not failed) when no interpreter is resolvable, so a machine without
 * the Python side cannot report a green shell it never exercised.
 */

import { spawnSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, describe, expect, it } from 'vitest';

import { HostClient } from '../../src/main/host-client.js';

const repoRoot = join(__dirname, '..', '..', '..');

function resolvePython(): string | null {
  const candidates = [
    process.env.ROLLO_PYTHON,
    join(repoRoot, '.venv', 'Scripts', 'python.exe'),
    'D:/Anaconda/envs/py313/python.exe',
  ].filter((value): value is string => Boolean(value));
  for (const candidate of candidates) {
    const probe = spawnSync(candidate, ['-c', 'import rollo, sys; print(sys.version)'], {
      env: { ...process.env, PYTHONPATH: join(repoRoot, 'src') },
      encoding: 'utf8',
    });
    if (probe.status === 0) return candidate;
  }
  return null;
}

const python = resolvePython();
const describeIfPython = python ? describe : describe.skip;

const clients: HostClient[] = [];
afterEach(async () => {
  await Promise.all(clients.splice(0).map((client) => client.shutdown().catch(() => undefined)));
});

function newClient(workspace: string): HostClient {
  const client = new HostClient({
    pythonExecutable: python!,
    workspace,
    runtimeDir: join(workspace, 'runtime'),
    shutdownTimeoutMs: 8000,
    // A checkout that is not pip-installed needs the import path explicitly;
    // a packaged build leaves this unset.
    pythonPath: process.env.ROLLO_PYTHONPATH ?? join(repoRoot, 'src'),
  });
  clients.push(client);
  return client;
}

/** A workspace shaped like the runtime expects, with one session that has events. */
function seedWorkspace(): { root: string; sessionId: string } {
  const root = mkdtempSync(join(tmpdir(), 'rollo-e2e-'));
  const sessionId = 'session-observation';
  mkdirSync(join(root, 'runtime', 'sessions', sessionId), { recursive: true });
  const script = `
import sys
from pathlib import Path
from rollo.runtime_store import SQLiteRuntimeStore
from rollo.runtime_event import RuntimeEvent

root = Path(sys.argv[1])
session_id = sys.argv[2]
store = SQLiteRuntimeStore(root / "runtime" / "sessions" / session_id / "runtime.sqlite")
store.append(RuntimeEvent.from_dict({
    "schema_version": 2,
    "id": f"open-{session_id}",
    "session_id": session_id,
    "run_id": "run-observation",
    "invocation_id": "inv-observation",
    "turn_id": "turn-observation",
    "ts": 0,
    "partial": False,
    "role": "system",
    "author": "agent",
    "content": {
        "kind": "invocation_opened",
        "protocol": "invocation_opened_v1",
        "route": {"provider": "fixture", "model": "fixture-model"},
        "configuration": {"attempt": 1},
        "root": {"kind": "agent"},
        "source": {"kind": "fresh"},
    },
}))
store.close()
print("seeded")
`;
  writeFileSync(join(root, 'seed.py'), script, 'utf8');
  const seeded = spawnSync(python!, [join(root, 'seed.py'), root, sessionId], {
    env: { ...process.env, PYTHONPATH: join(repoRoot, 'src') },
    encoding: 'utf8',
  });
  if (seeded.status !== 0) {
    throw new Error(`seeding failed: ${seeded.stderr || seeded.stdout}`);
  }
  return { root, sessionId };
}

describeIfPython('desktop client against a real Python host', () => {
  it('handshakes, lists, snapshots, subscribes and shuts down cleanly', async () => {
    const { root, sessionId } = seedWorkspace();
    const client = newClient(root);
    const events: Array<{ kind: string; ordinal: number | null }> = [];
    client.on('event', (event: { kind: string; ordinal: number | null }) => events.push(event));

    client.start();
    const handshake = await client.initialize();
    expect(handshake.protocol_version).toBe(1);
    expect(handshake.host_epoch).toMatch(/^[0-9a-f]{8,}$/);
    expect(handshake.capabilities.batch).toBe(false);
    expect(handshake.capabilities.control).toEqual([]);
    expect(handshake.capabilities.declared_not_implemented).toContain('run.start');

    const listed = await client.listSessions();
    const session = listed.sessions.find((row) => row.session_id === sessionId);
    expect(session).toBeDefined();
    // The renderer has no filesystem entry point, so the path must not travel.
    expect(JSON.stringify(listed)).not.toContain('runtime.sqlite');

    const snapshot = await client.snapshot(sessionId);
    expect(snapshot.high_water).toBeGreaterThanOrEqual(1);
    expect(snapshot.projection_version).toBe('gui-projection-v1');
    expect(snapshot.source_digest).toMatch(/^[0-9a-f]{16,}$/);

    const subscribed = await client.subscribe(sessionId);
    expect(subscribed.status).toBe('subscribed');
    expect(subscribed.host_epoch).toBe(handshake.host_epoch);

    // The first delivery must be the atomic snapshot, at the boundary it reports.
    await waitFor(() => events.some((event) => event.kind === 'snapshot'));
    const first = events.find((event) => event.kind === 'snapshot')!;
    expect(first.ordinal).toBe(snapshot.high_water);

    // A concurrent session.list must still be served while the pump is running:
    // the read loop and delivery have to be independent.
    await expect(client.listSessions()).resolves.toBeTruthy();

    const stopped = await client.unsubscribe(subscribed.subscription_id);
    expect(stopped.status).toBe('unsubscribed');

    const pid = client.pid;
    expect(pid).toBeGreaterThan(0);
    await client.shutdown();
    expect(client.state).toBe('stopped');
    expect(isAlive(pid!)).toBe(false);
  }, 60000);

  it('reports a business code when the host refuses a control method', async () => {
    const { root } = seedWorkspace();
    const client = newClient(root);
    client.start();
    await client.initialize();

    await expect(client.request('run.start', { session_id: 'x' })).rejects.toMatchObject({
      code: 'not_implemented',
    });
    await client.shutdown();
  }, 60000);
});

function isAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

async function waitFor(predicate: () => boolean, timeoutMs = 15000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error('timed out waiting for the condition');
}
