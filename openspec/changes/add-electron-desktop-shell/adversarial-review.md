# 对抗性审查报告：add-electron-desktop-shell（C06）

result_identity: role=独立对抗性审查者（证伪导向，非确认导向）；主体=DSH 子代理会话（deepseek-flash），与实现者、与先前的复核者均为不同主体；时间=2026-09-14 00:20–01:10（Asia/Shanghai，本地）；HEAD=f06831eabf69ce76c70d174089b5dd8bbef259dd（base=f06831e）；Python=3.13.13（`D:\Anaconda\envs\py313\python.exe`）；Node=v22.21.0；npm=10.9.4；Electron=33.4.11；被审修订=`desktop/src/main/index.ts` sha256:1155C07AA0BEA6D6…（142 行）、`ipc.ts` sha256:E539EF23244B594C…（187 行）、`workspaces.ts` sha256:A1BF54E91B2683F3…（149 行）、`host-client.ts` sha256:8E3D303E781E6369…（356 行）、`sidecar.ts` sha256:C33C7264E42ABC60…（88 行）、`preload/index.ts` sha256:14E0ADCDD214A1B5…（79 行）、`shared/wire.ts` sha256:773B4D95A7FCFFA3…（155 行）；C05 侧见另一份报告。

---

## 0. 审查对象在审查期间发生了变化（必须先读）

`src/rollo/host/server.py` 与 `src/rollo/tests/test_host_process.py` 在我审查期间（00:15:27 / 00:15:34）被另一个写入者改过（详见 C05 报告的 §0）。C06 的 7 个文件在我审查期间**未**被第三方改动（mtime 为 23:30–23:50；00:26 的两次 mtime 是我做变异测试后逐字还原造成的）。

C06 与 C05 的耦合点是 host 的 wire 行为；我下面对"续传不可达"等结论的复算是在**当前修订**上做的。

---

## 1. 逐条裁决：16 条断言

裁决口径：`CONFIRMED` = 我用独立探针复算并得到支持；`FALSIFIED` = 有可复算的反例；`UNVERIFIABLE` = 我无法构造可复算的判据。

### 1.0 全部 16 条一览（本节给出 C06 的 8–16 详证，1–7 的详证在 C05 报告的对应小节）

| # | 断言 | 裁决 | 详证位置 |
| --- | --- | --- | --- |
| 1 | 分帧正确（半帧/多帧/UTF-8 拆分）；超限关闭并留诊断 | CONFIRMED | C05 报告 §1 C05-1 |
| 2 | 版本协商：`version≠1` → `unsupported_version` 且保持未初始化 | CONFIRMED | C05 报告 §1 C05-2 |
| 3 | stdout 只含协议帧（进程级重定向） | CONFIRMED | C05 报告 §1 C05-3 |
| 4 | 已声明未实现 vs 未知方法可区分 | CONFIRMED | C05 报告 §1 C05-4 |
| 5 | `session.list` 限定 workspace / 不返回路径 / 支持分页 | CONFIRMED | C05 报告 §1 C05-5 |
| 6 | 打开会话的 store 是只读的 | **CONFIRMED（三条具名性质）/ FALSIFIED（"打开即读"的强表述）** | C05 报告 §1 C05-6 |
| 7 | 全量回归 599 passed / exit 0 | CONFIRMED（修订 A 599；当前修订 600） | C05 报告 §1 C05-7 |
| 8 | 渲染进程无 Node；preload 只暴露具名方法 | CONFIRMED（性质）；交付 e2e 对 `sandbox: true` 无鉴别力 | §1 C06-8、§3 P1-3 |
| 9 | 每个 IPC 处理器校验 sender 与 origin；`file://` 按 scheme 判定 | CONFIRMED（两个分支均端到端实测） | §1 C06-9 |
| 10 | 渲染进程无法提交路径/可执行文件 | CONFIRMED（提交方向）；"只持有不透明 id"被 `workerStatus` 泄露打破 | §1 C06-10、§3 P1-1 |
| 11 | 每 workspace 一个 host；重复注册复用；失败条目保留 | **FALSIFIED**（一个 workspace 实测起两个 host，4/4 次复现） | §1 C06-11、§3 P0-1 |
| 12 | 刷新页面不中止 host 子进程 | CONFIRMED（但刷新会泄漏订阅） | §1 C06-12、§3 P1-2 |
| 13 | 退出时关闭全部 host，不留孤儿 | CONFIRMED（优雅退出与整树强杀均无孤儿） | §1 C06-13、§3 P2-3 |
| 14 | 单实例：第二次启动不起第二套 worker | CONFIRMED（行为）/ FALSIFIED（交付套件无任何覆盖） | §1 C06-14 |
| 15 | `typecheck`/`build`/`test`(20)/`playwright`(4) 全绿 | CONFIRMED | §1 C06-15 |
| 16 | C05/C06 未修改任何既有文件（除 `.gitignore` +2 行） | CONFIRMED | §1 C06-16、§8 |

### 1.1 C06 详证

### C06-8 渲染进程无 Node；preload 只暴露具名方法，无 `invoke(channel,…)` —— **CONFIRMED（性质成立）/ 交付 e2e 对 `sandbox: true` 无鉴别力**

真实窗口内实测（`zz-adversarial-probe.spec.ts`，Playwright + 真实 Electron）：
```
PROBE PASS | C06.1 no require/process in renderer | {"requireType":"undefined","processType":"undefined","moduleType":"undefined","globalType":"undefined","apiKeys":["autoWorkspace","chooseWorkspace","initialize","listSessions","listWorkspaces","onEvent","onWorkerState","snapshot","subscribe","unsubscribe","workerStatus"],"apiFrozen":true,"hasIpcRenderer":"undefined","electronType":"undefined"}
PROBE PASS | C06.1 bridge is a named whitelist, no generic invoke | apiKeys=autoWorkspace,chooseWorkspace,initialize,listSessions,listWorkspaces,onEvent,onWorkerState,snapshot,subscribe,unsubscribe,workerStatus
PROBE PASS | C06.1 no ipcRenderer handle | ipcRenderer=undefined electron=undefined
```
11 个具名方法、`Object.freeze`、桥对象原型上只有 `Object.prototype` 的成员，没有 `invoke`/`send`/`on`。`page.evaluate` 跑在主世界，所以 `require`/`process` 为 `undefined` 是有意义的观测。

**但**：spec 明确要求 `sandbox: true`，而交付的 e2e **测不出它被关掉**（变异 M1，见 §4）。这是断言 8 的覆盖缺口，不是性质缺陷。

### C06-9 每个 IPC 处理器校验 sender 与 origin；`file://` 按 scheme 判定 —— **CONFIRMED（两个分支都端到端验证）**

