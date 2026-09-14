# 对抗性审查报告：add-stdio-host-protocol（C05）

result_identity: role=独立对抗性审查者（证伪导向，非确认导向）；主体=DSH 子代理会话（deepseek-flash），与实现者、与先前的复核者均为不同主体；时间=2026-09-14 00:20–01:00（Asia/Shanghai，本地）；HEAD=f06831eabf69ce76c70d174089b5dd8bbef259dd（base=f06831e）；Python=3.13.13（`D:\Anaconda\envs\py313\python.exe`）；Node=v22.21.0；npm=10.9.4；Electron=33.4.11；被审修订=C05 `server.py` sha256:0EA57A0D2B823EC2…（604 行，mtime 00:15:34）、`protocol.py` sha256:99F58AF940BD3348…（187 行，mtime 21:12:32）、`test_host_process.py` sha256:674AC8901BAE3D32…（602 行）、`test_host_protocol.py` sha256:62BA36F72C3472B2…（160 行）；C06 侧见另一份报告。

---

## 0. 审查对象在审查期间发生了变化（必须先读）

**这是本次审查最重要的一条元事实：被审文件在我审查进行中被另一个写入者改写了。**

| 文件 | 我首次读取时 | 复核时（当前） |
| --- | --- | --- |
| `src/rollo/host/server.py` | 549 行（`_store_for` 只做打开） | **604 行** sha256 `0EA57A0D2B823EC2…`，mtime **00:15:34** |
| `src/rollo/tests/test_host_process.py` | 567 行，收集 15 条 | **602 行** sha256 `674AC8901BAE3D32…`，mtime **00:15:27**，收集 16 条 |

复算命令与输出：

```
Get-Item src\rollo\host\server.py, src\rollo\tests\test_host_process.py | Select Name,Length,LastWriteTime
  server.py              Length 24684  LastWriteTime 2026/9/14 0:15:34
  test_host_process.py   Length 22219  LastWriteTime 2026/9/14 0:15:27
Get-Date → 2026年9月14日 0:27:51
```

改动内容：新增 `_session_exists()` 与 `_store_for()` 的拒绝分支（未签发的 session 不再被打开）、`_dispatch` 的 `SubscriptionError` 映射（`_subscription_error`）、以及新用例 `test_an_unissued_session_is_refused_without_creating_anything`。

**后果**：
1. 我在 00:15 之前跑出的"幽灵 session 会落盘建库"这一 P0 级发现，**在当前修订上已被修复**。我按纪律保留原始证据（它证明了该缺陷真实存在过），并在当前修订上重跑全部探针，报告以当前修订为准。
2. `implementation-status.md` 中 `server.py 549 行`、`test_host_process.py 567 行`、`收集 17 + 15 = 32 条` 三处数字**在成文时是准确的，但相对当前修订已经过时**（现为 604 / 602 / 17 + 16 = 33）。这不是作者夸大，而是文档未随 00:15 的改动更新。
3. 冻结工件是"独立审查"的前提。本次审查期间工件仍在变动，说明 C05 的交付流程**尚未把实现冻结**。这一点必须由流程所有者决定如何处理（重跑审查，或冻结后再审）。

---

## 1. 逐条裁决：16 条断言

裁决口径：`CONFIRMED` = 我用独立探针复算并得到支持；`FALSIFIED` = 有可复算的反例；`UNVERIFIABLE` = 我无法构造可复算的判据。

### 1.0 全部 16 条一览（本节给出 C05 的 1–7 详证，8–16 的详证在 C06 报告的对应小节）

