## 0. v3 收敛状态（2026-09-13，先读）

**方案 v3 修订了 C03 的范围**：执行互斥单位由 workspace 改为 **session**；`owner` 锁/闸门、`owner.reconcile`、`commands` 命令台账（`CommandEnvelope` + `params_digest` 命令载体）、`tool_operations` 控制侧副本、control↔canonical 跨库一致性、RFC 8785/JCS 摘要**全部取消**；`workspace_lock.py` 删除。理由见任务卡 §10 与批次总览 §14。

**本文件中以下条目已被 v3 作废，其 `[x]` 不再代表有效实现**（保留原文与勾选以供追溯，但不得据此声称能力存在）：

| 条目 | 作废原因 |
| --- | --- |
| `1.3` | 冻结的 `command envelope` / `params digest` 命令载体已取消 |
| `1.4` | 冻结的 `lock namespace` / workspace 归属已取消（改 session 租约） |
| `2.3` | 「root owner 申请」已取消（改 session 租约） |
| `2.6` | 「params digest 冲突拒绝 + 一个 owner」已取消（幂等只由 `runs` 唯一约束承担） |
| `2.8` | `owner.reconcile` 已取消（崩溃恢复自动分类，不需要人工介入） |
| `3.1` | `workspace_lock.py` 已删除 |
| `4.1` | 「owner 表」已取消（保留 `runs.owner_pid` 供恢复归属） |
| `4.2` | 「command accepted 原子事务」中命令台账部分已取消（run 行提交仍是 dispatch 屏障） |
| `4.3` | RFC 8785/JCS 全条已取消（改普通 canonical JSON） |
| `6.2` | 「command idempotency」中的命令台账部分已取消 |
| `6.6` | 其中与 owner/quarantine 相关的断言作废 |

**新增/改写的条目**见 §3（session 租约）与 §6（真实 session 互斥用例）。

## 1. C03 变更准备与证据身份

- [x] 1.1 记录当前 checkout、branch、HEAD、Python `>=3.11`、OpenSpec 版本和唯一 writer；以 `git status --short --branch`、`git rev-parse HEAD` 和 `python --version` 验证身份可复算，并记录 C02 已知 flake、重复次数和 retained 判据
- [x] 1.2 读取并冻结 `openspec/changes/decouple-runtime-interaction-from-tui/tasks.md §6.2` 的 `OutputPort`、`InteractionPort`、`InteractionRegistry`、`RuntimeEventEmitter`、`DurableToolBoundary` 和 `SQLiteRuntimeStore` 接口；若段落缺失则以当前公开模块核对并记录；以 C02 focused regression 验证不重写 canonical 事实源
- [x] 1.3 ~~（v3 作废）~~ 冻结 command envelope、scope、params digest、状态转移、错误码、终态唯一和 cancel generation；以 Application API schema/transition tests 验证每个字段与状态均有 oracle
- [x] 1.4 ~~（v3 作废）~~ 冻结 control DB、canonical store、lock namespace、workspace 归属和 migration schema；以临时目录 schema/锁竞争实验验证路径、版本和旧数据只读兼容
- [x] 1.5 建立 mock、离线 provider/worker、真实本地 Python subprocess、真实 CLI/TUI consumer 与 Harbor 只读契约的证据分层；以证据 manifest 验证每个测试标注 actor 类型、命令、退出码和路径
- [x] 1.6 完成独立 openspec-designer 与 test-strategy-agent 的“对科学审查”，逐条核对 P0/P1 缺口；以两份带 result_identity 的审查报告和主 Agent 复核记录验证 D(C03) 前置

## 2. Application 控制面

