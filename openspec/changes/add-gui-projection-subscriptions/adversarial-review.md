# C04 对抗性审查报告（`add-gui-projection-subscriptions`）

result_identity: 独立对抗性审查子代理（DSH `deepseek-flash`，角色＝证伪者而非确认者）；审查时间 2026-09-14（本地时区，审查会话开始于 2026-09-13 深夜）；仓库 `D:\PycharmProjects\pythonProject\Rollo-Code`；HEAD `410ac7e`；Python `D:\Anaconda\envs\py313\python.exe` 3.13.13；所有产物未提交。本报告作者与 C04 实现者、§8「主 Agent 复核」者均为不同主体。

---

## 0. 复算环境与阴性对照（先证明测量装置本身有效）

历史上本 Change 出过「harness 丢 `USERPROFILE` → 11/11 变异全红（假绿）」与「CRLF 未归一 → 变异字符串 count=0（假红/假无变异）」。本轮先做两件事：

1. **行尾地图**（harness 打印，确认仓库源码为 CRLF）：

```
CRLF map: {'...gui_projection.py': True, '...subscriptions.py': True,
           '...test_c04_gui_projection.py': True, '...test_c04_gui_subscriptions.py': True,
           '...test_c04_gui_subscription_bound.py': True}
```

harness 的做法：`read_bytes()` → 解码 → `\r\n` 归一为 `\n` → 字符串匹配/替换 → 回写时若原文件是 CRLF 则 `\n` → `\r\n` → `write_bytes()`。每次变异都记录 `bytes_changed=True`，并在 `finally` 中按**内存里的原始字节**还原，还原后 `restored=True`。

2. **阴性对照**（每一轮变异批次都先跑一次未变异代码）：

| 轮次 | 阴性对照结果 |
| --- | --- |
| round 0（父代理 8 条） | `exit=0 failed=[]` → `30 passed in 21.65s` |
| round 1（自选 8 条） | `exit=0 failed=[]` → `30 passed in 19.79s` |
| round 2（自选 10 条） | `exit=0 failed=[]` → `30 passed in 20.04s` |
| round 3（自选 8 条） | `exit=0 failed=[]` → `30 passed in 20.03s` |

命令（每次）：
```powershell
$env:PYTHONPATH="D:\PycharmProjects\pythonProject\Rollo-Code\src"
D:\Anaconda\envs\py313\python.exe -m pytest -q -p no:randomly `
  src/rollo/tests/test_c04_gui_projection.py `
  src/rollo/tests/test_c04_gui_subscriptions.py `
  src/rollo/tests/test_c04_gui_subscription_bound.py
```

**harness 自身出过的两次问题（如实记录）**：

- round 1 首跑在第 2 条变异即 `MUTATION NOT FOUND` —— 我把 `runs=tuple(dict(item) for item in (runs or base.runs))` 的宿主文件写成了 `subscriptions.py`，实际在 `gui_projection.py`。修正后重跑。这条是**我自己的探针错误**，不是被测代码问题。
- round 1 第 7 条变异（R8）触发 pytest **挂死**，harness 的 `subprocess.run(timeout=900)` 抛 `TimeoutExpired` 并中断整批。已把 harness 改为 `Popen` + `taskkill /F /T` 杀进程树 + 240s 上限，并对挂死单独判定（见 §3 的 F9）。

**还原证据（报告写作前的最终实测）**：

```
$ git status --short -uall
 M .gitignore
 M src/rollo/__main__.py
 M src/rollo/agent.py
 M src/rollo/application.py
 M src/rollo/tests/test_local_consumers.py
D  src/rollo_code.egg-info/PKG-INFO
D  src/rollo_code.egg-info/SOURCES.txt
D  src/rollo_code.egg-info/dependency_links.txt
D  src/rollo_code.egg-info/entry_points.txt
D  src/rollo_code.egg-info/requires.txt
D  src/rollo_code.egg-info/top_level.txt
?? openspec/changes/add-gui-projection-subscriptions/.openspec.yaml
?? openspec/changes/add-gui-projection-subscriptions/design.md
?? openspec/changes/add-gui-projection-subscriptions/implementation-status.md
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
（与审查开始前的 `git status` 逐字一致；除本报告 `adversarial-review.md` 外未新增任何文件，无 `.orig`/`.bak` 残留。）

五个交付文件的 SHA256 与 `implementation-status.md` §2 的冻结值**逐字相同**：

```
OK   src/rollo/projections/gui_projection.py
OK   src/rollo/projections/subscriptions.py
OK   src/rollo/tests/test_c04_gui_projection.py
OK   src/rollo/tests/test_c04_gui_subscriptions.py
OK   src/rollo/tests/test_c04_gui_subscription_bound.py
```

审查结束后再跑一次 focused：`30 passed in 19.69s`。**未修改任何源码或测试**（变异只作临时注入，逐条还原）。

---

## 1. 逐条裁决（父代理的 8 条断言）

### 断言 1 —— `_poll_events` 水位分离正确 —— **CONFIRMED**

实测：`subscriptions.py:881-886` 的当前文本为

```python
state.last_ordinal = max(state.last_ordinal, int(ordinal))
if event.partial:
    continue
state.high_water = max(state.high_water, int(ordinal))
```

反向判据（变异）：

| 变异 | 结果 |
| --- | --- |
| P1 删除 `if event.partial: continue` | **DETECTED**，`2 failed`：`test_a_draft_advances_the_draft_version_but_not_the_boundary`、`test_partial_canonical_record_is_never_delivered` |
| P4 把 `high_water` 自增移回 `partial` 判定之前 | **DETECTED**，`2 failed`：同上 |

另有 SQL 级旁证（见断言 4 的 trace）：后缀读确实是 `ordinal > 3` 左开区间。

**限度（我未能验证的部分）**：「`last_ordinal` 对每条记录都前进」这一半**没有直接 oracle**。真实 store 从不把 `partial=True` 写进 `runtime_events`（`runtime_store._partial_fields` 与 `_append_in_transaction` 互斥），所以该分支只被两个 L1 替身用例命中，而这两个用例只断言 `high_water` 不变、`last_ordinal >= 999`，删掉 `state.last_ordinal = ...` 不会变红。我未构造出「删掉 last_ordinal 前进导致重读死循环」的真实路径，故该半条判为「代码正确、无鉴别力」，不计入 CONFIRMED 的实测部分。

### 断言 2 —— run DTO / pending / draft 都带 `prefix_boundary_exempt: True` —— **CONFIRMED**

- `subscriptions.py:694`：`runs[-1]["prefix_boundary_exempt"] = True`；
- `gui_projection.py:124`（draft）与 `:145`（pending）的 `to_dict()` 各带该字段。
- 变异 P5（删除 run 的该行）→ **DETECTED**，`test_non_prefix_facts_are_marked_as_such_in_the_dto` 变红。
- 我的独立探针 `probe_runs.py` 直接打印：`runs[0].prefix_boundary_exempt = True`。

**但这条 CONFIRMED 恰好暴露了本 Change 最重的问题**：spec 把 `runs` 明确列为**前缀字段**，实现却给它打上「非前缀」标记。详见 §2 的怀疑 6 与 §3 的 F1。