| # | 断言 | 裁决 | 详证位置 |
| --- | --- | --- | --- |
| 1 | 分帧正确（半帧/多帧/UTF-8 拆分）；超限关闭并留诊断 | CONFIRMED | §1 C05-1 |
| 2 | 版本协商：`version≠1` → `unsupported_version` 且保持未初始化 | CONFIRMED | §1 C05-2 |
| 3 | stdout 只含协议帧（进程级重定向） | CONFIRMED | §1 C05-3 |
| 4 | 已声明未实现 vs 未知方法可区分 | CONFIRMED | §1 C05-4 |
| 5 | `session.list` 限定 workspace / 不返回路径 / 支持分页 | CONFIRMED | §1 C05-5 |
| 6 | 打开会话的 store 是只读的 | **CONFIRMED（三条具名性质）/ FALSIFIED（"打开即读"的强表述）** | §1 C05-6 |
| 7 | 全量回归 599 passed / exit 0 | CONFIRMED（修订 A 599；当前修订 600） | §1 C05-7 |
| 8 | 渲染进程无 Node；preload 只暴露具名方法 | CONFIRMED（性质）；交付 e2e 对 `sandbox: true` 无鉴别力 | C06 报告 §1 C06-8 |
| 9 | 每个 IPC 处理器校验 sender 与 origin；`file://` 按 scheme 判定 | CONFIRMED（两个分支均端到端实测） | C06 报告 §1 C06-9 |
| 10 | 渲染进程无法提交路径/可执行文件 | CONFIRMED（提交方向）；"只持有不透明 id"被 `workerStatus` 泄露打破 | C06 报告 §1 C06-10、§3 P1-1 |
| 11 | 每 workspace 一个 host；重复注册复用；失败条目保留 | **FALSIFIED**（一个 workspace 实测起两个 host，4/4 次复现） | C06 报告 §1 C06-11、§3 P0-1 |
| 12 | 刷新页面不中止 host 子进程 | CONFIRMED（但刷新会泄漏订阅） | C06 报告 §1 C06-12、§3 P1-2 |
| 13 | 退出时关闭全部 host，不留孤儿 | CONFIRMED（优雅退出与整树强杀均无孤儿） | C06 报告 §1 C06-13 |
| 14 | 单实例：第二次启动不起第二套 worker | CONFIRMED（行为）/ FALSIFIED（交付套件无任何覆盖） | C06 报告 §1 C06-14 |
| 15 | `typecheck`/`build`/`test`(20)/`playwright`(4) 全绿 | CONFIRMED | C06 报告 §1 C06-15 |
| 16 | C05/C06 未修改任何既有文件（除 `.gitignore` +2 行） | CONFIRMED | 本报告 §1 C05-7 与本报告 §8 |

### 1.1 C05 详证

### C05-1 分帧正确（半帧 / 一读多帧 / UTF-8 字节拆分）；超限关闭连接并留诊断 —— **CONFIRMED**

探针 `probe_host.py`（真实管道，非仓库测试装置），命令：
`& D:\Anaconda\envs\py313\python.exe C:\Users\Administrator\AppData\Local\Temp\rollo-adv\probe_host.py`

原始输出摘要：
```
PASS | A1.half frame reassembled | {"jsonrpc": "2.0", "id": 2, "result": {"workspace_id": "2bfde6148a2cd6bd", "sessions": [], "next_page_cursor": null}}
PASS | A1.multi-frame single write | ids=[3, 4, 5]
PASS | A1.UTF-8 split across writes | {"jsonrpc": "2.0", "id": 6, "error": {"code": -32602, "message": "session '中文会话' does not exist in this workspace", "data": {"code": "scope_mismatch"}}}
PASS | A1.oversize -> frame_too_large | {"jsonrpc": "2.0", "id": null, "error": {"code": -32600, "message": "frame exceeded 1048576 bytes without a newline", "data": {"code": "frame_too_large"}}}
PASS | A1.oversize -> stdout closed | trailing=b''
PASS | A1.oversize -> clean exit 0 | rc=0
PASS | A1.oversize -> stderr diagnostic | '[host] closing connection: frame exceeded 1048576 bytes without a newline\r\n'
PASS | A1.process gone after oversize | poll=0
```
超限场景用的是**默认 1 MiB 上限**（`b"x" * (1024*1024+1)`），不是缩小的测试上限；关闭后 stdout 立即 EOF、进程 exit 0、stderr 有诊断，且此后写入报 `OSError`（连接已死）。"不猜测恢复"得到直接支持。

### C05-2 版本协商 —— **CONFIRMED**

```
PASS | A2.version!=1 -> unsupported_version | {"id":1,"error":{"code":-32600,"message":"protocol version 2 is not supported","data":{"code":"unsupported_version","supported":1}}}
PASS | A2.still uninitialized after bad version | ... {"code":"not_initialized"}
PASS | A2.version=1 accepted after refusal | ... result {"protocol_version": 1, ...}
PASS | A2.session.list before initialize -> not_initialized
PASS | A2.session.snapshot before initialize -> not_initialized
PASS | A2.events.subscribe before initialize -> not_initialized
PASS | A2.events.unsubscribe before initialize -> not_initialized
PASS | A2.host.shutdown before initialize -> not_initialized
```
"保持未初始化"是用**后续调用仍被拒绝**证明的，不是看内部标志。

