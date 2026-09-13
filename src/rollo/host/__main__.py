"""Entry point: ``python -m rollo.host --workspace <dir> [--runtime-dir <dir>]``.

The desktop main process starts this with an absolute sidecar path, a fixed
cwd, an argument array and ``shell=false``; the host therefore accepts only
explicit arguments and never parses a shell string.  It initialises the runtime
context and speaks the protocol -- it does not start the CLI REPL.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


def _configure_stdio() -> None:
    """Pipes carry the protocol; make their encoding explicit and UTF-8.

    The host's stdout is a pipe, not a console, so the console code page is
    irrelevant here -- but the default locale encoding on a CP936 system is gbk,
    which cannot represent the full protocol payload.  The wire is specified as
    UTF-8, so it is pinned unconditionally.
    """

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError, ValueError):
            pass


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="rollo.host", add_help=True)
    parser.add_argument(
        "--workspace",
        required=True,
        help="workspace root the host serves; resolved to an absolute path",
    )
    parser.add_argument(
        "--runtime-dir",
        default=None,
        help="runtime data directory; defaults to the workspace context default",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    from .server import HostServer, build_context

    _configure_stdio()
    args = _parse_args(argv)
    workspace = Path(args.workspace).expanduser()
    if not workspace.is_dir():
        sys.stderr.write(f"[host] workspace is not a directory: {workspace}\n")
        return 2
    context = build_context(str(workspace), args.runtime_dir)
    server = HostServer(context)
    try:
        return asyncio.run(server.serve())
    except KeyboardInterrupt:
        # Ctrl+C is an explicit shutdown intent; the loop unwinds through
        # ``serve`` so subscriptions and the control store are closed first.
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