### 断言 3 —— 30 条用例有鉴别力；父代理声称的 8 个变异全部变红 —— **部分 FALSIFIED**

**父代理点名的 8 条（我另写 harness 重跑，阴性对照绿）：8/8 DETECTED**，且归因与其自述一致（仅 P6 的用例集合、P3 的变异措辞略有差异，不影响结论）：

| 我的 ID | 变异 | 结果 | 变红用例 |
| --- | --- | --- | --- |
| P1 | 删除 partial 过滤 | DETECTED | 2 条（draft-boundary、partial-never-delivered） |
| P2 | 删除饱和时 `_trim_front` | DETECTED | `test_a_new_entry_replaces_the_oldest_queued_one` |
| P3 | 交互跳过 `tool_name` 非空项 | DETECTED | `test_a_pending_interaction_with_a_tool_name_is_delivered` |
| P4 | 撤销水位修复 | DETECTED | 2 条 |
| P5 | 删除 run 的 exempt 字段 | DETECTED | `test_non_prefix_facts_are_marked_as_such_in_the_dto` |
| P6 | `after_ordinal` 差一（`-1`） | DETECTED | 7 条 |
| P7 | 不校验 `service_epoch` | DETECTED | `test_cursor_expired_on_every_declared_criterion` |
| P8 | 不校验 `projection_version` | DETECTED | 同上 |

**FALSIFIED 的部分**：父代理在同一段里声称「**草稿 revision 改用 `fragment_count`**」也能使至少一条用例变红（表格 M14 记为「红」）。实测该变异**30 passed / exit 0，无任何用例变红**：

```
[M14] revision = int(getattr(partial, "fragment_count", 0) or 0)
exit: 0
failed: []
30 passed in 19.97s
```
（变异点唯一性已单独验证：`_read_drafts` 中 `last_partial_seq` 行在归一化后的源码里出现 1 次；还原后 SHA256 与冻结值一致。）

父代理在 `implementation-status.md` §8.4 里其实**自己写过它是等价变异**（"两字段在全部可达路径上不可区分……§4 该行宜改记为等价变异"），但 §4 矩阵与本次汇总仍按「红」陈述 —— 属于**自述内部未收口**，见 F4。

**「30 条用例全部有鉴别力」不成立**：我自写的 33 条变异中 15 条存活（详见 §3、§4）。

### 断言 4 —— 只读边界成立（id 集合 + 计数 + 前缀 digest + 文件哈希）—— **CONFIRMED（附判据收窄）**

我用的判据比父代理更强：给真实 `SQLiteRuntimeStore` 的连接挂上 `sqlite3.set_trace_callback`，把订阅路径执行的**每一条 SQL** 都记下来：

```
statements issued by the subscription path: 44
    SELECT * FROM runtime_events ORDER BY ordinal ASC
    SELECT * FROM runtime_events WHERE ordinal <= 3 ORDER BY ordinal ASC
    SELECT * FROM runtime_events WHERE session_id = 'session-c04' AND ordinal > 3 ORDER BY ordinal ASC
    SELECT * FROM runtime_stream_partials WHERE session_id = 'session-c04' ORDER BY created_at, stream_key
    SELECT high_water FROM runtime_session_event_ordinals WHERE session_id = 'session-c04'
    SELECT invocation_id, event_seq FROM runtime_events ORDER BY invocation_id ASC, event_seq ASC
write statements: []
event ids equal : True
event count     : 3 -> 3
sha256 equal    : True
delivered kinds : ['snapshot']
snapshot H      : 3
```

⇒ 订阅/投影路径在 canonical 连接上**只发 SELECT**，零写语句；id 集合、计数、文件哈希三者不变。

**判据收窄（如实记录）**：既有的 `test_subscription_path_writes_nothing` 并没有按 spec 的字面判据复算。spec `gui-projection` 非前缀字段那节要求的是「canonical 的事件 id 集合、事件计数、以及 `ordinal <= high_water` 的**前缀** digest」，而该用例实现的是：

```python
before_ids = [pair[1].id for pair in store.read_event_records()]      # 无 session_id / 无 high_water
before_digest = sha256(b"".join(pair[1].canonical_bytes() for pair in store.read_event_records()))
```

即「**整本账**的 id/计数/digest」，既没有 `session_id` 过滤也没有 `ordinal <= H` 过滤（文件字面上调用的 `iter_event_records`/`source_digest` 在用例里根本没被使用，只有顶部 import）。这不是漏检（整本账不变 ⇒ 前缀不变），但它**不是 spec 要求的那个判据**，实际承担全部鉴别力的是 `sha256(runtime.sqlite)` 那一行。见 F8。

### 断言 5 —— `Application.runs_list` 是纯新增 —— **CONFIRMED**

```
$ git diff --numstat 410ac7e -- src/rollo/application.py src/rollo/agent.py src/rollo/__main__.py
28      0       src/rollo/__main__.py
0       2       src/rollo/agent.py
19      0       src/rollo/application.py

$ git diff -U0 410ac7e -- src/rollo/application.py
@@ -1150,0 +1151,19 @@ class Application:
+    def runs_list(self, session_id: str) -> ApplicationResponse:
...
```
单一 hunk、`19/0`、hunk 头为 `-1150,0`（删除侧长度为 0）⇒ 未触碰任何既有行。方法体只调用 `self._ensure_open()` 与 `self.control.runs_for_session(...)`。

### 断言 6 —— `agent.py` / `__main__.py` 不含 C04 内容 —— **CONFIRMED**

```
@@ -3128 +3127,0 @@   agent.py   （删除 1 行）
@@ -3545 +3543,0 @@   agent.py   （删除 1 行）
@@ -392,0 +393,27 @@    __main__.py（新增 _configure_stdio_encoding）
@@ -393,0 +421 @@       __main__.py（新增调用）
```
`agent.py` 的两处删除都是 `self._emit_text(thinking)` / `self._emit_text(rc)`（思考文本双路径重复修复）；`__main__.py` 的新增是 `_configure_stdio_encoding()` + 其调用 + docstring。

我另跑了关键词扫描（大小写不敏感）覆盖 `agent.py`、`__main__.py`、`test_local_consumers.py` 的全部 diff：

```
$ git diff 410ac7e -- <三文件> | Select-String 'projection|subscription|Gui|gui_projection|high_water|runs_list' -CaseSensitive:$false
（空输出）
```
`test_local_consumers.py` 的新增只有 `test_thinking_delta_emits_exactly_one_output_event` 与 `test_sub_agent_captured_output_carries_the_body_but_not_the_thinking` 两个测试。**无任何 C04 内容夹带**。

### 断言 7 —— 全量回归 562 passed / 0 failed / exit 0 —— **CONFIRMED（一处笔误）**

```
$env:PYTHONPATH="D:\PycharmProjects\pythonProject\Rollo-Code\src"
D:\Anaconda\envs\py313\python.exe -m pytest -q src/rollo/tests
→ 562 passed, 11 warnings in 276.91s  [exit code: 0]
```
我自己从零重跑，与 `implementation-status.md` §10 的 `562 passed / 12 warnings / exit 0` 一致，唯一差异是 **warnings 数为 11 而非 12**（§3 的 `552 passed, 11` 与 §8.6 的 `556, 11` 也都是 11）。属文档笔误，无实质影响。

