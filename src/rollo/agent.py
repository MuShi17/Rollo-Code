"""
Agent 核心循环 — 双后端（Anthropic + OpenAI 兼容）、流式输出、
4 层上下文压缩、Plan Mode、Sub-Agent、预算控制。
实现本地 Coding Agent 的核心运行时架构。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable, Awaitable

import anthropic
import openai

from .tools import (
    tool_definitions,
    execute_tool_value,
    commit_tool_state,
    check_permission,
    CONCURRENCY_SAFE_TOOLS,
    get_active_tool_definitions,
    ToolDef,
    PermissionMode,
)
from .memory import (
    start_memory_prefetch,
    format_memories_for_injection,
    MemoryPrefetch,
)
from .session import runtime_data_dir, runtime_store_path, save_session_v2
from .prompt import build_system_prompt
from .subagent import get_sub_agent_config
from .mcp_client import McpManager
from .interactions import (
    DenyingInteractionPort,
    InteractionError,
    InteractionKind,
    InteractionPort,
    InteractionRegistry,
    InteractionReply,
    InteractionRequest,
    digest_params,
)
from .project_context import ProjectContext
from .runtime_ports import NullOutputPort, OutputEvent, OutputPort, emit_safely
from .event_ids import IdentityFactory, RunContext
from .event_sink import EventSink, RuntimeEventEmitter
from .runtime_event import RuntimeEvent, canonical_json_bytes
from .redaction import redact_payload
from .runtime_lifecycle import DurableToolBoundary, ModelCallRecorder
from .context_transition import (
    ContextReplacement,
    ContextTransition,
    build_context_transition,
    validate_transition_candidate,
)
from .provider_content import (
    display_tool_result,
    materialize_tool_result,
    materialized_content_bytes,
)
from .runtime_store import SQLiteRuntimeStore
from .run_lifecycle import RunStateGuard
from .compaction import CompactionCheckpoint, CompactionCheckpointBuilder, CompactionError
from .projections.base import EventRecord
from .projections.model_replay_projection import ModelReplayProjection, ModelReplayResult
from .projections.incremental_replay import IncrementalModelReplayCursor, IncrementalReplayError
from .projections.provider_context import (
    CanonicalModelContextAdapter,
    ProviderCapacityError,
    ProviderRequestCycle,
    ProviderRequestCycleIdentity,
    provider_request_size_bytes,
)
from .artifact_archive import ArtifactArchive
from .archive_capability import ToolResultArchiveCapability
from .archive_projection import format_terminal_tool_result, project_terminal_tool_result
from .llm_capture import LLMCaptureManager, LLMCapturePolicy

# ─── 指数退避重试 ──────────────────────────────────────────
# 对 429（限流）、503/529（过载）、网络错误进行最多 3 次重试，
# 延迟 = 1s/2s/4s（上限 30s）+ 随机抖动，避免惊群效应。


def _is_retryable(error: Exception) -> bool:
    """判断是否为可重试的 API 错误（限流/过载/网络错误）。"""
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if status in (429, 503, 529):
        return True
    msg = str(error)
    if "overloaded" in msg or "ECONNRESET" in msg or "ETIMEDOUT" in msg:
        return True
    return False


async def _with_retry(fn, max_retries: int = 3, on_retry: Callable[[int, Exception], Any] | None = None):
    """对异步函数 fn 执行指数退避重试。"""
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except Exception as error:
            if attempt >= max_retries or not _is_retryable(error):
                raise
            delay = min(1000 * (2 ** attempt), 30000) / 1000 + (hash(str(time.time())) % 1000) / 1000
            status = getattr(error, "status_code", None) or getattr(error, "status", None)
            reason = f"HTTP {status}" if status else (getattr(error, "code", None) or "network error")
            if on_retry:
                on_retry(attempt + 2, error)
            # 注意：重试事件的发布由调用方的 `on_retry` 负责；本函数是模块级
            # 工具函数，没有 Agent 实例，不得在此引用 `self`（历史遗留会在
            # 该分支触发 NameError，而该分支因 `_is_retryable` 的严格判定
            # 在常规异常下不可达，属潜在崩溃点）。
            await asyncio.sleep(delay)


# ─── 模型上下文窗口大小 ────────────────────────────────────
# 用于判断何时需要触发会话压缩（auto-compact）。

MODEL_CONTEXT = {
    "claude-opus-4-6": 200000,
    "claude-sonnet-4-6": 200000,
    "claude-sonnet-4-20250514": 200000,
    "claude-haiku-4-5-20251001": 200000,
    "claude-opus-4-20250514": 200000,
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000,
}

CONTEXT_WINDOW_USAGE_RATIO = 0.70


def _get_context_window(model: str) -> int:
    return MODEL_CONTEXT.get(model, 200000)


# ─── Thinking（扩展思考）支持检测 ────────────────────────────
# Claude Opus/Sonnet/Haiku 4.x 支持 extended thinking，
# Claude 3.x 系列不支持；Opus 4.6 / Sonnet 4.6 额外支持 adaptive 模式。

THINKING_EFFORTS = ("none", "low", "high", "max")
DEFAULT_THINKING_EFFORT = "max"


def _normalize_thinking_effort(effort: str | None) -> str:
    """规范化思考强度；none 用于显式关闭思考模式。"""
    value = (effort or DEFAULT_THINKING_EFFORT).strip().lower()
    value = {"off": "none", "disabled": "none"}.get(value, value)
    if value not in THINKING_EFFORTS:
        allowed = ", ".join(THINKING_EFFORTS)
        raise ValueError(
            f"Invalid thinking effort {effort!r}; expected one of: {allowed}"
        )
    return value


def _model_supports_thinking(model: str) -> bool:
    """判断模型是否支持扩展思考功能。"""
    m = model.lower()
    if "deepseek" in m or "reasoner" in m:
        return True
    if "claude-3-" in m or "3-5-" in m or "3-7-" in m:
        return False
    if "claude" in m and any(x in m for x in ("opus", "sonnet", "haiku")):
        return True
    if "reasoner" in m or any(name in m for name in ("gpt-5", "o1", "o3", "o4")):
        return True
    return False


class CanonicalFinalizationError(RuntimeError):
    """The run could not durably publish its canonical terminal state."""

    code = "canonical_finalization_failed"


class AgentClosedError(RuntimeError):
    """The Agent session resources have already been closed."""

    code = "agent_closed"


class RuntimeResourceMismatchError(RuntimeError):
    """The canonical runtime facade has inconsistent resource identities."""

    code = "runtime_resource_mismatch"


class ProviderContentNormalizationError(ValueError):
    """A provider returned a text-bearing block with an unsafe value shape."""

    code = "provider_content_normalization_failed"

    def __init__(
        self,
        *,
        provider: str,
        block_kind: str,
        block_index: int,
        value: Any,
    ) -> None:
        self.provider = provider
        self.block_kind = block_kind
        self.block_index = block_index
        self.value_type = _provider_value_type(value)
        # Keep the message useful for diagnosis while deliberately excluding
        # the value itself: compatible providers may return secrets or huge
        # structured payloads in malformed text fields.
        super().__init__(
            "provider content rejected: "
            f"provider={provider} block_kind={block_kind} "
            f"block_index={block_index} value_type={self.value_type}"
        )


def _provider_value_type(value: Any) -> str:
    """Return a stable, payload-free type label for diagnostics."""

    if value is None:
        return "null"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "mapping"
    if isinstance(value, (list, tuple)):
        return "sequence"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return type(value).__name__
    return "object"


def _normalize_provider_text(
    value: Any,
    *,
    provider: str,
    block_kind: str,
    block_index: int,
) -> str:
    """Normalize one provider text field without coercing unknown objects."""

    if isinstance(value, str):
        return value
    raise ProviderContentNormalizationError(
        provider=provider,
        block_kind=block_kind,
        block_index=block_index,
        value=value,
    )


def _model_supports_adaptive_thinking(model: str) -> bool:
    """判断模型是否支持 adaptive thinking（动态调整思考预算）。"""
    m = model.lower()
    return "opus-4-6" in m or "sonnet-4-6" in m


def _get_anthropic_request_max_tokens(model: str) -> int:
    """返回 Anthropic 请求所需的 token envelope，而非模型输出上限。

    Anthropic Messages API 要求请求携带 max_tokens；这里使用模型上下文窗口
    作为协议层预算，不再根据模型名称把可见输出硬编码为 16K/32K/64K。
    模型和服务端仍会执行其自身的上下文及输出能力限制。
    """
    if _is_deepseek_model(model):
        return 1_000_000
    return max(_get_context_window(model), 1)


def _model_supports_openai_reasoning_effort(model: str) -> bool:
    """判断 OpenAI Chat Completions 后端是否应发送 reasoning_effort。"""
    m = model.lower()
    return (
        "deepseek" in m
        or "reasoner" in m
        or any(name in m for name in ("gpt-5", "o1", "o3", "o4"))
    )


def _is_deepseek_model(model: str) -> bool:
    return "deepseek" in model.lower()


def _get_thinking_budget_tokens(effort: str) -> int:
    """为旧版 Anthropic thinking 参数将 effort 映射成 token 预算。

    该预算只控制旧版 thinking 块，不限制最终可见输出长度；新式 adaptive
    和 DeepSeek output_config 模式不使用此映射。
    """
    return {"low": 8192, "high": 16384, "max": 32768}[effort]


def _thinking_request_params(
    model: str,
    effort: str,
    *,
    use_openai: bool,
) -> dict[str, Any]:
    """构造后端对应的思考参数，不为 none 或不支持的模型发送参数。"""
    normalized = _normalize_thinking_effort(effort)
    if normalized == "none":
        if _is_deepseek_model(model):
            return {"thinking": {"type": "disabled"}}
        return {}

    if use_openai:
        if not _model_supports_openai_reasoning_effort(model):
            return {}
        params = {"reasoning_effort": normalized}
        if _is_deepseek_model(model):
            params["thinking"] = {"type": "enabled"}
        return params

    if _model_supports_adaptive_thinking(model):
        return {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": normalized},
        }
    if _is_deepseek_model(model):
        return {
            "thinking": {"type": "enabled"},
            "output_config": {"effort": normalized},
        }
    if _model_supports_thinking(model):
        return {
            "thinking": {
                "type": "enabled",
                "budget_tokens": _get_thinking_budget_tokens(normalized),
            }
        }
    return {}


# ─── 工具转换为 OpenAI 格式 ────────────────────────────────
# Anthropic 和 OpenAI 的工具 schema 格式不同，此函数将 Anthropic 格式
# 的 tool_definitions 转换为 OpenAI function calling 格式。


def _to_openai_tools(tools: list[ToolDef]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


# ─── 多层压缩常量 ──────────────────────────────────────────
# SNIPPABLE_TOOLS：可被裁剪的工具类型（结果为文本，模型可重读）
# SNIP_THRESHOLD：利用率超过 60% 时触发 stale snip
# MICROCOMPACT_IDLE_S：空闲 5 分钟后触发 microcompact（清除旧工具结果）
# KEEP_RECENT_RESULTS：压缩时保留最近 3 条工具结果

SNIPPABLE_TOOLS = {"read_file", "grep_search", "list_files", "run_shell"}
SNIP_PLACEHOLDER = "[Content snipped - re-read if needed]"
SNIP_THRESHOLD = 0.60
MICROCOMPACT_IDLE_S = 5 * 60  # 5 分钟
KEEP_RECENT_RESULTS = 3


# ─── Agent 主类 ────────────────────────────────────────────
# 核心编排类，负责：双后端流式调用、工具执行调度、权限检查、
# 上下文压缩、Plan Mode、Sub-Agent 管理、记忆召回、MCP 集成。


class Agent:
    def __init__(
        self,
        *,
        permission_mode: str = "default",
        model: str = "claude-opus-4-6",
        api_base: str | None = None,
        anthropic_base_url: str | None = None,
        api_key: str | None = None,
        thinking: bool | None = None,
        thinking_effort: str = DEFAULT_THINKING_EFFORT,
        max_cost_usd: float | None = None,
        max_turns: int | None = None,
        confirm_fn: Callable[[str], Awaitable[bool]] | None = None,
        custom_system_prompt: str | None = None,
        custom_tools: list[ToolDef] | None = None,
        is_sub_agent: bool = False,
        runtime_store: SQLiteRuntimeStore | None = None,
        runtime_sink: EventSink | None = None,
        runtime_parent_run_id: str | None = None,
        runtime_run_id: str | None = None,
        runtime_session_id: str | None = None,
        runtime_context_id: str | None = None,
        runtime_parent_context_id: str | None = None,
        artifact_archive: ArtifactArchive | None = None,
        archive_capability: ToolResultArchiveCapability | None = None,
        llm_capture_policy: LLMCapturePolicy | None = None,
        project_context: ProjectContext | None = None,
        output_port: OutputPort | None = None,
        interaction_port: InteractionPort | None = None,
        provider_client: Any | None = None,
    ):
        self.permission_mode = permission_mode
        # 结构化输出端口：显式注入优先；缺省用 NullOutputPort（不写业务输出）。
        # 本模块不隐式回退到终端渲染（design D1/D2）。
        self.output_port: OutputPort = output_port if output_port is not None else NullOutputPort()
        # 人工交互端口：显式注入优先；缺省保守拒绝（headless 不回退终端输入）。
        self.interaction_port: InteractionPort = (
            interaction_port if interaction_port is not None else DenyingInteractionPort()
        )
        self.interaction_registry = InteractionRegistry()
        self._application_interaction_mode = False
        # 项目上下文：显式优先；缺省按构造点 cwd 一次性解析（D7 保留 CLI 默认语义）。
        self.context = (
            project_context
            if project_context is not None
            else ProjectContext.from_root(Path.cwd())
        )
        self.thinking_effort = _normalize_thinking_effort(thinking_effort)
        # 保留旧版 thinking bool 参数：False 显式关闭；新的调用方应优先使用
        # thinking_effort，因此不让旧参数覆盖显式的 effort=none。
        if thinking is False:
            self.thinking_effort = "none"
        self.thinking = self.thinking_effort != "none"
        self.model = model
        self.use_openai = bool(api_base)
        self.is_sub_agent = is_sub_agent
        self._runtime_store = runtime_store
        self._runtime_sink = runtime_sink
        self._runtime_emitter: RuntimeEventEmitter | None = None
        self._runtime_context: RunContext | None = None
        self._runtime_recorder: ModelCallRecorder | None = None
        self._runtime_boundary: DurableToolBoundary | None = None
        self._runtime_store_owned = False
        self._runtime_closed = False
        self._runtime_canonical_sink: EventSink | None = None
        self._runtime_parent_run_id = runtime_parent_run_id
        self._runtime_run_id = runtime_run_id
        self._runtime_context_id = runtime_context_id
        self._runtime_parent_context_id = runtime_parent_context_id
        self._identity_factory = IdentityFactory(prefix="agent")
        self._runtime_guard: RunStateGuard | None = None
        self._runtime_exit_status: str | None = None
        self._runtime_exit_reason: str | None = None
        self._artifact_archive = artifact_archive
        self._artifact_archive_owned = artifact_archive is None
        self._artifact_archive_store: SQLiteRuntimeStore | None = None
        self._archive_capability = archive_capability
        self._llm_capture_policy = llm_capture_policy or LLMCapturePolicy()
        self._llm_capture_manager: LLMCaptureManager | None = None
        self.tools = custom_tools or tool_definitions
        self.max_cost_usd = max_cost_usd
        self.max_turns = max_turns
        self.confirm_fn = confirm_fn
        # 有效上下文窗口 = 模型窗口的 70%，剩余 30% 留给 system prompt、output 和封装开销
        self.effective_window = max(
            0, int(_get_context_window(model) * CONTEXT_WINDOW_USAGE_RATIO)
        )
        self.session_id = runtime_session_id or uuid.uuid4().hex[:8]
        if self._runtime_context_id is None:
            self._runtime_context_id = f"context:{self.session_id}"
        self.session_start_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Token 累计统计
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.last_input_token_count = 0
        self.current_turns = 0
        self.last_api_call_time = 0.0
        self._context_epoch = "context:initial"
        self._pending_compaction_tail: list[dict[str, Any]] | None = None
        self._pending_compaction_summary_source: list[dict[str, Any]] | None = None
        self._replay_cursor: IncrementalModelReplayCursor | None = None
        self._replay_events_read = 0
        self._replay_refresh_count = 0
        self._replay_last_read_count = 0
        self._replay_last_duration_ms = 0
        self._replay_last_mode = "cold"
        self._replay_last_rebuild_reason = "not_initialized"
        self._replay_last_source_digest = ""
        self._replay_last_projection_digest = ""
        self._provider_request_cycle: ProviderRequestCycle | None = None
        self._current_request_id: str | None = None

        # Ctrl+C 中断支持
        self._aborted = False
        self._current_task: asyncio.Task | None = None

        # 权限白名单：本次会话已确认过的路径
        self._confirmed_paths: set[str] = set()

        # Plan Mode 状态
        self._pre_plan_mode: str | None = None
        self._plan_file_path: str | None = None
        self._plan_approval_fn: Callable[[str], Awaitable[dict]] | None = None
        self._context_cleared: bool = False  # plan 审批通过后清空上下文

        # Thinking（扩展思考）模式：disabled / adaptive / enabled
        self._thinking_mode = self._resolve_thinking_mode()

        # Sub-Agent 输出缓冲区（子 Agent 通过 _output_buffer 捕获输出）
        self._output_buffer: list[str] | None = None

        # 先读后改保护：记录每个文件上次读取时的 mtime（绝对路径 → mtime）
        self._read_file_state: dict[str, float] = {}

        # MCP 集成（主 Agent 首次聊天时惰性初始化）
        self._mcp_manager = McpManager(self.context)
        self._mcp_initialized = False

        # 记忆召回状态 — 每个用户轮次的语义预取
        self._already_surfaced_memories: set[str] = set()
        self._session_memory_bytes = 0

        # 事件钩子系统（观测/Observer模式）
        self._event_hooks: dict[str, list] = {}

        # Ask 计数（每次 chat() 入口自增，用于 trace 文件编号）
        self._ask_count: int = 0

        # 双后端消息历史（分开存储，避免格式转换）
        self._anthropic_messages: list[dict] = []
        self._openai_messages: list[dict] = []

        # Build system prompt
        self._base_system_prompt = custom_system_prompt or build_system_prompt(self.context)
        if self.permission_mode == "plan":
            self._plan_file_path = self._generate_plan_file_path()
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
        else:
            self._system_prompt = self._base_system_prompt

        # 初始化 API 客户端（Anthropic 或 OpenAI 兼容）。provider_client 是
        # 可选的显式构造注入点：生产入口仍按 api_base/api_key 创建 SDK，
        # 本地消费者/测试可在 public Agent 构造点提供同一 SDK 接口的边界替身。
        if self.use_openai:
            self._openai_client = (
                provider_client
                if provider_client is not None
                else openai.AsyncOpenAI(base_url=api_base, api_key=api_key)
            )
            self._anthropic_client = None
            self._openai_messages.append({"role": "system", "content": self._system_prompt})
        else:
            kwargs: dict[str, Any] = {}
            if api_key:
                kwargs["api_key"] = api_key
            if anthropic_base_url:
                kwargs["base_url"] = anthropic_base_url
            self._anthropic_client = (
                provider_client
                if provider_client is not None
                else anthropic.AsyncAnthropic(**kwargs)
            )
            self._openai_client = None

    def _resolve_thinking_mode(self) -> str:
        if self.thinking_effort == "none":
            return "disabled"
        if not _model_supports_thinking(self.model):
            return "disabled"
        if _model_supports_adaptive_thinking(self.model):
            return "adaptive"
        return "enabled"

    # ─── 事件发射器 ─────────────────────────────────────────

    def on(self, event: str, callback) -> None:
        """订阅事件。callback 接收 payload dict 参数。"""
        self._event_hooks.setdefault(event, []).append(callback)

    def off(self, event: str, callback) -> None:
        """取消订阅。"""
        hooks = self._event_hooks.get(event)
        if hooks and callback in hooks:
            hooks.remove(callback)

    async def _emit(self, event: str, payload: Any = None) -> None:
        """发射事件。同步和异步回调均支持。"""
        import asyncio as _asyncio
        self._emit_runtime_observation(event, payload)
        for cb in self._event_hooks.get(event, []):
            try:
                res = cb(payload)
                if _asyncio.iscoroutine(res):
                    await res
            except Exception:
                pass  # 观测错误不影响主流程

    def _setup_runtime_facade(self) -> None:
        """Create session resources once and a fresh runtime scope per turn."""

        if self._runtime_closed:
            raise AgentClosedError("agent runtime is closed")

        if self._runtime_emitter is None:
            if self._runtime_store is None and self._runtime_sink is None:
                self._runtime_store = SQLiteRuntimeStore(
                    runtime_store_path(self.session_id)
                )
                self._runtime_store_owned = True
            canonical: EventSink = self._runtime_store or self._runtime_sink  # type: ignore[assignment]
            if canonical is None:
                raise RuntimeError("runtime facade requires a canonical sink")
            if self._artifact_archive is None:
                self._artifact_archive = ArtifactArchive(
                    runtime_data_dir() / "artifacts",
                    metadata_store=self._runtime_store,
                )
                self._artifact_archive_owned = True
            if self._artifact_archive_owned:
                self._artifact_archive_store = self._runtime_store
            self._llm_capture_manager = LLMCaptureManager(
                policy=self._llm_capture_policy,
                archive=self._artifact_archive,
                runtime_store=self._runtime_store,
            )
            if self._archive_capability is None:
                self._archive_capability = ToolResultArchiveCapability(
                    self._artifact_archive,
                    session_id=self.session_id,
                )
            elif (
                self._archive_capability.archive is not self._artifact_archive
                or self._archive_capability.session_id != self.session_id
            ):
                raise RuntimeResourceMismatchError(
                    "ArchiveRead capability is bound to a different runtime archive or session"
                )
            self._runtime_canonical_sink = canonical
            self._runtime_emitter = RuntimeEventEmitter(canonical)
        else:
            canonical = self._runtime_store or self._runtime_sink
            if (
                canonical is None
                or self._runtime_canonical_sink is not canonical
                or self._runtime_emitter.sink is not canonical
            ):
                raise RuntimeResourceMismatchError(
                    "runtime canonical sink changed while the Agent session is active"
                )
            if (
                self._artifact_archive_owned
                and self._artifact_archive_store is not self._runtime_store
            ):
                raise RuntimeResourceMismatchError(
                    "automatically-created artifact archive is bound to a different runtime store"
                )
            if (
                self._llm_capture_manager is None
                or self._llm_capture_manager.archive is not self._artifact_archive
                or self._llm_capture_manager.runtime_store is not self._runtime_store
            ):
                raise RuntimeResourceMismatchError(
                    "LLM capture manager is bound to a different runtime resource"
                )
            if (
                self._archive_capability is None
                or self._archive_capability.archive is not self._artifact_archive
                or self._archive_capability.session_id != self.session_id
            ):
                raise RuntimeResourceMismatchError(
                    "ArchiveRead capability is bound to a different runtime resource"
                )

        self._runtime_context = RunContext(
            session_id=self.session_id,
            turn_id=f"turn-{self._ask_count:04d}",
            run_id=self._runtime_run_id or self._identity_factory.run_id(),
            invocation_id=self._identity_factory.invocation_id(),
            parent_run_id=self._runtime_parent_run_id,
            context_id=self._runtime_context_id,
            parent_context_id=self._runtime_parent_context_id,
        )
        self._runtime_guard = RunStateGuard(self._runtime_context, self._runtime_emitter)
        if self._archive_capability is not None:
            self._archive_capability.grant_run(
                self._runtime_context.run_id,
                self._runtime_context.parent_run_id,
            )
        self._runtime_guard.start()
        self._runtime_recorder = None
        self._runtime_boundary = None
        self._runtime_exit_status = None
        self._runtime_exit_reason = None

    def _start_runtime_model_call(self, request_id: str, provider: str, request: Any) -> None:
        if self._runtime_emitter is None or self._runtime_context is None:
            return
        context = RunContext(
            session_id=self._runtime_context.session_id,
            turn_id=self._runtime_context.turn_id,
            run_id=self._runtime_context.run_id,
            invocation_id=request_id,
            parent_run_id=self._runtime_context.parent_run_id,
            branch=self._runtime_context.branch,
            context_id=self._runtime_context.context_id,
            parent_context_id=self._runtime_context.parent_context_id,
        )
        self._runtime_recorder = ModelCallRecorder(
            self._runtime_emitter,
            context,
            provider=provider,
            model=self.model,
        )
        self._runtime_recorder.start(request_id, request=request if isinstance(request, dict) else None)
        self._runtime_boundary = DurableToolBoundary(
            self._runtime_emitter,
            context,
            artifact_archive=self._artifact_archive,
            archive_capability=self._archive_capability,
        )

    def _provider_budget_bytes(self) -> int:
        """Return the conservative local byte budget for archive projection."""

        return max(0, int(self.effective_window) * 4)

    def _ensure_provider_request_cycle(
        self,
        result: ModelReplayResult,
        *,
        provider: str,
        active_turn_id: str | None,
        provider_tools: list[ToolDef],
        budget_bytes: int | None,
    ) -> ProviderRequestCycle | None:
        """Create/reuse the cycle that owns at most one active emergency pass."""

        request_id = self._current_request_id
        if not request_id:
            return None
        system_tools_digest = hashlib.sha256(
            canonical_json_bytes(
                {"system": self._system_prompt, "tools": provider_tools}
            )
        ).hexdigest()
        identity = ProviderRequestCycleIdentity(
            session_id=self.session_id,
            provider_request_id=str(request_id),
            source_digest=result.source_digest,
            source_high_water=result.high_water,
            provider=provider,
            active_turn_id=active_turn_id,
            system_tools_digest=system_tools_digest,
            request_budget_bytes=budget_bytes,
        )
        if (
            self._provider_request_cycle is None
            or self._provider_request_cycle.identity != identity
        ):
            self._provider_request_cycle = ProviderRequestCycle(identity)
        return self._provider_request_cycle

    def _assert_provider_request_fits(
        self,
        provider: str,
        messages: list[dict[str, Any]],
    ) -> None:
        """Fail closed immediately before any Provider SDK dispatch."""

        budget = self._provider_budget_bytes()
        request_size = provider_request_size_bytes(
            provider,
            messages,
            system_prompt=self._system_prompt,
            provider_tools=self._effective_tool_definitions(),
        )
        if request_size > budget:
            raise ProviderCapacityError(
                provider=provider,
                request_size_bytes=request_size,
                request_budget_bytes=budget,
                diagnostics=(
                    {
                        "code": "provider_capacity_exhausted",
                        "message": "final Provider context exceeds the local request budget",
                        "severity": "error",
                    },
                ),
                cycle_identity=(
                    self._provider_request_cycle.identity_digest
                    if self._provider_request_cycle is not None
                    else None
                ),
            )

    def _effective_tool_definitions(self) -> list[ToolDef]:
        """Build request tools, binding ArchiveRead only to this capability."""

        definitions = list(get_active_tool_definitions(self.tools))
        if self._archive_capability is None:
            return definitions
        if any(item.get("name") == "ArchiveRead" for item in definitions):
            raise RuntimeResourceMismatchError(
                "custom tools cannot shadow the runtime ArchiveRead capability"
            )
        definitions.append(self._archive_capability.tool_definition)
        return definitions

    def _display_tool_result(self, value: Any, *, provider: str) -> str:
        """Render the terminal view without changing Provider tool content."""

        formatted = format_terminal_tool_result(value, self._archive_capability)
        if formatted is not None:
            return formatted
        terminal_value = project_terminal_tool_result(value, self._archive_capability)
        return display_tool_result(
            materialize_tool_result(terminal_value, provider=provider)
        )

    def _record_runtime_model_error(self, error: BaseException) -> None:
        """Seal the current model call as failed before propagating its error."""

        if self._runtime_recorder:
            self._runtime_recorder.error(error)
            if self._runtime_guard and self._runtime_recorder.events:
                self._runtime_guard.adopt_terminal_event(self._runtime_recorder.events[-1])

    def _record_budget_exceeded(self, reason: str) -> None:
        """Finalize the run budget without re-finishing the model call.

        A provider response can finish its model recorder before the agent
        notices that the resulting tool turn exhausted the run budget.  The
        budget decision is therefore a run-level concern and must use the
        ``RunStateGuard`` rather than ``ModelCallRecorder.budget_exceeded``.
        """

        terminal = None
        if self._runtime_guard is not None:
            terminal = self._runtime_guard.budget_exceeded(reason)
        if terminal is not None:
            self._runtime_exit_status = "budget_exceeded"
            self._runtime_exit_reason = reason

    async def _run_durable_tool(
        self,
        *,
        request_id: str,
        call_id: str,
        name: str,
        inp: Any,
        permission: dict[str, Any] | str,
        arguments: Any | None = None,
    ) -> tuple[Any, bool, bool]:
        """Run one tool only after the canonical dispatch barrier succeeds."""

        del request_id
        if self._runtime_boundary is None:
            raise RuntimeError("canonical durable tool boundary is not initialized")
        result = await self._runtime_boundary.execute(
            call_id=call_id,
            name=name,
            arguments=inp if arguments is None else arguments,
            permission=permission,
            executor=lambda: self._execute_tool_call(name, inp),
            on_started=lambda: self._emit(
                "tool_start", {"tool_name": name, "tool_input": inp, "tool_call_id": call_id}
            ),
        )
        if result.success:
            commit_tool_state(name, inp, result.result, self._read_file_state, context=self.context)
        return result.result, result.success, result.executed

    def _emit_runtime_observation(self, event: str, payload: Any) -> None:
        """Persist non-provider lifecycle observations through the emitter."""

        if self._runtime_emitter is None or self._runtime_context is None:
            return
        if event not in {"chat_start", "chat_error", "first_token", "turn_start", "turn_end", "compaction"}:
            return
        details = dict(payload or {})
        if event == "chat_error":
            runtime_event = RuntimeEvent.create(
                self._runtime_context,
                role="system",
                author="system",
                content={
                    "kind": "error",
                    "code": "chat_error",
                    "message": str(details.get("error", "chat failed")),
                },
                ts=int(time.time() * 1000),
                metadata={"lifecycle": event},
            )
        else:
            if event == "chat_start":
                details = {"started": True}
            runtime_event = RuntimeEvent.create(
                self._runtime_context,
                role="system",
                author="system",
                actions={event: details},
                ts=int(time.time() * 1000),
                metadata={"lifecycle": event},
            )
        self._runtime_emitter.emit(runtime_event)

    def _emit_canonical_user_event(self, user_message: str) -> None:
        """Record the original user input before provider context mutation."""

        if self._runtime_emitter is None or self._runtime_context is None:
            return
        event = RuntimeEvent.create(
            self._runtime_context,
            role="user",
            author="user",
            content={"kind": "text", "text": user_message},
            ts=int(time.time() * 1000),
            metadata={
                "lifecycle": "user_input",
                "source": "user",
                "injected": False,
            },
        )
        self._runtime_emitter.emit(event)

    def _persist_memory_context_event(self, memories: list[Any]) -> RuntimeEvent:
        """Persist the exact memory context before rebuilding the request."""

        if self._runtime_emitter is None or self._runtime_context is None:
            raise RuntimeError("canonical runtime facade is not initialized")
        source_values = [str(memory.path) for memory in memories]
        raw_content = {
            "kind": "context",
            "context_type": "memory",
            "text": format_memories_for_injection(memories),
            "sources": source_values,
            "sequence": 0,
        }
        # Put the payload under ``content`` so the redaction policy recognizes
        # ``content.text`` as replay state and never replaces long memory text
        # with a non-string bounded reference.
        redacted_wrapper = redact_payload(
            {"content": raw_content}, self._runtime_emitter.redaction_policy
        )
        safe_content = dict(redacted_wrapper["content"])
        safe_text = str(safe_content["text"])
        content_digest = hashlib.sha256(safe_text.encode("utf-8")).hexdigest()
        context_id = self._runtime_context.context_id
        idempotency_key = (
            f"memory:{context_id}:{self._runtime_context.turn_id}:{content_digest}"
        )
        safe_content["content_digest"] = content_digest
        safe_content["idempotency_key"] = idempotency_key
        event_id = "memory-event:" + hashlib.sha256(
            idempotency_key.encode("utf-8")
        ).hexdigest()[:32]
        if self._runtime_store is not None:
            read_event = getattr(self._runtime_store, "read_event", None)
            if callable(read_event):
                existing = read_event(event_id)
                if existing is not None:
                    return existing
            else:
                for _, existing in self._runtime_store.read_event_records():
                    if existing.id == event_id:
                        return existing
        elif self._runtime_emitter is not None:
            sink_events = getattr(self._runtime_emitter.sink, "events", ())
            for existing in sink_events:
                if existing.id == event_id:
                    return existing
        event = RuntimeEvent.create(
            self._runtime_context,
            role="user",
            author="system",
            origin="code_mode",
            model_visibility="visible",
            content=safe_content,
            ts=int(time.time() * 1000),
            event_id=event_id,
            metadata={
                "lifecycle": "memory_injection",
                "context_type": "memory",
                "idempotency_key": idempotency_key,
            },
        )
        self._runtime_emitter.emit(event)
        return event

    def _record_sub_agent_event(self, *, name: str, agent_type: str, prompt: str) -> None:
        if self._runtime_emitter is None or self._runtime_context is None:
            return
        event = RuntimeEvent.create(
            self._runtime_context,
            role="system",
            author="agent",
            actions={"sub_agent": {"name": name, "agent_type": agent_type, "prompt_summary": prompt[:200]}},
            ts=int(time.time() * 1000),
            metadata={"lifecycle": "child_run_opened"},
        )
        self._runtime_emitter.emit(event)

    def _capture_llm(
        self,
        *,
        request_id: str,
        messages: list[dict],
        response: dict,
        usage: dict,
        latency_ms: int,
        input_tokens: int | None,
        output_tokens: int | None,
        cache_read_tokens: int | None,
        finish_reason: str,
    ) -> None:
        """Capture according to the explicit privacy policy only."""

        capture = None
        if self._llm_capture_manager is not None and self._runtime_context is not None:
            capture = self._llm_capture_manager.capture(
                request_id=request_id,
                session_id=self._runtime_context.session_id,
                run_id=self._runtime_context.run_id,
                invocation_id=request_id,
                attempt=self._runtime_recorder.attempt if self._runtime_recorder else 1,
                attempt_id=self._runtime_recorder.attempt_id if self._runtime_recorder else None,
                provider="openai" if self.use_openai else "anthropic",
                model=self.model,
                request=messages,
                response=response,
                usage=usage,
                latency_ms=latency_ms,
            )
            self._emit_llm_capture_observation(request_id, capture)

        del input_tokens, output_tokens, cache_read_tokens, finish_reason, latency_ms

    def _emit_llm_capture_observation(self, request_id: str, capture: Any) -> None:
        if self._runtime_emitter is None or self._runtime_context is None:
            return
        refs = {"llm_ref": capture.llm_ref} if capture.llm_ref else None
        event = RuntimeEvent.create(
            self._runtime_context,
            role="system",
            author="agent",
            actions={
                "llm_capture": {
                    "request_id": request_id,
                    "capture_status": capture.capture_status,
                    "error": capture.error,
                }
            },
            refs=refs,
            ts=int(time.time() * 1000),
            metadata={"lifecycle": "llm_capture", "capture_mode": self._llm_capture_policy.mode},
        )
        try:
            self._runtime_emitter.emit(event)
        except Exception:
            # Capture is auxiliary; a provider response must not be turned into
            # a different model result because a diagnostic row failed.
            pass

    def _msg_char_count(self) -> int:
        """计算当前消息列表的字符总数（用于压缩前后对比）。"""
        msgs = self._openai_messages if self.use_openai else self._anthropic_messages
        return sum(len(str(m)) for m in msgs)

    @property
    def is_processing(self) -> bool:
        return self._current_task is not None and not self._current_task.done()

    def _build_side_query(self):
        """构建 sideQuery 调用函数 — 用于记忆语义召回。
        向模型发送小 prompt，从记忆列表中选出相关者。
        双后端各有一套实现，返回 awaitable callable。"""
        if self._anthropic_client:
            client = self._anthropic_client
            model = self.model
            async def _sq(system: str, user_message: str) -> str:
                resp = await client.messages.create(
                    model=model, max_tokens=256, system=system,
                    messages=[{"role": "user", "content": user_message}],
                )
                return "".join(b.text for b in resp.content if b.type == "text")
            return _sq
        if self._openai_client:
            client = self._openai_client
            model = self.model
            async def _sq_oai(system: str, user_message: str) -> str:
                resp = await client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_message},
                    ],
                )
                return resp.choices[0].message.content or "" if resp.choices else ""
            return _sq_oai
        return None

    def abort(self) -> None:
        """中断当前 Agent 循环（Ctrl+C 时调用）。"""
        self._aborted = True
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()

    def set_confirm_fn(self, fn: Callable[[str], Awaitable[bool]]) -> None:
        self.confirm_fn = fn

    def set_interaction_port(self, port: InteractionPort) -> None:
        """替换交互端口（入口注入终端适配器时使用）。"""

        self.interaction_port = port

    def configure_application_interactions(self, enabled: bool = True) -> None:
        """Enable C03 full-identity interaction envelopes for an Application."""

        self._application_interaction_mode = bool(enabled)

    def configure_runtime_identity(self, *, session_id: str | None = None, run_id: str | None = None) -> None:
        """Bind a not-yet-started Agent turn to an Application identity."""

        if self.is_processing or (
            self._runtime_emitter is not None
            and self._runtime_guard is not None
            and not self._runtime_guard.is_terminal
        ):
            raise AgentClosedError("runtime identity cannot change while an Agent turn is active")
        if session_id is not None:
            self.session_id = session_id
        if run_id is not None:
            self._runtime_run_id = run_id

    def configure_runtime_store(self, runtime_store: SQLiteRuntimeStore) -> None:
        """Inject an Application-owned canonical store before the first turn."""

        if self.is_processing or (
            self._runtime_emitter is not None
            and self._runtime_guard is not None
            and not self._runtime_guard.is_terminal
        ):
            raise AgentClosedError("runtime store cannot change while an Agent turn is active")
        self._runtime_store = runtime_store
        self._runtime_store_owned = False

    def set_plan_approval_fn(self, fn: Callable[[str], Awaitable[dict]]) -> None:
        self._plan_approval_fn = fn

    # ─── Plan Mode 切换 ──────────────────────────────────────
    # 仅在交互式 REPL 中使用（/plan 命令），
    # 手动在普通模式与只读计划模式之间切换。

    def toggle_plan_mode(self) -> str:
        # 退出plan模式
        if self.permission_mode == "plan":
            self.permission_mode = self._pre_plan_mode or "default"
            self._pre_plan_mode = None
            self._plan_file_path = None
            self._system_prompt = self._base_system_prompt
            if self.use_openai and self._openai_messages:
                self._openai_messages[0]["content"] = self._system_prompt
            self._out_info(f"Exited plan mode → {self.permission_mode} mode")
            return self.permission_mode
        # 进入plan模式
        else:
            self._pre_plan_mode = self.permission_mode
            self.permission_mode = "plan"
            self._plan_file_path = self._generate_plan_file_path()
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
            if self.use_openai and self._openai_messages:
                self._openai_messages[0]["content"] = self._system_prompt
            self._out_info(f"Entered plan mode. Plan file: {self._plan_file_path}")
            return "plan"

    def get_token_usage(self) -> dict:
        return {"input": self.total_input_tokens, "output": self.total_output_tokens}

    # ─── 主入口 ──────────────────────────────────────────────

    async def chat(self, user_message: str) -> None:
        """Agent 主循环入口。路由到对应后端（Anthropic / OpenAI）。"""
        # 首次聊天时惰性连接 MCP 服务器（仅主 Agent）
        if not self._mcp_initialized and not self.is_sub_agent:
            self._mcp_initialized = True
            try:
                await self._mcp_manager.load_and_connect()
                mcp_defs = self._mcp_manager.get_tool_definitions()
                if mcp_defs:
                    self.tools = self.tools + mcp_defs
            except Exception as e:
                self._out_raw(f"[mcp] Init failed: {e}", flush=True)

        self._aborted = False
        self._ask_count += 1
        self._provider_request_cycle = None
        self._current_request_id = None
        # The cursor is run-scoped.  A new user turn starts from a cold
        # canonical prefix, then consumes only suffix ordinals for its loop.
        self._replay_cursor = None
        self._replay_last_rebuild_reason = "new_run"

        self._setup_runtime_facade()

        self._emit_canonical_user_event(user_message)
        await self._emit("chat_start", {"message": user_message, "timestamp": time.time()})

        coro = self._chat_openai(user_message) if self.use_openai else self._chat_anthropic(user_message)
        self._current_task = asyncio.current_task()
        primary_error: BaseException | None = None
        canonical_failure: Exception | None = None
        snapshot_saved = False
        try:
            await coro
        except asyncio.CancelledError as error:
            primary_error = error
            self._aborted = True
            self._runtime_exit_status = "cancelled"
            self._runtime_exit_reason = "asyncio cancellation"
        except Exception as error:
            primary_error = error
            try:
                self._out_error(str(error))
                await self._emit("chat_error", {"error": str(error)})
            except Exception as diagnostic_error:
                canonical_failure = diagnostic_error
            self._runtime_exit_status = "failed"
            self._runtime_exit_reason = str(error)
        finally:
            self._current_task = None
            if self._runtime_recorder:
                try:
                    # A cancellation may bypass ModelCallRecorder.finish();
                    # persist the last buffered observation before finalizing
                    # the run or flushing the canonical sink.
                    self._runtime_recorder.flush_partials()
                except Exception as error:
                    canonical_failure = canonical_failure or error
                    self._runtime_exit_status = "failed"
                    self._runtime_exit_reason = f"canonical partial flush failed: {error}"
                    self._out_raw(f"[runtime] partial flush failed: {error}", flush=True)
            if self._runtime_guard and not self._runtime_guard.is_terminal:
                final_status = self._runtime_exit_status or (
                    "aborted" if self._aborted else "completed"
                )
                try:
                    self._runtime_guard.finalize(
                        final_status,
                        reason=self._runtime_exit_reason,
                    )
                except Exception as error:
                    canonical_failure = canonical_failure or error
                    self._runtime_exit_status = "failed"
                    self._runtime_exit_reason = f"canonical terminal finalize failed: {error}"
                    self._out_raw(f"[runtime] terminal finalize failed: {error}", flush=True)
            if self._runtime_emitter:
                try:
                    self._runtime_emitter.flush()
                except Exception as error:
                    canonical_failure = canonical_failure or error
                    self._runtime_exit_status = "failed"
                    self._runtime_exit_reason = f"canonical flush failed: {error}"
                    self._out_raw(f"[runtime] flush failed: {error}", flush=True)
                finally:
                    if self._runtime_store_owned:
                        try:
                            # Keep the owned SQLite connection alive for the
                            # next user turn. It is closed by Agent.aclose().
                            self._auto_save()
                            snapshot_saved = True
                        except Exception as error:
                            canonical_failure = canonical_failure or error
                            self._runtime_exit_status = "failed"
                            self._runtime_exit_reason = f"canonical snapshot failed: {error}"

            self._current_request_id = None
            self._provider_request_cycle = None

        if canonical_failure is not None:
            diagnostic = CanonicalFinalizationError(
                f"canonical finalization failed: {canonical_failure}"
            )
            if primary_error is not None:
                primary_error.add_note(str(diagnostic))
            else:
                raise diagnostic from canonical_failure
        if primary_error is not None and not isinstance(primary_error, asyncio.CancelledError):
            raise primary_error

        if not self.is_sub_agent:
            self._out_divider()
            if not snapshot_saved:
                self._auto_save()

    async def aclose(self) -> None:
        """Close Agent-owned session resources exactly once.

        A caller-owned Store or Sink is flushed but never closed here. The
        Agent becomes terminal after this method and cannot start another chat.
        """

        if self._runtime_closed:
            return

        active_task = self._current_task
        current_task = asyncio.current_task()
        if (
            active_task is not None
            and active_task is not current_task
            and not active_task.done()
        ):
            try:
                # Provider calls and archive projections are part of the
                # session's durable boundary. Let the active turn settle
                # before closing an Agent-owned store underneath it.
                await asyncio.shield(active_task)
            except BaseException:
                # The active turn records its own terminal failure. A close
                # request must still release resources exactly once.
                pass
        self._runtime_closed = True

        failures: list[tuple[str, Exception]] = []
        try:
            await self._mcp_manager.disconnect_all()
        except Exception as error:
            failures.append(("mcp disconnect", error))

        emitter = self._runtime_emitter
        owned_store = self._runtime_store if self._runtime_store_owned else None
        try:
            if self._runtime_recorder is not None:
                try:
                    self._runtime_recorder.flush_partials()
                except Exception as error:
                    failures.append(("runtime partial flush", error))
            if emitter is not None:
                try:
                    emitter.flush()
                except Exception as error:
                    failures.append(("runtime flush", error))
            if owned_store is not None:
                try:
                    self._auto_save()
                except Exception as error:
                    failures.append(("runtime snapshot", error))
        finally:
            if owned_store is not None:
                try:
                    if emitter is not None:
                        emitter.close()
                    else:
                        owned_store.close()
                except Exception as error:
                    failures.append(("runtime close", error))
                    try:
                        owned_store.close()
                    except Exception:
                        pass

            self._runtime_emitter = None
            self._runtime_store = None
            self._runtime_store_owned = False
            self._runtime_canonical_sink = None
            self._artifact_archive = None
            self._artifact_archive_store = None
            self._archive_capability = None
            self._llm_capture_manager = None
            self._runtime_context = None
            self._runtime_guard = None
            self._runtime_recorder = None
            self._runtime_boundary = None

        if failures:
            details = "; ".join(f"{name}: {error}" for name, error in failures)
            raise CanonicalFinalizationError(f"agent close failed: {details}") from failures[0][1]

    # ─── Sub-Agent 入口 ──────────────────────────────────────
    # 子 Agent 通过 run_once 执行单次任务并返回结果，
    # 输出被 _output_buffer 捕获，token 消耗回计到父 Agent。

    async def run_once(self, prompt: str) -> dict:
        self._output_buffer = []
        prev_in = self.total_input_tokens
        prev_out = self.total_output_tokens
        await self.chat(prompt)
        text = "".join(self._output_buffer)
        self._output_buffer = None
        return {
            "text": text,
            "tokens": {
                "input": self.total_input_tokens - prev_in,
                "output": self.total_output_tokens - prev_out,
            },
        }

    # ─── Output helper ────────────────────────────────────────

    def _emit_text(self, text: str) -> None:
        if self._output_buffer is not None:
            self._output_buffer.append(text)
        else:
            self._out_assistant_text(text)

    # ─── 输出端口（结构化观察接口）────────────────────────────

    def _port_emit(self, kind: str, payload: dict[str, Any] | None = None, **ids: Any) -> None:
        """把观察事件发布到输出端口。

        端口是观察接口：canonical 事实仍由 emitter/store 承担；端口抛错由
        `emit_safely` 隔离为诊断，不影响 run 终态。
        """

        event = OutputEvent(
            kind=kind,
            session_id=str(self.session_id),
            run_id=str(self._runtime_run_id or self.session_id),
            payload=dict(payload or {}),
            attempt_id=ids.get("attempt_id"),
            tool_call_id=ids.get("tool_call_id"),
            stream=ids.get("stream"),
        )
        emit_safely(self.output_port, event)

    def _out_assistant_text(self, text: str) -> None:
        self._port_emit("assistant_text", {"text": text}, stream="assistant")

    def _out_tool_call(self, name: str, inp: dict, tool_call_id: str | None = None) -> None:
        self._port_emit(
            "tool_call",
            {"tool": name, "input": dict(inp or {})},
            tool_call_id=tool_call_id,
        )

    def _out_tool_result(
        self,
        name: str,
        result: str,
        *args: Any,
        tool_call_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        self._port_emit(
            "tool_result", {"tool": name, "result": result}, tool_call_id=tool_call_id
        )

    def _out_tool_denied(self, tool: str, message: str, tool_call_id: str | None = None) -> None:
        self._port_emit(
            "tool_denied", {"tool": tool, "message": message}, tool_call_id=tool_call_id
        )

    def _out_confirmation(self, command: str) -> None:
        self._port_emit("confirmation", {"command": command})

    def _out_divider(self) -> None:
        self._port_emit("divider")

    def _out_cost(self, input_tokens: int, output_tokens: int) -> None:
        self._port_emit("budget", {"input_tokens": input_tokens, "output_tokens": output_tokens})

    def _out_retry(self, attempt: int, max_retries: int, reason: str) -> None:
        self._port_emit(
            "retry", {"attempt": attempt, "max_retries": max_retries, "reason": reason}
        )

    def _out_info(self, message: str) -> None:
        self._port_emit("info", {"message": message})

    def _out_error(self, message: str) -> None:
        self._port_emit("error", {"message": message})

    def _out_thinking(self, text: str) -> None:
        self._port_emit("assistant_thinking", {"text": text}, stream="thinking")

    def _out_lifecycle(self, phase: str, **extra: Any) -> None:
        self._port_emit("lifecycle", {"phase": phase, **extra})

    def _current_attempt_id(self) -> str | None:
        """当前尝试身份（GAP-C02-18）：有 recorder 时取尝试号，否则 None。

        `attempt_id` 只在存在真实尝试语义（重试记录器）时填充，不凭空编造。
        """

        recorder = self._runtime_recorder
        attempt = getattr(recorder, "attempt", None) if recorder is not None else None
        if attempt is None:
            return None
        return f"attempt-{attempt}"

    def _out_sub_agent_start(self, agent_type: str, description: str) -> None:
        self._port_emit(
            "sub_agent_start", {"agent_type": agent_type, "description": description}
        )

    def _out_sub_agent_end(self, agent_type: str, description: str) -> None:
        self._port_emit(
            "sub_agent_end", {"agent_type": agent_type, "description": description}
        )

    def _out_start_spinner(self, label: str = "Thinking") -> None:
        self._port_emit("spinner", {"active": True, "label": label})

    def _out_stop_spinner(self) -> None:
        self._port_emit("spinner", {"active": False})

    def _out_raw(self, *args: Any, **kwargs: Any) -> None:
        """诊断级别的原始输出（绕过端口家族）。

        这些是后台/接线失败诊断（MCP 初始化、runtime 快照失败等），不属用户可见
        的业务输出；发到诊断通道而不是终端，避免破坏"runtime 无终端依赖"。
        """

        message = " ".join(str(a) for a in args) if args else str(kwargs.get("sep", ""))
        self._port_emit("diagnostic", {"message": message})

    # ─── REPL 命令 ───────────────────────────────────────────

    def clear_history(self) -> None:
        """清空对话历史（/clear 命令）。"""
        self._anthropic_messages = []
        self._openai_messages = []
        if self.use_openai:
            self._openai_messages.append({"role": "system", "content": self._system_prompt})
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.last_input_token_count = 0
        self._out_info("Conversation cleared.")

    def show_cost(self) -> None:
        total = self._get_current_cost_usd()
        budget_info = f" / ${self.max_cost_usd} budget" if self.max_cost_usd else ""
        turn_info = f" | Turns: {self.current_turns}/{self.max_turns}" if self.max_turns else ""
        self._out_info(f"Tokens: {self.total_input_tokens} in / {self.total_output_tokens} out\n  Estimated cost: ${total:.4f}{budget_info}{turn_info}")

    def _get_current_cost_usd(self) -> float:
        return (self.total_input_tokens / 1_000_000) * 3 + (self.total_output_tokens / 1_000_000) * 15

    def _check_budget(self) -> dict:
        if self.max_cost_usd is not None and self._get_current_cost_usd() >= self.max_cost_usd:
            return {"exceeded": True, "reason": f"Cost limit reached (${self._get_current_cost_usd():.4f} >= ${self.max_cost_usd})"}
        if self.max_turns is not None and self.current_turns >= self.max_turns:
            return {"exceeded": True, "reason": f"Turn limit reached ({self.current_turns} >= {self.max_turns})"}
        return {"exceeded": False}

    async def compact(self) -> None:
        await self._compact_conversation()

    # ─── 会话持久化 ──────────────────────────────────────────
    # 每次 chat 结束自动保存到 ~/.rollo/sessions/，
    # --resume 启动时恢复消息历史。

    def restore_session(self, data: dict) -> None:
        meta = data.get("metadata")
        if meta and meta.get("id"):
            # Continuations stay in the same canonical session namespace.
            self.session_id = meta["id"]
            restored_ask_count = meta.get("askCount")
            if restored_ask_count is None:
                # Canonical snapshots use coverage.turnIds.  Recover the
                # largest completed turn so the next chat gets a fresh run.
                turn_numbers = []
                coverage = data.get("coverage") or {}
                for turn_id in coverage.get("turnIds") or []:
                    try:
                        turn_numbers.append(int(str(turn_id).rsplit("-", 1)[-1]))
                    except (TypeError, ValueError):
                        continue
                restored_ask_count = max(turn_numbers, default=len(data.get("runs") or []))
            self._ask_count = int(restored_ask_count or 0)
            # A restored session may contain a sealed terminal run.  Resume
            # the session namespace, but always allocate a fresh run for the
            # next turn instead of reusing that sealed run identity.
            self._runtime_run_id = None
            if self._runtime_parent_context_id is None:
                self._runtime_context_id = f"context:{self.session_id}"
        if data.get("source") != "canonical" and data.get("metadata", {}).get("source") != "canonical":
            raise ValueError("session snapshot is not canonical-derived")
        self.restore_canonical_context(data.get("canonicalMessages", []))

    def restore_canonical_context(self, messages: list[dict[str, Any]]) -> None:
        """Restore provider context from a canonical model projection only."""

        if self.use_openai:
            self._openai_messages = [{"role": "system", "content": self._system_prompt}]
            for message in messages:
                item = dict(message)
                item.pop("runtime_event_id", None)
                self._openai_messages.append(item)
        else:
            self._anthropic_messages = []
            for message in messages:
                role = message.get("role")
                if role == "assistant" and message.get("tool_calls"):
                    blocks = [
                        {
                            "type": "tool_use",
                            "id": call.get("id"),
                            "name": call.get("name"),
                            "input": call.get("arguments", {}),
                        }
                        for call in message["tool_calls"]
                    ]
                    self._anthropic_messages.append({"role": "assistant", "content": blocks})
                elif role in {"user", "assistant"}:
                    self._anthropic_messages.append(
                        {"role": role, "content": message.get("content", "")}
                    )
                elif role == "tool":
                    self._anthropic_messages.append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": message.get("tool_call_id"),
                                    "content": message.get("content", ""),
                                }
                            ],
                        }
                    )
        self._out_info(f"Canonical context restored ({self._get_message_count()} messages).")

    def project_canonical_model_context(self, *, high_water: int | None = None):
        """Build a read-only replay from the active canonical store."""

        if self._runtime_store is None:
            return None
        result = ModelReplayProjection().build(
            self._runtime_store,
            high_water=high_water,
            context_id=self._runtime_context.context_id if self._runtime_context else self._runtime_context_id,
        )
        return result

    def _refresh_provider_context_from_canonical(self):
        """Refresh provider context from a cold prefix or an event suffix."""

        if self._runtime_store is None or not hasattr(self._runtime_store, "read_event_records"):
            raise RuntimeError("canonical runtime store is not initialized")
        started = time.perf_counter()
        context_id = self._runtime_context.context_id if self._runtime_context else self._runtime_context_id
        cold = self._replay_cursor is None
        reason = "cold_start" if cold else "warm_suffix"
        if cold:
            cursor = IncrementalModelReplayCursor(context_id=context_id)
            pairs = self._runtime_store.read_event_records(context_id=context_id)
            cursor.append(
                EventRecord(ordinal, event) for ordinal, event in pairs
            )
        else:
            cursor = self._replay_cursor
            current_high_water = self._runtime_store.current_high_water
            if current_high_water < cursor.high_water:
                cold = True
                reason = "source_high_water_regressed"
                cursor = IncrementalModelReplayCursor(context_id=context_id)
                pairs = self._runtime_store.read_event_records(context_id=context_id)
                cursor.append(
                    EventRecord(ordinal, event) for ordinal, event in pairs
                )
            elif current_high_water > cursor.high_water:
                try:
                    pairs = self._runtime_store.read_event_records(
                        after_ordinal=cursor.high_water,
                        context_id=context_id,
                    )
                except TypeError:
                    # Keep compatibility with caller-owned test stores that
                    # expose the pre-incremental read signature.
                    reason = "warm_suffix_fallback_full_read"
                    all_pairs = self._runtime_store.read_event_records(context_id=context_id)
                    pairs = [
                        (ordinal, event)
                        for ordinal, event in all_pairs
                        if ordinal > cursor.high_water
                    ]
                try:
                    cursor.append(
                        EventRecord(ordinal, event) for ordinal, event in pairs
                    )
                except IncrementalReplayError:
                    reason = "cursor_invalid"
                    cold = True
                    cursor = IncrementalModelReplayCursor(context_id=context_id)
                    pairs = self._runtime_store.read_event_records(context_id=context_id)
                    cursor.append(
                        EventRecord(ordinal, event) for ordinal, event in pairs
                    )
            else:
                pairs = []

        read_count = len(pairs)
        self._replay_cursor = cursor
        self._replay_events_read += read_count
        self._replay_refresh_count += 1
        self._replay_last_read_count = read_count
        self._replay_last_mode = "cold" if cold else "warm"

        # A committed transition changes the effective prefix.  Reinitialize
        # from the canonical source once at that boundary so stale call-group
        # indexes cannot resurrect pre-transition messages.
        if not cold and cursor.last_append_had_transition:
            reason = "context_transition"
            cursor = IncrementalModelReplayCursor(context_id=context_id)
            all_pairs = self._runtime_store.read_event_records(context_id=context_id)
            cursor.append(
                EventRecord(ordinal, event) for ordinal, event in all_pairs
            )
            self._replay_cursor = cursor
            self._replay_events_read += len(all_pairs)
            self._replay_last_read_count += len(all_pairs)
            self._replay_last_mode = "cold"

        result = cursor.result()
        provider = "openai" if self.use_openai else "anthropic"
        if self._current_request_id is None:
            # Direct/local consumers may refresh outside chat(); give that
            # refresh a stable owner so repeated calls still share one cycle.
            self._current_request_id = f"refresh-{self._ask_count:04d}"
        provider_tools = self._effective_tool_definitions()
        active_turn_id = self._runtime_context.turn_id if self._runtime_context else None
        request_cycle = self._ensure_provider_request_cycle(
            result,
            provider=provider,
            active_turn_id=active_turn_id,
            provider_tools=provider_tools,
            budget_bytes=self._provider_budget_bytes(),
        )
        context = CanonicalModelContextAdapter().build_result(
            result,
            provider=provider,
            system_prompt=self._system_prompt,
            provider_tools=provider_tools,
            archive_capability=self._archive_capability,
            budget_bytes=self._provider_budget_bytes(),
            session_id=self.session_id,
            request_id=self._current_request_id,
            active_turn_id=active_turn_id,
            request_cycle=request_cycle,
        )
        errors = [
            diagnostic for diagnostic in context.diagnostics
            if getattr(diagnostic, "severity", None) == "error"
            and getattr(diagnostic, "code", "") == "invalid_context_transition"
        ]
        if errors:
            raise CompactionError(
                "canonical context transition is not verifiable: "
                + "; ".join(str(item.message) for item in errors[:3])
            )
        self._replay_last_duration_ms = int(
            (time.perf_counter() - started) * 1000
        )
        self._replay_last_rebuild_reason = reason
        self._replay_last_source_digest = result.source_digest
        self._replay_last_projection_digest = result.digest
        self._context_epoch = context.context_epoch
        if not context.request_fits:
            raise ProviderCapacityError(
                provider=context.provider,
                request_size_bytes=context.request_size_bytes,
                request_budget_bytes=context.request_budget_bytes
                if context.request_budget_bytes is not None
                else 0,
                diagnostics=context.diagnostics,
                cycle_identity=context.cycle_identity,
            )
        messages = [dict(message) for message in context.messages]
        if self.use_openai:
            self._openai_messages = messages
        else:
            self._anthropic_messages = messages
        return context

    def replay_diagnostics(self) -> dict[str, Any]:
        """Return bounded, content-free replay instrumentation for this run."""

        return {
            "source_high_water": self._replay_cursor.high_water if self._replay_cursor else 0,
            "context_epoch": self._context_epoch,
            "source_digest": self._replay_last_source_digest,
            "projection_digest": self._replay_last_projection_digest,
            "events_read_total": self._replay_events_read,
            "events_read_last_refresh": self._replay_last_read_count,
            "refresh_count": self._replay_refresh_count,
            "projection_duration_ms": self._replay_last_duration_ms,
            "mode": self._replay_last_mode,
            "rebuild_reason": self._replay_last_rebuild_reason,
        }

    def _get_message_count(self) -> int:
        return len(self._openai_messages) if self.use_openai else len(self._anthropic_messages)

    def _auto_save(self) -> None:
        if self._runtime_store is None:
            raise RuntimeError("canonical runtime store is not initialized")
        save_session_v2(self.session_id, self._runtime_store)

    # ─── 自动压缩 ────────────────────────────────────────────
    # 当上下文利用率超过 85% 时自动触发完整压缩（compact）。
    # 压缩用模型生成摘要替代历史消息，保留关键决策和文件路径。

    async def _check_and_compact(self) -> None:
        if self.last_input_token_count > self.effective_window * 0.85:
            self._out_info("Context window filling up, compacting conversation...")
            await self._emit("compaction", {"tier": 4})
            await self._compact_conversation()

    async def _compact_conversation(self) -> None:
        summary_text = await (
            self._compact_openai() if self.use_openai else self._compact_anthropic()
        )
        if summary_text is not None:
            checkpoint = self._write_compaction_checkpoint(summary_text)
            if checkpoint is not None and self._runtime_store is not None:
                self._refresh_provider_context_from_canonical()
        else:
            return
        self._out_info("Conversation compacted.")

    def _compaction_context_messages(self) -> list[dict[str, Any]]:
        """Return a complete, source-preserving neutral compaction tail."""

        if self._pending_compaction_tail is not None:
            return [dict(message) for message in self._pending_compaction_tail]

        projection = self.project_canonical_model_context()
        if projection is not None and projection.messages:
            source = [dict(message) for message in projection.messages]
        else:
            source = self._neutralize_working_messages_for_compaction()

        groups: list[list[dict[str, Any]]] = []
        index = 0
        while index < len(source):
            message = source[index]
            role = message.get("role")
            if role == "tool":
                raise CompactionError("cannot compact an orphaned tool result")
            group = [message]
            if role == "assistant" and message.get("tool_calls"):
                expected = {
                    str(call.get("id"))
                    for call in message.get("tool_calls", [])
                    if isinstance(call, Mapping) and call.get("id")
                }
                index += 1
                while index < len(source) and source[index].get("role") == "tool":
                    result = source[index]
                    if result.get("tool_call_id") not in expected:
                        raise CompactionError("tool result does not belong to its call group")
                    group.append(result)
                    index += 1
                actual = {
                    str(item.get("tool_call_id"))
                    for item in group[1:]
                    if item.get("tool_call_id")
                }
                if actual != expected:
                    raise CompactionError("cannot compact an incomplete tool-call group")
                groups.append(group)
                continue
            groups.append(group)
            index += 1

        if len(groups) <= 1:
            return [dict(message) for message in source]
        tail_count = min(8, len(groups) - 1)
        tail = [message for group in groups[-tail_count:] for message in group]
        self._pending_compaction_tail = [dict(message) for message in tail]
        self._pending_compaction_summary_source = [
            dict(message) for group in groups[:-tail_count] for message in group
        ]
        return [dict(message) for message in tail]

    def _neutralize_working_messages_for_compaction(self) -> list[dict[str, Any]]:
        """Best-effort fallback for callers that have no canonical store."""

        source = self._openai_messages if self.use_openai else self._anthropic_messages
        result: list[dict[str, Any]] = []
        for message in source:
            role = message.get("role")
            if role == "system":
                continue
            if role == "assistant" and message.get("tool_calls"):
                calls = []
                for call in message["tool_calls"]:
                    function = call.get("function", call)
                    arguments = function.get("arguments", {})
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except json.JSONDecodeError:
                            pass
                    calls.append({
                        "id": call.get("id"),
                        "name": function.get("name"),
                        "arguments": arguments,
                    })
                result.append({"role": "assistant", "tool_calls": calls})
            elif role in {"user", "assistant", "tool"}:
                result.append({
                    key: value
                    for key, value in message.items()
                    if key in {"role", "content", "tool_call_id", "runtime_event_id"}
                })
        return result

    def _compaction_summary_messages(self) -> list[dict[str, Any]]:
        if self._pending_compaction_summary_source is None:
            self._compaction_context_messages()
        return [dict(message) for message in (self._pending_compaction_summary_source or [])]

    def _provider_messages_for_neutral(
        self, messages: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], ...]:
        result = ModelReplayResult(
            projection_version="projection-v1",
            schema_version=1,
            high_water=0,
            source_digest="compaction-source",
            digest="compaction-context",
            messages=tuple(messages),
            partial_count=0,
            diagnostics=(),
            context_epoch=self._context_epoch,
            context_id=self._runtime_context.context_id if self._runtime_context else self._runtime_context_id,
        )
        return CanonicalModelContextAdapter().build_result(
            result,
            provider="openai" if self.use_openai else "anthropic",
            system_prompt=None,
            archive_capability=self._archive_capability,
            budget_bytes=self._provider_budget_bytes(),
        ).messages

    def _write_compaction_checkpoint(self, summary_text: str) -> CompactionCheckpoint | None:
        """Persist a checkpoint and a reset marker after summarization succeeds."""

        if self._runtime_store is None or self._runtime_context is None:
            return None
        bounded_summary = str(summary_text)[:8192]
        retained_tail = self._compaction_context_messages()
        context_messages = [
            {"role": "user", "content": f"[Previous conversation summary]\n{bounded_summary}"},
            {
                "role": "assistant",
                "content": "Understood. I have the context from our previous conversation. How can I continue helping?",
            },
            *retained_tail,
        ]
        try:
            high_water = self._runtime_store.current_high_water
            context_id = self._runtime_context.context_id
            active_projection = ModelReplayProjection().build(
                self._runtime_store,
                high_water=high_water,
                context_id=context_id,
            )
            checkpoint = CompactionCheckpointBuilder().build(
                self._runtime_store,
                high_water=high_water,
                context_id=context_id,
                summary={
                    "text": bounded_summary,
                    "provider": "openai" if self.use_openai else "anthropic",
                    "context_message_count": len(context_messages),
                },
            )
            next_epoch = f"context:{checkpoint.checkpoint_id}"
            transition = build_context_transition(
                source_high_water=checkpoint.source_high_water,
                source_digest=checkpoint.source_digest,
                projection_version=checkpoint.projection_version,
                policy_version="compression-policy-v1",
                context_epoch=next_epoch,
                reason="full_compaction",
                replacements=[],
                effective_context=context_messages,
                context_id=context_id,
            )
            if self._runtime_emitter is None:
                return None
            transition_event = RuntimeEvent.create(
                self._runtime_context,
                role="system",
                author="system",
                actions={
                    "compaction": {
                        "checkpoint_id": checkpoint.checkpoint_id,
                        "source_high_water": checkpoint.source_high_water,
                        "source_digest": checkpoint.source_digest,
                        "reset_model_context": True,
                        "summary": bounded_summary,
                        "context_messages": context_messages,
                        "context_epoch": next_epoch,
                    },
                    "context_transition": transition.to_dict(),
                },
                refs={"checkpoint_id": checkpoint.checkpoint_id},
                ts=int(time.time() * 1000),
                metadata={
                    "lifecycle": "compaction_checkpoint",
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "context_epoch": next_epoch,
                },
            )
            prepared = self._runtime_emitter.prepare(transition_event)
            prepared_actions = prepared.actions or {}
            validate_transition_candidate(
                active_projection.messages,
                ContextTransition.from_value(
                    prepared_actions["context_transition"]
                ),
                source_high_water=checkpoint.source_high_water,
                source_digest=checkpoint.source_digest,
                expected_projection_version=checkpoint.projection_version,
                expected_policy_version="compression-policy-v1",
                current_context_epoch=active_projection.context_epoch,
                context_id=context_id,
                reset_context=(prepared_actions.get("compaction") or {}).get(
                    "context_messages", []
                ),
            )
            if hasattr(self._runtime_store, "append_compaction_transition"):
                self._runtime_store.append_compaction_transition(checkpoint, prepared)
            else:
                self._runtime_store.write_compaction_checkpoint(checkpoint)
                self._runtime_emitter.emit(prepared)
            self._context_epoch = next_epoch
            self._pending_compaction_tail = None
            self._pending_compaction_summary_source = None
            return checkpoint
        except Exception as error:
            self._pending_compaction_tail = None
            self._pending_compaction_summary_source = None
            self._runtime_exit_status = "failed"
            self._runtime_exit_reason = f"compaction checkpoint failed: {error}"
            if isinstance(error, CompactionError):
                raise
            raise CompactionError(f"compaction checkpoint failed: {error}") from error

    async def _compact_anthropic(self) -> str | None:
        self._compaction_context_messages()
        summary_source = self._compaction_summary_messages()
        if not summary_source:
            return None
        summary_resp = await self._anthropic_client.messages.create(
            model=self.model,
            max_tokens=4096,
            system="You are a conversation summarizer. Be concise but preserve important details.",
            messages=[
                *self._provider_messages_for_neutral(summary_source),
                {"role": "user", "content": "Summarize the conversation so far in a concise paragraph, preserving key decisions, file paths, and context needed to continue the work."},
            ],
        )
        summary_text = summary_resp.content[0].text if summary_resp.content and summary_resp.content[0].type == "text" else "No summary available."
        self.last_input_token_count = 0
        return summary_text

    async def _compact_openai(self) -> str | None:
        self._compaction_context_messages()
        summary_source = self._compaction_summary_messages()
        if not summary_source:
            return None
        summary_resp = await self._openai_client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "You are a conversation summarizer. Be concise but preserve important details."},
                *self._provider_messages_for_neutral(summary_source),
                {"role": "user", "content": "Summarize the conversation so far in a concise paragraph, preserving key decisions, file paths, and context needed to continue the work."},
            ],
        )
        summary_text = summary_resp.choices[0].message.content or "No summary available."
        self.last_input_token_count = 0
        return summary_text

    # ─── 多层压缩管线 ────────────────────────────────────────
    # legacy 路径每轮 API 调用前执行以下 3 层（Tier 1-3）：
    #   Tier 1: budget 截断 — 超出预算的大工具结果被头尾截断
    #   Tier 2: stale snip — 利用率 > 60% 时裁剪旧工具结果
    #   Tier 3: microcompact — 空闲 > 5 分钟时清除旧结果
    # Tier 4 (auto-compact) 在每轮 API 调用后检查触发。

    def _run_compression_pipeline(self, *, archive_aware: bool = False) -> None:
        """Run legacy result rewrites unless the live archive-aware path opts out.

        The live Provider loop refreshes from canonical replay immediately
        afterwards.  Running the old Tier 1-3 rewrites first would replace a
        newly completed tool result before its first Provider request, so that
        path is deliberately a no-op here.  Explicit legacy callers retain the
        existing compression behavior.
        """

        if archive_aware:
            return

        import copy

        previous_messages = copy.deepcopy(
            self._openai_messages if self.use_openai else self._anthropic_messages
        )
        before = self._capture_compression_tool_results()
        try:
            if self.use_openai:
                self._budget_tool_results_openai()
                self._snip_stale_results_openai()
                self._microcompact_openai()
            else:
                self._budget_tool_results_anthropic()
                self._snip_stale_results_anthropic()
                self._microcompact_anthropic()
            self._persist_compression_replacements(before)
        except Exception:
            if self.use_openai:
                self._openai_messages = previous_messages
            else:
                self._anthropic_messages = previous_messages
            raise

    def _compression_tool_result_entries(
        self,
    ) -> list[tuple[str, str, str | list[Any]]]:
        """Pair current provider-visible results with neutral source IDs."""

        if self._runtime_store is None and self._replay_cursor is None:
            return []
        replay = (
            self._replay_cursor.result()
            if self._replay_cursor is not None
            else self.project_canonical_model_context()
        )
        if replay is None:
            return []
        source_keys = [
            (message.get("runtime_event_id"), message.get("tool_call_id"))
            for message in replay.messages
            if message.get("role") == "tool"
        ]
        working: list[tuple[Any, Any]] = []
        if self.use_openai:
            working = [
                (message.get("tool_call_id"), message.get("content"))
                for message in self._openai_messages
                if message.get("role") == "tool"
            ]
        else:
            for message in self._anthropic_messages:
                if message.get("role") != "user" or not isinstance(message.get("content"), list):
                    continue
                for block in message["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        working.append((block.get("tool_use_id"), block.get("content")))
        entries: list[tuple[str, str, str | list[Any]]] = []
        for (event_id, source_call_id), (working_call_id, content) in zip(source_keys, working):
            if (
                isinstance(event_id, str)
                and event_id
                and isinstance(source_call_id, str)
                and source_call_id == working_call_id
                and isinstance(content, (str, list))
            ):
                entries.append((event_id, source_call_id, content))
        return entries

    def _capture_compression_tool_results(
        self,
    ) -> dict[str, tuple[str, str | list[Any]]]:
        return {
            event_id: (call_id, content)
            for event_id, call_id, content in self._compression_tool_result_entries()
        }

    def _current_compression_tool_results(self) -> dict[str, str | list[Any]]:
        return {
            event_id: content
            for event_id, _call_id, content in self._compression_tool_result_entries()
        }

    def _persist_compression_replacements(
        self, before: dict[str, tuple[str, str | list[Any]]]
    ) -> None:
        if not before or self._runtime_store is None or self._runtime_emitter is None or self._runtime_context is None:
            return
        current = self._current_compression_tool_results()
        replacements = [
            ContextReplacement(
                target_event_id=event_id,
                target_call_id=call_id,
                replacement=current[event_id],
                reason="lightweight_compression",
            )
            for event_id, (call_id, old_value) in before.items()
            if event_id in current and current[event_id] != old_value
        ]
        if not replacements:
            return
        source_high_water = self._runtime_store.current_high_water
        replay = ModelReplayProjection().build(
            self._runtime_store,
            high_water=source_high_water,
            context_id=self._runtime_context.context_id,
        )
        effective_context = {
            "replacements": [item.to_dict() for item in replacements]
        }
        transition = build_context_transition(
            source_high_water=source_high_water,
            source_digest=replay.source_digest,
            projection_version=replay.projection_version,
            policy_version="compression-policy-v1",
            context_epoch=self._context_epoch,
            reason="lightweight_compression",
            replacements=replacements,
            effective_context=effective_context,
            context_id=self._runtime_context.context_id,
        )
        event = RuntimeEvent.create(
            self._runtime_context,
            role="system",
            author="system",
            actions={"context_transition": transition.to_dict()},
            refs={"context_epoch": self._context_epoch},
            ts=int(time.time() * 1000),
            metadata={
                "lifecycle": "context_transition",
                "context_epoch": self._context_epoch,
                "reason": "lightweight_compression",
            },
        )
        try:
            prepared = self._runtime_emitter.prepare(event)
            prepared_transition = ContextTransition.from_value(
                (prepared.actions or {})["context_transition"]
            )
            validate_transition_candidate(
                replay.messages,
                prepared_transition,
                source_high_water=source_high_water,
                source_digest=replay.source_digest,
                expected_projection_version=replay.projection_version,
                expected_policy_version="compression-policy-v1",
                current_context_epoch=self._context_epoch,
                context_id=self._runtime_context.context_id,
            )
            if hasattr(self._runtime_store, "append_context_transition"):
                self._runtime_store.append_context_transition(
                    prepared,
                    source_high_water=source_high_water,
                    source_digest=replay.source_digest,
                    context_id=self._runtime_context.context_id,
                )
            else:
                self._runtime_emitter.emit(prepared)
        except Exception as error:
            self._runtime_exit_status = "failed"
            self._runtime_exit_reason = f"context transition failed: {error}"
            raise CompactionError(self._runtime_exit_reason) from error

    # Tier 1: 预算截断 — 当利用率 > 50% 时，将超长工具结果头尾保留、中间截断
    def _budget_tool_results_anthropic(self) -> None:
        utilization = self.last_input_token_count / self.effective_window if self.effective_window else 0
        if utilization < 0.5:
            return
        budget = 15000 if utilization > 0.7 else 30000
        for msg in self._anthropic_messages:
            if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
                continue
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), str) and len(block["content"]) > budget:
                    keep = (budget - 80) // 2
                    block["content"] = block["content"][:keep] + f"\n\n[... budgeted: {len(block['content']) - keep * 2} chars truncated ...]\n\n" + block["content"][-keep:]

    def _budget_tool_results_openai(self) -> None:
        utilization = self.last_input_token_count / self.effective_window if self.effective_window else 0
        if utilization < 0.5:
            return
        budget = 15000 if utilization > 0.7 else 30000
        for msg in self._openai_messages:
            if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and len(msg["content"]) > budget:
                keep = (budget - 80) // 2
                msg["content"] = msg["content"][:keep] + f"\n\n[... budgeted: {len(msg['content']) - keep * 2} chars truncated ...]\n\n" + msg["content"][-keep:]

    # Tier 2: 过期剪除 — 利用率 > 60% 时，裁剪旧的只读工具结果
    # 对同一文件多次读取只保留最近一次，只保留最后 KEEP_RECENT_RESULTS 个结果
    def _snip_stale_results_anthropic(self) -> None:
        utilization = self.last_input_token_count / self.effective_window if self.effective_window else 0
        if utilization < SNIP_THRESHOLD:
            return

        results = []
        for mi, msg in enumerate(self._anthropic_messages):
            if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
                continue
            for bi, block in enumerate(msg["content"]):
                if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), str) and block["content"] != SNIP_PLACEHOLDER:
                    tool_use_id = block.get("tool_use_id")
                    tool_info = self._find_tool_use_by_id(tool_use_id)
                    if tool_info and tool_info["name"] in SNIPPABLE_TOOLS:
                        results.append({"mi": mi, "bi": bi, "name": tool_info["name"], "file_path": tool_info.get("input", {}).get("file_path")})

        if len(results) <= KEEP_RECENT_RESULTS:
            return

        to_snip = set()
        seen_files: dict[str, list[int]] = {}
        for i, r in enumerate(results):
            if r["name"] == "read_file" and r.get("file_path"):
                seen_files.setdefault(r["file_path"], []).append(i)

        for indices in seen_files.values():
            if len(indices) > 1:
                for j in indices[:-1]:
                    to_snip.add(j)

        snip_before = len(results) - KEEP_RECENT_RESULTS
        for i in range(snip_before):
            to_snip.add(i)

        for idx in to_snip:
            r = results[idx]
            self._anthropic_messages[r["mi"]]["content"][r["bi"]]["content"] = SNIP_PLACEHOLDER

    def _snip_stale_results_openai(self) -> None:
        utilization = self.last_input_token_count / self.effective_window if self.effective_window else 0
        if utilization < SNIP_THRESHOLD:
            return
        tool_msgs = []
        for i, msg in enumerate(self._openai_messages):
            if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and msg["content"] != SNIP_PLACEHOLDER:
                tool_msgs.append(i)
        if len(tool_msgs) <= KEEP_RECENT_RESULTS:
            return
        snip_count = len(tool_msgs) - KEEP_RECENT_RESULTS
        for i in range(snip_count):
            self._openai_messages[tool_msgs[i]]["content"] = SNIP_PLACEHOLDER

    # Tier 3: 微压缩 — 空闲时间 > 5 分钟时，清除旧工具结果（标记为 [Old result cleared]）
    def _microcompact_anthropic(self) -> None:
        if not self.last_api_call_time or (time.time() - self.last_api_call_time) < MICROCOMPACT_IDLE_S:
            return
        all_results = []
        for mi, msg in enumerate(self._anthropic_messages):
            if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
                continue
            for bi, block in enumerate(msg["content"]):
                if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), str) and block["content"] not in (SNIP_PLACEHOLDER, "[Old result cleared]"):
                    all_results.append((mi, bi))
        clear_count = len(all_results) - KEEP_RECENT_RESULTS
        for i in range(max(0, clear_count)):
            mi, bi = all_results[i]
            self._anthropic_messages[mi]["content"][bi]["content"] = "[Old result cleared]"

    def _microcompact_openai(self) -> None:
        if not self.last_api_call_time or (time.time() - self.last_api_call_time) < MICROCOMPACT_IDLE_S:
            return
        tool_msgs = []
        for i, msg in enumerate(self._openai_messages):
            if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and msg["content"] not in (SNIP_PLACEHOLDER, "[Old result cleared]"):
                tool_msgs.append(i)
        clear_count = len(tool_msgs) - KEEP_RECENT_RESULTS
        for i in range(max(0, clear_count)):
            self._openai_messages[tool_msgs[i]]["content"] = "[Old result cleared]"

    def _find_tool_use_by_id(self, tool_use_id: str) -> dict | None:
        """根据 tool_use_id 在 Anthropic 消息历史中反向查找对应的工具调用信息。"""
        for msg in self._anthropic_messages:
            if msg.get("role") != "assistant" or not isinstance(msg.get("content"), list):
                continue
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id") == tool_use_id:
                    return {"name": block["name"], "input": block.get("input", {})}
        return None

    # ─── 工具执行路由 ──────────────────────────────────────
    # 统一分发：plan_mode / agent / skill / MCP / 标准工具。
    # agent 和 skill 在此处理以避免循环依赖（tools.py 不引用 agent.py）。

    async def _execute_tool_call(self, name: str, inp: dict) -> Any:
        if name in ("enter_plan_mode", "exit_plan_mode"):
            return await self._execute_plan_mode_tool(name)
        if name == "ArchiveRead":
            if self._archive_capability is None:
                return {
                    "kind": "archive_read_error",
                    "error_type": "capability_unavailable",
                    "message": "ArchiveRead capability is unavailable",
                }
            return self._archive_capability.execute(inp)
        if name == "agent":
            return await self._execute_agent_tool(inp)
        if name == "skill":
            return await self._execute_skill_tool(inp)
        # Route MCP tool calls to the MCP manager
        if self._mcp_manager.is_mcp_tool(name):
            return await self._mcp_manager.call_tool_value(name, inp)
        return await execute_tool_value(name, inp, self._read_file_state, context=self.context)

    # ─── Skill 执行（支持 inline / fork 双模式）─────────────
    # inline: 返回解析后的 prompt，注入当前对话
    # fork: 创建独立 sub-agent 执行，输出返回当前对话

    async def _execute_skill_tool(self, inp: dict) -> str:
        from .skills import execute_skill
        result = execute_skill(inp.get("skill_name", ""), inp.get("args", ""), self.context)
        if not result:
            return f"Unknown skill: {inp.get('skill_name', '')}"

        if result["context"] == "fork":
            tools = (
                [t for t in self.tools if t["name"] in result["allowed_tools"]]
                if result.get("allowed_tools")
                else [t for t in self.tools if t["name"] != "agent"]
            )
            skill_name = inp.get("skill_name", "")
            self._record_sub_agent_event(
                name=skill_name,
                agent_type="skill-fork",
                prompt=(inp.get("args") or ""),
            )
            self._out_sub_agent_start("skill-fork", skill_name)
            child_run_id = f"run-{self.session_id}-skill-{skill_name}-{uuid.uuid4().hex[:8]}"
            child_session_id = (
                self._runtime_context.session_id
                if self._runtime_context is not None
                else self.session_id
            )
            child_parent_run_id = (
                self._runtime_context.run_id if self._runtime_context else None
            )
            child_archive_capability = (
                self._archive_capability.derive(
                    session_id=child_session_id,
                    run_id=child_run_id,
                    parent_run_id=child_parent_run_id,
                )
                if self._archive_capability is not None
                else None
            )
            try:
                sub_agent = Agent(
                    model=self.model,
                    api_base=str(self._openai_client.base_url) if self.use_openai and self._openai_client else None,
                    thinking_effort=self.thinking_effort,
                    custom_system_prompt=result["prompt"],
                    custom_tools=tools,
                    is_sub_agent=True,
                    permission_mode="plan" if self.permission_mode == "plan" else "bypassPermissions",
                    runtime_store=self._runtime_store,
                    runtime_sink=self._runtime_sink,
                    runtime_parent_run_id=child_parent_run_id,
                    runtime_run_id=child_run_id,
                    runtime_session_id=child_session_id,
                    runtime_context_id=self._identity_factory.new("context"),
                    runtime_parent_context_id=(
                        self._runtime_context.context_id
                        if self._runtime_context is not None
                        else None
                    ),
                    artifact_archive=self._artifact_archive,
                    archive_capability=child_archive_capability,
                    llm_capture_policy=self._llm_capture_policy,
                    project_context=self.context,
                    output_port=self.output_port,
                    interaction_port=self.interaction_port,
                )
                sub_result = await sub_agent.run_once(inp.get("args") or "Execute this skill task.")
                self.total_input_tokens += sub_result["tokens"]["input"]
                self.total_output_tokens += sub_result["tokens"]["output"]
                self._out_sub_agent_end("skill-fork", inp.get("skill_name", ""))
                return sub_result["text"] or "(Skill produced no output)"
            except Exception as e:
                self._out_sub_agent_end("skill-fork", inp.get("skill_name", ""))
                return f"Skill fork error: {e}"
        # inline mode
        return f'[Skill "{inp.get("skill_name", "")}" activated]\n\n{result["prompt"]}'

    # ─── Plan Mode 辅助方法 ──────────────────────────────────

    def _generate_plan_file_path(self) -> str:
        """生成计划文件路径：~/.rollo/plans/plan-{session_id}.md"""
        d = Path.home() / ".rollo" / "plans"
        d.mkdir(parents=True, exist_ok=True)
        return str(d / f"plan-{self.session_id}.md")

    def _build_plan_mode_prompt(self) -> str:
        """构建 Plan Mode 的 system prompt 扩展：只读限制 + 计划文件路径。"""
        return f"""