- **origin 分支**：把真实窗口导航到 `http://127.0.0.1:<port>/`（bridge 仍在，`typeof rollo=object`），再调用桥接方法：
```
PROBE PASS | C06.9 origin check rejects an http origin | rejected: Error invoking remote method 'desktop:list-workspaces': Error: rejected: sender origin http://127.0.0.1:50415 is not allowed
PROBE PASS | C06.info bridge on the foreign page | typeof rollo=object
```
- **sender 身份分支**：由主进程额外创建**第二个真实窗口**（同一 preload、同一 `file://` 文档、`contextIsolation/sandbox` 同配置），该窗口不在 `index.ts` 的 `windows` 数组里：
```
PROBE7 windows=2 extraFound=true
PROBE7 extra-window invoke -> rejected: Error invoking remote method 'desktop:list-workspaces': Error: rejected: unknown sender
PROBE7 legitimate window still works: listed=1
```
两条拒绝路径都真实触发，且合法窗口不受影响。
- **`file://` 接受路径**：由变异 M2 反证——把 `url.protocol === 'file:' ? 'file://' : url.origin` 改成字面 `url.origin` 后，交付的 `shell.spec.ts` 有 2 条用例变红（见 §4）。说明该分支被交付套件真实覆盖。
- `ipcMain` 注册面：`registerIpc` 里 8 个 `ipcMain.handle` 全部包在 `handler(context, …)` 里，而 `handler` 的第一行就是 `assertTrustedSender(event, context)`（源码复核，`ipc.ts:63-73`）。无未包装的处理器。

### C06-10 渲染进程无法提交文件系统路径/可执行文件 —— **CONFIRMED（提交方向）/ 但"只持有不透明句柄"被泄露打破（见 P1-1）**

伪造/路径型 `workspace_id` 的全矩阵实测（`zz-adversarial-probe.spec.ts`，6 个方法 × 5 个 id = 30 次调用）：
```
PROBE PASS | C06.2 every unregistered id is rejected | {"initialize:deadbeef1234":"rejected: … unknown workspace: deadbeef1234",
 "initialize:C:/Windows":"rejected: … unknown workspace: C:/Windows", "snapshot:../../etc":"…", "subscribe:CON":"…", "unsubscribe: ":"…", …}
```
全部 30 次都以 `unknown workspace`（或非空字符串校验）失败，没有任何一次落到默认目录、没有生成新条目、没有产生子进程。`workspace_id` 只能来自 `registry.add()`（唯一入口是原生对话框或主进程环境变量），`registry.get()` 对未注册 id 直接抛错。

解释器路径同理：`resolveSidecarPython()` 只读 `process.env.ROLLO_PYTHON` 与 `join(repoRoot,'.venv',…)`；渲染进程没有任何参数能进入这条路径。**"接口里根本没有那个参数"这一设计判断成立。**

反例见 P1-1（渲染进程能**读到**路径，只是不能**提交**）与 P2-1（校验强度不足）。

### C06-11 每 workspace 一个 host；同目录重复注册复用；启动失败条目保留 —— **FALSIFIED（前半句）**

**一个 workspace 会起两个 host 进程，4/4 次启动可复现。** 原始输出：

```
# zz-adversarial-probe2.spec.ts
PROBE2 FAIL | C06.11 exactly one host for one workspace | [{"ProcessId":26280,"ParentProcessId":8048,"CommandLine":"D:/Anaconda/envs/py313/python.exe -m rollo.host --workspace C:\\Users\\MuShi\\AppData\\Local\\Temp\\rollo-p2-JJRiY8"},
                                                                   {"ProcessId":31160,"ParentProcessId":8048,"CommandLine":"D:/Anaconda/envs/py313/python.exe -m rollo.host --workspace C:\\Users\\MuShi\\AppData\\Local\\Temp\\rollo-p2-JJRiY8"}]
PROBE2 FAIL | C06.11c repeated registration reuses the entry and the host | ids={"firstId":"992b5bbe68f7","secondId":"992b5bbe68f7","listIds":["992b5bbe68f7"]} hosts=2

# zz-adversarial-probe4.spec.ts（两轮独立启动）
PROBE4 | round1 host count for one registered workspace | count=2 main=15400 hosts=[{28628,…},{29280,…}]
PROBE4 | round1 registry entries vs hosts | listed=1 windows=1 hosts=2
PROBE4 | round2 host count for one registered workspace | count=2 main=11956 hosts=[{23260,…},{29912,…}]
PROBE4 | round2 registry entries vs hosts | listed=1 windows=1 hosts=2
```
注意 `listed=1 windows=1 hosts=2`：注册表只有 1 个条目、只有 1 个窗口，却有 2 个 Python 进程，命令行完全相同（含 `--workspace` 同一临时目录）。

**根因（源码可复核，位于 `workspaces.ts:93-130`）**：`connect()` 的就绪判据是
```ts
if (entry.client && entry.state === 'ready' && entry.handshake) return entry;
```
而 `HostClient.start()` 在 spawn 之后**立刻** `setState('ready')`（`host-client.ts:210`），注册表的状态监听把这个 `ready` 复制进 `entry.state`；此时 `entry.handshake` 仍为 `null`（握手要等 Python 解释器启动，实测该跨进程集成用例耗时 1330 ms）。于是**在 spawn 与握手完成之间的约 1 秒窗口内，任何第二次 `connect()` 都会通过判据、新建一个 `HostClient`、再 spawn 一个 host，并把 `entry.client` 覆盖掉**。没有 in-flight promise 去重。

触发源不止一个：`App.tsx` 的挂载 effect 依赖 `[workspace?.workspace_id]`，`autoWorkspace()` 的 `.then` 里 `setWorkspace(preset)` 会改变依赖 → effect 清理并再跑一次 → 第二次 `autoWorkspace()` + 第二次 `connect()`；`CHANNELS.listSessions` 的处理器也会先 `await registry.connect(...)`。因此两次 `initialize` IPC 在毫秒级内先后到达，而第一次还卡在握手上。

**后果**：
1. 每个 workspace 多一个常驻 Python 进程（CPU/内存、多一份 runtime 目录写入者）。
2. 被顶掉的那个 `HostClient` **从注册表里失联**：`entry.client` 指向第二个，`closeAll()` 遍历 `entries` 只能关掉第二个；第一个既没有 `host.shutdown`，也没人等待它。
3. 两个 client 的 `state`/`event` 监听器都还挂在同一个 `entry` 上：被顶掉的进程若先死，会把 `entry.state` 置为 `failed`（尽管在用的那个是健康的）。这条是**源码推断**，我没有单独去杀"被顶掉的那个"来实测（两个进程命令行相同，无法从外部区分谁是谁）——如实标注。
4. 交付的 4 条 e2e 与 20 条 vitest **全都没有数进程**，所以这个缺陷对整套测试不可见。

