# 实现状态：add-electron-desktop-shell（C06）

## 1. 证据身份

- checkout：`D:\PycharmProjects\pythonProject\Rollo-Code`
- base：`f06831e`（C04 提交；C05 与 C06 均未提交）
- Node `v22.21.0` / npm `10.9.4` / Electron `33.4.11` / Python `3.13.13`
- Electron 二进制经 `desktop/.npmrc` 配置的镜像获取（GitHub release 直连会长时间无输出）
- 唯一 writer：主 Agent；无子代理参与实现

## 2. 交付物

| 路径 | 说明 |
| --- | --- |
| `desktop/package.json`、`package-lock.json`、`.npmrc` | 工程定义、锁定依赖、镜像配置 |
| `desktop/tsconfig.json`、`tsconfig.build.json` | 类型检查（`tsc --noEmit`） |
| `desktop/vite.config.ts`、`vitest.config.ts`、`playwright.config.ts` | renderer 构建、单测、e2e |
| `desktop/scripts/build-electron.mjs` | esbuild 打包 main 与 preload 为自包含 CommonJS |
| `desktop/src/shared/wire.ts` | 协议 v1 的类型镜像与通道名 |
| `desktop/src/main/host-client.ts` | 分帧、请求关联、stderr 环形缓冲、关闭回收 |
| `desktop/src/main/workspaces.ts` | 每 workspace 一个 host 的注册表 |
| `desktop/src/main/sidecar.ts` | 解释器解析与校验 |
| `desktop/src/main/ipc.ts` | 唯一 IPC 面 + sender/origin 校验 |
| `desktop/src/main/index.ts` | 窗口、单实例、退出收尾 |
| `desktop/src/preload/index.ts` | 具名方法白名单桥 |
| `desktop/src/renderer/{index.html,main.tsx,App.tsx}` | 只读视图 |
| `desktop/tests/main/*.test.ts` | 单测 + 真实子进程集成测试 |
| `desktop/tests/e2e/shell.spec.ts` | 真实 Electron 窗口 smoke |
| `desktop/docs/shell-observation.png` | 交付截图（真实窗口 + 真实 host） |

**`src/rollo/**` 零改动**：`git status --short -uall` 只显示新增目录。

## 3. 命令与结果

```
npm --prefix desktop run typecheck      → exit 0
npm --prefix desktop run build          → exit 0
npm --prefix desktop run test           → 20 passed (2 files)
npx --prefix desktop playwright test    → 4 passed
$env:PYTHONPATH=…; python -m pytest -q src/rollo/tests
                                        → 599 passed, 11 warnings (exit 0)
```

真实环境与 mock 边界：Provider 从未被调用（种子直接写入 canonical store）；真实的部分是 **Python 解释器进程、stdio 管道、Electron 窗口与 IPC**。

## 4. 交付截图说明了什么

`desktop/docs/shell-observation.png` 是真实窗口的截图，其中的每一个值都来自 host：

- `protocol v1` / `epoch` / `workspace` —— 来自 `host.initialize` 的响应；
- `settings: missing: ANTHROPIC_API_KEY, OPENAI_API_KEY` —— 只报缺失项**名称**，不报值；
- `control: not in this build` —— 来自 `capabilities.declared_not_implemented`，即 C05 的切片边界被界面如实呈现；
- `Sessions (1)` —— `session.list` 的真实结果；
- `#1 snapshot ordinal 3 · high_water 3 · 2 messages · runs 1 · drafts 0` —— 订阅的首条投递与快照边界。

## 5. 实现过程中发现并修正的问题（全部在壳这一侧）

真实缺陷 2 处：

1. **`file://` 页面的 origin 是字符串 `"null"`，而允许列表里放的是 `'file://'`**，两者永不相等 ⇒ **每一个**来自打包 renderer 的 IPC 调用都被自己的安全校验拒绝。改为按 scheme 判定。这是安全校验里的逻辑错误，不是配置问题。
2. **读循环占用共享线程池执行器**（C05 侧，已在其记录中说明）——同一类问题在本轮被再次确认：等待输入与投递事件必须互不阻塞。

实现错误 1 处，值得单独记住：

3. **`hostEnvironment()` 返回的键是 `PYTHONPATH`，而 `HostClientOptions` 的字段叫 `pythonPath`。** 该对象被 `...spread` 进选项，而**对象展开不参与多余属性检查**，于是 TypeScript 全程通过、运行时该字段永远是 `undefined`，host 因缺少导入路径而无法启动。教训：靠展开装配的对象，其键名必须由**消费方的契约**决定，并且这种装配点需要能观测（本 Change 为此保留了 `HostClient.spawnPlan`）。

验证方法问题 2 处：

4. **smoke 的解释器探针只检查 `import rollo`**，于是选中了「装了 rollo、但没有 `rollo.host`」的系统解释器——探针必须检查**真正的入口**。
5. **e2e 未隔离运行时目录**：`ProjectContext` 默认 `~/.rollo`，host 因此读了开发者真实的 26 个会话。测试不仅要断言正确的事，还要确保**它读的是自己的数据**。

另有一处编辑事故：用 PowerShell 的 `-replace … | Set-Content -NoNewline` 改源文件，把整个文件的换行压平。已用 `write` 重写恢复；此后不再用 PowerShell 改写源码。

