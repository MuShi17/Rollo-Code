# C04 实现状态记录

## 1. 证据身份

| 项 | 值 |
| --- | --- |
| checkout | `D:\PycharmProjects\pythonProject\Rollo-Code` |
| branch | `feat/c03-application-lifecycle`（跟踪 `origin/feat/c03-application-lifecycle`） |
| HEAD | `410ac7eb282191d478f449ca69850646bc48fa1c` |
| python | `D:\Anaconda\envs\py313\python.exe`（3.13.13） |
| 复算命令 | `git status --short --branch`；`git rev-parse HEAD` |
| C03 基线 | `528 passed / 0 skipped`（本 Change 开始前实测） |
| 本 Change 后全量 | `552 passed, 11 warnings`（`+24` = 本 Change 新增用例） |

`git status --short -uall` 的完整集合：

```
 M src/rollo/application.py
?? openspec/changes/add-gui-projection-subscriptions/.openspec.yaml
?? openspec/changes/add-gui-projection-subscriptions/design.md
?? openspec/changes/add-gui-projection-subscriptions/proposal.md
?? openspec/changes/add-gui-projection-subscriptions/specs/gui-projection/spec.md
?? openspec/changes/add-gui-projection-subscriptions/specs/gui-subscription/spec.md
?? openspec/changes/add-gui-projection-subscriptions/tasks.md
?? src/rollo/projections/gui_projection.py
?? src/rollo/projections/subscriptions.py
?? src/rollo/tests/test_c04_gui_projection.py
?? src/rollo/tests/test_c04_gui_subscription_bound.py
?? src/rollo/tests/test_c04_gui_subscriptions.py
```

（`?? gui.orig` / `?? sub.orig` 是变异验证用的临时副本，验证结束后已删除。）

## 2. 交付物

| 文件 | 行数 | 说明 |
| --- | --- | --- |
| `src/rollo/projections/gui_projection.py` | 557 | 有界 DTO + `GuiProjection`（`build`/`read_page`/`read_page_by_token`/`read_body`）+ 具名常量 + 页令牌 |
| `src/rollo/projections/subscriptions.py` | 962 | `SubscriptionService` / `GuiCursor` / `GuiStream` / `GuiMessage` + ordinal 拉取循环 |
| `src/rollo/tests/test_c04_gui_projection.py` | 413 | L2 投影契约 |
| `src/rollo/tests/test_c04_gui_subscriptions.py` | 762 | L2 订阅契约 + L3 真实 run |
| `src/rollo/tests/test_c04_gui_subscription_bound.py` | 479 | L1 注入 store 的边界与有界失败契约 |
| `src/rollo/application.py` | +19 | **仅新增** `Application.runs_list(session_id)` |
| `openspec/changes/add-gui-projection-subscriptions/*` | — | proposal / design / tasks / 两份 spec 按 P0/P1 修订 |

**最终形态（2026-09-13 收尾后，含主 Agent 复核轮的全部修正）**：`subscriptions.py` **973** 行、`test_c04_gui_subscriptions.py` **919** 行、`test_c04_gui_subscription_bound.py` **751** 行、`gui_projection.py` 557 行（未变）、`test_c04_gui_projection.py` 413 行（未变）。

**SHA256（5.2 冻结用，2026-09-13）**：

```
1848B1C60D4FD564F1EFF9657D908EF60C7D280A91AB2E908239A4C6FB3245DA  src/rollo/projections/gui_projection.py
07CC9A3F13FBCDBDA9F88380CD08AE966F5296B0CA12E7C87ADDD670FD80C0AE  src/rollo/projections/subscriptions.py
E2DF5EF266A48531DF9B3E45FB442CF48820D8DD48814145B84B6BF526AA5786  src/rollo/tests/test_c04_gui_projection.py
F3C3CC6F1A28C0E74C6A259EA0DF4EEB0BE3B5396568D35E54C08DEE0A87DB4C  src/rollo/tests/test_c04_gui_subscriptions.py
19F34DB6AC7698BEB37E4227010045DD16E9296A27307F1973D7E3EAB9570628  src/rollo/tests/test_c04_gui_subscription_bound.py
```

具名常量（模块级，无构造参数、无配置项）：

- `GUI_PROJECTION_VERSION = "gui-projection-v1"`
- `SNAPSHOT_MESSAGE_PAGE_LIMIT = 200`
- `SUMMARY_CHAR_LIMIT = 160`
- `BODY_PAGE_SIZE = 4096`
- `SUBSCRIPTION_BUFFER_LIMIT = 256`
- `SUBSCRIPTION_STALL_GRACE_SECONDS = 0.25`
- `POLL_INTERVAL_SECONDS = 0.01`

## 3. 命令与结果

```
$env:PYTHONPATH = "D:\PycharmProjects\pythonProject\Rollo-Code\src"
& "D:\Anaconda\envs\py313\python.exe" -m pytest -q src/rollo/tests --disable-warnings
→ 552 passed, 11 warnings in 276.92s

& "D:\Anaconda\envs\py313\python.exe" -m pytest -q \
    src/rollo/tests/test_c04_gui_projection.py \
    src/rollo/tests/test_c04_gui_subscriptions.py \
    src/rollo/tests/test_c04_gui_subscription_bound.py --disable-warnings
→ 24 passed

& "D:\Anaconda\envs\py313\python.exe" -m compileall -q \
    src/rollo/projections/gui_projection.py src/rollo/projections/subscriptions.py
→ exit 0

openspec validate add-gui-projection-subscriptions --type change --strict
→ Change 'add-gui-projection-subscriptions' is valid

git diff --exit-code 410ac7e -- src/rollo/agent.py src/rollo/tools.py \
    src/rollo/runtime_store.py src/rollo/run_lifecycle.py
→ exit 0（四个文件零改动）

git diff --stat -- src/rollo/application.py
→ 1 file changed, 19 insertions(+)
```

