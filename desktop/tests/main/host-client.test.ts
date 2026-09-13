/**
 * Main-process unit tests.
 *
 * Everything here runs without Electron and without a real interpreter: the
 * spawn boundary is injected, so framing, correlation and the id table are
 * exercised directly.  The real window and the real Python host are covered by
 * the Playwright smoke, which is where process-level truth belongs.
 */

import { EventEmitter } from 'node:events';
import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, describe, expect, it, vi } from 'vitest';

import { HostClient, HostError, assertUsablePython } from '../../src/main/host-client.js';
import { resolveSidecarPython, PYTHON_ENV } from '../../src/main/sidecar.js';
import { WorkspaceRegistry, workspaceIdFor } from '../../src/main/workspaces.js';

/** A child process double: we drive its stdout/stderr and inspect stdin. */
class FakeChild extends EventEmitter {
  readonly stdout = new EventEmitter() as EventEmitter & { setEncoding: (e: string) => void };
  readonly stderr = new EventEmitter() as EventEmitter & { setEncoding: (e: string) => void };
  readonly written: string[] = [];
  exitCode: number | null = null;
  killed = false;
  stdin = {
    write: (chunk: string, _encoding?: string, callback?: (error?: Error | null) => void): boolean => {
      this.written.push(chunk);
      callback?.(null);
      return true;
    },
  };

  constructor() {
    super();
    this.stdout.setEncoding = () => undefined;
    this.stderr.setEncoding = () => undefined;
  }

  emitLine(payload: unknown): void {
    this.stdout.emit('data', `${JSON.stringify(payload)}\n`);
  }

  emitRaw(text: string): void {
    this.stdout.emit('data', text);
  }

  emitStderr(text: string): void {
    this.stderr.emit('data', text);
  }

  kill(): boolean {
    this.killed = true;
    this.exitCode = 1;
    this.emit('exit', 1, 'SIGTERM');
    return true;
  }

  finish(code = 0): void {
    this.exitCode = code;
    this.emit('exit', code, null);
  }

  /** Reply to the n-th request this double has seen. */
  reply(index: number, result: unknown): void {
    const request = JSON.parse(this.written[index]!) as { id: number };
    this.emitLine({ jsonrpc: '2.0', id: request.id, result });
  }

  replyError(index: number, message: string, code?: string, rpcCode = -32603): void {
    const request = JSON.parse(this.written[index]!) as { id: number };
    this.emitLine({
      jsonrpc: '2.0',
      id: request.id,
      error: { code: rpcCode, message, ...(code ? { data: { code } } : {}) },
    });
  }
}

function makeClient(overrides: Partial<ConstructorParameters<typeof HostClient>[0]> = {}) {
  const child = new FakeChild();
  const client = new HostClient({
    pythonExecutable: 'C:/python/python.exe',
    workspace: 'C:/workspace',
    validateExecutable: false,
    spawnChild: (() => child) as never,
    ...overrides,
  });
  return { client, child };
}

const tempDirs: string[] = [];
afterEach(() => {
  vi.restoreAllMocks();
  tempDirs.length = 0;
});

function tempWorkspace(): string {
  const root = mkdtempSync(join(tmpdir(), 'rollo-desktop-'));
  tempDirs.push(root);
  return root;
}

describe('NDJSON framing', () => {
  it('decodes a frame delivered in two chunks', async () => {
    const { client, child } = makeClient();
    client.start();
    const pending = client.request<{ ok: boolean }>('session.list');
    const frame = `${JSON.stringify({ jsonrpc: '2.0', id: 1, result: { ok: true } })}\n`;
    child.emitRaw(frame.slice(0, 12));
    child.emitRaw(frame.slice(12));
    await expect(pending).resolves.toEqual({ ok: true });
  });

  it('decodes two frames arriving in one chunk', async () => {
    const { client, child } = makeClient();
    client.start();
    const first = client.request<number>('a');
    const second = client.request<number>('b');
    child.emitRaw(
      `${JSON.stringify({ jsonrpc: '2.0', id: 1, result: 1 })}\n` +
        `${JSON.stringify({ jsonrpc: '2.0', id: 2, result: 2 })}\n`,
    );
    await expect(first).resolves.toBe(1);
    await expect(second).resolves.toBe(2);
  });

  it('ignores a line that is not JSON instead of failing the connection', async () => {
    const { client, child } = makeClient();
    client.start();
    const pending = client.request<string>('session.list');
    child.emitRaw('this is not json\n');
    child.emitLine({ jsonrpc: '2.0', id: 1, result: 'ok' });
    await expect(pending).resolves.toBe('ok');
    expect(client.stderrTail.join('\n')).toContain('not JSON');
  });

  it('routes notifications to listeners, not to the pending table', () => {
    const { client, child } = makeClient();
    const seen: unknown[] = [];
    client.on('event', (event) => seen.push(event));
    client.start();
    child.emitLine({
      jsonrpc: '2.0',
      method: 'events.event',
      params: { subscription_id: 's1', session_id: 'x', kind: 'snapshot', transport_seq: 1 },
    });
    expect(seen).toHaveLength(1);
  });
});

