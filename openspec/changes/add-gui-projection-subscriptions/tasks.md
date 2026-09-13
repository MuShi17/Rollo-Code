## 1. C04 变更准备与证据身份

- [x] 1.1 记录 checkout、branch、HEAD、Python 版本、OpenSpec 版本与唯一 writer；以 `git status --short --branch`、`git rev-parse HEAD`、`python --version` 复算身份，并记录 C03 已知残余风险与本 Change 的 retained 判据
  - 实测（2026-09-13，主 Agent 复算）：branch `feat/c03-application-lifecycle`（与 `origin/` 同名分支同步）、HEAD `410ac7eb282191d478f449ca69850646bc48fa1c`、Python `3.13.13`（`D:\Anaconda\envs\py313`）、`openspec validate add-gui-projection-subscriptions --type change --strict` → `Change 'add-gui-projection-subscriptions' is valid`（exit 0）。唯一 writer = 本 Change 的实现子代理 + 主 Agent 复核；期间用户另在本工作树独立修复两个 CLI/UI 缺陷（见 5.1 判据修订）。
  - C03 已知残余风险（retained）：`graceful_exit`/`forced_exit` 是公开字段但无消费者；`tool_operations` 不再保留 invocation/turn 绑定。两者均与本 Change 无关，本 Change 不触碰 C03 的 canonical 语义。
- [x] 1.2 冻结 C03 交接物：Application API 的 `session.create/list`、`run.status`、`interaction.respond`、`runs_list` 语义与 canonical 事实源边界（`SQLiteRuntimeStore`、`RunContext` 身份）；以 C03 focused regression 验证本 Change 未改写这些语义
  - 以 C03 全部回归文件为证据（`test_c03_crash_recovery`、`test_c03_adversarial`、`test_c03_platform_oracles`、`test_c03_dispatch_evidence`、`test_c03_interaction_binding`、`test_c03_cli_failure_surface`、`test_application`、`test_execution`），全量 560 passed 中一并通过。
  - `Application` 的唯一改动是**纯新增** `runs_list(session_id)`（+19/-0，见 5.1）。
- [x] 1.3 冻结既有投影消费面：`SessionProjection`、`CanonicalMetricsProjection`、`RunTraceProjection` 的字段与 `high_water` 约定；以现有投影测试复跑验证不改写。
  - 既有投影测试文件全部未修改且在全量中通过（`test_archive_projection`、`test_incremental_replay` 等）。
  - 明确记录：本 Change **不复用** `projections/incremental_replay.py` 的 `IncrementalModelReplayCursor`（仓库里没有名为 `IncrementalReplayCursor` 的类）。它是 run 作用域的模型重放游标，既没有游标失效/过期语义，也不暴露 draft 版本；`source_high_water_regressed` 只是 `agent.py` 的本地字符串，不是游标 API。C04 因此自建 `GuiCursor`，只复用 `runtime_store` 的 ordinal 边界与 `after_ordinal` 左开后缀读取。
- [x] 1.4 冻结 GUI DTO 形状、具名常量（`SNAPSHOT_MESSAGE_PAGE_LIMIT` / `SUMMARY_CHAR_LIMIT` / `BODY_PAGE_SIZE` / `SUBSCRIPTION_BUFFER_LIMIT` / `SUBSCRIPTION_STALL_GRACE_SECONDS`）、草稿版本语义与投递面（`snapshot`/`subscribe`/`resume`/`unsubscribe` 的返回类型）；以本轮 spec 的每个 MUST 至少一条 oracle 验证
  - 常量实测：`200` / `160` / `4096` / `256` / `0.25`；由 `test_default_bounds_are_finite_and_enforced` 以**硬编码数值**（而非常量名）断言有限且生效；`SUBSCRIPTION_BUFFER_LIMIT` 与 `SUBSCRIPTION_STALL_GRACE_SECONDS` 另由 bound 三文件的 patch 值间接覆盖。
  - 投递面返回类型：`GuiSnapshotResult` / `GuiStream` / `GuiResumeResult` / `GuiUnsubscribeResult`（均为 frozen dataclass，`test_c04_gui_subscriptions.py` 逐项断言）。
  - 每个 MUST 的 oracle 见 §4 分层矩阵（30 条）。
