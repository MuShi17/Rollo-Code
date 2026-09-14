## Why

GUI 是默认入口，而它现在无法观察任何东西：`Application` 是进程内对象，没有跨进程边界。C03 定义了控制面，C04 定义了读模型，但两者都只在同一个 Python 进程内可用。本 Change 把已有的 `Application` 只读面暴露成一条 stdio 协议，让桌面主进程能启动一个真实 Python 子进程并与它对话。

本 Change 交付**观测切片**：GUI 能列出会话、取一次原子快照、然后持续跟随一个会话的增量事件。控制类方法（`run.start`/`run.cancel`/`interaction.respond`）已在冻结的方法表里，但**明确不在本次实现**，并且在 wire 上以 `not_implemented` 显式拒绝，而不是伪装成未知方法或静默失败。

## What Changes

- 新增 `src/rollo/host/`：`protocol.py`（NDJSON 分帧、上限、错误映射）、`server.py`（薄适配层：控制类命令转发 `Application.dispatch`，观测面走 C04 订阅服务）、`__main__.py`（`python -m rollo.host` 入口）。
- 新增 `test_host_protocol.py`（分帧与错误码，纯函数）与 `test_host_process.py`（真实读循环 + 一条真实子进程用例）。
- 不改动 `src/rollo/` 的任何既有模块：host 是纯新增消费者，`Application`/`SubscriptionService` 一个字节都不改。

## 传输约定（沿用任务卡 §3.1 冻结口径）

- 一行一个 JSON 对象，UTF-8、LF；客户端必须处理半帧、合并帧与 UTF-8 字节拆分。
- 单帧上限 1 MiB；超限**关闭连接并留诊断**，不猜分帧。
- 首条调用为 `host.initialize`，协商 `protocol_version=1`、能力、workspace 身份、`host_epoch` 与限制；版本不符返回明确错误，不猜测兼容。
- JSON-RPC `id` 管响应关联，`command_id` 独立管业务幂等；通知没有响应，且不承担必须确认的动作。
- 不支持 JSON-RPC batch，`initialize` 的能力块里如实声明 `batch: false`。
- 标准错误用 JSON-RPC 标准码；业务错误在 `error.data.code`：`session_busy`、`scope_mismatch`、`command_conflict`、`request_expired`、`run_terminal`、`cursor_expired`、`frame_too_large`、`runtime_unavailable`、`not_initialized`、`unsupported_version`、`not_implemented`。
- **stdout 只含协议**；`stderr` 只做日志。

## 方法表（本次实现）

| 方法 | 输入 | 行为 |
| --- | --- | --- |
| `host.initialize` | `version`、可选 `workspace_id` | 版本、`host_epoch`、能力、限制、**缺失配置项名称**（不含值） |
| `session.list` | `limit`、`page_cursor` | 当前 workspace 的有界摘要，**不含 canonical 文件路径** |
| `session.snapshot` | `session_id` | 原子快照 + 边界 + digest |
| `events.subscribe` | `session_id`、可选 `cursor` | 建立订阅或续传；游标不可覆盖时返回 `cursor_expired` |
| `events.unsubscribe` | `subscription_id` | 仅停止观察，不停止 run |
| `host.shutdown` | — | 先停订阅再退出 |

已声明但本次回答 `not_implemented`：`run.start`、`run.cancel`、`interaction.respond`、`content.read`。

## Impact

- 受影响能力：新增 `host-protocol` 与 `host-process` 两份 spec 能力。
- 受影响代码：仅新增文件；`openspec` 工件新增。
- 消费方：C06 的 Electron 主进程与 C04 的读模型之间第一次有了真实进程边界。
- 不涉及：Electron、打包、网络端口、真实 Provider 调用。
