## Context

C02 把输出与人工交互抽象成 `OutputPort`/`InteractionPort`；C03 给出进程内 Application 控制面，并把 canonical 账本确立为唯一事实源。GUI 需要的第三件东西是**读模型**：把账本折成可渲染的视图，并且能在不丢事件的前提下持续接收增量。

现有仓库已经提供了大部分材料，本设计的原则是**组合而不是重建**：

| 已有 | 提供什么 |
| --- | --- |
| `SQLiteRuntimeStore.read_event_records(high_water=…/after_ordinal=…)` | 按 ordinal 的不可变前缀，以及左开区间的后缀；`after_ordinal` 是既有常规 warm-replay 路径 |
| `SQLiteRuntimeStore.high_water(session_id=…)` / `current_high_water` | 边界。`current_high_water` 是**全局**值，跨 session；按 session 取边界必须带 `session_id` |
| `projections/base.py:iter_event_records` / `source_digest` / `RuntimeEventReducer` | 事件折叠与来源摘要约定 |
| `SessionProjection` | 会话消息与 run 列表（已有 `high_water`、`messages`、`runs`、`errors`、`terminals`、`partial_count`） |
| `CanonicalMetricsProjection` / `RunTraceProjection` | 指标与 run 阶段轨迹 |
| `runtime_stream_partials` 表 | **可变**草稿：不产生 ordinal、不动 high_water，清理是 DELETE 无墓碑；`last_partial_seq` 是唯一版本号 |
| `pending_interactions` 表（C03） | 待处理交互的持久视图：**无 ordinal** |
| `agent.py:1635-1704`（`_refresh_provider_context_from_canonical`） | 同构先例：`current_high_water < cursor.high_water` → 冷重建；否则 `read_event_records(after_ordinal=cursor.high_water)` 读后缀 |

因此本 Change 只新增两件事：**有界 DTO 形状**与**原子快照 + 可续传订阅**。

## Goals / Non-Goals

**Goals:**

- 给非终端消费者一个**有界**的会话视图：消息以引用+分页给出，正文不内联；状态部分小而稳定。
- 让"取快照"与"开始收增量"之间**不留缺口**：边界之前的数据在快照里，边界之后的数据在流里，重复可幂等去重。
- 让订阅可**续传**（缓冲仍覆盖游标）或在不可续传时**明确过期**，而不是静默丢事件。
- 让慢消费者**有界失败**：超限发恰好一次 `resync_required` 并只关闭该订阅。
- 全程只读：订阅路径不得写 canonical、不得触发模型或工具、不得持有执行权。

**Non-Goals:**

- **不做 wire 格式**。DTO 是进程内对象；`subscription_id`/`host_epoch`/`transport_seq` 的编码、分帧与错误码映射属 C05。C04 只保证语义与边界，不定义 JSON schema。
- 不做 Electron、不做 UI 组件、不做 Windows 打包（C06/C07）。
- 不重写既有投影（`SessionProjection` / `CanonicalMetricsProjection` / `RunTraceProjection`），也不改 `agent.py`/`tools.py`/`runtime_store.py`/`run_lifecycle.py`。
- **不复用 `IncrementalModelReplayCursor`**：它是 run 作用域的**模型重放**游标，没有"游标失效/过期"语义，也不暴露 draft 版本。C04 自建 `GuiCursor`。
- 不新增持久化表：订阅是**进程内**状态，不落盘。进程重启后游标一律过期，客户端重新走原子快照。
- **不做跨进程/跨线程线性化**：本 Change 的订阅路径在单进程、单事件循环内运行；跨进程边界属 C05。
- 不做历史分页与实时状态的耦合：加载旧历史不改变当前游标。

## Decisions

### D1：投影是只读派生，边界由 ordinal 承载，且必须显式声明

`GuiProjection.build(store, session_id=…, high_water=H)` 只在**不可变前缀** `ordinal <= H` 上构建，并返回带 `high_water`、`projection_version`、`source_digest` 的 `GuiSnapshot`。

