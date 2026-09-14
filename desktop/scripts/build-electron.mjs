/**
 * Bundle the Electron entry points.
 *
 * They must be single self-contained CommonJS files.  A sandboxed preload runs
 * in a bundle environment with no module resolver, so a require of a sibling
 * file fails at load time and the bridge simply never appears; splitting the
 * main process across files has the same shape of problem for no benefit.
 *
 * `tsc` still owns type checking -- this script only produces the artifacts.
 */

import { build } from 'esbuild';

const shared = {
  bundle: true,
  platform: 'node',
  format: 'cjs',
  target: 'node20',
  sourcemap: true,
  // Electron is provided by the runtime, never bundled.
  external: ['electron'],
  logLevel: 'info',
};

await build({ ...shared, entryPoints: ['src/main/index.ts'], outfile: 'dist/main/index.js' });
await build({ ...shared, entryPoints: ['src/preload/index.ts'], outfile: 'dist/preload/index.js' });
console.log('build-electron: bundled main and preload');