### 断言 8 —— `tasks.md` 勾选项都有真实证据，尤其 4.4 变异矩阵 —— **FALSIFIED**

逐项核对了 26 项任务的勾选与其证据，绝大多数可复算（`openspec validate --strict` → `Change 'add-gui-projection-subscriptions' is valid` exit 0，我独立重跑；冻结文件 `git diff --exit-code 410ac7e` 结果与自述一致；`4.5`/`5.5` 如实未勾选）。**但 4.4 的矩阵与实现、与其自身后文均不符**，属夸大/未收口：

1. **M14 行与实测相反**：`tasks.md:83` 记「**等价变异，无鉴别力** —— 实测……两字段在全部可达路径不可区分」；`implementation-status.md:116` 记 `M14 | 草稿 revision 改用 fragment_count | 红 | 两个文件整文件 error`；同文件 `:119` 又断言「**无鉴别力项：无**。15 个变异全部至少使一条用例变红」；`§8.4` 与 `§8.5` 则明确要求把该行改记为等价变异。**四个位置三种说法**，且 §4 的「红」与实测（30 passed）相反。我复跑 3 次全部 `30 passed`。
2. **M11 / M12 / M13 / M16 的记载与我实测的归因不完全一致**：M12/M13 我得到的归因（`test_cursor_expired_on_every_declared_criterion`）与 §4 一致；M11 我得到 7 条变红，与「7 条」一致；但 §8 记录的 M11 归因是「4 条（burst / overflow-sibling / draft-at-bound / no-gap）」，两处不一致（§8 用的是它自己的变异子集，未声明这一点，读起来像同一变异的两个数字）。属**证据链自相矛盾**。
3. **§4 的 M3 归因（「溢出时静默丢弃**最新**条目」）用词歧义，且它点名的第一条用例并不具备该鉴别力**。§4 记该变异使「`test_stalled_subscriber_is_closed_once_with_the_backlog_intact`、`test_terminal_and_interaction_survive_a_full_backlog`」变红。我按「丢弃**最旧的已排队**条目」的反面实测了一条追加变异（`_trim_front` 改为丢弃**队列尾部**条目，即丢弃最新的已排队条目）：

   ```
   $ D:\Anaconda\envs\py313\python.exe round4.py     # 先跑阴性对照：30 passed
   [DETECTED] M3' saturation trim drops the NEWEST queued entry  (subscriptions.py)
     bytes_changed=True restored=True exit=1
     1 failed, 29 passed in 21.20s
       FAILED test_a_new_entry_replaces_the_oldest_queued_one
   ```

   ⇒ 抓住它的**只有** `test_a_new_entry_replaces_the_oldest_queued_one`；§4 点名的两条**都不在失败列表里**（`test_stalled_subscriber_...` 的 `ordinals == sorted(...)`、`ordinals[-1] == 12`、`len <= LIMIT+1`、`count(resync)==1` 在丢最新下仍全部成立，因为 `_overflow` 的 flush 会把 buffer 中的最新条目补回队列尾部）。若 §4 的原意是「丢弃**最新到达**的条目（即让新条目顶掉自己）」，那它的归因可以自洽——但该描述与「替换最旧」的对偶关系没有写清，读者无法判断到底是哪种，且**该行的两条并列暗示第一条有鉴别力，实测为无**。这一条不是实现缺陷，是**证据描述的可靠性问题**：一个把「有鉴别力」与「无鉴别力」用例并列的矩阵，会让复核者误以为覆盖更宽。
4. `implementation-status.md` §2 的交付物行数（`subscriptions.py 962`、`test_c04_gui_subscriptions.py 762`、`test_c04_gui_subscription_bound.py 479`）与实际（973 / 919 / 751）不符，虽然同文件 `:46` 给出「最终形态」更正；§5 的新旧用例清单也是旧的（24 条 → 现为 30 条），实际状态在 §10。这些是**未收口的陈旧段落**，不构成功能缺陷，但作为「证据」会误导复核者。

---

## 2. 逐条裁决（6 个怀疑方向）

### 怀疑 1 —— `_enqueue` 非溢出路径静默丢最旧、`resync_required` 迟发 —— **CONFIRMED 可达；未构成 spec MUST 违反（但 spec 场景三的「MUST NOT 静默消失」在一条可达路径上落空）**

实测（`probe_overflow.py`，真实 `SQLiteRuntimeStore`，`LIMIT=8`，订阅者停止消费后写入 79 条事件）：

```
declared LIMIT                 : 8
peak queue size observed       : 9
peak unconsumed (queue+buffer) : 9
closed                         : True
queue after close              : [('event',73) ... ('event',80), ('resync_required',None), '_CLOSE', '_CLOSE']
event ordinals still queued    : [73, 74, 75, 76, 77, 78, 79, 80]
loss (never delivered)         : [2, 3, ..., 72]
```

- **丢弃确实可达**：ordinal 2–72 共 71 条**永远不会被投递**（消费者随后只会拿到 73–80 + resync 通知）。
- **不是永久静默**：`resync_required` 恒在队列中存活。原因是 `_overflow` 把通知 append 到 buffer **之后**才 `_drain`，而该次 `_drain` 走 `state.overflowed` 为真的分支，只 flush 不裁剪；非溢出裁剪路径又够不到已经入队的通知（通知是队列尾部条目）。我在 141 次带插桩的 `_drain` 调用中确认通知从未在队列内被裁剪。
- **因此「恰好一次 `resync_required`」的基数性成立**（另有 `test_stalled_subscriber_is_closed_once_with_the_backlog_intact` 断言 `kinds.count("resync_required") == 1`），spec 的 MUST 未被绕过。
- **但 spec 场景三字面落空**：「缓冲已满时到达终态事件与交互请求 → 两者都仍被投递（或以 `resync_required` 明确关闭该订阅），MUST NOT 静默消失」。当**终态已经坐在队列头部**（例如上一轮的 run 终态）而随后到来的事件触发 at-bound 替代时，该终态会被 `_trim_front` 丢掉，而订阅仍处于 open、`resync_required` 要等 stall 宽限期届满才出现。此时终态消失了、且当刻没有任何通知。要触发它需要「同一 session 先有终态、后有大量新事件、且订阅者停读」——多 run 会话完全可达。这是**规范与实现的真实缺口**（严重度低于 F1：这条路径丢的是历史终态，且最终仍会以 resync 收口）。

### 怀疑 2 —— `_drain` 在 `not overflowed` 且 `qsize() > LIMIT` 时静默丢队列头：可达吗？ —— **FALSIFIED（不可达）**

插桩实测（`probe_drain.py` / `probe_drain2.py`，真实 store，一个停读订阅 + 一个持续消费订阅 + 连续写入，`LIMIT=4`）：

```
STATS: {'drain_calls': 141, 'drain_drop_branch_seen': 0, 'drain_max_excess': 0,
        'queue_size_after_drain_max': 5, 'post_drain_excess_positive': 0,
        'trim_calls': 29, 'trim_entries': 29}
guard `queue.qsize() > LIMIT` was true BEFORE a drain ... 0 times
```

