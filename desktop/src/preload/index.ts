/**
 * The renderer's entire view of the outside world.
 *
 * There is no generic `invoke(channel, ...)`: each method below is a named
 * business operation with its own argument shape, so a compromised renderer
 * cannot reach an arbitrary IPC channel.  The preload also re-validates its
 * arguments -- the main process validates them again, because a preload is a
 * convenience layer and not a trust boundary.
 */

import { contextBridge, ipcRenderer } from 'electron';

import {
  CHANNELS,
  type DesktopApi,
  type EventEnvelope,
  type InitializeResult,
  type SessionListResult,
  type SnapshotResult,
  type SubscribeResult,
  type WorkerState,
  type WorkerStateEnvelope,
} from '../shared/wire.js';

function requireId(value: unknown, field: string): string {
  if (typeof value !== 'string' || !value.trim()) {
    throw new TypeError(`${field} must be a non-empty string`);
  }
  return value;
}

function subscribeTo<T>(channel: string): (listener: (payload: T) => void) => () => void {
  return (listener: (payload: T) => void) => {
    if (typeof listener !== 'function') throw new TypeError('listener must be a function');
    const wrapped = (_event: unknown, payload: T): void => listener(payload);
    ipcRenderer.on(channel, wrapped);
    return () => {
      ipcRenderer.removeListener(channel, wrapped);
    };
  };
}

const api: DesktopApi = {
  chooseWorkspace: () => ipcRenderer.invoke(CHANNELS.chooseWorkspace),
  listWorkspaces: () => ipcRenderer.invoke(CHANNELS.listWorkspaces),
  initialize: (workspaceId: string): Promise<InitializeResult> =>
    ipcRenderer.invoke(CHANNELS.initialize, requireId(workspaceId, 'workspace_id')),
  listSessions: (workspaceId: string, cursor?: string | null): Promise<SessionListResult> =>
    ipcRenderer.invoke(
      CHANNELS.listSessions,
      requireId(workspaceId, 'workspace_id'),
      cursor ?? null,
    ),
  snapshot: (workspaceId: string, sessionId: string): Promise<SnapshotResult> =>
    ipcRenderer.invoke(
      CHANNELS.snapshot,
      requireId(workspaceId, 'workspace_id'),
      requireId(sessionId, 'session_id'),
    ),
  subscribe: (workspaceId: string, sessionId: string): Promise<SubscribeResult> =>
    ipcRenderer.invoke(
      CHANNELS.subscribe,
      requireId(workspaceId, 'workspace_id'),
      requireId(sessionId, 'session_id'),
    ),
  unsubscribe: (workspaceId: string, subscriptionId: string): Promise<{ status: string }> =>
    ipcRenderer.invoke(
      CHANNELS.unsubscribe,
      requireId(workspaceId, 'workspace_id'),
      requireId(subscriptionId, 'subscription_id'),
    ),
  workerStatus: (workspaceId: string) =>
    ipcRenderer.invoke(CHANNELS.workerStatus, requireId(workspaceId, 'workspace_id')),
  onEvent: subscribeTo<EventEnvelope>(CHANNELS.event),
  onWorkerState: subscribeTo<WorkerStateEnvelope>(CHANNELS.workerState),
  autoWorkspace: () => ipcRenderer.invoke(CHANNELS.autoWorkspace),
};

contextBridge.exposeInMainWorld('rollo', Object.freeze(api));
