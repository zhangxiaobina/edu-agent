# EduAgent 产品优化路线：从可靠 Runtime 到可交付 Agent

> 审计基线：2026-08-21，对照本机 Hermes Agent `0.20.0` 源码。该 Hermes 目录没有可用的
> Git 元数据，因此版本来自其 `pyproject.toml`，不能把本次比较表述为某个 commit 的永久结论。
> 目标不是复制通用个人助理，而是选择性迁移对教学 Agent 有价值的运行时机制，把 EduAgent
> 打磨成可接真实平台、可恢复、可扩展、可审计、能在秋招现场稳定演示的产品。

## 1. 结论

EduAgent 已经具备可靠 Agent Runtime 的主要骨架：统一服务入口、Plan/Evidence、课程 RAG、
事务写工具、session lease/fencing、受限委派、隔离代码执行、Trace 和离线评测。它不是一个需要
推倒重写的 Demo。下一阶段不应继续堆叠通用工具，而应补齐五条产品闭环：

1. **Runtime 闭环**：模型、工具、流式事件和持久化共享同一 turn 状态机，崩溃后从明确边界恢复。
2. **Provider 闭环**：显式解析 Provider/API Mode，统一超时、重试、限流、fallback 和用量审计。
3. **真实平台闭环**：在不公开生产数据库的前提下，通过稳定领域契约连接真实教学平台。
4. **记忆与技能闭环**：先做人工可治理的 Memory/Skill，再考虑后台产生候选，不能自动改写生产行为。
5. **工程证据闭环**：独立测试集、真实模型评测、CI、部署和回滚共同证明能力，而不是只靠 Demo。

路线裁剪遵守四条原则：

- 当前源码和可复现测试是现状真相，README 中的名词不能替代实现证据。
- 先补 turn 内可靠性、真实流式和 Provider 路由，再做自动复盘、大数据集和 UI。
- 保留 LangGraph、SQLite、Plan/Evidence 和事务工具主线，通过窄接口演进，不另造第二套 Runtime。
- 每项新增能力必须同时有失败注入、Trace 事件和验收指标；没有消费者的扩展点不进入核心。

目标形态：

```text
Teacher / Student / Scheduler
              |
        EduAgentService
              |
   Agent Runtime + Policy + Plan/Evidence
       |               |               |
 Provider Gateway  Tool Runtime    Context/Session
       |               |               |
       +---------- Run Journal ---------+
                       |
              Trace / Evaluation
                       |
      Canonical Teaching Tool Contract
          /                         \
 SyntheticProvider          TeachingPlatformProvider
   (public CI)               (private/live E2E)
```

## 2. 真实数据库与合成数据库不一致

### 2.1 会不会有问题

**如果 Agent、工具和 SQL 表结构直接耦合，会有问题；如果以领域契约隔离，就不是问题。**

公开合成库的职责不是复制生产数据库，而是稳定表达 Agent 所需的业务语义、关系和边界条件。真实库
可能有完全不同的表名、拆表方式、状态码、历史字段和权限模型。两者只需要对同一组规范化工具契约
给出等价语义，不需要表级一致。

例如：

| Agent 工具契约 | 公开合成实现 | 真实平台实现 | 统一输出 |
|---|---|---|---|
| `list_exams` | SQLite 查询 | 教学平台考试 API | 规范化考试摘要 |
| `query_student_scores` | SQLite 联表 | 成绩 Controller/API | 作用域内成绩结果 |
| `assign_homework` | 本地事务表 | 平台写 API | operation id、状态和业务 id |
| `get_learning_progress` | 合成进度表 | 学习记录服务 | 课程/章节维度进度 |

### 2.2 推荐的防腐层

新增真实 Provider 时遵守以下边界：

```text
Tool Schema
   -> Domain Command/Query
      -> TeachingPlatformProvider
         -> HTTP DTO / private DB adapter
      <- Canonical ToolResult
   <- Policy / Evidence / Trace
```

- Agent Loop、Plan、Memory 和 Evidence 不得依赖生产表名或 ORM 实体。
- 优先通过真实平台 API 接入；读数据库时使用只读账号，业务写入必须走平台服务/API。
- Provider 负责状态码、分页、字段和错误映射，Runtime 只识别稳定错误分类和规范化结果。
- 每个 Provider 声明 capabilities；缺少真实能力时不向模型暴露对应工具。
- SyntheticProvider 与 TeachingPlatformProvider 共享契约测试，不共享存储实现。
- 只覆盖 Agent 实际使用的业务切片，不把整个教学平台数据库复制进 Agent 仓库。

因此，真实 DDL 仍然很有价值：它用于建立映射、发现约束、设计合成数据和契约测试，而不是替换
当前公开数据库。

### 2.3 需要的私有输入

首轮接入不需要任何生产数据行。建议在 Git 仓库之外或被 `.gitignore` 明确排除的目录提供：

```text
private-contract/
├── schema.sanitized.sql       # 相关基础表 DDL，无数据
├── data-dictionary.md         # 状态码、字段语义、权限说明
├── openapi.sanitized.json     # 相关 Controller/API 契约
├── aggregate-profile.json     # 可选，经过抑制/取整的统计画像
└── examples.synthetic.json    # 完全人工或程序生成的请求/响应
```

结构文件也可能属于业务资产；未经所有者许可，不应提交到公开仓库。DDL 需要移除 `DEFINER`、账号、
主机名、内部路径、注释中的组织信息、当前 `AUTO_INCREMENT` 数值、视图/存储过程中的敏感业务逻辑，
以及所有 `INSERT`、默认账号、token 和密钥。

### 2.4 数据策略

公开仓库只保存**重新生成的合成数据**，不保存“哈希后的真实行”。姓名、学号等低熵值即使经过普通
哈希，仍可能被字典攻击；班级、精确时间、成绩组合也可能重新识别个人。

