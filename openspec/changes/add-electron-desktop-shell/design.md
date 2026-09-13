## Context

C05 把 `Application` 的只读面暴露成了 stdio 协议，交付时的判据是「真实 Python 子进程完成观测闭环」。协议设计得再仔细，也只有真正有人消费时才知道够不够用。C06 就是那个消费者。

壳的技术形态在任务卡里已经定了：Electron 主进程 + preload 白名单桥 + React renderer，`desktop/` 独立工程。本 Change 按那份约定实现，不重新设计边界。

## Goals / Non-Goals

**Goals**

- 一个真实 Electron 窗口，经主进程托管的真实 Python host 观察一个 workspace。
- renderer 拿不到 Node、拿不到任意 IPC、拿不到文件系统入口。
- 刷新不中止 worker；退出不留孤儿进程。
- 用真实窗口 + 真实 host 的离线 smoke 作为验收证据。

**Non-Goals**

- 控制路径（发起运行、审批、取消）。C05 的观测切片里没有这些方法，界面因此不做。
- 打包与安装（C07）。
- 多窗口、多实例。桌面工具默认单实例；多会话并行发生在**同一个 host 内部**，那才是运行时真正提供的能力。

## Decisions

### D1 壳不做策略，只做传输与托管

主进程不含任何权限判断、不含 canonical 事实、不解析模型输出。它只做三件事：托管子进程、转发已具名的方法、把事件推给窗口。一旦壳里出现第二个授权源，权限语义就会分裂——这是 C02/C03 已经划清的边界。

### D2 renderer 只持有不透明句柄

注册表签发 `workspace_id`，渲染进程永远拿不到目录路径；解释器路径由主进程从环境解析并校验（绝对路径、无 shell 元字符、文件名必须含 `python`）。这是「renderer 不能提交任意二进制路径」在实现上的落点——不是靠文档约束，而是**接口里根本没有那个参数**。

### D3 preload 是便利层，不是信任边界

preload 自己也校验参数，但主进程**再校验一次**并核对 sender 与来源。preload 运行在被渲染内容影响的进程里，把它当成边界等于没设边界。

### D4 打包形态的 origin 必须按 scheme 判定

以 `file://` 加载的文档，`new URL(url).origin` 是字符串 `"null"`，而不是 `"file://"`。拿 `origin` 去和 `file://` 比较会拒绝**每一个**合法调用——这个错误在本 Change 的实现过程中真实发生过，并被 smoke 抓到。正确做法是按 scheme 判定，文档身份由「是自身窗口」承担。

### D5 打包 preload 必须是单文件

沙箱 preload 运行在一个没有模块解析器的环境里：`require('../shared/wire.js')` 会以 `module not found` 失败，桥接对象根本不出现，页面随之空白。因此 main 与 preload 都用 esbuild 打成自包含 CommonJS。`tsc` 只负责类型检查，不再产出运行产物——**产物形状由构建步骤保证，而不是由源码组织方式碰巧满足**。

### D6 host 的失败必须携带它自己的诊断

子进程死亡时唯一的线索是它的 stderr。因此 stderr 有界保存（环形缓冲），并在失败时随错误一起抛出。一个只有「exited code=1」的错误会让每一次排查都从零开始——本 Change 的调试过程反复证明了这一点。

### D7 验证必须隔离真实数据

`ProjectContext` 的运行时目录默认是 `ROLLO_RUNTIME_DIR` 或 `~/.rollo`。smoke 若不显式覆盖，host 会去读**开发者真实的会话**（实测扫出 26 个）。因此 e2e 必须把 `ROLLO_RUNTIME_DIR` 指向临时 workspace 内的目录。这既是"不写真实会话"的要求，也是让断言可复现的前提。

### D8 消费协议后的结论：协议本身没有暴露缺口

真实消费过程中发现的问题**全部在壳这一侧**，协议层一个都没有：

- preload 需要被打包（D5）——Electron 的约束，与协议无关；
- `file://` origin 判定（D4）——壳的安全校验，与协议无关；
- 一个选项键名与字段名不匹配（内部实现错误）；
- 测试装置选错解释器、以及没隔离运行时目录（验证方法问题）。

协议侧唯一被真实用到而此前未被充分验证的是：**快照信封的 `ordinal` 就是续传边界**、**`not_implemented` 与 `METHOD_NOT_FOUND` 可区分**、以及**控制能力缺失能被界面如实呈现**。这三条都在 smoke 里被断言。

## Risks / Trade-offs

- **控制缺口**：界面不能发起运行。风险是被误读为"功能不全"。缓解：`capabilities.declared_not_implemented` 来自 host，界面直接显示，不靠文档解释。
- **Electron 二进制体积与获取**：`npm install` 会去 GitHub release 拉 ~100MB，链路慢时会长时间无输出、看起来像挂死。已在 `desktop/.npmrc` 配置镜像，使新克隆的安装方式一致。
- **`session.list` 的游标是偏移量**（继承自 C05）：会话增删会漂移。列表短且界面会重拉，当前够用。
- **单实例**：多实例被显式拒绝。若将来需要"同一 workspace 两个窗口"，那是窗口管理而非 worker 管理，届时再决定。
- **Playwright 的 Electron 支持不驱动原生对话框**：因此自动化用 `ROLLO_AUTO_WORKSPACE` 注册工作区，走的是与选择器**同一注册表入口**，其余行为不变；交互式启动仍走原生选择器。

## Migration Plan

纯新增。既有 CLI/TUI 不受影响：它们不启动 Electron，也不依赖 `desktop/`。

## Open Questions

- 控制切片是否与 C07 合并，取决于何时需要从界面发起运行。
- 何时把 `desktop/` 纳入仓库的 CI（当前只有本地脚本）。