「before」这一次测量是关键：该分支只在**进入 `_drain` 时队列已超限**才会执行，而 141 次调用中该前置条件从未成立。静态上也可推导：`_enqueue` 在 at-bound 时先裁到 `LIMIT` 再 append ⇒ 装载后至多 `LIMIT+1`；随后的 `_drain` 走 buffer 分支后队列降到 `LIMIT`，`elif` 永不进入；而 `_drain` 的另外两个调用点（`subscribe` 内、`_overflow` 内）前置条件分别是不可能超限与 `overflowed=True`（短路掉 `elif`）。

独立变异佐证：**删除整个 `elif` 分支**（S2）→ `30 passed / exit 0`，无任何用例变红 ⇒ 它是**死代码**。

### 怀疑 3 —— `_overflow` 冲洗后队列可能超过 `SUBSCRIPTION_BUFFER_LIMIT` —— **CONFIRMED（实测超限 +1）**

`peak queue size observed = 9`，`declared LIMIT = 8` ⇒ 队列实际持有 `LIMIT + 1 = 9` 条未消费条目。机制：`_enqueue` 在 at-bound 时裁到 8、append 使装载达 9，随后 `_drain` 在 `overflowed=True` 时只 flush 不裁剪，于是队列停在 9。

- spec「每个订阅的未消费条目数 MUST 受具名常量 `SUBSCRIPTION_BUFFER_LIMIT` 约束」按**绝对值**读，此处被超出 1 条（12.5%）。
- 这不是隐藏行为：`test_stalled_subscriber_is_closed_once_with_the_backlog_intact` 自己就断言 `len(ordinals) <= subs.SUBSCRIPTION_BUFFER_LIMIT + 1`，**把这个 +1 写进了期望**。
- 与怀疑 1 相比，这条没有丢数据、也没有绕过通知；属**判据与实现的口径不符**，两种读法（`≤ LIMIT` vs `≤ LIMIT+1`）都能自圆其说，但 spec 只写了前者。另注：同一用例还断言 `state.high_water == 2`（在我读来是 `_pending` 打错成了 `high_water`），该行不起作用。

### 怀疑 4 —— `GuiCursor.to_dict()` 不含 `service_epoch` —— **CONFIRMED（无 oracle；不影响本 Change 声明的判据）**

实测：`to_dict()` 返回 `{subscription_id, session_id, high_water, projection_version, partial_versions}`，无 `service_epoch`。而 `resume` 第一道闸就是 `cursor.service_epoch != self._service_epoch` ⇒ 任何经 `to_dict()` 往返重建的游标 `service_epoch` 为空串，**必然** `cursor_expired / service_instance_replaced`。

- 对 spec **不构成违反**：spec 只要求 `GuiCursor` MUST 至少含那五个字段（dataclass 满足），三类过期判据也照常生效；`service_epoch` 的 wire 编码明确属 C05，且本 Change 声明单进程。
- 但它是一个**静默降级的接口陷阱**：一个「看起来可序列化、可文档化」的方法会破坏它唯一可能的用途（进程内重连时携带游标）。
- **零 oracle**：变异 R7（`to_dict()` 删掉 `partial_versions`）→ `30 passed`。`to_dict()` 与 `service_epoch` 处理在其任何形态下都没有用例。
- **缓解**：`resume` 返回的 `cursor` 是 `replace(cursor, service_epoch=self._service_epoch)`，所以服务自己发的游标永远带 epoch；只有跨进程/序列化才会命中。判为 P2，不判违反。

### 怀疑 5 —— 草稿 `revision := last_partial_seq` 与 `fragment_count` 恒等 —— **CONFIRMED（等价变异）**

实测：变异把 `revision` 改读 `fragment_count`，`30 passed / exit 0`（跑 3 次，含一次专用诊断跑）。我另外从代码路径独立验证了这个「恒等」为什么成立：

- `runtime_store._upsert_stream_partial_in_transaction` **INSERT 分支**：`fragment_count=1`，`last_partial_seq = fields["partial_seq"]`。存在一个**理论上的分歧构造**：首次落库时若 `partial_seq > 1`，两者立即不等（1 vs N）——但 `_partial_fields` + recorder 侧 `_next_partial_sequence`（从 0 起 +1）保证经公开 API 的首次落库必为 `partial_seq == 1`。
- **UPDATE 分支**：`fragment_count = row.fragment_count + 1`、`last_partial_seq = incoming_seq or last_seq + 1`，且 `incoming_seq` 非空时必须 `== last_seq + 1`（否则抛 `IdempotencyConflictError`）⇒ 两者同步 +1。
- **中间清空后重建**：`final_text` → `append_event_and_clear_runtime_partials` DELETE 该 key 的行；`_forget_partial_streams` 同时 `_partial_sequences.pop(key)`。故行重建时 `fragment_count` 重新从 1 起、`last_partial_seq` 也从 1 起，**仍然恒等**。seq 跳号被 store 显式拒绝。

⇒ **在全部经公开 recorder/store API 的可达路径上，`revision`（`last_partial_seq`）恒等于 `fragment_count`**，这条变异不可能有鉴别力。父代理的「等价变异」结论成立，但同一份 `implementation-status.md` §4 却把它记为「红」，且本次汇总把「改用 fragment_count」列入「8 个都能使至少一条用例变红」——**该陈述被证伪**。

唯一能打破恒等的路径是绕过 recorder 直接调用 `store.append_runtime_partial_batch` 并让首片带 `partial_seq > 1`：我**未构造**该用例，故只能说「经公开 API 不可区分」，不能断言「任何输入都不可区分」。

### 怀疑 6 —— 快照的 `runs`（非前缀）与 `terminals`（前缀）可能自相矛盾 —— **CONFIRMED（且与 spec 冲突）**

`probe_runs.py`（真实 `SQLiteRuntimeStore` + 可控控制面替身）：

```
== phase 1: control says running, no terminal in the ledger ==
  H = 1
  snapshot.high_water           = 1
  runs[0].status                = running
  runs[0].prefix_boundary_exempt= True
  terminals                     = ()
== phase 2: the control plane moves; canonical does NOT ==
  H (unchanged)                 = 1
  snapshot.high_water           = 1
  runs[0].status                = succeeded
  terminals                     = ()
  source_digest unchanged       = True
  SELF-CONTRADICTORY AT THE SAME H: True
```

即：**同一个 `high_water = 1`、同一个 `source_digest` 的两份快照，对同一个 run 给出 `running` 与 `succeeded` 两个状态**，而该 run 的终态 canonical 事件（ordinal 2）**不在**这两份快照里。phase 4 还显示 `runs` 里可以出现一个前缀中根本没有事实的 run（控制面 `queued`，而 prefixes 里只有上一轮的 `terminal-1`）。

这与判据（spec 是唯一需求判据）**直接冲突**：

