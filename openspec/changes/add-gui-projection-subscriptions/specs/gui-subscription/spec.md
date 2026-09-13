## Purpose

规定"原子快照 + 增量订阅"的边界、游标续传与过期、慢消费者有界失败，以及草稿版本替换语义，使非终端消费者在不丢 canonical 事件的前提下持续接收变化。

## ADDED Requirements

### Requirement: 投递面与订阅身份必须被定义

服务 MUST 提供四个操作，其返回类型固定：

- `snapshot(session_id)` → `GuiSnapshotResult{status, cursor, snapshot}`，`cursor` 为 `GuiCursor`；
- `subscribe(session_id)` → 带 `subscription_id` 的**异步迭代器** `GuiStream`；其第一条消息 MUST 是 `kind="snapshot"` 的快照；
- `resume(cursor)` → `GuiResumeResult{status, cursor, stream, error_code, current_high_water}`；
- `unsubscribe(subscription_id)` → `GuiUnsubscribeResult{status, subscription_id}`，MUST 关闭该 `GuiStream`。

同一 session MUST 允许多个并发订阅，各自独立编号、独立缓冲、独立关闭。`subscription_id` 是**进程内身份**；其 wire 编码不属本 capability。

`GuiCursor` MUST 至少含 `{subscription_id, session_id, high_water, projection_version, partial_versions}`。

#### Scenario: 订阅以快照开场

- **WHEN** 调用方 `subscribe` 一个 session
- **THEN** 它收到的第一条消息是 `snapshot`，其 `payload.high_water` 即该订阅的起始边界

#### Scenario: 同一 session 多订阅互不影响

- **WHEN** 对同一 session 建立多个并发订阅
- **THEN** 每个订阅有独立的 `subscription_id` 与独立缓冲，关闭其中一个不改变其余订阅的状态

#### Scenario: 断开订阅

- **WHEN** 调用方 `unsubscribe(subscription_id)`
- **THEN** 该订阅被关闭、其迭代器终止；`unsubscribe` 一个未知 id 返回 `unknown_subscription`

### Requirement: 不留缺口由 ordinal 与左开后缀保证

`snapshot`/`subscribe` MUST 在一个 ordinal 边界 `H` 上构建快照，并从 `after_ordinal = H` 读取增量；**MUST NOT** 依赖任何写入侧通知钩子。`OutputPort` 只可作唤醒提示，MUST NOT 作为事实源。

投递语义是 **at-least-once**：服务 MUST NOT 重发同一 canonical ordinal；消费者仍 MAY 据 ordinal 幂等去重。非前缀事实（草稿、待处理交互）MUST NOT 被当作"不留缺口"等式的一部分。

"不留缺口"的定义（可复算等式）：把快照中 `ordinal <= H` 的条目，与增量流按序收到的 `(H, H2]` 条目合并，MUST 与在该 session 上以 `high_water = H2` 直接投影得到的结果**逐条相等**（消息身份、ordinal、摘要、字节数一致）。

#### Scenario: 快照与首次后缀读取之间提交的事件不丢

- **WHEN** 一次 canonical 提交恰好发生在快照边界确定之后、第一次后缀读取之前
- **THEN** 该事件不出现在快照里，但作为增量按序到达，且其 ordinal 严格大于快照边界

#### Scenario: 合并结果等于在 H2 上的直接投影

- **WHEN** 把快照（边界 `H`）与后缀 `(H, H2]` 的增量按序合并
- **THEN** 合并得到的消息序列与以 `high_water = H2` 直接投影得到的结果逐条相等

#### Scenario: 同一 ordinal 不重发

- **WHEN** 服务连续多轮读取后缀
- **THEN** 同一个 ordinal 至多被投递一次，且投递序的 ordinal 单调不减

### Requirement: 游标续传与过期必须是显式结果

`resume(cursor)` MUST 在缓冲仍覆盖该游标、`projection_version` 与当前一致、且服务实例未更替时从游标之后继续；否则 MUST 返回 `cursor_expired` 并携带当前 high-water，要求调用方重新执行 `snapshot + subscribe`。MUST NOT 静默跳过任何 canonical 事件。

