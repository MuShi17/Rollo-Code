## Purpose

规定交给非终端消费者的**有界读模型**：会话视图的形状、前缀字段与非前缀字段的区分、正文引用与分页规则，以及"投影只读、不产生事实"的边界。

## ADDED Requirements

### Requirement: GUI 投影必须在显式边界上构建，且只含该 session 的前缀事实

投影 MUST 在单一不可变前缀上构建：调用方显式给出 `high_water = H`，投影 MUST NOT 读取该边界之后的事件，也 MUST NOT 在构建过程中重新查询"最新值"。投影 MUST 只包含请求 `session_id` 的事实。返回的快照 MUST 携带其来源边界（`high_water`）、`projection_version`（C04 自己的 `GUI_PROJECTION_VERSION`）与 `source_digest`。

`high_water` 字段本身 MUST NOT 被当作边界已生效的证据：判据是快照内容与在 `H` 上独立复算的前缀相等。

#### Scenario: 快照内容等于在 H 上的独立复算

- **WHEN** 调用方以 `high_water = H` 构建快照
- **THEN** 快照的 `source_digest` 与消息序列逐条等于用 `iter_event_records(store, high_water=H)` 过滤该 session 后独立复算的结果，且每条消息的 `ordinal <= H`

#### Scenario: 边界之后的事件不进入快照

- **WHEN** 在构建快照的请求发出之后、构建完成之前写入新事件
- **THEN** 该事件不出现在这份快照中；它只能通过该快照之后的增量流到达

#### Scenario: 两个 session 互不渗透

- **WHEN** 同一账本里有另一个 session 的事实，且其 ordinal 与本 session 交错
- **THEN** 本 session 的快照与另一 session 的快照不相交，两者的消息并集等于两个 session 的完整消息集

### Requirement: 前缀字段与非前缀字段必须被显式区分

快照 MUST 把字段分为两类并如实标注：

- **前缀字段**（`messages`、`terminals`、`errors`、`source_digest`）：由 `ordinal <= high_water` 的事实派生；
- **非前缀字段**（`runs`、`drafts`、`pending_interactions`）：**无 ordinal 边界**，是读取时刻的可变状态；其每一条 MUST 带 `prefix_boundary_exempt: true`。
  - `runs` 属于此类：run 状态存放在 C03 控制库中，**没有 ordinal**，物理上无法由 `ordinal <= high_water` 派生。把它归入前缀字段会让同一份快照同时声称两种分类——实测同一 `high_water` 与同一 `source_digest` 下可给出不同的 run 状态，而该 run 的终态 canonical 事件并不在快照内。
- 快照 MUST NOT 同时把同一字段列为前缀与非前缀；其 `prefix_fields` 与 `non_prefix_fields` 两个清单 MUST 与本节一致。

快照 MUST 另带该时刻草稿的最大 `last_partial_seq` 与读取时刻。投影 MUST NOT 把非前缀字段呈现为前缀事实。

#### Scenario: 草稿被标注为非前缀

- **WHEN** 会话存在 streaming 草稿
- **THEN** 快照的 `drafts` 含该草稿、其 `revision` 等于 `last_partial_seq`、其 `prefix_boundary_exempt` 为 `true`，且快照的 `high_water` 不因草稿变化而改变

#### Scenario: 待处理交互被标注为非前缀

- **WHEN** 某 run 上有 `pending` 状态的交互请求
- **THEN** 该请求出现在 `pending_interactions` 且带 `prefix_boundary_exempt: true`

#### Scenario: run 状态被标注为非前缀

- **WHEN** 同一 session 在**同一 `high_water`** 下先后取两次快照，期间只有控制库里的 run 状态发生变化
- **THEN** 两次快照的 `runs` 各条均带 `prefix_boundary_exempt: true`，且 `source_digest` 相同——消费者据此知道 run 状态不是该前缀的一部分，不得用它推导页码或游标

### Requirement: 正文必须分页引用，不得内联进快照

消息条目 MUST 只携带身份、序号、角色、种类、摘要、字节数与正文引用；正文 MUST 通过显式分页读取获得。`size` MUST 是 canonical JSON 的 UTF-8 字节数。快照单页消息条数 MUST 不超过具名常量 `SNAPSHOT_MESSAGE_PAGE_LIMIT`；`summary` MUST 不超过 `SUMMARY_CHAR_LIMIT`；单次正文读取 MUST 不超过 `BODY_PAGE_SIZE`。

#### Scenario: 大正文不进入快照

- **WHEN** 会话中存在正文远大于摘要上限的消息
- **THEN** 快照中该条目的 `size` 等于其 canonical JSON 的 UTF-8 字节数、`summary` 被截断到 `SUMMARY_CHAR_LIMIT`，且完整正文不出现在快照的序列化结果里

#### Scenario: 消息条目按页返回

- **WHEN** 会话消息数超过 `SNAPSHOT_MESSAGE_PAGE_LIMIT`
- **THEN** 快照返回该页条目与 `has_more`/`next_page_token`，且逐页读取可完整覆盖而不重复、不缺口

#### Scenario: 页令牌绑定边界

- **WHEN** 调用方用某页的 `next_page_token` 继续分页
- **THEN** 页令牌携带其产生时的 `high_water` 与前缀 `source_digest`，读取被固定在该边界上；前缀不再匹配时抛出 `GuiPageTokenError`，而不是静默读到更新的值

#### Scenario: 正文逐页可完整还原

- **WHEN** 某条消息的正文超过 `BODY_PAGE_SIZE`
- **THEN** 依次读取 `body_ref` 与其 `next_page_token` 得到的分片按序拼接后逐字等于完整正文，且每页 `size` 与该消息的 `size` 一致

### Requirement: 投影只读，不得产生事实

投影与正文读取 MUST NOT 写入 canonical 事件、MUST NOT 调用模型或工具、MUST NOT 修改控制记录、MUST NOT 打开第二个存储连接。

可观察判据（静默 store）：执行投影与分页读取前后，canonical 的事件 id 集合、事件计数、以及 `ordinal <= high_water` 的前缀 digest 三者完全相等，且 storage 文件的内容哈希不变。

#### Scenario: 投影不改变账本

- **WHEN** 在静默 store 上反复构建快照并读取正文
- **THEN** canonical 的事件 id 集合、计数与前缀 digest 保持不变，storage 文件哈希保持不变

#### Scenario: 并发写入只落在边界之后

- **WHEN** 投影读取期间确有其他 writer 提交新事件
- **THEN** 新增事件的 ordinal 全部 `> H`，快照内容不变
