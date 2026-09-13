/**
 * Wire types for IPC protocol v1, mirrored from the Python host.
 *
 * The host is the authority: these declarations exist so the main process can
 * type its own client, not so it can redefine the protocol.  Anything the host
 * does not send is not represented here.
 */

/** Protocol version this client speaks. The host negotiates, never guesses. */
export const PROTOCOL_VERSION = 1;

/** Names the host answers today. Control methods are deliberately absent. */
export const OBSERVATION_METHODS = [
  'host.initialize',
  'session.list',
  'session.snapshot',
  'events.subscribe',
  'events.unsubscribe',
  'host.shutdown',
] as const;

export interface JsonRpcRequest {
  jsonrpc: '2.0';
  id: number;
  method: string;
  params: Record<string, unknown>;
}

export interface JsonRpcNotification {
  jsonrpc: '2.0';
  method: string;
  params: Record<string, unknown>;
}

export interface JsonRpcError {
  code: number;
  message: string;
  data?: { code?: string; [key: string]: unknown };
}

export type JsonRpcFrame =
  | { jsonrpc: '2.0'; id: number; result: unknown }
  | { jsonrpc: '2.0'; id: number | null; error: JsonRpcError }
  | JsonRpcNotification;

export interface InitializeResult {
  protocol_version: number;
  host_epoch: string;
  workspace_id: string;
  capabilities: {
    observation: string[];
    control: string[];
    declared_not_implemented: string[];
    batch: boolean;
  };
  limits: { frame_bytes: number; page_items: number };
  /** Missing settings are reported by NAME; values never cross the wire. */
  settings: { ready: boolean; missing: string[]; model: string | null };
}

export interface SessionSummary {
  session_id: string;
  workspace_id: string | null;
  status: string | null;
}

export interface SessionListResult {
  workspace_id: string;
  sessions: SessionSummary[];
  next_page_cursor: string | null;
}

export interface SnapshotResult {
  session_id: string;
  host_epoch: string;
  high_water: number;
  source_digest: string;
  projection_version: string;
  snapshot: Record<string, unknown>;
}

export interface SubscribeResult {
  subscription_id: string;
  session_id: string;
  host_epoch: string;
  status: 'subscribed' | 'resumed';
}

/** One delivered observation. `ordinal` is the resume boundary for snapshots. */
export interface WireEvent {
  subscription_id: string;
  session_id: string;
  host_epoch: string;
  transport_seq: number;
  kind: string;
  ordinal: number | null;
  key: string | null;
  prefix_boundary_exempt: boolean;
  payload: unknown;
}

export type WorkerState = 'starting' | 'ready' | 'failed' | 'stopped';

/** What the renderer may ask the main process to do. Nothing else exists. */
export interface DesktopApi {
  chooseWorkspace(): Promise<{ workspace_id: string; label: string } | null>;
  listWorkspaces(): Promise<Array<{ workspace_id: string; label: string }>>;
  initialize(workspaceId: string): Promise<InitializeResult>;
  listSessions(workspaceId: string, cursor?: string | null): Promise<SessionListResult>;
  snapshot(workspaceId: string, sessionId: string): Promise<SnapshotResult>;
  subscribe(workspaceId: string, sessionId: string): Promise<SubscribeResult>;
  unsubscribe(workspaceId: string, subscriptionId: string): Promise<{ status: string }>;
  workerStatus(workspaceId: string): Promise<{ state: WorkerState; stderr: string[] }>;
  onEvent(listener: (envelope: EventEnvelope) => void): () => void;
  onWorkerState(listener: (envelope: WorkerStateEnvelope) => void): () => void;
  /**
   * Workspace registered by the main process for automated runs.
   *
   * Only the smoke test sets this up; an interactive launch registers through
   * the native picker instead.  It is a request, not a subscription, so it works
   * on a reload too.
   */
  autoWorkspace(): Promise<{ workspace_id: string; label: string } | null>;
}

/**
 * Events are delivered with the workspace they came from.
 *
 * A window may hold more than one workspace and a subscription id alone does not
 * say which host produced it, so the envelope is part of the contract.
 */
export interface EventEnvelope {
  workspace_id: string;
  event: WireEvent;
}

export interface WorkerStateEnvelope {
  workspace_id: string;
  state: WorkerState;
}

/** IPC channel names, kept in one place so both sides cannot drift. */
export const CHANNELS = {
  chooseWorkspace: 'desktop:choose-workspace',
  listWorkspaces: 'desktop:list-workspaces',
  initialize: 'desktop:initialize',
  listSessions: 'desktop:list-sessions',
  snapshot: 'desktop:snapshot',
  subscribe: 'desktop:subscribe',
  unsubscribe: 'desktop:unsubscribe',
  workerStatus: 'desktop:worker-status',
  event: 'desktop:event',
  workerState: 'desktop:worker-state',
  autoWorkspace: 'desktop:auto-workspace',
} as const;