**其余两半成立**：重复注册复用条目（`ids.firstId === ids.secondId`、`listIds.length === 1`）为真；启动失败保留条目并带诊断也有用例（`keeps a failed worker in the table with its problem, instead of vanishing`，我复跑通过）。另外 `--workspace`/`-m rollo.host` 以参数数组、`shell:false` 传入（命令行实测 `D:/Anaconda/envs/py313/python.exe -m rollo.host --workspace …`，无 shell 拼接）。

### C06-12 刷新页面不中止 host 子进程 —— **CONFIRMED（但刷新会泄漏订阅，见 P1-2）**

交付 e2e 第 4 条用例通过（`ok 4 … a reload keeps the window working and re-attaches to the same host`）；我在探针里独立复核到的是：刷新后窗口数仍为 1，刷新后的页面能重新列出会话（`Sessions (1)` 可见）并成功 `subscribe`，而**刷新前建立的订阅仍在投递增量事件**——这证明承载订阅表的 host 进程跨刷新存活（订阅表是进程内状态，进程重启即丢失）。我没有逐字比对刷新前后的 `workspace_id`/`host_epoch` 字符串（见 §6 第 9 条），因此"重新取得**同一** workspace"这一措辞在我这里的直接证据是"旧订阅仍在投递"，而非 epoch 相等。**"不中止 worker"与"重新取得同一 host"在这个 slice 里是同一件事**，因为 `registry.connect()` 对已就绪条目直接返回。

### C06-13 退出时关闭全部 host，不留孤儿 Python 进程 —— **CONFIRMED（但"关闭全部"只对注册表可达的那个成立）**

- 正常退出（`app.close()`）后按命令行标记枚举 `python.exe`：
```
PROBE2 PASS | C06.13 graceful quit leaves no orphan host | pidBefore=26280 alive=false survivors=
PROBE4 | round1 after graceful close | alive=28628:false,29280:false listed=0   （t+1s/4s/10s/20s 四次均为 0）
PROBE4 | round2 after graceful close | alive=23260:false,29912:false listed=0
```
- 强杀整棵 Electron 进程树（Task Manager 的"结束任务"语义：按父子关系自底向上 kill 全部 electron.exe）：
```
PROBE6 | tree | electron=23820<-30836,2800<-23820,31356<-23820,31364<-23820 hosts=2504,21028
PROBE6 | kill order (children first) | 2800,31356,31364,23820
PROBE6 | after whole-tree kill | hosts=2504:false,21028:false listed=0 electron_left=0   （2s 起即全部消失）
```
即：**整树被杀时，Python host 会在 2 秒内自行退出**——因为父进程消失后它的 stdin 读到 EOF，`_read_loop` 正常收尾。这是我的探针第二次测量的结论，与我第一次的相反（见 §5 无鉴别力声明第 1 条）。
- 孙进程：host 的子进程只有 `conhost.exe`（`PROBE2 PASS | C06.info host children (grandchildren of Electron) | {"ProcessId":28556,"Name":"conhost.exe",…}`）。本切片没有任何 host 侧的 shell 执行（控制方法全为 `not_implemented`），因此**不存在"派 shell 的孙进程"可杀**；用户提示里担心的 `execution.py` 进程组问题在这个 slice 里没有触发面。
- 残余风险见 P2-3（`closeAll()` 触达不到被顶掉的 host；不留孤儿目前依赖"stdin EOF 自杀"，而这是副产品而非设计）。

### C06-14 单实例：第二次启动聚焦已有窗口，不起第二套 worker —— **CONFIRMED（行为）/ FALSIFIED（覆盖）**

行为实测（`zz-adversarial-probe2.spec.ts`，用 `require('electron')` 的真实二进制启动第二个进程，环境与第一个完全一致）：
```
PROBE2 FAIL | C06.14b second launch exits without a second worker | secondExit=0 windows=1 hosts=2 stderr=
```
第二个进程**自己以 exit 0 退出**、窗口数仍为 1、host 数不变（2 → 2；2 是 C06-11 缺陷造成的底数，不是第二次启动带来的）。即 `requestSingleInstanceLock()` 生效、没有第二套 worker。

但断言原文里的"是否有测试证明"答案是**没有**：交付的 4 条 e2e（`tests/e2e/shell.spec.ts`）分别覆盖 no-Node、握手+列表、订阅快照、刷新，**没有一条涉及第二次启动**；`vitest` 也不涉及 Electron 生命周期。这是我用临时探针补测出来的，交付物本身没有这个 oracle。

### C06-15 `typecheck` / `build` / `test`（20 passed）/ `playwright test`（4 passed）全绿 —— **CONFIRMED**

```
npm.cmd run typecheck  → exit 0（tsc --noEmit，无输出）
npm.cmd run build      → exit 0（esbuild 产出 dist/main/index.js 21.5kb、dist/preload/index.js 2.5kb；vite v6.4.3 构建 renderer，26 modules，index-BFV14XcO.js 149.05 kB）
npm.cmd run test       → Test Files 2 passed (2) / Tests 20 passed (20)，exit 0
npx.cmd playwright test→ 4 passed (4.5s)，exit 0
```
20 = `host-client.test.ts` 18 + `host-integration.test.ts` 2（后者是真 Python 子进程的跨进程集成，2.66 s）。我在还原变异后复跑同样全绿。
另注：`host-integration.test.ts` 里 `real host` 的两条用例在无解释器时会 `describe.skip`，`shell.spec.ts` 在无解释器时 `test.skip`（见 P2-2）。

### C06-16 C05/C06 都没有修改任何既有文件（除 `.gitignore` +2 行） —— **CONFIRMED**

```
git status --short -uall | Where-Object { $_ -match '^ ?M' }
 M .gitignore
git diff --stat
 .gitignore | 2 ++
 1 file changed, 2 insertions(+)
git diff -- .gitignore
+test-results/
+playwright-report/
```
不存在其它 ` M ` 条目；C05 的 6 个文件、C06 的 15 个文件全部是 `??` 未跟踪新文件。`dist/`、`node_modules/` 已由既有 `.gitignore` 覆盖，新增的 2 行让 playwright 产物也不进版本库。

---

## 2. 逐条裁决：10 个怀疑方向

### 怀疑 1：`assertUsablePython` 可否被绕过 —— **CONFIRMED（可绕过），但渲染进程不可达**