- [x] 1.5 建立证据分层：纯逻辑/替身（L1）与真实 `SQLiteRuntimeStore` 前缀（L2）与真实 run（L3）；以证据 manifest 验证每条测试标注 actor 类型与可复算命令。
  - L1 = `test_c04_gui_subscription_bound.py` 全部 10 条（`ScriptedStore` 替身 / 控制面替身）；L2 = `test_c04_gui_projection.py` 8 条 + `test_c04_gui_subscriptions.py` 多数（真实 `SQLiteRuntimeStore`）；L3 = `test_unsubscribe_does_not_stop_the_run_and_replay_reads_the_terminal`（真实 `Application` + 真实 run）。各文件 docstring 已标注层归属与理由。
  - 分层归属（冻结）：**必须 L2 真实 store** = 边界内容断言（用 `iter_event_records`+`source_digest` 独立复算）、跨 session 隔离、只读（含 `sha256(runtime.sqlite)`）、草稿版本与 final 清草稿、`after_ordinal` 差一（`projections/base.py` 会吞 `TypeError` 回退，假 store 会静默吸收该错误）；**必须 L1 插桩 store** = 注册期/读取期交错注入与有界失败（缓冲上限是服务自身状态）；**必须真实 run** = 退订不影响运行。本 Change **不需要**真实多进程/多线程证据（订阅路径单进程单事件循环，"不留缺口"由 ordinal 与左开后区间保证，与锁无关）。
- [x] 1.6 完成独立设计审查与测试策略审查，逐条核对 P0/P1 缺口；以两份带 `result_identity` 的审查报告与主 Agent 复核记录验证 D(C04) 前置
  - 两份**互不通气**的独立审查：`C04 设计审查`（openspec-designer）与 `c04-test-strategy-review-2026-09-13`（test-strategy-agent），各自独立复现同样的两个根本缺陷（无变更源、单不可变前缀覆盖不到 3/4 DTO 区段），结论 `REVISION_REQUIRED`。完整结论见批次总览 §15.5.1；12 条 delta 已全部落入工件与实现。

## 2. 有界 GUI 投影

- [x] 2.1 新增 `src/rollo/projections/gui_projection.py` 的 DTO（消息引用、run 状态、待处理交互、草稿）与 `GuiProjection.build(...)`，要求显式 `session_id` 与 `high_water`，返回 `projection_version`（自有 `GUI_PROJECTION_VERSION`）/`source_digest`，并显式区分前缀字段与非前缀字段（`prefix_boundary_exempt`）；以"快照内容等于在 H 上的独立复算"与"边界之后的事件不进入快照"测试验证
  - `test_snapshot_carries_the_prefix_boundary_that_was_asked_for`（独立复算 digest + 消息序列）、`test_snapshot_excludes_events_written_while_it_is_building`（构建期提交不进入快照）、`test_projection_version_is_its_own_namespace`（自有 `gui-projection-v1`）。
  - `PREFIX_FIELDS` = messages/terminals/errors/source_digest；`NON_PREFIX_FIELDS` = **runs**/drafts/pending_interactions（`runs` 于 2026-09-14 从①移入②，理由见 design D2 与 §4.4 后的收口记录）。豁免标记由 `test_non_prefix_facts_are_marked_as_such_in_the_dto` 对 run/pending 断言、`test_draft_replaces_by_revision_and_final_clears_it` 对 draft 断言。
- [x] 2.2 实现消息引用的摘要截断与 `body_ref` 分页读取；以"大正文不内联 + `size` 为真实 canonical JSON 字节数 + 逐页完整覆盖不重复不缺口"测试验证
  - `test_large_body_is_paged_and_never_inlined`（不内联、`size` 等于 canonical JSON UTF-8 字节数、逐页还原、令牌篡改必须抛 `GuiPageTokenError`）、`test_pages_cover_the_boundary_without_repeats_or_gaps`（覆盖、无重复、令牌绑定边界）。
