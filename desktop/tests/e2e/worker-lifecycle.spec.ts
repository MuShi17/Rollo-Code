/**
 * Worker lifecycle: one host per workspace, and no host left behind.
 *
 * These are the checks the delivery suite was missing.  A second host is
 * invisible to every behavioural assertion -- the UI still shows one window and
 * one session -- so only the OS process table can see it.  The first version of
 * this suite shipped without that check, and a duplicate host went unnoticed
 * until an adversarial review counted processes.
 */

import { spawnSync } from 'node:child_process';

import { expect, test } from '@playwright/test';

import { launchShell, resolvePython, seedWorkspace } from './helpers.js';

const python = resolvePython();

interface HostProcess {
  pid: number;
  command: string;
}

/**
 * Every python process running the host sidecar.
 *
 * The PowerShell text carries no interpolated path, only a plain wildcard, so
 * the count cannot be broken by quoting -- an earlier probe of mine returned
 * zero for exactly that reason and made a real duplication look clean.
 */
function hostProcesses(): HostProcess[] {
  const script =
    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | " +
    "Where-Object { $_.CommandLine -like '*rollo.host*' } | " +
    'ForEach-Object { "$($_.ProcessId)`t$($_.CommandLine)" }';
  const out = spawnSync('powershell.exe', ['-NoProfile', '-Command', script], {
    encoding: 'utf8',
    windowsHide: true,
  });
  return (out.stdout || '')
    .split(/\r?\n/)
    .filter((line) => line.includes('\t'))
    .map((line) => {
      const [pid, command] = line.split('\t');
      return { pid: Number.parseInt(pid!, 10), command: command ?? '' };
    })
    .filter((entry) => Number.isFinite(entry.pid));
}

function hostsFor(workspace: string): HostProcess[] {
  return hostProcesses().filter((entry) => entry.command.includes(workspace));
}

test('one workspace gets one host, even when connect races the handshake', async () => {
  test.skip(!python, 'no Python interpreter with the host module was found');
  const { root } = seedWorkspace(python!, 'dupe');
  const app = await launchShell(python!, root);
  try {
    const page = await app.firstWindow();
    await page.waitForLoadState('domcontentloaded');

    // Drive connect three times concurrently on a workspace that is still
    // handshaking: the race window is roughly a second wide.
    await page.evaluate(async () => {
      const api = (globalThis as unknown as { rollo: Record<string, unknown> }).rollo;
      const preset = (await (api.autoWorkspace as () => Promise<{ workspace_id: string }>)())!;
      await Promise.all([
        (api.initialize as (id: string) => Promise<unknown>)(preset.workspace_id),
        (api.initialize as (id: string) => Promise<unknown>)(preset.workspace_id),
        (api.listSessions as (id: string) => Promise<unknown>)(preset.workspace_id),
      ]);
    });

    await expect.poll(() => hostsFor(root).length, { timeout: 30000 }).toBe(1);
    // And it stays one: a bounded settle, not a lucky instant.
    await page.waitForTimeout(2500);
    expect(hostsFor(root)).toHaveLength(1);
  } finally {
    await app.close();
  }
});

test('closing the application leaves no host behind', async () => {
  test.skip(!python, 'no Python interpreter with the host module was found');
  const { root } = seedWorkspace(python!, 'exit');
  const app = await launchShell(python!, root);
  const page = await app.firstWindow();
  await page.waitForLoadState('domcontentloaded');
  await expect.poll(() => hostsFor(root).length, { timeout: 30000 }).toBe(1);

  await app.close();
  await expect.poll(() => hostsFor(root).length, { timeout: 30000 }).toBe(0);
});