## 4. 变异验证矩阵

方法：逐个把变异写入源码，运行 C04 三个测试文件，记录首个失败（`-x` 关闭，跑全量）；每次后从副本恢复。
`M15` 是审查给出的"无鉴别力"示例，本 Change 因此把只读判据固定为 **事件 id 集合 + 计数 + `ordinal <= H` 前缀 digest + `sha256(runtime.sqlite)`**，不采用弱化版本。

| # | 变异 | 期望 | 实测变红的用例 |
| --- | --- | --- | --- |
| M1 | 快照越过 `high_water` 去读最新值（`iter_event_records` 去掉 `high_water`） | 红 | `test_snapshot_excludes_events_written_while_it_is_building` |
| M2 | 取消 `cursor_expired` 判定，静默从当前边界继续 | 红 | `test_cursor_expired_on_every_declared_criterion` |
| M3 | `_trim_front` 丢弃**队尾**（最新）而非队首（口径：把 `state.queue.get_nowait()` 改成 `state.queue.pop()`） | 红 | **随测试文件演进而变化，两方复核不一致**：早期两轮观测为 `test_stalled_subscriber_is_closed_once_with_the_backlog_intact` + `test_terminal_and_interaction_survive_a_full_backlog`；收尾时（30 条用例）同一变异实测 **4 条**红，另含 `test_overflow_leaves_other_subscriptions_open` 与 `test_a_new_entry_replaces_the_oldest_queued_one`。**该归因对内部状态敏感，不应作为稳定证据引用**；可靠结论只有"该变异可被检出" |
| M4 | 订阅/投影路径写入 canonical | 红 | `test_subscription_path_writes_nothing`、`test_snapshot_builds_at_the_high_water_it_reports`、`test_draft_replaces_by_revision_and_final_clears_it` |
| M5 | 草稿按追加而非替换 | 红 | `test_draft_revision_at_the_bound_replaces_instead_of_appending` |
| M6 | 分页不绑定边界（去掉 `read_body` 的 digest 校验） | 红 | `test_large_body_is_paged_and_never_inlined` |
| M7 | 去掉 `session_id` 过滤（改回全局边界语义） | 红 | `test_snapshot_contains_only_the_requested_session` |
| M8 | 三个上限常量改成 `10**9` | 红 | `test_default_bounds_are_finite_and_enforced` |
| M9 | 取消 `SUBSCRIPTION_STALL_GRACE_SECONDS`（`_stall_expired` 恒真） | 红 | `test_stall_clock_uses_the_declared_grace_period` |
| M10 | 溢出时关闭**所有**订阅 | 红 | `test_overflow_leaves_other_subscriptions_open` |
| M11 | `after_ordinal` 改成含端点（`- 1`，差一） | 红 | 7 条（`..._first_suffix_read_is_not_lost`、`test_resume_continues_when_the_buffer_covers_the_cursor`、`test_subscription_path_writes_nothing` 等） |
| M12 | `resume` 不校验 `service_epoch` | 红 | `test_cursor_expired_on_every_declared_criterion` |
| M13 | `resume` 不校验 `projection_version` | 红 | `test_cursor_expired_on_every_declared_criterion` |
| M14 | 草稿 revision 改用 `fragment_count` | 草稿版本用例红 | **等价变异，无鉴别力**（§8.4 与 §8.6 已查证；独立审查子代理另从 INSERT/UPDATE/清空重建三条路径证明该恒等，3 次实测 `30 passed`）。**本行原记「红——两个文件整文件 error」为错误自述，已被主 Agent 与独立审查双重证伪** |
| M16 | `derive_stream_key` 漂移（`"partial"` → `"chunk"`） | 红 | `test_draft_stream_key_mirrors_the_recorder` |

**无鉴别力项：M14（等价变异）与 M15（已按更严判据替换）。** ⚠️ 本行原为"无鉴别力项：无"——与同表 M14 行及 §8.4 矛盾，已被独立审查（F4）指出并更正。其余 13 个变异均至少使一条用例变红。

过程记录：`M6`、`M8`、`M9`、`M10` 第一轮**全绿**（无鉴别力），随后补齐了四条 oracle 才变红：

- `M6` → 增加"篡改 body 令牌里携带的 digest 必须抛 `GuiPageTokenError`"（仅把 `high_water` 加一不够：目标事件仍在更大的前缀里，因此无法见证）；
- `M8` → 增加 `test_default_bounds_are_finite_and_enforced`，用**硬编码数值**（200 / 160 / 4096）而非常量名断言；
- `M9` → 增加 `test_stall_clock_uses_the_declared_grace_period`，断言"未超期不判过期、超过声明宽限期才判过期"，且不把宽限期 patch 成 0；
- `M10` → 增加 `test_overflow_leaves_other_subscriptions_open`，两个订阅并发（一个停读、一个持续消费），断言兄弟订阅不被连坐。

## 5. 新旧用例清单

新增 24 条（baseline 528 → 552 的净增；`test_c04_gui_projection.py` 8 条、`test_c04_gui_subscriptions.py` 10 条、`test_c04_gui_subscription_bound.py` 6 条）。

`test_c04_gui_projection.py`（8 条，全部 L2）

