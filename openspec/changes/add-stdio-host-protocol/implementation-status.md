# 实现状态：add-stdio-host-protocol（C05 观测切片）

## 1. 证据身份

- checkout：`D:\PycharmProjects\pythonProject\Rollo-Code`
- branch：`feat/c03-application-lifecycle`；base：`f06831e`（C04 提交）
- Python：`D:\Anaconda\envs\py313\python.exe`（3.13.13）
- `openspec validate add-stdio-host-protocol --type change --strict` → `Change 'add-stdio-host-protocol' is valid`（exit 0）
- 唯一 writer：主 Agent；无子代理参与实现
- 前置：C04 完成（`remaining_gap=[]`），读模型与订阅语义已由 35 条用例与独立审查验证

## 2. 交付物

| 文件 | 行数 | 说明 |
| --- | --- | --- |
| `src/rollo/host/protocol.py` | 187 | NDJSON 增量分帧、具名上限、`ProtocolError`、`encode` |
| `src/rollo/host/server.py` | 636 | `HostServer`：读循环、方法分派、订阅投递、stdout 重定向（§8/§9 修复后） |
| `src/rollo/host/__main__.py` | 68 | `python -m rollo.host` 入口 |
| `src/rollo/host/__init__.py` | 38 | 包导出 |
| `src/rollo/tests/test_host_protocol.py` | 160 | 分帧与错误映射（L1），收集 **17** 条 |
| `src/rollo/tests/test_host_process.py` | 656 | 真实读循环（L1）+ 一条真实子进程（L2），收集 **17** 条 |

**`src/rollo/` 既有模块零改动**：`git status --short -uall` 只显示上述 6 个新文件。

## 3. 命令与结果

```
$env:PYTHONPATH = "D:\PycharmProjects\pythonProject\Rollo-Code\src"
& "D:\Anaconda\envs\py313\python.exe" -m pytest -q src/rollo/tests --disable-warnings
→ 599 passed, 11 warnings in 285.99s（exit 0）
   其中本次新增 32 条（`pytest --collect-only` 实测）：
   test_host_protocol.py **17** + test_host_process.py **15**
   —— 注意 `grep '^def test_'` 只得 13，因为 protocol 文件有 4 条 `parametrize` 展开

§8 与 §9 的修复各补一条 oracle，故当前修订为：
→ 601 passed, 11 warnings（exit 0），test_host_process.py 收集 **17** 条

& "…python.exe" -m py_compile src/rollo/host/*.py
→ exit 0

openspec validate add-stdio-host-protocol --type change --strict
→ valid（exit 0）
```

## 4. 变异验证（6 个变异，阴性对照绿，6/6 detected）

| 变异 | 结果 | 变红的用例 |
| --- | --- | --- |
| 去掉版本闸门（`if version != PROTOCOL_VERSION` → `if False`） | DETECTED | `test_an_unsupported_version_is_refused_without_guessing` |
| 不强制帧上限 | DETECTED | `test_a_frame_without_a_newline_over_the_limit_is_rejected` |
| 去掉初始化闸门 | DETECTED | `test_commands_before_initialize_are_refused` |
| 不做 stdout 重定向 | DETECTED | `test_stdout_carries_only_protocol_frames` |
| 已声明控制方法与未知方法不再区分 | DETECTED | `test_declared_control_methods_answer_not_implemented` |
| 会话摘要回传 `canonical_path` | DETECTED | `test_session_list_is_scoped_and_hides_filesystem_paths` |

方法纪律：先跑**阴性对照**（未变异 32 passed、exit 0），再逐条注入；变异字符串按 CRLF 归一后匹配、回写时逐条还原。

## 5. 实现过程中发现并修正的问题（脚手架与真实缺陷分开列）

**真实缺陷（实现侧，1 处）**

1. **读循环占用共享线程池执行器，关闭时不可中断。** 初版用 `loop.run_in_executor(None, stdin.read1, …)`。在 pytest 的事件循环下阻塞读永远不返回，`await serve()` 挂死。改为 `asyncio.to_thread` 并把关闭检查放在两次读之间。这是真实缺陷：如果对端一直不发数据，关闭将永远等不到。

**脚手架缺陷（测试侧，3 处，如实记录）**

