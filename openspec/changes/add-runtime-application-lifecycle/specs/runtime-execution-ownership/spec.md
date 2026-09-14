## Purpose

规定 session 级执行互斥、受管理执行句柄、取消传播和关闭顺序，确保 UI 层的取消请求与真实 OS 执行状态之间有可验证的边界。

## ADDED Requirements

### Requirement: 执行互斥的单位是 session，不是 workspace

互斥 MUST 以 session 为单位。同一 workspace 中的不同 session MUST 可以同时被不同客户端持有并各自运行，因为每个 session 拥有自己的 canonical store，彼此不共享任何写入目标。同一 session MUST NOT 被两个客户端同时持有：第二个持有者 MUST 收到明确的 `session_conflict` 结果，而不是静默交错写入同一会话。

session 租约 MUST 持久化在 workspace 控制库中并以原子事务获取（唯一键 + `BEGIN IMMEDIATE`），使跨进程判定不依赖进程内布尔值或 PID 文件的偶然存在。租约持有进程已消失时 MUST 自动接管，不得让 session 永久不可用。

`owner_id`/`root_owner_id`/`parent_owner_id`/`generation`/`workspace_id` MUST 继续持久化，作为**诊断与恢复归属**信息（把孤儿 run 归因到某个进程并按 D14 分类）；它 MUST NOT 被用作拒绝新工作的排他闸门。

#### Scenario: 同一 workspace 的不同 session 并行

- **WHEN** TUI 持有一个 session 并正在运行，GUI 在**同一 workspace** 打开另一个 session 并提交 run
- **THEN** 两边的 run 都被接受并各自运行；互不影响，也不产生冲突错误

#### Scenario: 同一 session 不被两个客户端持有

- **WHEN** 第二个客户端提交同一 session 的 run，而该 session 正被一个存活进程持有
- **THEN** 请求以 `session_conflict` 被拒绝，且租约持有者不被顶替

#### Scenario: 持有进程消失后 session 自动恢复可用

- **WHEN** 持有某 session 的进程已终止，另一个客户端提交该 session 的 run
- **THEN** 租约被接管、run 被接受；该 session 中上一持有者留下的非终态 run 必须先按 D14 分类为 `interrupted`/`uncertain`，且 MUST NOT 被重放

#### Scenario: workspace 隔离

- **WHEN** 两个规范化 workspace 同时运行
- **THEN** 每个 workspace 的 session 状态独立，一个 workspace 的活动不影响另一个

### Requirement: child execution 必须继承 owner 树

每个 child Agent、受管理 shell 和派生执行句柄 MUST 记录 parent/root owner 身份，并只能由拥有该 owner 树的控制边界创建、取消和收回。

#### Scenario: child 在父 owner 树内启动

- **WHEN** active root run 创建一个 child Agent 或受管理 shell
- **THEN** child 继承 root/session/run/owner 身份并计入同一 owner 树

#### Scenario: 外部调用方不能收回他人 child

- **WHEN** 不属于 owner 树的调用方提交 child cancel 或 shutdown 请求
- **THEN** 请求被拒绝且 child 的执行和 owner 记录不被修改

#### Scenario: root owner crash with live child

- **WHEN** the root Application process terminates while a managed child or shell is still alive
- **THEN** the physical OS handle is released by the OS, the control row is marked `uncertain` with `quarantine=1` for diagnostics, and the orphaned runs are classified from the canonical ledger; a later client MUST be able to adopt the workspace **without** a manual reconcile step, and it must not silently replay the child

### Requirement: 受管理 shell 的取消必须反映真实 OS 状态

系统 MUST 为受管理 shell 保存可控异步执行句柄和 owner 身份，持续 drain stdout/stderr，并区分优雅停止、强制终止、已退出和仍在运行。取消 asyncio task 或模型调用本身 MUST NOT 被当作 OS 子进程已经死亡。输出 drain MUST have bounded queues and report complete byte count/hash or an explicit truncation record; managed execution MUST cover descendant/Job Object state.