### C05-3 stdout 只含协议帧 —— **CONFIRMED**

两路独立证据：
1. 真实子进程：4 个场景的全部 stdout 行都是合法 JSON 对象（`PASS | A3.stdout only JSON objects | lines=3 non_json=[]`）。
2. 进程内探针 `probe_purity.py`：在 host 服务期间执行 `print("STRAY-STDOUT-MARKER")` 与 `sys.stdout.write(...)`，协议流（bytes 通道）中不出现该文本，诊断通道出现：
```
PASS | A3.stdout has no stray text | '…"next_page_cursor": null}}\n'
PASS | A3.stray stdout text landed in diagnostics | ['STRAY-STDOUT-MARKER', '\n', 'STRAY-STDOUT-WRITE\n', '[host] stdin closed\n']
```

### C05-4 已声明未实现 vs 未知方法可区分 —— **CONFIRMED**

```
PASS | A4.run.start business='not_implemented' rpc=-32601 | {"error":{"code":-32601,"message":"run.start belongs to the control slice and is not implemented in this host build","data":{"code":"not_implemented"}}}
PASS | A4.run.cancel business='not_implemented' rpc=-32601
PASS | A4.interaction.respond business='not_implemented' rpc=-32601
PASS | A4.content.read business='not_implemented' rpc=-32601
PASS | A4.session.explode business=None rpc=-32601 | {"error":{"code":-32601,"message":"unknown method 'session.explode'"}}
PASS | A4.events.nonexistent business=None rpc=-32601
PASS | A4.host.ping business=None rpc=-32601
PASS | A4.<empty> business=None rpc=-32601
```
两者 RPC 码相同（-32601），**区分点只有 `error.data.code` 的有无**。这是可区分的，但客户端必须读 `data.code`——只读 `error.code` 的客户端无法区分。这是契约本身的特性，不是缺陷，但值得在实现状态里写明。

### C05-5 `session.list` 只返回当前 workspace、不返回 canonical 路径、支持 limit/page_cursor —— **CONFIRMED**

`probe_purity.py`：
```
PASS | A5.limit=2 page1 | {"next_page_cursor": "2", sessions:[page-0,page-1]}
PASS | A5.page2 returns the rest with a null cursor | {"sessions":[page-2],"next_page_cursor":null}
PASS | A5.limit above the ceiling is clamped, not rejected | limit=100000 → 3 条
PASS | A5.limit=0 is an invalid-params error | {"code":-32602,"message":"limit must be positive"}
PASS | A5.bad page_cursor is an invalid-params error | {"code":-32602,"message":"page_cursor is not a valid cursor"}
PASS | A5.summaries carry no path | 键集合恰为 {session_id, workspace_id, status}
```
"只返回当前 workspace"由 `_rpc_session_list` 走 `Application.session_list()`（按 `context` 隔离）保证；我用键集合精确断言了摘要字段，未发现任何路径字段。

### C05-6 打开会话的 store 是只读的 —— **CONFIRMED（三条具名性质）/ FALSIFIED（强表述）**

**支持部分**（当前修订，`probe_readonly.py` + `probe_attribution.py`）：
```
PASS | A6.observation writes no control record | created=[] changed=[]
PASS | A6.path traversal session_id refused | {"code":-32602,"message":"session '../escaped' does not exist in this workspace","data":{"code":"scope_mismatch"}}
PASS | A6.no escape outside sessions/ | escaped_exists=False
PASS | A6.snapshot issues no INSERT/UPDATE/DELETE | statements=[PRAGMA foreign_keys = ON, PRAGMA busy_timeout = 2000, PRAGMA user_version, BEGIN, COMMIT, CREATE TABLE IF NOT EXISTS runtime_events (…)]
```
观察一个**已存在**的 session，canonical 行数不变（`events` 计数前后同为 1），控制记录不写。就断言原文的三条具名性质（不创建 session / 不触发恢复 / 不写控制记录）而言成立。

**反例部分（当前修订仍然成立）**：

