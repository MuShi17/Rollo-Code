"""P0 tests that cross the real local SDK and CLI consumer boundaries."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import threading
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

try:  # Anthropic >= 1.4 uses the httpx2 compatibility package.
    import httpx2 as anthropic_httpx
except ImportError:  # pragma: no cover - exercised by older supported SDKs.
    anthropic_httpx = httpx

from rollo.agent import Agent
from rollo.archive_capability import ToolResultArchiveCapability
from rollo.archive_projection import format_terminal_tool_result
from rollo.artifact_archive import ArtifactArchive
from rollo.runtime_event import canonical_json_bytes
from rollo.runtime_lifecycle import DurableToolBoundary
from rollo.runtime_store import SQLiteRuntimeStore
from rollo.project_context import ProjectContext
from rollo.tool_result import MAX_TOOL_RESULT_BYTES
from rollo.projections.provider_context import ProviderCapacityError
from rollo.interactions import DenyingInteractionPort
from rollo.runtime_ports import RecordingOutputPort, payload_is_safe


LARGE_CONTENT = "内容🙂\n" * 4_500
# The numbered read_file rendering is about 18 UTF-8 bytes per input line;
# keep this fixture over the common 16 MiB canonical-result cap.
CLI_LARGE_CONTENT = "内容🙂\n" * (MAX_TOOL_RESULT_BYTES // 18 + 100_000)


def _sse_event(event_type: str, payload: dict[str, Any]) -> bytes:
    return (
        f"event: {event_type}\n"
        f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
    ).encode("utf-8")


def _anthropic_stream_body(text: str = "ack") -> bytes:
    return b"".join(
        (
            _sse_event(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg-fixture",
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": "fixture-model",
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 11, "output_tokens": 0},
                    },
                },
            ),
            _sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            _sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                },
            ),
            _sse_event(
                "content_block_stop",
                {"type": "content_block_stop", "index": 0},
            ),
            _sse_event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 1},
                },
            ),
            _sse_event("message_stop", {"type": "message_stop"}),
        )
    )


def _anthropic_stream_body_with_tool(
    name: str, arguments: str, call_id: str = "call-agent-read"
) -> bytes:
    return b"".join(
        (
            _sse_event(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg-tool-fixture",
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": "fixture-model",
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 11, "output_tokens": 0},
                    },
                },
            ),
            _sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": call_id,
                        "name": name,
                        "input": {},
                    },
                },
            ),
            _sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": arguments},
                },
            ),
            _sse_event(
                "content_block_stop",
                {"type": "content_block_stop", "index": 0},
            ),
            _sse_event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                    "usage": {"output_tokens": 1},
                },
            ),
            _sse_event("message_stop", {"type": "message_stop"}),
        )
    )


def _anthropic_stream_body_with_thinking(
    thinking: str = "plan", text: str = "done"
) -> bytes:
    return b"".join(
        (
            _sse_event(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg-thinking-fixture",
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": "fixture-model",
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 11, "output_tokens": 0},
                    },
                },
            ),
            _sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "thinking", "thinking": ""},
                },
            ),
            _sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": thinking},
                },
            ),
            _sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "signature_delta", "signature": "fixture-signature"},
                },
            ),
            _sse_event("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            _sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "text_delta", "text": text},
                },
            ),
            _sse_event("content_block_stop", {"type": "content_block_stop", "index": 1}),
            _sse_event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 2},
                },
            ),
            _sse_event("message_stop", {"type": "message_stop"}),
        )
    )


def _openai_stream_body(text: str = "ack") -> bytes:
    chunks = [
        {
            "id": "chatcmpl-fixture",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "fixture-model",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": text},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-fixture",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "fixture-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
        {
            "id": "chatcmpl-fixture",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "fixture-model",
            "choices": [],
            "usage": {"prompt_tokens": 11, "completion_tokens": 1, "total_tokens": 12},
        },
    ]
    return b"".join(
        b"data: "
        + json.dumps(chunk, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + b"\n\n"
        for chunk in chunks
    ) + b"data: [DONE]\n\n"


def _openai_stream_body_with_reasoning(
    reasoning: str = "plan", text: str = "done"
) -> bytes:
    chunks = [
        {
            "id": "chatcmpl-reasoning-fixture",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "fixture-model",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "reasoning_content": reasoning},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-reasoning-fixture",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "fixture-model",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": text},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-reasoning-fixture",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "fixture-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
        {
            "id": "chatcmpl-reasoning-fixture",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "fixture-model",
            "choices": [],
            "usage": {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13},
        },
    ]
    return b"".join(
        b"data: "
        + json.dumps(chunk, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + b"\n\n"
        for chunk in chunks
    ) + b"data: [DONE]\n\n"


def _provider_tool_content(provider: str, messages: list[dict[str, Any]]) -> Any:
    if provider == "anthropic":
        return next(
            block["content"]
            for message in messages
            if message.get("role") == "user"
            for block in message.get("content", [])
            if isinstance(block, dict) and block.get("type") == "tool_result"
        )
    return next(message["content"] for message in messages if message.get("role") == "tool")


def _provider_context_bytes(provider: str, payload: dict[str, Any]) -> int:
    context = {
        "messages": payload["messages"],
        "tools": payload.get("tools", []),
    }
    if provider == "anthropic":
        context["system"] = payload.get("system")
    return len(canonical_json_bytes(context))


def _provider_client(
    provider: str,
    captured: list[dict[str, Any]],
    *,
    response_text: str = "ack",
    response_body: bytes | None = None,
    response_status: int = 200,
):
    content_type = "text/event-stream" if response_status == 200 else "application/json"

    if provider == "anthropic":
        def handler(request: Any):
            captured.append(json.loads(request.content))
            return anthropic_httpx.Response(
                response_status,
                headers={"content-type": content_type},
                content=response_body or _anthropic_stream_body(response_text),
            )

        return AsyncAnthropic(
            api_key="fixture-key",
            base_url="https://fixture.invalid",
            http_client=anthropic_httpx.AsyncClient(
                transport=anthropic_httpx.MockTransport(handler)
            ),
        )

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            response_status,
            headers={"content-type": content_type},
            content=response_body or _openai_stream_body(response_text),
        )

    return AsyncOpenAI(
        api_key="fixture-key",
        base_url="https://fixture.invalid/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _agent_chat_provider_client(
    provider: str,
    captured: list[dict[str, Any]],
    tool_arguments: str,
    tool_name: str = "read_file",
):
    if provider == "anthropic":
        def handler(request: Any):
            captured.append(json.loads(request.content))
            if len(captured) == 1:
                body = _anthropic_stream_body_with_tool(
                    tool_name, tool_arguments
                )
            else:
                body = _anthropic_stream_body("done")
            return anthropic_httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=body,
            )

        return AsyncAnthropic(
            api_key="fixture-key",
            base_url="https://fixture.invalid",
            http_client=anthropic_httpx.AsyncClient(
                transport=anthropic_httpx.MockTransport(handler)
            ),
        )

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        if len(captured) == 1:
            body = _openai_stream_body_with_tool(tool_name, tool_arguments)
        else:
            body = _openai_stream_body("done")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        )

    return AsyncOpenAI(
        api_key="fixture-key",
        base_url="https://fixture.invalid/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def _build_agent_with_large_result(tmp_path: Path, provider: str):
    store = SQLiteRuntimeStore(tmp_path / f"{provider}.sqlite")
    archive = ArtifactArchive(tmp_path / f"{provider}-artifacts", metadata_store=store)
    agent = Agent(
        api_base="https://fixture.invalid/v1" if provider == "openai" else None,
        api_key="fixture-key",
        model="fixture-model",
        thinking_effort="none",
        custom_system_prompt="fixture system",
        is_sub_agent=True,
        runtime_store=store,
        artifact_archive=archive,
        runtime_session_id="session-consumer",
        runtime_run_id=f"run-consumer-{provider}",
        runtime_context_id=f"context-consumer-{provider}",
    )
    agent._ask_count = 1
    agent._setup_runtime_facade()
    assert agent._runtime_context is not None
    assert agent._runtime_emitter is not None
    boundary = DurableToolBoundary(
        agent._runtime_emitter,
        agent._runtime_context,
        artifact_archive=archive,
        archive_capability=agent._archive_capability,
    )
    result = await boundary.execute(
        call_id="call-large-consumer",
        name="read_file",
        arguments={"file_path": "sample.txt"},
        executor=lambda: LARGE_CONTENT,
    )
    assert result.success is True
    return store, archive, agent, result


async def _build_agent_with_binary_result(tmp_path: Path, provider: str):
    store = SQLiteRuntimeStore(tmp_path / f"{provider}-binary.sqlite")
    archive = ArtifactArchive(
        tmp_path / f"{provider}-binary-artifacts", metadata_store=store
    )
    agent = Agent(
        api_base="https://fixture.invalid/v1" if provider == "openai" else None,
        api_key="fixture-key",
        model="fixture-model",
        thinking_effort="none",
        custom_system_prompt="fixture system",
        is_sub_agent=True,
        runtime_store=store,
        artifact_archive=archive,
        runtime_session_id="session-consumer",
        runtime_run_id=f"run-binary-consumer-{provider}",
        runtime_context_id=f"context-binary-consumer-{provider}",
    )
    agent._ask_count = 1
    agent._setup_runtime_facade()
    assert agent._runtime_context is not None
    assert agent._runtime_emitter is not None
    boundary = DurableToolBoundary(
        agent._runtime_emitter,
        agent._runtime_context,
        artifact_archive=archive,
        archive_capability=agent._archive_capability,
    )
    value = bytes(range(256)) * 80
    result = await boundary.execute(
        call_id="call-binary-consumer",
        name="read_file",
        arguments={"file_path": "sample.bin"},
        executor=lambda: value,
    )
    assert result.success is True
    return store, archive, agent, result


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_actual_agent_sdk_thinking_stream_uses_isolated_partial_kind(
    tmp_path: Path, provider: str
):
    async def scenario() -> None:
        store = SQLiteRuntimeStore(tmp_path / f"{provider}-thinking.sqlite")
        archive = ArtifactArchive(
            tmp_path / f"{provider}-thinking-artifacts", metadata_store=store
        )
        model = "claude-sonnet-4-6" if provider == "anthropic" else "fixture-model"
        agent = Agent(
            api_base="https://fixture.invalid/v1" if provider == "openai" else None,
            api_key="fixture-key",
            model=model,
            thinking_effort="low" if provider == "anthropic" else "none",
            custom_system_prompt="fixture system",
            is_sub_agent=True,
            runtime_store=store,
            artifact_archive=archive,
            runtime_session_id=f"session-thinking-{provider}",
            runtime_run_id=f"run-thinking-{provider}",
            runtime_context_id=f"context-thinking-{provider}",
        )
        agent._ask_count = 1
        agent._setup_runtime_facade()
        if provider == "anthropic":
            agent._anthropic_messages.append({"role": "user", "content": "hello"})
        else:
            agent._openai_messages.append({"role": "user", "content": "hello"})
        captured: list[dict[str, Any]] = []
        body = (
            _anthropic_stream_body_with_thinking()
            if provider == "anthropic"
            else _openai_stream_body_with_reasoning()
        )
        client = _provider_client(provider, captured, response_body=body)
        try:
            if provider == "anthropic":
                agent._anthropic_client = client
                messages = agent._anthropic_messages
            else:
                agent._openai_client = client
                messages = agent._openai_messages
            agent._start_runtime_model_call(
                f"request-thinking-{provider}", provider, {"messages": messages}
            )
            if provider == "anthropic":
                response = await agent._call_anthropic_stream()
                assert any(block.type == "thinking" for block in response.content)
            else:
                response = await agent._call_openai_stream()
                assert response["choices"][0]["message"]["reasoning_content"] == "plan"

            snapshots = store.read_runtime_stream_partials()
            assert {snapshot.stream_kind for snapshot in snapshots} == {"thinking", "text"}
            thinking_snapshot = next(
                snapshot for snapshot in snapshots if snapshot.stream_kind == "thinking"
            )
            assert thinking_snapshot.payload["content"]["text"] == "plan"
            assert all(not event.partial for event in store.read_events())

            assert agent._runtime_recorder is not None
            agent._runtime_recorder.final_thinking(
                "plan", signature="fixture-signature"
            )
            agent._runtime_recorder.final_text("done")
            agent._runtime_recorder.finish(
                "stop", usage={"input_tokens": 11, "output_tokens": 2}
            )
            final_events = store.read_events()
            assert store.read_runtime_stream_partials() == []
            assert not any(event.partial for event in final_events)
            assert any(
                event.content and event.content.get("kind") == "thinking"
                for event in final_events
            )
            assert any(
                event.content and event.content.get("kind") == "text"
                for event in final_events
            )
        finally:
            await agent.aclose()
            await client.close()
            store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_thinking_delta_emits_exactly_one_output_event(
    tmp_path: Path, provider: str
) -> None:
    """思考增量只允许发出一个输出事件。

    回归：流式分支曾对同一段思考文本既调 ``_out_thinking``（端口 →
    ``ui.print_assistant_text``）又调 ``_emit_text``（同样落到
    ``print_assistant_text``），而无缓冲的 ``sys.stdout.write`` 让控制台出现
    token 级重复（``LetLet me me``）。正文只有一条 ``_emit_text``，因此不受
    影响——这正是"只有 think 会重复"的原因。
    """

    async def scenario() -> None:
        store = SQLiteRuntimeStore(tmp_path / f"{provider}-thinking-once.sqlite")
        archive = ArtifactArchive(
            tmp_path / f"{provider}-thinking-once-artifacts", metadata_store=store
        )
        model = "claude-sonnet-4-6" if provider == "anthropic" else "fixture-model"
        agent = Agent(
            api_base="https://fixture.invalid/v1" if provider == "openai" else None,
            api_key="fixture-key",
            model=model,
            thinking_effort="low" if provider == "anthropic" else "none",
            custom_system_prompt="fixture system",
            is_sub_agent=True,
            runtime_store=store,
            artifact_archive=archive,
            runtime_session_id=f"session-once-{provider}",
            runtime_run_id=f"run-once-{provider}",
            runtime_context_id=f"context-once-{provider}",
        )
        # 端口在真实 CLI 由 __main__ 注入终端实现；此处记录事件即可，因为重复
        # 输出必然表现为同一段文本发出两个端口事件。
        port = RecordingOutputPort()
        agent.output_port = port
        agent._ask_count = 1
        agent._setup_runtime_facade()
        if provider == "anthropic":
            agent._anthropic_messages.append({"role": "user", "content": "hello"})
        else:
            agent._openai_messages.append({"role": "user", "content": "hello"})
        captured: list[dict[str, Any]] = []
        body = (
            _anthropic_stream_body_with_thinking()
            if provider == "anthropic"
            else _openai_stream_body_with_reasoning()
        )
        client = _provider_client(provider, captured, response_body=body)
        try:
            if provider == "anthropic":
                agent._anthropic_client = client
                await agent._call_anthropic_stream()
            else:
                agent._openai_client = client
                await agent._call_openai_stream()

            # 流中还有既有的 "\n" 分隔事件；只按内容文本过滤。
            thinking = [
                event.payload["text"]
                for event in port.events
                if event.kind == "assistant_thinking" and event.payload["text"].strip()
            ]
            text = [
                event.payload["text"]
                for event in port.events
                if event.kind == "assistant_text" and event.payload["text"].strip()
            ]

            # 夹具只发送一个思考增量与一个正文增量：两者都必须恰好出现一次。
            # 若思考同时走 `_out_thinking` 与 `_emit_text`，这里会得到 2 个条目。
            assert thinking == ["plan"]
            assert text == ["done"]
        finally:
            await agent.aclose()
            await client.close()
            store.close()

    asyncio.run(scenario())




@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_sub_agent_captured_output_carries_the_body_but_not_the_thinking(
    tmp_path: Path, provider: str
) -> None:
    """子 Agent 的捕获输出只收正文，思考既不重复也不混入返回值。

    这条钉住 ``_emit_text`` 与 ``_out_thinking`` 的分工，而不是它们的重复：

    * ``_out_thinking`` 是**端口**路径。子 Agent 继承父 Agent 的端口
      （``agent.py`` 构造子 Agent 时传 ``output_port=self.output_port``），
      而 ``TerminalOutputPort`` 把 ``assistant_thinking`` 与
      ``assistant_text`` 都交给 ``ui.print_assistant_text``——思考**已经**上屏。
      因此那里再补一条 ``_emit_text`` 就是重复（``LetLet me me``）。
    * ``_emit_text`` 是**缓冲**路径：``run_once`` 通过 ``_capture_output`` 设置
      ``_output_buffer``，正文靠它进入子 Agent 的返回值。

    两者是不同分支，与端口是否为 ``NullOutputPort`` 无关；这正是"删掉思考的
    ``_emit_text`` 不影响无端口场景"这一说法不成立的地方。
    """

    async def scenario() -> None:
        store = SQLiteRuntimeStore(tmp_path / f"{provider}-subagent-buffer.sqlite")
        archive = ArtifactArchive(
            tmp_path / f"{provider}-subagent-buffer-artifacts", metadata_store=store
        )
        model = "claude-sonnet-4-6" if provider == "anthropic" else "fixture-model"
        agent = Agent(
            api_base="https://fixture.invalid/v1" if provider == "openai" else None,
            api_key="fixture-key",
            model=model,
            thinking_effort="low" if provider == "anthropic" else "none",
            custom_system_prompt="fixture system",
            is_sub_agent=True,
            runtime_store=store,
            artifact_archive=archive,
            runtime_session_id=f"session-buffer-{provider}",
            runtime_run_id=f"run-buffer-{provider}",
            runtime_context_id=f"context-buffer-{provider}",
        )
        # ``run_once`` sets this; no output port is installed, so the port path
        # is a no-op and only the buffer can carry anything to the caller.
        agent._output_buffer = []
        agent._ask_count = 1
        agent._setup_runtime_facade()
        if provider == "anthropic":
            agent._anthropic_messages.append({"role": "user", "content": "hello"})
        else:
            agent._openai_messages.append({"role": "user", "content": "hello"})
        captured: list[dict[str, Any]] = []
        body = (
            _anthropic_stream_body_with_thinking()
            if provider == "anthropic"
            else _openai_stream_body_with_reasoning()
        )
        client = _provider_client(provider, captured, response_body=body)
        try:
            if provider == "anthropic":
                agent._anthropic_client = client
                await agent._call_anthropic_stream()
            else:
                agent._openai_client = client
                await agent._call_openai_stream()
            buffered = "".join(agent._output_buffer)
        finally:
            await agent.aclose()
            await client.close()
            store.close()

        assert "done" in buffered, buffered
        assert "plan" not in buffered, (
            "thinking must not be folded into the sub-agent's captured output: "
            f"{buffered!r}"
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_public_agent_chat_emits_provider_events_through_output_port(
    tmp_path: Path, provider: str
) -> None:
    """C02：本地 SDK 边界替身驱动完整 `Agent.chat()` 输出事件链。"""

    async def scenario() -> None:
        store = SQLiteRuntimeStore(tmp_path / f"{provider}-output.sqlite")
        port = RecordingOutputPort()
        model = "claude-sonnet-4-6" if provider == "anthropic" else "fixture-model"
        captured: list[dict[str, Any]] = []
        body = (
            _anthropic_stream_body_with_thinking(thinking="reasoning", text="answer")
            if provider == "anthropic"
            else _openai_stream_body_with_reasoning(reasoning="reasoning", text="answer")
        )
        client = _provider_client(provider, captured, response_body=body)
        agent = Agent(
            api_base="https://fixture.invalid/v1" if provider == "openai" else None,
            api_key="fixture-key",
            model=model,
            thinking_effort="low" if provider == "anthropic" else "none",
            custom_system_prompt="fixture system",
            runtime_store=store,
            output_port=port,
            provider_client=client,
        )
        agent._mcp_initialized = True
        try:
            await agent.chat("hello")

            kinds = set(port.kinds())
            assert {"assistant_thinking", "assistant_text", "budget", "lifecycle"} <= kinds
            assert any(
                event.payload.get("phase") == "turn_complete"
                for event in port.of_kind("lifecycle")
            )
            assert all(event.session_id == str(agent.session_id) for event in port.events)
            assert all(event.run_id for event in port.events)
            assert all(payload_is_safe(event.payload) for event in port.events)
            assert any(event.is_terminal for event in store.read_events())
        finally:
            await agent.aclose()
            await client.close()
            store.close()

        assert captured, "Agent.chat() 未到达本地 Provider SDK 边界"

    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_public_agent_chat_provider_error_seals_failed_canonical_run(
    tmp_path: Path, provider: str
) -> None:
    """普通 SDK Provider 错误同时可观察，并封存 failed/chat_error canonical 事实。"""

    async def scenario() -> None:
        store = SQLiteRuntimeStore(tmp_path / f"{provider}-error.sqlite")
        port = RecordingOutputPort()
        agent = Agent(
            api_base="https://fixture.invalid/v1" if provider == "openai" else None,
            api_key="fixture-key",
            model="fixture-model",
            thinking_effort="none",
            custom_system_prompt="fixture system",
            is_sub_agent=True,
            runtime_store=store,
            output_port=port,
        )
        agent._mcp_initialized = True
        captured: list[dict[str, Any]] = []
        body = json.dumps(
            {
                "type": "error",
                "error": {
                    "type": "server_error",
                    "message": "fixture provider failure",
                    "code": "fixture_error",
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        client = _provider_client(
            provider,
            captured,
            response_body=body,
            response_status=500,
        )
        try:
            if provider == "anthropic":
                agent._anthropic_client = client
            else:
                agent._openai_client = client
            with pytest.raises(Exception, match="fixture provider failure"):
                await agent.chat("hello")

            assert captured, "public Agent.chat() 未到达 SDK 错误边界"
            assert any(
                event.kind == "error"
                and "fixture provider failure" in event.payload["message"]
                for event in port.events
            )
            records = store.read_events()
            assert any(
                event.kind == "error"
                and event.metadata.get("lifecycle") == "provider_error"
                and event.status == "failed"
                for event in records
                if event.metadata
            ), [
                {
                    "kind": event.kind,
                    "status": event.status,
                    "lifecycle": (event.metadata or {}).get("lifecycle"),
                    "actions": dict(event.actions or {}),
                }
                for event in records
            ]
            assert any(
                event.is_terminal
                and event.status == "failed"
                and event.kind == "error"
                for event in records
            )
        finally:
            await agent.aclose()
            await client.close()
            store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_public_agent_chat_port_failure_preserves_sdk_messages_and_canonical_state(
    tmp_path: Path, provider: str
) -> None:
    """端口是观察面：双 Provider public chat 失败时不改 Provider 请求或 canonical 终态。"""

    class _BoomPort:
        name = "boom"

        def __init__(self) -> None:
            self.failures: list[str] = []

        def emit(self, event) -> None:
            self.failures.append(event.kind)
            raise RuntimeError(f"renderer failed for {event.kind}")

    def canonical_shape(store: SQLiteRuntimeStore) -> list[tuple]:
        return [
            (
                event.role,
                event.author,
                event.status,
                event.partial,
                event.content.get("kind") if event.content else None,
                event.content.get("text") if event.content else None,
                event.content.get("message") if event.content else None,
                event.metadata.get("lifecycle") if event.metadata else None,
            )
            for event in store.read_events()
        ]

    async def run(output_port, suffix: str) -> tuple[list[tuple], list[dict[str, Any]]]:
        store = SQLiteRuntimeStore(tmp_path / f"{provider}-{suffix}.sqlite")
        agent = Agent(
            api_base="https://fixture.invalid/v1" if provider == "openai" else None,
            api_key="fixture-key",
            model="fixture-model",
            thinking_effort="none",
            custom_system_prompt="fixture system",
            is_sub_agent=True,
            runtime_store=store,
            output_port=output_port,
            runtime_session_id=f"session-port-{provider}",
            runtime_run_id=f"run-port-{provider}",
        )
        agent._mcp_initialized = True
        captured: list[dict[str, Any]] = []
        body = (
            _anthropic_stream_body("provider text")
            if provider == "anthropic"
            else _openai_stream_body("provider text")
        )
        client = _provider_client(provider, captured, response_body=body)
        try:
            if provider == "anthropic":
                agent._anthropic_client = client
            else:
                agent._openai_client = client
            await agent.chat("hello")
            return canonical_shape(store), captured
        finally:
            await agent.aclose()
            await client.close()
            store.close()

    boom = _BoomPort()
    normal, normal_requests = asyncio.run(run(RecordingOutputPort(), "normal"))
    failed, failed_requests = asyncio.run(run(boom, "failed"))

    assert boom.failures
    assert normal == failed
    assert normal_requests == failed_requests


def test_public_openai_agent_chat_preserves_sequential_tool_call_ids(
    tmp_path: Path,
) -> None:
    """C02：OpenAI 顺序工具 dispatch 的输出与下一请求都保留各自 call id。"""

    call_ids = ["call-sequential-first", "call-sequential-second"]
    first_body = _openai_stream_body_with_tools(
        [
            (
                "write_file",
                json.dumps({"file_path": "first.txt", "content": "first"}),
                call_ids[0],
            ),
            (
                "write_file",
                json.dumps({"file_path": "second.txt", "content": "second"}),
                call_ids[1],
            ),
        ]
    )

    async def scenario() -> None:
        store = SQLiteRuntimeStore(tmp_path / "openai-sequential.sqlite")
        port = RecordingOutputPort()
        captured: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            body = first_body if len(captured) == 1 else _openai_stream_body("done")
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=body,
            )

        client = AsyncOpenAI(
            api_key="fixture-key",
            base_url="https://fixture.invalid/v1",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        agent = Agent(
            api_base="https://fixture.invalid/v1",
            api_key="fixture-key",
            model="fixture-model",
            thinking_effort="none",
            permission_mode="acceptEdits",
            custom_system_prompt="fixture system",
            project_context=ProjectContext.from_root(tmp_path),
            runtime_store=store,
            output_port=port,
            provider_client=client,
            is_sub_agent=True,
        )
        agent._mcp_initialized = True
        try:
            await agent.chat("write both files")
        finally:
            await agent.aclose()
            await client.close()
            store.close()

        assert (tmp_path / "first.txt").read_text(encoding="utf-8") == "first"
        assert (tmp_path / "second.txt").read_text(encoding="utf-8") == "second"
        assert [event.tool_call_id for event in port.of_kind("tool_call")] == call_ids
        assert [event.tool_call_id for event in port.of_kind("tool_result")] == call_ids
        assert len(captured) == 2

        assistant = next(
            message
            for message in captured[1]["messages"]
            if message.get("role") == "assistant"
        )
        assert [call["id"] for call in assistant["tool_calls"]] == call_ids
        tool_messages = [
            message for message in captured[1]["messages"] if message.get("role") == "tool"
        ]
        assert [message["tool_call_id"] for message in tool_messages] == call_ids

    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_public_agent_chat_preserves_tool_call_id_when_approval_is_denied(
    tmp_path: Path, provider: str
) -> None:
    """C02：完整 Provider 工具路径的 tool_call/tool_denied 使用真实调用 ID。"""

    async def scenario() -> None:
        store = SQLiteRuntimeStore(tmp_path / f"{provider}-deny.sqlite")
        port = RecordingOutputPort()
        target = tmp_path / f"{provider}-must-not-write.txt"
        agent = Agent(
            api_base="https://fixture.invalid/v1" if provider == "openai" else None,
            api_key="fixture-key",
            model="fixture-model",
            thinking_effort="none",
            custom_system_prompt="fixture system",
            is_sub_agent=True,
            runtime_store=store,
            output_port=port,
            interaction_port=DenyingInteractionPort(),
        )
        agent._mcp_initialized = True
        captured: list[dict[str, Any]] = []
        client = _agent_chat_provider_client(
            provider,
            captured,
            json.dumps({"file_path": str(target), "content": "should not write"}),
            tool_name="write_file",
        )
        try:
            if provider == "anthropic":
                agent._anthropic_client = client
            else:
                agent._openai_client = client
            await agent.chat("write the file")
        finally:
            await agent.aclose()
            await client.close()
            store.close()

        assert target.exists() is False
        expected_call_id = "call-agent-read" if provider == "anthropic" else "call-cli-read"
        for event in port.events:
            if event.kind in {"tool_call", "tool_denied"}:
                assert event.tool_call_id == expected_call_id
        assert {event.kind for event in port.events} >= {"tool_call", "tool_denied"}

    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
@pytest.mark.parametrize("capacity_rescue", [False, True])
def test_actual_agent_sdk_receives_full_or_capacity_rescue_projection(
    tmp_path: Path, provider: str, capacity_rescue: bool
):
    async def scenario() -> None:
        store, _archive, agent, _result = await _build_agent_with_large_result(
            tmp_path, provider
        )
        try:
            if capacity_rescue:
                # The budget must include the real tools envelope; 400 tokens
                # cannot even carry the active tool definitions.
                agent.effective_window = 2_000
            context = agent._refresh_provider_context_from_canonical()
            captured: list[dict[str, Any]] = []
            client = _provider_client(provider, captured)
            try:
                if provider == "anthropic":
                    agent._anthropic_client = client
                else:
                    agent._openai_client = client
                request_messages = (
                    agent._anthropic_messages
                    if provider == "anthropic"
                    else agent._openai_messages
                )
                agent._start_runtime_model_call(
                    "request-consumer", provider, {"messages": request_messages}
                )
                if provider == "anthropic":
                    response = await agent._call_anthropic_stream()
                    assert response.content[0].text == "ack"
                else:
                    response = await agent._call_openai_stream()
                    assert response["choices"][0]["message"]["content"] == "ack"
                assert not any(event.partial for event in store.read_events())
                assert store.read_runtime_stream_partials()
            finally:
                await client.close()

            assert len(captured) == 1
            assert context.request_fits is True
            assert context.request_size_bytes <= context.request_budget_bytes
            assert _provider_context_bytes(provider, captured[0]) == context.request_size_bytes
            assert _provider_context_bytes(provider, captured[0]) <= agent._provider_budget_bytes()
            wire_content = _provider_tool_content(provider, captured[0]["messages"])
            if capacity_rescue:
                rescue = json.loads(wire_content)
                assert rescue["kind"] == "bounded_ref"
                assert rescue["truncated"] is True
                assert "preview" not in rescue
                assert "ArchiveRead" in rescue["read_instructions"]
            else:
                assert wire_content == LARGE_CONTENT
                assert len(wire_content) > 16_000

            tool_definitions = captured[0].get("tools", [])
            names = [
                item.get("name") or item.get("function", {}).get("name")
                for item in tool_definitions
            ]
            assert "ArchiveRead" in names
        finally:
            await agent.aclose()
            store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_actual_agent_capacity_gate_blocks_sdk_dispatch(
    tmp_path: Path, provider: str
):
    async def scenario() -> None:
        store, _archive, agent, _result = await _build_agent_with_large_result(
            tmp_path, provider
        )
        captured: list[dict[str, Any]] = []
        client = _provider_client(provider, captured)
        try:
            agent.effective_window = 400
            if provider == "anthropic":
                agent._anthropic_client = client
            else:
                agent._openai_client = client
            with pytest.raises(ProviderCapacityError):
                if provider == "anthropic":
                    await agent._chat_anthropic("capacity check")
                else:
                    await agent._chat_openai("capacity check")
            assert captured == []

            # The SDK helper itself also owns a final guard, so a caller that
            # bypasses the replay refresh cannot dispatch an over-budget body.
            with pytest.raises(ProviderCapacityError):
                if provider == "anthropic":
                    await agent._call_anthropic_stream()
                else:
                    await agent._call_openai_stream()
            assert captured == []
        finally:
            await client.close()
            await agent.aclose()
            store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_final_capacity_gate_rejects_suffix_added_after_projection(
    tmp_path: Path, provider: str
):
    async def scenario() -> None:
        store, _archive, agent, _result = await _build_agent_with_binary_result(
            tmp_path, provider
        )
        captured: list[dict[str, Any]] = []
        client = _provider_client(provider, captured)
        try:
            agent.effective_window = 2_000
            context = agent._refresh_provider_context_from_canonical()
            assert context.request_fits is True
            messages = agent._anthropic_messages if provider == "anthropic" else agent._openai_messages
            messages.append({"role": "user", "content": "late suffix " * 5_000})
            if provider == "anthropic":
                agent._anthropic_client = client
            else:
                agent._openai_client = client

            with pytest.raises(ProviderCapacityError):
                if provider == "anthropic":
                    await agent._call_anthropic_stream()
                else:
                    await agent._call_openai_stream()
            assert captured == []
        finally:
            await client.close()
            await agent.aclose()
            store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_actual_agent_sdk_binary_rescue_preserves_byte_continuation(
    tmp_path: Path, provider: str
):
    async def scenario() -> None:
        store, _archive, agent, _result = await _build_agent_with_binary_result(
            tmp_path, provider
        )
        try:
            agent.effective_window = 2_000
            context = agent._refresh_provider_context_from_canonical()
            captured: list[dict[str, Any]] = []
            client = _provider_client(provider, captured)
            try:
                if provider == "anthropic":
                    agent._anthropic_client = client
                else:
                    agent._openai_client = client
                request_messages = (
                    agent._anthropic_messages
                    if provider == "anthropic"
                    else agent._openai_messages
                )
                agent._start_runtime_model_call(
                    "request-binary-consumer", provider, {"messages": request_messages}
                )
                if provider == "anthropic":
                    await agent._call_anthropic_stream()
                else:
                    await agent._call_openai_stream()
            finally:
                await client.close()

            assert len(captured) == 1
            assert context.request_fits is True
            assert context.request_size_bytes <= context.request_budget_bytes
            assert _provider_context_bytes(provider, captured[0]) == context.request_size_bytes
            assert _provider_context_bytes(provider, captured[0]) <= agent._provider_budget_bytes()
            wire_content = _provider_tool_content(provider, captured[0]["messages"])
            rescue = json.loads(wire_content)
            assert rescue["kind"] == "bounded_ref"
            assert rescue["truncated"] is True
            assert "preview" not in rescue
            assert "ArchiveRead" in rescue["read_instructions"]
            page = json.loads(
                await agent._execute_tool_call(
                    "ArchiveRead",
                    {
                        "operation": "read",
                        "ref": rescue["ref"],
                        "offset": 0,
                        "limit": 32,
                    },
                )
            )
            assert page["kind"] == "archive_page"
            assert page["unit"] == "bytes"
            assert page["page"].startswith("base64:")
            assert page["next_offset"] == len(base64.b64decode(page["page"][7:]))
        finally:
            await agent.aclose()
            store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_actual_agent_sdk_receives_stale_and_archive_page_without_terminal_substitution(
    tmp_path: Path, provider: str
):
    async def scenario() -> None:
        store, _archive, agent, result = await _build_agent_with_large_result(
            tmp_path, provider
        )
        client = None
        try:
            assert agent._runtime_emitter is not None
            assert agent._runtime_context is not None
            agent._ask_count = 2
            agent._setup_runtime_facade()
            agent._emit_canonical_user_event("middle step")
            agent._ask_count = 3
            agent._setup_runtime_facade()
            agent._emit_canonical_user_event("next step")
            context = agent._refresh_provider_context_from_canonical()
            stale_content = _provider_tool_content(
                provider,
                agent._anthropic_messages
                if provider == "anthropic"
                else agent._openai_messages,
            )
            stale = json.loads(stale_content)
            assert stale["kind"] == "bounded_ref"
            assert "preview" not in stale
            assert "ArchiveRead" in stale["read_instructions"]
            ref = stale["ref"]

            boundary = DurableToolBoundary(
                agent._runtime_emitter,
                agent._runtime_context,
                artifact_archive=agent._artifact_archive,
                archive_capability=agent._archive_capability,
            )
            page = await boundary.execute(
                call_id="call-archive-page",
                name="ArchiveRead",
                arguments={
                    "operation": "read",
                    "ref": ref,
                    "offset": 0,
                    "limit": 32,
                },
                executor=lambda: agent._execute_tool_call(
                    "ArchiveRead",
                    {
                        "operation": "read",
                        "ref": ref,
                        "offset": 0,
                        "limit": 32,
                    },
                ),
            )
            assert page.success is True
            context = agent._refresh_provider_context_from_canonical()
            captured: list[dict[str, Any]] = []
            client = _provider_client(provider, captured)
            if provider == "anthropic":
                agent._anthropic_client = client
            else:
                agent._openai_client = client
            request_messages = (
                agent._anthropic_messages
                if provider == "anthropic"
                else agent._openai_messages
            )
            agent._start_runtime_model_call(
                "request-page-consumer", provider, {"messages": request_messages}
            )
            if provider == "anthropic":
                await agent._call_anthropic_stream()
            else:
                await agent._call_openai_stream()
            assert len(captured) == 1
            assert context.request_fits is True
            assert context.request_size_bytes <= context.request_budget_bytes
            assert _provider_context_bytes(provider, captured[0]) == context.request_size_bytes
            assert _provider_context_bytes(provider, captured[0]) <= agent._provider_budget_bytes()
            wire = json.dumps(captured[0]["messages"], ensure_ascii=False)
            assert "内容🙂" in wire
            assert '"tool_use"' in wire if provider == "anthropic" else '"tool_calls"' in wire
            assert "archive_page" in wire
            assert "terminal" not in wire
        finally:
            if client is not None:
                await client.close()
            await agent.aclose()
            store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_actual_agent_sdk_without_archive_capability_fails_closed(
    tmp_path: Path, provider: str
):
    async def scenario() -> None:
        store, _archive, agent, _result = await _build_agent_with_large_result(
            tmp_path, provider
        )
        try:
            agent._archive_capability = None
            agent.effective_window = 2_000
            captured: list[dict[str, Any]] = []
            client = _provider_client(provider, captured)
            try:
                if provider == "anthropic":
                    agent._anthropic_client = client
                else:
                    agent._openai_client = client
                with pytest.raises(ProviderCapacityError) as error:
                    agent._refresh_provider_context_from_canonical()
            finally:
                await client.close()

            assert captured == []
            assert error.value.request_size_bytes > error.value.request_budget_bytes
            assert "provider_capacity_exhausted" in {
                item["code"] for item in error.value.to_dict()["diagnostics"]
            }
            assert "archive_write_failed" in {
                item["code"] for item in error.value.to_dict()["diagnostics"]
            }
        finally:
            await agent.aclose()
            store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_public_agent_tool_loop_reaches_second_provider_request(
    tmp_path: Path, provider: str
):
    async def scenario() -> None:
        database = tmp_path / f"{provider}-loop.sqlite"
        store = SQLiteRuntimeStore(database)
        archive = ArtifactArchive(
            tmp_path / f"{provider}-loop-artifacts", metadata_store=store
        )
        source = tmp_path / f"{provider}-loop.txt"
        source.write_text(LARGE_CONTENT, encoding="utf-8")
        agent = Agent(
            api_base="https://fixture.invalid/v1" if provider == "openai" else None,
            api_key="fixture-key",
            model="fixture-model",
            thinking_effort="none",
            custom_system_prompt="fixture system",
            is_sub_agent=True,
            runtime_store=store,
            artifact_archive=archive,
        )
        agent.effective_window = 2_000
        captured: list[dict[str, Any]] = []
        client = _agent_chat_provider_client(
            provider,
            captured,
            json.dumps({"file_path": str(source)}, ensure_ascii=False),
        )
        try:
            if provider == "anthropic":
                agent._anthropic_client = client
            else:
                agent._openai_client = client
            await agent.chat(f"read {source}")
            assert not any(event.partial for event in store.read_events())
            assert store.read_runtime_stream_partials() == []
        finally:
            await agent.aclose()
            await client.close()
            store.close()

        assert len(captured) == 2
        second_request = captured[1]
        assert _provider_context_bytes(provider, second_request) <= agent._provider_budget_bytes()
        wire_content = _provider_tool_content(provider, second_request["messages"])
        rescue = json.loads(wire_content)
        assert rescue["kind"] == "bounded_ref"
        assert rescue["truncated"] is True
        assert "preview" not in rescue
        assert "ArchiveRead" in rescue["read_instructions"]
        assert "terminal" not in json.dumps(second_request, ensure_ascii=False)
        tool_definitions = second_request.get("tools", [])
        names = [
            item.get("name") or item.get("function", {}).get("name")
            for item in tool_definitions
        ]
        assert "ArchiveRead" in names

    asyncio.run(scenario())


def test_real_openai_consumer_semantically_prunes_duplicate_read_results(
    tmp_path: Path,
):
    _LoopbackSemanticPruningHandler.requests = []
    source = tmp_path / "semantic.txt"
    source.write_text("semantic line🙂\n" * 220, encoding="utf-8")
    _LoopbackSemanticPruningHandler.source_path = str(source)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), _LoopbackSemanticPruningHandler
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        cli_home = tmp_path / "cli-home"
        cli_home.mkdir()
        env = os.environ.copy()
        env.update(
            {
                "PYTHONPATH": str(Path(__file__).parents[2]),
                "HOME": str(cli_home),
                "USERPROFILE": str(cli_home),
                "ROLLO_RUNTIME_DIR": str(cli_home / ".rollo"),
                "OPENAI_API_KEY": "fixture-key",
                "OPENAI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
                "ROLLO_THINKING_EFFORT": "none",
                "PYTHON_DOTENV_DISABLED": "1",
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "rollo",
                "--no-thinking",
                "--model",
                "fixture-model",
                "read",
                str(source),
            ],
            cwd=Path(__file__).parents[2],
            env=env,
            capture_output=True,
            text=False,
            timeout=60,
        )
        stdout = completed.stdout.decode("utf-8", errors="replace")
        stderr = completed.stderr.decode("utf-8", errors="replace")
        assert completed.returncode == 0, stderr + stdout
        assert len(_LoopbackSemanticPruningHandler.requests) == 3

        second_tools = [
            message
            for message in _LoopbackSemanticPruningHandler.requests[1]["messages"]
            if message.get("role") == "tool"
        ]
        assert len(second_tools) == 1
        assert "semantic line🙂" in second_tools[0]["content"]

        third_tools = [
            message
            for message in _LoopbackSemanticPruningHandler.requests[2]["messages"]
            if message.get("role") == "tool"
        ]
        assert len(third_tools) == 2
        first_result = json.loads(third_tools[0]["content"])
        assert first_result["kind"] == "bounded_ref"
        assert "ArchiveRead" in first_result["read_instructions"]
        assert isinstance(third_tools[1]["content"], str)
        assert "semantic line🙂" in third_tools[1]["content"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_terminal_formatter_is_content_first_bounded_and_path_free(tmp_path: Path):
    archive = ArtifactArchive(tmp_path / "artifacts")
    ref = archive.archive(
        "终端正文🙂\n" * 2_000,
        mime_type="text/plain",
        encoding="utf-8",
        scope="tool-result",
    )
    capability = ToolResultArchiveCapability(archive, session_id="session-consumer")
    value = ref.placeholder()
    text = format_terminal_tool_result(value, capability, max_chars=500)
    assert text is not None
    assert len(text) <= 500
    assert text.startswith("终端正文🙂")
    assert "ArchiveRead" in text
    assert "\\\"kind\\\"" not in text
    assert str(tmp_path) not in text

    page = {
        "kind": "archive_page",
        "page": "页正文🙂" * 200,
        "offset": 0,
        "next_offset": 32,
        "total_units": 400,
        "unit": "chars",
        "has_more": True,
    }
    page_text = format_terminal_tool_result(page, capability, max_chars=500)
    assert page_text is not None
    assert len(page_text) <= 500
    assert page_text.startswith("页正文🙂")
    assert "next_offset=32" in page_text

    error_text = format_terminal_tool_result(
        {
            "kind": "archive_read_error",
            "error_type": "archive_store_closed",
            "message": "archive store is closed",
        },
        capability,
    )
    assert error_text == "Error [archive_store_closed]: archive store is closed"

    closed_store = SQLiteRuntimeStore(tmp_path / "closed.sqlite")
    closed_archive = ArtifactArchive(
        tmp_path / "closed-artifacts", metadata_store=closed_store
    )
    closed_ref = closed_archive.archive(
        "closed content",
        mime_type="text/plain",
        encoding="utf-8",
        scope="tool-result",
    )
    closed_capability = ToolResultArchiveCapability(
        closed_archive, session_id="session-consumer"
    )
    closed_store.close()
    closed_text = format_terminal_tool_result(
        closed_ref.placeholder(), closed_capability
    )
    assert closed_text == "Error [archive_store_closed]: archive store is closed"


def test_archive_and_canonical_bytes_are_unchanged_by_all_local_projections(
    tmp_path: Path,
):
    async def scenario() -> None:
        store, archive, agent, result = await _build_agent_with_large_result(
            tmp_path, "openai"
        )
        try:
            assert isinstance(result.result, str)
            events_before = tuple(
                (ordinal, canonical_json_bytes(event.to_dict()))
                for ordinal, event in store.read_event_records()
            )
            agent._refresh_provider_context_from_canonical()
            provider_messages_before = deepcopy(agent._openai_messages)
            assert format_terminal_tool_result(result.result, agent._archive_capability) is None
            agent.effective_window = 2_000
            context = agent._refresh_provider_context_from_canonical()
            rescue = _provider_tool_content("openai", agent._openai_messages)
            rescue_value = json.loads(rescue)
            assert rescue_value["kind"] == "bounded_ref"
            ref = rescue_value["ref"]
            assert agent._archive_capability is not None
            artifact_before = archive.read(ref, max_bytes=64 * 1024)
            metadata_before = canonical_json_bytes(archive.metadata(ref))
            sha256_before = archive.inspect(ref).sha256
            ref_before = ref
            assert format_terminal_tool_result(
                rescue_value, agent._archive_capability
            ) is not None
            page = json.loads(
                agent._archive_capability.execute(
                    {"operation": "read", "ref": ref, "offset": 0, "limit": 16}
                )
            )
            assert page["kind"] == "archive_page"

            assert ref == ref_before
            assert archive.read(ref, max_bytes=64 * 1024) == artifact_before
            assert canonical_json_bytes(archive.metadata(ref)) == metadata_before
            assert tuple(
                (ordinal, canonical_json_bytes(event.to_dict()))
                for ordinal, event in store.read_event_records()
            ) == events_before
            assert provider_messages_before != agent._openai_messages
            assert archive.inspect(ref).sha256 == sha256_before
        finally:
            await agent.aclose()
            store.close()

    asyncio.run(scenario())


class _LoopbackProtocolHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    lock = threading.Lock()

    def do_POST(self) -> None:  # noqa: N802 - stdlib protocol hook.
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        payload = json.loads(body)
        with self.lock:
            self.requests.append(payload)
            request_number = len(self.requests)
        if request_number == 1:
            prompt = str(payload.get("messages", [{}])[-1].get("content", ""))
            file_path = prompt[5:].strip() if prompt.lower().startswith("read ") else prompt
            tool_args = json.dumps(
                {"file_path": file_path},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            response = _openai_stream_body_with_tool("read_file", tool_args)
        else:
            response = _openai_stream_body("done")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(response)
        self.wfile.flush()

    def log_message(self, format: str, *args: Any) -> None:
        del format, args


class _LoopbackSemanticPruningHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    lock = threading.Lock()
    source_path = ""

    def do_POST(self) -> None:  # noqa: N802 - stdlib protocol hook.
        length = int(self.headers.get("content-length", "0"))
        payload = json.loads(self.rfile.read(length))
        with self.lock:
            self.requests.append(payload)
            request_number = len(self.requests)

        if request_number in {1, 2}:
            arguments = json.dumps(
                {"file_path": self.source_path},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            call_id = (
                "call-semantic-first"
                if request_number == 1
                else "call-semantic-second"
            )
            response = _openai_stream_body_with_tool(
                "read_file", arguments, call_id=call_id
            )
        else:
            response = _openai_stream_body("done")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(response)
        self.wfile.flush()

    def log_message(self, format: str, *args: Any) -> None:
        del format, args


class _LoopbackArchiveFollowupHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    lock = threading.Lock()
    source_path = ""
    mode = "page"

    def do_POST(self) -> None:  # noqa: N802 - stdlib protocol hook.
        length = int(self.headers.get("content-length", "0"))
        payload = json.loads(self.rfile.read(length))
        with self.lock:
            self.requests.append(payload)
            request_number = len(self.requests)

        if request_number == 1:
            arguments = json.dumps(
                {"file_path": self.source_path},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            response = _openai_stream_body_with_tool(
                "read_file", arguments, call_id="call-cli-read"
            )
        elif request_number == 2:
            limit = 6_000 if self.mode == "page" else 0
            arguments = json.dumps(
                {
                    "file_path": self.source_path,
                    "offset": 0,
                    "limit": limit,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            response = _openai_stream_body_with_tool(
                "read_file", arguments, call_id="call-cli-page"
            )
        else:
            response = _openai_stream_body("done")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(response)
        self.wfile.flush()

    def log_message(self, format: str, *args: Any) -> None:
        del format, args


def _openai_stream_body_with_tool(
    name: str, arguments: str, call_id: str = "call-cli-read"
) -> bytes:
    chunks = [
        {
            "id": "chatcmpl-tool",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "fixture-model",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
                                "type": "function",
                                "function": {"name": name, "arguments": arguments},
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-tool",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "fixture-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        },
        {
            "id": "chatcmpl-tool",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "fixture-model",
            "choices": [],
            "usage": {"prompt_tokens": 11, "completion_tokens": 1, "total_tokens": 12},
        },
    ]
    return b"".join(
        b"data: "
        + json.dumps(chunk, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + b"\n\n"
        for chunk in chunks
    ) + b"data: [DONE]\n\n"


def _openai_stream_body_with_tools(
    tool_calls: list[tuple[str, str, str]],
) -> bytes:
    """构造同一 Provider 响应中的多个工具调用，覆盖顺序 dispatch。"""

    delta_tool_calls = [
        {
            "index": index,
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        }
        for index, (name, arguments, call_id) in enumerate(tool_calls)
    ]
    chunks = [
        {
            "id": "chatcmpl-sequential-tools",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "fixture-model",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "tool_calls": delta_tool_calls},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-sequential-tools",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "fixture-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        },
        {
            "id": "chatcmpl-sequential-tools",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "fixture-model",
            "choices": [],
            "usage": {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13},
        },
    ]
    return b"".join(
        b"data: "
        + json.dumps(chunk, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + b"\n\n"
        for chunk in chunks
    ) + b"data: [DONE]\n\n"


def test_real_cli_subprocess_uses_loopback_protocol_and_resume(tmp_path: Path):
    _LoopbackProtocolHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LoopbackProtocolHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        source = tmp_path / "large.txt"
        source.write_text(LARGE_CONTENT, encoding="utf-8")
        cli_home = tmp_path / "cli-home"
        cli_home.mkdir()
        env = os.environ.copy()
        env.update(
            {
                "PYTHONPATH": str(Path(__file__).parents[2]),
                "HOME": str(cli_home),
                "USERPROFILE": str(cli_home),
                "ROLLO_RUNTIME_DIR": str(cli_home / ".rollo"),
                "OPENAI_API_KEY": "fixture-key",
                "OPENAI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
                "ROLLO_THINKING_EFFORT": "none",
                "PYTHON_DOTENV_DISABLED": "1",
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        first = subprocess.run(
            [
                sys.executable,
                "-m",
                "rollo",
                "--no-thinking",
                "--model",
                "fixture-model",
                "read",
                str(source),
            ],
            cwd=Path(__file__).parents[2],
            env=env,
            capture_output=True,
            text=False,
            timeout=60,
        )
        first_stdout = first.stdout.decode("utf-8", errors="replace")
        first_stderr = first.stderr.decode("utf-8", errors="replace")
        assert first.returncode == 0, first_stderr + first_stdout
        assert "内容🙂" in first_stdout
        assert len(_LoopbackProtocolHandler.requests) == 2
        assert any(cli_home.joinpath(".rollo").rglob("session.v2.json"))

        second = subprocess.run(
            [
                sys.executable,
                "-m",
                "rollo",
                "--resume",
                "--no-thinking",
                "--model",
                "fixture-model",
                "continue",
            ],
            cwd=Path(__file__).parents[2],
            env=env,
            capture_output=True,
            text=False,
            timeout=60,
        )
        second_stdout = second.stdout.decode("utf-8", errors="replace")
        second_stderr = second.stderr.decode("utf-8", errors="replace")
        assert second.returncode == 0, second_stderr + second_stdout
        assert "done" in second_stdout
        assert len(_LoopbackProtocolHandler.requests) == 3
        assert "bounded_ref" not in first_stdout
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("mode", ["page", "error"])
def test_real_cli_subprocess_displays_archive_page_and_error(
    tmp_path: Path, mode: str
):
    _LoopbackArchiveFollowupHandler.requests = []
    source = tmp_path / "large.txt"
    source.write_text(CLI_LARGE_CONTENT, encoding="utf-8")
    _LoopbackArchiveFollowupHandler.source_path = str(source)
    _LoopbackArchiveFollowupHandler.mode = mode
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), _LoopbackArchiveFollowupHandler
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        cli_home = tmp_path / "cli-home"
        cli_home.mkdir()
        env = os.environ.copy()
        env.update(
            {
                "PYTHONPATH": str(Path(__file__).parents[2]),
                "HOME": str(cli_home),
                "USERPROFILE": str(cli_home),
                "ROLLO_RUNTIME_DIR": str(cli_home / ".rollo"),
                "OPENAI_API_KEY": "fixture-key",
                "OPENAI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
                "ROLLO_THINKING_EFFORT": "none",
                "PYTHON_DOTENV_DISABLED": "1",
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "rollo",
                "--no-thinking",
                "--model",
                "fixture-model",
                "read",
                str(source),
            ],
            cwd=Path(__file__).parents[2],
            env=env,
            capture_output=True,
            text=False,
            timeout=60,
        )
        stdout = completed.stdout.decode("utf-8", errors="replace")
        stderr = completed.stderr.decode("utf-8", errors="replace")
        assert completed.returncode == 0, stderr + stdout
        assert len(_LoopbackArchiveFollowupHandler.requests) == 3
        if mode == "page":
            assert "内容🙂" in stdout
            assert "result_too_large" in stdout
        else:
            assert "read_file limit" in stdout
        assert "Traceback" not in stdout
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
