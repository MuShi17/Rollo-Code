## Why

C05 交付了 stdio 协议，但它此前**从未被真正的消费方使用过**——测试驱动的是它自己。一个没有消费者的协议无法证明自己是够用的：字段缺不缺、错误码够不够区分、关闭是否可预期，都要等真的有人按它做事才会暴露。

本 Change 建立 Electron 桌面壳：主进程按 workspace 托管 host 子进程，preload 只暴露具名业务方法，renderer 用一个只读视图消费协议。它同时是 C05 的第一次真实消费。

## What Changes

- 新增 `desktop/`：Electron + React + TypeScript + Vite 工程，`typecheck` / `build` / `test` / `test:e2e` 四个脚本，依赖锁入 `package-lock.json`。
- 主进程：`host-client`（协议客户端）、`workspaces`（每 workspace 一个 host 的注册表）、`ipc`（唯一 IPC 面 + sender 校验）、`sidecar`（解释器解析）。
- preload：`contextBridge` 暴露**具名方法白名单**，没有通用的 `invoke(channel, …)`。
- renderer：只读视图——选 workspace、列会话、看快照与增量。**不提供任何会改变运行时状态的命令**，因为 C05 的观测切片里没有这样的方法。
- 测试：`tests/main`（单测 + **真实 Python 子进程**的跨进程集成测试）、`tests/e2e`（**真实 Electron 窗口** smoke）。

## 不做什么

- **不实现控制路径**。`run.start` / `run.cancel` / `interaction.respond` / `content.read` 在 C05 里是 `not_implemented`，界面因此不能发起运行。这是**声明的切片边界**，界面上如实显示为 `control: not in this build`。
- 不做打包（属 C07）；不新增网络端口；不改动 `src/rollo/**` 的任何既有模块。

## Impact

- 受影响代码：仅新增 `desktop/`；`src/rollo/` 零改动。
- 消费方关系：renderer → preload 白名单 → 主进程 IPC → `HostClient` → stdio → `rollo.host` → `Application` / C04 订阅服务。整条链在 smoke 里被真实跑通。
- 对 C05 的反馈：见 `design.md` 的 D8——消费过程中发现的两处协议无关缺陷都在壳这一侧，协议本身没有暴露缺口。