- [x] 2.3 实现 run 状态与待处理交互的只读装配（复用 C03 控制记录，不新增表）；以 run 终态与 pending 交互在快照中可见、且带 `prefix_boundary_exempt: true` 的测试验证
  - `test_non_prefix_facts_are_marked_as_such_in_the_dto`（run 与 pending 均 `prefix_boundary_exempt: true`，且同一快照的有界半边不冒充豁免）、`test_a_pending_interaction_is_also_carried_by_the_snapshot`、`test_a_pending_interaction_with_a_tool_name_is_delivered`。
  - **本轮修复的真实缺陷**：`_read_runs` 构造的 run DTO 原本**缺** `prefix_boundary_exempt`，消费者的 `dict.get()` 得到 `None` —— 违反本任务与 design D2。已补该字段并加断言。
  - 无新表（`git diff` 对 `runtime_store.py`/控制库 schema 为零）。
- [x] 2.4 实现草稿装配（`stream_key` + `revision := last_partial_seq` + `partial_seq`）与 `derive_stream_key` 契约镜像；以版本严格递增、final 清除草稿、以及与 `ModelCallRecorder._event_stream_key`/`_stream_key` 的**双向**断言验证
  - `test_draft_stream_key_mirrors_the_recorder`（双向：镜像 == recorder 写的键 == store 落库的键；无显式 key 的回退分支也覆盖）、`test_draft_replaces_by_revision_and_final_clears_it`（真实 recorder 驱动、revision 1→2、final 清除草稿行）、`test_draft_revision_at_the_bound_replaces_instead_of_appending`。
- [x] 2.5 以"投影前后 canonical 事件 id 集合、计数与前缀 digest 不变 + `sha256(runtime.sqlite)` 不变"验证投影只读边界
  - `test_subscription_path_writes_nothing`：静默 store 前后比对事件 id 集合、计数、`ordinal <= H` 前缀 digest，并加 `sha256(runtime.sqlite)` 文件哈希（因为草稿写入对前三者**全不可见**，只有文件哈希能见证）。M15 据此被明确拒绝。
- [x] 2.6 以 import 图守卫断言 `rollo/projections/*` 不 import `agent`/`tools`/`application`
  - `test_projections_do_not_import_the_agent_or_tools`。**已如实记录限度**：`rollo/__init__.py` 会 eager import `Application`，故 `rollo.application` 无法进入该守卫；守卫断言 `rollo.agent`/`rollo.tools` 未被引入，已在 design D9 与测试 docstring 写明。

## 3. 原子快照与可续传订阅

- [x] 3.1 新增 `src/rollo/projections/subscriptions.py` 的 `SubscriptionService`（存储句柄/工厂由宿主注入）、`GuiCursor`、`GuiStream` 异步迭代器投递面、`subscribe`/`snapshot`/`resume`/`unsubscribe`；以"快照为第一条消息"与"同一 session 多订阅互不影响"测试验证
  - 构造时 `store` 与 `store_factory` 必须注入其一，否则抛 `SubscriptionError`（不自开第二连接：`runtime.sqlite` 非 WAL、`busy_timeout=2000`）。
  - `test_multiple_concurrent_subscriptions_are_allowed`（同 session 3 个订阅互不影响）、各投递面用例均断言快照是第一条消息。
- [x] 3.2 实现 ordinal 拉取式增量：`read_event_records(session_id=…, after_ordinal=last)`，推进 `last_ordinal`；服务保证不重发同一 ordinal，消费者仍可据 ordinal 幂等去重；以重复读取不产生重复条目、且交错提交（快照与首次后缀读取之间）不丢事件的测试验证
  - `test_event_committed_between_snapshot_and_first_suffix_read_is_not_lost`（L1 插桩：在 `read_event_records` 内部提交 E2 → 必须不丢）、`test_no_gap_equation_between_snapshot_and_event_suffix`（不留缺口等式）。`after_ordinal` 为左开后区间，M11 差一变异使 7 条变红。
- [x] 3.3 实现 `resume(cursor)`：缓冲覆盖 + `projection_version` 一致 + `service_epoch` 一致时续传，否则返回 `cursor_expired` 与当前 high-water；以正反两向（含三类过期判据）测试验证
  - `test_resume_continues_when_the_buffer_covers_the_cursor`（正向）、`test_cursor_expired_on_every_declared_criterion`（三类判据：缓冲不覆盖 / `projection_version` 不一致 / `service_instance_replaced`，另含 `subscription_unknown`）。