- `projection_version` 是 C04 **自己的** `GUI_PROJECTION_VERSION = "gui-projection-v1"`，不是全局 `projection-v1`。理由：DTO 形状会独立演进，兼容判据必须能独立移动。
- `high_water` 字段本身没有鉴别力（`SessionProjection.project(..., high_water=99)` 会原样回显 99），所以**判据必须是内容**：快照的 `source_digest` 与消息序列必须等于调用方用 `iter_event_records(store, high_water=H)` + `source_digest` 独立复算的结果。验证见 spec 的"不留缺口"等式。

**考虑过的替代：让投影自己反复查库取"最新值"。** 会导致快照内部自相矛盾且不可复算，不采用。

### D2：字段分三类，禁止把非前缀事实冒充前缀事实

| 类别 | 字段 | 边界 |
| --- | --- | --- |
| ① 前缀字段 | `messages`、`terminals`、`errors`、`source_digest` | `ordinal <= high_water`，由 store 复算 |
| ② 非前缀字段 | `runs`（C03 控制库）、`drafts`（`runtime_stream_partials`）、`pending_interactions`（C03 控制库） | **无 ordinal、无前缀边界**；读取时刻的最新可变值，逐条带 `prefix_boundary_exempt: true` |
| ③ | 任何把②当①用的表达 | 禁止 |

快照另带 `last_partial_seq`（该时刻草稿的最大 `last_partial_seq`）与 `read_at_ms`，让消费者能说明②是"什么时候读的"。

**`runs` 归入②，不归①（2026-09-14 收口）。** 初版把 `runs` 写成前缀字段，与 §D8 的"控制库只读依赖"直接冲突：run 状态存于 `control.sqlite`，**没有 ordinal**，无法由 `ordinal <= high_water` 派生。后果已在独立审查中实测到：同一 `high_water`、同一 `source_digest` 的两份快照给出 `running` 与 `succeeded` 两个状态，而该 run 的终态事件并不在这两份快照内；`GuiSnapshot.to_dict()` 同时把 `runs` 列进 `prefix_fields` 又给它打 `prefix_boundary_exempt: true`——同一 DTO 自称两类。**这正是 D2 要禁止的失败模式，而初版 spec 并未收编它。** 修正后①只剩四个真正由前缀派生（且全部进 `source_digest`）的字段。

理由：草稿不产生 ordinal、也不动 `high_water`，清理是 DELETE 无墓碑；run 状态与待处理交互同样无 ordinal。把它们说成"某个前缀的一部分"是错的——而 GUI 最容易出的错正是把不同时刻读到的值拼成一张"当时"的图。把两类事实分开标注，消费者才能判断哪些字段是同一时刻的。

### D3：交付路径是 ordinal 拉取式；不留缺口与锁无关

订阅的事实源只有两条可选路径：

- **(a) 轮询 + `after_ordinal`（默认，本 Change 实现）**：`H = store.high_water(session_id=…)` → 在 `H` 上建快照 → 登记 `last_ordinal = H` → 循环 `store.read_event_records(session_id=…, after_ordinal=last_ordinal)` 并推进 `last_ordinal`；
- **(b) `OutputPort` 只作唤醒提示**：它无 ordinal、非全量、`emit_safely` 会吞异常，**不可作为事实源**。当前实现不使用它。

**明确禁止依赖任何未声明的写入侧钩子。** `SQLiteRuntimeStore.append` 经 `RuntimeEventEmitter.emit` 是纯转发：它不认识 `SubscriptionService`、不共享锁、没有 listener/notify。因此"注册订阅期间到达的变更进入缓冲"**没有 actor**，不能作为线性化依据。

**不留缺口由 ordinal 单调 + `after_ordinal` 左开区间保证，与锁无关**：写方提交后 `high_water` 单调不减；`after_ordinal=H` 是排他的，所以任何 `ordinal > H` 的事件都在后续某一轮后缀读取里出现，任何 `ordinal <= H` 的事件都在快照里。仓库同构先例：`agent.py:1635-1704`。

`asyncio.Lock` 只用于串行化**服务自身**的注册/裁剪/发布状态，不承担与写入方的线性化。

### D4：游标续传与过期是显式结果，判据三类

