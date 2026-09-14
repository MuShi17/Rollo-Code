/**
 * Electron main process: one window, one registry of workspace hosts.
 *
 * The shell is a single-instance application (the default shape for a desktop
 * tool): a second launch focuses the existing window instead of starting a
 * second set of workers.  Multi-session concurrency happens *inside* one
 * workspace host, which is where the runtime already provides it.
 */

import { app, BrowserWindow } from 'electron';
import { join } from 'node:path';

import { CHANNELS, type WireEvent, type WorkerState } from '../shared/wire.js';
import { registerIpc, type IpcContext } from './ipc.js';
import { WorkspaceRegistry } from './workspaces.js';

/** Repository root: desktop/dist/main -> desktop/dist -> desktop -> repo. */
const repoRoot = join(__dirname, '..', '..', '..');

const windows: BrowserWindow[] = [];
/** Only our own renderer origins may drive the runtime. */
const allowedOrigins = new Set<string>();
let closing = false;

function broadcast(channel: string, payload: unknown): void {
  for (const window of windows) {
    if (!window.isDestroyed()) window.webContents.send(channel, payload);
  }
}

const registry = new WorkspaceRegistry({
  repoRoot,
  onEvent: (workspaceId: string, event: WireEvent) =>
    broadcast(CHANNELS.event, { workspace_id: workspaceId, event }),
  onState: (workspaceId: string, state: WorkerState) =>
    broadcast(CHANNELS.workerState, { workspace_id: workspaceId, state }),
});

function createWindow(): BrowserWindow {
  const window = new BrowserWindow({
    width: 1180,
    height: 800,
    show: false,
    backgroundColor: '#101418',
    title: 'Rollo Code',
    webPreferences: {
      // The renderer gets no Node, no remote module and a narrow preload bridge.
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      preload: join(__dirname, '..', 'preload', 'index.js'),
    },
  });

  window.once('ready-to-show', () => window.show());
  // The window never navigates away and never opens a second window.
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', (event) => event.preventDefault());
  return window;
}

async function loadRenderer(window: BrowserWindow, devServerUrl?: string): Promise<void> {
  if (devServerUrl) {
    allowedOrigins.add(new URL(devServerUrl).origin);
    await window.loadURL(devServerUrl);
    return;
  }
  allowedOrigins.add('file://');
  await window.loadFile(join(__dirname, '..', 'renderer', 'index.html'));
}

function installIpc(): void {
  const context: IpcContext = {
    registry,
    allowedOrigins,
    windows: () => windows,
    emitEvent: (workspaceId, event) =>
      broadcast(CHANNELS.event, { workspace_id: workspaceId, event }),
    emitState: (workspaceId, state) =>
      broadcast(CHANNELS.workerState, { workspace_id: workspaceId, state }),
  };
  registerIpc(context);
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    const [window] = windows;
    if (!window) return;
    if (window.isMinimized()) window.restore();
    window.focus();
  });

  void app.whenReady().then(async () => {
    installIpc();

    // A native directory dialog cannot be driven by automation, so an explicitly
    // configured workspace is registered at start-up instead.  It becomes the
    // same registry entry the picker would create; nothing else differs.
    const preset = process.env.ROLLO_AUTO_WORKSPACE;
    let presetId: string | null = null;
    if (preset && preset.trim()) {
      try {
        presetId = registry.add(preset.trim()).workspace_id;
      } catch (error) {
        console.error(`[desktop] ignoring ROLLO_AUTO_WORKSPACE: ${String(error)}`);
      }
    }

    const window = createWindow();
    windows.push(window);
    window.on('closed', () => {
      const index = windows.indexOf(window);
      if (index >= 0) windows.splice(index, 1);
    });
    await loadRenderer(window, process.env.ROLLO_RENDERER_URL);
    if (presetId) window.webContents.send(CHANNELS.autoWorkspace, { workspace_id: presetId });

    app.on('activate', () => {
      if (windows.length > 0) return;
      const created = createWindow();
      windows.push(created);
      void loadRenderer(created, process.env.ROLLO_RENDERER_URL);
    });
  });

  app.on('window-all-closed', () => app.quit());

  // Hosts are stopped before the app actually leaves, so no Python child is
  // orphaned; the quit is re-issued once they are gone.
  app.on('before-quit', (event) => {
    if (closing) return;
    event.preventDefault();
    void (async () => {
      await registry.closeAll();
      closing = true;
      app.quit();
    })();
  });
}