- [x] 3.4 实现有界失败：`SUBSCRIPTION_BUFFER_LIMIT` 处新条目替代最旧已排队条目、只合并同一 `stream_key` 草稿、连续 `SUBSCRIPTION_STALL_GRACE_SECONDS` 未消费时发**恰好一次** `resync_required` 并关闭该订阅；以"溢出只影响该订阅、另一订阅与 runtime 继续"、"消费中的订阅不被突发关闭"、"终态/交互不被合并丢弃"三条测试验证
  - `test_stalled_subscriber_is_closed_once_with_the_backlog_intact`（恰好一次 + 不重发）、`test_a_new_entry_replaces_the_oldest_queued_one`（幸存者恒等于 `[3,4]`，直接调用 `_enqueue` 并中和 `_drain`/停 pump）、`test_reading_subscriber_is_never_closed_by_a_burst`、`test_overflow_leaves_other_subscriptions_open`、`test_terminal_and_interaction_survive_a_full_backlog`、`test_stall_clock_uses_the_declared_grace_period`。
  - **口径说明**：spec 对终态的要求是析取的（"两者都仍被投递 **或** 以 `resync_required` 明确关闭"），故"替代最旧"与"恰好一次通知"是可断言的部分，"终态永不被替代"不可断言。
- [x] 3.5 实现草稿随增量轮询一并读取（`read_runtime_stream_partials(session_id=…)`），因为草稿不推动 `high_water`；以"只发生草稿变化时边界不变但草稿仍到达"测试验证
  - `test_a_draft_advances_the_draft_version_but_not_the_boundary`：同时覆盖**两条**能移动水位而不投递可见事件的机制 —— 可变草稿表（无 ordinal）与后缀读取中的 `partial=True` 记录（有 ordinal、会推进去重水位但**不得**推进已投递边界）。
  - **本轮修复的真实缺陷**：`_poll_events` 中 `high_water` 的自增原本位于 `if event.partial` **之前**，于是一条 partial 记录把对外边界从 2 抬到 999 而快照 H=2，直接违反本任务。已分离 `last_ordinal`（去重水位，每条都前进，否则反复重读）与 `high_water`（已投递边界，仅非 partial 推进）。
- [x] 3.6 以"退订后 run 仍到达终态、重订可读到终态"验证订阅不持有执行权
  - `test_unsubscribe_does_not_stop_the_run_and_replay_reads_the_terminal`（L3：真实 `Application` + 真实 run；退订后 run 仍 `succeeded`，重订快照读到终态）。

## 4. 分层测试与验收

- [x] 4.1 新增 GUI 投影 focused tests（L2），覆盖边界内容等式、跨 session 隔离、分页覆盖与页令牌绑定、摘要截断与字节数、草稿版本、只读断言
  - `test_c04_gui_projection.py` 8 条（全 L2）：边界等式、构建期提交排除、跨 session 隔离（`test_snapshot_contains_only_the_requested_session`）、分页覆盖与令牌绑定、大正文分页与字节数、常量有限性、不留缺口等式、版本命名空间。
- [x] 4.2 新增订阅 focused tests（L2 + L1），覆盖投递面、不留缺口等式、去重、续传、过期正反、有界失败、草稿替换、退订不影响运行
  - `test_c04_gui_subscriptions.py` 12 条（L2/L3）+ `test_c04_gui_subscription_bound.py` 10 条（L1），合计 30 条 = 22 条（实现交付）+ 8 条（主 Agent 复核轮补齐）。
- [x] 4.3 运行 C04 focused、C03 全量回归、`compileall` 与 `openspec validate add-gui-projection-subscriptions --type change --strict`；以分层结果而非单一全绿验证
  - C04 focused：**30 passed**。全量回归（主 Agent 独立重跑）：**560 passed / 11 warnings / 0 skipped / exit 0**（278s）。`compileall -q src/rollo` → exit 0。`openspec validate … --strict` → valid（exit 0）。冻结文件 `git diff --exit-code 410ac7e -- tools.py runtime_store.py run_lifecycle.py` → exit 0。
