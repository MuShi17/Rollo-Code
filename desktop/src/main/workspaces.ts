/**
 * One Python host per workspace, keyed by a normalised directory.
 *
 * The renderer never names a child process: it holds an opaque `workspace_id`
 * that this registry issued, and every lookup goes through the table.  That is
 * the whole reason the registry exists -- without it, "start a worker" would be
 * an arbitrary-executable primitive.
 *
 * A workspace whose host cannot start stays in the table with a `failed` state
 * and its diagnostics, so the UI can explain what to fix instead of silently
 * doing nothing.
 */

import { createHash } from 'node:crypto';
import { existsSync, statSync } from 'node:fs';
import { resolve } from 'node:path';

import { HostClient } from './host-client.js';
import { hostEnvironment, resolveSidecarPython } from './sidecar.js';
import type { InitializeResult, WireEvent, WorkerState } from '../shared/wire.js';

export interface WorkspaceEntry {
  workspace_id: string;
  /** Absolute, symlink-resolved directory. Never rendered verbatim as a path. */
  root: string;
  /** Short label for the UI: the directory name. */
  label: string;
  client: HostClient | null;
  state: WorkerState;
  problem: string | null;
  handshake: InitializeResult | null;
}

export function workspaceIdFor(root: string): string {
  return createHash('sha256').update(root.toLowerCase()).digest('hex').slice(0, 12);
}

export class WorkspaceRegistry {
  private readonly entries = new Map<string, WorkspaceEntry>();
  /** One in-flight connect per workspace, so a second caller joins it. */
  private readonly connecting = new Map<string, Promise<WorkspaceEntry>>();
  private closing = false;

  constructor(
    private readonly options: {
      /** Called for every event from every worker. */
      onEvent: (workspaceId: string, event: WireEvent) => void;
      onState: (workspaceId: string, state: WorkerState) => void;
      repoRoot: string;
      runtimeDirFor?: (root: string) => string | undefined;
    },
  ) {}

  list(): Array<{ workspace_id: string; label: string }> {
    return [...this.entries.values()].map((entry) => ({
      workspace_id: entry.workspace_id,
      label: entry.label,
    }));
  }

  get(workspaceId: string): WorkspaceEntry {
    const entry = this.entries.get(workspaceId);
    if (!entry) throw new Error(`unknown workspace: ${workspaceId}`);
    return entry;
  }

  /**
   * Register a directory chosen by the native picker.
   *
   * Re-adding the same directory returns the existing entry rather than
   * spawning a second host for it.
   */
  add(directory: string): WorkspaceEntry {
    const root = resolve(directory);
    if (!existsSync(root) || !statSync(root).isDirectory()) {
      throw new Error(`not a directory: ${root}`);
    }
    const workspaceId = workspaceIdFor(root);
    const existing = this.entries.get(workspaceId);
    if (existing) return existing;

    const entry: WorkspaceEntry = {
      workspace_id: workspaceId,
      root,
      label: root.split(/[\\/]/).filter(Boolean).pop() ?? root,
      client: null,
      state: 'starting',
      problem: null,
      handshake: null,
    };
    this.entries.set(workspaceId, entry);
    return entry;
  }

  /**
   * Start the host for a registered workspace. Safe to call concurrently.
   *
   * The readiness test alone is not enough: `HostClient.start()` reports
   * `ready` as soon as the process is spawned, while the handshake takes about a
   * second.  Any `connect()` arriving inside that window used to spawn a second
   * host and overwrite the first, leaving a process the registry could no longer
   * reach.  Concurrent callers therefore share one in-flight attempt.
   */
  async connect(workspaceId: string): Promise<WorkspaceEntry> {
    const entry = this.get(workspaceId);
    if (entry.client && entry.state === 'ready' && entry.handshake) return entry;
    if (this.closing) throw new Error('the workspace registry is shutting down');

    const inFlight = this.connecting.get(workspaceId);
    if (inFlight) return inFlight;

    const attempt = this.connectOnce(entry).finally(() => this.connecting.delete(workspaceId));
    this.connecting.set(workspaceId, attempt);
    return attempt;
  }

  private async connectOnce(entry: WorkspaceEntry): Promise<WorkspaceEntry> {
    const workspaceId = entry.workspace_id;
    const sidecar = resolveSidecarPython(this.options.repoRoot);
    if (!sidecar.python) {
      entry.state = 'failed';
      entry.problem = sidecar.problem;
      this.options.onState(workspaceId, entry.state);
      return entry;
    }

    const client = new HostClient({
      pythonExecutable: sidecar.python,
      workspace: entry.root,
      runtimeDir: this.options.runtimeDirFor?.(entry.root),
      ...hostEnvironment(),
    });
    entry.client = client;
    client.on('event', (event: WireEvent) => this.options.onEvent(workspaceId, event));
    client.on('state', (state: WorkerState) => {
      entry.state = state;
      this.options.onState(workspaceId, state);
    });

    try {
      client.start();
      entry.handshake = await client.initialize();
      entry.state = 'ready';
      entry.problem = null;
    } catch (error) {
      entry.state = 'failed';
      entry.problem = error instanceof Error ? error.message : String(error);
      entry.handshake = null;
    }
    this.options.onState(workspaceId, entry.state);
    return entry;
  }

  require(workspaceId: string): HostClient {
    const entry = this.get(workspaceId);
    if (!entry.client) throw new Error(entry.problem ?? `workspace ${workspaceId} has no host`);
    return entry.client;
  }

  /** Stop every host. Called once, on application quit. */
  async closeAll(): Promise<void> {
    // A connect that is still in flight during shutdown would spawn a process
    // after this method has swept the table, and nothing would ever reap it.
    this.closing = true;
    await Promise.allSettled([...this.connecting.values()]);
    await Promise.all(
      [...this.entries.values()].map(async (entry) => {
        if (!entry.client) return;
        await entry.client.shutdown().catch(() => undefined);
        entry.client = null;
        entry.state = 'stopped';
      }),
    );
  }
}
