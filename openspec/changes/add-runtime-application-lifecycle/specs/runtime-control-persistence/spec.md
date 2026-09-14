## Purpose

规定 session/run/command/pending-interaction 控制记录的版本、原子边界、迁移和重启恢复，保护 canonical 事实并避免不确定副作用被自动重放。

## ADDED Requirements

### Requirement: 控制记录必须版本化且不保存秘密

系统 MUST 以明确 schema 版本保存 session、run、command、owner 和 pending-interaction 控制记录；命令和交互记录 MUST 保存必要的稳定身份与参数摘要，但 MUST NOT 保存 API key、完整配置、未受限的内部状态或原始秘密载荷。控制库 MUST 记录 workspace_id、dispatch_intent、cancel_generation，以及版本化结构 `canonical_correlation_id`：该结构至少保存精确的 `session_id/run_id`、观察到的 `invocation_ids[]/turn_ids[]` 和逐个配对的 `tool_operations[]`（每项含 `operation_id/provider_tool_call_id/tool_name/canonical_args_hash`）；它不是单个 C02 `operation_id`，公共 `command_id` 也不得映射为 tool operation。

#### Scenario: 记录可按版本读取

- **WHEN** 打开一个已知版本的控制记录
- **THEN** 系统能按其版本解析 session/run/command 身份和状态，并为未知版本返回可诊断的不支持结果

#### Scenario: 敏感字段被排除

- **WHEN** command 或 pending-interaction 记录被写入并读回
- **THEN** 记录只包含受限参数摘要和必要元数据，不包含 API key、完整 Provider 配置或原始敏感输入

### Requirement: 接受命令与 run 开启必须原子落盘后再调度

对于可能产生执行副作用的 `run.start`、`run.cancel` 和 `interaction.respond`，系统 MUST 在同一受保护控制边界内完成命令幂等记录、必要的 run/interaction 状态变更和提交点；只有提交点成功后才允许 dispatch。提交前崩溃 MUST 不产生无法追踪的执行，重复提交 MUST 不产生第二次副作用。

#### Scenario: commit 前崩溃不启动 run

- **WHEN** 在命令记录或 run 开启提交前注入进程崩溃
- **THEN** 重启后不存在已执行但无控制记录的 run，原命令可按未接受或可安全重试状态处理，不发生隐藏 dispatch

#### Scenario: commit 后重试不重复执行

- **WHEN** 命令提交成功后调用方因响应丢失再次提交同一 command_id 与摘要
- **THEN** 系统返回原 run/终态记录，工具或模型 dispatch 不重复发生

### Requirement: 旧 session 与 partial 数据必须可迁移或安全失败

系统 MUST 支持现有旧 session 和 canonical partial 数据的只读兼容与显式 schema 迁移；迁移 MUST 保留可读事实和未知字段，失败时 MUST 保留旧数据并返回可诊断错误，不得通过删除数据库、覆盖旧记录或丢弃事实解决兼容问题。显式 workspace 映射前，缺少归属的记录 MUST 为 inspect-only。

#### Scenario: 旧 session 读取并迁移

- **WHEN** 启动后读取一个旧版本 session 并请求 `session.list` 或 resume
- **THEN** 系统按迁移规则返回可读 session，记录迁移版本和结果，历史 canonical/partial 内容不被改写为伪造新事实

#### Scenario: 迁移失败保留旧数据

- **WHEN** 旧记录缺少必需字段或迁移步骤失败
- **THEN** 系统返回明确迁移错误，旧文件/记录仍可读取，且不会通过删库或自动重建覆盖原数据

### Requirement: 重启恢复必须区分中断与不确定结果且禁止自动重放

重启时系统 MUST 将未执行的 accepted command、未完成 run 和未确认的工具结果按记录证据分别标为 interrupted 或 uncertain；未知/不确定工具结果 MUST 保留其不确定性。系统 MUST NOT 自动重新执行可能产生副作用的工具，也 MUST NOT 把未知工具名错误和未知工具结果混为一个重试语义。

#### Scenario: accepted 但未 dispatch 的命令恢复

- **WHEN** 进程在命令已接受但 dispatch 尚未确认时崩溃，随后重新启动
- **THEN** 恢复结果标识该命令为 interrupted 或可安全处理的未执行状态，不自动调用原工具

#### Scenario: 工具结果不确定时不重放

- **WHEN** 重启发现工具调用记录存在但结果提交不完整或未知
- **THEN** 系统保留 uncertain 结果并要求显式人工/上层决策，不自动再次执行该工具

#### Scenario: 未知工具名与未知结果分开

