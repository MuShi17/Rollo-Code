/**
 * Resolve the Python sidecar that runs the host.
 *
 * A renderer must never supply this: the value is read once in the main process
 * from the environment or a known development layout, validated, and then used
 * for every spawn.  When it cannot be resolved the caller gets an actionable
 * message (which setting to fix) instead of a stack trace.
 */

import { existsSync } from 'node:fs';
import { join } from 'node:path';

export interface SidecarResolution {
  python: string | null;
  /** Why the resolution failed, phrased for a user to act on. */
  problem: string | null;
}

/** Environment variable that pins the interpreter, mirroring the CLI's contract. */
export const PYTHON_ENV = 'ROLLO_PYTHON';

/**
 * Extra import path for the host process.
 *
 * A packaged application finds `rollo` because it is installed in the pinned
 * interpreter.  A development checkout is often run without installing, so the
 * shell accepts an explicit import path instead of guessing at one: a guess
 * would be a heuristic that silently disagrees with how the runtime is actually
 * installed.
 */
export const PYTHONPATH_ENV = 'ROLLO_PYTHONPATH';

/**
 * The `HostClient` option the import path belongs in.
 *
 * The key is `pythonPath`, not `PYTHONPATH`: this object is spread into
 * `HostClientOptions`, and a spread is not subject to excess-property checking.
 * Returning the environment variable's own uppercase name here would produce an
 * option that type-checks and is silently never read.
 */
export function hostEnvironment(env: NodeJS.ProcessEnv = process.env): { pythonPath?: string } {
  const extra = env[PYTHONPATH_ENV];
  if (!extra || !extra.trim()) return {};
  return { pythonPath: extra.trim() };
}

/**
 * Absolute paths a development checkout may keep an interpreter at.
 *
 * `repoRoot` is the checkout this desktop directory lives in; a venv beside it
 * is the layout the benchmark and the CLI already use.
 */
function devCandidates(repoRoot: string): string[] {
  return [
    join(repoRoot, '.venv', 'Scripts', 'python.exe'),
    join(repoRoot, '.venv', 'bin', 'python3'),
    join(repoRoot, '.venv', 'bin', 'python'),
  ];
}

export function resolveSidecarPython(
  repoRoot: string,
  env: NodeJS.ProcessEnv = process.env,
): SidecarResolution {
  const pinned = env[PYTHON_ENV];
  if (pinned && pinned.trim()) {
    const value = pinned.trim();
    if (!existsSync(value)) {
      return {
        python: null,
        problem: `${PYTHON_ENV} points at ${value}, which does not exist`,
      };
    }
    return { python: value, problem: null };
  }

  for (const candidate of devCandidates(repoRoot)) {
    if (existsSync(candidate)) return { python: candidate, problem: null };
  }

  return {
    python: null,
    problem:
      `no Python interpreter for the host was found. Set ${PYTHON_ENV} to the ` +
      `interpreter that has the rollo package installed, or create a virtual ` +
      `environment at ${join(repoRoot, '.venv')}.`,
  };
}