(1) 打开 store **确实是一次写事务**。`probe_bytes2.py` 输出：
```
baseline header:      {"change_counter": 23, "schema_cookie": 21, "version_valid_for": 23, "sha": "c1db6b0b3ddfff3b"}
after plain RO select: 同 sha（普通只读打开不改字节）
after plain RW select: 同 sha（普通读写打开+select 也不改字节）
after SQLiteRuntimeStore open/close: {"change_counter": 24, "version_valid_for": 24, "sha": "9fd3f74570b85258"} differing_bytes= [27, 95]
```
即：`SQLiteRuntimeStore` 的构造走 `BEGIN … COMMIT` 的 schema bootstrap，**即使 DDL 全是 no-op 也会提交一个写事务，把数据库头的 change counter（偏移 24）与 version-valid-for（92）各 +1**。`_store_for` 的 docstring "Opening the database is a read" 与 design D6 的字面表述因此不成立。数据页、schema、行数均不变，所以这不影响"canonical 内容不因观察而改变"这条 spec 场景，但它是**每次观察都会发生的落盘写**（对只读介质、对备份/同步工具、对"零副作用"的更强期望都有意义）。探针同时确认 store **没有**以 `query_only`/`immutable` 打开（`PASS | A6.store is not opened with query_only / immutable`）。

(2) host 进程启动本身就写盘：`Application(context)` 的构造会建 `runtime/application/<workspace_id>/control.sqlite`：
```
PASS | A6.ProjectContext alone writes nothing | created=[]
PASS | A6.Application(...) creates the control store on disk | created_after_Application=['runtime\\application\\82d2b399eb974a\\control.sqlite']
```
对一个"只读客户端"来说，"连上 host"这一步并非无副作用。这是 C03 既有行为，不是 C05 引入的，但 C05 的 design D6 把它算进了"只读"叙事里。

**已修复的历史缺陷（修订 A）**：在 00:15 之前的 `server.py` 上，`session.snapshot {"session_id": "ghost-session"}` 会**创建** `runtime/sessions/ghost-session/runtime.sqlite`（163840 字节，含 10 张表的完整 schema），`session.list` 随后把它当成一个真实会话列出（`status: inspect_only`），并且调用返回**成功**（`high_water=0`）。原始证据：
```
FAIL | A6/read-only: snapshot of a ghost session writes nothing | created=['sessions\\ghost-session\\runtime.sqlite'] changed=[] replies={... "result": {"session_id": "ghost-session", ...}}
PASS | A6.ghost store carries a schema | tables=['runtime_artifacts', … 10 张表] rowcounts 全 0
PASS | A6.ghost session appears in session.list | before=['session-real'] after=['session-real','totally-made-up']
PASS | A6.renderer-named sessions create directories | created_dirs=['CON','aaaa…(200)','arbitrary-name']
```
在 C06 语境下这曾是**渲染进程可驱动的磁盘增长原语**（`sessionId` 由渲染进程任意给出）。当前修订已用 `_session_exists()` 修复，我复算确认：
```
FAIL | A6.ghost store created on disk | exists=False bytes=0        ← 探针标签为旧代码所写；exists=False 即"未创建"，是修复后的期望
FAIL | A6.renderer-named sessions create directories | created_dirs=[]
FAIL | A6.ghost session appears in session.list | before=['session-real'] after=['session-real']
```

### C05-7 全量 Python 回归 599 passed / exit 0 —— **CONFIRMED（修订 A）/ 修订 B 为 600**

修订 A（我首次运行时）：
```
$env:PYTHONPATH=…\src; & D:\Anaconda\envs\py313\python.exe -m pytest src/rollo/tests -q
599 passed, 11 warnings in 290.16s (0:04:50)
EXIT=0
```
修订 B（00:15 改动后重跑）：
```
600 passed, 11 warnings in 277.65s (0:04:37)
EXIT=0
=== hash after run === 0EA57A0D2B823EC2 / 674AC8901BAE3D32   ← 运行期间工件未再变动
```
`PytestUnknownMarkWarning: Unknown pytest.mark.timeout`（11 个 warning 的主要来源）确实存在，`implementation-status.md` §7.5 的描述属实。

---

## 2. 逐条裁决：10 个怀疑方向

