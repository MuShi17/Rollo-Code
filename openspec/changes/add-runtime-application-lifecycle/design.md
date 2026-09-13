## Context

C02 已把 runtime 产生的输出和人工交互抽象为 `OutputPort`、`InteractionPort`，并由 `InteractionRegistry` 负责交互请求的身份校验、单向状态转移和回复幂等。`RuntimeEventEmitter`、`DurableToolBoundary` 和 `SQLiteRuntimeStore` 已经分别承担 canonical 事件、工具操作耐久边界和事件提交；SQLite 写入使用受保护事务，并在提交后才返回成功。

当前入口仍由 `__main__.py` 直接构造和持有 `Agent`。REPL 的 SIGINT 路径读取 `Agent._aborted`/`_output_buffer`，运行取消、会话恢复、终端输入和受管理 shell 还没有一个可供 TUI、未来 GUI 及其他进程内调用方复用的统一控制面。C03 要解决的是应用层生命周期和拥有权，而不是再次实现事件事实源或新增跨进程 wire 协议。

本设计对应 proposal 中的四个 capability：`runtime-application-api`、`runtime-execution-ownership`、`runtime-control-persistence` 和 `tui-application-integration`。它也遵循四份 delta spec：公开 API 只返回结构化控制结果，所有副作用命令先完成幂等记录和提交，再允许 dispatch；恢复不得猜测或自动重放不确定工具；TUI 继续消费 C02 端口。

约束如下：

- `ProjectContext` 是所有严格入口的唯一 workspace 来源，规范化后的 `workspace_id` 是锁、控制记录和运行身份的绑定键；不得通过当前目录、终端文本或 UI 布尔值补推上下文。
- C02 canonical 事件事实源保持不变。控制记录是控制面状态和恢复证据，不替代事件 ledger，也不把派生 session JSON 当作写入事实源。
- 不新增第三方依赖，不实现 stdio host、GUI 投影、Electron 壳、Windows 安装包或付费 Harbor benchmark。
- C03 实现只能在本 Change 的 design/specs/tasks 通过独立审查并获得主 Agent D(C03) 接受、工作树和 writer 明确后进行；本设计本身不授权代码改动。

## Goals / Non-Goals

**Goals:**

- 建立进程内 `Application` 控制外观，统一承载 `session.create/list`、`run.start/status/cancel`、`interaction.respond` 和 `shutdown`。
- 让命令身份、参数摘要、workspace root owner、child owner 树和生命周期终态具有可恢复、可审计的控制记录。
- 将取消传播到模型流、C02 交互等待、受管理 shell 和 child Agent，并把“任务已请求取消”与“OS 子进程已退出”分开表达。
- 在同一规范化 workspace 上为 TUI 和 Application API 提供同一把 OS 级拥有权锁，防止第二个 root run 静默 dispatch。
- 使 one-shot、REPL、resume 和 TUI 适配器经由 Application API 工作，同时保留既有 flags、权限、退出码、REPL 命令、EOF 语义和 C02 结构化事件边界。
- 以临时 workspace/runtime 目录、离线 provider/worker、真实本地 Python 子进程和 CLI/TUI consumer 组合出分层验证证据，并显式区分替身与真实 OS 进程证据。

**Non-Goals:**

- 不重写 `RuntimeEvent`、`RuntimeEventEmitter`、session projection 或 C02 port 的 canonical 语义。
- 不把 Application API 做成网络协议；本 Change 只定义进程内调用边界，不创建 stdio/HTTP/RPC host。
- 不在 C03 中交付 GUI、Electron、Windows 打包、跨进程 GUI 通信、部署或真实付费 Harbor 任务。
- 不自动接管、恢复或重放一个没有充分 dispatch/结果证据的旧运行；不通过清空数据库、覆盖旧记录或删除事实解决迁移问题。
- 不以本 Change 的 OpenSpec 工件完成推导 C03 产品代码、Git 提交、推送、MR、发布或部署授权。

## Decisions

### D1：以 Application 作为唯一进程内控制面，Agent 作为受管理执行器

新增 `application.py`，对外提供结构化 command/result 类型和异步控制方法；外部调用方只提交操作名、稳定身份、`ProjectContext`、受限参数及超时，返回 session/run/interaction/owner 状态。Application 内部负责命令校验、幂等查询、拥有权仲裁、状态持久化和执行监督，再把实际模型调用委托给已有 `Agent`。