实测（临时 vitest 探针，调用真实导出函数与真实 `HostClient.start()`）：
```
PROBE3 FAIL | assertUsablePython(directory contains python, binary is not python) | path=C:\Users\MuShi\AppData\Local\Temp\rollo-p3-Ya8uUp\python-tools\notepad.exe threw=null
PROBE3 PASS | HostClient.start launches the impostor | startError=null state=ready pid=21132
PROBE3 FAIL | assertUsablePython(symlink python.exe -> where.exe) | threw=null
```
- 目录名含 `python` 的**非 Python 可执行文件**（`…\python-tools\notepad.exe`，实际复制自 `where.exe`）**通过了全部检查**，并且 `HostClient.start()` 真的把它当作 host 启动（`state=ready`、`pid=21132`）。
- 名为 `python.exe` 的**符号链接**指向另一个二进制同样通过（`/python/i` 与 `existsSync` 都只看名字与存在性）。
- 大小写不敏感：`…\PYTHON.EXE` 同样通过（Windows 文件系统大小写不敏感 + 正则带 `i`）。
- 相对路径、shell 元字符、名字不含 python 的三条检查本身有效（`C:/x/python.exe; rm -rf /` → `metacharacters`；`C:/x/notepad.exe` → `non-Python`；`python.exe` → `absolute`）。

**威胁模型结论（重要，避免夸大）**：该路径只来自 `process.env.ROLLO_PYTHON` 或 `<repo>/.venv/…`，**渲染进程无法设置环境变量，也无法提供解释器路径**。所以这不是渲染进程可利用的提权面，而是"纵深防御被写成了它并不具备的强度"：`host-client.ts:72-76` 的注释说"must contain `python` so a swapped value cannot silently become anything else"，design D2 说"文件名必须含 `python`"——实际是**整条路径的子串匹配**，任何名为 `python*` 的目录都能让任意 `.exe` 混进来。真实可控的加固手段应是校验解释器自身（例如 `spawn -c "import rollo.host"` 探针，正如 e2e 自己已经在做的那样），或改称其为"防误配"而非"防替换"。

### 怀疑 2：`workspace_id` 是否真的不可伪造 —— **CONFIRMED（不可伪造且无默认回落）**

见 C06-10 的 30 次调用矩阵。补充：`registry.require()` 对未注册 id 抛 `unknown workspace`（vitest 用例与我的探针都验证）；`listSessions` 处理器里的 `await registry.connect(id)` 也在 spawn 之前先 `get()`；不存在"取不到就用当前目录/默认工作区"的分支（`workspaces.ts` 全文无默认值）。**唯一能进入注册表目录的入口**是 `dialog.showOpenDialog`（原生选择器）与主进程环境变量 `ROLLO_AUTO_WORKSPACE`——两者渲染进程都控制不了参数（`autoWorkspace` 处理器签名不接收任何参数，实测调用不接受渲染进程传入的值）。

### 怀疑 3：单实例断言是否有覆盖 —— **FALSIFIED（无覆盖）**

`tests/e2e/shell.spec.ts` 只有 4 条用例（`grep -c "^test(" → 4`），无第二次启动相关用例；`tests/main/` 也不涉及 `requestSingleInstanceLock`。行为本身成立（见 C06-14），但"是否真的没有第二套 worker"在交付物里**只靠代码存在，没有任何 oracle**。我用临时探针补测后确认行为正确。

### 怀疑 4：刷新语义与订阅泄漏 —— **CONFIRMED（主进程/host 侧订阅泄漏，每次刷新 +1）**

实测（`zz-adversarial-probe.spec.ts`，页面内挂 `window.rollo.onEvent` 收集投递）：
```
PROBE PASS | C06.reload leaves the previous subscription alive | first=48dca81605fd45419194bae89a598fe5 second=b7334f195fbc49dbbcf05e54b379de14
  distinct_delivering=2 kinds=[{"id":"b7334f195fbc49dbbcf05e54b379de14","kind":"snapshot"},
                               {"id":"b7334f195fbc49dbbcf05e54b379de14","kind":"event"},
                               {"id":"48dca81605fd45419194bae89a598fe5","kind":"event"}]
```
刷新前建立的订阅（`48dca8…`）在刷新**之后仍然投递**增量事件，与刷新后新建的订阅（`b7334f…`）并存。原因是两侧都没有清理路径：
- 渲染进程：`App.tsx` 的 `useEffect` 只在清理时 `offEvent()/offState()`（摘掉监听器），**从不调用 `unsubscribe`**；订阅 id 只存在组件 state 里，刷新即丢失。
- 主进程：`ipc.ts` 没有 `webContents.on('destroyed'|'did-start-navigation')` 之类的钩子去回收该窗口持有的订阅；`index.ts` 也没有。
- host：`HostServer.subscriptions` 只在 `events.unsubscribe`、`host.shutdown` 或 stdin 关闭时清理（我用 `probe_delivery.py` 独立验证：3 条未退订的订阅会**各自**投递同一个 canonical 事件，`distinct_subscriptions` 有 3 个）。

**后果**：每次刷新在 host 里留一个永久订阅（一条 pump 任务 + 一个打开的 store 句柄 + 一张表项），事件投递量随刷新次数线性增长，而主进程还会把陈旧订阅的事件 `broadcast` 给窗口。在 C06 的 scope 里这不是安全缺陷，但是**明确的资源泄漏与"刷新"语义的缺口**（spec 只要求"刷新后 MUST 能重新取得同一 workspace 与其连接"，没要求清理，但设计目标是"刷新只影响渲染进程"）。修法：preload 暴露 unsubscribe 并在 `beforeunload` 里批量退订，或主进程按 webContents 记账、在导航/销毁时回收。

### 怀疑 5：孤儿进程 / 孙进程 / 进程组 —— **实测结论：正常退出与整树强杀都不留孤儿；"只杀一个进程"会留，但那不是真实的 Task Manager 语义**

三次独立测量见 C06-13。需要特别说明的是**我的第一次测量是错的**：
```
# 第一次（错的）：kill 的是 Playwright 报告的 app.process().pid
PROBE2 FAIL | C06.13b host present before the kill | main=28980 hosts=[{"ProcessId":28824,"ParentProcessId":12868,…},…]
PROBE2 PASS | C06.13c t+2000ms host alive after taskkill /F | hostPid=28824 alive=true listed=2
PROBE2 PASS | C06.13c t+8000ms host alive after taskkill /F | hostPid=28824 alive=true listed=2
PROBE4 | t+40000ms after taskkill /F main | alive=13796:true,30380:true listed=2
# 归因探针
PROBE5 | before kill | main=23436 electron=4 hosts=2
PROBE5 | after kill | electron_alive=4 pids=17244(ppid=23436),24708(ppid=17244),22276(ppid=17244),26928(ppid=17244) hosts_alive=2
PROBE5 | host ppids still present | 2948->17244,31308->17244
```
`app.process().pid` 是 Playwright 启动的 launcher 进程；真正的 Electron 主进程是它的子进程（17244），host 的父进程也是 17244。杀掉 launcher 之后**整个应用还在跑**，所以 host 活着是理所当然的，与"孤儿"无关。按 Task Manager 的真实语义杀掉整棵树之后（PROBE6），两个 host 都在 2 秒内消失。