| 数据类别 | 示例 | 公开策略 |
|---|---|---|
| 直接标识 | 姓名、学号、电话、邮箱、证件号 | 不导出、不进入模型和 Trace |
| 准标识 | 班级、地区、精确时间、设备/IP | 仅生成合成值；聚合时抑制小群体 |
| 教育敏感数据 | 成绩、作答、学习轨迹、教师评语 | 不发布真实行；只做受控本地 E2E |
| 非敏感契约 | 类型、约束、枚举语义、API 形状 | 审核后可用于合成与契约测试 |

真实环境测试在本地或私有 CI 运行，只输出脱敏后的通过率、延迟、错误分类和 Trace 摘要。自由文本
默认视为敏感，不依赖正则“猜测”是否包含个人信息。

### 2.5 当前接入决策

当前主开发阶段**不连接本地教学平台数据库**。结构审计已经证明它能承载关系型教务主链，但仍存在
版本差异、未填充的知识图谱/RAG/掌握度域和状态口径问题；现在接入会把业务库的不稳定性带入 Agent
主线，也会诱导工具实现直接耦合生产表。

- 查询/分析/知识图谱 10 个切片与 5 个教学 command 已通过 registry-backed SQLite
  `SyntheticProvider` 执行；canonical query/command/result/receipt/error 不暴露 SQLite Row、表名或生产
  ORM。写 command 仍加入既有 ToolOperation 同库事务，Provider 不自行审批或 commit。
- 私有 DDL 仅用于设计 Canonical Contract、发现状态和权限差异，不作为运行依赖。
- 主仓不得保存本地数据库凭据、连接配置、原始行、查询结果或由真实学生数据生成的 Fixture。
- 写工具继续在合成库验证审批、幂等、补偿和 outbox；最终接平台时只能通过业务 API 写入。
- 只有达到“可交付候选版”门禁后，才实现 `TeachingPlatformProvider` 和私有 E2E。

这项决策是接入顺序调整，不是取消真实平台目标。Provider 边界和契约测试必须提前完成，避免最后
接入时重写 Agent Loop、Plan、Memory、Evidence 或 Trace。

## 3. Hermes 中值得迁移的机制

以下结论来自当前 Hermes 源码，而不是旧路线文档。路径均相对于 Hermes 仓库。

| 能力 | Hermes 源码证据 | EduAgent 当前证据 | 判断与迁移方式 | 阶段 |
|---|---|---|---|---|
| Agent Loop / turn 收尾 | `agent/conversation_loop.py`、`agent/turn_finalizer.py` | `agent/graph.py` 已有 `agent -> tools -> verify`、双预算和确定性结束门 | 不重写 LangGraph；增加 typed event、增量 journal 和唯一 finalizer | R2 |
| 工具注册与窄核心 | `model_tools.py`、`toolsets.py`、`tools/registry.py` | `tools/registry.py`、MCP、entry point plugin 已能扩展 | 增加不可变 `ToolManifest`、来源/版本/hash/capability/并发元数据；会话内冻结 | R3 |
| Provider/API Mode 解析 | `hermes_cli/runtime_provider.py` | 只有 OpenAI-compatible Chat Completions；Provider 选择等同于一个字符串 | 建立窄 `ProviderGateway`；首期只支持 `chat_completions` 与 `responses` 两种 mode | R1 |
| 流式响应与中断 | `agent/chat_completion_helpers.py`、`agent/stream_single_writer.py`、`tools/interrupt.py` | API SSE 已输出 typed Provider/Agent delta；单 writer fence 与统一 token 贯穿执行树 | 保持进程内流与持久恢复边界分离；不承诺强杀任意 SDK | R2 |
| 工具参数修复与校验 | `model_tools.py::coerce_tool_args`、`agent/tool_executor.py` | 能解析 JSON 并递归校验基础 Schema；坏 JSON 结构化拒绝后由模型自行重试 | 只做 schema-guided、无歧义规范化；写工具禁止语义猜测；保留 original/normalized 审计 | R3 |
| 顺序/并发工具执行 | `agent/tool_dispatch_helpers.py::_plan_tool_batch_segments`、`agent/tool_executor.py` | 同一 assistant 回合的 tool calls 顺序执行；只有受控子任务层已有并发 | 只并发显式 `parallel_safe` 的只读调用，写/审批/代码执行为 barrier，结果按原顺序回灌 | R3 |
| 重试、限流与 fallback | `agent/retry_utils.py`、`agent/backend_identity.py` | 有错误分类、固定指数退避、熔断和一个 fallback | 增加 `Retry-After`、jitter、并发上限、按 deployment 的 breaker 和兼容性门禁 | R1 |
| 凭据轮换 | `agent/credential_pool.py` | 主/备用 key 各一个环境变量，无 pool/quarantine | 秋招主线只做可选小型凭据集合；无多凭据需求时不实现 3000 行级通用池 | L1 |
| 上下文超限与压缩 | `agent/context_engine.py`、`agent/context_compressor.py`、`docs/micro-compaction.md` | 有原子工具组、近似 token、可恢复确定性 checkpoint；Provider overflow 不会触发压缩重试 | 增加实际 token 口径、输出预留、反抖动、一次 overflow recovery 和摘要保真评测 | R4 |
| 会话存储与恢复 | `hermes_state.py`、工具后的增量 flush | SQLite 已按稳定 cursor 增量提交 assistant/tool、finalizer、预算和冻结身份；五窗按显式决策重开 | EventBus 继续只做进程内传输；不把未知状态猜成可恢复 | R2 |
| 父子 Agent 预算 | `agent/iteration_budget.py`、`tools/delegate_tool.py` | `delegation_roots` 已做 child 预算预留、聚合 usage、成本和 token 上限 | Hermes 子 Agent 实际使用独立新预算；保留 EduAgent 更严格的 root 治理，再把父级自身 usage 纳入统一 ledger | R4 |
| 长期运行与安全停机 | `gateway/drain_control.py`、`gateway/shutdown_flush.py` | Scheduler、lease/heartbeat、stale recovery 已有；缺少进程级 drain/final flush/备份恢复演练 | 增加 readiness/draining、拒收新 turn、有界等待、未完成 run 标记和恢复演练 | R4 |
| 结束持久化与后台复盘 | `agent/turn_finalizer.py`、`agent/background_review.py` | 统一 finalizer 已完成唯一消息、usage/terminal、hooks/cleanup；尚无自动复盘 | 后台只产 Memory/Skill candidate，主链失败不受影响 | R2/L2 |
| Memory/Skill 生命周期 | `agent/memory_provider.py`、`tools/skill_usage.py`、`agent/curator.py` | Memory 有 FTS5/scope/冲突/过期；无 Skill 实体和 usage/rollback | 先做人工创建、评测、审批和回滚，再考虑 Curator | L2 |
| 验证、Artifact 与审计 | `agent/verification_evidence.py`、`tools/tool_result_storage.py`、`agent/trajectory.py` | 教学 Plan/Evidence、scoped Artifact、Trace index 和导出已更完整 | 保持当前设计，只补新事件类型和真实 Provider 故障 E2E | 持续 |
| CI 与供应链 | `.github/workflows/`、精确依赖约束 | 已建立单平台 secret-free CI、真实 Git provenance 和 lock 漂移门禁；本地等价流程已通过，尚未观察托管 GitHub Actions 运行 | 保持凭据清空、冻结安装、离线评测和敏感数据审计门禁 | R0 |