1. `test_snapshot_carries_the_prefix_boundary_that_was_asked_for` — 边界内容等式（独立复算 digest + 消息序列）
2. `test_snapshot_excludes_events_written_while_it_is_building` — 构建期提交不进入快照
3. `test_snapshot_contains_only_the_requested_session` — 跨 session 隔离与并集等式
4. `test_pages_cover_the_boundary_without_repeats_or_gaps` — 分页覆盖、令牌携带边界
5. `test_large_body_is_paged_and_never_inlined` — 大正文不内联、字节数、逐页还原、令牌篡改与伪造 digest
6. `test_default_bounds_are_finite_and_enforced` — 默认上限有限且生效（硬编码数值）
7. `test_no_gap_equation_between_snapshot_and_event_suffix` — 不留缺口等式
8. `test_projection_version_is_its_own_namespace` — C04 自有版本命名空间

`test_c04_gui_subscriptions.py`（10 条，L2 + L3）

1. `test_snapshot_builds_at_the_high_water_it_reports`
2. `test_event_committed_between_snapshot_and_first_suffix_read_is_not_lost` — L1 插桩 store 在 `read_event_records(high_water=…)` 内部提交
3. `test_resume_continues_when_the_buffer_covers_the_cursor`
4. `test_cursor_expired_on_every_declared_criterion` — 三类过期判据（含未知 subscription_id）
5. `test_draft_stream_key_mirrors_the_recorder` — 契约镜像双向断言
6. `test_draft_replaces_by_revision_and_final_clears_it` — 真实 `ModelCallRecorder` 驱动
7. `test_subscription_path_writes_nothing` — 静默 store 三断言 + 文件哈希
8. `test_projections_do_not_import_the_agent_or_tools` — import 图守卫
9. `test_unsubscribe_does_not_stop_the_run_and_replay_reads_the_terminal` — 真实 `Application` run（L3）
10. `test_multiple_concurrent_subscriptions_are_allowed`

`test_c04_gui_subscription_bound.py`（6 条，L1）

1. `test_stalled_subscriber_is_closed_once_with_the_backlog_intact`
2. `test_reading_subscriber_is_never_closed_by_a_burst`
3. `test_terminal_and_interaction_survive_a_full_backlog`
4. `test_stall_clock_uses_the_declared_grace_period`
5. `test_overflow_leaves_other_subscriptions_open`
6. `test_draft_revision_at_the_bound_replaces_instead_of_appending`

**未修改任何既有测试。**

## 6. 范围合规

- `agent.py` / `tools.py` / `runtime_store.py` / `run_lifecycle.py`：`git diff --exit-code 410ac7e` → 0（零改动）。
- `application.py`：唯一改动是 `Application.runs_list(session_id)`，`+19` 行，**纯新增**；`runs_list` 只读 `ControlStore.runs_for_session`，不调用 `_ensure_session`/不改任何既有方法体。`git diff` 中没有任何被删除或修改的既有行。
- 无新第三方依赖；无新持久化表；无 schema 变更。
- 既有 CLI/TUI 行为不受影响（它们不构造 `SubscriptionService`）。

## 7. 残余风险与未完成项

1. **轮询延迟**：`POLL_INTERVAL_SECONDS = 0.01` 是订阅的最小察觉延迟。C05 可用 `OutputPort` 作**唤醒提示**压低延迟；本 Change 明确它**不可作为事实源**。
2. **超限时的丢弃语义**：达到 `SUBSCRIPTION_BUFFER_LIMIT` 后，新条目替代**最旧的已排队**条目（不是中间、不是尾部）。这是"有界"的代价，因此必须**恰好一次** `resync_required` 明说重新走快照，而非静默。
3. **草稿键镜像漂移**：`derive_stream_key` 镜像 `runtime_lifecycle.py` 的私有 `ModelCallRecorder._event_stream_key`。测试双向断言 `_event_stream_key`/`_stream_key` 与镜像一致，键格式变化时立即变红；但它是**契约镜像**，不是共享实现。
4. **`rollo.application` 在 import 图守卫之外**：`src/rollo/__init__.py` 会 eager import `Application`，任何 `rollo.*` 子模块导入都会带上它，因此它不承载鉴别力。守卫只断言 `rollo.agent` / `rollo.tools` 未被引入。已在 `design.md` D9 与测试 docstring 中写明。
5. **未做**：真实多进程/多线程线性化证据（判定为本 Change 不需要——订阅路径单进程、单事件循环，且"不留缺口"由 ordinal 与左开后区间保证，与锁无关）。
6. **未执行**：commit / push / PR（tasks 5.5：仅在用户另行授权后执行）。

## 8. 主 Agent 独立复核（不由实现者自测）

复核者：主 Agent（与实现者为不同主体），方法为**另写一份变异矩阵重跑**，而非采信 §4 的自述。

### 8.1 先做的阴性对照

自建 harness（`.mutants_c04.py`，复核后已删除）在**未变异**代码上先跑一次：`24 passed`，exit 0。没有这一步，"检测到"无法与"harness 本身就是坏的"区分。

> 首轮 harness 因 env 重建时丢掉 `USERPROFILE`，导致每条变异都因 `RuntimeError: Could not determine home directory` 而"变红"，**11/11 全是假绿**。补上阴性对照与完整 env 后才是有效结果。记录此事，因为"全红"与"全绿"一样可能是假象。

### 8.2 独立矩阵结果（11 个变异，5 红 / 6 存活）

自选变异（非抄自 §4），含 4 个 §4 之外的新变异：