关于用户提示里的具体机制问题："Node 侧 spawn host 时没有建进程组"——属实（`spawn(..., {shell:false, windowsHide:true})`，没有 job object / `detached`+`process.kill(-pid)` 结构）。因此**如果**将来出现"父进程非正常消失且 stdin 未关闭"的组合，host 不会被连带终止。当前唯一能让 host 失去管理又存活到应用结束之后的场景，正是 C06-11 那个被顶掉的重复 host；它之所以没变成孤儿，靠的是 stdin EOF 自杀这条**副产品**（探针实测有效，但没有任何测试或文档把它当成契约）。列为 P2-3。

### 怀疑 6：`not_implemented` 的稳定性 —— **CONFIRMED（区分成立）；稳定性风险成立且无 wire 提示**

区分性见 C05 报告 §1 C05-4（`run.start` → `data.code="not_implemented"`；`session.explode`/`host.ping`/空串 → 仅 `-32601` 无业务码）。C06 侧多一条独立证据：`host-integration.test.ts` 的 `reports a business code when the host refuses a control method` 断言 `client.request('run.start')` 以 `code: 'not_implemented'` 拒绝，我复跑通过。
"客户端若把它当永久状态"确实是真风险，而 wire 上**没有任何字段**（无 `expires`/`until`/`since`/`slice`）让客户端区分"永久未实现"与"本构建未实现"，只能靠文档。`implementation-status.md` §7.1 与 design D1 已把它写成声明过的切片边界，界面也如实显示 `control: not in this build`（截图核对见下），所以**风险已被承认，但缓解手段只有文案**。

### 怀疑 7：只读断言能否被意外写入打破 —— **FALSIFIED（强表述）**

C05 侧结论（见 C05 报告 §1 C05-6）：在修订 A 上，"观察一个不存在的会话"会**建库**（已修复）；在当前修订上，打开已存在的 store 仍会提交一个写事务（SQLite 头 change counter +1），且 host 启动本身会创建 `runtime/application/<ws>/control.sqlite`。
C06 侧的放大效应：`session.snapshot`/`subscribe` 的 `session_id` 是**渲染进程可任意给出的字符串**（`requireString(sessionId,'session_id')` 只校验非空），在修订 A 上这等于给了渲染进程一个"按任意单段名字在用户 runtime 目录里建 160 KiB 文件"的原语。当前修订已被 host 的 `_session_exists` 拒绝（我在 C06 侧复算：刷新后 `session.list` 里不再出现伪造条目）。

### 怀疑 8：e2e `no Node` 用例是否假绿 —— **部分 CONFIRMED（可被更宽的断言掩盖的部分存在）**

- 若 bridge 缺失会不会"也通过"？**不会**。`expect(exposure.apiKeys).toContain('initialize')` 在 `window.rollo` 不存在时 `apiKeys=[]`，必然失败；`page.evaluate` 抛错也不会被吞（Playwright 会让用例失败）。我用变异 M3（`contextIsolation:false` + `nodeIntegration:true`）得到**红灯**，证明该用例对"Node 真的暴露到主世界"这一性质有鉴别力：
```
M3: x 1 tests\e2e\shell.spec.ts:117:5 › the renderer has no Node and no generic IPC primitive
    Error: expect(received).toBe(expected) / Expected: false / Received: true
    > 128 |   expect(exposure.hasRequire).toBe(false);
```
- 但它**测不出 `sandbox: true` 被关掉**（变异 M1 仍绿）：`contextIsolation:true + nodeIntegration:false` 本身就足以让主世界没有 `require`/`process`，所以 spec 要求的 `sandbox` 这一项在交付套件里没有 oracle。这是"断言写得太宽"的典型：断言的是"没有 Node"，spec 要求的是具体配置项。
- 另一条假绿通道：`test.beforeAll` 里 `test.skip(!python, 'no Python interpreter … was found')`。在找不到解释器的机器上，整个 `shell.spec.ts` 会变成 **skipped 且退出码 0**——一次"绿"的 e2e 可能什么都没跑。这是我**读源码**得出的结论，没有实测（本机一定能解析到解释器，见 §6）。

### 怀疑 9：C06 是否偷偷重定义权限语义 —— **CONFIRMED（没有）**

```
grep -iE "permission|bypass|yolo|approve|allowlist|deny|policy|mode\b|acceptEdits|dontAsk" desktop/src
  desktop\src\main\host-client.ts:6: * It does not decide policy, does not touch canonical facts, and cannot run a
  desktop\src\main\index.ts:58: window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
```
`desktop/src` 全域没有任何权限模式白名单、没有工具调用解释、没有对 `Application.dispatch` 语义的复制（host 侧控制方法本就 `not_implemented`）。`ipc.ts` 的 8 个处理器全部是"查表 → 转发已具名方法"，`host-client.ts` 的 `request()` 是唯一出口且只发已声明的方法名（`initialize/listSessions/snapshot/subscribe/unsubscribe/shutdown`）。界面也没有任何会改变运行时状态的入口：`App.tsx` 只有 `Choose/Change workspace`、`Watch`、`Stop` 三个按钮，控制能力显示为 `not in this build`。

### 怀疑 10：规格自述是否可靠 —— **混合；逐项复算如下**