- **WHEN** 模型请求一个未知工具名，或恢复一个未知/不确定的已请求工具结果
- **THEN** 前者返回 Unknown tool 查询失败且无重试路径，后者进入 uncertain 恢复语义；两者的记录、状态和测试断言彼此区分

### Requirement: control 与 canonical store 的崩溃窗口必须可分类

控制库与 canonical event store MAY 是不同事务边界，但系统 MUST 持久化 `dispatch_intent` 和 canonical correlation identity，使 control commit 前、dispatch 后和 canonical/result 记录之间的崩溃均可分类为未执行、`interrupted` 或 `uncertain`。任何分类 MUST 禁止无证据自动重放副作用。

#### Scenario: control commit 前崩溃

- **WHEN** 进程在 command accepted 或 dispatch intent 提交前崩溃
- **THEN** 重启看不到已执行但无控制记录的副作用，命令可安全地报告为未接受或未执行

#### Scenario: 跨库窗口崩溃

- **WHEN** control commit 已成功但 canonical dispatch/result 记录尚未完成时崩溃
- **THEN** 恢复通过 correlation identity 返回 interrupted/uncertain 诊断，不自动重放 provider、tool 或 shell

#### Scenario: 独立 worker 硬崩溃后重启

- **WHEN** 独立 worker 在四个精确屏障之一调用 `os._exit` 或 Windows 等价终止，随后启动新的 Application 进程
- **THEN** 联合 oracle 能从 control.sqlite、canonical SQLite、恢复状态和 side-effect marker/count 判定未执行、interrupted 或 uncertain，且副作用计数不会因恢复自动增加

### Requirement: control 状态必须按 canonical 证据确定性投影

恢复 MUST 使用 design.md D14 的按 operation 固定映射，并同时返回其中冻结的 `error_code`。公共 `command_id` 仅是 control 幂等身份，绝不等同于 C02 `operation_id`；`canonical_correlation_id` 必须保留精确的 `session_id/run_id`、观察到的 `invocation_ids[]/turn_ids[]` 和逐个配对的 `tool_operations[]`，同一 run 的多个 tool operation 不得折叠。`run.start` 无 accepted row/commit failure 为 `not_accepted/command_not_accepted`，accepted 无 dispatch_intent 为 `interrupted/run_interrupted_before_dispatch`，dispatch_intent 无 canonical dispatch 为 `interrupted/run_dispatch_not_observed`，有 canonical tool_dispatch 但无 matching outcome 为 `uncertain/tool_outcome_uncertain`，matching canonical completed 为 `succeeded/null`、failed 为 `failed/provider_error`、cancelled 为 `cancelled/cancelled`、budget_exceeded 为 `failed/budget_exceeded`，provider-only terminal success/error 以 exact `(session_id, run_id)` 及已记录 invocation/turn 约束映射为 `succeeded/null` 或 `failed/provider_error`；`run.cancel` 在传播未确认时为 `cancelling/cancel_propagation_unconfirmed`、canonical cancelled 为 `cancelled/cancelled`、recovery aborted 为 `interrupted/recovery_aborted`；`interaction.respond` 在 reply commit/Future completion 之前崩溃时，旧 request/Future 为 `interrupted/interaction_interrupted` 且旧回复被拒绝，在 reply 已提交且 Future 已完成、尚无后续 dispatch 时只产生 `resolved/null` decision、run 保持 waiting/running，后续 tool/model dispatch 再按 `run.start` 矩阵分类；`shutdown` 阶段未完成为 `shutdown_incomplete/shutdown_incomplete`、全部确认才为 `shutdown_complete/null`。control/canonical 终态冲突 MUST 为 `uncertain/control_canonical_conflict`，不得由任一层静默覆盖另一层。`error_code` 为稳定小写字符串或 JSON `null`，除确认的正常 terminal 外不得省略。

四类硬崩溃屏障必须按同一固定 oracle 断言 operation、control evidence、canonical evidence、结果、`error_code` 与恢复后的副作用计数：B1 control commit 前为 `run.start/no accepted/no dispatch/not_accepted/command_not_accepted/0`；B2 accepted+dispatch_intent 但无 canonical dispatch 为 `run.start/interrupted/run_dispatch_not_observed/0`；B3 canonical tool_dispatch 无 outcome 为 `run.start/uncertain/tool_outcome_uncertain` 且恢复不得增加 marker/count；B4 `interaction.respond` 已接受但 reply/Future 未完成为 `interaction.respond/interrupted/interaction_interrupted/0`。未知工具查询返回 `unknown_tool`，未知或不确定的已 dispatch 结果返回 `unknown_tool_result` 并保持 `uncertain`；迁移失败返回 `migration_error`，inspect-only 返回 `inspect_only`；缺少 tool operation identity 返回 `uncertain/canonical_identity_missing` 且 side-effect count=`0`，矛盾身份返回 `uncertain/canonical_identity_conflict` 且恢复不增加副作用，provider-only 多候选返回 `uncertain/canonical_identity_ambiguous` 且恢复不增加副作用。