- [x] 4.4 对本轮新 oracle 做**变异验证**，逐条确认对应用例变红；无可鉴别力处如实报告。矩阵：

  | # | 变异 | 期望 | 实测 |
  | --- | --- | --- | --- |
  | M1 | 快照越过 `high_water` 去读最新值 | 边界内容用例红 | 红 `test_snapshot_excludes_events_written_while_it_is_building` |
  | M2 | 取消 `cursor_expired` 判定，静默从当前边界继续 | 过期用例红 | 红 `test_cursor_expired_on_every_declared_criterion` |
  | M3 | 溢出时静默丢弃**最新**条目 | 终态用例红 | 红（2 条） |
  | M4 | 订阅/投影路径写入 canonical | 只读用例红 | 红 `test_subscription_path_writes_nothing` +2 |
  | M5 | 草稿按追加而非替换 | 草稿版本用例红 | 红 `test_draft_revision_at_the_bound_replaces_instead_of_appending` |
  | M6 | 分页不绑定边界 | 分页覆盖/令牌用例红 | 红 `test_large_body_is_paged_and_never_inlined`（**首轮无鉴别力**，补"篡改令牌 digest 必须抛错"后变红） |
  | M7 | 去掉 `session_id` 过滤（改用全局 high-water） | 跨 session 隔离用例红 | 红 `test_snapshot_contains_only_the_requested_session` |
  | M8 | 三个上限常量改成 10⁹ | 有界性与分页用例红 | 红 `test_default_bounds_are_finite_and_enforced`（**首轮无鉴别力**，补硬编码数值断言后变红） |
  | M9 | 取消 `SUBSCRIPTION_STALL_GRACE_SECONDS`（`_stall_expired` 恒真） | 突发关闭用例红 | 红 `test_stall_clock_uses_the_declared_grace_period`（**首轮无鉴别力**，补正反两向时钟断言后变红） |
  | M10 | 有界失败时关闭**所有**订阅 | 另一订阅继续用例红 | 红 `test_overflow_leaves_other_subscriptions_open`（**首轮无鉴别力**，补两订阅并发后变红） |
  | M11 | `after_ordinal` 改成含端点（`- 1`，差一） | 不留缺口等式用例红 | 红（7 条） |
  | M12 | `resume` 不校验 `service_epoch` | 服务实例更替过期用例红 | 红 `test_cursor_expired_on_every_declared_criterion` |
  | M13 | `resume` 不校验 `projection_version` | 版本不一致过期用例红 | 红 同上 |
  | M14 | 草稿 revision 改用 `fragment_count` | 草稿版本用例红 | **等价变异，无鉴别力** —— 实测真实 recorder 逐片驱动 `last_partial_seq == fragment_count` 恒成立（1..4 同步，metadata 亦同步），两字段在全部可达路径不可区分。不采用弱化版本，如实记为等价 |
  | M15 | 投影只读断言退化为"收到的消息数不变" | 声明为**无鉴别力** | 确认为无鉴别力（草稿写入对 count/digest/high_water 全不可见），故只读判据固定为 id 集合 + 计数 + 前缀 digest + **`sha256(runtime.sqlite)`** |
  | M16 | `derive_stream_key` 漂移（改格式） | 契约镜像双向断言红 | 红 `test_draft_stream_key_mirrors_the_recorder` |

  **主 Agent 另跑的独立变异（不抄上表，含 5 个新变异）**：删除 `partial` 过滤 → 红；删除饱和时 `_trim_front` → 红；交互跳过 `tool_name` 非空项 → 红；撤销 `high_water` 修复 → 红（2 条）；删除 run 的 `prefix_boundary_exempt` → 红。**8/8 detected，阴性对照均绿。**
  **两次等价变异**（`min` 用 `qsize()+len(buffer)`、`_ensure_open` 重复判断）经查为不可达/死分支，如实记录。
  方法纪律：每个 harness 先跑**阴性对照**（未变异必须绿）；变异字符串按 CRLF 归一后匹配、回写恢复行尾。