`Agent` 继续负责 provider loop、工具选择和 C02 事件发布，但不再成为 TUI 的生命周期协议。Application 创建 Agent 时显式注入 `ProjectContext`、`OutputPort`、`InteractionPort`、runtime store 和 owner context；取消通过 Application 的 supervisor 调用公开的 Agent/port 取消能力，并将结果写回控制面。入口不得读取或修改 `_aborted`、`_output_buffer`。

命令名称保持 proposal/spec 中的点号形式，但 Python 方法名和 dataclass 的具体命名属于实现细节；它们必须共同映射到同一份 command envelope，不得为 CLI、TUI、未来 GUI 各自再造一套控制状态机。

**考虑过的替代方案：**

- 让 `Agent` 自身直接暴露全部 session/run API：会把 provider loop、控制面和入口生命周期耦合在一起，且不能自然覆盖跨入口 workspace 锁，因此不采用。
- 让 TUI 继续调用 `Agent.abort()` 并通过私有字段判断运行中：无法支持外部状态查询和重启恢复，也会把终端适配器重新变成控制协议，因此不采用。
- 新建 HTTP/stdio host 作为统一入口：超出 C03 的进程内边界，并会把跨进程协议问题提前混入本 Change，因此不采用。

### D2：用命令 envelope 和单一状态串行器实现幂等与终态唯一

所有 `run.start`、`run.cancel`、`interaction.respond` 等有副作用操作使用统一 envelope，至少包含 `command_id`、`scope_type`、`scope_id`、`operation`、`params_digest`、`session_id`、可选 `run_id/request_id`、提交时间和 schema 版本。参数摘要只覆盖允许持久化的规范化参数，不保存 API key、完整 provider 配置或原始敏感载荷。

Application 先在同一个控制边界内按 `(scope_type, scope_id, command_id)` 查询：相同摘要返回第一次的响应/目标身份；摘要不同返回冲突；未知命令再进入状态转移。进程内用一个按 session 的异步串行器避免竞态，跨进程依靠 SQLite 受保护事务和 session 租约共同约束。终态转移使用集中 guard，成功、失败、取消、interrupted、uncertain 只能单向提交一次；取消与终态竞争时，谁先在控制事务中成功落定谁拥有终态，另一方只能读取已落定结果。

互斥的单位是 **session** 而非 workspace：每个 session 拥有自己的 canonical store，因此同一 workspace 的不同 session 可以被不同客户端（例如 TUI 与 GUI）同时持有并各自运行；只有同一 session 不能被两个存活进程同时持有。workspace 级 `owner` 记录保留为诊断与恢复归属信息（把孤儿 run 归因到某个进程并按 D14 分类），不作为拒绝新工作的闸门。

**考虑过的替代方案：**

- 只在内存中用 `asyncio.Lock`：只能保护一个 Application 实例，不能阻挡第二个 TUI/进程，因此不采用。
- 只依赖事件序号推导 command 幂等：canonical 事件可保持事实，但无法表达响应丢失后的控制重试和摘要冲突，因此单独保存控制命令记录。
- 让 cancel 无条件覆盖成功/失败：会制造两个终态或伪造取消成功，违反生命周期 spec，因此不采用。

### D3：控制记录使用 workspace 控制库，canonical 事件库保持事实边界

新增版本化的 workspace 控制存储层，默认位于 `ProjectContext.runtime_data_dir` 下按 `workspace_id` 隔离的应用控制目录；具体文件名和目录迁移必须在任务中与现有 `runtime_store_path()` 兼容性核对后确定。控制层保存 session index、run control、command、owner、pending-interaction 和迁移诊断，canonical 事件仍写入 C02 现有 `SQLiteRuntimeStore`。

一次副作用命令遵循固定顺序：

1. 规范化上下文、身份和受限摘要，检查 owner/状态/交互请求。
2. 在 `BEGIN IMMEDIATE` 或等价受保护事务中写入 command accepted、必要的 run/interaction 状态和 dispatch intent。
3. flush/commit 成功后才调用 Agent、InteractionRegistry 或受管理 shell。
4. 将 dispatch 已观察到的阶段、返回值或错误再次写入控制记录，并由 C02 canonical 事件记录实际运行事实。

提交前进程崩溃不能留下无控制记录的 dispatch；提交后至 dispatch/结果记录之间的崩溃被恢复为 `interrupted` 或 `uncertain`，不能靠自动重试弥补 exactly-once 的未知窗口。响应丢失时，原 command 记录是重试的唯一命中源，不重复调用 provider/tool。