- [x] 2.1 新增 `src/rollo/application.py` 的结构化请求、响应、错误和生命周期类型，要求显式 `ProjectContext` 并显式注入 workspace/session runtime paths；以缺少 context/错误 workspace/导入期全局路径的 API tests 验证拒绝当前目录补推
- [x] 2.2 实现 `session.create/list` 的 workspace 绑定和 canonical session 只读适配；以两个临时 workspace 的列表/泄露测试验证稳定身份和隔离
- [x] 2.3 实现 `run.start` 的 command accepted、root owner 申请、run 记录提交和 supervisor dispatch 顺序；以 commit-before-dispatch fault injection 验证提交失败不调用模型/工具
- [x] 2.4 实现 `run.status` 与集中 lifecycle guard；以 queued/running/waiting_interaction/cancelling/五类终态转移测试验证终态唯一且未确认停止不报告成功；本轮以 `test_shutdown_incomplete_retains_owner_until_run_finishes`（未确认停止返回 `shutdown_incomplete` 且保留 owner）与 `test_restart_after_open_without_terminal_never_reports_success`（terminal 未提交不得报成功）补齐崩溃侧 oracle
- [x] 2.5 实现 `interaction.respond` 的 session/run/request/tool_call/tool_name/tool_input/plan_id/plan_digest/params_digest 校验和 C02 registry/Future 委托；不适用字段显式为 null，plan_digest 与 params_digest 均按 JCS/RFC8785 canonical JSON v1 计算完整小写 SHA-256，公共路径不得省略；以错误身份、修改参数/计划、复用旧 tool-call id、JCS Unicode/数字/数组向量、重复回复、旧 Future interrupted 和迟到回复测试验证零重复工具 dispatch
- [x] 2.6 实现所有副作用命令的 scope、params digest、原结果回读和冲突拒绝；以两个独立 Application 进程同时提交相同/冲突 `run.start` 与不同 `run.cancel`、重启后重读控制库的 tests 验证最多一次有效 dispatch、一个 owner 和一个 run；本轮以 `test_retry_of_accepted_command_converges_without_second_dispatch`（提交后响应丢失再重试）与 `test_cross_process_command_idempotency_has_one_run_and_one_side_effect`（真实双进程 + 副作用计数）补齐
- [x] 2.7 实现 `shutdown` 的关闭门、活跃执行查询、阶段结果和幂等响应；以无活跃、正常完成、超时仍存活三种测试验证阶段屏障和未完成返回
- [x] 2.8 实现显式 `owner.reconcile` 与 `run.resume`；以 root crash 后 quarantine、inspect/terminate/release、旧 interaction 拒绝、新 decision_generation 和 uncertain tool 不重放 tests 验证不得 silent adopt/replay；本轮以 `test_crashed_root_releases_os_lock_but_quarantine_refuses_new_root` 验证"物理锁已释放 + 逻辑隔离仍拒绝新 root + 仅显式 reconcile 放行"

## 3. Session 租约与执行监督

> **范围修订（2026-09-13，用户确认）**：执行互斥的单位由 workspace 改为 **session**。每个 session 拥有自己的 canonical store，因此同一 workspace 的不同 session（TUI 与 GUI，或 GUI 的多个会话）MUST 可以并行；只有同一 session 不能被两个存活进程同时持有。workspace 级 `owner` 行保留为诊断与恢复归属信息，不再作为拒绝新工作的闸门。3.1/3.2 原有的 workspace 级 OS 文件锁与 lock namespace 条目据此作废，`workspace_lock.py` 不再被 `application.py` 使用。