`resume(cursor)` 的过期判据（任一命中即 `cursor_expired` + 当前 high-water）：

1. **缓冲不覆盖**：游标边界早于该订阅仍可重放的边界（已被裁剪）；
2. **`projection_version` 不一致**：DTO 形状变了却不作废旧游标会让兼容判据失效；
3. **进程内服务实例更替**：`cursor.service_epoch != service.service_epoch`（进程重启/服务重建后游标一律过期）。

`GuiCursor` 至少含 `{subscription_id, session_id, high_water, projection_version, partial_versions}`，另带 `service_epoch`。`subscription_id` 是**进程内身份**，wire 编码属 C05。`resume` 返回 `GuiResumeResult(status, cursor, stream, error_code, current_high_water)`。

理由：静默跳事件比报错危险得多——GUI 会显示一个永远缺一段的对话。

### D5：有界失败；终态/交互/错误不被合并丢弃

每个订阅有一个按**未消费条目数**计的具名上限 `SUBSCRIPTION_BUFFER_LIMIT`（模块级常量）。到达上限后：

- 新条目替代**最旧的已排队**条目（不是中间、不是尾部），保证最新事实总是保留；
- **只合并**同一 `stream_key` 的草稿更新（后到的 partial 覆盖先到的同名版本，且不占新槽位）；
- 若订阅连续 `SUBSCRIPTION_STALL_GRACE_SECONDS` 没有消费任何东西，则向该订阅发**恰好一次** `resync_required` 并**关闭该订阅**，runtime 与其他订阅继续；
- 只要订阅在消费（哪怕很慢），时钟在每次取走消息时重置，**突发流量本身不会关闭订阅**。

理由：GUI 刷新慢是常态，但"因为客户端慢就让它错过一个审批请求"不可接受；同时"生产者太快"不该被当成"消费者卡死"。

### D6：草稿（partial）以版本替换，不以追加表达

`GuiDraft` 携带 `stream_key` + `revision` + `partial_seq`，消费者以**替换**语义处理同一 `stream_key`。

- `revision := StreamingPartialSnapshot.last_partial_seq`（该表**没有** revision 列，`last_partial_seq` 是唯一版本号）；
- `stream_key` 的来源：`ModelCallRecorder._stream_key` 写进事件的 `metadata.partial_stream_key`；`SQLiteRuntimeStore._partial_fields` 在该键缺失时用同一格式派生兜底。C04 侧 `derive_stream_key` 是 `runtime_lifecycle.py` 私有 `ModelCallRecorder._event_stream_key` 的**逐字镜像**（契约镜像，有漂移风险，测试**双向**断言）；
- **草稿变化不推动 `high_water`**，所以拉取式订阅必须把 `read_runtime_stream_partials(session_id=…)` **随增量轮询一并读取**，否则永远察觉不到草稿变化；
- `final` 清除草稿由写入侧在同一个 `append_event_and_clear_runtime_partials` 事务里 DELETE 该键的行；C04 只是在下一轮读到"行不在了"。

**允许跳号**：同一 `stream_key` 的中间 revision 可以不可见（被合并），因为 GUI 只需要最新草稿。spec 的"不得静默跳过任何事件"因此**限定于 canonical 事件**。

### D7：正文一律引用 + 分页，且页绑定边界

`GuiMessageRef` 只带 `message_id`/`ordinal`/`role`/`kind`/`summary`/`size`/`body_ref`。

- `size` 口径是 **canonical JSON 的 UTF-8 字节数**（`json.dumps(..., ensure_ascii=False, sort_keys=True).encode("utf-8")` 的长度）；
- `summary` 截断到 `SUMMARY_CHAR_LIMIT`；正文由 `GuiProjection.read_body(store, page_token)` 分页读取（在 C05 里映射为 `content.read`），单页上限 `BODY_PAGE_SIZE`；
- **页令牌必须携带快照边界**：`body_ref`/`next_page_token` 内含 `session_id`、`high_water` 与该前缀的 `source_digest`；`read_body` 重新读取同一前缀并要求 digest 逐字相等，否则抛 `GuiPageTokenError`。否则在活动会话下 offset 分页会重复或跳过。

