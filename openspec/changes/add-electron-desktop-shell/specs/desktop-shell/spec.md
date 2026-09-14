## Purpose

规定 Electron 桌面壳的进程结构、worker 托管、桥接暴露面与窗口生命周期，使渲染进程能在拿不到 Node 与任意 IPC 的前提下，经主进程托管的 Python host 观察一个工作区。

## ADDED Requirements

### Requirement: 渲染进程不得拥有 Node 或任意 IPC 原语

窗口 MUST 以 `contextIsolation: true`、`nodeIntegration: false`、`sandbox: true` 创建。preload MUST 只经 `contextBridge` 暴露**具名业务方法**，MUST NOT 暴露 `invoke`、`send` 或任何按通道名调用的通用入口。窗口 MUST NOT 导航离开自身文档，MUST NOT 打开新窗口。

#### Scenario: 渲染进程的能力面

- **WHEN** 在页面里检查全局对象
- **THEN** `require` 与 `process` 均不存在，桥接对象只含具名方法，不含 `invoke`/`send`

#### Scenario: 外链与新窗口

- **WHEN** 页面内容尝试打开新窗口或导航到其它地址
- **THEN** 两者都被拒绝，窗口停留在原文档

### Requirement: 桥接方法必须逐一具名并双向校验参数

preload MUST 为每个业务操作提供具名方法，并在调用前校验自身参数；主进程 MUST 再校验一次，因为 preload 是便利层而不是信任边界。

#### Scenario: 非法参数

- **WHEN** 渲染进程以空字符串或非字符串作为 `workspace_id` 调用
- **THEN** 调用被拒绝，且不产生任何子进程或文件操作

### Requirement: 主进程必须校验 sender 与来源

每个 IPC 处理器 MUST 校验调用方是自身窗口之一，且其来源在允许列表内。以 `file://` 加载的文档其 origin 为字符串 `"null"`，校验 MUST 按 scheme 判定而不是拿 `origin` 与 `file://` 比较。

#### Scenario: 打包形态下的合法调用

- **WHEN** 窗口以 `file://` 加载并调用任一桥接方法
- **THEN** 调用被接受

#### Scenario: 非本窗口的调用

- **WHEN** 消息来自未知 webContents
- **THEN** 调用被拒绝并给出明确原因

### Requirement: 渲染进程不得提交文件系统路径或可执行文件

渲染进程 MUST 只持有主进程签发的**不透明** `workspace_id`。工作区目录 MUST 只能经原生目录选择器进入注册表。解释器路径 MUST 在主进程解析并校验，MUST NOT 由渲染进程提供。

#### Scenario: 会话摘要不泄露路径

- **WHEN** 渲染进程列出会话
- **THEN** 返回的摘要只含身份字段，不含 canonical 数据库路径

### Requirement: 每个 workspace 一个 host，失败必须可见

主进程 MUST 为每个已注册 workspace 托管一个 host 子进程，MUST 以参数数组、`shell=false`、固定 cwd 启动，MUST NOT 拼接 shell 字符串。同一目录重复注册 MUST 复用同一条目。host 启动失败时该条目 MUST 保留在表中并带上诊断，MUST NOT 静默消失。

#### Scenario: 解释器无法解析

- **WHEN** 无法解析出可用的 Python 解释器
- **THEN** 该 workspace 状态为失败，并给出指向具体配置项的说明

#### Scenario: 重复注册

- **WHEN** 同一目录被注册两次
- **THEN** 返回同一个 `workspace_id`，且不产生第二个 host

### Requirement: stderr 必须有界

host 的 stderr MUST 被有界保存（环形缓冲），MUST NOT 无限累积。host 异常退出时，其最后的诊断 MUST 随失败一起呈现。

#### Scenario: host 崩溃

- **WHEN** host 进程异常退出
- **THEN** 等待中的调用收到失败，且失败信息包含该进程最后的 stderr 输出

### Requirement: 窗口刷新不得中止 worker

刷新或重新加载页面 MUST 只影响渲染进程：主进程与 host 子进程 MUST 继续运行，且刷新后 MUST 能重新取得同一 workspace 与其连接。

#### Scenario: 刷新后仍连着

- **WHEN** 页面被重新加载
- **THEN** 窗口数量与主进程状态不变，重新查询即可再次取得该 workspace 并握手

### Requirement: 退出必须收尾且不留孤儿进程

应用退出前 MUST 先关闭所有 host，再执行退出。应用 MUST 为单实例：第二次启动 MUST 聚焦已有窗口，而不是再起一套 worker。

#### Scenario: 应用退出

- **WHEN** 关闭最后一个窗口
- **THEN** 所有 host 被关闭，进程以 0 退出，且没有遗留的 Python 子进程

#### Scenario: 第二次启动

- **WHEN** 应用已在运行且用户再次启动
- **THEN** 已有窗口被聚焦，不产生新的 host

### Requirement: 只读视图不得暗示它具备控制能力

renderer MUST 只提供其桥接面支持的操作。当协议声明控制方法未实现时，界面 MUST 如实呈现这一事实，MUST NOT 提供会必然失败的操作入口。

#### Scenario: 控制能力缺失

- **WHEN** 用户查看宿主信息
- **THEN** 控制能力显示为「不在本次构建中」，界面没有发起运行的按钮
