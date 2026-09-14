## 1. 传输与分帧

- [x] 1.1 新增 `src/rollo/host/protocol.py`：`HostProtocol` 增量解码（缓冲到换行）、`Frame` 载体、具名上限常量、`ProtocolError`（JSON-RPC 码 + 业务码）与 `encode`（唯一 stdout 出口）。以「半帧/多帧/UTF-8 拆分/空行/畸形输入」测试验证。
  - 证据：`test_host_protocol.py` **17 collected**（其中 4 条来自 `parametrize` 展开）。上限常量：`MAX_FRAME_BYTES=1MiB`、`TEXT_DELTA_BYTES=32KiB`、`MAX_PAGE_ITEMS=100`、`PAGE_BODY_TARGET_BYTES=256KiB`、`PREVIEW_BYTES=16KiB`、`BODY_PAGE_*(64KiB/256KiB)` —— 与任务卡 §3.1 逐项一致。
- [x] 1.2 业务码必须在构造期校验，未声明的码不得上 wire。以 `test_business_codes_are_declared_or_rejected` 验证（`ProtocolError(business="made_up_code")` → `ValueError`）。
- [x] 1.3 单帧超限必须关闭连接并留诊断，不得继续猜分帧。以 `test_a_frame_without_a_newline_over_the_limit_is_rejected`、`test_a_complete_frame_over_the_limit_is_rejected`、`test_an_oversized_frame_closes_with_a_diagnostic` 验证。

## 2. Host 服务

- [x] 2.1 新增 `src/rollo/host/server.py`：`HostServer` 持有 `Application` 与 C04 `SubscriptionService`；控制类命令一律转发 `Application.dispatch`，观测面直连订阅服务；host 自身不做策略与权限判断。
- [x] 2.2 `python -m rollo.host --workspace <dir> [--runtime-dir <dir>]` 入口，只接受显式参数、不解析 shell 字符串、不启动 REPL。以 `test_the_entry_point_serves_a_real_child_process` 验证（真实 `sys.executable -m rollo.host` 子进程 + 真实管道）。
- [x] 2.3 stdout 只承载协议：服务期间把进程级 `sys.stdout` 指向 stderr，帧写到保存下来的真实流。以 `test_stdout_carries_only_protocol_frames` 验证。
- [x] 2.4 读循环与订阅投递并发：阻塞读在工作线程执行，调用方在两次读之间检查关闭请求。以 `test_snapshot_and_subscription_deliver_a_live_event`（客户端停止发送后仍收到增量）验证。
- [x] 2.5 打开会话的 store 是只读操作：复用 `runtime_store_path`，不创建 session、不触发恢复、不写控制记录。

## 3. 方法表

- [x] 3.1 `host.initialize`：协商 `protocol_version`、返回 `host_epoch`/workspace/能力/限制/缺失配置**名称**。以 `test_initialize_reports_version_epoch_and_capabilities` 验证；未支持版本以 `test_an_unsupported_version_is_refused_without_guessing` 验证。
- [x] 3.2 未初始化即调用其他方法必须被拒绝。以 `test_commands_before_initialize_are_refused` 验证。
- [x] 3.3 已声明但未实现的控制方法必须回答 `not_implemented`，与未知方法的 `METHOD_NOT_FOUND` 可区分。以 `test_declared_control_methods_answer_not_implemented`、`test_an_unknown_method_is_reported_as_method_not_found` 验证。
- [x] 3.4 `session.list`：限定 workspace、支持 `limit`/`page_cursor`、**不返回 canonical 路径**。以 `test_session_list_is_scoped_and_hides_filesystem_paths`、`test_session_list_pages_and_reports_a_cursor` 验证。
- [x] 3.5 `session.snapshot`、`events.subscribe`（含 cursor 续传与 `cursor_expired`）、`events.unsubscribe`、`host.shutdown`。以 `test_the_snapshot_ordinal_is_the_resume_boundary`、`test_a_malformed_cursor_is_rejected`、`test_shutdown_stops_the_server_and_closes_subscriptions` 验证。
- [x] 3.6 wire 事件信封：`subscription_id`/`session_id`/`host_epoch`/`transport_seq`/`kind`/payload，快照信封 `ordinal` 等于 `payload.high_water`。以 3.5 的用例验证。

## 4. 分层测试

- [x] 4.1 `test_host_protocol.py`：分帧与错误映射，纯函数，L1。
- [x] 4.2 `test_host_process.py`：真实读循环 + 脚本化 stdin + 捕获协议流，L1；另含**一条真实子进程**用例（L2）覆盖握手、拆帧与关闭。
- [x] 4.3 全量回归与 `compileall`、`openspec validate --strict`。
- [x] 4.4 变异验证：对分帧上限、版本闸门、初始化闸门、stdout 重定向、`not_implemented` 区分、会话摘要不泄露路径逐条注入变异，**6/6 detected**，且先做阴性对照确认 harness 在未变异代码上为绿。
  - 提交前自审又补一条：**未签发的 session id 不得创建数据库**。撤掉守卫 → 该用例变红，控制组绿（见 `implementation-status.md` §8）。全量 Python 因此 599 → **600 passed**。
- [ ] 4.5 独立对抗性审查（**进行中**，与 C06 一并执行；通过后才提交）。

## 5. Gate 与交付边界

- [x] 5.1 确认本 Change **未修改**任何既有 `src/rollo/**` 模块：仅新增 `src/rollo/host/` 四个文件与两个测试文件。以 `git status --short -uall` 的文件集合验证。
- [x] 5.2 结果回写 `implementation-status.md`。
- [ ] 5.3 仅在用户另行授权后执行 commit/push/PR。