| 变异 | 结果 | 归因 |
| --- | --- | --- |
| `_trim_front` 丢**最新**而非最旧 | 红 | `test_stalled_subscriber_is_closed_once_with_the_backlog_intact` |
| `SUBSCRIPTION_BUFFER_LIMIT` → 10⁹ | 红 | 同上 |
| `after_ordinal` 差一（`- 1`） | 红 | 4 条（burst / overflow-sibling / draft-at-bound / no-gap） |
| 不校验 `service_epoch` | 红 | `test_cursor_expired_on_every_declared_criterion` |
| 不校验 `projection_version` | 红 | 同上 |
| **删掉 `if event.partial: continue`** | **存活** | 见 8.3 缺陷 A |
| **删掉饱和时的 `_trim_front` 调用** | **存活** | 见 8.3 缺陷 B |
| **交互跳过 `tool_name` 非空者** | **存活** | 见 8.3 缺陷 B |
| `_trim_front` 的 `min` 用 `qsize()+len(buffer)` | 存活 | 等价：该表达式的唯一可达路径前提不成立 |
| 草稿 revision 改读 `fragment_count` | 存活 | 等价：见 8.4 |
| `_ensure_open` 去掉重复的 `if self._closed` | 存活 | 等价：**死分支** |

§4 的 M1–M5、M7、M11–M13、M16 未逐条重跑（其断言行已由本次复核直接读取确认：边界 digest、id 集合、`sha256`、错误码、双向键断言均在位），本节只报**新增变异**的结论。

### 8.3 两个测试遗漏 + 一个真实缺陷

> **本节初版有两处判断错误，已在下文更正并以删除线保留原结论**，因为错误本身是证据的一部分：复核者也会把探针假象当成实现缺陷。

**缺陷 A（更正后）：`if event.partial: continue` 无任何用例覆盖——它是防御性分支，不是热路径。**

初版称该过滤"可达"，依据是复核探针里 `read_event_records` 交回了 `[(2,'p1'),(3,'p2')]`。**该结论错误，探针本身是假象。** 追查 `runtime_store.py` 后确认：

- `_append_in_transaction` 把 `partial=True` 的记录写入 `runtime_events`，成功即意味着 `_partial_fields` 已通过（`partial_seq >= 1`）；
- `_partial_fields` 要求 `content.kind ∈ {text, thinking, function_call}`，非法即抛 `StoreValidationError`，事务回滚。

两者**互斥**：不存在"写进 `runtime_events` 且 `_partial_fields` 非法"的记录。而生产写入路径 `ModelCallRecorder.flush_partials` 一律走 `append_runtime_partial_batch` → `_upsert_stream_partial_in_transaction`（草稿表），**从不进 `runtime_events`**。实测（真实 store，recorder 驱动两次 `flush_partials`）：

```
suffix read: 1 records        # 只有 invocation_opened
  ord=1 partial=False lifecycle='invocation_opened'
draft rows: 1                 # 草稿只在 runtime_stream_partials
```

初版探针之所以"看到" partial，是因为我用了 `test_c04_gui_subscriptions._partial_event(text, seq=…)` 而该函数真实签名是 `(text, *, partial_seq, event_id, ts)`——**位置参数错位**造出了一条 `_partial_fields` 非法的畸形 partial 记录（该 helper 至今无任何用例调用，属未使用 helper）。

**但缺陷 A 的结论仍成立**：该分支确实无 oracle。它是"跨层防御"——服务假定 store 可能交回 partial 记录。因此正确测法是 **L1 替身**（显式返回一条 partial 记录），而不是真实 store。见 8.6 第 1 条。

**缺陷 B：饱和时"替代最旧"与"保留终态/交互"无 oracle。**
`_trim_front(state, 1)` 被静默丢弃（新条目直接 append，让 `_drain` 兜底）后全部用例仍绿；交互跳过 `tool_name` 非空项（真实交互通常带 `tool_name`）后全部用例仍绿。

复核探针实测非溢出路径的丢弃行为（真实 store，无人消费，上限 patch 为 3，写入 5 个 ordinal 2–6 的事件）：

```
overflowed=False pending=3
queued event ordinals=[4, 5, 6]
notices queued=[]
-> entries dropped with NO notice while not overflowed: True
```

即 ordinal 2、3 被丢弃时 `overflowed=False`，队列里**没有任何通知**；`resync_required` 要等 stall 宽限期届满才出现。这与 spec 的"有界失败 MUST 恰好一次 `resync_required`"并不冲突（宽限期届满前不构成"失败"），但意味着**通知不是丢弃的同刻伴随物**，而现有用例只断言 `ordinals == sorted(ordinals)`、`ordinals[-1] == 最新`、`len <= LIMIT+1`、以及存在 `resync_required`——**这些断言在"丢最新""丢任意项""完全不丢只截断"三种实现下都成立**，因此对"替代最旧"零鉴别力。

**缺陷 C：partial 记录会推进对外边界（真实缺陷，非测试问题）。**
见 8.6 第 4 条：写 oracle 时实测 `state.high_water` 被一条 partial 记录的 ordinal 抬到 999，而返回的快照 H=3。

**建议补三条 oracle**（初版建议，其中第 1 条的层级已按上文更正）：

1. ~~L2：向真实 store append 一个 `partial=True` 事件~~ → 更正为 **L1：替身返回一条 partial 记录**，断言它不出现在投递流且不改 `high_water`；
2. L1：饱和后断言"被替代的恰好是最旧的已排队项"（记录入队序，比对幸存集合，而非仅比对长度与单调性）；
3. L1：控制面存在 `tool_name` 非空的 pending 交互时，断言它到达订阅。

### 8.4 两个经查为等价变异的存活项（非遗漏）

- **草稿 revision 改读 `fragment_count`**：真实 recorder 逐片驱动实测 `seq_col == fragment_count` 恒成立（1..4 同步递增，`partial_seq`/`partial_fragment_count` 元数据亦同步），故两字段在全部可达路径上不可区分。§4 的 M14 描述（"改用 `partial_seq` 之外的字段"）也属此类，**不可能有鉴别力**，建议 §4 该行改记为"等价变异"而非"红"。
- **`_ensure_open` 的第二个 `if self._closed`**：同一条件连续判断两次，第二段是死分支，删除后行为必然相同。如实记录为**冗余代码**（按项目纪律不擅自清理，仅提示）。