| 自述 | 复算 | 判定 |
| --- | --- | --- |
| `npm run test → 20 passed (2 files)` | `Tests 20 passed (20) / Test Files 2 passed (2)` | 准确 |
| `npx playwright test → 4 passed` | `4 passed (4.5s)` | 准确 |
| Python 全量 `599 passed / exit 0` | 修订 A：599 passed / exit 0（290.16s）；修订 B：600 passed / exit 0（277.65s，因 00:15 新增 1 条） | 成文时准确，现为 600 |
| `src/rollo/**` 零改动，`git status --short -uall` **只显示新增目录** | 实际还有 ` M .gitignore`（+2 行）；C06 tasks 4.4 与 implementation-status §2/§6 的同一句表述不准确 | **不准确**（作用域需限定为 `src/rollo/**`） |
| Electron 33.4.11 / Node v22.21.0 / npm 10.9.4 | `node_modules/electron/package.json` 与 `node -v`、`npm -v` 一致 | 准确 |
| §4 截图里的每个值都来自 host | 我用 `read_image` 读了 `desktop/docs/shell-observation.png`（1438×993，sha256 `3b1d437238d2d8a2e7f5dcb0f710684c8940cb16e5e7430a850aa5118f44bdbf`）：`protocol v1`、`epoch b4763247ade8`、`workspace fe79dfc795413556`、`settings missing: ANTHROPIC_API_KEY, OPENAI_API_KEY`、`control not in this build`、`Sessions (1)` / `session-screenshot inspect_only`、`#1 snapshot ordinal 3 · high_water 3 · 2 messages · runs 1 · drafts 0` | **与 §4 逐字一致**，`epoch` 是 `host_epoch.slice(0,12)` 的观感也吻合；`missing` 只有名字无值 |
| §5.1 真实缺陷：`file://` 的 origin 是 `"null"`，拿它与 `file://` 比较会拒绝每一个合法调用 | 变异 M2 复现：改回字面比较后交付 e2e 有 2 条变红；改回按 scheme 判定后全绿 | 准确（且该修复被测试覆盖） |
| §5.2 读循环占用共享线程池执行器（C05 侧） | 不属 C06 范围；C05 报告已核 | — |
| §5.3 `hostEnvironment()` 返回 `PYTHONPATH` 而字段叫 `pythonPath` | 当前源码已修（`sidecar.ts:41-45` 返回 `{pythonPath}`），且有注释解释为什么键名由消费方契约决定 | 准确（修复在位） |
| §5.5 e2e 未隔离运行时目录会读开发者真实会话 | `ProjectContext.from_root(..., None)` 会读 `ROLLO_RUNTIME_DIR`（`project_context.py:171`），e2e 确实设置了它；我复跑时 host 读的是临时目录内的数据 | 准确（机制在位）；"26 个会话"这个历史数字无法复现 |
| design D2 "渲染进程永远拿不到目录路径" | **被 `workerStatus` 打破**（见 P1-1） | **FALSIFIED** |
| design D8 "协议层一个问题都没有" | C05 侧独立审查找到观测层缺口（未签发 session 建库、续传不可达） | 过度概括（C05 报告 P2-1 详述） |

---

## 3. 新发现（C06）

### P0-1 一个 workspace 会起两个 host 进程（`connect()` 无并发去重）
见 C06-11。4/4 次启动复现；根因是"就绪判据用了 spawn 后立刻置位的 `state='ready'`，而不是 in-flight promise"。被顶掉的 client 从注册表失联，`closeAll()` 够不到它，其状态监听仍会污染 `entry.state`。
最小修法：`WorkspaceRegistry` 里用 `Map<string, Promise<WorkspaceEntry>>` 记住在飞的 connect，或在 `entry` 上加 `pending` 标记，并在 `HostClient` 上区分 `spawned` 与 `handshaken` 两个状态。
**测试缺口**：交付的 24 条测试（20 vitest + 4 e2e）没有任何一条数进程。建议加一条直白的断言：注册一个 workspace、等握手完成、按命令行枚举 `python.exe -m rollo.host --workspace <该目录>` 恰好 1 个。

### P1-1 `workerStatus` 把解释器绝对路径、PYTHONPATH 与 workspace 目录路径交给渲染进程
`ipc.ts:175-185` 返回 `plan: entry.client?.spawnPlan ?? null`，而 `spawnPlan`（`host-client.ts:133-145`）含 `executable`、`pythonPath`、`workspace`。渲染进程实测拿到：
```
PROBE FAIL | C06.4a workerStatus does NOT disclose host paths | payload={"workspace_id":"2e3af40371b3","label":"rollo-probe-vTjUOs","state":"ready","problem":null,
 "settings":{"ready":false,"missing":["ANTHROPIC_API_KEY","OPENAI_API_KEY"],"model":null},"stderr":[],
 "plan":{"executable":"D:/Anaconda/envs/py313/python.exe","pythonPath":"D:\\PycharmProjects\\pythonProject\\Rollo-Code\\src",
         "workspace":"C:\\Users\\MuShi\\AppData\\Local\\Temp\\rollo-probe-vTjUOs","optionKeys":[…]}}
```
这与 design D2 "注册表签发 `workspace_id`，渲染进程**永远拿不到目录路径**"直接冲突，也让 spec 的"渲染进程 MUST 只持有主进程签发的不透明 `workspace_id`"在"持有"这一半上不成立（提交方向仍然安全）。附带的一致性缺陷：`DesktopApi.workerStatus` 的声明返回类型是 `Promise<{ state: WorkerState; stderr: string[] }>`（`wire.ts:113`），运行时却多出 `workspace_id/label/problem/settings/plan` 五个字段——类型在撒谎，`plan` 这个泄露字段因此在评审时看不见。
`spawnPlan` 的注释说明它是为**诊断启动参数**而存在的（"两个已被它抓到的错误：缺导入路径、选项键名与字段名不匹配"），这个动机是合理的；但把它放在**渲染进程可达**的返回值里，等于把主进程的诊断面当成了 UI 契约。修法：要么按声明类型裁剪（只回 `workspace_id/label/state/problem/settings/stderr`），要么把 `plan` 移到仅主进程可读的诊断通道。

### P1-2 刷新会泄漏 host 侧订阅（每次刷新 +1，永不回收）
见怀疑 4 的实测。修法与验证建议：让 `HostClient`/`WorkspaceRegistry` 按 webContents 记账，或在 preload 暴露 `unsubscribe` 并在 `beforeunload` 退订；测试上可以断言"刷新两次后，host 对同一条新增 canonical 事件只投递一次增量"。

### P1-3 spec 的 `sandbox: true` 在交付套件里没有 oracle（变异 M1 未检出）
见怀疑 8 与 §4。修法：在 e2e 里断言 `app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows()[0].webContents.getLastWebPreferences())` 的 `sandbox/contextIsolation/nodeIntegration` 三项，这是可直接观测的配置，而不是间接的"有没有 Node"。

### P2-1 `assertUsablePython` 的强度低于其注释与 design D2 的宣称
见怀疑 1。不是渲染进程可达路径，但"refusing to launch a non-Python executable"目前只是"路径里得有 python 字样"。建议改成对解释器做一次探针（`-c "import rollo.host"`，代价约 0.3 s，e2e 已经在这么做），或把文档改成"防误配"。

### P2-2 交付的测试里硬编码了本机解释器路径，且 e2e 可以静默整体跳过
`tests/e2e/shell.spec.ts:25` 与 `tests/main/host-integration.test.ts:27` 都有 `'D:/Anaconda/envs/py313/python.exe'`；`shell.spec.ts:93` 的 `test.skip(!python, …)` 会让 4 条 e2e 在无解释器机器上变成 skipped + exit 0（"绿"但零覆盖）。前者是移植性异味，后者是假绿通道。建议：路径走 `ROLLO_PYTHON`/`PATH` 探测并在找不到时**失败而非跳过**（或至少在 CI 里把 skipped 视为失败）。

