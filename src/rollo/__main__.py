"""CLI 入口点和交互式 REPL — 镜像 cli.ts。"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import uuid
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()
from .agent import Agent, DEFAULT_THINKING_EFFORT
from .application import Application
from .project_context import ProjectContext, ProjectContextError, WorkspaceNotFoundError
from .runtime_ports import set_diagnostic_sink
from .runtime_ports import NullOutputPort
from .interactions import DenyingInteractionPort
from .tui_adapter import TerminalInteractionPort, TerminalOutputPort
from .ui import print_welcome, print_user_prompt, print_error, print_info, print_plan_for_approval, print_plan_approval_options, print_diagnostic
from .session import (
    CanonicalRecoveryError,
    get_latest_session_id,
    list_canonical_runtime_sessions,
    list_runtime_store_paths,
    load_session,
    runtime_data_dir,
    runtime_store_path,
)
from .runtime_store import SQLiteRuntimeStore
from .recovery import RecoveryProjection
from .artifact_archive import ArtifactArchive
from .memory import list_memories
from .skills import discover_skills, resolve_skill_prompt, get_skill_by_name, execute_skill


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="rollo",
        description="Rollo Code — a minimal coding agent",
        add_help=False,
    )
    parser.add_argument("prompt", nargs="*", help="One-shot prompt")
    parser.add_argument("--yolo", "-y", action="store_true", help="Skip all confirmation prompts")
    parser.add_argument("--plan", action="store_true", help="Plan mode: read-only")
    parser.add_argument("--accept-edits", action="store_true", help="Auto-approve file edits")
    parser.add_argument("--dont-ask", action="store_true", help="Auto-deny confirmations (for CI)")
    parser.add_argument(
        "--thinking",
        dest="thinking",
        action="store_true",
        default=None,
        help="Enable extended thinking",
    )
    parser.add_argument(
        "--no-thinking",
        dest="thinking",
        action="store_false",
        help="Disable extended thinking",
    )
    parser.add_argument(
        "--thinking-effort",
        choices=("none", "low", "high", "max"),
        default=None,
        help="Thinking effort: none, low, high, or max (default: max)",
    )
    parser.add_argument("--model", "-m", default=None, help="Model to use")
    parser.add_argument("--api-base", default=None, help="OpenAI-compatible API base URL")
    parser.add_argument("--resume", action="store_true", help="Resume last session")
    parser.add_argument("--list", dest="list_sessions", action="store_true", help="List local sessions")
    parser.add_argument("--latest", action="store_true", help="Show the latest local session")
    parser.add_argument("--max-cost", type=float, default=None, help="Max USD spend")
    parser.add_argument("--max-turns", type=int, default=None, help="Max agentic turns")
    parser.add_argument("--help", "-h", action="store_true", help="Show help")
    return parser.parse_args()


def _resolve_permission_mode(args: argparse.Namespace) -> str:
    if args.yolo:
        return "bypassPermissions"
    if args.plan:
        return "plan"
    if args.accept_edits:
        return "acceptEdits"
    if args.dont_ask:
        return "dontAsk"
    return "default"


def _open_latest_canonical_store() -> tuple[SQLiteRuntimeStore | None, str | None]:
    """Open the newest session store, conservatively rejecting corruption."""

    candidates: list[tuple[Path, SQLiteRuntimeStore, str]] = []
    failures: list[tuple[Path, Exception]] = []
    for database in list_runtime_store_paths():
        store: SQLiteRuntimeStore | None = None
        try:
            store = SQLiteRuntimeStore(database)
            session_id = get_latest_session_id(runtime_store=store)
            if session_id:
                candidates.append((database, store, session_id))
            else:
                store.close()
        except Exception as error:
            if store is not None:
                store.close()
            failures.append((database, error))

    if failures:
        for _, store, _ in candidates:
            store.close()
        path, error = failures[0]
        raise CanonicalRecoveryError(
            f"canonical runtime store classified as corrupt: {error}", path=path
        ) from error
    if not candidates:
        return None, None

    # list_runtime_store_paths is newest-first, so the first canonical
    # candidate is the resume target.  Other stores remain untouched and are
    # closed without being rewritten.
    _, selected, session_id = candidates[0]
    for _, store, _ in candidates[1:]:
        store.close()
    return selected, session_id


async def run_repl(agent: Agent, *, application: Application | None = None) -> None:
    """Interactive REPL loop."""

    # 终端交互统一经适配器：阻塞 input 在独立线程中执行、不阻塞事件循环，
    # 且取消可以解除等待。**不再注册 `confirm_fn`** —— 它内部的同步 input
    # 会优先于端口，使端口收不到任何请求（复核 N-12）。
    terminal_port = TerminalInteractionPort()
    agent.set_interaction_port(terminal_port)
    if application is not None:
        # The REPL owns the concrete terminal adapter; the Application remains
        # the lifecycle boundary and receives that adapter explicitly.
        application.interaction_port = terminal_port

    active_run_id: str | None = None

    async def application_chat(prompt: str) -> None:
        nonlocal active_run_id
        if application is None:
            await agent.chat(prompt)
            return
        started = await application.run_start(
            session_id=agent.session_id,
            prompt=prompt,
            command_id=f"repl-{agent.session_id}-{uuid.uuid4().hex}",
        )
        if started.error_code:
            raise RuntimeError(started.error_code)
        active_run_id = started.run_id
        try:
            await application.wait_run(started.run_id or "")
        finally:
            active_run_id = None

    async def plan_approval_fn(plan_content: str) -> dict:
        print_plan_for_approval(plan_content)
        print_plan_approval_options()
        while True:
            try:
                choice = input("  Enter choice (1-4): ").strip()
            except EOFError:
                return {"choice": "manual-execute"}
            if choice == "1":
                return {"choice": "clear-and-execute"}
            elif choice == "2":
                return {"choice": "execute"}
            elif choice == "3":
                return {"choice": "manual-execute"}
            elif choice == "4":
                try:
                    feedback = input("  Feedback (what to change): ").strip()
                except EOFError:
                    feedback = ""
                return {"choice": "keep-planning", "feedback": feedback or None}
            else:
                print("  Invalid choice. Enter 1, 2, 3, or 4.")

    agent.set_plan_approval_fn(plan_approval_fn)

    sigint_count = 0

    def handle_sigint(sig, frame):
        nonlocal sigint_count
        if agent.is_processing:
            # Agent is processing
            if application is not None and active_run_id is not None:
                asyncio.create_task(
                    application.run_cancel(
                        run_id=active_run_id,
                        command_id=f"repl-cancel-{uuid.uuid4().hex}",
                    )
                )
            else:
                agent.abort()
            print("\n  (interrupted)")
            sigint_count = 0
            print_user_prompt()
        else:
            sigint_count += 1
            if sigint_count >= 2:
                print("\nBye!\n")
                sys.exit(0)
            print("\n  Press Ctrl+C again to exit.")
            print_user_prompt()

    signal.signal(signal.SIGINT, handle_sigint)
    print_welcome()

    while True:
        print_user_prompt()
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!\n")
            break

        inp = line.strip()
        sigint_count = 0

        if not inp:
            continue
        if inp in ("exit", "quit"):
            print("\nBye!\n")
            break

        # REPL commands
        if inp == "/clear":
            agent.clear_history()
            continue
        if inp == "/plan":
            agent.toggle_plan_mode()
            continue
        if inp == "/cost":
            agent.show_cost()
            continue
        if inp == "/compact":
            try:
                await agent.compact()
            except Exception as e:
                print_error(str(e))
            continue
        if inp == "/memory":
            memories = list_memories(agent.context)
            if not memories:
                print_info("No memories saved yet.")
            else:
                print_info(f"{len(memories)} memories:")
                for m in memories:
                    print(f"    [{m.type}] {m.name} — {m.description}")
            continue
        if inp == "/skills":
            skills = discover_skills(agent.context)
            if not skills:
                print_info("No skills found. Add skills to .rollo/skills/<name>/SKILL.md")
            else:
                print_info(f"{len(skills)} skills:")
                for s in skills:
                    tag = f"/{s.name}" if s.user_invocable else s.name
                    print(f"    {tag} ({s.source}) — {s.description}")
            continue

        # Skill invocation: /<skill-name> [args]
        if inp.startswith("/"):
            space_idx = inp.find(" ")
            cmd_name = inp[1:space_idx] if space_idx > 0 else inp[1:]
            cmd_args = inp[space_idx + 1:] if space_idx > 0 else ""
            skill = get_skill_by_name(cmd_name, agent.context)
            if skill and skill.user_invocable:
                print_info(f"Invoking skill: {skill.name}")
                try:
                    if skill.context == "fork":
                        result = execute_skill(skill.name, cmd_args, agent.context)
                        if result:
                            await application_chat(f'Use the skill tool to invoke "{skill.name}" with args: {cmd_args or "(none)"}')
                    else:
                        resolved = resolve_skill_prompt(skill, cmd_args)
                        await application_chat(resolved)
                except Exception as e:
                    if "abort" not in str(e).lower():
                        print_error(str(e))
                continue

        # Normal chat
        try:
            await application_chat(inp)
        except Exception as e:
            if "abort" not in str(e).lower():
                print_error(str(e))


async def _run_repl_with_cleanup(agent: Agent) -> None:
    """Run the REPL and close Agent-owned resources on every exit path."""

    if hasattr(agent, "session_id") and hasattr(agent, "configure_application_interactions"):
        context = agent.context
        if getattr(agent, "_runtime_store", None) is None and hasattr(agent, "configure_runtime_store"):
            agent.configure_runtime_store(SQLiteRuntimeStore(runtime_store_path(agent.session_id, context=context)))
        application = Application(
            context,
            output_port=getattr(agent, "output_port", NullOutputPort()),
            interaction_port=getattr(agent, "interaction_port", DenyingInteractionPort()),
            existing_agent=agent,
        )
        # The canonical runtime file may already exist for this CLI-created
        # session.  Registering the explicit session in the C03 control index
        # is the deliberate migration boundary; it is not implicit adoption
        # of an arbitrary inspect-only legacy database.
        application.session_create(agent.session_id)
        try:
            await run_repl(agent, application=application)
        finally:
            await application.shutdown()
        return
    try:
        await run_repl(agent)
    finally:
        await agent.aclose()


async def _run_one_shot(agent: Agent, prompt: str) -> None:
    """Run one prompt and close Agent-owned resources on success or failure."""

    try:
        await agent.chat(prompt)
    finally:
        await agent.aclose()


async def _run_application_one_shot(agent: Agent, context: ProjectContext, prompt: str) -> None:
    """Run one-shot through the public Application boundary."""

    # Keep lightweight embedding/test doubles compatible with the historical
    # CLI seam; real Agent instances expose the C03 identity configuration API.
    if not hasattr(agent, "session_id") or not hasattr(agent, "configure_application_interactions"):
        await agent.chat(prompt)
        close = getattr(agent, "aclose", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result
        return

    if getattr(agent, "_runtime_store", None) is None and hasattr(agent, "configure_runtime_store"):
        # Preserve the CLI's legacy SESSION_DIR override used by offline
        # consumers; the Application itself still receives an explicit store.
        agent.configure_runtime_store(SQLiteRuntimeStore(runtime_store_path(agent.session_id)))

    app = Application(
        context,
        output_port=getattr(agent, "output_port", NullOutputPort()),
        interaction_port=getattr(agent, "interaction_port", DenyingInteractionPort()),
        existing_agent=agent,
    )
    try:
        application_session = app.session_create(agent.session_id)
        if application_session.error_code:
            raise RuntimeError(application_session.error_code)
        started = await app.run_start(session_id=agent.session_id, prompt=prompt, command_id=f"cli-{agent.session_id}-{os.getpid()}-{uuid.uuid4().hex}")
        # A rejected run.start (quarantined/foreign owner, digest conflict, ...)
        # must fail loudly.  Waiting on an empty run id would otherwise exit 0
        # with no output, which is indistinguishable from a successful run.
        if started.error_code:
            raise RuntimeError(
                f"run.start rejected: {started.error_code} ({started.status})"
            )
        await app.wait_run(started.run_id or "")
    finally:
        await app.shutdown()


def _entry_workspace() -> Path:
    """读取入口的 workspace 根（未指定时为进程当前目录）。

    进程当前目录不可读取时转换为可诊断的 ProjectContext 错误，避免裸 OSError
    冒泡成未处理堆栈（D8）。
    """

    try:
        return Path.cwd()
    except OSError as exc:
        raise WorkspaceNotFoundError(
            f"无法读取进程当前目录（可能已被删除或不可访问）：{exc}"
        ) from exc


def main() -> None:
    args = parse_args()

    # 入口一次性解析 workspace 上下文（D7：未指定时使用进程当前目录）。
    # 解析失败时给出可诊断错误与非零退出码，不打印未处理堆栈（D8）。
    try:
        project_context = ProjectContext.from_root(_entry_workspace())
    except ProjectContextError as exc:
        print_error(f"Cannot resolve workspace: {exc}")
        sys.exit(2)

    # 诊断通道：把 MCP/记忆等后台诊断接到 stderr（不污染 stdout）。
    set_diagnostic_sink(print_diagnostic)

    if args.help:
        print("""
