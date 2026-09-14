/**
 * Mutation-verify the two new e2e oracles.
 *
 * Negative control first: the suite must be green unmutated, otherwise a red
 * result says nothing about the mutation.  Files are restored byte-for-byte.
 */

import { spawnSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const desktop = join(dirname(fileURLToPath(import.meta.url)), '..');
const INDEX = join(desktop, 'src', 'main', 'index.ts');
const WORKSPACES = join(desktop, 'src', 'main', 'workspaces.ts');
const NPM = 'D:/nodejs/npm.cmd';
const NPX = 'D:/nodejs/npx.cmd';

function run(command, args) {
  return spawnSync(command, args, { cwd: desktop, encoding: 'utf8', shell: true });
}

function build() {
  const out = run(NPM, ['run', 'build']);
  if (out.status !== 0) throw new Error(`build failed: ${(out.stderr ?? '').slice(0, 400)}`);
}

function playwright() {
  const out = run(NPX, ['playwright', 'test']);
  const text = `${out.stdout ?? ''}${out.stderr ?? ''}`;
  const summary = (text.match(/\d+ (?:passed|failed)[^\n]*/g) ?? []).join(' | ');
  const failing = (text.match(/^\s+x\s+\d+ [^\n]*/gm) ?? []).map((line) => line.trim().slice(0, 100));
  return { code: out.status ?? 1, summary, failing };
}

const MUTANTS = [
  {
    name: 'sandbox-off (expect the sandbox oracle to fail)',
    file: INDEX,
    old: '      sandbox: true,\n',
    new: '      sandbox: false,\n',
  },
  {
    name: 'no-in-flight-dedup (expect the duplicate-host oracle to fail)',
    file: WORKSPACES,
    old: `    const inFlight = this.connecting.get(workspaceId);
    if (inFlight) return inFlight;

    const attempt = this.connectOnce(entry).finally(() => this.connecting.delete(workspaceId));
    this.connecting.set(workspaceId, attempt);
    return attempt;`,
    new: '    return this.connectOnce(entry);',
  },
];

build();
const control = playwright();
console.log(`[CONTROL] unmutated: exit=${control.code}  ${control.summary}`);
if (control.code !== 0) {
  console.log(`HARNESS BROKEN: control is not green\n${control.failing.join('\n')}`);
  process.exit(2);
}

for (const mutant of MUTANTS) {
  const raw = readFileSync(mutant.file);
  const text = raw.toString('utf8');
  const matches = text.split(mutant.old).length - 1;
  if (matches !== 1) {
    console.log(`[HARNESS-ERROR] ${mutant.name}: anchor matched ${matches} times`);
    continue;
  }
  try {
    writeFileSync(mutant.file, text.replace(mutant.old, mutant.new), 'utf8');
    build();
    const result = playwright();
    const verdict = result.code !== 0 ? 'DETECTED' : 'NOT-DETECTED';
    console.log(`[${verdict}] ${mutant.name}`);
    console.log(`    ${result.summary}`);
    for (const line of result.failing) console.log(`    ${line}`);
  } finally {
    writeFileSync(mutant.file, raw);
  }
}

build();
const restored = playwright();
console.log(`[RESTORED] exit=${restored.code}  ${restored.summary}`);