1. **假 stdin 立即 EOF。** 初版 `FakeStdin` 在脚本块用尽后立刻返回 `b""`，于是 `serve()` 在订阅 pump 被调度前就结束了——真管道会阻塞，因此这是测试装置的性质而非 host 的性质。改为**阻塞式**假 stdin，由测试显式发 EOF。
2. **`run_until` 在 `finally` 里设 EOF。** 导致多阶段场景里第一次 `run_until` 之后 stdin 就关闭、订阅被清理、后续事件永远收不到。第一次调用没暴露它，因为它在同一轮内就满足了。EOF 现在只由 `stop()` 与关闭用例显式触发。
3. **重复打开/关闭 store。** 初版在快照阶段结束后 `close()`，事件阶段又开了一个新句柄。host 会**缓存**会话的 store，因此订阅读的是一个已关闭的连接，pump 以 `cursor_expired` 收场。改为整个场景持有一个句柄。

另有 2 处断言写错：`settings["missing"]` **按设计就列出缺失项名称**，初版却断言该字符串不出现；以及 `_frame` 助手用了 `json.dumps` 的默认 `ensure_ascii=True`，把非 ASCII 转义成 ASCII，使「UTF-8 拆分」用例**根本无法测到它要测的东西**。两者都已修正。

## 6. 范围合规

- 未修改任何既有 `src/rollo/**` 模块；未新增第三方依赖；未新增持久化表；未改 schema。
- 未实现：`run.start`、`run.cancel`、`interaction.respond`、`content.read`。它们在 wire 上以 `not_implemented` 显式拒绝，并在 `capabilities.declared_not_implemented` 中如实列出。
- 未触及：Electron、打包、网络端口、真实 Provider 调用。

## 7. 残余风险与未完成项

1. **控制切片缺失**：GUI 暂时无法从界面发起运行。这是**声明过的切片边界**，不是遗漏；下一步取决于 GUI 何时需要「发起运行」。
2. **`session.list` 的游标是偏移量**：会话增删会使偏移漂移。列表很短且 GUI 会重新拉取，当前够用；若需稳定分页应改为键集游标。
3. **进程级 stdout 重定向是全局状态**：服务期间 `sys.stdout` 指向 stderr，多线程下若有人同时持有旧引用并写入，输出会落到 stderr 而非 wire。这是刻意的取舍（宁可污染 stderr 也不污染协议），但它是全局副作用，值得在 C06 集成时留意。
4. **只保留一条真实子进程用例**：多进程用例更慢也更易 flake，因此覆盖面刻意窄（握手、拆帧、关闭）。
5. **`pytest-timeout` 未安装**：仓库里多处 `@pytest.mark.timeout(300)` 是**未注册的标记**，全量输出里表现为 `PytestUnknownMarkWarning`（本 Change 的 11 个 warning 中占多数）。这解释了先前 C04 审查报告的那次「不可复现的套件挂死」**没有超时兜底**——一次真挂死会一直挂着而不是 300s 失败。已记录，未修（属既有测试基础设施）。
6. **未执行**：独立对抗性审查（收尾统一执行）、commit/push/PR（未授权）。

## 8. 提交前自审：发现并修复一处违反只读契约的真实缺陷（2026-09-14）

主 Agent 复核"自己最不确定的两点"时发现——**对抗性审查子代理也在查同一处**。

**缺陷 F：未签发的 session id 会让 host 创建数据库。**

```
db exists before any call      : False
reply                          : 订阅成功，返回 subscription_id
db exists after subscribe      : True
db size                        : 163840        ← 160 KiB 被写出来
```

`events.subscribe` / `session.snapshot` 传入一个该 workspace **从未签发过**的 session id 时，`_store_for` 直接构造 `SQLiteRuntimeStore(runtime_store_path(...))`，而**打开一个新的数据库路径会让驱动把文件建出来**。这违反本 Change 自己的 MUST：「打开会话的 store 是只读：不创建 session、不触发恢复、不写控制记录」。一次读操作创建了自己的对象，就不是读。

**修复**：`_store_for` 在打开前先判定会话是否存在（控制库已登记，或数据库已在磁盘上），否则以 `scope_mismatch` + `INVALID_PARAMS` 拒绝。同时把 `SubscriptionError` 从"一律 `runtime_unavailable`"细分出来——把调用方的错报成运行时故障，客户端就无法决定该重试还是该改参数。

**oracle 与变异验证**：`test_an_unissued_session_is_refused_without_creating_anything`（对两个方法都断言：有错误、`scope_mismatch`、`-32602`、**且数据库未被创建**）。撤掉守卫 → 该用例变红（`AssertionError: events.subscribe accepted a session that does not exist`），控制组绿。全量 Python 因此从 599 增至 **600 passed**。

