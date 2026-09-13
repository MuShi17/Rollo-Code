## Purpose

规定本机 runtime 的统一 Application API、session/run/interaction 命令身份、状态和幂等边界，使 TUI、未来 GUI 与其他受控进程内调用方使用同一运行控制入口。

## ADDED Requirements

### Requirement: Application API 提供统一的运行控制入口

系统 MUST 提供进程内 Application API，至少覆盖 `session.create`、`session.list`、`run.start`、`run.status`、`run.cancel`、`run.resume`、`interaction.respond`、`owner.reconcile` 与 `shutdown`。调用方 MUST 通过结构化请求和响应使用这些入口，不得依赖 Agent 私有字段、终端输出或 UI 按钮作为控制协议。

#### Scenario: 创建并查询会话

- **WHEN** 调用方以一个有效的 ProjectContext 请求 `session.create`，随后请求 `session.list`
- **THEN** 系统返回稳定的 session 身份，且列表只包含与该 workspace 绑定、可读取的会话记录

#### Scenario: 公开入口返回结构化运行状态

- **WHEN** 调用方提交 `run.start` 并轮询 `run.status`
- **THEN** 响应包含稳定的 session/run 身份、当前生命周期状态和可关联的结果信息，不要求解析终端文本或访问 Agent 私有字段

#### Scenario: 交互回复进入同一控制边界

- **WHEN** 调用方提交带 request/session/run/tool 身份的 `interaction.respond`
- **THEN** 回复由同一 Application 控制边界交给 C02 InteractionRegistry 校验，错误身份或已结束请求不能触发工具执行

### Requirement: 命令身份和重复命令必须幂等

每个会改变运行状态的命令 MUST 带有命令身份、作用域身份和参数摘要。相同作用域内相同命令身份与相同摘要的重复请求 MUST 返回首次请求的原结果且不得重复调度；相同命令身份但摘要不同 MUST 被拒绝。命令记录 MUST 能区分 session、run、interaction 和 owner 作用域。

#### Scenario: 重复 run.start 不重复启动

- **WHEN** 同一 session 以同一命令身份和相同参数摘要提交两次 `run.start`
- **THEN** 两次响应指向同一个 run，模型/工具 dispatch 最多发生一次

#### Scenario: 命令身份冲突被拒绝

- **WHEN** 已接受的命令身份再次携带不同参数摘要
- **THEN** 系统返回明确的冲突错误，保留首次命令结果，不创建第二个 run 或覆盖原记录

#### Scenario: cancel 与终态竞争只有一个结果

- **WHEN** `run.cancel` 与 run 的成功、失败或交互回复几乎同时到达
- **THEN** 控制边界只提交一个合法终态，所有重复或冲突命令都返回该终态的确定结果

### Requirement: 运行生命周期和状态查询必须诚实

Application MUST 为 queued、running、waiting_interaction、cancelling、succeeded、failed、cancelled、interrupted 和 uncertain 等状态定义可验证的转移约束；终态 MUST 单向且唯一。状态查询不得在尚未确认 OS 执行停止、持久化完成或关闭完成时伪造成功。

#### Scenario: 活跃 run 可观察交互等待

- **WHEN** run 等待一个 C02 InteractionRequest
- **THEN** `run.status` 返回 `waiting_interaction` 及可关联 request 身份，调用方可以提交回复或取消，而无需接触终端输入

#### Scenario: 未确认停止不报告成功

- **WHEN** shutdown 或 cancel 超时且受管理 shell、MCP 或持久化仍未确认完成
- **THEN** API 返回未完成信息或相应的 cancelling/interrupted/uncertain 状态，不返回成功终态

### Requirement: 同一 workspace 的 root run 由 Application 统一仲裁

Application MUST 以规范化 `session_id` 约束 active run；同一 session 同时最多一个 active run，child run MUST 绑定到该 session 的 owner 树。同一 workspace 的不同 session MUST 可以各有 active run。第二个针对**同一 session** 的 root 请求 MUST 被明确拒绝（`session_conflict`），不得静默抢占，也不得产生第二次 root dispatch。

#### Scenario: 同一 session 的第二个 root 被拒绝

- **WHEN** 两个受控 Application 调用方同时为同一 session 请求 `run.start`
- **THEN** 只有第一个获得租约，另一个收到 `session_conflict`，且不会产生第二次 root dispatch

#### Scenario: 同一 workspace 的不同 session 各自运行

- **WHEN** 同一 workspace 的两个 session 分别请求 `run.start`，且第一个仍在运行
- **THEN** 两个 run 都被接受并各自运行；它们各自的 canonical store、控制记录与终态互不覆盖