### 3.1 必须如实描述的现状

- **当前 HTTP 已消费真实 Provider/Agent 流。** `edu_agent/api.py::_stream_chat` 将 typed RunEvent 按单调
  sequence 映射为 SSE，逐步发送 text/tool/plan/usage 与 terminal；keepalive 只在空闲时保活。EventBus
  future-only 且不跨进程，因此这不是持久断线 payload 回放；恢复 writer 从持久 sequence 高水位继续。
- **当前没有自动修复坏 JSON。** `parse_tool_arguments()` 对 malformed JSON 返回 `INVALID_JSON`；
  `_validate_value()` 会拒绝类型、范围和未知字段错误。模型下一轮可以自行修正，这是“校验 + 回灌”，不是修复器。
- **当前工具批次是顺序执行。** `agent/graph.py::tools_node` 逐个执行 tool call。委派层的并发不能当作
  单回合工具并发，因为两者的事务、取消和结果配对边界不同。
- **当前恢复从声明的稳定 cursor 继续。** assistant tool-call envelope、每个 tool result、TurnFinalizer cursor
  与 terminal 都增量持久化；进程重开按 `continue/replay-read/reuse-operation/manual-review/terminal-replay`
  决策，复验冻结 route/manifest/budget。五个进程重开窗口已通过；未知边界或不确定写仍 fail closed，不能
  描述成任意机器指令位置无损续跑。
- **当前 system prompt 稳定，但整个请求前缀不总是稳定。** Plan 模式会按 ready step 裁剪工具并改写
  tool description；如果将来依赖 Provider prompt cache，需要冻结 session tool manifest，或先量化接受缓存失效的成本。
- **当前 Provider 容错仍是单 primary + 单 fallback。** 已有 Chat Completions/Responses 两种显式 API mode、
  Provider 事件流、同步聚合兼容、`Retry-After`、按冻结 route identity 的 breaker/并发隔离和 capability-safe
  fallback，以及 typed HTTP SSE；仍没有凭据池、OAuth 或多厂商完整矩阵。
- **父子共享预算不能写成 Hermes 优势。** 当前 Hermes `IterationBudget` 明确给每个子 Agent 新预算；
  EduAgent 的 child 预留与 root 聚合更严格。不过现有 root ledger 主要统计委派树，父级主 Loop 的调用仍需纳入
  才能成为真正的全树总预算。

### 3.2 借鉴后的目标结构

```text
HTTP / Scheduler
      |
  RunController ---- CancellationToken ---- DrainController
      |
  Agent Loop (保留 LangGraph + Plan/Evidence)
      |                    |                    |
 ProviderGateway       ToolBatchPlanner      ContextPolicy
      |                    |                    |
 API Mode Adapter      PolicyToolExecutor    Checkpoint/Artifact
      |                    |                    |
      +------------- RunEventBus ----------------+
                         |
          RunJournal + TraceRepository
```

`RunEventBus` 不是第二套状态库。它只定义运行中的稳定事件协议；`RunJournal` 保存恢复所需的最小游标和
消息提交状态，现有 Plan、Evidence、ToolOperation、Artifact 和 Trace 表仍是各自业务真相。

### 3.3 秋招主线的 Runtime 改造合同

#### A. Agent Loop、增量 journal 与唯一 finalizer

- 定义 `RunPhase`：`accepted -> planning -> model -> tools -> verifying -> finalizing -> terminal`，并为每次
  model attempt、tool call、compaction 和 fallback 分配单调 `event_sequence`。
- 在工具执行前原子持久化 assistant tool-call envelope；每个工具结束后立即持久化对应 tool result。
  任一时刻都必须满足“已提交 tool result 有且只有一个 call”，尚未完成的 call 显式为 pending/cancelled。
- 持久化 `loop_cursor`、tool manifest hash、provider route、context checkpoint 和预算快照。恢复时只允许从
  已声明的稳定边界继续；无法证明结果的只读调用可重放，写调用只查询既有 `ToolOperation`，不能直接再执行。
- 建立一个幂等 `TurnFinalizer`，固定顺序为：关闭未配对工具消息 -> 完成 Plan 验证 -> 提交最终 assistant
  消息 -> 结算 usage/budget -> terminal run -> 触发非阻塞后处理。重复调用不得产生第二条最终消息。
- 用 fault injection 覆盖模型返回后、tool-call 持久化后、只读工具后、写提交后、最终消息后五个崩溃窗。

#### B. Provider Gateway 与 API Mode

- 用 `ProviderSpec`、`ResolvedRoute`、`ProviderCapabilities` 分开表示用户配置、解析结果和运行能力；不要让
  `provider="openai"` 同时承担协议、厂商、凭据和 endpoint 四种含义。