- spec `gui-projection`「前缀字段与非前缀字段必须被显式区分」MUST 列表把 **`runs` 列为前缀字段**（`messages`、`runs`、`terminals`、`errors`、`source_digest`），非前缀字段只有 `drafts` 与 `pending_interactions`；
- 同一 spec ：「投影 MUST NOT 在任何构建过程中重新查询『最新值』」、「MUST NOT 读取该边界之后的事件」；
- 同一 spec 场景：「`source_digest` 与消息序列**逐条等于**用 `iter_event_records(store, high_water=H)` 过滤该 session 后独立复算的结果」；
- spec `gui-subscription`「不留缺口」等式同样把 `ordinal <= H` 的条目当作前缀事实。
- `proposal.md:9` 也自述「①前缀字段（消息、**run**、终态、错误；`ordinal <= high_water`）」。
- 更刺眼的是**实现自己在 DTO 里说反话**：`GuiSnapshot.to_dict()["prefix_fields"]` 仍包含 `"runs"`，而 `runs[0]["prefix_boundary_exempt"]` 却是 `True`。同一份 DTO 同时声称 runs 是前缀字段和非前缀事实。
- 相关自相矛盾：`design.md` D2 表格把 `runs` 列在类别①「前缀字段 …… 由 store 复算」，并明说「`SessionProjection` | 会话消息与 run 列表（已有 `high_water`、`messages`、`runs`、……）」「由此 store 复算」；而「非前缀字段」定义又只含 drafts/pending。

**零 oracle**：`test_no_gap_equation_between_snapshot_and_event_suffix` 里的

```python
assert expected.runs == snapshot.runs          # 空列表 == 空列表
```

在 `runs` 恒为空时恒真。变异 R1（`subscribe` 里去掉 `runs=runs`）与 R2（`build` 忽略传入的 `runs`、只用前缀 runs）都**只**因 `test_non_prefix_facts_are_marked_as_such_in_the_dto` 的人造控制面替身（`run-exempt` 在前缀里不存在）变红，**没有任何用例断言 `runs` 受 `ordinal <= H` 约束**。

⇒ 见 F1（P0）。

---

## 3. 新发现（按严重度分级）

### F1（P0）`runs` 违反 spec 的前缀字段契约，快照可在同一 `high_water` 下自相矛盾，且零 oracle

- **事实**：`SubscriptionService._read_runs` 从 C03 控制面实时读取 run 状态并在快照里覆盖前缀投影（`subscriptions.py:677-696` + `:396`/`:446`），同时给每行打 `prefix_boundary_exempt = True`。spec 明确把 `runs` 列为前缀字段。§2 怀疑 6 的探针给出了同一 H 下 `running` → `succeeded`、「run 已 succeeded 但该 run 的终态不在快照里」的实例。
- **判据**：spec `gui-projection` 前缀字段 MUST 与「MUST NOT 在构建过程中重新查询最新值」／spec `gui-subscription` 不留缺口等式／`proposal.md:9`／`design.md` D2 表格。
- **可复算命令**：
  ```powershell
  $env:PYTHONPATH="D:\PycharmProjects\pythonProject\Rollo-Code\src"
  D:\Anaconda\envs\py313\python.exe C:\Users\Administrator\AppData\Local\Temp\c04rev\probe_runs.py
  ```
- **影响**：GUI 用 `high_water` 推页码/游标，却从 `runs` 里拿到边界之外的实时状态；「快照＝H 上的前缀」这一整个验收判据对 3/4 的 DTO 区段（run 状态）不成立。这正是 `tasks.md` 1.6 记录的两份独立设计审查给出的 P0（「单一不可变前缀覆盖不到 3/4 DTO 区段」）——它被**记进了设计文档、却没有被 spec 收编为"runs 属非前缀"**，于是交付物同时违反 spec 又在 DTO 里自称前缀字段。
- **建议**（不属于审查动作，供决策）：二选一——要么让 `runs` 真正取自 `ordinal <= H` 的前缀（并让 `runs` 进入 digest 或显式声明它不参与 digest），要么在 spec 里把 `runs` 从 `PREFIX_FIELDS` 移到非前缀字段并同步 `PREFIX_FIELDS`/`proposal`/`design` 的表述。**当前状态是两者都没做。**
- **鉴别力**：新增 oracle（在 H 处锁定控制面、把 run 推进到 succeeded、重建同一 H 的快照，断言 `runs` 不变）在现状下必然变红；变异 R1/R2 应被该断言抓住，而不是靠人造控制面替身。

### F2（P1）「不留缺口等式」没有覆盖等式本身；两条相关断言是恒真式

- **事实**：`test_no_gap_equation_between_snapshot_and_event_suffix` 声称验证 `merge(snapshot@H, suffix (H, H2]) == project@H2`，但：
  1. 它**自己用 store 重读后缀**（`store.read_event_records(session_id=SESSION, after_ordinal=h1)`），完全没有经过 `SubscriptionService` 的投递路径；用例注释「the exact read the service performs」只对读法成立、对**交付**不成立。
  2. 循环体 `merged = _items(projection.build(..., high_water=ordinal))` 每次都用**当时最新的完整前缀**重建，下一次迭代覆盖上一次 ⇒ 最终值只取决于最后一次迭代的 `ordinal`，与「合并」无关。等价于「直接用最新 H 建一次快照」（我在探针里对比过：`rebuild@h2 == expected` 恒真）。
  3. 用例里 `assert expected.runs == snapshot.runs` 是 `[] == []` 恒真。
- **判据**：`proposal.md` 的 D(C04) 退出条件明写「快照与订阅的**不留缺口**有不留缺口的**等式 oracle**」；spec `gui-subscription` 场景「合并结果等于在 H2 上的直接投影」。
- **可复算**：
  ```powershell
  D:\Anaconda\envs\py313\python.exe C:\Users\Administrator\AppData\Local\Temp\c04rev\probe_nogap.py
  ```
  输出会逐步打印循环重建的中间值，可直接看出每次覆盖。
- **注**：缺失后缀记录**会**被最后的 `merged == _items(expected)` 抓住（循环不执行 ⇒ merged 停在 H1），所以这不是「完全无鉴别力」，而是「严格等式没有 oracle」：投递序、快照/后缀衔接、重复项、`(H, H2]` 左开边界都没有被这条用例验证。变异 R6（快照消息 ordinal +1，即边界令牌本身错位）30 passed，是同一空白的第二个实例。
- **建议**：用 `SubscriptionService` 驱动后缀投递，收集 `GuiMessage(kind="event")`，再逐条与 `project@H2` 比对（含 ordinal 与 body_ref/摘要），并断言投递序严格递增、无重复、无缺口。

### F3（P1）`GuiMessage.ordinal`（快照的首条投递序号）无任何断言

- **事实**：变异 `ordinal=high_water` → `ordinal=high_water + 1`（R6/R8）**30 passed / exit 0**（R8 复跑一次确定性命中，另有一次挂死见 F9）。快照消息是订阅的第一条消息，其 `ordinal` 是 C05 在 wire 上要用作边界令牌的字段；代码里的语义注释还专门写了「This is the boundary. Start the suffix strictly after it.」
- **判据**：spec `gui-subscription` 场景「`subscribe` → 第一条消息是 `snapshot`，其 `payload.high_water` 即该订阅的起始边界」——`payload.high_water` 有断言，**投递面同名字段没有**。
- **建议**：补 `assert snapshot_message.ordinal == snapshot.payload.high_water`，并断言其后第一条 `event` 的 ordinal 严格大于它。

