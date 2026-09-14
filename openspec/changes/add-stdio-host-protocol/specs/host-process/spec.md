## Purpose

规定 host 进程的生命周期与观测语义：入口启动方式、读循环与订阅的并发关系、会话枚举的边界、快照与增量的投递，以及关闭行为，使桌面主进程能与一个真实 Python 子进程保持可预期、可诊断的连接。

## ADDED Requirements

### Requirement: 入口必须可被主进程以参数数组启动

host MUST 提供 `python -m rollo.host` 入口，只接受显式参数（workspace 与可选 runtime 目录），MUST NOT 解析 shell 字符串，MUST NOT 启动 CLI 的 REPL 或读取交互式输入。

#### Scenario: 以参数数组启动并握手

- **WHEN** 主进程以 `-m rollo.host --workspace <dir>` 启动真实子进程并发送 `host.initialize`
- **THEN** 子进程返回协议版本与 `host_epoch`，且不打印任何非协议内容到 stdout

### Requirement: 读循环必须与订阅投递并发

等待输入 MUST NOT 阻塞订阅投递。取消或关闭 MUST NOT 依赖对端再发送数据。

#### Scenario: 空闲连接期间订阅仍然投递

- **WHEN** 订阅已建立而客户端不再发送任何帧
- **THEN** 该订阅的增量事件仍然被投递到 stdout

### Requirement: 会话枚举必须有界且不暴露文件系统路径

`session.list` MUST 只返回当前 workspace 的会话，MUST 支持 `limit` 与 `page_cursor` 分页，MUST NOT 返回 canonical 数据库的绝对路径。

#### Scenario: 分页

- **WHEN** 存在 3 个会话且客户端以 `limit=2` 请求两次
- **THEN** 第一次返回 2 条并给出下一页游标，第二次返回 1 条且游标为 null

#### Scenario: 摘要不泄露路径

- **WHEN** 客户端列出会话
- **THEN** 每条摘要只含身份类字段，不含 `canonical_path`

### Requirement: 观测只读，且不因打开会话而产生副作用

`session.snapshot` 与 `events.subscribe` MUST 只读：打开一个会话的 store MUST NOT 创建会话、触发恢复或写入控制记录。

#### Scenario: 观察一个已存在的会话

- **WHEN** 客户端对一个已存在的 session 取快照并订阅
- **THEN** 该 session 的 canonical 内容与控制记录不因观察而改变

### Requirement: 订阅必须承载快照边界与传输序号

订阅的 wire 事件 MUST 携带 `subscription_id`、`session_id`、`host_epoch`、`transport_seq`、`kind` 与 payload。第一条 MUST 是快照，且其信封 `ordinal` MUST 等于快照自身的 `high_water`——客户端据此续传。

#### Scenario: 快照边界可续传

- **WHEN** host 对一个 `high_water = H` 的 session 投递快照
- **THEN** 该事件的 `ordinal` 与 `payload.high_water` 同为 H

#### Scenario: 增量按序到达

- **WHEN** 订阅建立后 canonical 新增一条事件
- **THEN** 该事件作为一条 `kind = "event"` 的 wire 事件到达，`transport_seq` 单调递增，且 `prefix_boundary_exempt` 为 false

### Requirement: 续传不可覆盖时必须显式过期

`events.subscribe` 携带的 cursor MUST 在可续传时续传；缓冲不覆盖、`projection_version` 不一致或 host 实例更替时 MUST 返回 `cursor_expired` 并给出当前 `high_water`，MUST NOT 静默从当前边界继续。

#### Scenario: 畸形游标被拒绝

- **WHEN** 客户端提交结构不完整的 cursor
- **THEN** 返回非法参数错误，订阅不被建立

### Requirement: 退订只停止观察

`events.unsubscribe` MUST 只结束该订阅的投递，MUST NOT 停止对应的 run。

#### Scenario: 退订后 run 不受影响

- **WHEN** 客户端退订一个正在观察的 session
- **THEN** 该订阅从 host 的订阅表移除，runtime 侧不受影响

### Requirement: 关闭必须有序

`host.shutdown` MUST 先停止接收新的 start 语义请求，结束订阅，再确认退出。stdin 关闭 MUST 产生与之一致的收尾：订阅被清理、Application 被关闭。

#### Scenario: 显式关闭

- **WHEN** 客户端调用 `host.shutdown`
- **THEN** 返回 `shutting_down`，订阅表为空，进程以 0 退出

#### Scenario: stdin 关闭

- **WHEN** 对端关闭 stdin
- **THEN** 读循环结束并执行同样的收尾