- mode 解析优先级固定为：显式配置 -> 已注册 Provider 元数据 -> 受信任官方 host 规则 -> 保守默认。
  首期只实现当前必需的 `chat_completions` 和一个 `responses` adapter，不接 Anthropic/Gemini 等完整矩阵。
- route 在 turn 开始冻结并写入 Trace；fallback 只能选择工具调用、上下文长度和结构化输出能力兼容的模型。
  无效请求、权限错误和输出上限错误不得伪装成可通过 fallback 修复的瞬态故障。
- 重试遵守 `Retry-After`，使用 jittered backoff，并在 `(provider, model, normalized endpoint)` 粒度做
  并发上限与 circuit breaker。Trace 记录 attempt、delay、failure kind、route 和是否切换，不记录 key。
- 凭据轮换不是首期硬指标。只有真实部署确有多 key 时才增加 `CredentialRef` 列表、租用和 cooldown；
  401 可隔离具体 credential，429 按 Provider 返回窗口处理，凭据明文永不进入 SQLite 或事件。

#### C. 真流式与中断

- 将 Engine 扩为 Provider 事件迭代器，事件至少包括 `text.delta`、`tool_call.delta`、`usage`、`completed`
  和 `error`；现有同步 `chat()` 作为聚合兼容层，避免一次重写所有 eval/mock。
- Agent 层再产生 `plan.updated`、`tool.started/completed`、`context.compacted`、`fallback.activated` 和
  terminal 事件；HTTP SSE 直接消费这些事件，不轮询内存字符串。
- 每个 run 只有一个带 fencing token 的流 writer；旧 attempt、fallback 前的流和恢复前 owner 的 delta
  必须被丢弃。并发工具事件可乱序到达，但展示事件有 sequence，tool result 回灌仍按原 call 顺序。
- 客户端断开、显式 cancel 和超时共用一个 `CancellationToken`。它传到 Provider adapter、工具执行器、
  子 Agent 和代码执行 Provider；阻塞 SDK 无法强杀时，返回后必须通过 fence 拒绝提交。
- 测试首 token 延迟、断流取消延迟、半个 tool-call JSON 被中断、fallback 后旧流继续到达和 completed
  之后不再出现 delta。

#### D. ToolManifest、参数规范化与安全并发

- 扩展 `ToolSpec`：`source/version/schema_hash/capability/risk/effect/parallel_safe/resource_keys/timeout`
  与字段级数据分类。注册时校验完整 JSON Schema、名称冲突和 handler 契约，生成不可变 `ToolManifest`。
- manifest 按 actor/tenant/role/course、Provider capability 和代码执行健康状态裁剪，在 session 或 run
  开始冻结。Plan 的 `allowed_tools` 只能收窄它；executor 仍做最终鉴权。
- 参数处理分三层：严格 JSON object 解析；schema-guided 的无歧义规范化；完整校验。只允许诸如
  `"42" -> 42`、`"true" -> true`、JSON 字符串形式的 array/object 等可证明转换，并记录修复路径。
  ID、自由文本、枚举近似值、日期和写操作业务字段不得模糊猜测；失败继续结构化回灌，最多消耗一次重试预算。
- BatchPlanner 将原始 call 顺序切成连续 segment：显式只读且无资源冲突的 segment 有界并发；写工具、
  审批、代码执行、交互工具、未知插件和不可解析参数形成顺序 barrier。不能仅凭工具名字符串猜测安全性。
- SyntheticProvider 并发调用必须让每个 worker 自己获取 SQLite connection；不得跨线程共享传入的
  `db_conn`。结果按模型发出的 call 顺序追加，每个超时/取消调用也生成配对 result。
- 验收同时比较串行/并行结果一致性、P95 加速、最大并发、取消、预算原子扣减和读写 barrier 顺序。

#### E. 上下文超限与压缩恢复

- token 口径优先使用 Provider 实际 usage/模型 tokenizer，缺失时才用可校准估算；预算必须预留 system、
  tool schema、当前输入和最大输出，不能只统计 message 字符数。
- 压缩保护 system prompt、当前 turn、未完成 Plan、审批/operation 回执、citation、Artifact 引用和用户
  的明确约束；优先将大工具结果换成 Artifact 引用，再摘要已完成的旧 exchange。
- checkpoint 保存 source message range/hash、压缩策略版本、摘要 hash、token before/after 和保留项，
  原文继续软归档可恢复。增加 trigger/release 双阈值与最小回收量，避免每轮反复压缩。
- Provider 返回真正的 input context overflow 时，最多执行一次“重新计数 -> 压缩 -> 同 route 重试”；
  output cap、无效参数和当前 user 输入本身过长分别返回不同错误，不进入压缩死循环。
- 不默认照搬 Hermes micro-compaction。它会持续改写历史并破坏 prompt cache；只有真实测量证明收益后才作为 opt-in。

#### F. 全树预算、长期运行和后台复盘

- 将现有 `IterationBudget` 与 `delegation_roots` 收口成持久 `RunBudgetLedger`，父级模型/工具、planner、
  压缩、fallback attempt 和全部后代共同结算 model calls、tool calls、tokens、cost 与 wall time。
  子任务先预留、结束后按实际结算并释放余量；恢复不能重置已用预算。
- 增加进程状态 `starting/running/draining/stopped`。draining 后 readiness 失败并拒绝新 turn，但允许在
  deadline 内完成现有 run；超时 run 持久化为可恢复状态，最后有界 flush journal/outbox/Trace。
- 提供 SQLite backup/restore、schema migration、retention/GC 和磁盘满/只读故障演练。当前单机 SQLite
  边界保持不变，不为简历展示虚构跨主机一致性。
- Background Review 只有在 finalizer 完成后入独立低优先级 job；只读脱敏 Trace，只能写 candidate。
  该项位于 L2，不得抢占 R0-R5，也不能让失败影响主回答。