### F4（P1）`tasks.md` 4.4 变异矩阵存在与实现相反的记录，且跨文件三处不一致

- **事实**：`tasks.md:83` 记 M14「等价变异，无鉴别力」；`implementation-status.md:116` 记 M14「红」；`:119` 又写「**无鉴别力项：无**。15 个变异全部至少使一条用例变红」；§8.4/§8.5 又建议把该行改记为等价变异。实测 `30 passed / exit 0`（我跑了 3 次）。同一份自述在同一事实上有三种说法。
- 另外 `implementation-status.md` §2 行数（962/762/479）与 §5 用例清单（24 条）都是陈旧值（实际 973/919/751 与 30 条），虽在同文件后文更正，但作为「证据」会误导。
- **判据**：`tasks.md` 4.4「逐条确认对应用例变红；无可鉴别力处**如实报告**」；4.5 要求「不采信自述」。
- **建议**：把 M14 行改记为「等价变异（无鉴别力）」并删除 §4 的「无鉴别力项：无」断言；统一 §4/§8 的 M11 归因口径；把 §2/§5 更新为最终值。

### F5（P2）错误路径与若干公开 API 分支零 oracle（15 条存活变异）

以下变异全部 `30 passed / exit 0`，说明对应分支没有任何用例（阴性对照绿，排除 harness 失效）。**「存活」分三种性质**：`无 oracle`（可达但没人测）、`不可达`／`死路径`（改不改都一样，属冗余代码而非覆盖缺口）、`等价`（两字段在可达路径上恒等）：

| 我的 ID | 变异 | 性质 |
| --- | --- | --- |
| T1 | `unsubscribe` 未知 id 返回 `unsubscribed`（而非 `unknown_subscription`） | **无 oracle**，且是 spec 场景 |
| T2 | `resume` 去掉「已订阅」闸门 | 无 oracle |
| T4 | `subscribe` 不再拒绝携带 cursor | 无 oracle（防御性检查） |
| T7 | `snapshot` 不再校验 `projection_version` | 无 oracle（公开参数校验） |
| R3 | `_expiry_reason` 去掉 `cursor_ahead_of_source` | 无 oracle（未构造该类游标） |
| R7 | `GuiCursor.to_dict()` 去掉 `partial_versions` | 无 oracle（`to_dict` 零用例） |
| R6 | 快照消息 `ordinal` +1 | 无 oracle（F3） |
| S6 | `_read_pending` 保留非 `pending` 状态 | 无 oracle（替身只造 pending） |
| S8 | `_read_runs` 不排序（`reverse()`） | 无 oracle / 无法区分（run 顺序恰好一致） |
| S9 | `_read_pending` 不按 `request_id` 排序 | 同上（只有 1 条） |
| M14 | 草稿 `revision` 改读 `fragment_count` | 等价（公开 API 路径恒等；§2 怀疑 5 已独立推导） |
| S1 | `_storage_key` 去掉 `stream_key` 兜底 | 不可达（`candidate` 恒非空） |
| T8 | `_read_drafts` 保留首行而非新 revision | 不可达（每个 `stream_key` 至多一行，去重循环从不生效） |
| R4 | 去掉 `_drain` 的 `not overflowed` 守卫 | 不可达（同 S2 的死分支） |
| S2 | 删除 `_drain` 的静默丢队列头分支 | 死代码（删掉后全绿，与 F6 一致） |
| T3 | `_expiry_reason` 去掉 `source_expired` | 死路径（`state.expired` 只在 `_pump` 异常分支设，该分支随即置 `closed=True`，`subscription_closed` 先命中） |

（表内 16 行 = 15 个独立存活变异 + 1 行重复：R4 与 S2 是同一条分支的两种改法；另 R6 与 F3 同源。）

**其中真正的覆盖缺口是「无 oracle」那 10 条**（T1、T2、T4、T7、R3、R7、R6、S6、S8、S9），其余 5 条是冗余代码或等价变异、补用例也无意义（应当直接删代码或记录为等价）。**T1 最值得修**：spec 明写「`unsubscribe` 一个未知 id 返回 `unknown_subscription`」，这条 **MUST 级场景没有任何 oracle**。T4/T7 属公开 API 未覆盖分支。S8/S9 的排序是投递面确定性的基础（GUI 帧顺序），建议补一条多 run/多交互的顺序断言。

### F6（P2）`_drain` 里那段「静默丢队列头」是死代码

- **事实**：141 次插桩调用中前置条件 `not overflowed and qsize() > LIMIT` 从未成立；**删除整段**（S2）→ `30 passed / exit 0`。
- **判据**：实现自身「This is the only silent drop」的注释描述了一个不可达路径；spec 的「有界失败恰好一次通知」不因此被绕过。
- **建议**：实为冗余代码，建议删除或改为 `assert`（本次审查按项目纪律不擅自清理，仅提示）。同时建议把 spec/实现的口径统一为 `≤ LIMIT+1`（见怀疑 3），或让 `_enqueue` 在装载阶段真正守住 `≤ LIMIT`。

### F7（P2）`GuiCursor.to_dict()` 丢 `service_epoch`，且 `to_dict()` 零 oracle

见 §2 怀疑 4。建议：要么把 `service_epoch` 放进 `to_dict()`，要么在 docstring 与 spec 层面明确「本方法仅用于展示，不得用于重建游标」，并补一条 `to_dict() → GuiCursor(**d) → resume` 的行为断言（现状必然 `cursor_expired`，把它固定下来也好过现在的空白）。

### F8（P2）只读判据未按 spec 字面实现（无 session / 无 `ordinal <= H` 过滤）

- **事实**：`test_subscription_path_writes_nothing` 的 id 集合、计数、digest 都作用于**整本账**（`store.read_event_records()` 无参），而 spec 要求的是「canonical 的事件 id 集合、事件计数、以及 `ordinal <= high_water` 的**前缀** digest」。用例顶部 import 了 `iter_event_records`/`source_digest` 却从未使用。
- **判据**：spec `gui-projection` 只读那节的「可观察判据（静默 store）」。真判据更强：整本账不变 ⇒ 前缀不变，所以**没有漏检**；但「MUST 的前缀 digest 判据」在测试里不存在，容易被误读为已覆盖。
- **建议**：按 spec 字面用 `iter_event_records(store, high_water=H)` + `source_digest` 复算前缀 digest，并显式加 `session_id` 过滤（这也是 `test_c04_gui_projection.py` 里现成的 `_prefix_digest` 辅助函数在做的事）。

### F9（P2）L3/插件级用例在全量 C04 套件下出现一次不可复现的挂死

