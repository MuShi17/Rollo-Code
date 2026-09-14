/**
 * The main process side of IPC protocol v1: spawn a Python host and talk to it.
 *
 * This is the only place in the desktop shell that owns a child process.  It is
 * deliberately dumb: it frames bytes, correlates responses and surfaces errors.
 * It does not decide policy, does not touch canonical facts, and cannot run a
 * command -- every method here maps to one wire method the host already offers.
 */

import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { existsSync } from 'node:fs';

import {
  PROTOCOL_VERSION,
  type InitializeResult,
  type JsonRpcFrame,
  type JsonRpcNotification,
  type JsonRpcRequest,
  type SessionListResult,
  type SnapshotResult,
  type SubscribeResult,
  type WireEvent,
  type WorkerState,
} from '../shared/wire.js';

/** Bounded stderr buffer: a chatty worker must not grow memory without limit. */
const STDERR_LINES = 200;

export interface HostClientOptions {
  /** Absolute path to the Python interpreter that runs the host sidecar. */
  pythonExecutable: string;
  /** Workspace root the host serves. Resolved by the caller, never by a renderer. */
  workspace: string;
  /** Runtime data directory; defaults to the context default when omitted. */
  runtimeDir?: string;
  /** Extra arguments appended after `-m rollo.host`. */
  extraArgs?: string[];
  /** Shutdown budget before the child is killed. */
  shutdownTimeoutMs?: number;
  /** Import path handed to the host process; unset when `rollo` is installed. */
  pythonPath?: string;
  /**
   * Spawn boundary, injectable so framing and response correlation can be unit
   * tested without a real interpreter.  Production always takes the default.
   */
  spawnChild?: (
    executable: string,
    args: string[],
    options: Record<string, unknown>,
  ) => ChildProcessWithoutNullStreams;
  /**
   * When false, the executable is not required to exist on disk.  Only the
   * injected-spawn tests use this; a real spawn always validates.
   */
  validateExecutable?: boolean;
}

/** A wire-level failure, carrying the host's business code when it gave one. */
export class HostError extends Error {
  constructor(
    message: string,
    readonly code: string | undefined,
    readonly rpcCode: number | undefined,
    readonly data: Record<string, unknown> | undefined,
  ) {
    super(message);
    this.name = 'HostError';
  }
}

/**
 * A renderer must never be able to point the shell at an arbitrary binary, so
 * the path is validated once here: absolute, no shell metacharacters, and it
 * must contain "python" so a swapped value cannot silently become anything else.
 */
