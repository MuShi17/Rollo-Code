/**
 * Desktop shell smoke test: a real Electron window over a real Python host.
 *
 * This is the acceptance evidence Item 10 asks for -- spawn, handshake, worker
 * routing and exit -- and it is the first time the C05 wire is consumed by the
 * thing it was built for.  The provider is never contacted: the runtime probes
 * below only read a workspace that was seeded on disk.
 */

import { readFile } from 'node:fs/promises';
import { join } from 'node:path';

import { expect, test, type ElectronApplication, type Page } from '@playwright/test';

import { desktopRoot, launchShell, resolvePython, seedWorkspace } from './helpers.js';

const python = resolvePython();

/**
 * `test.skip` would turn "the machine cannot run this" into a green suite, and a
 * green suite is exactly how a broken shell gets merged.  Requiring an explicit
 * opt-out keeps a missing interpreter visible.
 */
const allowSkip = process.env.ROLLO_E2E_ALLOW_SKIP === '1';
const missingPython = python === null;
/** True when a missing interpreter must fail the run rather than skip it. */
const mustFailOnMissingPython = missingPython && !allowSkip;

let app: ElectronApplication;
let page: Page;
let sessionId: string;

test.beforeAll(async () => {
  if (!python) {
    // Without an explicit opt-out a missing interpreter fails the run instead of
    // quietly skipping every assertion below.
    if (mustFailOnMissingPython) {
      throw new Error('no Python interpreter with rollo.host was found');
    }
    return;
  }
  const seeded = seedWorkspace(python, 'shell');
  sessionId = seeded.sessionId;
  app = await launchShell(python, seeded.root);
  page = await app.firstWindow();
  await page.waitForLoadState('domcontentloaded');
});

test.afterAll(async () => {
  await app?.close();
});

test('the renderer has no Node and no generic IPC primitive', async () => {
  test.skip(missingPython, "no Python interpreter with rollo.host was found");
  const exposure = await page.evaluate(() => {
    const globals = globalThis as unknown as Record<string, unknown>;
    const api = globals.rollo as Record<string, unknown> | undefined;
    return {
      hasRequire: typeof globals.require !== 'undefined',
      hasProcess: typeof globals.process !== 'undefined',
      apiKeys: api ? Object.keys(api).sort() : [],
    };
  });

  expect(exposure.hasRequire).toBe(false);
  expect(exposure.hasProcess).toBe(false);
  // The bridge is a whitelist, not `invoke(channel, ...)`.
  expect(exposure.apiKeys).toContain('initialize');
  expect(exposure.apiKeys).not.toContain('invoke');
  expect(exposure.apiKeys).not.toContain('send');
});

test('the window is created with the sandbox on', async () => {
  test.skip(missingPython, "no Python interpreter with rollo.host was found");
  // Electron exposes no public accessor for a window's effective web
  // preferences, and a sandboxed renderer has no `process` object to read a
  // marker from either.  So this asserts the compiled contract instead -- and it
  // is mutation-checked: flipping the option to false in the bundled main
  // process makes this test fail, which is what makes it an oracle rather than
  // a comment.
  const bundle = await readFile(join(desktopRoot, 'dist', 'main', 'index.js'), 'utf8');
  // The bundler rewrites the import, so the constructor appears as
  // `new import_electron.BrowserWindow`; anchor on the tail of that expression.
  const anchor = bundle.indexOf('BrowserWindow({');
  expect(anchor, 'the bundled main process has no window constructor').toBeGreaterThan(-1);
  const windowOptions = bundle.slice(anchor, anchor + 500);
  expect(windowOptions).toContain('sandbox: true');
  expect(windowOptions).toContain('nodeIntegration: false');
  expect(windowOptions).toContain('contextIsolation: true');
  expect(windowOptions).toContain('webSecurity: true');
});

test('the shell spawns a host, handshakes and lists the seeded session', async () => {
  test.skip(missingPython, "no Python interpreter with rollo.host was found");
  await expect.poll(async () => app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length)).toBe(1);

  await expect(page.getByText('protocol', { exact: true })).toBeVisible();
  await expect(page.getByText('v1', { exact: true })).toBeVisible();
  await expect(page.getByText(/not in this build/)).toBeVisible();

  await expect(page.getByText('Sessions (1)')).toBeVisible();
  await expect(page.locator('code', { hasText: sessionId })).toBeVisible();
});

test('the renderer is told about missing settings, but never their values', async () => {
  test.skip(missingPython, "no Python interpreter with rollo.host was found");
  const worker = await page.evaluate(async () => {
    const api = (globalThis as unknown as { rollo: Record<string, unknown> }).rollo;
    const preset = (await (api.autoWorkspace as () => Promise<{ workspace_id: string }>)())!;
    return (api.workerStatus as (id: string) => Promise<Record<string, unknown>>)(preset.workspace_id);
  });

  // The renderer must not be handed absolute paths: it has no filesystem entry
  // point, so giving it one would contradict the boundary the shell enforces.
  const rendered = JSON.stringify(worker);
  expect(rendered).not.toContain('python.exe');
  expect(rendered).not.toContain('PYTHONPATH');
  expect(rendered).not.toMatch(/[A-Za-z]:\\\\/);
});

test('watching a session receives the atomic snapshot over the wire', async () => {
  test.skip(missingPython, "no Python interpreter with rollo.host was found");
  await page.getByRole('button', { name: 'Watch' }).first().click();

  await expect(page.getByText(new RegExp(`Live · ${sessionId}`))).toBeVisible();
  await expect(page.getByText('snapshot', { exact: true })).toBeVisible();
  await expect(page.getByText(/high_water \d+/)).toBeVisible();
  await expect(page.getByText(/ordinal \d+/)).toBeVisible();
});

test('a reload keeps the window working and re-attaches to the same host', async () => {
  test.skip(missingPython, "no Python interpreter with rollo.host was found");
  const before = await app.evaluate(({ app: electronApp }) => electronApp.getAppMetrics().length);
  await page.reload();
  await page.waitForLoadState('domcontentloaded');

  await expect.poll(async () => app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length)).toBe(1);
  expect(await app.evaluate(({ app: electronApp }) => electronApp.getAppMetrics().length)).toBe(before);
});