### 3.4 不迁移的 Hermes 能力

- Telegram/Discord/Slack 等大量消息平台和通用个人助理表面。
- Electron/TUI、皮肤、语音、图像、浏览器和通用终端工具全集。
- 自动把所有对话写入长期记忆。
- 允许后台 Agent 任意修改用户技能、生产代码或执行教学写操作。
- 为展示技术数量而加入多个记忆 SaaS、模型 Provider 或环境后端。
- Hermes 的独立子 Agent 迭代预算；EduAgent 继续使用更严格的 root 级预算治理。
- 默认开启 micro-compaction、通用凭据池或消息 Gateway；没有当前用户故事时不承担其复杂度。

这些能力会扩大工具 Schema、攻击面和维护成本，却不能证明教学业务可靠性。

## 4. Memory Runtime 2.0

### 4.1 必须分开的五类状态

| 层次 | 内容 | 生命周期 | 真相来源 |
|---|---|---|---|
| Working Context | 当前 turn、近期消息、工具结果 | 单次会话 | message/checkpoint |
| Episodic Memory | 某次任务做了什么、是否成功、失败类型 | 有 TTL，可归档 | Trace 的结构化投影 |
| Preference Memory | 教师的稳定格式、课程范围和工作偏好 | 可更新、可撤回 | 显式输入或批准候选 |
| Authoritative Learning State | 成绩、进度、作答、班级成员 | 不作为长期记忆复制 | 当前只读查询走 SyntheticProvider；私有阶段才查询 TeachingPlatformProvider |
| Procedural Memory | 如何完成一类教学任务 | 版本化 Skill | 评测通过的 SkillSpec |

成绩和学习进度会变化，不能因为“长期记忆”而缓存成事实。相关回答必须查询当前激活的权威
Provider，并携带来源、版本或观测时间；切换到私有平台后只能以 `TeachingPlatformProvider` 为准。
Memory 只保存可治理的偏好、上下文线索和已验证过程。

### 4.2 生命周期接口

在现有 `MemoryProvider` 上逐步增加：

- `on_turn_start(context, query)`：非平凡问题才异步预取。
- `snapshot(context, query)`：在 turn 开始冻结召回结果，执行中不漂移。
- `sync_turn(context, outcome)`：只写结构化 episode 或候选，不阻塞主回答。
- `on_pre_compact(messages)`：压缩前提取仍需保留的实体、决策和未完成事项。
- `on_session_end(outcome)`：生成候选总结，不直接写权威事实。
- `shutdown()`：有界等待后台任务，失败不影响主 Runtime。

所有写入继续携带 `actor_id/tenant_id/course_scope/source/expires_at/conflict_key`，并增加
`confidence/classification/consent_status/source_event_ids/content_hash`。

### 4.3 评测指标

- Recall precision：召回内容是否真的帮助当前问题。
- Stale fact rate：过期或已冲突记忆进入回答的比例。
- Contradiction rate：同一 conflict key 同时激活多个版本的比例。
- Scope leak rate：跨 actor/tenant/course 泄漏，目标必须为 `0`。
- Added latency：prefetch 对 P50/P95 的影响。
- Write acceptance rate：候选被人工接受、拒绝和撤回的比例。

## 5. 受控自进化 Skill Runtime

### 5.1 Skill 不是任意 Prompt 文件

建议使用结构化定义：

```json
{
  "skill_id": "class-weakness-intervention",
  "version": 3,
  "owner_scope": {"tenant_id": "school-a", "course_ids": [1]},
  "trigger": "分析班级薄弱点并生成干预方案",
  "input_schema": {"class_id": "integer", "course_id": "integer"},
  "allowed_tools": ["diagnose_weak_points", "search_questions", "assign_homework"],
  "plan_template": ["diagnose", "select_practice", "request_write_approval"],
  "evidence_policy": ["tool_event", "citation", "committed_operation"],
  "created_from": ["run-id-1", "run-id-2"],
  "evaluation_set_hash": "sha256:...",
  "status": "evaluated",
  "content_hash": "sha256:..."
}
```

技能可以包含说明、Plan 模板和辅助脚本，但执行层只信任结构化字段。`allowed_tools` 只能收窄调用者
权限，不能扩大角色、课程或租户范围。

### 5.2 生命周期

```text
observed
   -> candidate
   -> evaluated
   -> approved
   -> active
   -> stale
   -> archived

active -> candidate(new version) -> evaluated -> canary -> active
                                      |             |
                                   rejected      rollback
```

- **observed**：Trace 中出现重复成功路径、用户纠正或稳定失败模式。
- **candidate**：后台 Review Run 生成结构化候选，记录来源和理由。
- **evaluated**：在独立数据集上验证正确性、权限、成本、延迟和副作用。
- **approved**：教师/管理员确认适用范围和写操作风险。
- **active**：按 query 检索相关 Skill；每个 session 固定版本，避免 Prompt/行为漂移。
- **stale/archive**：长期未用、数据契约变化或指标下降时停用；保留审计和恢复能力。

### 5.3 后台 Review Run

借鉴 Hermes 的后台 fork，但对教育场景收紧：

- 主 turn 完成后异步运行，不改变当前回答和 system prompt。
- 只读取已脱敏、已授权的结构化 Trace，不默认回放完整学生文本。
- 工具白名单仅包含 `propose_memory`、`propose_skill`、`link_evidence`。
- 不能调用教学写工具、代码执行、通用插件或再次委派。
- 模型输出只能进入 candidate 表，不能直接激活或覆盖既有 Skill。
- 相同来源事件和内容 hash 幂等，失败可重试且不重复创建候选。

### 5.4 激活门禁

一个 Skill 至少满足以下条件才可进入 canary：