#### Scenario: shell 取消后确认进程退出

- **WHEN** run 取消一个持有输出流的受管理 shell
- **THEN** 系统先执行有界优雅停止并 drain 输出，必要时执行明确的强制终止，最终状态包含可验证的 OS 退出结果

#### Scenario: 子进程仍存活时报告未完成

- **WHEN** shell 在取消超时后仍未退出
- **THEN** 控制面返回未完成/仍运行信息并保留 owner 记录，不伪造 cancelled-success 或释放仍被占用的锁

### Requirement: 取消必须传播到所有受管理执行层

公开 `run.cancel` MUST 向模型流、C02 交互等待、受管理 shell 和 owner 树内 child Agent 传播，并在每一层记录已请求、已确认或未完成的状态。取消传播 MUST 至多触发一次有效 dispatch/cancel 操作。

#### Scenario: 交互等待期间取消

- **WHEN** run 处于 waiting_interaction 且调用方提交合法 `run.cancel`
- **THEN** InteractionRequest 被转为 cancelled，等待解除，后到的回复不再触发工具执行

#### Scenario: child 与 shell 同时取消

- **WHEN** root run 同时管理模型流、child Agent 和 shell，并收到一次 cancel
- **THEN** 所有受管理层收到同一取消身份，重复传播不会产生第二次 dispatch，最终结果逐层反映确认或未完成状态

### Requirement: shutdown 必须按顺序收敛并报告超时

`shutdown` MUST 停止新的 run.start，取消并等待活跃执行，flush partial 与终态记录，关闭 MCP/store 等资源，最后释放 owner 锁。任一阶段超时 MUST 返回未完成信息并保留可恢复的状态，不得以静默强杀或伪成功替代证据。

#### Scenario: 无活跃执行时正常关闭

- **WHEN** Application 没有 active run 且调用 `shutdown`
- **THEN** 新 start 被关闭，控制记录完成 flush，MCP/store 按顺序关闭，owner 锁最终释放并返回成功关闭结果

#### Scenario: 活跃执行关闭超时

- **WHEN** shutdown 等待一个不能在时限内确认退出的 shell 或 child
- **THEN** API 返回未完成项及当前 owner/OS 状态，下一次恢复可以读取该状态，不能报告完整关闭

### Requirement: session 租约命名空间必须稳定且可重算

租约键 MUST 是规范化 `session_id`，并绑定 `workspace_id` 与 `owner_id`。同一 session 的重复获取 MUST 幂等；只有当前持有者（同进程）可以释放，陈旧释放 MUST NOT 顶替另一个存活进程已接管的租约。

#### Scenario: 跨进程判定同一 session

- **WHEN** 两个独立进程对同一 session 调用获取
- **THEN** 只有存活持有者保留租约，另一个得到 `session_conflict`，且持有者记录不被改写

#### Scenario: 陈旧释放不顶替新持有者

- **WHEN** 一个已失去租约的进程调用 release
- **THEN** 释放被忽略，当前持有者的租约与控制记录保持不变

### Requirement: stdout/stderr 背压和 shutdown 屏障必须有 OS 证据

受管理 shell MUST 在 stdout/stderr 同时产生持续输出时持续 drain；shutdown MUST 在关闭 MCP/store 和释放资源前等待 drain、child 退出和 terminal/partial flush，超时则保留可恢复状态。

#### Scenario: 双流持续输出后取消

- **WHEN** 真实本地 Python 子进程同时写入超过 Windows pipe buffer 的 stdout/stderr，并在取消时延迟退出
- **THEN** drain 不死锁，两个流的完整 byte count/hash、return code 和 PID 退出状态均有证据

#### Scenario: shutdown 每个阶段超时

- **WHEN** 分别在 start gate、cancel/wait、partial/terminal flush、MCP/store close 阶段注入超时
- **THEN** 系统报告对应未完成阶段，严格保持阶段顺序，并保留可恢复状态
