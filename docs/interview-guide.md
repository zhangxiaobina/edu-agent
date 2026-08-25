# EduAgent 面试讲解指南

## 3 分钟介绍

第一分钟讲问题。教学 Agent 的难点不是让模型返回一次工具调用，而是长链是否真的完成、读写权限是否收窄、
写操作在崩溃后是否重复、上下文和预算是否有界，以及失败后能否给出可核对的原因。EduAgent 用 16 个教学工具和
固定 seed 合成数据重建公开工作流，真实学生数据、私有平台和凭据不进入演示。

第二分钟讲主线。`EduAgentService` 是入口；`ProviderGateway/ResilientEngine` 冻结 route 与 API mode；
RunEvent 单 writer 传输 text/tool/usage；只并发 Provider 明确允许且 Manifest 标记 `parallel_safe` 的读取。
复杂任务进入 PlanGraph，步骤必须绑定真实 Evidence。写工具进入 approval、ToolOperation、业务写和 outbox
事务；run journal、lease/fencing 和恢复 planner 负责进程重开。

第三分钟讲证据。`TraceRepository` 不要求面试官读表，而是把 route/retry/fallback、工具 segment、参数规范化、
Plan/Evidence、审批写、checkpoint、恢复和 root/child budget 投影为 owner-scoped `review`，最后再脱敏。
10 分钟演示同时保留正常和故障报告；事件快照只比较稳定事实，不比较回答全文。

## 四类证据口径

这四类不能互相升级，也不要用“真实”一个词混过去。

### 已实现

- Chat Completions/Responses 显式 route、provider stream、retry/fallback 与 breaker 状态机。
- RunEventBus、RunStreamWriter、单调 sequence、writer replacement/fencing 和唯一 terminal。
- 只读工具并发 segment、写/审批/code execution barrier、参数规范化审计。
- Plan/Evidence、审批绑定的事务写、稳定 idempotency key、outbox、run journal 和恢复决策。
- artifact-first context checkpoint、root/child budget ledger、scope-checked Trace review。

“已实现”只说明正式代码路径存在，运行结论仍要落到下面三类证据之一。

### fixture/离线验证

- R5.4 使用 seed `314` 的 `SyntheticProvider` 和确定性本地 ProviderAdapter，不访问网络或私有平台。
- 正常路径验证 primary `chat_completions`、真实 RunEvent text delta、两个读取并发、Plan/Evidence、审批写、
  checkpoint 与 root budget finalization。
- 故障路径显式注入 primary transport failure 和
  `after_write_operation_commit_before_result` 进程崩溃，验证 fallback、旧 writer fencing、
  `reuse-operation -> terminal-replay`，且考试/operation/approval 各一次。
- child budget 的 `settled/outstanding` 由独立 `RunBudgetLedger` fixture 验证；本次主 run 没有 child，
  因此诚实输出 `not_exercised`。

### 真实模型运行已验证

[R5.2 报告](../artifacts/r52-real-model-eval.json) 的范围固定为 DashScope `qwen-plus`、
`chat_completions`、Test split 6 条、3 次重复。它是旧提交上的 `development/dirty` evidence，可说明该次
真实请求结果，不能说明当前候选提交已通过发布 provenance。可说：

- trajectory success 三次均为 `1.0`，tool recall `1.0`；
- tool precision mean 约 `0.8889`，F1 约 `0.9259`；
- 参数准确率 mean 约 `0.6667`；
- 44 次 provider observations，失败 Trace 0；
- 首输出 delta mean 约 `782ms`，总延迟 p95 约 `6737ms`，都只是该报告环境的观测。

不可说：这些数字代表其他 Provider、生产流量或真实学生任务；实际 Provider 账单仍未知。该 live run 没有注入
恢复故障，`recovery_safety=not_exercised`；`plan_observed=false`，不能把 harness step completion 写成
真实模型 PlanGraph 验证。

### 尚未验证

- `TeachingPlatformProvider` 与私有教学平台 E2E 尚未实现。
- 当前机器没有 Docker daemon，R5.3 只有容器静态 `verified`；镜像运行、Jobe、只读 rootfs、restart 和容器
  SIGTERM smoke 仍为 `not_verified`。
- SQLite lease/fencing 只覆盖共享同一文件的单机进程，不是跨主机、网络分区或多主共识。
- 任意阻塞第三方 SDK 的强杀、自由文本完整 DLP、生产身份生命周期和 trace 冷存储尚未验证。
- R5.5 发布审计结论为 `blocked/not ready`：当前没有 clean candidate Stage 8 与同一 commit 的真实模型
  candidate provenance，不能称为 release-ready。

## 六个高频追问

### 1. 为什么说是真 text delta，又说模型是 fixture？

这是两个维度。模型内容来源是离线 fixture；但它发出标准 `ProviderStreamEvent.TEXT_DELTA`，经正式
`consume_provider_stream -> RunStreamWriter -> RunEventBus subscription` 传输。报告统计订阅者实际收到的
typed delta、sequence 和 terminal，不是将最终答案事后切片。外部模型能力单独由 R5.2 报告证明。

