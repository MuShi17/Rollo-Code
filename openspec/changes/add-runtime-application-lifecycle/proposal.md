## Why

C02 已将 runtime 的输出与人工交互从终端渲染中解耦，但当前运行仍主要由 CLI 直接持有 `Agent`，缺少可供 TUI、未来 GUI 和受控进程内调用方复用的统一 session/run 控制边界。现在建立 Application API、workspace owner 与可恢复的控制记录，才能在继续接入 GUI/host 之前明确一次运行的身份、取消、关闭和崩溃恢复语义。

## What Changes

- 新增统一的 Application API，提供 `session.create/list`、`run.start/status/cancel/resume`、`interaction.respond`、`owner.reconcile` 与 `shutdown` 入口；命令携带稳定身份和参数摘要，重复命令按幂等规则返回已有结果。
- 新增按规范化 workspace 隔离的 OS 执行锁与 owner 树：同一 workspace 同时最多一个 active root run，child run 只能在同一 owner 树内运行，不依赖 UI 按钮或 PID 文件实现互斥。
- 增加版本化的 session/run/command/owner/pending-interaction 控制记录，支持旧 session 读取与 schema 迁移；崩溃恢复按 operation/canonical evidence 矩阵标为未接受、interrupted 或 uncertain，root crash 进入 owner quarantine，禁止自动重放可能产生副作用的工具。
- 将模型流、交互等待、受管理 shell 和子 Agent 的取消统一接入公开控制接口；区分任务取消与 OS 子进程实际停止，处理 stdout/stderr drain、优雅停止、强制终止和关闭超时的未完成结果。
- 让既有 one-shot、REPL、resume 和 TUI 适配器改走 Application API，保持 flags、权限模式、退出码、REPL 命令及 C02 输出/交互端口语义；Harbor 仅作为离线消费者契约核对，不运行付费 benchmark。
- 不改变 C02 的 canonical 事件事实源，不实现 stdio host、GUI 投影、Electron 壳或 Windows 安装包；不新增跨进程协议，也不自动接管正在运行的会话。

## Capabilities

### New Capabilities

- `runtime-application-api`: session/run/interaction/shutdown 的公开入口、命令身份、状态、幂等与同 workspace 运行约束。
- `runtime-execution-ownership`: workspace 级 owner 锁、child owner 树、受管理 shell 的取消/终止状态与关闭顺序。
- `runtime-control-persistence`: 版本化控制记录、参数摘要、旧数据迁移、重启恢复、中断与 uncertain 语义，以及不自动重放副作用工具的边界。
- `tui-application-integration`: one-shot/REPL/resume 通过 Application API 使用 C02 端口，保持 CLI/TUI 兼容并提供离线消费者验证边界。

### Modified Capabilities

（`openspec/specs/` 当前只有 `.gitkeep`，没有已归档的 main capability；本 Change 不修改既有 capability requirement。）

## Impact

- 新增 `src/rollo/application.py`、`src/rollo/workspace_lock.py` 及对应的 Application/TUI 测试。
- 修改 `session.py`、`runtime_store.py`、`run_lifecycle.py`、`agent.py`、`tools.py`、`tui_adapter.py`、`__main__.py` 的运行控制、取消、关闭和历史 session 接线；复用 C02 的 `OutputPort`/`InteractionPort`，不重写 Rich 渲染。
- 可能扩展现有本地 runtime 数据目录中的控制表和迁移逻辑；必须保持旧 canonical/partial 数据可读，敏感值不进入控制记录。
- 不引入新的第三方依赖；验证使用临时 workspace/runtime 目录、受控本地子进程和离线 Provider/worker 替身，分别标注 mock、真实 Python 进程和 CLI/TUI consumer 证据。
- 本 Change 的实现前置为中文 design/specs/tasks、独立设计评审、本 Change 测试策略和主 Agent D(C03) 接受；在这些 Gate 完成前不修改 C03 产品代码。

## C03 Gate closure contract

- 本 Change 以当前仓库 `Rollo-Code` 的实际 checkout、当前 HEAD 和 Python `>=3.11` 环境作为证据身份；旧交接文档中的 `D:/workspace/My-Claude-Code` 与 `21b9bdf` 仅作历史输入，不能作为实现基线。
- D(C03) 必须同时满足：设计审查确认 control/canonical 边界、Future 唤醒、稳定 lock namespace、旧 session 归属、plan/REPL contract、run-level cancel 去重和 tool-call digest 绑定；测试策略审查确认真实 crash/restart、多入口 owner、双流背压/OS 存活、shutdown 屏障以及 CLI/TUI consumer 的可复现判据。
- 审查意见必须由主 Agent 逐条核对源码/工件证据后采纳；独立角色的 `sufficient` 或 `REVISION_REQUIRED` 只作为证据，不自动改变授权或状态。