**考虑过的替代方案：**

- 把控制字段直接塞进 canonical event payload：会改变 C02 事实 schema，并把控制重试和业务事实混为一层，因此不采用。
- 继续把 session JSON 当主数据库：当前 JSON 是 canonical projection/cache，不能提供跨入口原子命令和 owner 互斥，因此不采用。
- 通过重建/删除旧库完成迁移：会破坏历史事实且无法恢复未知结果，因此不采用。

### D4：workspace 锁使用 OS 持有句柄，owner 元数据只做诊断

实现 `workspace_lock.py` 的后端接口，以规范化 `workspace_id` 计算锁键，并使用标准库实现的 OS 级独占句柄/咨询锁。锁的“是否持有”由活跃句柄决定，而不是 lock 文件是否存在或其中记录的 PID；锁记录中的 `owner_id`、root/run/session、创建时间和版本仅用于冲突诊断。TUI 和 Application 都从同一个工厂取得同一个锁键。

root owner 成功后生成不可变 `owner_id`，写入控制记录并把 root、child Agent、managed shell 和派生句柄挂到同一 owner tree。只有持有相同 root owner 的控制边界才能创建/取消/释放 child；锁释放必须校验 token，不能因为另一个调用方失败而释放他人的锁。不同 workspace 使用独立键和控制目录。

跨平台细节由后端隐藏：Windows 使用可证明的独占文件句柄/平台锁语义，其他平台使用相应 OS advisory lock；不得把 PID 文件存在性作为互斥实现。真实多进程测试必须覆盖“入口 A 持锁、入口 B 争用、A 退出后 B 才能取得”的顺序，并验证仍存活子进程时不提前释放锁。

**考虑过的替代方案：**

- 只在 `Application` 上放布尔值：无法覆盖第二个进程。
- 只创建 `.pid` 文件：崩溃后容易陈旧，且文件存在不等于持锁。
- 让每个 child 自己申请 root：会绕过 workspace 互斥和父子取消树，因此不采用。

### D5：以 ManagedExecutionHandle 统一取消与 OS 退出观察

模型任务、交互等待、child Agent 和 shell 都注册到 root owner 的 supervisor。每个受管理项有稳定 execution identity、parent/root owner、requested 状态、confirmed 状态和未完成原因。`run.cancel` 只接受一次有效取消命令，然后按模型/交互/child/shell 顺序传播；后续相同命令返回同一结果，错误身份不得触碰执行树。

受管理 shell 由标准库 subprocess 封装为异步 handle，启动时绑定 workspace cwd 和 owner；stdout/stderr 由独立 drain 任务持续读取，取消先执行有界优雅停止，再按策略强制终止，最后等待并记录真实 return code/仍运行状态。取消 asyncio task 或 provider task 只表示任务控制状态变化，不能直接推导 OS 子进程死亡。若超时仍存活，run 保持 `cancelling`/`interrupted`/`uncertain` 等未完成状态并保留 owner 锁。

shutdown 使用明确的阶段状态：禁止新的 start → 请求并等待活跃执行 → flush partial/terminal 控制记录和 C02 事件 → 关闭 MCP/store 等资源 → 仅在确认完成后释放 owner。任何阶段超时返回未完成项和当前 OS/owner 证据，下一次启动可读取，不伪造成功关闭。

**考虑过的替代方案：**

- 取消时仅调用 `asyncio.Task.cancel()`：不能终止 shell，也无法确认 stdout/stderr drain。
- 直接 `kill` 后立即释放锁：可能遗留子进程和写入，违反“仍运行不释放”的边界。
- 让每个 tool 自己实现取消：传播顺序和幂等无法统一，因此由 owner supervisor 统一编排、由各 handle 报告真实状态。

### D6：TUI 是 Application 的消费者，C02 port 仍是事件/交互端口

`__main__.py` 和 `tui_adapter.py` 只负责解析既有 flags、创建 `ProjectContext`、构造终端 ports、显示事件和读取输入；one-shot、REPL、resume、SIGINT、EOF 以及 REPL 命令转换为 Application command。SIGINT 的首次运行中取消通过公开 `run.cancel`，第二次退出走 `shutdown`/CLI 既有退出路径，不读取 Agent 私有状态。

TerminalOutputPort 继续复用 `ui.py`，不解析控制台文本来获得状态；TerminalInteractionPort 继续是终端输入的唯一位置，并把 `InteractionRequest`/`InteractionReply` 交给 C02 `InteractionRegistry` 的同一身份校验。plan approval 若需要额外输入，也必须通过 Application 绑定的交互桥接，不能成为隐藏的第二控制协议。CLI flags、permission mode、退出码和拒绝/EOF 语义用现有行为回归测试锁定。

