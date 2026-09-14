## 1. 工程与工具链

- [x] 1.1 建立 `desktop/` Electron + React + TypeScript + Vite 工程，四个脚本 `typecheck` / `build` / `test` / `test:e2e`，依赖锁入 `package-lock.json`（不依赖全局隐式版本）。
  - 证据：`npm run typecheck` → exit 0；`npm run build` → exit 0；`npm run test` → 20 passed；`npx playwright test` → 4 passed。
  - Electron 二进制从 GitHub release 获取，链路慢时长时间无输出；已在 `desktop/.npmrc` 固定镜像，使新克隆安装方式一致。
- [x] 1.2 main 与 preload 必须打成**自包含单文件**。以 `esbuild` 产出 CommonJS；`tsc` 仅做类型检查。
  - 原因：沙箱 preload 无模块解析器，`require('../shared/wire.js')` 会以 `module not found` 失败，桥接对象不出现、页面空白。以 smoke 的「渲染进程能力面」用例验证。

## 2. 主进程

- [x] 2.1 `host-client`：NDJSON 增量分帧（半帧/多帧/UTF-8 拆分）、请求关联、业务码提取、stderr 环形缓冲、优雅关闭后强制回收。
  - 以 `tests/main/host-client.test.ts` 的注入式 spawn 单测验证（18 条）。
- [x] 2.2 `workspaces`：每 workspace 一个 host，按规范化目录去重，失败条目保留并带诊断；`workspace_id` 为不透明句柄。
- [x] 2.3 `sidecar`：解释器解析（`ROLLO_PYTHON` → checkout 内 `.venv`），路径校验拒绝相对路径、shell 元字符与非 Python 名；无法解析时给出可操作说明。
- [x] 2.4 `ipc`：唯一 IPC 面；逐处理器校验 sender 与来源；按 scheme 判定 `file://`；渲染进程只能提交不透明句柄。
- [x] 2.5 窗口安全：`contextIsolation` 开、`nodeIntegration` 关、`sandbox` 开、拒绝新窗口与导航。
- [x] 2.6 单实例；退出前关闭全部 host，再退出。
- [x] 2.7 原生目录选择器登记工作区；自动化路径经 `ROLLO_AUTO_WORKSPACE` 走**同一注册表入口**。

## 3. preload 与 renderer

- [x] 3.1 preload：`contextBridge` 暴露具名方法白名单，无 `invoke`/`send`；自身校验参数，主进程再校验。
- [x] 3.2 renderer：只读视图——选择工作区、列出会话、订阅并显示快照与增量；不提供任何会改变运行时状态的命令。
- [x] 3.3 控制能力缺失必须如实呈现（显示 `control: not in this build`），界面不得提供必然失败的入口。

## 4. 验证

- [x] 4.1 `tests/main`：单测（注入式 spawn）+ **真实 Python 子进程**的跨进程集成测试（握手→列表→快照→订阅→退订→关闭，并断言子进程确实退出）。
- [x] 4.2 `tests/e2e`：**真实 Electron 窗口** smoke——spawn、握手、会话列表、订阅快照、刷新后仍连着、渲染进程无 Node。
  - 证据：4 passed。交付截图 `desktop/docs/shell-observation.png`。
- [x] 4.3 隔离验证数据：e2e 必须把 `ROLLO_RUNTIME_DIR` 指向临时工作区内的目录。未隔离时 host 会读到开发者真实会话（实测扫出 26 个）。
- [x] 4.4 确认本 Change **未修改** `src/rollo/**`：`git status --short -uall` 只显示 `desktop/` 新增；Python 全量回归 599 passed / exit 0。
- [ ] 4.5 独立对抗性审查（收尾统一执行，尚未进行，与 C05 一并）。

## 5. Gate 与交付边界

- [x] 5.1 结果回写 `implementation-status.md`。
- [ ] 5.2 仅在用户另行授权后执行 commit/push/PR。