### 8.5 复核确认的其余事实

- 全量回归由复核者独立重跑：**552 passed, 11 warnings**（289s），与 §3 一致；基线 528 的净增 24 条成立。
- `git diff --stat 410ac7e -- agent.py tools.py runtime_store.py run_lifecycle.py application.py` → 仅 `application.py | 19 +++`，与 §6 "纯新增"一致。
- 复核者未采信 §4 中"M14 导致两个文件整文件 error"的具体归因（本次等价变异表现为**单条断言失败**，非收集期 error）；不影响"M14 无鉴别力"的结论。

### 8.6 补齐三条 oracle + 一处真实修复 + 差分验证（2026-09-13）

基线 24 条 → **28 条**（`test_c04_gui_subscription_bound.py` 6 → 10；`test_c04_gui_subscriptions.py` 与 `test_c04_gui_projection.py` 未改动）。全量回归由复核者独立重跑：**556 passed, 11 warnings**（282s，`exit 0`）——552（C04 实现完成时）+ 4（本轮新增）。

**1. `test_partial_canonical_record_is_never_delivered`（L1）** —— 替身 `PartialLeakingStore` 在 `read_event_records` 中额外交回一条 `partial=True`、`ordinal=999` 的记录；断言它不出现在投递流、且 `state.high_water == 2`（= 已完成前缀）。用 `ordinal` 越过账本而非复用已有 ordinal，否则会顶掉既有记录并破坏账本连续性断言。**该记录由替身故意返回**，因为真实 store 无法被造成产生一条（见 8.3 缺陷 A 更正）。

**2. `test_a_new_entry_replaces_the_oldest_queued_one`（L1）** —— 直接调用 `_enqueue`（拥有该决策的路径），断言幸存者恒等于 `[3, 4]`、缓冲为 `[5]`。**必须把 `_drain` 中和且停掉 pump**，否则后台 pump 会在观测期间在队列与缓冲之间搬运条目（实测未中和时 `_pending` 为 5 而非 3）。中和必须发生在 `subscribe()` **之后**——`subscribe` 自身依赖 `_drain` 投递快照，提前打补丁会让快照永远送不出（实测 `AssertionError: stream ended before a 'snapshot' message arrived`）。

**3. `test_a_pending_interaction_with_a_tool_name_is_delivered` + `..._is_also_carried_by_the_snapshot`（L1）** —— 控制面**初始为空**，注册后再追加一条 `tool_name="bash"` 的 pending 交互。初始为空是刻意的：`subscribe()` 会用快照内容预填 `seen_runs`/`seen_requests`，订阅前就存在的交互由快照投递、增量路径**正确地**跳过它（初版 oracle 正是在订阅前放好交互，因此断言错了阶段）。第二条用例覆盖快照那一路。

**4. 真实修复：partial 记录不得推进对外边界。**

写第 1 条 oracle 时实测出**实现缺陷**（不是测试问题）：`_poll_events` 中 `state.high_water` 的自增位于 `if event.partial` **之前**，因此一条 partial 记录虽未被投递，却把对外边界从 2 抬到 999，而返回的快照 H=2。这违反 spec「草稿 MUST NOT 改变 `high_water`」——GUI 会拿一个比已投递内容更高的边界去推导页码与游标。

修复（`subscriptions.py`，`_poll_events`）：**分离两个水位**。

```python
state.last_ordinal = max(state.last_ordinal, int(ordinal))   # 去重水位：每条都前进
if event.partial:
    continue                                                  # 不是可见前缀的一部分
state.high_water = max(state.high_water, int(ordinal))        # 已投递边界：仅非 partial
```

`last_ordinal` 必须越过每条记录（partial 亦然），否则同一记录会被反复重读；`high_water` 只能由进入可见前缀的记录推进。**这是本次唯一的行为改动**，spec 语义按原文收窄而非改写。

**5. 差分验证（先阴性对照，再逐条变异）**

自建 harness 在**未变异**代码上先跑一次并确认全绿，否则"检测到"与"harness 坏了"无法区分。四条变异全部归因到本轮新 oracle，**4/4 detected**：

| 变异 | 结果 | 变红的用例 |
| --- | --- | --- |
| 删除 `if event.partial: continue` | DETECTED | `test_partial_canonical_record_is_never_delivered` |
| 删除饱和时的 `_trim_front(state, 1)` | DETECTED | `test_a_new_entry_replaces_the_oldest_queued_one` |
| 交互跳过 `tool_name` 非空项 | DETECTED | `test_a_pending_interaction_with_a_tool_name_is_delivered` |
| 把 `high_water` 自增移回 partial 判定之前（即撤销第 4 条修复） | DETECTED | `test_partial_canonical_record_is_never_delivered` |

最后一行是本轮最重要的验证：**撤销修复确实变红**，所以第 4 条不是"看着对"的改动。

**6. 本轮复核自身的三次假象（如实记录）**

- 首次 harness 重建 env 时丢掉 `USERPROFILE`，11/11 变异因 `Could not determine home directory` 而"全红"——**全是假绿**；
- 用 `_partial_event` 位置参数错位造出畸形 partial 记录，据此误判"过滤器可达"（8.3 已更正）；
- 两次因 CRLF 未归一化导致变异字符串 `count = 0`（源码与测试均为 CRLF；`read_text`/`Path.write_text` 会改变行尾）。

**结论：C04 现具备 `remaining_gap=[]` 的条件（28 条用例全绿、冻结文件零改动、四条新 oracle 均经变异验证），但最终验收仍需独立子代理对抗性审查（tasks 4.5）与用户授权，故 C04 仍不得报告完成。**