- [x] 4.5 由独立子代理对实现做对抗性审查；以带 `result_identity` 的报告与主 Agent 逐条复核记录验证
  - 审查子代理 `5d31d765-bc0d-4c97-b326-ed1c3db7bea0`，报告 `adversarial-review.md`（51921 字节，含 `result_identity`）；41 条变异 DETECTED 26 / SURVIVED 15 / UNEXPECTED-RED 0；阴性对照 15 次全绿。审查未修改任何源码或测试（逐条字节还原，五个交付文件 SHA256 与冻结值逐字相同）。
  - **裁决**：8 条断言 CONFIRMED 6 / FALSIFIED 2（M14 记录矛盾、M3 归因）；6 个怀疑方向 CONFIRMED 5 / FALSIFIED 1（`_drain` 静默分支为死代码）。
  - **P0 = F1**（`runs` 违反前缀字段契约）已由用户裁决为"改为非前缀字段"并同步五处；**原 P1 终态被替代**已由用户裁决为"永不被替代"并实现 + 加 oracle。F2/F7/F8/F9 如实记为残余，F3/F5/F6 已补或已修。逐条处置见 `implementation-status.md` §11。
  - 主 Agent 另跑差分验证 **5/5 detected**（阴性对照绿）。

## 5. Gate、写回与交付边界

- [x] 5.1 确认本 Change **未修改** `agent.py`/`tools.py`/`runtime_store.py`/`run_lifecycle.py`；以 `git diff --exit-code 410ac7e -- <这四个文件>` 验证（注意：`git diff --stat` 对新模块输出为空，零判别力，不得作为越界判据），并断言 `git status --short -uall` 的文件集合等于允许集合（四个新模块/测试文件 + `application.py` + 本 change 的工件）。`application.py` 的唯一改动是 `Application.runs_list` 的**纯新增**；若发现做不到纯新增则停手并报告
  - **判据修订（2026-09-13）**：工作树里 `agent.py` 与 `__main__.py` **确有改动，但不是本 Change 引入的**——那是用户在同一工作树上单独修复的两个真实缺陷（思考文本双路径重复、CP936 控制台 `UnicodeEncodeError`），已由主 Agent 独立验证（批次总览 §15.5.4）。
  - 实测：`git diff 410ac7e -- src/rollo/agent.py` → **仅 −2 行**（两处 `self._emit_text(<思考文本>)`）；`__main__.py` → 仅 `_configure_stdio_encoding` 及其调用（+28，含 TTY 闸门）；`tools.py`/`runtime_store.py`/`run_lifecycle.py` → `--exit-code 0`。
  - `application.py` → `+19/-0`，唯一改动是纯新增 `Application.runs_list`（只读 `ControlStore.runs_for_session`，不改任何既有方法体）。
  - `git status --short -uall` 文件集合 = 允许集合（4 个新模块/测试文件 + `application.py` + 本 change 的 7 个工件 + 用户的两个修复文件 + `.gitignore`）。`src/rollo_code.egg-info/*` 的 `D` 是用户在 `.gitignore` 加 `*.egg-info/` 后 `git rm --cached` 的**取消跟踪**，磁盘文件仍在、`import rollo` 正常。
- [x] 5.2 冻结实现 diff（逐字 `git status` + 全量 SHA256），复核 `.gitignore`/`AGENTS.md` 等既有改动未被误纳入
  - 交付新模块 SHA256：`gui_projection.py`、`subscriptions.py`、三个 `test_c04_*.py`（见 `implementation-status.md` §2）。`.gitignore` 的两处改动（`*.egg-info/`、`docs/` 已不再忽略）为用户授权范围，非误纳入；`AGENTS.md` 未改动。
- [x] 5.3 将实现结果、命令/结果、残余风险、diff digest 与授权字段写回 `openspec/changes/add-gui-projection-subscriptions/implementation-status.md`（新建）
  - 已新建并写回，含 §1 证据身份、§2 交付物、§3 命令与结果、§4 变异矩阵、§5 新旧用例清单、§6 范围合规、§7 残余风险、**§8 主 Agent 独立复核**（阴性对照、11 变异、2 处测试遗漏更正、2 个等价变异、本轮两处真实修复与差分验证）。
- [x] 5.4 独立审查通过且主 Agent 接受后才报告 C04 完成；以最终 focused/全量回归、审查 `result_identity` 与 `remaining_gap=[]` 验证退出条件
  - 现状：focused **30 passed**、全量 **560 passed / exit 0**、`openspec validate --strict` valid、`compileall` exit 0、冻结文件合规、全部变异经阴性对照验证。**`remaining_gap` 仍含一项：4.5 的独立对抗性审查尚未执行**，故本任务保持未勾选、C04 不报告完成。