Usage: rollo [options] [prompt]

Options:
  --yolo, -y          Skip all confirmation prompts (bypassPermissions mode)
  --plan              Plan mode: read-only, describe changes without executing
  --accept-edits      Auto-approve file edits, still confirm dangerous shell
  --dont-ask          Auto-deny anything needing confirmation (for CI)
  --thinking          Enable extended thinking
  --no-thinking       Disable extended thinking
  --thinking-effort   Thinking effort: none, low, high, or max (default: max,
                      or ROLLO_THINKING_EFFORT)
  --model, -m         Model to use (default: claude-opus-4-6, or ROLLO_MODEL env)
  --api-base URL      Use OpenAI-compatible API endpoint (key via env var)
  --resume            Resume the last session
  --list              List local sessions without contacting a provider
  --latest            Show the latest local session without contacting a provider
  Runtime logs are always written to the canonical session SQLite store.
  --max-cost USD      Stop when estimated cost exceeds this amount
  --max-turns N       Stop after N agentic turns
  --help, -h          Show this help

REPL commands:
  /clear              Clear conversation history
  /plan               Toggle plan mode (read-only <-> normal)
  /cost               Show token usage and cost
  /compact            Manually compact conversation
  /memory             List saved memories
  /skills             List available skills
  /<skill-name>       Invoke a skill (e.g. /commit "fix types")