- **事实**：round 1 第 7 条变异（R8，改 `subscribe` 里快照消息的 `ordinal`）使整套 30 条用例**挂死 >240s**（harness 在 900s 处首次超时，改 harness 后又以 240s 复现，故区间是「>240s 且 ≥900s 未收尾」），pytest 进度行停在 `.........FFF.F.....F.`（22 个测试已完成：全部通过点 + 4 个 `F`）。同一变异在 `diag.py` 单跑时 `30 passed in 19.79s`；随后我又跑了 3 次完整 C04 套件（20.4s / 20.4s / 20.3s，全绿）与 5 次 L3 单测（0.56–0.75s，全绿），**均未复现**。
- 该进度行同时暴露 4 个 `F`：其中 `test_non_prefix_facts_are_marked_as_such_in_the_dto` 的失败可用 R8 之外的因素解释（R8 不改 runs），故我对这 4 个失败**不做归因**。
- **诚实结论**：**UNVERIFIABLE**。我无法把这次挂死归因到具体用例或具体机制；它更像测试装置（`asyncio.run` + 真实 `Application` + `_GatedAgent` 门 + 后台 pump 任务）在某种调度下的偶发，而不是 R8 的确定性后果。建议：给 L3 用例与 `_collect`/`_next`/`_take` 类辅助统一加硬超时，并把挂死风险作为一个已知残余记录在 `implementation-status.md` §7。

### F10（P3）`_finish` 非幂等；`_CLOSE` 会被入队两次

- **事实**：`_overflow` 自己调用 `_finish(state)`（第 793 行），随后 `_pump` 的 `finally` 又调用一次 `_finish(state)`（第 845 行）。实测队列内容：
  ```
  queue after close : [..., ('resync_required',None), '_CLOSE', '_CLOSE']
  ```
- **影响**：无害（`__anext__` 遇到第一个 `_CLOSE` 就 `StopAsyncIteration`，第二个永远看不到），但「关闭恰好一次」在实现层面不成立，未来若有人改为「排空所有消息后再判断」就会踩到。
- **建议**：`_finish` 加 `state.subscribed`/`closed` 幂等保护，或让 `_overflow` 不直接 `_finish`。

---

## 4. 无鉴别力声明（我写的变异里没能变红的部分，逐条如实列出）

我共**完成** **41 条变异**的注入：主体批次 40 条（父代理命名 8 条 + 自选 32 条：round 0 = 8，round 1 = 8，round 2 = 10，round 3 = 8），另加 M14 的专用诊断注入 4 次，以及写完报告后为核实 §1 断言 8 第 3 点的推演而追加的 1 条 `M3'`（丢弃最新已排队条目）。总计 **DETECTED 26 / SURVIVED 15 / UNEXPECTED-RED 0（阴性对照恒绿）**。

分轮计数（DETECTED/SURVIVED）：

```
round 0（父代理 8 条）     8 / 0
round 1（自选 8 条）       4 / 4      + R8 一次以 240s 挂死收场（见 F9）
round 2（自选 10 条）      5 / 5
round 3（自选 8 条）       2 / 6
诊断注入（M14）            1 / 0      存活表里的 M14 指同一变异的另一次注入
追加验证（M3'）            1 / 0      为核实 §1 断言 8 第 3 点
--------------------------------------------------------------
合计                      26 / 15  = 41 条
```

（未计入这 41 条的三类运行：一次 `MUTATION NOT FOUND` —— 我自己写错目标文件、未注入；一次 S4 挂死 —— 按退出码属 detected，但因**未拿到 FAILED 归因**故不计入 26 条的归因统计，其归因列为 OPEN；以及 R8/M14/L3 的重复确认跑。整场审查共执行约 60 次 pytest，其分布为：阴性对照 15 次、41 次变异各 1 次、M14/R8/L3 重复确认约 6–8 次。）

**存活（无鉴别力）的 15 条**，以及我判定它们各自的性质：

| # | 变异 | 判定 |
| --- | --- | --- |
| M14 | 草稿 `revision` 改读 `fragment_count` | **等价变异**（经公开 API 不可区分，§2 怀疑 5 已独立推导；我未构造「首片 `partial_seq > 1` 直写 store」的路径，故只声明公开 API 范围） |
| S1 | `_storage_key` 去掉 `stream_key` 兜底 | **不可达**（`derive_stream_key` 对任何 `invocation_id` 非空的记录都返回非空串） |
| T3 | 去掉 `source_expired` 过期码 | **死路径**（`state.expired` 与 `state.closed` 同时置位，`subscription_closed` 先命中） |
| T4 | `subscribe` 不再拒绝 cursor | 防御性检查，**无 oracle** |
| T8 | `_read_drafts` 保留首行而非新 revision | **不可达**（store 的 `read_runtime_stream_partials` 每个 `stream_key` 至多一行，去重循环从不生效） |
| R4 | 去掉 `_drain` 的 `not overflowed` 守卫 | **不可达**（同 S2 的死分支，两种改法都无影响） |
| S2 | 删除 `_drain` 的静默丢队列头分支 | **死代码**（删掉后全绿，与 F6 一致） |
| R3 | 去掉 `cursor_ahead_of_source` | **无 oracle**（未构造该类游标） |
| R7 | `GuiCursor.to_dict()` 去掉 `partial_versions` | **无 oracle**（`to_dict` 零用例） |
| R6 | 快照消息 `ordinal` +1 | **无 oracle**（F3） |
| S6 | `_read_pending` 保留非 pending | **无 oracle**（替身只造 pending） |
| S8 | `_read_runs` 不排序 | **无 oracle / 无法区分**（run 顺序恰好一致） |
| S9 | `_read_pending` 不排序 | **同上** |
| T1 | 未知订阅的 `unsubscribe` 返回 `unsubscribed` | **无 oracle**，且是 spec 场景（F5） |
| T2 | `resume` 去掉「已订阅」闸门 | **无 oracle**（F5） |
| T7 | `snapshot` 不校验 `projection_version` | **无 oracle**（F5） |

（表内 16 行 = 15 个独立存活变异 + 1 行重复计数：R4 与 S2 是同一条分支的两种改法；另 R6 与 F3 同源。故实际独立缺口数少于行数。）

**我对自己判据出过的错，如实记录**：

1. **我在写这份报告时差点误报一条**：起草时我把 S5（`_enqueue_draft` 去掉同 key 合并回退）写进「存活」表，并顺手编了一段「该用例其实没有鉴别力」的解释。回查 round 2 原始输出后确认：S5 **实测 `1 failed, 29 passed`（exit 1）**，是被 `test_draft_revision_at_the_bound_replaces_instead_of_appending` 抓住的 **DETECTED** 变异。原因也清楚：用例让 buffer 内同 key 草稿序列为 1、2、3，S5 让第 4 版走 `_trim_front` + append ⇒ buffer 变成 `[2,3,4]` 而断言 `== [1,2,4]` 失败。**该行已从存活表删除**；记录此事，因为它正是「先有结论、再补解释」这一最危险的失效模式。
2. 我把「有鉴别力」的判据定义为「至少一条用例变红」，这对 S5 这类单点断言成立，但对「丢最新」那类断言会给出**过强的名分**——§1 断言 8 第 3 点已用对照实验说明：「丢最新」只被两条被点名用例中的一条真正抓住，而矩阵把两条并列，掩盖了第一条无鉴别力。

**未采用的弱化手法声明**：本报告中所有「DETECTED」都指该变异使 pytest 退出码非 0 且至少一条用例进入 FAILED；挂死（exit=-999）单独标注，不与 FAILED 混计。没有任何一条结论建立在「看起来对」之上。

---

## 5. 我未能验证的部分