## 9. C04 期间发现并修复的两个既有缺陷（用户逐项授权）

两者**均不属于 C04**，是在排查"`rollo` 无法启动"时发现的既有问题。

### 9.1 `rollo` 无法启动（环境问题，非代码缺陷）

`rollo` 由 uv tool 以可编辑方式安装，其 `__editable__*_finder.py` 的 `MAPPING` 与 `uv-receipt.toml` 写死了**改名前**的 `My-Claude-Code\src\rollo`；目录改名为 `Rollo-Code` 后该路径不存在 → `ModuleNotFoundError: No module named 'rollo'`。已按原解释器（py313）重装并修正登记。**仓库代码无涉**；另注：`uv tool list` 曾显示 "No tools installed"，因其 `UV_TOOL_DIR` 与该自定义安装位置脱钩。

### 9.2 思考（thinking）文本被输出两次 —— 控制台 token 级重复

**症状**：控制台出现 `LetLet me me explain explain`；**只有 think 重复，正文不重复**（用户观察）。

**根因**：两个 provider 的流式分支对同一段思考文本各调了**两条**输出路径：

```python
self._out_thinking(rc)   # → 端口 → ui.print_assistant_text → sys.stdout.write
self._emit_text(rc)      # → _output_buffer is None 时 → _out_assistant_text → 同一函数
```

`ui.print_assistant_text` 是无缓冲的 `sys.stdout.write(text)`，故同一 chunk 连续写两遍。正文只有一条 `_emit_text`，因此重复只出现在思考内容上——这正是症状不对称的原因。

**修复**：删除两处多余的 `_emit_text`（Anthropic 分支与 OpenAI 兼容分支），端口路径保留。`_emit_text` 仍被正文路径使用，未成死代码。

> **更正（主 Agent 复核）**：此处原写"默认 `NullOutputPort` 下两者皆为 no-op，故对无端口场景无影响"——该论证**不成立**。`_emit_text` 走的是**另一个分支**：`_output_buffer` 非 `None` 时它是 `append`，而 `_output_buffer` 由 `run_once` 的 `_capture_output` 设置，**与端口无关**。实测子 Agent 的捕获输出因此由 `'plan'+'done'` 变为仅 `'done'`。用户确认这是预期语义（返回值不掺思考），已加断言 `test_sub_agent_captured_output_carries_the_body_but_not_the_thinking`（双 provider）并变异验证。

**证据（含变异验证）**

| 项 | 结果 |
| --- | --- |
| 真实端口测量（`TerminalOutputPort` + 真实流式循环） | 修复后 thinking=1、text=1；**加回重复行 → thinking=2**、text=1 |
| 端到端（真实 `rollo` 进程 + 假 provider：4 个思考 chunk + 2 个正文 chunk） | exit 0，六个 chunk 全部**恰好 1 次** |
| 新增回归用例 | `test_local_consumers.py::test_thinking_delta_emits_exactly_one_output_event`（Anthropic + OpenAI 双参数） |
| 变异验证（两处分别注入） | OpenAI 位注入 → `[openai]` 红；Anthropic 位注入 → `[anthropic]` 红 |

**为何既有 552 条用例全绿却漏掉它**：没有任何用例断言思考输出的**次数**。`test_tui_adapter.py` 只断言 `_out_thinking` 发出一个 `assistant_thinking` 事件——端口侧本来就正确，缺陷是**调用方多调了一次**，位于端口之前的 agent 层。

### 9.3 未处理的相邻现象（如实记录，非本缺陷）

退出时 stderr 出现 asyncio `unclosed transport` 警告，**同一异常的 traceback 会被打印两遍**（实测样本全部来自该警告）。已复核为子进程 transport 未关闭的既有清理噪声，与 9.2 无关，未修。

### 9.4 复核方法论教训（本轮我自己的三次假象）

1. **"全红"也可能是假象**：harness 丢 `USERPROFILE` 时 11/11 变异全红，实为环境错误。必须先跑未变异阴性对照。
2. **多次被自己的正则骗**：用 `\b(\w{2,})\s+\1` 判"倍增"，该模式要求词间空白，而实际是字符级；得出过 "B=0 所以修复有效" 的错误结论。
3. **测试装置本身会造假象**：`Agent` 默认 `output_port` 是 `NullOutputPort`（丢弃一切），不注入真实端口时"零调用"会被误读为"无重复"；用 `sys.executable -m rollo` 而不设 `PYTHONPATH` 会得到 `No module named rollo`，被误读为"端到端通过"。**每一次都要先证明测量装置本身有效。**

## 10. 收尾：26 项任务逐条核实（2026-09-13）

按"任务 → 测试"逐条映射后核实 26 项任务，**又发现两处缺口，其中一处是真实实现缺陷**：

**缺陷 D（真实，已修）：run DTO 缺 `prefix_boundary_exempt`。**
任务 2.3 与 design D2 都要求 run 状态与待处理交互带 `prefix_boundary_exempt: true`。实测 `snapshot.runs[0]["prefix_boundary_exempt"]` → `KeyError`：`_read_runs` 构造的 dict 从未带上该键，消费者的 `dict.get()` 得到 `None`。`GuiPendingInteraction.to_dict()` 与 `GuiDraft.to_dict()` 都有该字段，**只有 run 漏了**——而 run 状态恰恰是第二个 P0（"单一不可变前缀覆盖不到 3/4 DTO 区段"）的核心。已补字段 + `test_non_prefix_facts_are_marked_as_such_in_the_dto`。