### P2-3 "不留孤儿"目前依赖 stdin EOF 自杀这一副产品
`index.ts:133-141` 的 `before-quit` → `closeAll()` 只能关闭 `entry.client`（受 P0-1 影响时只关得掉一个）；其余 host 之所以在退出后消失，是因为父进程消失→stdin EOF→`_read_loop` 收尾退出（PROBE6 实测 2 s 内消失）。这条链路没有任何测试，也没有文档把它当作契约。若将来 host 改为不读 stdin、或改为长跑重连模型，重复 host 就会变成真正的孤儿。建议显式化：spawn 时建 job object / 进程组，或在 `will-quit` 里按 pid 兜底 kill。

### P2-4 `index.ts` 里有一处永远收不到的 IPC 推送
`index.ts:119` `window.webContents.send(CHANNELS.autoWorkspace, { workspace_id: presetId })`，但 preload 只把 `autoWorkspace` 暴露成 `ipcRenderer.invoke`（`preload/index.ts:76`），没有任何 `ipcRenderer.on(CHANNELS.autoWorkspace, …)`。渲染进程改为在挂载时主动询问（`ipc.ts:110-112` 的注释解释了这个选择）。因此这行是死代码，且会让读者以为存在"推送→监听"的链路。删除或改注释即可。

---

## 4. 变异测试（我自己注入的）与鉴别力

| # | 变异 | 位置 | 预期 | 实测 | 结论 |
| --- | --- | --- | --- | --- | --- |
| M1 | `sandbox: true` → `sandbox: false` | `index.ts:50` | 期望 `no Node` 用例变红 | **仍然 ok 1 passed**（`-g "no Node"`） | **未检出**：交付 e2e 对 `sandbox` 无鉴别力（P1-3） |
| M2 | `url.protocol === 'file:' ? 'file://' : url.origin` → `url.origin` | `ipc.ts:49` | 期望打包形态的合法调用被拒 → 用例变红 | **2 failed / 2 passed**（`shell.spec.ts` 的"spawns a host…"与"watching a session…"变红，`locator.click` 30 s 超时） | **检出**：`file://` 分支被交付套件真实覆盖（也反证 D4 的修复是必要的） |
| M3 | `contextIsolation: true, nodeIntegration: false` → `false, true`（并关 sandbox） | `index.ts:48-50` | 期望 `no Node` 用例变红 | **1 failed**：`Expected: false / Received: true`，卡在 `expect(exposure.hasRequire).toBe(false)` | **检出**（正对照）：该用例对"Node 暴露"这一性质有鉴别力，只是覆盖不到 `sandbox` 这一项 |

三次变异后都已逐字还原：`index.ts` sha256 回到变异前的 `1155C07AA0BEA6D6…`；`ipc.ts` 恢复为 `E539EF23244B594C…`（该文件变异前我未取哈希——这是我的证据缺口，如实标注），且还原后 `npm run build` / `vitest 20 passed` / `playwright 4 passed` 全绿。

---

## 5. 无鉴别力声明（我的探针里不区分问题的部分，如实列出）

1. **我的第一次"孤儿进程"探针是错的，且我一开始得出了相反的结论。** `taskkill /F /PID app.process().pid` 杀的是 Playwright 的 launcher，不是 Electron 主进程（PROBE5 显示真正的应用进程 17244 仍在，host 的父进程也是它）。因此 `hostAliveAfterKill=true`（持续 40 s）**不能**作为"孤儿"证据。修正后的整树杀（PROBE6）给出相反结论：host 在 2 s 内自行退出。任何引用"实测发现孤儿"的说法都必须用 PROBE6 的口径。
2. **`probe2`/`probe4` 里"每 workspace 一个 host"的判据一开始也是坏的**：我用 `-like '*C:\\Users\\…*'` 在 PowerShell 里做模式匹配，反斜杠转义导致列表恒为空，于是第一版探针报告 `hostsBefore=0`、并让"优雅退出不留孤儿"假绿（`pids=` 为空）。修正为"PowerShell 只取全表，Node 侧按 basename 过滤"之后才看到 2 个 host。**修正前的 `C06.13 graceful quit leaves no orphan host | PASS` 是无鉴别力的空断言。**
3. **`probe1` 里的 "C06.10 no ipc handler accepts a path-like argument name" 断言无效**：我用 `/path/i` 扫 `registerIpc` 函数体，命中的是 `picked.filePaths[0]`——那正是我们**希望**存在的原生选择器用法。该检查没有任何鉴别力；"渲染进程不能提交路径"这一结论是靠 30 次伪造 id 的实测矩阵 + 注册表入口分析得出的，不是靠这条 grep。
4. **`probe1` 的 "C06.4b workerStatus payload matches its declared type" 是自证的**：我允许的键列表里预先包含了 `plan` 等全部实际键，所以它必然通过，不构成类型一致性的证据；真正的发现来自把 payload 打印出来后与 `wire.ts:113` 的声明类型对照。
5. **`probe3` 的元字符用例标签写反了**：我把 `[value, shouldThrow]` 的第二个位置当成"期望抛出"，但打印与判读时方向不一致，导致 `%TEMP%`（因为文件不存在而抛出）与 `;`/`|`（正确的元字符拒绝）看起来像同一类。该段**没有鉴别力**；有意义的观测只有两条：`…\PYTHON.EXE` 被接受（大小写不敏感 + 文件存在），以及 `…\python-tools\notepad.exe` 被接受。
6. **`probe3` 的 "relative ROLLO_PYTHON failure names the setting" 用例设计错误**：我期望 `HostClient.start()` 抛错，但 `resolveSidecarPython` 先用 `existsSync`（相对当前 cwd）判定不存在，直接返回了带 `ROLLO_PYTHON` 字样的 problem，`start()` 根本没被调用——所以那次 FAIL 是断言写错，不是行为缺陷。我另写一条探针（`zz-probe7.test.ts`）用"相对但存在"的路径才测到真实行为：`resolved={"python":"src\\main\\sidecar.ts","problem":null}` → `startError="the Python executable must be an absolute path: src\\main\\sidecar.ts"`，即**这条失败信息里没有 `ROLLO_PYTHON`**，与 spec 场景"给出指向具体配置项的说明"有落差（我把它算在 P2-1 同一族里，未单列）。
7. **"两个 client 都往同一个 `entry.state` 写"是源码推断**，我没有单独杀掉"被顶掉的那个 host"来实测 UI 是否误报 failed（两个进程命令行相同，外部无法区分）。
8. **`--workspace` 目录名撞车**：我用 basename 过滤进程，若同一时刻有其它会话的同名临时目录就会误配；实测未发生（每次的 `rollo-p*` 后缀唯一），但这是探针的隐含假设。

