/**
 * Renderer: a read-only view over one workspace.
 *
 * It can pick a workspace, list sessions, snapshot one and follow it live.  It
 * deliberately offers no command that changes runtime state -- the observation
 * slice of the host has none, and inventing one here would just fail at the
 * wire.  Everything it can do is a named method on `window.rollo`.
 */

import { useEffect, useMemo, useRef, useState } from 'react';

import type {
  InitializeResult,
  SessionSummary,
  WireEvent,
  WorkerState,
} from '../shared/wire.js';
import type { DesktopApi } from '../shared/wire.js';

declare global {
  interface Window {
    rollo: DesktopApi;
  }
}

interface Timeline {
  subscriptionId: string;
  sessionId: string;
  events: WireEvent[];
}

export function App(): JSX.Element {
  const [workspace, setWorkspace] = useState<{ workspace_id: string; label: string } | null>(null);
  const [workspaceState, setWorkspaceState] = useState<WorkerState>('starting');
  const [handshake, setHandshake] = useState<InitializeResult | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [timeline, setTimeline] = useState<Timeline | null>(null);
  const [busy, setBusy] = useState(false);
  /** Workspace the bootstrap already connected, so a re-run does not repeat it. */
  const connectedRef = useRef<string | null>(null);

  // A single subscription at a time keeps the placeholder honest: it shows the
  // stream as it arrives rather than pretending to multiplex.
  useEffect(() => {
    const offEvent = window.rollo.onEvent(({ workspace_id, event }) => {
      if (workspace?.workspace_id !== workspace_id) return;
      setTimeline((current) =>
        current && current.subscriptionId === event.subscription_id
          ? { ...current, events: [...current.events, event].slice(-500) }
          : current,
      );
    });
    const offState = window.rollo.onWorkerState(({ workspace_id, state }) => {
      if (workspace?.workspace_id !== workspace_id) return;
      setWorkspaceState(state);
    });

    // An automated launch registers the workspace in the main process; an
    // interactive one goes through the picker below.  The ref guard keeps the
    // effect from connecting again when it re-runs for that same workspace --
    // which is exactly what happened when `workspace` became a dependency and
    // StrictMode double-invoked the effect.
    const bootstrap = async (): Promise<void> => {
      const preset = await window.rollo.autoWorkspace();
      if (!preset) return;
      setWorkspace(preset);
      if (preset.workspace_id === connectedRef.current) return;
      connectedRef.current = preset.workspace_id;
      await connect(preset.workspace_id);
    };
    void bootstrap();

    return () => {
      offEvent();
      offState();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspace?.workspace_id]);

  const capabilities = handshake?.capabilities;

  async function pick(): Promise<void> {
    setProblem(null);
    const picked = await window.rollo.chooseWorkspace();
    if (!picked) return;
    setWorkspace(picked);
    setHandshake(null);
    setSessions([]);
    setTimeline(null);
    await connect(picked.workspace_id);
  }

  async function connect(workspaceId: string): Promise<void> {
    setBusy(true);
    try {
      const result = await window.rollo.initialize(workspaceId);
      setHandshake(result);
      setWorkspaceState('ready');
      setSessions((await window.rollo.listSessions(workspaceId)).sessions);
    } catch (error) {
      setProblem(error instanceof Error ? error.message : String(error));
      setWorkspaceState('failed');
    } finally {
      setBusy(false);
    }
  }

  async function watch(session: SessionSummary): Promise<void> {
    if (!workspace) return;
    setBusy(true);
    setProblem(null);
    try {
      const subscribed = await window.rollo.subscribe(workspace.workspace_id, session.session_id);
      setTimeline({
        subscriptionId: subscribed.subscription_id,
        sessionId: session.session_id,
        events: [],
      });
    } catch (error) {
      setProblem(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function stopWatching(): Promise<void> {
    if (!workspace || !timeline) return;
    await window.rollo.unsubscribe(workspace.workspace_id, timeline.subscriptionId).catch(() => undefined);
    setTimeline(null);
  }

  const status = useMemo(() => {
    if (!workspace) return 'no workspace';
    return `${workspace.label} · ${workspaceState}`;
  }, [workspace, workspaceState]);

  return (
    <main style={styles.main}>
      <header style={styles.header}>
        <strong style={styles.title}>Rollo Code</strong>
        <span style={styles.status}>{status}</span>
        <button style={styles.button} onClick={pick} disabled={busy}>
          {workspace ? 'Change workspace' : 'Choose workspace'}
        </button>
      </header>

      {problem && <p style={styles.problem}>{problem}</p>}

      {handshake && (
        <section style={styles.panel}>
          <h2 style={styles.h2}>Host</h2>
          <dl style={styles.dl}>
            <dt>protocol</dt>
            <dd>v{handshake.protocol_version}</dd>
            <dt>epoch</dt>
            <dd>{handshake.host_epoch.slice(0, 12)}</dd>
            <dt>workspace</dt>
            <dd>{handshake.workspace_id}</dd>
            <dt>settings</dt>
            <dd>
              {handshake.settings.ready
                ? `ready${handshake.settings.model ? ` · ${handshake.settings.model}` : ''}`
                : `missing: ${handshake.settings.missing.join(', ')}`}
            </dd>
            <dt>control</dt>
            <dd>
              {capabilities?.control.length
                ? capabilities.control.join(', ')
                : 'not in this build'}
            </dd>
          </dl>
        </section>
      )}

      {workspace && (
        <section style={styles.panel}>
          <h2 style={styles.h2}>Sessions ({sessions.length})</h2>
          {sessions.length === 0 ? (
            <p style={styles.muted}>No sessions in this workspace yet.</p>
          ) : (
            <ul style={styles.list}>
              {sessions.map((session) => (
                <li key={session.session_id} style={styles.listItem}>
                  <code>{session.session_id}</code>
                  <span style={styles.muted}>{session.status ?? 'unknown'}</span>
                  <button
                    style={styles.smallButton}
                    onClick={() => void watch(session)}
                    disabled={busy}
                  >
                    Watch
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {timeline && (
        <section style={styles.panel}>
          <h2 style={styles.h2}>
            Live · {timeline.sessionId}
            <button style={styles.smallButton} onClick={() => void stopWatching()}>
              Stop
            </button>
          </h2>
          <ol style={styles.timeline}>
            {timeline.events.map((event, index) => (
              <li key={`${event.transport_seq}-${index}`} style={styles.listItem}>
                <span style={styles.seq}>#{event.transport_seq}</span>
                <span style={styles.kind}>{event.kind}</span>
                <span style={styles.muted}>
                  {event.ordinal !== null ? `ordinal ${event.ordinal}` : 'no ordinal'}
                  {event.prefix_boundary_exempt ? ' · exempt' : ''}
                </span>
                <code style={styles.payload}>{summarize(event)}</code>
              </li>
            ))}
          </ol>
        </section>
      )}
    </main>
  );
}

/** A bounded, readable summary: the wire can carry far more than a row shows. */
function summarize(event: WireEvent): string {
  const payload = event.payload as Record<string, unknown> | null;
  if (!payload || typeof payload !== 'object') return String(payload);
  if (event.kind === 'snapshot') {
    const messages = Array.isArray(payload.messages) ? payload.messages.length : 0;
    return `high_water ${payload.high_water} · ${messages} messages · runs ${
      Array.isArray(payload.runs) ? payload.runs.length : 0
    } · drafts ${Array.isArray(payload.drafts) ? payload.drafts.length : 0}`;
  }
  const id = payload.id ?? payload.request_id ?? payload.run_id;
  return id ? String(id) : JSON.stringify(payload).slice(0, 120);
}

const styles: Record<string, React.CSSProperties> = {
  main: {
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
    background: '#101418',
    color: '#e6edf3',
    minHeight: '100vh',
    margin: 0,
    padding: '20px 24px',
    fontSize: 13,
  },
  header: { display: 'flex', alignItems: 'center', gap: 16, marginBottom: 18 },
  title: { fontSize: 16 },
  status: { color: '#8b98a5', marginRight: 'auto' },
  button: {
    background: '#1f6feb',
    color: '#fff',
    border: 'none',
    borderRadius: 6,
    padding: '6px 12px',
    cursor: 'pointer',
  },
  smallButton: {
    background: '#21262d',
    color: '#e6edf3',
    border: '1px solid #30363d',
    borderRadius: 5,
    padding: '2px 8px',
    cursor: 'pointer',
    marginLeft: 8,
  },
  panel: {
    border: '1px solid #30363d',
    borderRadius: 8,
    padding: '12px 16px',
    marginBottom: 14,
    background: '#0d1117',
  },
  h2: { fontSize: 13, margin: '0 0 10px', display: 'flex', alignItems: 'center' },
  dl: { display: 'grid', gridTemplateColumns: '110px 1fr', gap: '4px 12px', margin: 0 },
  list: { listStyle: 'none', margin: 0, padding: 0 },
  timeline: { listStyle: 'none', margin: 0, padding: 0, maxHeight: 380, overflowY: 'auto' },
  listItem: {
    display: 'flex',
    alignItems: 'center',
    gap: 10,
    padding: '3px 0',
    borderBottom: '1px solid #161b22',
  },
  seq: { color: '#6e7681', minWidth: 44 },
  kind: { color: '#7ee787', minWidth: 150 },
  muted: { color: '#8b98a5' },
  problem: { color: '#ff7b72', border: '1px solid #6e2a26', background: '#2d1214', padding: '8px 12px', borderRadius: 6 },
  payload: { color: '#a5d6ff', marginLeft: 'auto', maxWidth: '48%', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
};