| # | 怀疑 | 裁决 | 关键证据 |
| --- | --- | --- | --- |
| 1 | `assertUsablePython` 可被绕过 | **CONFIRMED（可绕过）**，但 C06 侧见另一报告：不是渲染进程可达路径 | （C06 报告 §3 P2-1） |
| 2 | `workspace_id` 可否伪造 | **CONFIRMED 安全**（C06 报告 §2-10） | 30 组伪造 id 全部 `unknown workspace`，无默认目录回落 |
| 3 | 单实例断言有无覆盖 | **FALSIFIED（无覆盖）** | C06 侧 |
| 4 | 刷新语义 / 订阅泄漏 | **CONFIRMED（存在泄漏）** | C06 侧 |
| 5 | 孤儿进程 / 孙进程 | 见 C06 报告（实测结论与我的初版推测相反） | C06 侧 |
| 6 | `not_implemented` 的稳定性与可区分性 | **CONFIRMED（可区分）**；稳定性问题成立但属声明过的切片边界 | §1 C05-4；wire 上无任何"临时/永久"提示字段，客户端只能读文档 |
| 7 | 只读断言可否被意外写入打破 | **FALSIFIED（强表述）** | §1 C05-6：ghost 建库（修订 A，已修）+ 打开即写 change counter（仍存在）+ 启动建 control.sqlite |
| 8 | e2e `no Node` 是否假绿 | **部分 CONFIRMED** | C06 侧：bridge 缺失时会红（非假绿），但 `sandbox: true` 无鉴别力（变异 M1 未检出） |
| 9 | C06 是否偷偷重定义权限语义 | **CONFIRMED 没有** | C06 侧：`desktop/src` 全域 grep 无 permission/policy/mode/approve/allow/deny 逻辑（唯一命中是"拒绝新窗口"） |
| 10 | 规格自述是否可靠 | **混合**：数字大体准确，三处过时/不准确 | 见下面"自述核查" |

### 自述核查（C05，逐项）

| 自述 | 复算 | 判定 |
| --- | --- | --- |
| `protocol.py` 187 行 | `read` 工具 → 187 行；sha256 `99F58AF940BD3348…` | 准确 |
| `server.py` 549 行 | 成文时准确；当前 604 行（00:15 改动） | **过时** |
| `__main__.py` 68 行 / `__init__.py` 38 行 | 68 / 38 | 准确 |
| `test_host_protocol.py` 160 行，收集 17 条 | 160 行；`pytest --collect-only` → `17 tests collected` | 准确（`parametrize` 展开确为 4 条） |
| `test_host_process.py` 567 行，收集 15 条 | 成文时准确；当前 602 行、`16 tests collected` | **过时** |
| `git status --short -uall` **只显示**上述 6 个新文件 | 实际还有 ` M .gitignore`（+2 行）。该句只在"限 `src/rollo/`"时成立 | **不准确**，应限定作用域 |
| "6 个变异，6/6 detected，先做阴性对照" | 我未复现其 6 个变异；但我独立验证了对应的 6 条性质全部成立 | 未独立复核（见 §6） |
| §7.5 `pytest-timeout` 未安装导致 `Unknown pytest.mark.timeout` | 全量输出确有 `PytestUnknownMarkWarning: Unknown pytest.mark.timeout` | 准确 |

---

## 3. 新发现（C05）

### P1-1【已修复，但仍需记录】未签发 session 可让 host 建库并污染会话列表
见 §1 C05-6 的历史缺陷段。修订 A 上，任意客户端（含 C06 的渲染进程，它会直接把用户/页面给出的 `session_id` 转发到 wire）可以：为任意单段名字创建 160 KiB 的 schema 完整数据库、让该名字出现在 `session.list` 里、并得到"成功"的快照响应。当前修订（`_session_exists`）已消除该行为，并补了用例。**我复核了修复本身**：逻辑为"控制面登记过 或 磁盘上已有库"，两者都是纯读。