过期判据 MUST 明确为三类：(a) 缓冲不覆盖该游标；(b) `projection_version` 不一致；(c) 进程内服务实例更替（`service_epoch` 不同）。

#### Scenario: 缓冲覆盖时续传

- **WHEN** 调用方以近期游标 `resume`，且该订阅的缓冲仍覆盖该游标
- **THEN** 返回续传结果，且游标之后的事件按序到达（含断开期间缓冲的事件）

#### Scenario: 不可续传时明确过期

- **WHEN** 调用方以已被裁剪的游标、`projection_version` 不一致的游标、或另一服务实例签发的游标 `resume`
- **THEN** 返回 `cursor_expired`、具体 `error_code` 与当前 high-water；调用方重新走原子快照后能获得一致视图

### Requirement: 慢消费者必须有界失败且不影响他人

每个订阅的未消费条目数 MUST 受具名常量 `SUBSCRIPTION_BUFFER_LIMIT` 约束：达到上限后，新条目 MUST 替代**最旧的已排队**条目。若某订阅连续 `SUBSCRIPTION_STALL_GRACE_SECONDS` 未消费任何条目，服务 MUST 向该订阅发出**恰好一次** `resync_required` 并关闭该订阅；runtime 与其他订阅 MUST 继续正常工作。

在消费中的订阅 MUST NOT 因突发流量被关闭：每次取走条目 MUST 重置该时钟。

#### Scenario: 溢出只影响该订阅

- **WHEN** 一个订阅长时间不消费直到超限，同时另一个订阅正常消费
- **THEN** 未消费的订阅收到**恰好一次** `resync_required` 并被关闭；另一个订阅继续收到后续事件，运行不受影响

#### Scenario: 消费中的订阅不被突发关闭

- **WHEN** 账本在短时间内新增远多于 `SUBSCRIPTION_BUFFER_LIMIT` 的事件，而订阅持续消费
- **THEN** 该订阅收到全部事件且保持打开，不出现 `resync_required`

#### Scenario: 终态与交互不因合并被丢弃

- **WHEN** 缓冲已满时到达终态事件与交互请求
- **THEN** 两者都仍被投递（或以 `resync_required` 明确关闭该订阅），MUST NOT 静默消失

### Requirement: 草稿以版本替换，final 清除草稿

草稿更新 MUST 携带 `stream_key` 与单调 `revision`（等于 `StreamingPartialSnapshot.last_partial_seq`），消费者按**替换**语义处理同一 `stream_key`；终态 MUST 清除对应草稿。草稿 MUST NOT 改变 `high_water`。同一 `stream_key` 的中间 revision MUST 允许被合并而不可见；"不得静默跳过"仅约束 canonical 事件。`stream_key`、`partial_seq`、canonical ordinal 与 wire 序号 MUST NOT 互相推导。

#### Scenario: 同 stream 后到覆盖先到

- **WHEN** 同一 `stream_key` 连续产生多个草稿版本
- **THEN** 消费者只保留最新版本，且 `revision` 严格递增（可跳号）

#### Scenario: 草稿不推动边界

- **WHEN** 只发生草稿变化，没有任何 canonical 事件写入
- **THEN** 订阅的 `high_water` 不变，而草稿更新仍然通过增量流到达

#### Scenario: final 清除草稿

- **WHEN** 某 stream 收到终态
- **THEN** 该 `stream_key` 的草稿行被清除，后续快照的 `drafts` 不再包含它

### Requirement: 订阅不持有执行权，也不产生事实

断开订阅 MUST NOT 取消或中断任何 run。订阅路径 MUST NOT 写入 canonical、MUST NOT 调用模型或工具、MUST NOT 打开第二个存储连接。存储句柄 MUST 由宿主注入。

#### Scenario: 退订不影响运行

- **WHEN** 一个正在运行的会话被退订
- **THEN** run 继续执行并正常到达终态；重新订阅后能够读到该终态

#### Scenario: 订阅与投影路径零 canonical 写入

- **WHEN** 在静默 store 上执行 `snapshot`、`subscribe` 与后缀读取
- **THEN** canonical 的事件 id 集合、计数与前缀 digest 完全不变，storage 文件哈希不变
