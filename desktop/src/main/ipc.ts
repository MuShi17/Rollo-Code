/**
 * The main process's IPC surface.
 *
 * Two rules make this safe, and both are enforced here rather than in the
 * renderer:
 *
 * 1. **Sender validation.** Every handler checks that the message came from one
 *    of our own windows and from the origin we expect.  A renderer that is
 *    navigated somewhere else must not be able to drive the runtime.
 * 2. **Opaque handles only.** The renderer passes a `workspace_id` this process
 *    issued; it cannot pass a filesystem path or an executable.  The only way a
 *    directory enters the registry is the native picker.
 */

import { BrowserWindow, dialog, ipcMain, type IpcMainInvokeEvent } from 'electron';

import { CHANNELS, type WireEvent, type WorkerState } from '../shared/wire.js';
import type { WorkspaceEntry, WorkspaceRegistry } from './workspaces.js';

export interface IpcContext {
  registry: WorkspaceRegistry;
  /** Origins our renderer may legitimately be served from. */
  allowedOrigins: Set<string>;
  /** Windows that are allowed to talk to us at all. */
  windows: () => BrowserWindow[];
  /** Broadcast one worker event to every window. */
  emitEvent: (workspaceId: string, event: WireEvent) => void;
  emitState: (workspaceId: string, state: WorkerState) => void;
}

export class RejectedSender extends Error {}

/** Reject anything that is not one of our windows at an expected origin. */
export function assertTrustedSender(event: IpcMainInvokeEvent, context: IpcContext): void {
  const sender = event.sender;
  const known = context.windows().some((window) => window.webContents.id === sender.id);
  if (!known) throw new RejectedSender('rejected: unknown sender');

  let url: URL;
  try {
    url = new URL(sender.getURL());
  } catch {
    throw new RejectedSender(`rejected: unparseable sender URL ${sender.getURL()}`);
  }

  // A `file://` document reports the origin string "null" -- its real identity is
  // the scheme plus the window check above.  Comparing `origin` against
  // "file://" would reject every legitimate call from the packaged renderer.
  const identity = url.protocol === 'file:' ? 'file://' : url.origin;
  if (!context.allowedOrigins.has(identity)) {
    throw new RejectedSender(`rejected: sender origin ${identity} is not allowed`);
  }
}

function requireString(value: unknown, field: string): string {
  if (typeof value !== 'string' || !value.trim()) {
    throw new Error(`${field} must be a non-empty string`);
  }
  return value;
}

/** Wrap a handler so failures travel as messages, never as stack traces. */
function handler<T>(context: IpcContext, run: (...args: any[]) => Promise<T> | T) {
  return async (event: IpcMainInvokeEvent, ...args: any[]): Promise<T> => {
    assertTrustedSender(event, context);
    try {
      return await run(...args);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      throw new Error(message);
    }
  };
}

function describe(entry: WorkspaceEntry): {
  workspace_id: string;
  label: string;
  state: WorkerState;
  problem: string | null;
  settings: Record<string, unknown> | null;
} {
  return {
    workspace_id: entry.workspace_id,
    label: entry.label,
    state: entry.state,
    problem: entry.problem,
    settings: entry.handshake ? { ...entry.handshake.settings } : null,
  };
}

export function registerIpc(context: IpcContext): void {
  const { registry } = context;

  ipcMain.handle(
    CHANNELS.chooseWorkspace,
    handler(context, async () => {
      const window = context.windows()[0];
      const picked = await dialog.showOpenDialog(window!, {
        title: 'Choose a workspace',
        properties: ['openDirectory', 'createDirectory'],
      });
      if (picked.canceled || picked.filePaths.length === 0) return null;
      const entry = registry.add(picked.filePaths[0]!);
      return { workspace_id: entry.workspace_id, label: entry.label };
    }),
  );

  ipcMain.handle(CHANNELS.listWorkspaces, handler(context, () => registry.list()));

  // The renderer asks for this rather than waiting to be told: a message pushed
  // before the page registers its listener would be lost, and a reload needs the
  // same answer anyway.
  ipcMain.handle(
    CHANNELS.autoWorkspace,
    handler(context, () => {
      const preset = process.env.ROLLO_AUTO_WORKSPACE;
      if (!preset || !preset.trim()) return null;
      try {
        const entry = registry.add(preset.trim());
        return { workspace_id: entry.workspace_id, label: entry.label };
      } catch {
        return null;
      }
    }),
  );

  ipcMain.handle(
    CHANNELS.initialize,
    handler(context, async (workspaceId: unknown) => {
      const entry = await registry.connect(requireString(workspaceId, 'workspace_id'));
      if (!entry.handshake) throw new Error(entry.problem ?? 'the host did not handshake');
      return entry.handshake;
    }),
  );

  ipcMain.handle(
    CHANNELS.listSessions,
    handler(context, async (workspaceId: unknown, cursor: unknown) => {
      await registry.connect(requireString(workspaceId, 'workspace_id'));
      return registry.require(String(workspaceId)).listSessions(
        typeof cursor === 'string' && cursor ? cursor : null,
      );
    }),
  );

  ipcMain.handle(
    CHANNELS.snapshot,
    handler(context, async (workspaceId: unknown, sessionId: unknown) => {
      await registry.connect(requireString(workspaceId, 'workspace_id'));
      return registry
        .require(String(workspaceId))
        .snapshot(requireString(sessionId, 'session_id'));
    }),
  );

  ipcMain.handle(
    CHANNELS.subscribe,
    handler(context, async (workspaceId: unknown, sessionId: unknown) => {
      await registry.connect(requireString(workspaceId, 'workspace_id'));
      return registry
        .require(String(workspaceId))
        .subscribe(requireString(sessionId, 'session_id'));
    }),
  );

  ipcMain.handle(
    CHANNELS.unsubscribe,
    handler(context, async (workspaceId: unknown, subscriptionId: unknown) => {
      return registry
        .require(requireString(workspaceId, 'workspace_id'))
        .unsubscribe(requireString(subscriptionId, 'subscription_id'));
    }),
  );

  ipcMain.handle(
    CHANNELS.workerStatus,
    handler(context, (workspaceId: unknown) => {
      const entry = registry.get(requireString(workspaceId, 'workspace_id'));
      // Only what the UI needs to explain itself.  The spawn plan (interpreter
      // path, import path, workspace directory) is deliberately NOT sent: a
      // renderer has no filesystem entry point, so handing it absolute paths
      // would contradict the boundary the rest of this file enforces.
      return {
        workspace_id: entry.workspace_id,
        label: entry.label,
        state: entry.state,
        problem: entry.problem,
        settings: entry.handshake ? { ...entry.handshake.settings } : null,
        stderr: entry.client?.stderrTail ?? [],
      };
    }),
  );
}

