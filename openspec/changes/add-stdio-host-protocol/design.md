## Context

C03 交付了进程内控制面（`Application`、session 租约、run 生命周期），C04 交付了进程内读模型（有界投影 + 原子快照 + 可续传订阅）。GUI 是默认入口，但它在另一个进程里，因此缺的不是能力而是**边界**：一条能把已有只读面暴露出去的 stdio 协议。

任务卡 §3.1/§3.2 已经冻结了传输约定与方法表，本 Change 按那份契约实现，不重新设计 wire。

## Goals / Non-Goals

**Goals**

- 一个可被主进程以参数数组启动的真实 Python host，stdout 只承载协议。
- 观测闭环：`initialize` → `session.list` → `session.snapshot` / `events.subscribe` → 增量 → `events.unsubscribe` → `host.shutdown`。
- 分帧、上限、版本协商、错误映射达到「有 oracle 可验证」的程度。

**Non-Goals**

- `run.start` / `run.cancel` / `interaction.respond`：控制路径，各自有独立验收规则（取消必须反映真实 OS 状态、审批绑定执行参数），本 Change 不实现也不声称实现。若需要，另开 Change。
- `content.read`：正文分页读取。GUI 目前不展示大正文，且它需要 scope 检查，与控制切片的边界更近。
- Electron、打包、网络端口、真实 Provider 调用。

## Decisions

### D1 观测切片先于控制切片

先交付观测，是因为它的读模型与投递语义在 C04 已经验证完毕，host 只需做传输适配；而控制切片要跨进程重新验证会话租约、命令幂等与取消传播，成本与风险都高一个量级。分两步走可以让 GUI 先看到东西，而不必先把最难的部分做对。

代价：GUI 暂时无法从界面发起运行。这是明确的功能缺口，不是遗漏——wire 上以 `not_implemented` 显式暴露，客户端可据此给出可操作提示。

### D2 host 是薄适配层，不持有能力

控制类命令（未来的 `run.start` 等）一律转发 `Application.dispatch`，host 不做策略、不做权限、不碰 canonical。观测面直接调用 C04 的 `SubscriptionService`。理由是权限与事实源都已在别处定义好；host 一旦自己实现一层判断，就会出现第二个授权源。

### D3 未实现的方法用 `not_implemented` 而不是「未知方法」

两者对客户端的意义完全不同：前者是「这个能力还没做」，后者是「你调错了」。冻结的方法表已经声明了这些名字，所以 host 必须能区分。实现上以 `CONTROL_METHODS` 常量声明，并在 `capabilities.declared_not_implemented` 里如实列出。

### D4 分帧必须只由换行决定，超限即关闭

半帧、合并帧与 UTF-8 拆分都由「缓冲到换行」自然处理。超限则不同：一旦缓冲超过上限仍无换行，帧边界已不可信，继续下去只是猜测。契约因此选择**关闭连接并留诊断**，把「重连」的决定留给客户端。

### D5 stdout 重定向为进程级

协议要求 stdout 只含帧，而任何一处未预期的 `print` 都会破坏它。只在写帧处加锁不够——那是「我这一侧」的保证。因此 host 在服务期间把进程级 `sys.stdout` 指向 stderr，把编码后的帧写到**保存下来的**真实流。这样误输出会出现在 stderr（可诊断），而不是污染 wire。

### D6 打开 store 是读操作

`_store_for` 复用 `runtime_store_path` 并只做打开，不创建 session、不触发恢复、不写控制记录。这是「只读客户端不得触发 schema 迁移或 startup recovery」在实现上的落点；也是让一个客户端能观察它并不持有的 session 的前提。

### D7 读循环用线程执行阻塞读，不占用事件循环

阻塞读必须在工作线程里做，否则空闲等待会冻结订阅投递。调用方在两次读之间检查是否已请求关闭，因此关闭不必等对端再发数据。

## Risks / Trade-offs

- **控制缺口**：界面暂时不能发起运行。缓解：能力块如实声明，客户端可提示；这也让 C06 可以先做只读视图。
- **`session.list` 的游标是偏移量**：会话增删会让偏移漂移。当前够用（列表很短且 GUI 会重新拉取）；若将来需要稳定分页，应改为按 session_id 的键集游标。
- **一条真实子进程用例**：进程级失败（编码、启动路径、stdin 语义）只有在真子进程里才暴露，但多进程用例天然更慢也更易 flake。当前只保留一条覆盖握手、拆帧与关闭的用例。
- **`not_implemented` 的稳定性**：客户端可能把它当成永久状态。文档已写明这是切片边界，控制 Change 落地后该错误会消失。

## Migration Plan

纯新增，无迁移。既有 CLI/TUI 不受影响：它们不构造 `HostServer`。

## Open Questions

- 控制切片是否与 C06 的界面需求合并成一个 Change，取决于 GUI 何时需要「发起运行」。
- `content.read` 的 scope 检查应在 host 还是 Application 侧？倾向 Application，避免 host 成为第二授权源。