#### Scenario: durable dispatch without outcome

- **WHEN** recovery finds a durable tool dispatch without a matching tool outcome
- **THEN** the control projection is `uncertain`, the original evidence is preserved, and no provider/tool/shell retry is issued

#### Scenario: canonical terminal projection

- **WHEN** a matching canonical terminal and result evidence are present
- **THEN** the control status is the corresponding single terminal, and a conflicting control row is reported rather than overwritten

#### Scenario: two tool operations in one run

- **WHEN** one canonical run contains two durable tool dispatches with distinct `operation_id` and `provider_tool_call_id` values
- **THEN** the control correlation retains both operation identities, each outcome is joined only to its own operation, and a missing outcome for either operation projects `uncertain` without replaying the other operation

#### Scenario: provider-only terminal and ambiguous identity

- **WHEN** a run has no tool operation and a provider terminal matches exact `session_id/run_id` plus one recorded invocation/turn
- **THEN** it projects the provider success/error mapping; if multiple invocation/turn candidates remain without an explicit match, it returns `result=uncertain/error_code=canonical_identity_ambiguous`, preserves all candidates, and does not project a terminal or add a side effect

#### Scenario: missing or contradictory canonical identity

- **WHEN** a tool dispatch omits `operation_id` or a canonical event conflicts on session/run/invocation/turn/operation/provider-call/tool-name/args-hash identity
- **THEN** recovery returns `result=uncertain` with `error_code=canonical_identity_missing` or `canonical_identity_conflict`, preserves the evidence, and performs no dispatch or automatic replay; the missing-identity case has side-effect count `0`, while the conflict case does not increase the pre-recovery count

#### Scenario: interaction reply is not a run terminal

- **WHEN** `interaction.respond` commits a valid reply and completes its Future before any subsequent tool/model dispatch
- **THEN** the interaction decision is `resolved`, the run remains waiting/running, and a crash after the reply is classified by the later dispatch evidence rather than by a generic interrupted/uncertain choice

#### Scenario: interaction reply commit crashes before Future completion

- **WHEN** the process crashes after accepting an interaction command but before persisting the reply and completing the Future
- **THEN** the old request is recorded as `interrupted`, the old Future is not revived, and the old `interaction.respond` is rejected without dispatch

### Requirement: persisted interaction deadlines survive restart deterministically

Pending interaction records MUST store an absolute UTC `expires_at` plus schema version; a monotonic clock MAY be used only for in-process optimization. After restart, expiration MUST be decided from UTC and the stored version, and an expired request MUST complete/record `expired` rather than wait forever.

#### Scenario: pending interaction expires after restart

- **WHEN** a process crashes with a pending interaction and a new Application starts after the stored UTC deadline
- **THEN** the new process records `expired`, completes no old Future, and rejects late response without dispatch

### Requirement: 无 workspace 归属的旧 session 必须 inspect-only

缺少可验证 workspace_id 的旧 session 或 partial 数据 MUST 可读但 MUST 标记为 `inspect_only`；resume、run、interaction、cancel 和 shutdown MUST 拒绝对其执行副作用，除非调用方显式提交通过校验的一次性迁移映射。

#### Scenario: 旧 session 无归属

- **WHEN** `session.list` 发现旧记录没有 workspace 绑定
- **THEN** 系统返回 inspect-only 条目，且不会按当前 cwd 或 session 名称自动认领

#### Scenario: 显式迁移失败

- **WHEN** 一次性 workspace 映射缺少必需字段或校验失败
- **THEN** 系统返回迁移错误并保留原文件、未知字段和 canonical 事实

### Requirement: runtime data root 必须稳定绑定 workspace

Application 控制库和 canonical store 的根路径 MUST 来自显式 `ProjectContext.runtime_data_dir`，并在进入控制面前完成绝对化/规范化。显式相对 runtime-data-dir MUST 被拒绝或按固定、与 cwd 无关的基准解析；不得让不同 cwd 为同一 workspace 生成不同控制库或锁路径。

#### Scenario: 相对 runtime-data-dir 被拒绝

- **WHEN** 调用方以相对 runtime-data-dir 创建 Application
- **THEN** 系统返回明确路径错误，或使用预先冻结且可复算的固定基准；从不同 cwd 启动不会分裂控制库