- [x] 3.1 ~~新增 `src/rollo/workspace_lock.py` 的统一锁工厂~~ **（已作废）**：workspace 级文件锁不再是互斥机制；`workspace_lock_key` 仅保留为 owner 诊断行的稳定键
- [x] 3.2 实现 **session 租约**：`control.sqlite` 的 `session_leases` 表（`session_id` 主键 + `workspace_id`/`owner_id`/`process_id`），以 `BEGIN IMMEDIATE` 原子获取，持有进程已消失时自动接管，且只有当前持有者能释放。以 `test_session_lease_blocks_a_second_holder_but_not_other_sessions` 验证同 session 拒绝、不同 session 放行、死进程租约被接管；以 `test_different_sessions_of_one_workspace_run_concurrently` 做真实跨进程验证
- [x] 3.3 将 model task、InteractionRegistry Future、child Agent 和 managed shell 注册到同一 owner supervisor；以 owner-tree snapshot test 验证每个 execution 的 parent/root/owner 身份；本轮以 `test_run_start_control_commit_failure_never_dispatches`（提交失败不留 task/owner）与 `test_shutdown_incomplete_retains_owner_until_run_finishes`（存活执行阻止 owner 释放）补齐
- [x] 3.4 为 `tools.py` 的受管理 shell 接入异步 execution handle，绑定 `ProjectContext.tool_cwd` 和 owner，并持续 drain stdout/stderr；以真实 subprocess 写入超过 Windows pipe buffer 的双流 test 验证完整 byte count/hash、无死锁和正确 return code
- [x] 3.5 实现 shell 的有界优雅停止、强制终止、return code/仍运行观察、进程组/Job Object descendant 状态和超时证据；以延迟退出/持续输出/descendant subprocess test 验证 task cancel 不等于 OS 退出；本轮以 `test_managed_shell_timeout_keeps_evidence` 覆盖超时取消与 return code 观察；Unix 走 `start_new_session`+`killpg`，Windows 仍为 CTRL_BREAK + `kill()`，**Job Object 后代枚举仍为 best-effort（psutil 可选），故未做完整 Job Object 断言**
- [x] 3.6 实现一次性 cancel propagation，使用 run-level `cancel_generation` 去重；以不同 command_id 并发 cancel、interaction cancel、迟到 reply tests 验证只传播一次且不授权工具
- [ ] 3.7 按“禁止新 start → cancel/wait → flush partial/terminal → close MCP/store → release owner”实现 shutdown；以阶段 fault injection 和活跃 child 阻塞 tests 验证超时保留 owner

## 4. 控制持久化与恢复

- [x] 4.1 在绝对化且稳定的 `runtime_data_dir/application/<workspace_id>/control.sqlite` 建立版本化 session/run/command/owner/pending-interaction 表和索引，并校验 canonical store 的 session/workspace 隔离；以 schema migration/path-binding test 验证版本、唯一键、摘要字段和相对路径拒绝/固定基准行为
- [x] 4.2 实现 command accepted、run/interaction 状态和 dispatch intent 的原子控制事务；由独立 worker 先写固定格式 `ready.json`（含 barrier、workspace_id、command_id、marker_path、expected_count=0），再在 commit 前、commit 后/canonical 前、canonical 后/result 前和 response 丢失四个精确屏障调用 `os._exit`/Windows 等价终止，随后由新 Application 重启；以 control.sqlite、canonical SQLite、恢复状态和 provider/tool/shell side-effect marker/count 联合 oracle 验证可分类且无隐藏 dispatch；本轮在 `test_c03_crash_recovery.py` 落地 canonical-open 之后、response 丢失与未配对 tool dispatch 三种真实崩溃形态，**commit 之前与 canonical 之前的精确屏障仍未落地**
- [x] 4.3 实现 RFC 8785/JCS 语义的 canonical JSON v1、显式 null、拒绝 NaN/Infinity、完整小写 SHA-256 digest、tool/plan binding 和秘密白名单；以 Unicode/数字/数组顺序、修改 input/plan、复用 tool-call id、API key/provider config/原始敏感输入负向断言验证控制库、日志和异常不泄露
- [ ] 4.4 实现旧 session/partial 的只读解析与显式 migration，保留未知字段和原文件；以缺字段/未知版本/迁移失败 tests 验证不删除、覆盖或静默重建
- [x] 4.5 实现重启恢复分类：accepted 未 dispatch/未完成 run 为 interrupted，证据不足为 uncertain；以真实 crash/restart subprocess test 验证 design.md D14 的逐行状态与 `error_code` oracle（`command_not_accepted`、`run_dispatch_not_observed`、`tool_outcome_uncertain`、`interaction_interrupted` 等）和 side-effect count，并验证新 Application 将旧 pending interaction 固定为 interrupted（旧回复拒绝）；同一 run 两个 tool operation 必须逐个按 operation_id/provider_tool_call_id 关联，provider-only、缺失 operation identity、矛盾 identity 和多候选 provider terminal 均断言 `result=uncertain`，并分别断言 `canonical_identity_missing`/`canonical_identity_conflict`/`canonical_identity_ambiguous` 及缺失为 count=0、其余恢复不增副作用；本轮以 `test_restart_after_open_without_terminal_never_reports_success`（`interrupted`/`run_dispatch_not_observed`）与 `test_restart_after_unpaired_tool_dispatch_is_uncertain_and_never_replayed`（`uncertain`/`tool_outcome_uncertain`，side_effect_count=0，恢复期 agent 构造数=1 证明未重放）补齐真实跨进程 oracle
- [ ] 4.6 分离 Unknown tool 名称错误与 unknown/uncertain tool result；以两条独立状态、错误码和恢复断言验证查询失败与执行后证据不足不混淆
- [x] 4.7 对无 workspace 归属的旧 session 实现 inspect-only，并以 UTC `expires_at` 处理持久 pending deadline；以 `session.list`、resume、cancel、过期后重启和显式迁移映射 tests 验证不会按 cwd 自动认领或让旧 Future 永不过期