## 6. 范围合规

- 仅新增 `desktop/`；`src/rollo/**` 零改动（599 passed 复验）。
- 未实现控制路径；`run.start` / `run.cancel` / `interaction.respond` / `content.read` 仍由 host 回答 `not_implemented`。
- 未打包（C07）；无网络端口；Provider 未被调用。

## 7. 残余风险与未完成项

1. **控制缺口**：界面不能发起运行。这是声明过的切片边界，界面如实显示；补齐取决于何时需要"从界面开始一次运行"。
2. **Electron 二进制获取依赖镜像**：`.npmrc` 已固定，但首次安装仍需下载 ~100MB。
3. **`session.list` 偏移游标**（继承 C05）：会话增删会漂移；列表短且会重拉，当前够用。
4. **Playwright 不驱动原生对话框**：自动化经 `ROLLO_AUTO_WORKSPACE` 走同一注册表入口；交互式启动仍用原生选择器。这一差异已在 design D4 记录。
5. **未执行**：commit/push/PR（未授权）。

## 8. 对抗性审查的裁决与处置（2026-09-14）

报告：`adversarial-review.md`（含 `result_identity`）。16 条断言：12 CONFIRMED、**1 FALSIFIED**、3 部分成立；10 个怀疑方向：5 CONFIRMED、2 FALSIFIED。

### 8.1 P0（FALSIFIED）：一个 workspace 起了两个 host —— 已修 + 加 oracle

`WorkspaceRegistry.connect` 的就绪判据是 `entry.client && state === 'ready' && entry.handshake`，而 **`HostClient.start()` 在 spawn 后立刻把 state 置为 `ready`**，握手却要约 1 秒。这个窗口里任何第二次 `connect()` 都会再 spawn 一个 host 并覆盖 `entry.client`——**被顶掉的那个从注册表失联，`closeAll()` 永远够不到它**。

主 Agent 独立复现：`AFTER 1.2s: [27748, 27468]` — 两个进程并存，命令行完全相同。

**24 条交付测试没有一条数进程**，所以它对所有行为断言都不可见。触发源有两处：renderer 的挂载 effect 依赖 `workspace?.workspace_id` 会在 autoWorkspace 设置它之后再跑一次，而 `listSessions` 处理器也先 `connect`。

**修复**：`connect` 用 in-flight promise 去重（并发调用者共享同一次尝试）；`closeAll` 置 `closing` 并先 `allSettled` 掉在途连接，避免关停期间又 spawn 一个无人回收的进程；renderer 侧加 `connectedRef` 守卫。

**oracle**：`tests/e2e/worker-lifecycle.spec.ts` 并发驱动三次 connect，然后**从操作系统进程表**断言该 workspace 只有一个 host（并再等 2.5s 确认不是侥幸）。**变异验证**：去掉去重 → 该用例红；控制组与还原后均绿。

### 8.2 P1：`workerStatus` 把绝对路径交给渲染进程 —— 已修 + 加 oracle

返回体含 `python.exe` 绝对路径、`PYTHONPATH` 与 workspace 目录，与 design D2「渲染进程永远拿不到目录路径」直接冲突。**修复**：只返回 UI 解释自己所需的最小集合（id/label/state/problem/settings/stderr）。**oracle**：断言渲染进程收到的 payload 不含 `python.exe`、`PYTHONPATH` 或盘符路径。

### 8.3 P1：`sandbox: true` 无 oracle —— 已加，并用变异证明有鉴别力

审查用变异 M1（`sandbox: false`）证明原 `no-Node` 用例**仍然通过**——它对这项零鉴别力。渲染进程里连 `process` 都没有，`getWebPreferences()` 也不是公开 API，所以行为式读取不可行。改为断言**编译产物的窗口选项**，并**变异验证**：把 `sandbox` 改成 `false` → 新用例红。它因此是 oracle，而不是注释。

### 8.4 P1：刷新泄漏 host 侧订阅 —— **未修，如实记录**

刷新后旧订阅在 host 侧继续投递，每次刷新留下一条 pump 与 store 句柄，renderer/main 都没有回收路径。这是真实泄漏。未修的原因：正确的修法需要给订阅绑定"窗口/会话所有者"并在卸载时释放，那属于**订阅所有权**这一新语义；塞进观测切片会把一个已知缺口换成未经设计的所有权模型。已记入残余风险，建议与 C07 或控制切片一并处理。

### 8.5 P1：`test.skip(!python)` 可让整套 e2e 静默变绿 —— 已改

缺少解释器现在**默认让运行失败**，只有显式设置 `ROLLO_E2E_ALLOW_SKIP=1` 才跳过。一个静默绿的全套正是坏壳子被合并的方式。

### 8.6 审查自我推翻的结论（值得记录）

审查初版报告"强杀后 host 成为孤儿"，随后**自己推翻了它**：那次杀的是 Playwright 报告的 launcher 进程，不是 Electron 主进程。按真实语义杀整棵树后，两个 host 都在 2 秒内自行退出，且 host 的子进程只有 `conhost.exe`（控制切片未实现，无 shell 孙进程可留）。**这提醒一件事：进程类结论必须说清"杀的是哪个进程"**——我第一次写进程枚举探针时也因 PowerShell 转义写错而得到过空结果。