**考虑过的替代方案：**

- 在 TUI 中复制一份 session/run 状态机：会让两个入口的 owner、cancel 和恢复语义漂移，因此不采用。
- 让 TUI 直接订阅 SQLite 或终端日志：SQLite/event projection 不是控制命令边界，终端日志也不具备身份和幂等语义，因此不采用。

### D7：恢复按证据分类，默认 fail closed

启动 Application 时先读取控制记录、C02 canonical ledger 和已知 migration version，再生成恢复诊断：已接受但未确认 dispatch 的命令/运行标为 `interrupted` 或可安全处理的未执行状态；已观察到工具请求但结果提交不完整的标为 `uncertain`。Unknown tool name 是执行前的查询失败，unknown/uncertain tool result 是执行后证据不足，两者分别记录、分别测试。

恢复只提供状态查询、显式 resume/人工决策和安全的只读 projection；禁止自动重放可能有副作用的工具。旧 session 先以只读形式解析，迁移写入新版本前保留原记录和未知字段；迁移失败只返回诊断，不删除、覆盖或静默绑定到当前 workspace。没有可证明 workspace 归属的旧记录不得被当前 `ProjectContext` 自动认领。

### D8：冻结 control/canonical 双库边界与崩溃协议

控制记录使用独立的 workspace 控制库：`ProjectContext.runtime_data_dir / "application" / workspace_id / "control.sqlite"`；canonical store 的唯一映射为 `ProjectContext.runtime_data_dir / "sessions" / session_id / "runtime.sqlite"`，由显式 context/session 解析并校验 workspace_id，Application 必须把该路径注入 Agent 和 store，禁止回退到全局 cwd/导入期常量。两者不共享事务，也不把控制记录写入 canonical event payload。跨库一致性由 `dispatch_intent`、`canonical_correlation_id` 和恢复分类协议表达：

1. control commit 前禁止 provider、tool、shell 或 interaction dispatch；
2. control commit 后才允许 dispatch，并把观察到的 canonical event / execution handle 关联回控制记录；
3. 任一跨库窗口崩溃均落为 `interrupted` 或 `uncertain`，恢复只读并禁止自动重放；
4. 测试必须由独立 worker 进程在 control commit 前、control commit 后/canonical append 前、canonical append 后/result 记录前和 response 丢失四个精确屏障调用 `os._exit`（Windows 等价为 `TerminateProcess`），再启动新的 Application 进程；每个阶段都要联合检查 control.sqlite、canonical SQLite、恢复状态和 provider/tool/shell side-effect marker/count。

控制库使用 schema version、workspace_id、command_id 唯一键和事务内状态 guard；canonical store 的 append/close 仍由 C02 原有接口负责。这样明确承认跨库不存在 exactly-once，同时保证“可追踪、不可静默重放”。

控制记录中的 `canonical_correlation_id` 是版本化的结构化关联对象，不是 C02 `operation_id` 的别名：它至少包含 `workspace_id/session_id/run_id`，以及截至当前观察到的 `invocation_ids[]/turn_ids[]` 集合和逐工具对象 `tool_operations[]`（每项为 `operation_id/provider_tool_call_id/tool_name/canonical_args_hash`）。`command_id` 只属于 control 命令幂等域；每个 C02 tool dispatch 以自身 `operation_id` 单独加入集合，因此一个 run 可以关联零个、一个或多个 tool operation，provider-only terminal 的 `tool_operations[]` 明确为空。集合按规范化排序保存，缺失或矛盾的关联不猜测、不自动合并。

### D9：冻结锁命名空间与 owner capability

锁命名空间为 `sha256("rollo-workspace-lock-v1:" + workspace_id)[:32]`，锁句柄位于控制目录下；锁键、控制库路径和诊断中的 workspace_id 必须由同一个规范化 `ProjectContext` 生成。owner token 使用不可猜测的随机 capability，记录 `owner_id/root_owner_id/parent_owner_id` 和 generation；PID、文件存在性和 UI 状态只允许用于诊断。