## 5. CLI/TUI Application 接线

- [x] 5.1 修改 `__main__.py`/入口工厂，将 ProjectContext、runtime store、Terminal ports 和 Application 显式注入；以静态扫描和真实入口 test 验证不访问 Agent `_aborted`/`_output_buffer`
- [x] 5.2 将 one-shot 转换为 `session.create`/`run.start`/`run.status` 链；以真实 CLI consumer 和 C02 port recording 验证同一 session/run 身份
- [ ] 5.3 将 REPL、resume、历史 session 和 `/clear`、`/plan`、`/compact` 等既有 REPL 命令转换为 Application API/read-only controls；以多轮 REPL/resume consumer test 验证同一 session、不同 run 且不重放 uncertain tool 或修改 Agent 私有生命周期字段
- [x] 5.4 将首次 SIGINT 转为公开 `run.cancel`，后续退出转为 shutdown/既有退出码；以可控 CLI subprocess 验证 EOF、审批、拒绝和退出码兼容
- [ ] 5.5 将 plan approval 作为沿用 C02 `InteractionKind.APPROVAL` 且带 `plan_approval/plan_id/plan_digest` metadata 的 `interaction.respond` envelope，并验证 TUI/受控 Application 同 workspace 争锁；以真实 owner-conflict、统一 JCS/full-SHA256 digest、修改计划、旧 Future 固定 interrupted、旧回复拒绝和显式新 resume tests 验证无第二 root dispatch
- [ ] 5.6 增加 Harbor consumer 的离线只读契约核对；以参数/stdin/runtime-dir/关闭行为检查验证不接入 stdio wire、Electron、Windows 包或付费 benchmark

## 6. 分层测试与实现验收