- [ ] 5.5 仅在用户另行授权后执行 commit/push/PR；以 Git 状态与授权字段验证本 Change 不自动触发交付动作
  - 尚未授权。HEAD 仍为 `410ac7e`，本 Change 的全部产物均未提交。

## 6. 对抗性审查（4.5）的裁决与处置（2026-09-14）

独立审查子代理完成，报告见 `adversarial-review.md`（51921 字节，含 `result_identity`）。41 条变异：**DETECTED 26 / SURVIVED 15**；8 条断言 CONFIRMED 6 / FALSIFIED 2；6 个怀疑方向 CONFIRMED 5 / FALSIFIED 1。

**审查对我的两处证伪成立**：

1. **M14 记录三处矛盾**：`implementation-status.md` 的表（"红"）、紧随其后的"**无鉴别力项：无**"、以及 §8.4（"等价变异"）互相冲突。已按 §8.4 与审查的三路径论证统一为**等价变异、无鉴别力**。
2. **M3 行未声明变异口径**却并列两条用例，暗示第一条有鉴别力。主 Agent 复现为 **4 条**红（审查报 1 条）——该归因对内部状态敏感，**两方都不完整**。已改写为只保留"该变异可检出"这一可靠结论，并写明口径。

**P0 / F1：`runs` 违反 spec 的前缀字段契约——已由用户裁决并收口。**

- 事实（主 Agent 独立复现）：同一 `high_water = 2`、同一 `source_digest` 的两份快照给出 `running` 与 `succeeded`，而该 run 的终态不在快照内；`to_dict()["prefix_fields"]` 含 `"runs"` 却又给它打 `prefix_boundary_exempt: true`——同一 DTO 自称两类。
- 根因：run 状态存于 C03 控制库、**没有 ordinal**，物理上不可能前缀化。这正是 1.6 记录的两份独立设计审查给出的 P0（"单不可变前缀覆盖不到 3/4 DTO 区段"），**进了 design D2 却没有在 spec 收编**。
- 用户裁决：**把 `runs` 改为非前缀字段**。已同步五处：`specs/gui-projection/spec.md`（①只剩 messages/terminals/errors/source_digest，②含 runs，并新增"run 状态被标注为非前缀"场景）、`design.md` D2、`proposal.md`、`gui_projection.py` 的 `PREFIX_FIELDS`/`NON_PREFIX_FIELDS`、本节 2.1。

**用户另裁决：终态与交互永不被替代（原 P1）。**

- 事实：饱和时 `_trim_front` 会替代**最旧的已排队**条目；若终态恰在队首，它会被替代（`resync_required` 仍会发，但终态确实消失一瞬）。
- 实现：`_trim_front` 改为只替代**可弃**条目（`_is_disposable`）；终态（`kind == "terminal"` 或事件载荷带 `actions.run_terminal`）、交互、以及三种关闭通知均受保护。`_drain` 的静默裁剪分支同样改走该路径（原先直接丢弃且可绕过保护）。全部受保护时**界为软目标**，宁可超限也不丢终态。
- oracle：`test_a_terminal_is_never_displaced_by_a_newcomer`（终态在**队首**——既有用例把它排在普通条目之后，从未真正到达该决策）、`test_an_all_protected_backlog_exceeds_the_bound_rather_than_losing_one`。
- **实现过程中主 Agent 写反了布尔谓词**（`not _is_disposable` 导致替代受保护项），被上表两条新增 oracle 立即抓红——**新 oracle 的第一批用户就是它自己的实现**。

**其余审查发现**：F2（"不留缺口等式"缺真正的等式 oracle）、F3（快照首条消息 `ordinal` 零断言，已补）、F5（15 条存活变异中 10 条为真实覆盖缺口，已补 `unsubscribe(未知 id)`/`detach` 两条）、F6（`_drain` 静默丢弃分支为死代码；饱和峰值实测 `LIMIT+1`）、F7（`GuiCursor.to_dict()` 丢 `service_epoch`，wire 属 C05）、F8（只读判据未按 spec 字面复算；审查以 SQL trace 补证 44 条语句全为 SELECT）、F9（一次不可复现的套件挂死，判 `UNVERIFIABLE`）、F10（`_finish` 非幂等，无害）——逐条记入 `implementation-status.md` §11，未修项如实列为残余风险。