1. **跨进程/跨线程线性化**：本 Change 明确声明单进程、单事件循环，C05 才做 wire 与跨进程。我未构造多进程竞争，也未验证 `resume` 的 `service_epoch` 判据在序列化场景下的实际行为（只做了静态阅读 + 逻辑推断）。
2. **`GuiMessage`/`GuiCursor` 的 wire 编码**：spec 明确不属本 capability。F3（快照消息 ordinal 无断言）的实际后果取决于 C05 如何使用该字段，我只指出「字段值可任意错位而测试全绿」。
3. **F1 的修复方向**：我只证明「现状同时违反 spec 且自相矛盾」，未验证任何修法（把 runs 前缀化 vs 把 runs 写进 spec 的非前缀字段）在 C03 控制面语义下是否可行。
4. **F9 的挂死机制**：UNVERIFIABLE（详见 F9），未定位到具体用例或具体调度条件。
5. **`last_ordinal` 对 partial 记录前进这一半**：仅被 L1 替身用例命中，且现有用例只断言 `high_water` 不变、`last_ordinal >= 999`，删掉自增不会变红；我未构造出真实可达的重读死循环，故断言 1 的这一半只作「代码正确、无鉴别力」记录。
6. **`test_a_new_entry_replaces_the_oldest_queued_one` 中和 `_drain` 后的时序**：该用例 `monkeypatch` 掉 `_drain` 并把 `state.task` 置 None，我复跑了它（在 P2 变异下变红、在阴性对照下变绿）但未独立核验「中和之后是否真的没有任何后台任务在搬运条目」——父代理 §8.6 记录了它自己在这点上踩过坑（中和早了会让快照送不出），我没有重复那次实验。
7. **`.gitignore`/`.gitattributes` 的仓库级策略**：`core.autocrlf=true` 且无 `.gitattributes`；我只是按工作树现状（CRLF）做字节级还原，未验证「提交后 checkout 到其它平台」的行尾行为。
8. **`openspec validate --strict` 的完整语义**：我复算到 `is valid`（exit 0）为止，未逐条核对 strict 模式实际校验了哪些约束。
9. **全量回归的 warnings 计数差异**（11 vs 12）：我复算得 11，未追查 doc 中 12 的来源。
10. **§1 断言 8 第 3 点的「M3 替代最新」对照**：我只覆盖了其中一种口径（`_trim_front` 丢弃**队列尾部**），实测只被 `test_a_new_entry_replaces_the_oldest_queued_one` 抓住。§4 若本意是「丢弃最新到达的条目」则属另一种变异，我未注入该变体；因此我不能断定 §4 的归因错了，只能断定它**未声明自己指的是哪一种**，而按「丢弃最新已排队条目」这一自然读法，它点名的第一条用例无鉴别力。
11. **`test_stalled_subscriber_...` 中 `assert state.high_water == 2` 的原意**：`_pending` 与 `high_water` 在这个场景下数值都不为 2（`_pending == 3`、`_read_drafts` 的 `last_partial_seq == 0`），我只能把它标为「疑似应为 `service._pending(state) == LIMIT` 的笔误」，无法确定作者原意。

---

## 6. 结论摘要

| 断言 | 裁决 |
| --- | --- |
| 1 `_poll_events` 水位分离 | **CONFIRMED**（P1/P4 变异均变红；`last_ordinal` 前进那一半无 oracle） |
| 2 run/pending/draft 带 exempt 标记 | **CONFIRMED**（但恰恰暴露 run 与前缀字段契约冲突 = F1） |
| 3 30 条用例全部有鉴别力 / 8 变异全红 | **部分 FALSIFIED**：点名的 8 条确实 8/8 变红；但「M14 变红」为假（实测 30 passed），且「30 条全部有鉴别力」为假（我自写的 41 条变异中 15 条存活，其中 10 条是真实覆盖缺口） |
| 4 只读边界成立 | **CONFIRMED**（SQL trace：44 条语句全为 SELECT；判据未按 spec 字面实现，见 F8） |
| 5 `Application.runs_list` 纯新增 | **CONFIRMED**（`19/0`，单一 hunk） |
| 6 `agent.py`/`__main__.py` 无 C04 内容 | **CONFIRMED**（关键词扫描空、改动逐行可解释） |
| 7 全量 562 passed / exit 0 | **CONFIRMED**（我独立重跑；doc 的 warnings=12 实为 11） |
| 8 `tasks.md` 勾选均有真实证据 | **FALSIFIED**：4.4 矩阵的 M14 行与实测相反（实测 30 passed 而记载为「红」）、同一事实跨文件三处不一致；M3 行的措辞未声明变异口径，按自然读法其点名的第一条用例经实测无鉴别力（真正抓住它的是 `test_a_new_entry_replaces_the_oldest_queued_one`） |

| 怀疑方向 | 裁决 |
| --- | --- |
| 1 非溢出静默丢最旧 / resync 迟发 | 丢弃**可达**（71 条永不投递）；通知不丢 ⇒ 未违反硬性 MUST；但 spec 场景三既有的「MUST NOT 静默消失」在一条可达路径上落空 |
| 2 `_drain` 静默丢队列头可达吗 | **FALSIFIED**：141 次插桩中前置条件 0 次成立；删除整段全绿 ⇒ 死代码 |
| 3 溢出冲洗后队列超 `SUBSCRIPTION_BUFFER_LIMIT` | **CONFIRMED**：实测峰值 `LIMIT+1`（8→9），且被测试期望固化 |
| 4 `to_dict()` 不含 `service_epoch` | **CONFIRMED**：往返即过期；不违反 spec（wire 属 C05），但零 oracle |
| 5 `revision` ≡ `fragment_count` | **CONFIRMED（等价变异）**：公开 API 路径上恒等；M14 记「红」为假 |
| 6 快照 `runs` 与 `terminals` 自相矛盾 | **CONFIRMED**，且与 spec 的前缀字段契约冲突（F1，P0） |

**P0 清单**：F1（`runs` 违反前缀字段契约 / 同一 H 自相矛盾 / 零 oracle）。
**P1 清单**：F2（不留缺口等式无等式 oracle）、F3（快照投递 ordinal 无断言）、F4（`tasks.md` 4.4 矩阵与实测相反且三处不一致）。
**P2 清单**：F5（15 条存活变异，其中 10 条为真实覆盖缺口，含 spec 场景 `unknown_subscription`）、F6（`_drain` 静默丢弃分支为死代码 + 边界口径 `LIMIT+1`）、F7（`to_dict()` 丢 `service_epoch` 且零 oracle）、F8（只读判据未按 spec 字面实现）、F9（一次不可复现的套件挂死，UNVERIFIABLE）。
**P3 清单**：F10（`_finish` 非幂等，`_CLOSE` 被入队两次）。

**是否存在 FALSIFIED 断言**：是。断言 3 与断言 8 含被证伪的成分（M14 的「红」与「30 条全部有鉴别力」均不成立）；6 个怀疑方向中怀疑 2 被证伪（`_drain` 的静默丢弃分支不可达、属死代码），其余 5 个成立。

**审查未修改任何源码或测试**：全部 41 条变异逐条按字节还原，五个交付文件的 SHA256 与冻结值逐字相同，`git status --short -uall` 与审查前一致（见 §0）。