export function assertUsablePython(executable: string): void {
  if (!executable) throw new Error('a Python executable path is required');
  if (!/^[A-Za-z]:[\\/]|^\//.test(executable)) {
    throw new Error(`the Python executable must be an absolute path: ${executable}`);
  }
  if (/[;&|<>`$"\r\n]/.test(executable)) {
    throw new Error('the Python executable path contains shell metacharacters');
  }
  if (!/python/i.test(executable)) {
    throw new Error(`refusing to launch a non-Python executable: ${executable}`);
  }
  if (!existsSync(executable)) {
    throw new Error(`the Python executable does not exist: ${executable}`);
  }
}

export class HostClient extends EventEmitter {
  private child: ChildProcessWithoutNullStreams | null = null;
  private buffer = '';
  private nextId = 1;
  private readonly pending = new Map<
    number,
    { resolve: (value: unknown) => void; reject: (error: Error) => void }
  >();
  private readonly stderrRing: string[] = [];
  private stateValue: WorkerState = 'starting';
  private initialized: InitializeResult | null = null;
  private closing = false;

  constructor(private readonly options: HostClientOptions) {
    super();
  }

  get state(): WorkerState {
    return this.stateValue;
  }

  get stderrTail(): string[] {
    return [...this.stderrRing];
  }

  get handshake(): InitializeResult | null {
    return this.initialized;
  }

  get pid(): number | null {
    return this.child?.pid ?? null;
  }

  /**
   * The arguments this client will spawn with.
   *
   * Exposed so a failure can be attributed to the launch parameters rather than
   * guessed at: the two mistakes this has already caught are a missing import
   * path and an option key that never matched the field it was meant to fill.
   */
  get spawnPlan(): {
    executable: string;
    pythonPath: string | null;
    workspace: string;
    optionKeys: string[];
  } {
    return {
      executable: this.options.pythonExecutable,
      pythonPath: this.options.pythonPath ?? null,
      workspace: this.options.workspace,
      optionKeys: Object.keys(this.options).sort(),
    };
  }

  private setState(state: WorkerState): void {
    if (this.stateValue === state) return;
    this.stateValue = state;
    this.emit('state', state);
  }

  /** Spawn the sidecar. Argument array, no shell, hidden console on Windows. */
  start(): void {
    if (this.child) throw new Error('the host is already started');
    if (this.options.validateExecutable !== false) {
      assertUsablePython(this.options.pythonExecutable);
    }

    const args = ['-m', 'rollo.host', '--workspace', this.options.workspace];
    if (this.options.runtimeDir) args.push('--runtime-dir', this.options.runtimeDir);
    if (this.options.extraArgs?.length) args.push(...this.options.extraArgs);

    const spawnChild = this.options.spawnChild ?? (spawn as unknown as NonNullable<HostClientOptions['spawnChild']>);
    const childEnv = {
      ...process.env,
      // The wire is UTF-8 by contract; a CP936 locale would otherwise decide
      // the pipe encoding and mangle non-ASCII frames.
      PYTHONIOENCODING: 'utf-8',
      PYTHONUTF8: '1',
      PYTHONUNBUFFERED: '1',
      ...(this.options.pythonPath ? { PYTHONPATH: this.options.pythonPath } : {}),
    };
    console.error(
      `[desktop] spawning host: pythonPath=${String(this.options.pythonPath)} env.PYTHONPATH=${String(childEnv.PYTHONPATH)}`,
    );
    this.child = spawnChild(this.options.pythonExecutable, args, {
      cwd: this.options.workspace,
      shell: false,
      windowsHide: true,
      stdio: ['pipe', 'pipe', 'pipe'],
      env: childEnv,
    });

    this.child.stdout.setEncoding('utf8');
    this.child.stdout.on('data', (chunk: string) => this.consume(chunk));
    this.child.stderr.setEncoding('utf8');
    this.child.stderr.on('data', (chunk: string) => this.pushStderr(chunk));
    this.child.on('error', (error) => {
      this.pushStderr(`spawn failed: ${error.message}`);
      this.setState('failed');
      this.rejectAll(new Error(`the host could not be started: ${error.message}`));
    });
    this.child.on('exit', (code, signal) => {
      const reason = `the host exited (code=${code ?? 'null'} signal=${signal ?? 'null'})`;
      this.pushStderr(reason);
      if (!this.closing) {
        // The child's own diagnostics are the only clue to why it died, so they
        // travel with the failure instead of staying in a ring buffer nobody
        // reads at that moment.
        const tail = this.stderrRing.slice(-12).join('\n');
        this.rejectAll(new Error(tail ? `${reason}\n${tail}` : reason));
        this.setState('failed');
      } else {
        this.setState('stopped');
      }
      this.child = null;
    });

    this.setState('ready');
  }

  private pushStderr(chunk: string): void {
    for (const line of chunk.split(/\r?\n/)) {
      if (!line.trim()) continue;
      this.stderrRing.push(line);
      if (this.stderrRing.length > STDERR_LINES) this.stderrRing.shift();
      this.emit('stderr', line);
    }
  }

  /**
   * Decode as many complete frames as the chunk contains.
   *
   * A read boundary is not a frame boundary: the remainder is kept until its
   * newline arrives, which also reassembles a UTF-8 sequence split across reads
   * (Node has already decoded the stream, so only the line split is left).
   */
  private consume(chunk: string): void {
    this.buffer += chunk;
    let index = this.buffer.indexOf('\n');
    while (index >= 0) {
      const line = this.buffer.slice(0, index).trim();
      this.buffer = this.buffer.slice(index + 1);
      if (line) this.handleLine(line);
      index = this.buffer.indexOf('\n');
    }
  }

  private handleLine(line: string): void {
    let frame: JsonRpcFrame;
    try {
      frame = JSON.parse(line) as JsonRpcFrame;
    } catch {
      this.pushStderr(`the host sent a line that is not JSON: ${line.slice(0, 200)}`);
      return;
    }
    if ('id' in frame && frame.id !== null && frame.id !== undefined) {
      const waiter = this.pending.get(frame.id);
      if (!waiter) return;
      this.pending.delete(frame.id);
      if ('error' in frame && frame.error) {
        waiter.reject(
          new HostError(frame.error.message, frame.error.data?.code, frame.error.code, frame.error.data),
        );
      } else {
        waiter.resolve((frame as { result: unknown }).result);
      }
      return;
    }
    const notification = frame as JsonRpcNotification;
    if (notification.method === 'events.event') {
      this.emit('event', notification.params as unknown as WireEvent);
    }
  }

  private rejectAll(error: Error): void {
    for (const waiter of this.pending.values()) waiter.reject(error);
    this.pending.clear();
  }

  /** Send one request and await its response. */
  request<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    const child = this.child;
    if (!child || child.exitCode !== null) {
      return Promise.reject(new Error('the host is not running'));
    }
    const id = this.nextId++;
    const payload: JsonRpcRequest = { jsonrpc: '2.0', id, method, params };
    return new Promise<T>((resolve, reject) => {
      this.pending.set(id, { resolve: resolve as (value: unknown) => void, reject });
      child.stdin.write(`${JSON.stringify(payload)}\n`, 'utf8', (error) => {
        if (error) {
          this.pending.delete(id);
          reject(error);
        }
      });
    });
  }

  async initialize(): Promise<InitializeResult> {
    const result = await this.request<InitializeResult>('host.initialize', {
      version: PROTOCOL_VERSION,
    });
    this.initialized = result;
    return result;
  }

  listSessions(cursor?: string | null, limit = 100): Promise<SessionListResult> {
    const params: Record<string, unknown> = { limit };
    if (cursor) params.page_cursor = cursor;
    return this.request<SessionListResult>('session.list', params);
  }

  snapshot(sessionId: string): Promise<SnapshotResult> {
    return this.request<SnapshotResult>('session.snapshot', { session_id: sessionId });
  }

  subscribe(sessionId: string): Promise<SubscribeResult> {
    return this.request<SubscribeResult>('events.subscribe', { session_id: sessionId });
  }

  unsubscribe(subscriptionId: string): Promise<{ status: string }> {
    return this.request<{ status: string }>('events.unsubscribe', {
      subscription_id: subscriptionId,
    });
  }

  /** Ask the host to close, then make sure the process is really gone. */
  async shutdown(): Promise<void> {
    const child = this.child;
    if (!child) return;
    this.closing = true;
    const budget = this.options.shutdownTimeoutMs ?? 5000;
    try {
      await Promise.race([
        this.request('host.shutdown', {}),
        new Promise((resolve) => setTimeout(resolve, budget)),
      ]);
    } catch {
      // A dead or unresponsive host still has to be reaped below.
    }
    await this.waitForExit(budget);
    if (this.child) {
      this.pushStderr('the host did not exit in time; killing it');
      this.child.kill();
      await this.waitForExit(2000);
    }
  }

  private waitForExit(timeoutMs: number): Promise<void> {
    const child = this.child;
    if (!child) return Promise.resolve();
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        child.removeListener('exit', onExit);
        resolve();
      }, timeoutMs);
      const onExit = (): void => {
        clearTimeout(timer);
        resolve();
      };
      child.once('exit', onExit);
    });
  }
}