理由：一个工具结果可以有几十 KB；把它塞进每帧快照会让 GUI 的每次重连都变慢，而绝大多数帧只需要摘要。

### D8：存储连接由宿主注入

`SubscriptionService(store=…, control_store=…)` 由宿主显式注入 store 句柄（或 `store_factory`）。**不自开第二连接**：`runtime.sqlite` 未启用 WAL、连接 `busy_timeout=2000`，写事务期间第二个连接的读可能 `database is locked`。测试由测试自己构造并注入。

`control_store` 是 C03 的 `ControlStore`；C04 只调用其公开读方法（`run` / `runs_for_session` / `pending_for_run`），**只读依赖**，不改语义。

### D9：订阅不持有执行权，也不产生事实

`SubscriptionService` 不调用 `Agent`、不调用工具、不写 `runtime_store`。测试显式断言：静默 store 上，订阅与投影路径前后 canonical 的事件 id 集合、计数、`ordinal <= H` 前缀 digest 完全相等，且 `sha256(runtime.sqlite)` 不变（草稿与 canonical 在同一文件）。import 图守卫断言 `rollo/projections/*` 不 import `agent`/`tools`/`application`。

理由：把"渲染视图"和"运行"分开，是 C02 就已确立的边界；C04 不能因为方便就把它们重新混起来。

### D10：投递语义是 at-least-once

`subscribe` 返回带 `subscription_id` 的**异步迭代器** `GuiStream`；`unsubscribe(subscription_id)` 关闭它。

- 服务保证**不重发同一 ordinal**（后缀读是左开区间，`last_ordinal` 单调推进）；
- 消费者**仍可据 ordinal 幂等去重**：游标之后重连时，边界处的消息可能既在缓冲里又被后缀读到，去重是调用方的权利而非义务；
- 快照本身是第一条投递消息（`kind="snapshot"`），因此在投递面上"快照"与"增量"是有序的，不存在"先到增量后到快照"的歧义。

### D11：不引入可配置项

上限与截断长度都是**模块级具名常量**，没有构造参数、没有配置文件项。测试用 `monkeypatch` 把常量改小来触发边界，因此"有界"在测试里是**可复算的具体数值**，而不是"收到 resync 就算过"。

## Risks / Trade-offs

- **轮询延迟**：`POLL_INTERVAL_SECONDS` 是订阅的最小察觉延迟。C05 可以用 `OutputPort` 作**唤醒提示**把延迟压下来（提示只触发一次额外轮询，不携带事实）。
- **有界失败的丢弃语义**：超限时最旧的**已排队**条目会被最新条目替代。这是"有界"的必然代价，因此必须**恰好一次** `resync_required` 明说"重新走快照"，而不是静默。
- **草稿键镜像的漂移风险**：`derive_stream_key` 镜像的是 `runtime_lifecycle.py` 的私有方法。测试双向断言 `_event_stream_key`/`_stream_key` 与镜像一致，并在键格式变化时立即变红。
- **非前缀字段不是同一时刻**：`drafts`/`pending_interactions` 读的是调用时刻的可变状态。已用 `prefix_boundary_exempt` + `read_at_ms` 显式标注，消费者不得把它们与 `high_water` 混为一谈。
- **只读断言的构造窗口**：`Application.__init__` 会写 `control.sqlite`，所以任何文件哈希类断言都必须在构造完成后取基线，否则会把构造写入误判成订阅写入。

## Migration Plan

无数据迁移：本 Change 不新增表、不改 schema、不写 canonical。纯新增模块 + 测试 + 一个只读方法。既有 CLI/TUI 行为不受影响（它们不订阅）。

## Open Questions

- `summary` 的截断长度与分页大小取什么默认值？本 Change 取保守默认并用测试固定；若 C06 的真实渲染需要不同值，届时按具体需求调整，而不是现在预留配置。
- C05 的唤醒提示如何与本服务的轮询配合（提示合并、退避策略）？属 C05 范围，本 Change 只声明 `OutputPort` **不可**作为事实源。