root owner 取得锁后才能接受 root `run.start`；child 只接受同一 root capability 派生的 capability。foreign release/cancel、旧 generation、不同 workspace 和已关闭 owner 均返回明确错误且不修改执行树。物理 OS handle 由 root Application 持有，root 崩溃后 OS 会释放 handle；控制库把 owner row 标记为 `uncertain/held` quarantine，新 Application 在显式 `owner.reconcile` 前拒绝新的 root start。`owner.reconcile` 必须携带 owner_id、generation 和 OS/child evidence，可选择 inspect、terminate 或 release；不得 silent adopt、silent release 或 replay。真实多进程测试必须验证“root 崩溃后第二入口被逻辑 quarantine 拒绝；reconcile 前后 child、lock 和 control row 的精确状态”。

### D10：冻结 interaction Future、tool-call 绑定和 plan/REPL contract

每个 pending interaction 同时登记 `request_id/session_id/run_id/tool_call_id/tool_name/tool_input/plan_id/plan_digest/params_digest`（不适用值显式为 null），ApplicationInteractionPort 为其创建唯一 awaitable Future。`interaction.respond` 必须校验全部身份和 digest，在控制事务中落盘、由适配器调用一次 registry.resolve 后完成同一个 Future；Agent 在此路径不得再次 resolve。重复回复返回原结果，cancel/shutdown/timeout 以固定 reason 的非批准 `InteractionReply` 完成 Future，迟到回复不能触发工具。Future 完成是等待方解除的唯一控制信号，不允许 TUI 直接调用 registry 私有状态。

plan approval 使用同一 `interaction.respond` envelope，沿用 C02 已存在的 `InteractionKind.APPROVAL`，并在受限 metadata 中标记 `plan_approval=true`，不扩展第二种 kind；REPL 每条 prompt 复用同一 session、创建新 run，并通过 `run.status` 观察终态。one-shot、REPL、resume 的 flags、EOF、退出码和权限模式由同一 Application contract 适配，不为 plan 或 REPL 增加第二套状态机。重启后旧 Future 必须固定为 `interrupted`；新进程不得唤醒旧 Future，旧 request 的 `interaction.respond` 必须拒绝，继续只能由显式 `run.resume`/`owner.reconcile` 创建新的 decision generation。

### D11：冻结 run-level cancel 去重

`run.cancel` 的幂等键除 command identity 外还包括 `(workspace_id, run_id, cancel_generation)`。同一 run 的第一个有效 cancel 生成唯一 `cancel_generation`、进入 `cancelling` 并向 supervisor 发出一次传播；不同 `command_id` 的后续 cancel 只读取该 generation 的结果，不再次 dispatch。若 run 已有成功/失败/取消/中断/不确定终态，cancel 只能返回已落定结果，不覆盖终态。超时后的继续动作只能是显式 `owner.reconcile`/`run.resume` 新命令，不能隐式重复 cancel dispatch。

### D12：冻结旧 session 归属与 inspect-only

控制记录或 session 缺少可验证 workspace_id 时，`session.list` 只能返回 `inspect_only` 条目；resume/run/interaction/cancel/shutdown 对该条目一律拒绝，除非调用方显式提供一次性迁移映射并通过 schema 校验。映射失败保留原文件和未知字段，不按当前 cwd、session 名称或 PID 自动认领。

### D13：冻结审查证据与实现入口

设计审查必须逐条检查 D8-D12 及 proposal 的 Gate closure contract；测试策略必须提供真实本地 Python 子进程、多进程 owner、两个独立 Application 进程的 command-idempotency 竞争、双 stdout/stderr drain、crash/restart fault injection、CLI/TUI consumer 和 C02 regression 的可复现命令。只有两类独立审查均 sufficient、`openspec validate --strict` 通过、主 Agent 完成差异/授权核验后，才允许进入 C03 implementation；独立审查意见不自动等于接受。

### D14：冻结按 operation 的 control/canonical 状态映射矩阵

Application control 状态不替换 C02 canonical 状态；恢复通过下表把证据投影为唯一可观察的 C03 状态：