#### Scenario: 不同 workspace 可独立运行

- **WHEN** 两个 Application 调用方分别为两个不同规范化 workspace 请求 `run.start`
- **THEN** 两个 root 可以分别获得租约，彼此的 session、控制记录和 dispatch 不互相覆盖

### Requirement: interaction.respond 必须绑定实际工具请求

`interaction.respond` MUST 同时校验 `request_id`、`session_id`、`run_id`、`tool_call_id`、`tool_name`、`tool_input`、`plan_id`、`plan_digest` 和 `params_digest`；不适用字段 MUST 显式编码为 null。`plan_digest` MUST be the full SHA-256 of canonical JSON `{plan_id, displayed_plan}`；`params_digest` MUST be the full lowercase SHA-256 of canonical JSON v1/RFC 8785 semantics over `{session_id, run_id, request_id, tool_call_id, tool_name, tool_input, plan_id, plan_digest}`. Application MUST 将回复提交到同一个 pending Future/InteractionRegistry 请求。公共 Application/TUI 路径不得省略这些字段；digest 或身份不匹配时 MUST 拒绝回复且不得调用工具。

#### Scenario: 审批回复绑定真实 tool call

- **WHEN** 调用方回复一个等待中的审批请求并携带与请求记录不同的 tool_call_id 或 params_digest
- **THEN** 系统返回绑定错误、pending 请求保持未授权，且工具 dispatch 计数为零

#### Scenario: 合法回复唤醒等待方

- **WHEN** 调用方以完全匹配的身份和 digest 回复 pending 请求
- **THEN** 同一个等待 Future 被完成，等待方只获得一次授权，重复回复只返回首次结果

#### Scenario: interrupted interaction cannot revive an old wait

- **WHEN** a restarted Application receives `interaction.respond` for a request whose old process wait is `interrupted`
- **THEN** it returns `interaction_interrupted` without dispatch; continuation requires explicit `run.resume` or `owner.reconcile` with a new decision_generation and command/run identity

### Requirement: owner.reconcile and run.resume must be explicit recovery operations

`owner.reconcile` MUST require owner_id, generation and current OS/child evidence, and MUST choose inspect, terminate or release; it MUST NOT silently adopt or replay. `run.resume` MUST create a new command/run identity and MUST reject automatic replay of uncertain tool calls.

#### Scenario: quarantine blocks new root start

- **WHEN** the previous root process crashed and its owner row is `uncertain/held`
- **THEN** a new `run.start` is rejected until explicit reconcile, and reconcile records its decision and evidence before any release or new run

### Requirement: run.cancel 必须按 run generation 去重

同一 run 的有效取消 MUST 在控制事务中以唯一约束生成唯一 `cancel_generation`；并发插入失败者 MUST 重读已存在 generation。不同 command_id 的后续取消请求 MUST 读取该 generation 的结果，不得再次传播到模型、交互、child 或 shell；cancelling 超时只能重读或显式生成下一次人工恢复操作，不得隐式重复 dispatch。已落定终态 MUST 优先于新的取消请求。

#### Scenario: 不同命令身份的重复取消

- **WHEN** 两个不同 command_id 几乎同时取消同一 run
- **THEN** 只有一个 cancel generation 和一次 supervisor dispatch，两个响应指向同一取消/终态结果

#### Scenario: 取消并发插入失败后重读

- **WHEN** 两个进程同时为同一 run 插入 cancel generation，其中一个事务先提交
- **THEN** 失败事务重读已提交 generation 和结果，不产生第二次传播；超时仍保留 cancelling/owner 证据

### Requirement: 跨进程相同命令必须共享唯一结果

同一 workspace 中两个独立 Application 进程同时提交相同 `run.start`、相同摘要或冲突摘要时，控制库 MUST 以持久化唯一约束和 owner 锁仲裁结果。两个进程最终 MUST 读取同一个 command/result 或明确的 digest-conflict；provider/tool dispatch MUST 最多一次，失败进程不得释放另一进程的 owner。

#### Scenario: 两个进程竞争相同 run.start

- **WHEN** 两个独立进程同时提交相同 workspace、session、command_id 和 params_digest 的 `run.start`
- **THEN** 只有一个 root owner 和一个 run/dispatch 被创建，两个进程重读控制库后得到同一个结果

#### Scenario: 两个进程提交冲突摘要

- **WHEN** 两个独立进程使用同一 command_id 但不同 params_digest 竞争 `run.start`
- **THEN** 一个请求保留原结果，另一个得到 digest-conflict，重启后仍不存在第二个 run