---

## 6. 我未能验证的部分

1. **`test.skip(!python, …)` 的假绿通道没有实测**：本机一定能解析到解释器（`ROLLO_PYTHON` → `.venv` → 硬编码 `D:/Anaconda/...`），我没有构造"没有任何可用解释器"的环境去验证它是否真的整体跳过并以 0 退出。结论基于源码阅读。
2. **原生目录选择器（`chooseWorkspace`）本身没有实测**：Playwright 驱动不了原生对话框，我也刻意没有手工启动 GUI。因此"经选择器登记的条目与 `ROLLO_AUTO_WORKSPACE` 走的确实是同一条 `registry.add` 路径"是**源码复核**（`ipc.ts:96-105` → `registry.add(picked.filePaths[0])`），不是端到端实测。
3. **第二次启动是否真的"聚焦已有窗口"没有观测到聚焦事件**：我验证的是"第二个进程 exit 0、窗口数仍为 1、host 数不增"，没有观测 `window.focus()`/`restore()` 的效果（`index.ts:89-94` 的 `second-instance` 处理器）。"聚焦"这一步在无头/后台窗口下难以断言，我未尝试。
4. **多显示器/多窗口/`activate`（macOS 语义）路径未测**：`app.on('activate')` 在 Windows 上不触发，我没有验证其在 macOS 的行为（也不可能在本机验证）。
5. **stderr 环形缓冲的"有界"我只在单测层面复算**（`records stderr in a bounded ring` → 260 行输入后留 200 行），没有在真实 host 上灌 200 行以上 stderr；"崩溃时最后诊断随失败呈现"我用注入式 spawn 复算过（`message="the host exited (code=1 signal=null)\nTraceback: rollo.host exploded\n…"`），但**真实 Python host 崩溃**（例如解释器段错误）这一路没有构造。
6. **打包形态（C07）未验证**：我跑的是 `electron .` + `dist/` 的未打包形态；`app.isPackaged` 分支、asar、代码签名等不存在于当前代码，无从验证。
7. **`desktop/docs/shell-observation.png` 是否由当前修订产生**：我只能核对图中数值与 `implementation-status.md` §4 的自述一致；截图无法证明它对应的提交（无内嵌元数据可查），时间是文件 mtime，我没有独立重放生成该截图的会话。
8. **C05 侧的"C06 消费协议"完整性**：`wire.ts` 只镜像了 host 实际会发的字段，我没有逐一比对"host 可能发出的每个字段都在 TS 类型里有位置"（例如 `events.event` 的 `payload` 是 `unknown`，`snapshot` 是 `Record<string, unknown>`，类型层等于放弃约束）。
9. **刷新前后的 `workspace_id`/`host_epoch` 字符串没有逐字比对**：我据以判断"同一 host"的证据是"刷新前的订阅仍在投递"（进程内订阅表跨刷新存活），不是 id/epoch 相等。`app.getAppMetrics().length` 不变与窗口数为 1 是交付 e2e 的断言，我复跑通过但未独立重建该断言。

---

## 7. 复算命令清单（C06 部分）

```powershell
cd D:\PycharmProjects\pythonProject\Rollo-Code\desktop
D:\nodejs\npm.cmd run typecheck          # exit 0
D:\nodejs\npm.cmd run build              # exit 0
D:\nodejs\npm.cmd run test               # Tests 20 passed (20) / Test Files 2 passed (2)
D:\nodejs\npx.cmd playwright test        # 4 passed

# 变异的做法（每次：编辑 → build → 只跑相关用例 → 逐字还原）
#   M1: sandbox: false          → npx playwright test shell.spec.ts -g "no Node"   → 仍 passed（未检出）
#   M2: identity = url.origin   → npx playwright test shell.spec.ts                → 2 failed（检出）
#   M3: contextIsolation/nodeIntegration 反转 → -g "no Node"                        → 1 failed（检出）
Get-FileHash src\main\index.ts -Algorithm SHA256   # 1155C07AA0BEA6D6…（还原后）

# 临时探针（已删除；运行方式留档）
D:\nodejs\npx.cmd playwright test zz-adversarial-probe  --reporter=list   # 渲染进程面/伪造 id/刷新泄漏/单实例/强杀/origin
D:\nodejs\npx.cmd playwright test zz-adversarial-probe2 --reporter=list   # 进程枚举：每 workspace 的 host 数、优雅退出
D:\nodejs\npx.cmd playwright test zz-adversarial-probe4 --reporter=list   # 两轮重复启动 + 40 s 存活观察
D:\nodejs\npx.cmd playwright test zz-adversarial-probe5 --reporter=list   # 归因：杀 launcher vs 杀应用
D:\nodejs\npx.cmd playwright test zz-adversarial-probe6 --reporter=list   # 整树杀（Task Manager 语义）
D:\nodejs\npx.cmd playwright test zz-probe7s            --reporter=list   # sender 身份校验（第二个真实窗口）
D:\nodejs\npx.cmd vitest run tests/main/zz-adversarial-probe.test.ts      # assertUsablePython 绕过 + 崩溃诊断
D:\nodejs\npx.cmd vitest run tests/main/zz-probe7.test.ts                 # 相对 ROLLO_PYTHON 的失败信息
```

---

## 8. 还原证据（我未改动任何仓库源码/测试）

审查期间我只临时修改过 `desktop/src/main/index.ts` 与 `desktop/src/main/ipc.ts`（3 次变异测试），并临时新增过 7 个 `zz-*` 探针文件；两者都已逐字还原/删除。**C05 的源文件与测试文件我一个字节都没有改**。

```
cd D:\PycharmProjects\pythonProject\Rollo-Code
git status --short -uall | Where-Object { $_ -match '^ ?M' }
 M .gitignore                       ← 唯一被修改的既有文件（+2 行忽略项）
Get-ChildItem -Recurse -Filter "zz-*"        → 无输出（探针全部删除）
Get-FileHash desktop\src\main\index.ts -Algorithm SHA256 → 1155C07AA0BEA6D6…
Get-FileHash desktop\src\main\ipc.ts   -Algorithm SHA256 → E539EF23244B594C…
```
还原后复跑：`npm run build` exit 0、`vitest 20 passed`、`playwright 4 passed`。`git status --short -uall` 中不存在任何其它 ` M ` 条目；C06 的 15 个文件与 C05 的 6 个文件仍全部是 `??` 未跟踪新文件（43 条 untracked，含两份 openspec change 的全部文档）。

**我没有执行任何 commit / push / PR 操作。**