### P1-2 声明的 cursor 续传路径对任何 wire 客户端都不可达（当前修订，未修复，且无测试）
spec（host-process）写："第一条 MUST 是快照，且其信封 `ordinal` MUST 等于快照自身的 `high_water`——客户端据此续传"，并要求"`events.subscribe` 携带的 cursor MUST 在可续传时续传"。实测（`probe_resume.py`）：
```
subscribe result   : {"subscription_id": "e6d00321738c4f92a045b3d59eb268e4", "session_id": "session-resume", "host_epoch": "da41a2d6cf1e4981b8e29a5a695a8a04", "status": "subscribed"}
snapshot result    : {"high_water": 1, "projection_version": "gui-projection-v1", "source_digest": "2b79cd…", "host_epoch": "da41a2d6…"}
snapshot envelope  : {"kind":"snapshot","ordinal":1,"key":null,"transport_seq":1,"prefix_boundary_exempt":false}
event keys         : ["host_epoch","key","kind","ordinal","payload","prefix_boundary_exempt","session_id","subscription_id","transport_seq"]
resume attempt [host_epoch as service_epoch] -> error data {"code":"cursor_expired","error_code":"service_instance_replaced","current_high_water":1}
resume attempt [guessed service_epoch]        -> 同上
resume attempt [no service_epoch key]         -> 同上
resume attempt [with empty partial_versions]  -> 同上
```
原因：`GuiCursor.service_epoch` 校验的是 `SubscriptionService` 内部随机生成的 `_service_epoch`（`subscriptions.py:636`），而 wire 上只发 `host_epoch`（`HostServer` 自己的另一个随机值），`service_epoch` 与 `partial_versions` **从未出现在任何一个响应或事件里**。因此客户端拿不到构造合法 cursor 所需的材料：**每一次诚实尝试都必然得到 `cursor_expired`，且 error_code 谎报为 `service_instance_replaced`（服务并未更替）**。三重后果：
1. spec 中"快照边界可续传"的场景无路可达；
2. `events.subscribe` 的 `cursor` 参数是"看起来有、实际用不了"的接口（对 C06 尤其重要：刷新后既不能续传也不能退订，见 C06 报告 P1-2）；
3. 无任何测试覆盖成功续传（`test_host_process.py` 只有 `test_a_malformed_cursor_is_rejected`）。我构造的 4 种 cursor 全部失败，说明这不是"缺测试"，而是"缺实现/缺 wire 字段"。

修复方向（供参考，未实施）：把 `service_epoch` 与 `partial_versions` 放进快照信封或 `events.subscribe`/`session.snapshot` 的响应，并区分"客户端没有 epoch"与"服务被替换"两种错误码。

### P1-3 `_subscription_error` 用**异常消息子串**决定业务码（当前修订新增，脆弱耦合）
`server.py:281-291`：
```python
if "does not exist" in str(error) or "no control store" in str(error):
    return ProtocolError(str(error), code=INVALID_PARAMS, business="scope_mismatch")
return ProtocolError(str(error), business="runtime_unavailable", code=INTERNAL_ERROR)
```
判定依据是 C04 模块抛出的**英文消息文本**。C04 改一次措辞（例如本地化、或改成 `no such session`），这条映射就会静默退化为 `runtime_unavailable`，而客户端据此做出的"要不要重试/要不要重建订阅"决策会随之改变。新用例 `test_an_unissued_session_is_refused_without_creating_anything` 恰好钉住了这两个字面量，所以改动会被发现——但耦合本身应换成结构化判据（专用异常类型或 `error_code` 字段）。

### P1-4 "观测只读"缺少面向落盘的 oracle（当前修订）
spec 场景"canonical 内容与控制记录不因观察而改变"很窄，因此现有用例（含新增的 unissued 用例）足以覆盖它。但 design D6 与 `_store_for` docstring 表达的是更强的"打开即读"，而这条强表述**没有任何测试**：没有用例对已存在会话的库做哈希/行数断言，也没有用例断言"连上 host 不写 control.sqlite"。P2-1 的两个实际写点（change counter、启动建 control store）因此长期不可见。建议按"要么改表述、要么以 `query_only` 打开并在启动时不建控制库"二选一。

### P2-1 design D8 "协议本身没有暴露缺口"是过度概括
D8 断言"真实消费过程中发现的问题全部在壳这一侧，协议层一个都没有"。本次独立审查在同一切片上找到两个**观测层**缺口：未签发 session 建库（修订 A，已修）与 cursor 续传不可达（当前修订，未修）。前者是协议切片自身的语义漏洞（"读"创造了被读对象），后者是 wire 字段缺失。D8 的结论应改写为"当时未发现"，而不是"没有"。

### P2-2 `implementation-status.md` 三处数字过时、一处作用域不准确
见 §2 "自述核查"表：`server.py` 549→604、`test_host_process.py` 567→602、收集 32→33、"只显示 6 个新文件"（实际含 ` M .gitignore`）。

### P2-3 声明的上限常量大多无处生效
`TEXT_DELTA_BYTES`、`MAX_PAGE_ITEMS`、`PAGE_BODY_TARGET_BYTES`、`PREVIEW_BYTES`、`BODY_PAGE_DEFAULT_BYTES/MAX_BYTES` 在 `protocol.py` 中声明并被 `__init__.py` 导出，但本切片没有任何方法使用它们（`content.read` 是 `not_implemented`）。`host.initialize` 的 `limits` 只回 `frame_bytes` 与硬编码的 `page_items: 100`。这不算缺陷（切片边界已声明），但 tasks 1.1 把它们列为"与任务卡 §3.1 逐项一致"的证据，容易被读成"已生效"。

