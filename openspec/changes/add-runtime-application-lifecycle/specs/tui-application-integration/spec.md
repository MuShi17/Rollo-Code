## Purpose

规定既有 one-shot、REPL、resume 和 TUI 如何通过 C03 Application API 使用 C02 端口，保证 CLI 兼容，同时不把 TUI 私有状态重新变成 runtime 控制协议。

## ADDED Requirements

### Requirement: one-shot、REPL 与 resume 必须经由 Application API

CLI/TUI 适配器 MUST 通过 Application 的 session/run/interaction/status/cancel 入口驱动运行；不得直接读取或修改 Agent 的 `_aborted`、`_output_buffer` 等私有控制状态。ProjectContext 来源和历史 session 绑定 MUST 在入口转换时保持一致。

#### Scenario: one-shot 使用统一入口

- **WHEN** 用户以 one-shot 模式提交一个 prompt
- **THEN** CLI 创建或取得对应 session，通过 Application 启动并观察 run，输出与交互事件来自 C02 端口而不是私有字段轮询

#### Scenario: REPL 与 resume 使用同一 session 语义

- **WHEN** 用户在 REPL 发送多条消息或使用 resume 恢复既有 session
- **THEN** 每次运行都通过公开 Application API 关联同一 ProjectContext/session 历史，不能因入口不同生成互相不可读的控制记录

### Requirement: 既有 CLI/TUI 兼容行为必须保留

C03 接入 MUST 保持已有 flags、权限模式、退出码、REPL 命令、审批/EOF 语义和 C02 输出/交互端口的结构化事件边界。若出现必要的兼容变化，MUST 先在 C03 spec 中明确，而不得由适配器私自扩大权限或改变事实源。

#### Scenario: flags 与权限模式保持

- **WHEN** 以既有 flags（包括 plan、yolo、accept-edits、dont-ask、resume）运行 CLI
- **THEN** Application 接入不改变对应权限决策、退出码和安全拒绝行为，终端只负责适配展示与输入

#### Scenario: C02 端口事件继续可观察

- **WHEN** TUI 运行产生文本、思考、工具、预算、错误、交互和生命周期事件
- **THEN** 事件仍通过 C02 OutputPort/InteractionPort 关联到同一 session/run，TUI 不新增第二套 canonical 事实流

### Requirement: TUI 的 owner、取消和恢复必须可验证

TUI MUST 使用 C03 的共同 session 租约、命令身份和公开 cancel/status 接口；同一 session 的第二个入口必须得到明确的 `session_conflict`，而同一 workspace 的不同 session MUST 可以并行，取消必须传播到模型、交互、shell 和 child，resume 必须读取持久化状态而非自动重放不确定工具。

#### Scenario: 第二个入口不能重复 dispatch 同一 session

- **WHEN** 一个 TUI 已持有某 session，第二个受控 TUI/Application 入口提交**同一 session** 的运行
- **THEN** 第二个入口得到 `session_conflict`，不产生第二个 root dispatch

#### Scenario: 同一 workspace 的不同 session 并行（TUI 与 GUI）

- **WHEN** TUI 持有 session A 正在运行，受控 Application 在同一 workspace 提交 session B 的运行
- **THEN** session B 被接受；两个会话各自的 canonical store 与控制记录互不覆盖

#### Scenario: TUI 完成会话到恢复闭环

- **WHEN** TUI 依次执行会话创建、工具调用、审批、取消和 resume
- **THEN** 每一步都通过 Application API 产生可关联控制记录，取消/终态只有一个结果，resume 能读取中断或 uncertain 状态且不自动重放副作用工具

### Requirement: C03 的 TUI 验证不依赖 stdio、Electron 或付费 benchmark

C03 的 TUI/Application 集成 MUST 在进程内、离线、临时 workspace/runtime 数据目录中完成；`benchmark/harbor_agent.py` 只能作为只读消费者契约参考，不能把真实 Electron、stdio host、Windows 包或付费 benchmark 证据混入 C03 验收。

#### Scenario: 离线 TUI consumer 可运行

- **WHEN** 使用受控本地 Provider/worker 和临时 runtime 目录运行 one-shot/REPL consumer
- **THEN** 可验证 session/run/端口/取消/恢复契约，且测试结果明确标注为本地离线 consumer 证据

#### Scenario: 超出 C03 边界的入口被排除

- **WHEN** 验证需要 stdio wire、Electron 窗口、Windows 安装包或真实付费 Harbor 任务
- **THEN** 该验证被记录为 C05/C06/C07 或独立环境 Gate 的前置，不修改 C03 代码或把缺失证据宣称为 C03 通过

### Requirement: plan approval 与 REPL 必须使用同一 Application contract

plan approval MUST reuse C02 `InteractionKind.APPROVAL` with restricted `plan_approval` metadata, stable request/tool identity and the same pending Future used by other interactions. REPL MUST reuse one session while creating a distinct run per prompt; neither path MAY read Agent private state or create a second control state machine. After restart, the old Future MUST be recorded as interrupted; a new Application MUST reject the old response and require explicit `run.resume`/`owner.reconcile` with a new decision generation.

#### Scenario: plan approval through public interaction

- **WHEN** plan mode waits for approval and the caller submits `interaction.respond`
- **THEN** the approval is persisted with its digest, the pending wait is released once, and the run continues or is denied through the normal lifecycle guard; after restart the old wait is interrupted and a late response is rejected without dispatch

#### Scenario: REPL multi-turn session

- **WHEN** a user submits two prompts in one REPL session and resumes the session later
- **THEN** both runs share the same session/workspace binding, each has its own command/run identity, and resume observes persisted state without replaying uncertain tools

### Requirement: plan approval payload MUST be bound to plan identity

Plan approval MUST use C02 `InteractionKind.APPROVAL` plus restricted metadata containing `plan_id` and `plan_digest`; `plan_digest` MUST be the full SHA-256 of canonical JSON `{plan_id, displayed_plan}`, and the interaction digest MUST include that value plus the nullable tool-call fields under the same canonical JSON v1 rules. REPL commands such as `/clear`, `/plan` and `/compact` MUST be represented as explicit Application operations or read-only controls with stable session/run scope, not direct Agent private-state mutation.

#### Scenario: modified plan is rejected

- **WHEN** a caller responds with a digest for a plan different from the pending plan_id/plan_digest
- **THEN** the response is rejected before dispatch and the pending approval remains unresolved

#### Scenario: REPL control command scope

- **WHEN** a user invokes `/clear`, `/plan` or `/compact` during a session
- **THEN** the Application receives a typed operation with the same ProjectContext/session binding, and no direct private Agent lifecycle field is changed by the TUI

### Requirement: C03 consumer evidence must be real and reproducible

The C03 acceptance evidence MUST include a real local CLI/TUI consumer and a real local Python subprocess for ownership/cancellation cases, while provider responses MAY use an offline deterministic fixture. The evidence MUST record interpreter version, workspace/runtime paths, commands, exit codes, and whether each actor is mock, fixture, or OS process.

#### Scenario: offline CLI consumer evidence

- **WHEN** the focused C03 consumer command runs with a temporary workspace and deterministic offline provider
- **THEN** the recorded output demonstrates session/run/interaction/cancel/resume behavior without relying on private Agent fields or remote provider access, and records the exact command, exit code, interpreter version, repeat count and golden output/exit-code baseline