**缺口 E（测试遗漏，已补）：无任何用例断言"只发生草稿变化时边界不变"。**
任务 3.5 的核心断言此前只有 3.4 覆盖草稿**饱和**行为，没有覆盖"草稿到达而边界不动"。补 `test_a_draft_advances_the_draft_version_but_not_the_boundary`，且刻意让它同时覆盖**两条**能移动水位而不投递可见事件的机制：可变草稿表（无 ordinal）与后缀读取中的 `partial=True` 记录（有 ordinal）。后者是 8.6 第 4 条修复的机制——**该 oracle 第一次写出来时抓不到那条修复的变异**（因为 recorder 只写草稿表、不含 partial 记录），把 partial 记录加进去后才变红。

**差分验证（4 个变异，阴性对照绿，4/4 detected）**

| 变异 | 结果 | 变红的用例 |
| --- | --- | --- |
| 删除 run 的 `prefix_boundary_exempt` | DETECTED | `test_non_prefix_facts_are_marked_as_such_in_the_dto` |
| 撤销 `high_water` 修复（自增移回 partial 判定前） | DETECTED | `test_a_draft_advances_the_draft_version_but_not_the_boundary` |
| 删除 `partial` 过滤 | DETECTED | `test_partial_canonical_record_is_never_delivered` |
| 删除饱和时的 `_trim_front` | DETECTED | `test_a_new_entry_replaces_the_oldest_queued_one` |

**最终状态（全部为独立实测）**

| 项 | 结果 |
| --- | --- |
| C04 focused | **30 passed**（8 + 12 + 10） |
| 全量回归 | **562 passed / 12 warnings / 0 skipped / exit 0**（281s） |
| `compileall -q src/rollo` | exit 0 |
| `openspec validate … --strict` | `Change 'add-gui-projection-subscriptions' is valid`（exit 0） |
| 冻结文件 | `tools.py` / `runtime_store.py` / `run_lifecycle.py` → `--exit-code 0`；`agent.py` 仅 −2（用户修复）；`application.py` 仅 +19/-0 |
| 身份 | branch `feat/c03-application-lifecycle`、HEAD `410ac7e`、Python 3.13.13 |

**26 项任务的勾选结论**：24 项已勾选并附实测证据；**未勾选的 2 项**为 `4.5`（独立对抗性审查，用户批准改为收尾统一审一次，尚未执行）与 `5.5`（commit/push/PR，未授权）。

**因此 `remaining_gap = [4.5 独立对抗性审查]`。**

## 11. 对抗性审查（4.5）的裁决与处置（2026-09-14）

审查报告：`adversarial-review.md`（51921 字节，含 `result_identity`）。41 条变异 → **DETECTED 26 / SURVIVED 15 / UNEXPECTED-RED 0**；8 条断言 CONFIRMED 6 / FALSIFIED 2；6 个怀疑方向 CONFIRMED 5 / FALSIFIED 1。审查未修改任何源码或测试（41 条变异逐条按字节还原，五个交付文件 SHA256 与 §2 冻结值逐字相同）。

### 11.1 审查对我的两处证伪（成立，已更正）

1. **M14 记录三处矛盾**：§4 表记"红"、紧随其后写"**无鉴别力项：无**"、§8.4 又记"等价变异"。审查另从 INSERT 分支（首片 `fragment_count=1` 且 `last_partial_seq=partial_seq`）、UPDATE 分支（seq 必须 `last_seq+1`）、final 清空后重建（`_partial_sequences` 同时 pop，仍从 1 起）三条路径独立证明该恒等。**已统一为"等价变异、无鉴别力"**，§4 与 §8.4/§8.6 不再冲突。
2. **M3 行未声明变异口径**却并列两条用例，暗示第一条有鉴别力。主 Agent 复现为 **4 条**红（审查报 1 条）——该归因对内部状态敏感，**两方都不完整**。已改写为只保留"该变异可检出"这一可靠结论并写明口径（`state.queue.get_nowait()` → `state.queue.pop()`）。

**教训（记入 §12）**：我把子代理的 M3 归因**未经验证抄进了 `tasks.md`**——这是本轮第二次"记录未验证的断言"。

### 11.2 F1（P0）：`runs` 违反 spec 的前缀字段契约 —— 已由用户裁决并收口

- **事实**（主 Agent 独立复现）：同一 `high_water = 2`、同一 `source_digest` 的两份快照给出 `running` 与 `succeeded`，而该 run 的终态 canonical 事件不在快照内；`to_dict()["prefix_fields"]` 含 `"runs"` 却又给它打 `prefix_boundary_exempt: true`——**同一 DTO 同时声称两类**。
- **根因**：run 状态存于 C03 控制库，**没有 ordinal**，物理上不可能由 `ordinal <= high_water` 派生。这正是 tasks 1.6 记录的 P0（"单不可变前缀覆盖不到 3/4 DTO 区段"）——**进了 design D2 却没有在 spec 收编**，于是交付物同时违反 spec 又在 DTO 里自称前缀字段。
- **用户裁决**：把 `runs` 改为非前缀字段。已同步五处：`specs/gui-projection/spec.md`（①只剩四个字段、②含 runs、新增"run 状态被标注为非前缀"场景、并加"同一字段 MUST NOT 同时列为两类"）、`design.md` D2（附收口理由）、`proposal.md`、`gui_projection.py` 的 `PREFIX_FIELDS`/`NON_PREFIX_FIELDS`、`tasks.md` 2.1。
- **新增 oracle**：`test_the_two_field_lists_match_the_actual_classification`（断言两个清单的确切内容、互斥与并集）。**此前无任何用例断言 `prefix_fields` 清单**——把 `runs` 改回前缀字段不会使任何用例变红。

### 11.3 用户另裁决：终态与交互永不被替代（原 P1）