---

## 4. 变异测试（我自己做的）与鉴别力

我做了 3 个变异，全部针对 C06（见 C06 报告 §4），C05 侧未做源码变异——因为 C05 的关键性质我用**黑盒 wire 探针**直接观测，比变异-测试更直接。真实产出如下：

| 探针 | 是否区分出问题 |
| --- | --- |
| `probe_host.py`（版本闸门/初始化闸门/超限/控制方法/未知方法） | 是：这些性质全部被正面观测到，无一条依赖"测试通过"来推断 |
| `probe_readonly.py` + `probe_attribution.py` | 是：区分出了"打开即写 change counter"与"未签发 session 建库"（后者随后被修复） |
| `probe_bytes2.py` | 是：把写点定位到 `SQLiteRuntimeStore` 构造，并排除了"普通只读/读写打开也会改文件"这一替代解释 |
| `probe_resume.py` | 是：区分出"续传不可达" |
| `probe_delivery.py` | 是：验证了空闲期投递、退订后无投递、陈旧订阅继续投递 |

---

## 5. 无鉴别力声明（我的探针里不区分问题的部分，如实列出）

1. **`probe_host.py` 的"半帧"阶段没有非阻塞窥视能力**：我只能在第二次写入后断言"恰好解出一帧"，无法观测"换行到达前不产生帧"。因此"半帧不提前成帧"这条依赖仓库单测（`test_a_frame_without_a_newline_over_the_limit_is_rejected` 等）与源码阅读，我的探针**不构成**该性质的正向证据。
2. **`probe_readonly.py` 的 "A5.session.list carries no path" 断言写错了**：我检查的是子串 `"sessions"`，而它本身就是 payload 的字段名，所以该断言**必然失败**，没有任何鉴别力。路径不泄露这一性质是靠 `probe_purity.py` 的**精确键集合断言**（`{session_id, workspace_id, status}`）确认的。
3. **`probe_readonly.py` 的 ghost 段标签是为旧代码写的**：修复后 `exists=False`、`created_dirs=[]` 会让这些行显示为 `FAIL`，实际含义是"未创建"，即修复生效。我按原始输出重新解释了这些行，未修改探针标签（避免出现"为让结论好看而改探针"）。
4. **`probe_attribution.py` 的 "A6.a bare sqlite3 open+select also rewrites the file" 探针设计错误**：它比较的是"同一个只读打开前后"的哈希（`before=14ef1de77e982ccc after=14ef1de77e982ccc`），而我原本想排除的是"任何打开都会改文件"。正确的对照在 `probe_bytes2.py`（普通只读/读写打开+select 均不改字节）。前者无鉴别力，后者的结论才被采信。
5. **`probe_subscription_table_growth` 只证明了"host 会继续投递陈旧订阅"**（3 个未退订订阅各自投递），并未测量 host 侧订阅表的内存/句柄增长——"表增长"是从"每条陈旧订阅仍有独立 pump 任务"推断的，不是直接测量。
6. **我用 `Get-Content | Measure-Object -Line` 得到过 153/482/54/35 行的错误数字**：它把空行计为 0，因此与"真实行数"不符。文档里的 187/549 才是真行数（我用 `read` 工具复核过）。**错的是我的第一次测量，不是文档**。
7. **"超限即关闭"我用的是默认 1 MiB 上限**，没有测"恰好等于上限"的边界（见 §6）。

---

## 6. 我未能验证的部分

1. **帧长恰好等于 `MAX_FRAME_BYTES` 的边界行为**：`feed()` 用的是 `>`，因此等于上限应当放行；仓库用例覆盖的是"缩小上限后 1 字节超限"和"超长无换行"，我没有在真实管道上构造"长度恰为 1 MiB 的一帧"。该边界**未经实测**。
2. **成功续传（`status: "resumed"`）**：我构造的 4 种 cursor 全部失败；我没有通过进程内直接调用 `SubscriptionService.resume()` 去验证"若客户端真能拿到 `service_epoch`，续传是否工作"。因此我的结论严格限定为"**wire 客户端无法到达该路径**"，而不是"续传实现本身有 bug"。
3. **`host.shutdown` 的"停止接收新的 start 语义请求"**：本切片没有任何控制方法可接收，这半句在观察切片里是空条件，无法验证。
4. **`implementation-status.md` §4 的 6 个变异（6/6 detected）**：我没有复现那 6 个变异注入；我验证的是对应的 6 条**性质**（版本闸门、帧上限、初始化闸门、stdout 重定向、控制/未知方法区分、摘要不含路径），全部成立。他们的变异结论我既不能确认也不能否认。
5. **C04 `SubscriptionService` 的续传语义**（缓冲覆盖、`projection_version` 一致性、`service_epoch` 更替）属于 C04 范围，我只看它在 C05 的可达性，没有独立验证 C04 自身的正确性。
6. **`events.subscribe` 的 `cursor` 与 `session.snapshot` 的 `source_digest`/`high_water` 之间的一致性**（例如 `source_digest` 是否真的可用于校验续传边界）未验证。