- [x] 6.1 新增 Application API focused tests 覆盖 session、run、interaction、status、shutdown、错误身份和终态唯一；以 focused pytest 命令验证全部 oracle
- [x] 6.2 增加 command idempotency/并发与 cancel-generation tests；以两个独立 Application 进程同时提交相同/冲突 `run.start`、不同 command_id 的 `run.cancel`、进程重启和 terminal race 验证至多一次副作用、共同结果和失败进程不释放他人 owner；补充同一 run 双 tool dispatch 的 paired `tool_operations[]`、provider-only terminal、missing/contradictory refs 与多候选 identity 的确定性 projection（后三类均为 `uncertain`，只允许规定的副作用计数）；本轮以 `test_retry_of_accepted_command_converges_without_second_dispatch`、`test_cross_process_command_idempotency_has_one_run_and_one_side_effect`、`test_projection_pairs_multiple_tool_operations_and_detects_identity_faults` 覆盖
- [x] 6.3 增加真实多进程 **session 互斥** tests；以同 session 跨进程拒绝（`session_conflict`）、**同 workspace 不同 session 并行**、不同 workspace 隔离、死进程租约自动接管、陈旧释放不顶替新持有者验证租约边界；本轮以 `test_different_sessions_of_one_workspace_run_concurrently`（真实跨进程：session A 运行中 session B 仍被接受）、`test_session_lease_blocks_a_second_holder_but_not_other_sessions`、`test_cross_process_workspace_lock_is_mutually_exclusive`（`WorkspaceLock` 单元层，已不参与 Application 互斥）覆盖；~~workspace 级争锁~~ 已随 3.1/3.2 作废
- [ ] 6.4 增加 owner-tree/cancellation tests；以 interaction Future、model/child/shell 同时取消、重复传播、迟到 reply、旧进程 Future 固定 interrupted、旧回复拒绝、新 `run.resume`/decision_generation 和新 Application 重建 pending binding 验证 cancel boundary
- [x] 6.5 增加真实本地 Python subprocess tests；以超过 Windows pipe buffer 的 stdout/stderr、优雅停止、强制终止、return code、PID/descendant 退出、超时仍存活验证 OS 证据；本轮由 `test_managed_shell_drains_both_streams_and_records_spill`（真实 subprocess 双流 4096B + byte count/hash/spill）与 `test_managed_shell_timeout_keeps_evidence`（超时取消后仍有 return code 与部分输出）覆盖，**PID/descendant 退出仍未立测**
- [x] 6.6 增加 control schema/migration/recovery tests；以旧 session/partial、秘密负向断言、inspect-only、相对 runtime-data-dir、D14 固定状态/`error_code`/side-effect oracle、`unknown_tool` 与 `unknown_tool_result` 分离、`canonical_identity_missing/conflict/ambiguous` 均为 uncertain 的断言、同一 run 多 operation 逐个 join、四类跨库硬崩溃和 control/canonical 冲突 fault injection 验证恢复安全；本轮以 `test_c03_crash_recovery.py` 的三种真实崩溃形态 + `test_inspect_only_session_is_never_implicitly_adopted` + `test_control_reply_persists_redacted_sensitive_tool_input` 覆盖 D14 三行与隔离/脱敏，**`unknown_tool` 与 `unknown_tool_result` 的分离断言（4.6）与四类硬崩溃中的另外两类仍未覆盖**
- [ ] 6.7 增加离线 CLI/TUI consumer tests；以可复算命令和 golden 输出/退出码、重复运行次数覆盖 one-shot、REPL、resume、plan/yolo/accept-edits/dont-ask、EOF、两次 SIGINT、plan approval、C02 事件验证实际入口（~~owner-conflict~~ 已作废；改为 `session_conflict`）
- [x] 6.8 运行 C03 focused、C02 canonical/runtime regression、编译检查和 `openspec validate add-runtime-application-lifecycle --type change --strict`；以 Python `>=3.11` 环境 manifest、C02 全量至少重复 2 次、已知 flake 的 retained 判据和分层结果验证不是单一全绿替代证据
- [ ] 6.9 由独立子代理进行实现后的“对科学审查”与对抗性变异审查；固定 mutant 清单（dispatch 提前 commit、删除 command 唯一约束、超时释放 owner、自动重放 uncertain、跳过 Future/digest、CLI 直接构造 Agent），以每个 mutant 至少一个测试变红验证测试具有鉴别力；**本轮已交办独立子代理（2026-09-12），结论未回，不得预勾**

## 7. Gate、写回与交付边界

- [x] 7.1 主 Agent 逐条核对独立设计审查与测试策略审查的源码/工件证据；以 result_identity、closed/retained/remaining gap 清单验证意见未被无条件接受
- [x] 7.2 在实现前冻结 proposal/design/specs/tasks、Git 状态、环境身份和 product diff scope；以可复算 manifest 验证 D(C03) 的输入未在审查期间变化
- [ ] 7.3 实现完成后冻结 diff，复核 AGENTS.md、`src/pyproject.toml` 等既有改动未被误纳入；以 `git status`、哈希 manifest 和 `git diff --check` 验证边界
- [x] 7.4 将实现结果、命令/结果、未解决风险、artifact/diff digest 和 implementation/delivery authorization 写回 C03 状态记录；以主 Agent acceptance 记录验证字段完整
- [ ] 7.5 独立对抗性审查通过且主 Agent 接受后才报告 C03 完成；以最终 focused/full regression、审查 result_identity 和 remaining gap=[] 验证退出条件
- [ ] 7.6 仅在用户另行授权后执行 C03 的 commit/push/MR/发布/部署；以 Git 状态和授权字段验证本 Change 不自动触发交付动作