| operation | control evidence | canonical evidence | C03 result | error_code | side effect rule |
| --- | --- | --- | --- | --- | --- |
| `run.start` | no accepted row / commit failed | no dispatch | `not_accepted` | `command_not_accepted` | no side effect; retry is safe only because no accepted evidence exists |
| `run.start` | accepted, no dispatch_intent | no model/tool dispatch | `interrupted` | `run_interrupted_before_dispatch` | never auto-dispatch |
| `run.start` | dispatch_intent committed | no canonical model/tool dispatch | `interrupted` | `run_dispatch_not_observed` | manual inspection; no auto-replay |
| `run.start` | dispatch_intent committed | canonical tool_dispatch without matching outcome | `uncertain` | `tool_outcome_uncertain` | side-effect count must not increase on recovery |
| `run.start` | accepted and terminal evidence | canonical `completed` | `succeeded` | `null` | terminal is read-only |
| `run.start` | accepted and terminal evidence | canonical `failed` | `failed` | `provider_error` | terminal is read-only |
| `run.start` | accepted and terminal evidence | canonical `cancelled` | `cancelled` | `cancelled` | terminal is read-only |
| `run.start` | accepted and terminal evidence | canonical `budget_exceeded` | `failed` | `budget_exceeded` | terminal is read-only; preserve budget error details |
| `run.start` | accepted and no tool call | provider/model terminal success with matching correlation | `succeeded` | `null` | provider-only evidence is matched by correlation |
| `run.start` | accepted and no tool call | provider/model terminal error with matching correlation | `failed` | `provider_error` | provider-only evidence is matched by correlation |
| `run.cancel` | cancel accepted, propagation unconfirmed | no terminal evidence | `cancelling` | `cancel_propagation_unconfirmed` | one cancel generation; owner retained |
| `run.cancel` | cancel propagation confirmed | canonical `cancelled` | `cancelled` | `cancelled` | no second propagation |
| `run.cancel` | recovery reports abort without canonical terminal | recovery `aborted` | `interrupted` | `recovery_aborted` | no second propagation; explicit resume/reconcile only |
| `interaction.respond` | reply and Future commit complete | no subsequent dispatch yet | `resolved` decision; run remains waiting/running | `null` | response is not a run terminal |
| `interaction.respond` | command accepted but reply or Future commit incomplete before crash | no completed reply evidence | `interrupted` | `interaction_interrupted` | old request/Future is never revived; old response is rejected |
| `interaction.respond` | reply and Future commit complete; dispatch phase not yet started | no subsequent dispatch yet | `resolved` decision; run remains waiting/running | `null` | later dispatch is classified by the `run.start` rows |
| `interaction.respond` | reply and Future commit complete | canonical tool_dispatch/outcome matched | interaction remains `resolved`; run projection follows `run.start` rows | `null` | digest mismatch never dispatches |
| `shutdown` | phase accepted but incomplete | live child/store/MCP evidence | `shutdown_incomplete` | `shutdown_incomplete` | second entry rejected; quarantine retained |
| `shutdown` | all phases confirmed | matching terminal/closed evidence | `shutdown_complete` | `null` | owner released last |
| any | control/canonical terminal conflict | contradictory evidence | `uncertain` | `control_canonical_conflict` | preserve both records, no overwrite |

The control command identity remains `(workspace_id, session_id, run_id, command_id, request_id?, tool_call_id?, tool_name?, params_digest?)`; nullable fields are encoded as explicit nulls. `command_id` MUST NOT be mapped to or stored as a C02 `operation_id`. Canonical matching is frozen as follows: `session_id` and `run_id` must match exactly; observed C02 `invocation_id` and `turn_id` are retained as sets (they are not one-to-one replacements for `run_id` or `request_id`); a `request_id` may be related to a `turn_id` only when the same interaction turn is explicitly recorded; each tool dispatch/outcome is joined by its own `operation_id` plus `provider_tool_call_id/tool_name/canonical_args_hash`, and the complete paired object is retained in `canonical_correlation_id.tool_operations[]`. Provider-only terminal evidence is joined by exact `(session_id, run_id)` plus any recorded invocation/turn constraints and has an empty tool-operation set. `error_code` is a stable lowercase string or JSON `null`; `null` is reserved for confirmed success/resolved/shutdown-complete projections, while cancellation is a terminal-with-reason and therefore uses `cancelled`. The precedence is: durable tool dispatch without outcome forces `uncertain`; a terminal control row cannot override contradictory canonical evidence; canonical evidence cannot create a control terminal without a matching command/run identity. The following four hard-crash barrier oracle is normative and MUST assert every column, including exact `error_code` and side-effect count:

| barrier | operation | control evidence | canonical evidence | result | error_code | side-effect count after recovery |
| --- | --- | --- | --- | --- | --- | --- |
| B1 control commit before dispatch | `run.start` | no accepted row | no dispatch | `not_accepted` | `command_not_accepted` | `0` |
| B2 accepted before canonical dispatch | `run.start` | accepted + dispatch_intent | no canonical dispatch | `interrupted` | `run_dispatch_not_observed` | `0` |
| B3 canonical dispatch before outcome | `run.start` | accepted + dispatch_intent | tool_dispatch without outcome | `uncertain` | `tool_outcome_uncertain` | unchanged from pre-crash marker; no recovery increment |
| B4 interaction response lost | `interaction.respond` | command accepted, reply/Future incomplete | no completed reply or tool dispatch | `interrupted` | `interaction_interrupted` | `0` |