describe('request correlation and errors', () => {
  it('surfaces the business code from error.data', async () => {
    const { client, child } = makeClient();
    client.start();
    const pending = client.request('run.start');
    child.replyError(0, 'busy', 'session_busy');
    await expect(pending).rejects.toBeInstanceOf(HostError);
    await pending.catch((error: HostError) => {
      expect(error.code).toBe('session_busy');
    });
  });

  it('rejects every pending request when the host exits unexpectedly', async () => {
    const { client, child } = makeClient();
    client.start();
    const pending = client.request('session.list');
    child.finish(3);
    await expect(pending).rejects.toThrow(/exited/);
    expect(client.state).toBe('failed');
  });

  it('refuses to send once the host is gone', async () => {
    const { client, child } = makeClient();
    client.start();
    child.finish(1);
    await expect(client.request('session.list')).rejects.toThrow(/not running/);
  });

  it('records stderr in a bounded ring', () => {
    const { client, child } = makeClient();
    client.start();
    for (let index = 0; index < 260; index += 1) child.emitStderr(`line ${index}\n`);
    expect(client.stderrTail).toHaveLength(200);
    expect(client.stderrTail.at(-1)).toBe('line 259');
  });
});

describe('sidecar validation', () => {
  it('rejects a relative path, shell metacharacters and non-Python names', () => {
    expect(() => assertUsablePython('python.exe')).toThrow(/absolute/);
    expect(() => assertUsablePython('C:/x/python.exe; rm -rf /')).toThrow(/metacharacters/);
    expect(() => assertUsablePython('C:/x/notepad.exe')).toThrow(/non-Python/);
    expect(() => assertUsablePython('C:/x/python.exe')).toThrow(/does not exist/);
  });
});

describe('sidecar resolution', () => {
  it('prefers the pinned environment variable', () => {
    const root = tempWorkspace();
    const exe = join(root, 'python.exe');
    writeFileSync(exe, '');
    const resolved = resolveSidecarPython(root, { [PYTHON_ENV]: exe } as NodeJS.ProcessEnv);
    expect(resolved.python).toBe(exe);
    expect(resolved.problem).toBeNull();
  });

  it('explains what to fix when a pinned path is wrong', () => {
    const root = tempWorkspace();
    const resolved = resolveSidecarPython(root, {
      [PYTHON_ENV]: join(root, 'missing.exe'),
    } as NodeJS.ProcessEnv);
    expect(resolved.python).toBeNull();
    expect(resolved.problem).toContain(PYTHON_ENV);
  });

  it('falls back to a checkout-local virtual environment', () => {
    const root = tempWorkspace();
    mkdirSync(join(root, '.venv', 'Scripts'), { recursive: true });
    writeFileSync(join(root, '.venv', 'Scripts', 'python.exe'), '');
    const resolved = resolveSidecarPython(root, {} as NodeJS.ProcessEnv);
    expect(resolved.python).toBe(join(root, '.venv', 'Scripts', 'python.exe'));
  });

  it('returns an actionable problem when nothing is found', () => {
    const resolved = resolveSidecarPython(tempWorkspace(), {} as NodeJS.ProcessEnv);
    expect(resolved.python).toBeNull();
    expect(resolved.problem).toMatch(/ROLLO_PYTHON|virtual environment/);
  });
});

describe('workspace registry', () => {
  function registry() {
    const events: unknown[] = [];
    return {
      events,
      registry: new WorkspaceRegistry({
        repoRoot: tempWorkspace(),
        onEvent: (_id, event) => events.push(event),
        onState: () => undefined,
      }),
    };
  }

  it('accepts a directory and hands back an opaque id, never the path', () => {
    const { registry: reg } = registry();
    const root = tempWorkspace();
    const entry = reg.add(root);
    expect(entry.workspace_id).toBe(workspaceIdFor(entry.root));
    expect(entry.workspace_id).toHaveLength(12);
    expect(entry.workspace_id).not.toContain(root);
  });

  it('returns the same entry when a directory is added twice', () => {
    const { registry: reg } = registry();
    const root = tempWorkspace();
    expect(reg.add(root).workspace_id).toBe(reg.add(root).workspace_id);
    expect(reg.list()).toHaveLength(1);
  });

  it('rejects a path that is not a directory', () => {
    const { registry: reg } = registry();
    expect(() => reg.add(join(tempWorkspace(), 'nope'))).toThrow(/not a directory/);
  });

  it('rejects an unknown workspace id', () => {
    const { registry: reg } = registry();
    expect(() => reg.get('deadbeef1234')).toThrow(/unknown workspace/);
    expect(() => reg.require('deadbeef1234')).toThrow(/unknown workspace/);
  });

  it('keeps a failed worker in the table with its problem, instead of vanishing', async () => {
    const { registry: reg } = registry();
    const entry = reg.add(tempWorkspace());
    // No interpreter is resolvable in a bare temp root.
    const connected = await reg.connect(entry.workspace_id);
    expect(connected.state).toBe('failed');
    expect(connected.problem).toMatch(/ROLLO_PYTHON|virtual environment/);
  });
});