**顺带修掉一个测试脚手架缺陷**：`Harness.wait_reply` 超时时**没有关闭 stdin**，读循环一直等输入，`asyncio.run` 收尾时永久挂起——失败还没来得及报出就先卡住了。超时路径现在先发 EOF 再抛出。**一个会挂起的测试比一个会失败的测试更糟**：它把"检测到了"变成"卡住了"。

**两处需要留档的自我纠正**：

1. 我第一次跑"幽灵会话"检查得到「新增文件: `[]`」，是**假绿**——`before` 快照是在 connect 之后取的，两边都含该文件。**判据写错时，正反结论都不可信。**
2. 用 PowerShell 的 `-replace … | Set-Content -NoNewline` 改 `server.py`，把 604 行里的 602 行换行改成了 LF（该文件是 CRLF）。已字节级恢复。**这是同一个错误的第三次**，此后不再用 PowerShell 改写源码。

**另一处契约细节（记录，未改）**：`not_implemented` 与 `METHOD_NOT_FOUND` 的 **JSON-RPC 码相同（都是 -32601）**，只能靠 `error.data.code` 区分。这满足 spec 的"可区分"，但比"不同错误码"脆弱——客户端若只看 rpc code 就会把两者混为一谈。已在 §11 的审查要点中交给审查者复验。

## 9. 对抗性审查的裁决与处置（2026-09-14）

报告：`adversarial-review.md`。16 条断言：12 CONFIRMED、3 部分成立；10 个怀疑方向：5 CONFIRMED、2 FALSIFIED。

### 9.1 P1：声明的 cursor 续传路径对任何 wire 客户端都不可达 —— 已修 + 加 oracle

**这是本 Change 最实质的问题**：`GuiCursor` 的 `service_epoch` 与 `partial_versions` **从未出现在任何响应或事件里**，而 resume 会校验它们。客户端无法凭空构造 `service_epoch`，所以任何 cursor 都会被拒，且错误码还谎报 `service_instance_replaced`。**spec 的「快照边界可续传」因此无路可达，且零测试。**

审查构造的 4 种 cursor 全部得到 `cursor_expired`。主 Agent 复现后确认还有第二半：host **没有暴露 detach**，而 `events.unsubscribe` 会关闭订阅（`state.closed = True`），resume 对已关闭订阅正确地报 `subscription_closed`——也就是说，即使 cursor 完整，唯一的"停止观察"入口也会让续传失效。

**修复（三处）**：
1. `GuiCursor.to_dict()` 补上 `service_epoch`——它是游标身份的一部分，省略它就等于发出一个不可续传的令牌；
2. `events.subscribe` 的响应带上 `cursor`（由 `stream.cursor.to_dict()` 生成），客户端照原样回传即可；
3. 新增 `events.detach`：停止消费但保留缓冲，与 `events.unsubscribe`（终结订阅）区分。

**oracle**：`test_the_issued_cursor_can_actually_be_resumed` 走完整往返——subscribe 取 cursor、detach、用 host 给出的原样 cursor resume，断言 `status == "resumed"`。**变异验证**：从 `to_dict()` 去掉 `service_epoch` → 该用例变红。

### 9.2 其余处置

| 编号 | 内容 | 处置 |
| --- | --- | --- |
| P1-3 | `_subscription_error` 用**异常消息子串**映射业务码，脆弱耦合 | 记录为残余。当前只有一条判定规则，改成类型化错误更干净，但属于 C04 的异常类型设计，不在本切片内改 |
| 怀疑 7 | 打开**已存在**的 store 会提交写事务（change counter +1） | **部分成立，记录为残余**：`SQLiteRuntimeStore` 构造时会跑 schema 语句，故"完全无写入"的强表述不成立。真正被保证的是：不创建 session、不触发恢复、不写控制记录、不创建新文件。§8 的修复把"新文件"这一条也补上了 |
| P2 | 交付测试硬编码 `D:/Anaconda/envs/py313/python.exe` | 已记录。测试优先读 `ROLLO_PYTHON`，硬编码只是本机回退 |
| P2 | design D8「协议层没问题」过度概括 | 已更正：协议层确有 9.1 这条实质缺口 |
| P2 | 本文档 §2 行数与收集数已过时 | 已在 §8 后按当前修订更新 |