---

## 7. 复算命令清单（C05 部分）

```powershell
# 修订快照
git rev-parse HEAD                                          # f06831eabf69ce76c70d174089b5dd8bbef259dd
Get-FileHash src\rollo\host\server.py -Algorithm SHA256      # 0EA57A0D2B823EC2…
Get-Item src\rollo\host\server.py | Select LastWriteTime     # 2026/9/14 0:15:34

# 全量回归
$env:PYTHONPATH="D:\PycharmProjects\pythonProject\Rollo-Code\src"
& D:\Anaconda\envs\py313\python.exe -m pytest src/rollo/tests -q
# 修订 A: 599 passed / exit 0 ；修订 B: 600 passed / exit 0

# 收集数
& D:\Anaconda\envs\py313\python.exe -m pytest src/rollo/tests/test_host_protocol.py --collect-only -q   # 17
& D:\Anaconda\envs\py313\python.exe -m pytest src/rollo/tests/test_host_process.py  --collect-only -q   # 16

# 独立探针（脚本在 %TEMP%\rollo-adv\，审查结束后已随临时目录留在仓库之外）
& D:\Anaconda\envs\py313\python.exe probe_host.py
& D:\Anaconda\envs\py313\python.exe probe_readonly.py
& D:\Anaconda\envs\py313\python.exe probe_attribution.py
& D:\Anaconda\envs\py313\python.exe probe_delivery.py bytes live grow trace
& D:\Anaconda\envs\py313\python.exe probe_bytes2.py
& D:\Anaconda\envs\py313\python.exe probe_purity.py
& D:\Anaconda\envs\py313\python.exe probe_resume.py
```

---

## 8. 还原证据（我未改动任何仓库源码/测试）

审查期间我只临时创建/修改过 **C06 的 3 个文件**（`desktop/src/main/index.ts`、`desktop/src/main/ipc.ts` 做变异测试；`desktop/tests/**` 下的临时探针），全部已还原/删除；**C05 的 4 个源文件与 2 个测试文件我一个字节都没有改**（它们在此期间被另一个写入者改过，见 §0）。

还原后的实测状态：
```
cd D:\PycharmProjects\pythonProject\Rollo-Code
git status --short -uall
 M .gitignore                     ← 唯一被修改的既有文件（+2 行忽略项）
?? desktop/…                      ← C06 新增
?? openspec/changes/add-electron-desktop-shell/…
?? openspec/changes/add-stdio-host-protocol/…
?? src/rollo/host/…               ← C05 新增
?? src/rollo/tests/test_host_*.py
git diff --stat
 .gitignore | 2 ++
 1 file changed, 2 insertions(+)
```
`git status --short -uall | Where-Object { $_ -match '^ ?M' }` 只有一行 ` M .gitignore`；不存在其它 ` M ` 条目。`desktop/tests` 下所有 `zz-*` 探针文件已删除（`Get-ChildItem -Recurse -Filter "zz-*"` 输出为空），`desktop/src/main/index.ts` 恢复为 sha256 `1155C07AA0BEA6D6…`（与变异前一致），`desktop/src/main/ipc.ts` 恢复为 sha256 `E539EF23244B594C…`（该文件我在变异前未取哈希，故以"逐字还原 + 复原后 typecheck/build/vitest/playwright 全绿"为证）。还原后复跑：`vitest 20 passed`、`playwright 4 passed`、`npm run build` exit 0。

**我没有执行任何 commit / push / PR 操作。**

（§0 提到的另两个写入者改动 `src/rollo/host/server.py`、`src/rollo/tests/test_host_process.py` 不是我做的——时间戳 00:15:27/00:15:34，我当时正在跑探针；我的任何写操作都只发生在 `desktop/` 与 `%TEMP%\rollo-adv\`。）
