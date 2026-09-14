## Purpose

规定桌面主进程与 Python host 之间的 stdio 传输：分帧、上限、版本协商、方法分派与错误映射，使 `Application` 的只读面能被一个真实子进程客户端消费，同时保证 stdout 只承载协议。

## ADDED Requirements

### Requirement: stdout 必须只承载协议帧

host MUST 保证进程的 stdout 只包含协议帧，一个 JSON 对象一行、UTF-8、LF 终止。任何人类可读输出（日志、诊断、未预期的 `print`）MUST 走 stderr。

#### Scenario: 未预期的 stdout 输出不污染协议

- **WHEN** host 运行期间进程内任意位置向 `sys.stdout` 写入一行文本
- **THEN** 该文本出现在 stderr，协议流中不出现任何非 JSON 帧

### Requirement: 分帧必须容忍任意读边界

客户端与 host MUST NOT 假定一次读取等于一帧。host MUST 能处理半帧（缓冲至换行）、一次读取含多帧、以及一个 UTF-8 序列被拆到两次读取之间。

#### Scenario: 帧被拆到两次读取

- **WHEN** 一帧的前半段到达后后半段才到达
- **THEN** host 在前半段到达时不产生任何帧，在换行到达时恰好产生一帧

#### Scenario: 多字节字符被拆开

- **WHEN** 一帧内含非 ASCII 字符且该字符的 UTF-8 字节被拆到两次读取
- **THEN** 字符被正确重组，帧内容与原文一致

### Requirement: 单帧上限必须被强制

单帧 MUST 不超过具名上限 `MAX_FRAME_BYTES`（1 MiB）。超过上限时 host MUST 关闭连接并在 stderr 留下诊断，MUST NOT 尝试猜测分帧边界后继续。

#### Scenario: 无换行的超长输入

- **WHEN** 输入超过上限仍未出现换行
- **THEN** host 以 `frame_too_large` 错误结束连接，并在 stderr 留下诊断

### Requirement: 版本必须协商而非猜测

首条调用 MUST 为 `host.initialize`，携带 `version`。版本不等于 host 支持的版本时 MUST 返回明确错误并保持未初始化状态，MUST NOT 猜测兼容。`host.initialize` 之前的任何其他方法 MUST 被拒绝为 `not_initialized`。

#### Scenario: 不支持版本

- **WHEN** 客户端以 `version=2` 调用 `host.initialize`
- **THEN** 返回 `unsupported_version` 且响应 data 里给出受支持版本，host 仍为未初始化

#### Scenario: 未初始化即调用

- **WHEN** 客户端在 `host.initialize` 之前调用 `session.list`
- **THEN** 返回 `not_initialized`

### Requirement: 错误必须映射到稳定码

标准错误 MUST 用 JSON-RPC 标准码；业务错误 MUST 在 `error.data.code` 使用已声明的具名码。未声明的业务码 MUST 在构造时即被拒绝，不得出现在 wire 上。畸形输入 MUST 产生错误响应而不终止连接；只有分帧不可信（超限）才允许关闭连接。

#### Scenario: 畸形 JSON 不终止连接

- **WHEN** 客户端发送一行非法 JSON，随后发送一帧合法请求
- **THEN** 非法那行得到解析错误响应，随后的合法请求正常得到结果

#### Scenario: 已声明但未实现的方法

- **WHEN** 客户端调用冻结方法表中已声明、但本切片未实现的 `run.start`
- **THEN** 返回 `not_implemented`，与「未知方法」的 `METHOD_NOT_FOUND` 可区分

### Requirement: 能力与限制必须如实声明

`host.initialize` 的返回 MUST 声明 `protocol_version`、`host_epoch`、workspace 身份、已实现能力、未实现但已声明的方法、是否支持 batch，以及分帧与分页限制。MUST NOT 声明未实现的能力。

#### Scenario: 未实现的控制能力

- **WHEN** 客户端读取 `capabilities`
- **THEN** `control` 为空列表，且未实现的方法出现在 `declared_not_implemented` 中，`batch` 为 `false`

### Requirement: 缺失配置只报名称不报值

`host.initialize` MUST 报告缺失的配置项**名称**以便 GUI 提示用户，MUST NOT 在 wire 上回显任何配置项的值。

#### Scenario: provider 配置缺失

- **WHEN** 环境缺少 provider 凭据
- **THEN** 响应里列出缺失项名称，且响应内容不包含任何凭据值