1. Schema、DAG、工具权限和 owner scope 静态校验通过。
2. 来源 Trace 完整、未篡改、没有被撤回或标记为泄密。
3. 独立 holdout 的任务成功率不低于基线。
4. ACL leak 和重复写副作用均为 `0`。
5. 平均模型/工具调用和 P95 延迟没有突破预算。
6. 涉及写操作时仍需逐次审批，Skill 本身不能充当批准凭据。
7. 有明确 approver、版本、hash、回滚目标和过期时间。

## 6. 数据体系、评测与训练隔离

### 6.1 三层数据体系

数据源不能在“真实数据”和“合成数据”之间二选一。完整开发路线使用三层数据，各层职责不可互换：

| 层次 | 主要用途 | 是否驱动默认 Demo | 是否进入公开仓库 |
|---|---|---|---|
| 合成教学数据 | 工具契约、ACL、幂等、故障和端到端业务闭环 | 是 | 只提交生成器和可复现 Fixture |
| 公开数据集 | 真实分布、算法能力、外部分布和专项 Benchmark | 否，由 Eval Adapter 读取 | 原始数据不提交，只提交 manifest/转换代码/许可允许的小样本 |
| 私有教学平台 | 最终身份、权限、读写和恢复 E2E | 否，仅私有验收 | 绝不提交原始行、凭据和可识别产物 |

公开数据集不是生产教学平台，也不应强行灌入合成 SQLite 或本地 MySQL。它们通过独立 Eval Adapter
进入现有 harness；只有与 Canonical Tool Contract 语义完全一致的只读切片，才允许复用 Provider
契约测试。默认 Demo、CI 和离线验收始终以固定 seed 的合成数据为准。

### 6.2 公开数据集接入顺序

| 数据集 | 主要覆盖 | 接入阶段 | 下载策略 |
|---|---|---|---|
| OULAD | 注册、成绩、学习进度、VLE 行为和风险分析 | L1 可选 | 许可与磁盘检查后下载完整版本，CI 使用固定小切片 |
| MBPP | Python 题库、代码执行、测试用例和自动判题 | R5 可选 | 固定仓库提交或发布版本；只在沙箱需要外部分布证据时接入 |
| XES3G5M | 知识追踪、薄弱点和题目推荐 | L3 | 先取 schema 和单学科小切片，验证价值后扩大 |
| MOOCCubeX | 课程资源、概念图谱、先修关系和 RAG | L3 | 先取课程/概念/资源子集，不假设与 XES ID 可直接连接 |
| LongMemEval | 跨会话记忆、知识更新、时间推理和拒答 | L2 | 先接小规模 smoke set，Memory harness 稳定后跑完整评测 |
| Project CodeNet | 代码错误分析和沙箱压力测试 | 暂不排期 | 不下载全量；只有 MBPP 暴露明确覆盖缺口时取 Python 小切片 |

每个数据集必须绑定一个明确用户故事和指标；不能为了展示数据规模而接入。OULAD、MBPP 和
LongMemEval 主要证明已有能力在外部分布上成立，XES3G5M 与 MOOCCubeX 用于补强个性化学习闭环，
CodeNet 只在 MBPP 暴露出覆盖不足时进入路线。

### 6.3 下载、画像与版本治理

数据需要下载后才能做字段、缺失率、基数、时间范围和关系完整性分析，但下载不是第一步。先完成
R0-R4 Runtime，再实现 manifest 和可恢复管线；大数据集先看 schema 和小样本，不阻塞核心开发。

```text
data_catalog/                 # 提交：来源、版本、许可证、SHA-256、字段映射
scripts/data/                 # 提交：download/profile/prepare 命令
var/datasets/                 # 忽略：raw/staged/processed，可由 EDU_AGENT_DATA_DIR 覆盖
tests/fixtures/datasets/      # 提交：许可允许的极小样本或等价合成样本
artifacts/data-profiles/      # 提交前审核：只保留无个人信息的聚合画像
```

- 下载前记录来源、许可/使用条件、版本或 commit、预期大小和校验和；条件不清晰时不下载。
- 原始文件只读保存，所有清洗由版本化转换生成；报告记录 raw/transform/config hash 和行数变化。
- 每个 Adapter 提供 smoke、regression、full 三档；CI 只跑 smoke/regression，full 由显式任务运行。
- 大数据处理依赖放入可选 `data` extra，不进入 Runtime 核心依赖。
- 自由文本、用户标识和轨迹样例即使来自公开集，也先经过许可证和泄漏审查再决定是否留作 Fixture。

### 6.4 Train/Dev/Test 隔离

R0.4 已把历史冻结题、DPO 派生集和新 Test 统一纳入 stable lineage。六类派生多步模板与原题共享
意图族，因此都归 Train；历史 19 题中其余已用于实验的题只归 Dev，不在事后改称独立 Test。Test 使用
同一合成生成器的独立 seed 314、5 个班、每班 3 门课和六个新意图族：

| 数据集 | 用途 | 约束 |
|---|---|---|
| Train（55） | SFT/DPO、技能候选学习 | 含 48 条派生任务；不得进入最终模型报告 |
| Dev（12） | Prompt、Plan 和阈值选择 | 历史实验集；不得进入训练或最终报告 |
| Test（6） | 最终真实模型报告 | 新意图模板、新 seed、新实体分布；训练/调参不可见 |

切分在模板族声明时完成，不做随机行切分。自动门禁复查跨 split sample/query 重复、模板族与等价语义
组重叠、缺 provenance、敏感字段和两次生成不一致。真实模型 runner 在完整 corpus preflight 后只消费
Test，并保存模型参数、lineage/config hash、重复 run、均值/方差和脱敏失败轨迹；当前仍未运行真实模型。
Oracle 继续只证明 harness 正确。

## 7. 实施路线与验收门禁

里程碑分为秋招主线 `R0-R5` 和候选版后的 `L1-L3`。前一阶段未过门禁时，不并行开启下一项大型能力：

```text
R0 可信基线 -> R1 Provider Gateway -> R2 Event/Journal/Stream
             -> R3 Tool Runtime -> R4 Context/Budget/Drain -> R5 候选版

R5 之后按真实需求选择：L1 平台接入 / L2 Memory-Skill / L3 外部分布与 Curator
```