# Plan Mode Active

Plan mode is active. You MUST NOT make any edits (except the plan file below), run non-readonly tools, or make any changes to the system.

## Plan File: {self._plan_file_path}
Write your plan incrementally to this file using write_file or edit_file. This is the ONLY file you are allowed to edit.

## Workflow
1. **Explore**: Read code to understand the task. Use read_file, list_files, grep_search.
2. **Design**: Design your implementation approach. Use the agent tool with type="plan" if the task is complex.
3. **Write Plan**: Write a structured plan to the plan file including:
   - **Context**: Why this change is needed
   - **Steps**: Implementation steps with critical file paths
   - **Verification**: How to test the changes
4. **Exit**: Call exit_plan_mode when your plan is ready for user review.

IMPORTANT: When your plan is complete, you MUST call exit_plan_mode. Do NOT ask the user to approve — exit_plan_mode handles that."""

    async def _execute_plan_mode_tool(self, name: str) -> str:
        """enter_plan_mode / exit_plan_mode 工具的实现。
        exit_plan_mode 包含审批流程：4 选项（clear-execute / execute / manual / keep-planning）。"""
        if name == "enter_plan_mode":
            if self.permission_mode == "plan":
                return "Already in plan mode."
            self._pre_plan_mode = self.permission_mode
            self.permission_mode = "plan"
            self._plan_file_path = self._generate_plan_file_path()
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
            if self.use_openai and self._openai_messages:
                self._openai_messages[0]["content"] = self._system_prompt
            self._out_info("Entered plan mode (read-only). Plan file: " + self._plan_file_path)
            return f"Entered plan mode. You are now in read-only mode.\n\nYour plan file: {self._plan_file_path}\nWrite your plan to this file. This is the only file you can edit.\n\nWhen your plan is complete, call exit_plan_mode."

        if name == "exit_plan_mode":
            if self.permission_mode != "plan":
                return "Not in plan mode."
            plan_content = "(No plan file found)"
            if self._plan_file_path and Path(self._plan_file_path).exists():
                plan_content = Path(self._plan_file_path).read_text()

            # Application-owned plan approval uses the same C02 registry/Future
            # envelope as tool approval.  The terminal adapter may render this
            # as a simple yes/no decision; the control plane still persists the
            # plan identity and digest before the decision can affect mode.
            if self._application_interaction_mode:
                from .application import params_digest as application_params_digest, plan_digest as application_plan_digest

                plan_id = self._plan_file_path or f"plan:{self.session_id}:{self._runtime_run_id or self.session_id}"
                digest = application_plan_digest(plan_id, plan_content)
                request_id = f"plan-approval-{self.session_id}-{uuid.uuid4().hex[:8]}"
                request = InteractionRequest(
                    request_id=request_id,
                    kind=InteractionKind.APPROVAL,
                    session_id=str(self.session_id),
                    run_id=str(self._runtime_run_id or self.session_id),
                    params_digest=application_params_digest(
                        session_id=str(self.session_id),
                        run_id=str(self._runtime_run_id or self.session_id),
                        request_id=request_id,
                        tool_call_id=None,
                        tool_name="plan_approval",
                        tool_input={"plan": plan_content},
                        plan_id=plan_id,
                        plan_digest_value=digest,
                    ),
                    prompt=plan_content,
                    tool_call_id=None,
                    tool_name="plan_approval",
                    tool_input={"plan": plan_content},
                    plan_id=plan_id,
                    plan_digest=digest,
                    metadata={"plan_approval": True},
                )
                self.interaction_registry.open(request)
                try:
                    reply = await self.interaction_port.request(request)
                    resolved = self.interaction_registry.resolve(reply)
                except asyncio.CancelledError:
                    with contextlib.suppress(InteractionError):
                        self.interaction_registry.cancel(request_id)
                    raise
                except InteractionError:
                    return "Plan approval was interrupted or rejected. Continue planning."
                if not resolved.approved:
                    return "User rejected the plan and wants to keep planning."
                choice = (resolved.answer or "execute").strip().lower()
                if choice not in {"clear-and-execute", "execute", "manual-execute"}:
                    choice = "execute"
            else:
                choice = None

            # Interactive approval flow
            if choice is None and self._plan_approval_fn:
                result = await self._plan_approval_fn(plan_content)
                choice = result.get("choice", "manual-execute")

                if choice == "keep-planning":
                    feedback = result.get("feedback") or "Please revise the plan."
                    return (
                        f"User rejected the plan and wants to keep planning.\n\n"
                        f"User feedback: {feedback}\n\n"
                        f"Please revise your plan based on this feedback. When done, call exit_plan_mode again."
                    )

                # User approved — determine target mode
                if choice == "clear-and-execute":
                    target_mode = "acceptEdits"
                elif choice == "execute":
                    target_mode = "acceptEdits"
                else:  # manual-execute
                    target_mode = self._pre_plan_mode or "default"

                # Exit plan mode
                self.permission_mode = target_mode
                self._pre_plan_mode = None
                saved_plan_path = self._plan_file_path
                self._plan_file_path = None
                self._system_prompt = self._base_system_prompt
                if self.use_openai and self._openai_messages:
                    self._openai_messages[0]["content"] = self._system_prompt

                if choice == "clear-and-execute":
                    self._clear_history_keep_system()
                    self._context_cleared = True
                    self._out_info(f"Plan approved. Context cleared, executing in {target_mode} mode.")
                    return (
                        f"User approved the plan. Context was cleared. Permission mode: {target_mode}\n\n"
                        f"Plan file: {saved_plan_path}\n\n"
                        f"## Approved Plan:\n{plan_content}\n\n"
                        f"Proceed with implementation."
                    )

                self._out_info(f"Plan approved. Executing in {target_mode} mode.")
                return (
                    f"User approved the plan. Permission mode: {target_mode}\n\n"
                    f"## Approved Plan:\n{plan_content}\n\n"
                    f"Proceed with implementation."
                )

            if choice is not None:
                target_mode = "acceptEdits" if choice in {"clear-and-execute", "execute"} else (self._pre_plan_mode or "default")
                self.permission_mode = target_mode
                self._pre_plan_mode = None
                saved_plan_path = self._plan_file_path
                self._plan_file_path = None
                self._system_prompt = self._base_system_prompt
                if self.use_openai and self._openai_messages:
                    self._openai_messages[0]["content"] = self._system_prompt
                if choice == "clear-and-execute":
                    self._clear_history_keep_system()
                    self._context_cleared = True
                return (
                    f"User approved the plan. Permission mode: {target_mode}\n\n"
                    f"Plan file: {saved_plan_path}\n\n"
                    f"## Approved Plan:\n{plan_content}\n\n"
                    "Proceed with implementation."
                )

            # Fallback: no approval function (e.g. sub-agents)
            self.permission_mode = self._pre_plan_mode or "default"
            self._pre_plan_mode = None
            self._plan_file_path = None
            self._system_prompt = self._base_system_prompt
            if self.use_openai and self._openai_messages:
                self._openai_messages[0]["content"] = self._system_prompt
            self._out_info("Exited plan mode. Restored to " + self.permission_mode + " mode.")
            return f"Exited plan mode. Permission mode restored to: {self.permission_mode}\n\n## Your Plan:\n{plan_content}"

        return f"Unknown plan mode tool: {name}"

    def _clear_history_keep_system(self) -> None:
        """清空历史但保留 system prompt（Plan Mode 'clear-and-execute' 选项用）。"""
        self._anthropic_messages = []
        self._openai_messages = []
        if self.use_openai:
            self._openai_messages.append({"role": "system", "content": self._system_prompt})
        self.last_input_token_count = 0

    async def _execute_agent_tool(self, inp: dict) -> str:
        """执行 agent 工具 — fork-return 模式启动子 Agent。
        子 Agent 使用独立上下文运行，token 消耗回计到父 Agent。"""
        agent_type = inp.get("type", "general")
        description = inp.get("description", "sub-agent task")
        prompt = inp.get("prompt", "")

        self._out_sub_agent_start(agent_type, description)

        self._record_sub_agent_event(
            name=description,
            agent_type=agent_type,
            prompt=prompt,
        )

        config = get_sub_agent_config(agent_type, self.context)
        child_run_id = f"run-{self.session_id}-{agent_type}-{uuid.uuid4().hex[:8]}"
        child_session_id = (
            self._runtime_context.session_id
            if self._runtime_context is not None
            else self.session_id
        )
        child_parent_run_id = (
            self._runtime_context.run_id if self._runtime_context else None
        )
        child_archive_capability = (
            self._archive_capability.derive(
                session_id=child_session_id,
                run_id=child_run_id,
                parent_run_id=child_parent_run_id,
            )
            if self._archive_capability is not None
            else None
        )
        try:
            sub_agent = Agent(
                model=self.model,
                api_base=str(self._openai_client.base_url) if self.use_openai and self._openai_client else None,
                thinking_effort=self.thinking_effort,
                custom_system_prompt=config["system_prompt"],
                custom_tools=config["tools"],
                is_sub_agent=True,
                permission_mode="plan" if self.permission_mode == "plan" else "bypassPermissions",
                runtime_store=self._runtime_store,
                runtime_sink=self._runtime_sink,
                runtime_parent_run_id=child_parent_run_id,
                runtime_run_id=child_run_id,
                runtime_session_id=child_session_id,
                runtime_context_id=self._identity_factory.new("context"),
                runtime_parent_context_id=(
                    self._runtime_context.context_id
                    if self._runtime_context is not None
                    else None
                ),
                artifact_archive=self._artifact_archive,
                archive_capability=child_archive_capability,
                llm_capture_policy=self._llm_capture_policy,
                project_context=self.context,
                output_port=self.output_port,
                interaction_port=self.interaction_port,
            )
            result = await sub_agent.run_once(prompt)
            self.total_input_tokens += result["tokens"]["input"]
            self.total_output_tokens += result["tokens"]["output"]
            self._out_sub_agent_end(agent_type, description)
            return result["text"] or "(Sub-agent produced no output)"
        except Exception as e:
            self._out_sub_agent_end(agent_type, description)
            return f"Sub-agent error: {e}"

    # ─── Anthropic 后端 ─────────────────────────────────────
    # 流式调用 Anthropic API；工具统一在完整 final boundary 后执行。

    async def _chat_anthropic(self, user_message: str) -> None:
        self._anthropic_messages.append({"role": "user", "content": user_message})

        # 启动异步记忆预取（非阻塞，每个用户轮次触发一次）
        memory_prefetch: MemoryPrefetch | None = None
        if not self.is_sub_agent:
            sq = self._build_side_query()
            if sq:
                memory_prefetch = start_memory_prefetch(
                    user_message, sq,
                    self._already_surfaced_memories, self._session_memory_bytes,
                    self.context,
                )

        while True:
            if self._aborted:
                break

            request_id = uuid.uuid4().hex
            self._current_request_id = request_id

            _pre_size = self._msg_char_count()
            self._run_compression_pipeline(archive_aware=True)
            self._refresh_provider_context_from_canonical()
            _post_size = self._msg_char_count()
            if _post_size < _pre_size:
                utilization = self.last_input_token_count / self.effective_window if self.effective_window else 0
                tier = 1 if utilization > 0.5 else 2 if utilization > SNIP_THRESHOLD else 3
                await self._emit("compaction", {"tier": tier})

            # 本轮 index
            turn_index = self.current_turns + 1
            await self._emit("turn_start", {"turn_index": turn_index})

            # 消费记忆预取结果（非阻塞轮询，zero-wait）。
            if memory_prefetch and memory_prefetch.settled and not memory_prefetch.consumed:
                memories = memory_prefetch.task.result()
                if memories:
                    self._persist_memory_context_event(memories)
                    self._refresh_provider_context_from_canonical()
                    for m in memories:
                        self._already_surfaced_memories.add(m.path)
                        self._session_memory_bytes += len(m.content.encode())
                memory_prefetch.consumed = True

            if not self.is_sub_agent:
                self._out_start_spinner()

            api_start = time.time()
            self._start_runtime_model_call(request_id, "anthropic", {"messages": self._anthropic_messages})
            try:
                response = await self._call_anthropic_stream()
            except Exception as error:
                self._record_runtime_model_error(error)
                raise

            if not self.is_sub_agent:
                self._out_stop_spinner()

            self.last_api_call_time = time.time()
            self.total_input_tokens += response.usage.input_tokens
            self.total_output_tokens += response.usage.output_tokens
            self.last_input_token_count = response.usage.input_tokens

            tool_uses = [
                block for block in response.content if getattr(block, "type", None) == "tool_use"
            ]

            try:
                normalized_blocks = self._normalize_anthropic_response_blocks(response.content)
            except ProviderContentNormalizationError as error:
                self._record_runtime_model_error(error)
                raise

            if self._runtime_recorder:
                try:
                    for block_index, block in enumerate(normalized_blocks):
                        if block["type"] == "text":
                            text = _normalize_provider_text(
                                block.get("text"),
                                provider="anthropic",
                                block_kind="text",
                                block_index=block_index,
                            )
                            self._runtime_recorder.final_text(text)
                        elif block["type"] == "thinking":
                            thinking = _normalize_provider_text(
                                block.get("thinking"),
                                provider="anthropic",
                                block_kind="thinking",
                                block_index=block_index,
                            )
                            self._runtime_recorder.final_thinking(
                                thinking, signature=block.get("signature")
                            )
                        elif block["type"] == "tool_use":
                            self._runtime_recorder.final_tool_call(
                                block["id"], block["name"], block.get("input", {})
                            )
                except ProviderContentNormalizationError as error:
                    self._record_runtime_model_error(error)
                    raise

            # ★ 发射 turn_end 事件
            cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
            cache_create = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
            finish = "end_turn" if tool_uses else "stop"
            if self._runtime_recorder:
                self._runtime_recorder.finish(
                    finish,
                    usage={
                        "input_tokens": response.usage.input_tokens,
                        "output_tokens": response.usage.output_tokens,
                        "cache_read_tokens": cache_read,
                        "cache_create_tokens": cache_create,
                    },
                    latency_ms=int((time.time() - api_start) * 1000),
                )
            await self._emit("turn_end", {
                "turn_index": turn_index,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cache_read_tokens": cache_read,
                "cache_create_tokens": cache_create,
                "finish_reason": finish,
            })

            if self._llm_capture_manager:
                latency_ms = int((time.time() - api_start) * 1000)
                response_dict = {"content": normalized_blocks}
                usage_dict = {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}
                self._capture_llm(
                    request_id=request_id,
                    messages=self._anthropic_messages,
                    response=response_dict,
                    usage=usage_dict,
                    latency_ms=latency_ms,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    cache_read_tokens=cache_read,
                    finish_reason=finish,
                )

            message = {
                "role": "assistant",
                "content": normalized_blocks,
            }

            self._anthropic_messages.append(message)

            if not tool_uses:
                if not self.is_sub_agent:
                    self._out_cost(self.total_input_tokens, self.total_output_tokens)
                self._out_lifecycle(
                    "turn_complete",
                    turns=self.current_turns,
                    attempt_id=self._current_attempt_id(),
                )
                break

            self.current_turns += 1
            budget = self._check_budget()
            if budget["exceeded"]:
                self._out_info(f"Budget exceeded: {budget['reason']}")
                self._out_error(f"Budget exceeded: {budget['reason']}")
                self._record_budget_exceeded(budget["reason"])
                break

            # Process complete tool calls only after the model final boundary.
            tool_results: list[dict] = []
            context_break = False
            for tu in tool_uses:
                if context_break or self._aborted:
                    break
                inp = dict(tu.input) if hasattr(tu.input, 'items') else tu.input
                self._out_tool_call(tu.name, inp, getattr(tu, "id", None))

                # 非提前启动工具的权限检查
                perm = check_permission(
                    tu.name, inp, self.permission_mode, self._plan_file_path,
                    context=self.context,
                )
                if perm["action"] == "deny":
                    self._out_info(f"Denied: {perm.get('message', '')}")
                    self._out_tool_denied(tu.name, perm.get("message", ""), getattr(tu, "id", None))
                    raw, success, executed = await self._run_durable_tool(
                        request_id=request_id, call_id=tu.id, name=tu.name, inp=inp,
                        permission={"decision": "deny", "reason": perm.get("message", "")},
                    )
                    res = materialize_tool_result(raw, provider="anthropic")
                    tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": res})
                    continue
                if perm["action"] == "confirm" and perm.get("message") and perm["message"] not in self._confirmed_paths:
                    confirmed = await self._confirm_dangerous(perm["message"])
                    if not confirmed:
                        self._out_tool_denied(
                            tu.name, perm.get("message", ""), getattr(tu, "id", None)
                        )
                        raw, success, executed = await self._run_durable_tool(
                            request_id=request_id, call_id=tu.id, name=tu.name, inp=inp,
                            permission={"decision": "deny", "reason": perm["message"]},
                        )
                        res = materialize_tool_result(raw, provider="anthropic")
                        tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": res})
                        continue
                    current_perm = check_permission(
                        tu.name, inp, self.permission_mode, self._plan_file_path,
                        context=self.context,
                    )
                    if current_perm["action"] == "deny":
                        self._out_info(f"Denied: {current_perm.get('message', '')}")
                        self._out_tool_denied(
                            tu.name, current_perm.get("message", ""), getattr(tu, "id", None)
                        )
                        raw, success, executed = await self._run_durable_tool(
                            request_id=request_id, call_id=tu.id, name=tu.name, inp=inp,
                            permission={
                                "decision": "deny",
                                "reason": current_perm.get("message", ""),
                            },
                        )
                        res = materialize_tool_result(raw, provider="anthropic")
                        tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": res})
                        continue
                    self._confirmed_paths.add(perm["message"])

                t0 = time.time()
                raw, success, executed = await self._run_durable_tool(
                    request_id=request_id, call_id=tu.id, name=tu.name, inp=inp,
                    permission={"decision": "allow", "reason": "permission granted"},
                )
                tool_duration = int((time.time() - t0) * 1000)
                res = materialize_tool_result(raw, provider="anthropic")
                await self._emit("tool_end", {
                    "tool_name": tu.name,
                    "tool_input": inp,
                    "duration_ms": tool_duration,
                    "result_length": len(materialized_content_bytes(res)) if res else 0,
                    "success": success,
                })
                self._out_tool_result(
                    tu.name,
                    self._display_tool_result(raw, provider="anthropic"),
                    tool_call_id=getattr(tu, "id", None),
                )

                # Plan Mode 'clear-and-execute' 后：直接追加工具结果并跳出
                if self._context_cleared:
                    self._context_cleared = False
                    self._anthropic_messages.append({"role": "user", "content": res})
                    context_break = True
                    break
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": res})

            if not context_break and tool_results:
                self._anthropic_messages.append({"role": "user", "content": tool_results})
            self._context_cleared = False


            await self._check_and_compact()

    @staticmethod
    def _block_to_dict(block) -> dict:
        """将 Anthropic content block 对象转为 dict 以便 JSON 序列化存储。"""
        if block.type == "text":
            return {"type": "text", "text": block.text}
        if block.type == "thinking":
            result = {"type": "thinking", "thinking": block.thinking}
            signature = getattr(block, "signature", None)
            if signature is not None:
                result["signature"] = signature
            return result
        if block.type == "tool_use":
            return {"type": "tool_use", "id": block.id, "name": block.name, "input": dict(block.input) if hasattr(block.input, 'items') else block.input}
        # Fallback
        return {"type": block.type}

    def _normalize_anthropic_response_blocks(self, content: Any) -> list[dict[str, Any]]:
        """Validate provider text fields before recording or replay storage."""

        normalized: list[dict[str, Any]] = []
        for index, block in enumerate(content):
            block_kind = getattr(block, "type", None)
            if block_kind == "text":
                normalized.append(
                    {
                        "type": "text",
                        "text": _normalize_provider_text(
                            getattr(block, "text", None),
                            provider="anthropic",
                            block_kind="text",
                            block_index=index,
                        ),
                    }
                )
            elif block_kind == "thinking":
                thinking = _normalize_provider_text(
                    getattr(block, "thinking", None),
                    provider="anthropic",
                    block_kind="thinking",
                    block_index=index,
                )
                signature = getattr(block, "signature", None)
                if signature is not None and not isinstance(signature, str):
                    raise ProviderContentNormalizationError(
                        provider="anthropic",
                        block_kind="thinking",
                        block_index=index,
                        value=signature,
                    )
                item: dict[str, Any] = {"type": "thinking", "thinking": thinking}
                if signature is not None:
                    item["signature"] = signature
                normalized.append(item)
            else:
                normalized.append(self._block_to_dict(block))
        return normalized

    async def _call_anthropic_stream(self, on_tool_block_complete=None):
        """流式解析 Anthropic 响应；只记录 partial，不提前执行工具。"""
        async def _do():
            self._assert_provider_request_fits("anthropic", self._anthropic_messages)
            create_params: dict[str, Any] = {
                "model": self.model,
                "max_tokens": _get_anthropic_request_max_tokens(self.model),
                "system": self._system_prompt,
                "tools": self._effective_tool_definitions(),
                "messages": self._anthropic_messages,
            }

            create_params.update(
                _thinking_request_params(
                    self.model,
                    self.thinking_effort,
                    use_openai=False,
                )
            )

            first_text = True
            first_thinking = True
            # 跟踪流式传输中的 tool_use 块（按 index），便于 content_block_stop 时解析执行
            tool_blocks_by_index: dict[int, dict] = {}

            async with self._anthropic_client.messages.stream(**create_params) as stream:
                async for event in stream:
                    if not hasattr(event, 'type'):
                        continue

                    if event.type == "content_block_start":
                        cb = getattr(event, 'content_block', None)
                        if cb and getattr(cb, 'type', None) == "tool_use":
                            tool_blocks_by_index[event.index] = {
                                "id": cb.id, "name": cb.name, "input_json": "",
                            }

                    elif event.type == "content_block_delta":
                        delta = event.delta
                        if hasattr(delta, 'text'):
                            text = _normalize_provider_text(
                                getattr(delta, "text", None),
                                provider="anthropic",
                                block_kind="text",
                                block_index=getattr(event, "index", -1),
                            )
                            if first_text:
                                self._out_stop_spinner()
                                self._emit_text("\n")
                                first_text = False
                                # ★ 发射 first_token 事件
                                await self._emit("first_token", {"is_thinking": False})
                            self._emit_text(text)
                            if self._runtime_recorder:
                                self._runtime_recorder.partial_text(text)
                        elif hasattr(delta, 'thinking'):
                            thinking = _normalize_provider_text(
                                getattr(delta, "thinking", None),
                                provider="anthropic",
                                block_kind="thinking",
                                block_index=getattr(event, "index", -1),
                            )
                            if first_thinking:
                                self._out_stop_spinner()
                                self._emit_text("\n")
                                first_thinking = False
                                # ★ 发射 first_token 事件
                                await self._emit("first_token", {"is_thinking": True})
                            self._out_thinking(thinking)
                            if self._runtime_recorder:
                                self._runtime_recorder.partial_text(thinking, kind="thinking")
                        elif hasattr(delta, 'partial_json'):
                            tb = tool_blocks_by_index.get(event.index)
                            if tb:
                                tb["input_json"] += delta.partial_json
                                if self._runtime_recorder:
                                    self._runtime_recorder.partial_tool_arguments(
                                        tb["id"], tb["name"], delta.partial_json
                                    )

                    elif event.type == "content_block_stop":
                        # The final response is the only executable boundary.
                        # Do not invoke a tool from content_block_stop: the
                        # caller still needs to validate and durably dispatch
                        # the complete function call below.
                        tool_blocks_by_index.pop(event.index, None)

                final_message = await stream.get_final_message()

            # Thinking blocks, including their signatures, are part of the
            # provider response state and must be preserved for the next
            # request.  The display path already streams their text; removing
            # them here would make tool-use follow-ups invalid for Anthropic
            # and compatible endpoints.
            final_message.content = [b for b in final_message.content]
            return final_message

        def _record_retry(attempt: int, error: Exception) -> None:
            if self._runtime_recorder:
                self._runtime_recorder.retry(attempt=attempt, reason=str(error))

        return await _with_retry(_do, on_retry=_record_retry)

    # ─── OpenAI 兼容后端 ────────────────────────────────────
    # 流式调用 OpenAI API，与 Anthropic 后端功能等价。
    # 并行执行：相邻的并发安全工具通过 asyncio.gather 并行执行。

    async def _chat_openai(self, user_message: str) -> None:
        self._openai_messages.append({"role": "user", "content": user_message})

        # 启动异步记忆预取（非阻塞，每个用户轮次触发一次）
        memory_prefetch: MemoryPrefetch | None = None
        if not self.is_sub_agent:
            sq = self._build_side_query()
            if sq:
                memory_prefetch = start_memory_prefetch(
                    user_message, sq,
                    self._already_surfaced_memories, self._session_memory_bytes,
                    self.context,
                )

        while True:
            if self._aborted:
                break

            request_id = uuid.uuid4().hex
            self._current_request_id = request_id

            _pre_size = self._msg_char_count()
            self._run_compression_pipeline(archive_aware=True)
            self._refresh_provider_context_from_canonical()
            _post_size = self._msg_char_count()
            if _post_size < _pre_size:
                utilization = self.last_input_token_count / self.effective_window if self.effective_window else 0
                tier = 1 if utilization > 0.5 else 2 if utilization > SNIP_THRESHOLD else 3
                await self._emit("compaction", {"tier": tier})

            # 本轮 index
            turn_index = self.current_turns + 1
            await self._emit("turn_start", {"turn_index": turn_index})

            # Consume memory prefetch if settled (non-blocking poll, zero-wait)
            if memory_prefetch and memory_prefetch.settled and not memory_prefetch.consumed:
                memories = memory_prefetch.task.result()
                if memories:
                    self._persist_memory_context_event(memories)
                    self._refresh_provider_context_from_canonical()
                    for m in memories:
                        self._already_surfaced_memories.add(m.path)
                        self._session_memory_bytes += len(m.content.encode())
                memory_prefetch.consumed = True

            if not self.is_sub_agent:
                self._out_start_spinner()

            api_start = time.time()
            self._start_runtime_model_call(request_id, "openai", {"messages": self._openai_messages})
            try:
                response = await self._call_openai_stream()
            except Exception as error:
                if self._runtime_recorder:
                    self._runtime_recorder.error(error)
                    if self._runtime_guard and self._runtime_recorder.events:
                        self._runtime_guard.adopt_terminal_event(self._runtime_recorder.events[-1])
                raise

            if not self.is_sub_agent:
                self._out_stop_spinner()

            self.last_api_call_time = time.time()

            usage = response.get("usage") or {}
            if usage:
                self.total_input_tokens += usage["prompt_tokens"]
                self.total_output_tokens += usage["completion_tokens"]
                self.last_input_token_count = usage["prompt_tokens"]

            choice = response.get("choices", [{}])[0] if response.get("choices") else {}
            message = choice.get("message", {})
            tool_calls = message.get("tool_calls")

            try:
                normalized_message = dict(message)
                if "content" in message and message.get("content") is not None:
                    normalized_message["content"] = _normalize_provider_text(
                        message["content"],
                        provider="openai",
                        block_kind="text",
                        block_index=0,
                    )
                if "reasoning_content" in message:
                    normalized_message["reasoning_content"] = _normalize_provider_text(
                        message["reasoning_content"],
                        provider="openai",
                        block_kind="thinking",
                        block_index=0,
                    )
                message = normalized_message
                if self._runtime_recorder:
                    if "reasoning_content" in message:
                        self._runtime_recorder.final_thinking(
                            message["reasoning_content"]
                        )
                    if "content" in message and message.get("content") is not None:
                        self._runtime_recorder.final_text(message["content"])
                    for tool_call in tool_calls or []:
                        function = tool_call.get("function", {})
                        self._runtime_recorder.final_tool_call(
                            tool_call.get("id", "unknown-call"),
                            function.get("name", "unknown"),
                            function.get("arguments", "{}"),
                        )
            except ProviderContentNormalizationError as error:
                self._record_runtime_model_error(error)
                raise

            self._openai_messages.append(message)

            # ★ 发射 turn_end
            cache_read = 0
            pt_details = usage.get("prompt_tokens_details")
            if isinstance(pt_details, dict):
                cache_read = pt_details.get("cached_tokens", 0)
            finish = "end_turn" if tool_calls else "stop"
            if self._runtime_recorder:
                self._runtime_recorder.finish(
                    finish,
                    usage={
                        "input_tokens": usage.get("prompt_tokens"),
                        "output_tokens": usage.get("completion_tokens"),
                        "cache_read_tokens": cache_read,
                    },
                    latency_ms=int((time.time() - api_start) * 1000),
                )
            await self._emit("turn_end", {
                "turn_index": turn_index,
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "cache_read_tokens": cache_read,
                "cache_create_tokens": 0,  # OpenAI 不单独报告创建
                "finish_reason": finish,
            })
            if self._llm_capture_manager:
                self._capture_llm(
                    request_id=request_id,
                    messages=self._openai_messages.copy(),
                    response={"choices": [{"message": message}]},
                    usage={
                        "input_tokens": usage.get("prompt_tokens"),
                        "output_tokens": usage.get("completion_tokens"),
                    },
                    latency_ms=int((time.time() - api_start) * 1000),
                    input_tokens=usage.get("prompt_tokens"),
                    output_tokens=usage.get("completion_tokens"),
                    cache_read_tokens=cache_read,
                    finish_reason=finish,
                )
            if not tool_calls:
                if not self.is_sub_agent:
                    self._out_cost(self.total_input_tokens, self.total_output_tokens)
                self._out_lifecycle(
                    "turn_complete",
                    turns=self.current_turns,
                    attempt_id=self._current_attempt_id(),
                )
                break

            self.current_turns += 1
            budget = self._check_budget()
            if budget["exceeded"]:
                self._out_info(f"Budget exceeded: {budget['reason']}")
                self._out_error(f"Budget exceeded: {budget['reason']}")
                self._record_budget_exceeded(budget["reason"])
                break

            # Phase 1: 解析 & 权限检查（串行 — 因为权限确认需要用户交互）
            oai_checked: list[dict] = []
            for tc in tool_calls:
                if self._aborted:
                    break
                if tc.get("type") != "function":
                    continue
                fn_name = tc["function"]["name"]
                fn_id = tc.get("id")
                raw_arguments = tc["function"].get("arguments", "")
                try:
                    inp = json.loads(raw_arguments)
                except Exception:
                    inp = {}

                self._out_tool_call(fn_name, inp, fn_id)

                perm = check_permission(
                    fn_name, inp, self.permission_mode, self._plan_file_path,
                    context=self.context,
                )
                if perm["action"] == "deny":
                    self._out_info(f"Denied: {perm.get('message', '')}")
                    self._out_tool_denied(fn_name, perm.get("message", ""), tc.get("id"))
                    oai_checked.append({
                        "tc": tc, "fn": fn_name, "inp": inp, "allowed": False,
                        "arguments_raw": raw_arguments, "decision": "deny", "reason": perm.get("message", ""),
                    })
                    continue
                if perm["action"] == "confirm" and perm.get("message") and perm["message"] not in self._confirmed_paths:
                    confirmed = await self._confirm_dangerous(perm["message"])
                    if not confirmed:
                        self._out_tool_denied(fn_name, perm["message"], tc.get("id"))
                        oai_checked.append({
                            "tc": tc, "fn": fn_name, "inp": inp, "allowed": False,
                            "arguments_raw": raw_arguments, "decision": "deny", "reason": perm["message"],
                        })
                        continue
                    current_perm = check_permission(
                        fn_name, inp, self.permission_mode, self._plan_file_path,
                        context=self.context,
                    )
                    if current_perm["action"] == "deny":
                        self._out_info(f"Denied: {current_perm.get('message', '')}")
                        self._out_tool_denied(
                            fn_name, current_perm.get("message", ""), tc.get("id")
                        )
                        oai_checked.append({
                            "tc": tc, "fn": fn_name, "inp": inp, "allowed": False,
                            "arguments_raw": raw_arguments, "decision": "deny",
                            "reason": current_perm.get("message", ""),
                        })
                        continue
                    self._confirmed_paths.add(perm["message"])
                oai_checked.append({
                    "tc": tc, "fn": fn_name, "inp": inp, "allowed": True,
                    "arguments_raw": raw_arguments, "decision": "allow", "reason": "permission granted",
                })

            # Phase 2: 分组 & 执行（连续的并发安全工具分组后 asyncio.gather 并行执行）
            oai_batches: list[dict] = []
            for ct in oai_checked:
                safe = ct["allowed"] and ct["fn"] in CONCURRENCY_SAFE_TOOLS
                if safe and oai_batches and oai_batches[-1]["concurrent"]:
                    oai_batches[-1]["items"].append(ct)
                else:
                    oai_batches.append({"concurrent": safe, "items": [ct]})

            oai_context_break = False
            for batch in oai_batches:
                if oai_context_break or self._aborted:
                    break

                if batch["concurrent"]:
                    async def _run_oai_safe(ct_item: dict) -> tuple[dict, str, bool, Any]:
                        raw, success, executed = await self._run_durable_tool(
                            request_id=request_id,
                            call_id=ct_item["tc"].get("id", f"tool-{ct_item['fn']}"),
                            name=ct_item["fn"],
                            inp=ct_item["inp"],
                            arguments=ct_item.get("arguments_raw"),
                            permission={"decision": "allow", "reason": ct_item.get("reason", "")},
                        )
                        res = materialize_tool_result(raw, provider="openai")
                        return ct_item, res, success, raw

                    t0_batch = time.time()
                    results = await asyncio.gather(*[_run_oai_safe(ct) for ct in batch["items"]])
                    for ct_item, res, success, raw in results:
                        tool_duration = int((time.time() - t0_batch) * 1000)
                        await self._emit("tool_end", {
                            "tool_name": ct_item["fn"],
                            "tool_input": ct_item["inp"],
                            "duration_ms": tool_duration,
                            "result_length": len(materialized_content_bytes(res)) if res else 0,
                            "success": success,
                        })
                        self._out_tool_result(
                            ct_item["fn"],
                            self._display_tool_result(raw, provider="openai"),
                            tool_call_id=ct_item.get("tc", {}).get("id"),
                        )
                        self._openai_messages.append({"role": "tool", "tool_call_id": ct_item["tc"]["id"], "content": res})
                else:
                    for ct in batch["items"]:
                        if not ct["allowed"]:
                            raw, success, executed = await self._run_durable_tool(
                                request_id=request_id,
                                call_id=ct["tc"].get("id", f"tool-{ct['fn']}"),
                                name=ct["fn"],
                                inp=ct["inp"],
                                arguments=ct.get("arguments_raw"),
                                permission={"decision": ct.get("decision", "deny"), "reason": ct.get("reason", "")},
                            )
                            res = materialize_tool_result(raw, provider="openai")
                            self._openai_messages.append({"role": "tool", "tool_call_id": ct["tc"]["id"], "content": res})
                            continue
                        t0 = time.time()
                        raw, success, executed = await self._run_durable_tool(
                            request_id=request_id,
                            call_id=ct["tc"].get("id", f"tool-{ct['fn']}"),
                            name=ct["fn"],
                            inp=ct["inp"],
                            arguments=ct.get("arguments_raw"),
                            permission={"decision": "allow", "reason": ct.get("reason", "")},
                        )
                        tool_duration = int((time.time() - t0) * 1000)
                        res = materialize_tool_result(raw, provider="openai")
                        self._out_tool_result(
                            ct["fn"],
                            self._display_tool_result(raw, provider="openai"),
                            tool_call_id=ct["tc"].get("id"),
                        )
                        await self._emit("tool_end", {
                            "tool_name": ct["fn"],
                            "tool_input": ct["inp"],
                            "duration_ms": tool_duration,
                            "result_length": len(materialized_content_bytes(res)) if res else 0,
                            "success": success,
                        })
                        if self._context_cleared:
                            self._context_cleared = False
                            self._openai_messages.append({"role": "user", "content": res})
                            oai_context_break = True
                            break
                        self._openai_messages.append({"role": "tool", "tool_call_id": ct["tc"]["id"], "content": res})

            self._context_cleared = False
            await self._check_and_compact()

    async def _call_openai_stream(self) -> dict:
        """流式调用 OpenAI API，实时输出文本，收集 tool_calls 增量。
        返回与 OpenAI API 兼容的响应格式以便统一处理。"""
        async def _do():
            self._assert_provider_request_fits("openai", self._openai_messages)
            create_params = {
                "model": self.model,
                "tools": _to_openai_tools(self._effective_tool_definitions()),
                "messages": self._openai_messages,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            create_params.update(
                _thinking_request_params(
                    self.model,
                    self.thinking_effort,
                    use_openai=True,
                )
            )

            stream = await self._openai_client.chat.completions.create(**create_params)

            content = ""
            reasoning_content = ""
            first_text = True
            tool_calls: dict[int, dict] = {}
            finish_reason = ""
            usage = None

            async for chunk in stream:
                if chunk.usage:
                    usage = {
                        "prompt_tokens": chunk.usage.prompt_tokens,
                        "completion_tokens": chunk.usage.completion_tokens,
                    }

                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                # 捕获 reasoning_content（DeepSeek的思考内容）
                rc = None
                if hasattr(delta, "reasoning_content"):
                    raw_reasoning = getattr(delta, "reasoning_content")
                    # ``None`` is the SDK's absent-field marker.  Every other
                    # value, including falsey values such as 0/False/[],
                    # must cross the same strict provider boundary.
                    if raw_reasoning is not None:
                        rc = _normalize_provider_text(
                            raw_reasoning,
                            provider="openai",
                            block_kind="thinking",
                            block_index=0,
                        )

                if rc:
                    if not reasoning_content:
                        self._emit_text("\n")
                        await self._emit("first_token", {"is_thinking": True})
                    self._out_thinking(rc)
                    if self._runtime_recorder:
                        self._runtime_recorder.partial_text(rc, kind="thinking")
                    reasoning_content += rc

                if delta is not None and getattr(delta, "content", None) is not None:
                    text_delta = _normalize_provider_text(
                        getattr(delta, "content"),
                        provider="openai",
                        block_kind="text",
                        block_index=0,
                    )
                    if first_text:
                        self._out_stop_spinner()
                        self._emit_text("\n")
                        first_text = False
                        await self._emit("first_token", {"is_thinking": False})
                    self._emit_text(text_delta)
                    if self._runtime_recorder:
                        self._runtime_recorder.partial_text(text_delta)
                    content += text_delta

                if delta and delta.tool_calls:
                    for tc in delta.tool_calls:
                        existing = tool_calls.get(tc.index)
                        if existing:
                            if tc.function and tc.function.arguments:
                                existing["arguments"] += tc.function.arguments
                                if self._runtime_recorder:
                                    self._runtime_recorder.partial_tool_arguments(
                                        existing["id"] or "unknown-call",
                                        existing["name"] or "unknown",
                                        tc.function.arguments,
                                    )
                        else:
                            tool_calls[tc.index] = {
                                "id": tc.id or "",
                                "name": (tc.function.name if tc.function else "") or "",
                                "arguments": (tc.function.arguments if tc.function else "") or "",
                            }
                            if self._runtime_recorder and tc.function and tc.function.arguments:
                                self._runtime_recorder.partial_tool_arguments(
                                    tc.id or "unknown-call",
                                    (tc.function.name or "unknown"),
                                    tc.function.arguments,
                                )

                if chunk.choices[0].finish_reason:
                    finish_reason = chunk.choices[0].finish_reason

            assembled = None
            if tool_calls:
                assembled = [
                    {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                    for _, tc in sorted(tool_calls.items())
                ]

            message = {
                "role": "assistant",
                "content": content or None,
                "tool_calls": assembled,
            }
            # DeepSeek thinking 模式要求所有 assistant 消息都包含 reasoning_content
            # 即使为空也需要保存，以保持一致性
            model_lower = self.model.lower()
            is_deepseek_thinking = "deepseek" in model_lower and ("v4" in model_lower or "v3" in model_lower or "reasoner" in model_lower)
            
            if reasoning_content:
                message["reasoning_content"] = reasoning_content
            elif is_deepseek_thinking:
                # DeepSeek thinking 模式下，即使没有 reasoning_content 也需要设置空字符串
                message["reasoning_content"] = ""

            return {
                "choices": [{
                    "message": message,
                    "finish_reason": finish_reason or "stop",
                }],
                "usage": usage,
            }

        def _record_retry(attempt: int, error: Exception) -> None:
            if self._runtime_recorder:
                self._runtime_recorder.retry(attempt=attempt, reason=str(error))

        return await _with_retry(_do, on_retry=_record_retry)

    # ─── 共享方法 ────────────────────────────────────────────

    async def _confirm_dangerous(self, command: str) -> bool:
        """危险操作确认：经交互端口请求批准。

        顺序（design D1/D5）：
        1. 显式注入的 `confirm_fn`（既有兼容入口，仍可用）；
        2. 交互端口（默认 `DenyingInteractionPort`，headless 下保守拒绝）；
        3. runtime 内**不再**回退到终端 `input`（spec interactive-requests）。
        """

        self._out_confirmation(command)
        if self.confirm_fn:
            # 兼容入口：`confirm_fn` 仍可用，但**结果必须落定到交互注册表**，
            # 使"批准"与"端口/一次性授权"语义一致（GAP-C02-19：此前它会绕过
            # 注册表，导致取消/幂等/失效判定对该路径全部失效）。
            approved = await self.confirm_fn(command)
            request = InteractionRequest(
                request_id=f"approval-{self.session_id}-legacy-{uuid.uuid4().hex[:8]}",
                kind=InteractionKind.APPROVAL,
                session_id=str(self.session_id),
                run_id=str(self._runtime_run_id or self.session_id),
                params_digest=digest_params({"command": command}),
                prompt=command,
                tool_input={"command": command},
            )
            self.interaction_registry.open(request)
            self.interaction_registry.resolve(
                InteractionReply(
                    request_id=request.request_id,
                    approved=bool(approved),
                    params_digest=request.params_digest,
                    source="confirm_fn",
                )
            )
            return bool(approved)

        request_id = f"approval-{self.session_id}-{uuid.uuid4().hex[:8]}"
        application_digest = None
        if self._application_interaction_mode:
            from .application import params_digest as application_params_digest

            application_digest = application_params_digest(
                session_id=str(self.session_id),
                run_id=str(self._runtime_run_id or self.session_id),
                request_id=request_id,
                tool_call_id=None,
                tool_name=None,
                tool_input={"command": command},
                plan_id=None,
                plan_digest_value=None,
            )
        request = InteractionRequest(
            request_id=request_id,
            kind=InteractionKind.APPROVAL,
            session_id=str(self.session_id),
            run_id=str(self._runtime_run_id or self.session_id),
            params_digest=application_digest or digest_params({"command": command}),
            prompt=command,
            tool_input={"command": command},
        )
        self.interaction_registry.open(request)
        try:
            reply = await self.interaction_port.request(request)
        except asyncio.CancelledError:
            # 等待期间被取消：请求转 cancelled，且不授权执行。
            try:
                self.interaction_registry.cancel(request.request_id)
            except InteractionError:
                pass
            raise
        try:
            resolved = self.interaction_registry.resolve(reply)
        except InteractionError:
            # 过期/冲突/参数不匹配一律不授权（spec interactive-requests）。
            return False
        return bool(resolved.approved)

    def cancel_pending_interactions(self) -> list[str]:
        """取消所有等待中的人工交互（run 取消 / UI 断连入口）。

        两步：①把 pending 请求转 `cancelled`；②通知端口解除**实际等待**（若端口
        支持 `cancel_pending()`，例如可取消的终端读取），使等待方不会永远挂着。
        返回被取消的 request_id 列表。
        """

        cancelled = self.interaction_registry.cancel_all()
        notifier = getattr(self.interaction_port, "cancel_pending", None)
        if callable(notifier):
            notifier()
        return cancelled