- **事实**：饱和时 `_trim_front` 替代**最旧的已排队**条目；若终态恰在队首即被替代（`resync_required` 仍会发，但终态确实消失一瞬），使 spec 场景三"终态 MUST NOT 静默消失"在一条可达路径上落空。
- **实现**：`_trim_front` 改为只替代**可弃**条目（`_is_disposable`）。受保护集合 = `interaction` / `resync_required` / `cursor_expired` / `stream_closed`，外加**载荷带 `actions.run_terminal` 的 event**（终态没有自己的 kind：投递路径把每条 canonical 记录都框成 `kind == "event"`）。`_drain` 的静默裁剪分支原先直接 `get_nowait()` 且可绕过保护，已改走同一路径。全部受保护时**界为软目标**——宁可超限也不丢终态。
- **oracle**：`test_a_terminal_is_never_displaced_by_a_newcomer`（终态在**队首**——既有用例把它排在普通条目之后，从未真正到达该决策）、`test_an_interaction_in_the_middle_is_never_displaced`（交互为**唯一**可弃候选，不保护就会被丢）。
- **实现过程中我写反了布尔谓词**（`not _is_disposable` 导致替代受保护项），被上述两条新增 oracle 立即抓红——**新 oracle 的第一批用户就是它自己的实现**。
- 顺带删掉我先前写的 `kind == "terminal"` 分支：服务从不产生该 kind（生产者只有 snapshot/event/draft/run/interaction/resync_required/cursor_expired），属死代码；该判断改由载荷承担。

### 11.4 差分验证（5 个变异，阴性对照绿，5/5 detected）

| 变异 | 结果 | 变红的用例 |
| --- | --- | --- |
| `runs` 改回 `PREFIX_FIELDS` | DETECTED | `test_the_two_field_lists_match_the_actual_classification` |
| 删除 run 的 `prefix_boundary_exempt` | DETECTED | `test_non_prefix_facts_are_marked_as_such_in_the_dto` |
| 从受保护集合移除 `interaction` | DETECTED | `test_an_interaction_in_the_middle_is_never_displaced` |
| 删除 `event` + `run_terminal` 保护 | DETECTED | `test_a_terminal_is_never_displaced_by_a_newcomer` 等 2 条 |
| 快照首条消息 `ordinal + 1` | DETECTED | `test_event_committed_between_snapshot_and_first_suffix_read_is_not_lost` |

### 11.5 其余审查发现（逐条处置）

| 编号 | 内容 | 处置 |
| --- | --- | --- |
| F2 | "不留缺口等式"缺真正的等式 oracle（用例自读 store、循环体恒被覆盖、`runs` 断言是 `[] == []` 恒真） | **如实记录为残余**。该用例并非完全无鉴别力（丢后缀记录会红），但投递序/衔接/重复/左开边界未验证 |
| F3 | 快照首条消息 `GuiMessage.ordinal` 零断言（`+1` 仍全绿） | **已补**断言 + 差分验证（见 11.4 末行） |
| F5 | 15 条存活变异中 10 条为真实覆盖缺口；最重是 spec 场景 `unsubscribe(未知 id) → unknown_subscription` 无 oracle | **已补** `test_unknown_subscription_and_closed_cursor_are_reported`（含 `subscription_closed` 过期判据）与 `test_detaching_keeps_the_subscription_resumable` |
| F6 | `_drain` 静默丢弃分支为死代码；饱和峰值实测 `LIMIT+1` | **已修**：该分支改走 `_trim_front`（保护感知）。超限 `+1` 是"界为软目标"的必然结果，spec 已按 11.3 收窄 |
| F7 | `GuiCursor.to_dict()` 丢 `service_epoch`，往返即 `service_instance_replaced` | **如实记录**：不违反 spec（wire 格式属 C05），但零 oracle |
| F8 | 只读判据未按 spec 字面复算（作用于整本账，无 `session_id`、无 `ordinal <= H`；用例 import 了 `iter_event_records`/`source_digest` 却未使用） | **如实记录为残余**。审查以 SQL trace 独立补证只读成立（44 条语句全为 SELECT、零 write） |
| F9 | 一次**不可复现**的套件挂死（240s+，进度停在 22/30；同变异单跑 19.8s 全绿，其后 3 次全套 + 5 次 L3 单测均绿） | **判 UNVERIFIABLE**，未归因。如实记录 |
| F10 | `_finish` 非幂等，`_CLOSE` 可入队两次 | **如实记录**（无害：`__anext__` 首次即 `StopAsyncIteration`） |

### 11.6 最终状态（全部独立实测）

| 项 | 结果 |
| --- | --- |
| C04 focused | **35 passed**（9 + 14 + 12） |
| 全量回归 | **567 passed / 11 warnings / 0 skipped / exit 0**（291s） |
| `openspec validate … --strict` | valid（exit 0） |
| 未勾选任务 | `5.5`（commit/push/PR，未授权） |
| `remaining_gap` | **`[]`** —— `4.5` 已执行并处置完毕 |

### 11.7 本轮新增/更正的自述可靠性记录

审查在第 8 条上说得对：**我的矩阵自述本身不可靠**。具体三处已修：M14 三种说法、M3 未声明口径、以及"无鉴别力项：无"与 M14 行矛盾。另有两点值得留给接任者：

1. **我在 `tasks.md` 里抄了子代理未经验证的 M3 归因**（本轮第二次"记录未验证的断言"）。
2. **我自己新写的两条保护 oracle 也不是一次就对**：第一条用 `kind="terminal"`（无生产者的死 kind），第二条的"可弃候选"恰好就是交互之前的那个事件，导致"移除 interaction 保护"这个变异**测不出来**。修正后才达到 5/5——**新 oracle 同样需要变异验证，否则它只是看起来在测**。