Unknown tool lookup MUST return `unknown_tool`; an unknown/uncertain post-dispatch result MUST return `unknown_tool_result` and preserve `uncertain`. Migration failure MUST return `migration_error`, and an inspect-only record MUST return `inspect_only`; these codes are distinct from the D14 crash barriers.

Canonical identity edge cases are deterministic: a tool dispatch with no `operation_id` in its action/refs returns `result=uncertain`, `error_code=canonical_identity_missing`, is quarantined without execution, and asserts side-effect count `0`; contradictory `session_id/run_id/invocation_id/turn_id/operation_id/provider_tool_call_id/tool_name/canonical_args_hash` returns `result=uncertain`, `error_code=canonical_identity_conflict`, preserves both records, and recovery adds no side effect; multiple same-run tool operations are tracked independently and a missing outcome makes only the corresponding operation uncertain while the run projection remains `uncertain` until its operation set is resolved. A provider-only terminal with more than one candidate invocation/turn and no explicit match returns `result=uncertain`, `error_code=canonical_identity_ambiguous`, preserves all candidates, and recovery adds no side effect; none of these three errors projects a success/failed/cancelled terminal or triggers automatic replay.

### D15：冻结 Application interaction adapter and restart protocol

The C03 Application supplies an `ApplicationInteractionPort` implementing the existing C02 `InteractionPort.request(request) -> InteractionReply` contract. On request, the adapter first persists the pending identity and creates one Future keyed by `request_id`; it then delegates display/observation to the injected port. `Application.interaction_respond` validates identity/digest, persists the reply, resolves the C02 registry exactly once, and completes that Future. Agent code never resolves the registry a second time on this adapter path.

Cancel, shutdown and timeout each complete the same Future with a non-approved `InteractionReply` carrying a stable reason (`cancelled`, `shutdown_timeout` or `expired`); a late reply is rejected and cannot dispatch a tool. On restart, a new Application reconstructs the pending row but MUST mark the old process wait as `interrupted`; an `interaction.respond` for that interrupted request is rejected with `interaction_interrupted`. Continuing requires explicit `run.resume` or `owner.reconcile`, which creates a new `decision_generation` and command/run identity; it may not replay an uncertain tool. This is the sole restart behavior.

### D16：冻结 tool-call digest and managed execution model

The approval digest input is canonical JSON v1 (RFC 8785/JCS semantics: UTF-8, sorted object keys, preserved array order, no insignificant whitespace, explicit nulls, rejected NaN/Infinity) of `{session_id, run_id, request_id, tool_call_id, tool_name, tool_input, plan_id, plan_digest}`. `tool_input` and non-applicable plan fields are explicit nulls. The digest is the full lowercase SHA-256 hex string, never an implicit truncation. A plan digest is separately the SHA-256 of canonical `{plan_id, displayed_plan}`; the interaction digest includes that plan_digest. Public Application replies MUST include all identity fields and this digest; only an internal C02 migration adapter may fill omitted legacy fields, and that adapter cannot be used by C03 Application/TUI paths. A changed tool input, displayed plan, reused provider tool-call id or mismatched request identity returns `interaction_binding_error` before any dispatch.

Managed shell execution uses `asyncio.create_subprocess_exec` with separate bounded drain tasks, a process-group/Windows Job Object backend, and an execution handle recorded under the root owner. The handle reports `requested_cancel`, `graceful_exit`, `forced_exit`, `returncode`, `pid_alive` and `descendant_alive`; drain queues apply backpressure and spill complete bytes to a per-execution artifact when the bounded in-memory limit is reached, recording byte count/hash (no silent truncation). A root crash releases its physical OS handle but leaves the control row in `uncertain/held` quarantine until a new Application explicitly reconciles it. MCP execution is registered in the same supervisor, while its transport close is a separate shutdown phase.

## Risks / Trade-offs