Examples:
  rollo "fix the bug in src/app.ts"
  rollo --yolo "run all tests and fix failures"
  rollo --plan "how would you refactor this?"
  rollo --max-cost 0.50 --max-turns 20 "implement feature X"
  OPENAI_API_KEY=sk-xxx rollo --api-base https://aihubmix.com/v1 --model gpt-4o "hello"
  rollo --resume
  rollo  # starts interactive REPL
""")
        sys.exit(0)

    permission_mode = _resolve_permission_mode(args)
    model = (
        args.model
        or os.getenv("ROLLO_MODEL")
        or "claude-opus-4-6"
    )
    api_base = args.api_base
    thinking_effort = (
        args.thinking_effort
        or os.getenv("ROLLO_THINKING_EFFORT")
        or DEFAULT_THINKING_EFFORT
    )
    if args.thinking is False:
        thinking_effort = "none"
    elif args.thinking is True and args.thinking_effort is None:
        # 旧 --thinking 开关显式启用默认强度，但不覆盖显式 --thinking-effort。
        thinking_effort = DEFAULT_THINKING_EFFORT

    # Resolve API config
    resolved_api_base = api_base
    resolved_api_key: str | None = None
    resolved_use_openai = bool(api_base)

    if os.getenv("OPENAI_API_KEY") and os.getenv("OPENAI_BASE_URL"):
        resolved_api_key = os.getenv("OPENAI_API_KEY")
        resolved_api_base = resolved_api_base or os.getenv("OPENAI_BASE_URL")
        resolved_use_openai = True
    elif os.getenv("ANTHROPIC_API_KEY"):
        resolved_api_key = os.getenv("ANTHROPIC_API_KEY")
        resolved_api_base = resolved_api_base or os.getenv("ANTHROPIC_BASE_URL")
        resolved_use_openai = False
    elif os.getenv("OPENAI_API_KEY"):
        resolved_api_key = os.getenv("OPENAI_API_KEY")
        resolved_api_base = resolved_api_base or os.getenv("OPENAI_BASE_URL")
        resolved_use_openai = True

    if not resolved_api_key and api_base:
        resolved_api_key = os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
        resolved_use_openai = True

    resume_store = None
    resume_session_id: str | None = None
    if args.resume:
        try:
            resume_store, resume_session_id = _open_latest_canonical_store()
            if resume_store is not None:
                recovery = RecoveryProjection(
                    artifact_archive=ArtifactArchive(runtime_data_dir() / "artifacts")
                )
                recovered = recovery.recover_startup(resume_store)
                for item in recovered:
                    if item.status in {"corrupt", "uncertain", "unmatched"}:
                        print(f"Recovery {item.status}: {item.run_id} - {item.recommended_action}")
                if any(item.status == "corrupt" for item in recovered):
                    raise CanonicalRecoveryError(
                        "canonical recovery found corrupt event data",
                        path=resume_store.database,
                    )
        except CanonicalRecoveryError as error:
            print_error(f"Canonical recovery blocked ({error.classification}): {error}")
            if resume_store is not None:
                resume_store.close()
            sys.exit(2)

    if args.list_sessions or args.latest:
        try:
            canonical_sessions = list_canonical_runtime_sessions()
        except CanonicalRecoveryError as error:
            print_error(f"Canonical listing blocked ({error.classification}): {error}")
            sys.exit(2)
        sessions = canonical_sessions
        if args.latest:
            if sessions:
                latest = sorted(
                    sessions,
                    key=lambda item: (
                        int(item.get("highWater", 0) or 0),
                        item.get("startTime", ""),
                        item.get("id", ""),
                    ),
                    reverse=True,
                )[0]
                print(f"Latest session: {latest.get('id')} (canonical)")
            else:
                print("No previous sessions found.")
        elif sessions:
            for item in sorted(sessions, key=lambda value: str(value.get("id", ""))):
                print(f"{item.get('id')} (canonical)")
        else:
            print("No previous sessions found.")
        return

    if not resolved_api_key:
        # A resume-only invocation is also a useful offline inspection command.
        # Do not require provider credentials merely to inspect canonical state.
        if args.resume and not args.prompt:
            if resume_session_id:
                print(f"Canonical session available: {resume_session_id}")
            else:
                print("No canonical sessions found.")
            if resume_store is not None:
                resume_store.close()
            return
        print_error(
            "API key is required.\n"
            "  Set ANTHROPIC_API_KEY (+ optional ANTHROPIC_BASE_URL) for Anthropic format,\n"
            "  or OPENAI_API_KEY + OPENAI_BASE_URL for OpenAI-compatible format."
        )
        sys.exit(1)

    agent = Agent(
        permission_mode=permission_mode,
        model=model,
        thinking=args.thinking,
        thinking_effort=thinking_effort,
        max_cost_usd=args.max_cost,
        max_turns=args.max_turns,
        project_context=project_context,
        output_port=TerminalOutputPort(),
        interaction_port=TerminalInteractionPort(),
        api_base=resolved_api_base if resolved_use_openai else None,
        anthropic_base_url=resolved_api_base if not resolved_use_openai else None,
        api_key=resolved_api_key,
        runtime_store=resume_store,
        runtime_session_id=resume_session_id,
    )

    # Resume session
    if args.resume:
        canonical_ids = resume_store.list_session_ids() if resume_store is not None else []
        session_id = resume_session_id if canonical_ids else None
        if session_id:
            session = load_session(
                session_id,
                runtime_store=resume_store,
            )
            if session:
                agent.restore_session({
                    **session,
                    "anthropicMessages": session.get("anthropicMessages"),
                    "openaiMessages": session.get("openaiMessages"),
                })
            else:
                print_info("Canonical session is unavailable; inspect recovery diagnostics before continuing.")
        else:
            print("No previous sessions found.")

    prompt = " ".join(args.prompt) if args.prompt else None

    try:
        if prompt:
            # One-shot mode
            try:
                asyncio.run(_run_application_one_shot(agent, project_context, prompt))
            except Exception as e:
                print_error(str(e))
                sys.exit(1)
        else:
            # Interactive REPL
            asyncio.run(_run_repl_with_cleanup(agent))
    finally:
        # A resumed Store is supplied by the CLI and remains caller-owned by
        # the Agent. Close it only after Agent cleanup has finished.
        if resume_store is not None:
            resume_store.close()


if __name__ == "__main__":
    main()
