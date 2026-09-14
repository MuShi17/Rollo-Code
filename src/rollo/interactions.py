"""runtime 人工交互端口：审批（approval）与提问（question）。

把"等待人类输入"从终端读取中解耦：runtime 只创建**带身份绑定的请求**并 await
可取消的等待对象；具体怎么问、在哪问由端口实现决定。

不变量（对应 spec ``interactive-requests``）：
- 请求身份不可变：`request_id` + session/run + `tool_call_id` + 参数摘要；
- 状态单向：``pending → resolved | expired | cancelled``，终态唯一；
- 相同回复幂等，冲突回复被拒绝；
- 提问的回答**不是**工具授权；
- runtime 内不保留终端 input fallback。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

__all__ = [
    "InteractionKind",
    "InteractionState",
    "InteractionRequest",
    "InteractionReply",
    "InteractionError",
    "UnknownRequestError",
    "RequestExpiredError",
    "RequestNotFoundOrClosedError",
    "ReplyConflictError",
    "InteractionPort",
    "DenyingInteractionPort",
    "RecordingInteractionPort",
    "InteractionRegistry",
    "digest_params",
]


class InteractionKind:
    APPROVAL = "approval"
    QUESTION = "question"


class InteractionState:
    PENDING = "pending"
    RESOLVED = "resolved"
    EXPIRED = "expired"
    CANCELLED = "cancelled"

    TERMINAL = (RESOLVED, EXPIRED, CANCELLED)


class InteractionError(RuntimeError):
    """交互请求错误基类。"""


class UnknownRequestError(InteractionError):
    """请求不存在。"""


class RequestExpiredError(InteractionError):
    """请求已过期。"""


class RequestNotFoundOrClosedError(InteractionError):
    """请求不存在或已处于终态（无法再回复）。"""


class ReplyConflictError(InteractionError):
    """回复与首次结果冲突或参数摘要不匹配。"""


def digest_params(params: Mapping[str, Any] | str | None) -> str:
    """计算参数摘要：用于绑定"这次审批到底批的是什么"。"""

    if params is None:
        payload = ""
    elif isinstance(params, str):
        payload = params
    else:
        payload = json.dumps(params, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class InteractionRequest:
    """一个待处理的人工交互请求（身份不可变）。"""

    request_id: str
    kind: str
    session_id: str
    run_id: str
    params_digest: str
    prompt: str = ""
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_input: Any = None
    plan_id: str | None = None
    plan_digest: str | None = None
    #: 应答该请求的宿主命令 id（C03 控制面用于 interaction.respond 幂等）。
    command_id: str | None = None
    expires_at: float | str | None = None
    expires_at_utc: str | None = None
    metadata: Mapping[str, Any] | None = None
    created_at: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("InteractionRequest 需要 request_id")
        if self.kind not in (InteractionKind.APPROVAL, InteractionKind.QUESTION):
            raise ValueError(f"未知的交互类型：{self.kind}")
        if not self.session_id or not self.run_id:
            raise ValueError("InteractionRequest 需要 session_id 与 run_id")

    def is_expired(self, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        if isinstance(self.expires_at, str):
            from datetime import datetime

            text = self.expires_at[:-1] + "+00:00" if self.expires_at.endswith("Z") else self.expires_at
            return datetime.now().astimezone() >= datetime.fromisoformat(text)
        return (now if now is not None else time.monotonic()) >= self.expires_at


@dataclass(frozen=True)
class InteractionReply:
    """一次回复：绑定被回复的请求与它看到的参数摘要。

    身份字段保持可选以兼容已有端口；端口/宿主一旦提供这些字段，注册表会逐项
    校验它们不能指向另一个 session、run 或 tool call。
    """

    request_id: str
    approved: bool = False
    answer: str | None = None
    params_digest: str | None = None
    source: str = "unknown"
    session_id: str | None = None
    run_id: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_input: Any = None
    plan_id: str | None = None
    plan_digest: str | None = None
    metadata: Mapping[str, Any] | None = None


@runtime_checkable
class InteractionPort(Protocol):
    """交互端口：决定"如何取得人的回复"。"""

    async def request(self, request: InteractionRequest) -> InteractionReply:  # pragma: no cover
        ...


class DenyingInteractionPort:
    """安全默认实现：不读终端，保守拒绝需要人工确认的操作。

    用于无终端调用方（headless、未来的 host/GUI 未接入时）。
    """

    name = "denying"

    def __init__(self, reason: str = "no interactive port injected") -> None:
        self.reason = reason
        self.requests: list[InteractionRequest] = []

    async def request(self, request: InteractionRequest) -> InteractionReply:
        self.requests.append(request)
        return InteractionReply(
            request_id=request.request_id,
            approved=False,
            params_digest=request.params_digest,
            source=self.name,
            session_id=request.session_id,
            run_id=request.run_id,
            tool_call_id=request.tool_call_id,
            tool_name=request.tool_name,
            tool_input=request.tool_input,
            plan_id=request.plan_id,
            plan_digest=request.plan_digest,
            metadata=request.metadata,
        )


class RecordingInteractionPort:
    """测试用：记录请求并按预设答案回复（``hold=True`` 时保持挂起以模拟等待）。"""

    name = "recording"

    def __init__(self, replies: list[InteractionReply] | None = None) -> None:
        self.requests: list[InteractionRequest] = []
        self._replies = list(replies or [])
        self.hold = False

    async def request(self, request: InteractionRequest) -> InteractionReply:
        self.requests.append(request)
        if self.hold:
            await asyncio.Event().wait()  # 保持挂起，直到被取消
        if self._replies:
            reply = self._replies.pop(0)
            return InteractionReply(
                request_id=request.request_id,
                approved=reply.approved,
                answer=reply.answer,
                params_digest=reply.params_digest or request.params_digest,
                source=self.name,
                session_id=(
                    request.session_id if reply.session_id is None else reply.session_id
                ),
                run_id=request.run_id if reply.run_id is None else reply.run_id,
                tool_call_id=(
                    request.tool_call_id
                    if reply.tool_call_id is None
                    else reply.tool_call_id
                ),
                tool_name=request.tool_name if reply.tool_name is None else reply.tool_name,
                tool_input=getattr(request, "tool_input", None) if getattr(reply, "tool_input", None) is None else reply.tool_input,
                plan_id=getattr(request, "plan_id", None) if getattr(reply, "plan_id", None) is None else reply.plan_id,
                plan_digest=getattr(request, "plan_digest", None) if getattr(reply, "plan_digest", None) is None else reply.plan_digest,
                metadata=getattr(request, "metadata", None) if getattr(reply, "metadata", None) is None else reply.metadata,
            )
        return InteractionReply(
            request_id=request.request_id,
            approved=False,
            params_digest=request.params_digest,
            source=self.name,
            session_id=request.session_id,
            run_id=request.run_id,
            tool_call_id=request.tool_call_id,
            tool_name=request.tool_name,
            tool_input=request.tool_input,
            plan_id=request.plan_id,
            plan_digest=request.plan_digest,
            metadata=request.metadata,
        )


class InteractionRegistry:
    """请求登记与状态机：单一控制序列化边界（同步方法，无 await 竞争窗口）。

    所有状态转移都在同步方法内完成，因此 respond/cancel/expire 之间不可能出现
    交错窗口；幂等与冲突判定同样在此集中处理。
    """

    def __init__(self) -> None:
        self._requests: dict[str, InteractionRequest] = {}
        self._states: dict[str, str] = {}
        self._replies: dict[str, InteractionReply] = {}

    # ─── 登记与查询 ────────────────────────────────────────

    def open(self, request: InteractionRequest) -> InteractionRequest:
        if request.request_id in self._requests:
            raise InteractionError(f"request_id 重复：{request.request_id}")
        self._requests[request.request_id] = request
        self._states[request.request_id] = InteractionState.PENDING
        return request

    def state(self, request_id: str) -> str:
        if request_id not in self._states:
            raise UnknownRequestError(request_id)
        return self._states[request_id]

    def get(self, request_id: str) -> InteractionRequest:
        if request_id not in self._requests:
            raise UnknownRequestError(request_id)
        return self._requests[request_id]

    def pending(self) -> list[InteractionRequest]:
        return [
            req for rid, req in self._requests.items()
            if self._states.get(rid) == InteractionState.PENDING
        ]

    def reply_for(self, request_id: str) -> InteractionReply | None:
        return self._replies.get(request_id)

    # ─── 状态转移（单向，终态唯一）────────────────────────

    def _close(self, request_id: str, state: str) -> None:
        if request_id not in self._states:
            raise UnknownRequestError(request_id)
        if self._states[request_id] in InteractionState.TERMINAL:
            raise RequestNotFoundOrClosedError(
                f"请求 {request_id} 已处于终态 {self._states[request_id]}"
            )
        self._states[request_id] = state

    def expire(self, request_id: str) -> None:
        self._close(request_id, InteractionState.EXPIRED)

    def cancel(self, request_id: str) -> None:
        self._close(request_id, InteractionState.CANCELLED)

    def cancel_all(self) -> list[str]:
        """取消所有 pending 请求（run 取消 / UI 断连）；返回被取消的 id 列表。"""

        cancelled: list[str] = []
        for request_id in list(self._requests):
            if self._states.get(request_id) == InteractionState.PENDING:
                self._states[request_id] = InteractionState.CANCELLED
                cancelled.append(request_id)
        return cancelled

    def resolve(self, reply: InteractionReply) -> InteractionReply:
        """校验并落定一次回复。

        - 请求不存在 → ``UnknownRequestError``
        - 已过期 → ``RequestExpiredError``（含"回复到达时才过期"）
        - 已终态 + 相同回复 → 幂等返回原结果
        - 已终态 + 冲突回复 → ``ReplyConflictError``
        - 参数摘要不匹配 → ``ReplyConflictError``（防篡改/错绑）
        """

        request_id = reply.request_id
        request = self.get(request_id)
        state = self._states[request_id]

        if not _reply_matches_request(reply, request):
            raise ReplyConflictError(
                f"回复的 session/run/tool 身份与请求不匹配（request {request_id}）"
            )

        # 终态回复也必须先验证显式摘要。否则一个旧端口省略摘要后，
        # 后到的错误摘要可能在幂等分支绕过绑定检查。
        if reply.params_digest is not None and reply.params_digest != request.params_digest:
            raise ReplyConflictError(
                f"回复的参数摘要与请求不匹配（request {request_id}）"
            )

        if state == InteractionState.EXPIRED:
            raise RequestExpiredError(request_id)

        if state in (InteractionState.RESOLVED, InteractionState.CANCELLED):
            previous = self._replies.get(request_id)
            if previous is not None and _same_reply(previous, reply):
                return previous  # 幂等
            raise ReplyConflictError(
                f"请求 {request_id} 已处于终态 {state}，拒绝冲突回复"
            )

        if request.is_expired():
            self._states[request_id] = InteractionState.EXPIRED
            raise RequestExpiredError(request_id)

        self._states[request_id] = InteractionState.RESOLVED
        self._replies[request_id] = reply
        return reply


def _same_reply(first: InteractionReply, second: InteractionReply) -> bool:
    return (
        first.approved == second.approved
        and (first.answer or "") == (second.answer or "")
        and _optional_same(first.params_digest, second.params_digest)
        and _optional_same(first.session_id, second.session_id)
        and _optional_same(first.run_id, second.run_id)
        and _optional_same(first.tool_call_id, second.tool_call_id)
        and _optional_same(first.tool_name, second.tool_name)
        and _optional_same(first.plan_id, second.plan_id)
        and _optional_same(first.plan_digest, second.plan_digest)
        and first.tool_input == second.tool_input
        and getattr(first, "metadata", None) == getattr(second, "metadata", None)
    )


def _optional_same(first: str | None, second: str | None) -> bool:
    """兼容旧端口省略字段，同时不放宽 pending 阶段的绑定校验。"""

    return first is None or second is None or first == second


def _reply_matches_request(reply: InteractionReply, request: InteractionRequest) -> bool:
    """校验回复携带的可选来源身份，兼容旧端口省略身份字段的写法。"""

    return all(
        value is None or value == getattr(request, field)
        for field, value in (
            ("session_id", reply.session_id),
            ("run_id", reply.run_id),
            ("tool_call_id", reply.tool_call_id),
            ("tool_name", reply.tool_name),
            ("tool_input", reply.tool_input),
            ("plan_id", reply.plan_id),
            ("plan_digest", reply.plan_digest),
            ("metadata", getattr(reply, "metadata", None)),
        )
    )