### 2. 怎样证明两个工具真的并发，而不只是元数据写了 parallel？

`ToolBatchPlanner` 只有在 effect=read、`parallel_safe=true`、Provider capability 允许且资源键不冲突时才合并
segment。R5.4 的 SyntheticProvider 包装器再用两方 barrier：两个独立 worker 都进入后才放行，并记录
`distinct_worker_count=2`、`max_simultaneous_calls=2`。barrier timeout 失败即整条演示失败，没有 `sleep`。

### 3. 为什么没有 retry，却发生了 fallback？

故障发生在可见 delta 前，分类为 retryable connection failure；但演示冻结 `max_retries=0`，所以 Trace 给出
`retry_limit_exhausted` 且 `retry_scheduled=false`。fallback 仍需 capability compatibility 和 failure policy
同时通过，之后才激活 Responses route。若 delta 已可见，state machine 会禁止切 attempt，避免把两路文本拼接。

### 4. 崩溃后为什么不会再建一场考试？

写入前以 tenant/actor、固定 replay scope、工具名和规范化参数派生 idempotency key；approval、业务考试、
ToolOperation committed 和 outbox 在同库事务提交。故障发生在 commit 后、tool result 前，恢复 planner 看到 committed
receipt 后选择 `reuse-operation`，执行器返回 `idempotent_replay=true`。

R5.4 首次故障实跑还发现恢复上下文没有重绑定 replay scope，会派生第二个 key 并真实双写。修复不是 demo 特判：
`016_run_replay_scope` 把 StateStore schema 提升到 16，`runs.replay_scope` 随 enqueue 持久化并在
`resume_run` 重建；旧库迁移和五崩溃窗回归都覆盖固定 scope。演示恢复时还会新建 model fixture，并只从
durable tool messages 推导阶段，不复用崩溃前的内存计数器。这个例子说明故障注入是查不变量，不是演成功结果。

### 5. Trace review 如何避免越权和泄密？

`inspect_run` 先通过 run/session 的 actor+tenant scope；audit recovery 还要求 `resource=run:<id>`，budget root
再次校验 owner。review 只选择解释所需字段：参数规范化显示 pointer/type/rule，不显示原值；route 不显示 endpoint
或 CredentialRef；Plan 不显示任务正文。整个结构最后经过调用方 `RedactionPolicy` 二次脱敏。专项测试覆盖跨 actor
拒绝、最小化 canary、literal secret canary 和 CLI `--format review`。

### 6. 上下文和父子预算如何解释？

演示预置固定合成长历史，并只在 demo config 降低 compression trigger；正式 checkpoint 把估算从报告中的 before
压到 after，保留 strategy、estimator、阈值和 provenance。这个阈值没有改生产默认配置。

预算 review 分 root/child owner 聚合 reservation、actual usage、unknown cost 和 outstanding operation。本 run
没有委派，所以 child 是 `not_exercised`；独立 fixture 证明 child commit 后为 `settled`，仍有 reservation 时为
`outstanding`。不能为了画完整树虚构子 Agent。

## 失败时怎么讲

- 正常路径失败：从 assertion 名定位 route、delta、并发、Plan、写、context 或 budget 的缺口；明确本次演示失败，
  不打开旧报告冒充。
- 故障路径失败：可以继续讲已独立通过的正常路径，但 fallback/recovery 归入“本次未验证”；保留 work dir 和 Trace。
- 外网不可用：R5.4 本来离线，不受影响。R5.2 只展示已保存、带 route/config/provenance 的报告，说明不是现场调用。
- Docker 不可用：直接展示 R5.3 的 runtime `not_verified`；不把静态 Dockerfile 检查讲成容器验收。
- Trace 中出现 `manual-review`：这是 fail closed 的正确结果；解释 operator 要核对外部结果，不能盲目重放 uncertain 写。

## 关键 trade-off

| 主题 | 当前选择 | 获得 | 代价 |
|---|---|---|---|
| 一致性 | 单 SQLite lease + fencing | 本机跨进程可测、恢复边界清楚 | 无跨区域共识，写吞吐受 SQLite 限制 |
| 幂等 | replay scope + payload hash + ToolOperation | commit 后重放不重复业务写 | 外部平台必须提供等价键/状态查询 |
| 并发 | 仅 capability-safe 读取 | 降低读取长尾且保持写入顺序 | 可并发工具面保守 |
| 上下文 | 确定性 artifact-first checkpoint | 可恢复、可审计、避免无限历史 | 摘要有信息损失，需要 fidelity 评测 |
| 评测 | offline fixture 与 live model 分栏 | 不把框架满分冒充模型能力 | 没有 endpoint 时不能给新模型结论 |
| 可观测性 | durable truth 投影 + 最小化 review | 可复盘且不要求读表 | 大部署仍需保留/冷存储策略 |