建议工期只用于控制范围，不是承诺：R0 约 2-3 天，R1/R3/R4 各 4-7 天，R2 约 7-10 天，R5 约
3-5 天。若时间不足，必须优先保证 R0-R3 和一条完整故障演示，不用半成品 UI 或数据下载填充进度。

### R0：可信、可追溯基线

交付：

- 评测只从真实 Git 元数据写入 commit，并同时记录 dirty 状态；当前开发态 artifact 已记录真实 commit，
  但 `dirty=true`，只能作为本地快照，不能作为候选版或发布证据。候选版/发布模式会拒绝 Git 元数据
  不可用、状态不可判定或工作区不干净的报告。
- GitHub Actions 或等价 CI：`uv sync --frozen`、ruff、全量测试、离线综合评测和敏感数据扫描。
- 对外统一使用 `accept_stage8.sh`；它调用 `accept_stage7.sh`，后者只保留为内部回归边界。后续新增阶段
  继续保持“最高阶段脚本包含前序回归”的单入口约定。
- 固定 Python、lockfile、依赖兼容范围和测试环境；CI 清空真实凭据并禁止读取私有数据库。
- 建立按意图模板族隔离的 Train/Dev/Test lineage；结果保存脱敏 JSON、配置 hash、重复运行和失败轨迹。

门禁：干净环境从 clone 到验收一条命令通过；报告含非空 commit/config hash；最终 Test 与训练模板无重叠；
CI 不依赖 `.venv`、本机数据库、API key 或已生成的 `edu.db`。

### R1：Provider Gateway 与恢复策略

交付：

- `ProviderSpec -> ResolvedRoute -> Adapter` 契约，显式 `api_mode` 和兼容性 capability。
- 保留当前 OpenAI-compatible Chat Completions，增加一个最小 Responses adapter；mock/fake server 覆盖真实 wire shape。
- 按 failure kind 和 backend identity 决定 retry/fallback；支持 `Retry-After`、jitter、并发上限和 per-route breaker。
- 配置解析、fallback 选择、attempt、延迟与 usage 进入 Trace；错误和事件经过中心脱敏。
- 不在本阶段建设通用多厂商认证、OAuth 或大型 credential pool。

门禁：同一 Agent Loop 在两个 API mode 的协议测试中得到等价 tool call；429 遵守 `Retry-After`；401/400/
context overflow 不盲重试；fallback 不选择 capability 不兼容模型；所有 attempt 均可按 run_id 审计且无 key。

### R2：真流式、增量 journal 与精确恢复边界

交付：

- `RunEvent v2` typed stream、单调 sequence、单 writer fence 和 SSE 事件映射。
- assistant tool-call、每个 tool result、loop cursor、预算、route 与 checkpoint 的增量提交。
- 幂等 `TurnFinalizer` 和兼容旧记录的迁移；终态只产生一次最终消息和一次 budget 结算。
- 统一 `CancellationToken` 贯穿 Provider、工具、子 Agent 和代码执行；断流、API cancel 与 timeout 走同一路径。
- 崩溃窗口 fixture 和 resume 决策表：continue、replay-read、reuse-operation、manual-review、terminal-replay。

门禁：SSE 可逐 delta 展示且不是 keepalive 假流；客户端断开后旧 writer 不再提交；五个崩溃窗恢复后
消息配对完整、最终消息唯一、已提交写副作用不重复；不支持强取消的 SDK 会被 fence 拒绝过期结果。

### R3：工具目录、参数治理与安全并发

交付：

- 为现有 16 个工具定义 `ToolManifest`、schema hash、capability、effect、parallel safety、resource key 和数据分类。
- 将 SQLite 实现收口为 `SyntheticProvider`，建立查询/分析/写入的 Canonical Contract 与契约测试。
- schema-guided deterministic normalizer、完整校验、repair audit 和坏参数 corpus；写工具只接受无歧义转换。
- 有界 BatchPlanner：只读无冲突 segment 并发，write/approval/code/unknown 为 barrier，结果保持 call 顺序。
- plugin/MCP 注册冲突、manifest 冻结、执行层二次鉴权和每 worker 独立 SQLite connection 测试。

门禁：16 个工具通过同一契约；替换 Provider 不修改 Agent 图；并发和串行输出等价且慢 I/O fixture 有可重复
加速；读写 barrier 顺序不变；取消/超时仍为每个 call 生成结果；ACL 泄漏和重复写副作用为 `0`。

### R4：上下文、全树预算与长期运行

交付：

- Provider usage/tokenizer 优先的 context breakdown，包含 tool schema 和 output reserve。
- 带 provenance/hash 的 checkpoint、压缩反抖动、摘要保真集和一次 context-overflow recovery。
- `RunBudgetLedger` 将父级、planner、压缩、fallback 和所有 child 纳入 token/cost/call/wall-time 总账。
- `starting/running/draining/stopped`、readiness、有限 drain、shutdown flush 和 stale run 恢复。
- SQLite migration、在线 backup/restore、retention/GC、磁盘满与只读模式演练。

门禁：长会话不出现孤立 tool message；同一 overflow 最多压缩重试一次；摘要关键约束/operation/citation
保真率达到预设门槛且 scope leak 为 `0`；父子恢复后预算不重置；SIGTERM drain 后新请求被拒、已有 run
完成或进入可恢复状态；备份可在空目录恢复并通过完整性检查。

### R5：秋招可交付候选版

交付：

- 一条命令完成环境检查、API/Demo、全量测试、离线评测、敏感数据审计和脱敏报告。
- 固定一个真实模型档位运行独立 Test，多次重复并报告均值/方差、token、成本、延迟和失败 Trace。
- 更新 10 分钟演示：Provider route -> 真流式 -> 并发只读 -> 批准写 -> 中断/崩溃恢复 -> Trace 复盘。
- 容器化 API、health/readiness、备份恢复命令、最小运行手册；前端工作台不是发布门禁。
- 私有平台契约已就绪时，可加一条只读 `TeachingPlatformProvider` E2E；公开候选版不能依赖它。

