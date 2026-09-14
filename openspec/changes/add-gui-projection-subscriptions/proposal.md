## Why

C03 已经给出进程内的 Application 控制面（session/run/interaction/cancel/shutdown），但一个非终端消费者仍然只能自己去读 canonical 账本：它要自己决定怎么把事件折成可渲染的视图、自己决定读多少、自己处理"读历史的过程中又来了新事件"。这正是 GUI 最需要、也最容易做错的一层。

本 Change 交付 **进程内读模型**：一个有界的 GUI 投影、一个原子快照、一个可续传的增量订阅。它只消费既有事实，不产生事实。

## What Changes

- 新增 `projections/gui_projection.py`：把 canonical 事实折成**有界**的 GUI DTO（消息引用 + run/交互状态 + 草稿），正文一律走引用 + 分页，绝不在快照里内联大正文。字段**分三类**：①前缀字段（消息、终态、错误；`ordinal <= high_water`）；②非前缀字段（**run 状态**、草稿、待处理交互；无 ordinal、无前缀边界，带 `prefix_boundary_exempt: true`）；③禁止把②冒充①。run 状态属②，因为它在 C03 控制库中、没有 ordinal。
- 新增 `projections/subscriptions.py`：`SubscriptionService` 提供
  - `snapshot(session_id)`：在**一致边界**上取 canonical high-water、partial 草稿版本、待处理交互与投影视图，返回该边界的 `GuiCursor`；
  - `subscribe(session_id)`：登记订阅，先把快照投递为第一条消息，随后按 ordinal 单调地拉取后缀（`read_event_records(after_ordinal=last)`）；
  - `resume(cursor)`：缓冲仍覆盖游标、`projection_version` 一致且服务实例未更替时续传，否则返回 `cursor_expired` 并要求重新走原子快照；
  - `unsubscribe(subscription_id)`：关闭该订阅（不取消 run）；`GuiStream` 是异步迭代器投递面；
  - 有界缓冲：条目数达到 `SUBSCRIPTION_BUFFER_LIMIT` 后，最旧的**已排队**条目被最新条目替代；持续 `SUBSCRIPTION_STALL_GRACE_SECONDS` 不消费则发**恰好一次** `resync_required` 并关闭**该订阅**（runtime 继续运行，不受影响）。
- **交付路径是 ordinal 拉取式**（已冻结）：事实源只有两条可选路径——(a) 轮询 + `after_ordinal`（本 Change 的实现）；(b) `OutputPort` 仅作唤醒提示（它无 ordinal、非全量、`emit_safely` 会吞异常，**不可作为事实源**）。**不依赖任何未声明的写入侧钩子**：`SQLiteRuntimeStore.append` 经 `RuntimeEventEmitter.emit` 是纯转发，不认识本服务。
- **C04 不复用 `IncrementalModelReplayCursor`**：自建 `GuiCursor`（含 `subscription_id`/`session_id`/`high_water`/`projection_version`/`partial_versions`/`service_epoch`）。`subscription_id` 是**进程内身份**，其 wire 编码属 C05。
- **存储连接由宿主注入**（`SubscriptionService(store=..., control_store=...)`），不自开第二连接。
- 明确 **C04 不引入 wire 格式**：进程内 DTO 与 `transport_seq` 由 C05 落到 stdio 上；C04 只保证语义与边界。
- 明确 **订阅不持有执行权**：断开订阅不取消 run；订阅不得写 canonical，也不得触发模型或工具。

## Capabilities

### New Capabilities

- `gui-projection`: 有界 GUI DTO 的形状、前缀/非前缀字段分类、分页与正文引用规则，以及"投影只读"的边界。
- `gui-subscription`: 原子快照 + 增量订阅的边界、游标续传/过期、慢消费者有界失败与草稿版本替换。

### Modified Capabilities

（不修改既有 main capability。C04 只新增读模型，不改变 C03 的 Application API 语义，也不改变 C02 的事件与交互端口。）

## Impact

- 新增 `src/rollo/projections/gui_projection.py`、`src/rollo/projections/subscriptions.py` 及对应测试。
- **复用（只读）**既有投影：`SessionProjection`、`CanonicalMetricsProjection`、`RunTraceProjection`、`iter_event_records`/`source_digest`，以及 `SQLiteRuntimeStore.read_event_records` / `high_water` / `read_runtime_stream_partials`。本 Change **不重写**这些模块。
- **新增的读接口（唯一一处触及 C03 文件）**：`Application.runs_list(session_id)` —— 把已存在的 `ControlStore.runs_for_session` 暴露出来。这是**纯新增只读方法**，不改任何既有方法与其语义；重连 GUI 需要先枚举 session 的 run，才能看到挂在其上的待处理交互。
- **依赖方向**：C04 对 C03 是**只读依赖**（调用 `ControlStore.run` / `runs_for_session` / `pending_for_run` 与 `Application` 的只读方法），不改其语义。
- **不修改** `agent.py`、`tools.py`、`runtime_store.py`、`run_lifecycle.py`。
- 不新增第三方依赖；不实现 stdio host、Electron 壳或 Windows 包（C05–C07）。
- 验证使用临时 workspace/runtime 目录；测试须覆盖一致性边界、跨 session 隔离、续传与过期、慢消费者有界失败、草稿版本替换，并断言订阅不产生任何 canonical 写入。

## C04 Gate closure contract

- 证据身份以当前 checkout `D:\PycharmProjects\pythonProject\Rollo-Code`、`feat/c03-application-lifecycle`（HEAD `410ac7e`）与 Python `>=3.11` 为准（实测 runtimePython 为 `D:\Anaconda\envs\py313\python.exe`）。
- 前置：C03 的 Application API 与 canonical 事实源已可用（`528 passed / 0 skipped`）。本 Change 不依赖 C05 的 wire 格式。
- D(C04) 必须同时满足：投影的**有界性**有可复算判据（具名常量 + 分页上限 + 正文不内联）；快照与订阅的**不留缺口**有不留缺口的等式 oracle；游标**续传与过期**各有正反用例；慢消费者**有界失败**必须发恰好一次 `resync_required` 且不影响其他订阅与 runtime；并显式断言订阅路径**零 canonical 写入**。
- 本 Change 的代码入口在 D(C04) 通过并获得用户对 C04 的授权后进行（用户 2026-09-13 已授权 C04 的文档 + 代码全流程）。