- **[Risk] commit 后 dispatch 前存在 crash window，无法证明 exactly-once。** → 记录 accepted/dispatch intent 的阶段，重启显式标为 interrupted/uncertain，禁止自动重放；把“可安全查询”和“需人工决策”分开验收。
- **[Risk] Windows 进程树终止语义可能因 shell 包装器不同而变化。** → 把 OS handle 做成平台后端，使用真实本地 Python 子进程覆盖优雅停止、强制终止、stdout/stderr drain 和超时保锁；未覆盖的 shell 行为不宣称 C03 通过。
- **[Risk] 新 workspace 控制目录与既有全局 session 目录并存时可能出现列表/恢复分裂。** → `session.py` 增加明确的 workspace 绑定/兼容适配，canonical store 仍是唯一事实；对无绑定旧 session 返回可诊断状态，不按当前目录猜测归属。
- **[Risk] Application supervisor 与 Agent 现有取消入口重复传播。** → 所有传播动作绑定 command_id/execution identity，并由一个 owner supervisor 去重；对重复 cancel 做相同结果回读测试。
- **[Risk] TUI 兼容层迁移期间可能改变退出码或 plan/EOF 行为。** → 在接线前冻结现有 CLI/TUI 回归样本，以相同 flags 和离线替身做 consumer 验证；任何必要变化先更新 spec，不以适配器临时分支绕过。
- **[Risk] 控制记录保存过多参数导致秘密泄露。** → 只保存 schema 白名单字段、类型化摘要和 digest；对 API key、provider 配置、原始交互输入做负向断言，并对日志/异常同样检查。
- **[Risk] 复用现有 SQLite store 时把控制事务误当成 canonical event 事实。** → 明确两层表/接口和提交责任，控制层只记录控制证据，canonical 事件仍通过现有 store/事件 emitter 写入；评审时逐项核对调用链。

## Migration Plan

1. 在实现前先锁定 C02 handoff（`openspec/changes/decouple-runtime-interaction-from-tui/tasks.md §6.2`）和现有 store/session/port 的事实，补齐 Application API、owner、migration 和 TUI consumer 的测试夹具边界；若该段落不存在，必须以当前公开接口核对并记录缺失。
2. 先添加版本化控制 schema 与只读加载/诊断，再添加写入事务和 workspace lock；老 session 只读兼容，明确迁移版本后才生成新控制记录。
3. 接入 command idempotency、run state guard、owner supervisor 和 managed shell，再把 `Agent` 的运行和取消回调纳入 Application；每一步先跑 focused tests，保持 C02 canonical tests 作为回归层。
4. 最后替换 one-shot、REPL、resume 和 TUI 的入口 wiring，运行离线 consumer、同 workspace 多进程争用、交互等待取消、真实子进程超时和恢复不重放测试。
5. 只有独立设计审查、独立测试策略、冻结 diff 复核和主 Agent D(C03) 接受都通过，才能进入实现；即使实现通过，也必须另行获得 Git 提交/推送、MR、发布或部署授权。

回滚策略是停止新的 Application wiring，保留 canonical event store 和旧 session 数据，使用兼容读取路径恢复旧入口；不得删除控制记录或覆盖旧 canonical 事实。若迁移失败，保留原版本并让 Application 返回 migration error，待修复后由显式迁移步骤重试。

## Resolved implementation parameters

- control DB 使用 D8 的独立路径与 schema version；canonical store 路径保持 C02 现状。
- lock namespace、owner capability、interaction Future、plan/REPL contract、cancel generation 和旧 session inspect-only 均按 D9-D12 冻结；实现不能把这些决策重新留给 TUI 或单个测试夹具。
- Windows 独占句柄、shell grace timeout 和进程树后端仍需以真实本地子进程实验选择具体标准库调用，但实验只能选择实现，不得改变 D8-D12 的可观察语义。
- C02 handoff 的规范路径为 `openspec/changes/decouple-runtime-interaction-from-tui/tasks.md §6.2`；若当前 checkout 无该段落，必须先记录缺失并以仓库现有 `runtime_ports.py`/`interactions.py`/`agent.py` 的公开接口核对，不得凭旧路径补写事实。
- root crash 的唯一语义是“物理 OS handle 释放、control owner quarantine held”；新入口必须先执行 `owner.reconcile`，不能把元数据当作物理锁仍持有。
- 持久 interaction deadline 使用 UTC wall-clock `expires_at`；运行期可同时保存 monotonic 观测值，但重启只按 UTC 和 schema version 判断过期。
- canonical session store 必须通过显式 `ProjectContext` 解析到 workspace 绑定路径，不能继续依赖导入期全局 `SESSION_DIR` 作为 Application 的事实来源。