门禁：新机器不接私有数据库也能演示完整主线；真实模型报告不再是 `not_run`；至少一次断流或崩溃恢复
可现场重放；Trace 能回答选了哪个 Provider、为何 retry/fallback、哪些工具并发、哪里压缩、预算如何结算。

### L1：真实平台与选择性外部分布

- 使用清理后的 DDL/OpenAPI/DTO 实现 `TeachingPlatformProvider`，读优先走平台 API、写只走业务 API。
- Synthetic/Teaching 双 Provider 运行同一契约；真实身份、capability、scope、request id 和固定业务回执 E2E。
- 只选择一个能回答明确问题的外部集：OULAD 验证学习风险，或 MBPP 补沙箱；不同时铺开全部数据集。
- 真实部署确有多个 key 时再增加小型 credential rotation，不建设通用账户管理产品。

### L2：Memory Runtime 2.0 与人工 Skill

- 完成五类状态、Memory hooks、LongMemEval smoke、候选审批/撤回和冲突治理。
- 建立 SkillSpec、版本、provenance、usage、pin/archive/rollback，先只支持人工创建与激活。
- 两个教学 Skill 必须在 holdout 上不低于无 Skill 基线，并在质量、成本或延迟至少一项更优。
- finalizer 后的 Background Review 只能生成 candidate/evaluated；任何激活仍需人工审批。

### L3：受控 Curator 与数据扩展

- 只有 L2 的候选、回滚和评测门禁稳定后，才做重复轨迹聚类、stale/archive 和 canary。
- XES3G5M/MOOCCubeX 只取与个性化学习故事直接相关的小切片；CodeNet 暂不进入默认路线。
- 自动化永远不能越过 candidate/evaluated，不能调用教学写工具、代码执行或递归委派。

## 8. 秋招成品的判断标准

成品不以功能数量判断，而以一条可现场验证的故事判断：

1. 教师通过 API 提出跨工具教学任务；Trace 显示确定的 Provider、API mode、tool manifest 和预算。
2. 回答以真实 delta 流式到达；用户中断后旧 stream writer 和旧 fencing token 不能继续提交。
3. Agent 对独立只读查询安全并发，对写/审批形成顺序 barrier，Plan/Evidence 阻止无证据早停。
4. 写操作经过审批和幂等事务；在工具后或最终消息前注入崩溃，恢复后不会重复布置或生成两个回答。
5. 长会话触发可解释压缩；checkpoint 可追溯原消息，关键约束、citation 和 operation receipt 不丢失。
6. 父级、fallback、压缩和 child usage 进入同一预算总账；耗尽后确定性停止，恢复不能刷新配额。
7. Trace 能解释 retry/fallback、参数修复、并发 segment、压缩和恢复；公共仓库在 CI 用合成数据完整复现。

这条链同时证明 Agent 编排、数据工程、安全、事务、评测、可观测性和产品判断，比复制 Hermes 的
通用 UI 或工具数量更有说服力。真实平台 E2E、Memory/Skill 回滚是加分项，不应成为 R5 前阻塞项。

面试表述必须区分三类证据：

| 表述 | 可接受证据 | 当前示例 |
|---|---|---|
| 已实现 | 源码 + 专项测试 + 一键验收 | Plan/Evidence、事务工具、lease/fencing、Trace、ToolManifest、只读 SyntheticProvider |
| 已接入但未线上验证 | 协议/故障测试通过，真实环境报告明确 `not_run/not_verified` | 新 Provider adapter、外部数据 Adapter |
| 计划中 | 只出现在本文，不写入 README “技术亮点” | 参数治理/工具并发、统一预算总账、Background Review |

## 9. 当前下一步

当前严格按 R0-R5 推进，不连接本地教学数据库，也不先实现生产表 SQL：

逐会话实施时使用 [`optimization-implementation-prompts.md`](optimization-implementation-prompts.md)。它把
R0-R5 拆成 33 个可独立验收和交接的提示词；前一编号未满足停止条件时，不进入下一编号。

1. 完成 R0：建立可追溯 Git/CI 基线，保持 Stage 8 单一公开验收入口，并修正 `commit="unavailable"`。
2. R1 已完成：Provider Gateway 已跑通 Chat Completions/Responses mode、Retry-After 和兼容 fallback。
3. R2 已完成：RunEvent、RunJournal、增量工具消息、TurnFinalizer、Provider/SSE 真流、统一取消、持久 writer
   fence、五崩溃窗恢复和独立门禁均已通过。
4. R3.1 已冻结 ToolManifest，R3.2 已收口只读切片，R3.3 已将教学 command/receipt/error 与 16 工具契约矩阵收口；下一步 R3.4 做 schema-guided 参数规范化与 repair audit，仍不并发。
5. 完成 R4：补实际 token/overflow recovery、全树预算、drain 与 backup/restore。
6. 完成 R5：跑一次固定真实模型独立 Test，更新演示、部署和运行手册，冻结秋招候选版。

R5 前明确不做：消息平台 Gateway、桌面工作台、通用浏览器/终端、多媒体工具、全量多 Provider、完整
credential pool、自动 Skill 激活、XES/MOOCCube 大规模下载和跨主机协调。公开数据尚未下载不能阻塞
Runtime 主线，合成 Demo 已通过也不能冒充真实模型结果。

R5 后再根据真实需求选择 L1/L2/L3，不要求三个方向同时推进。接入私有平台无需提前读取生产数据行；
数据库只读核验也必须使用专用账号，任何私有 DDL、凭据、原始行和可识别 Trace 都不得进入公开仓库。

现有实现与边界继续以 [`architecture.md`](architecture.md)、
[`production-runtime.md`](production-runtime.md) 和 [`eval.md`](eval.md) 为准；本文只描述未来演进和
验收顺序，未通过门禁的能力不得在 README 中写成已实现。
