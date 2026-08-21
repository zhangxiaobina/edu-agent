# EduAgent R0-R5 分会话实施提示词

> 配套路线：[`product-optimization-roadmap.md`](product-optimization-roadmap.md)。本文件是实施提示词，
> 不是完成状态；任何能力只有源码、测试和验收同时成立后，才能从“计划中”改为“已实现”。

## 1. 使用方法

1. 严格按 `R0.1 -> R0.2 -> ... -> R5.5` 顺序执行。一个编号对应一个新会话，不把相邻提示词合并。
2. 新会话直接复制对应的 `text` 代码块。Agent 可以读取本仓文件，因此提示词会引用本文件的通用协议，
   不重复粘贴所有背景。
3. 每次都先读取当前源码和 `docs/optimization-progress.md`。后者由 R0.1 创建，是跨会话交接账本，
   但不能替代源码、数据库 migration 和测试证据。
4. 上一会话未通过自己的停止条件时，不进入下一编号。环境限制可以记录为 `not_verified`，代码失败不能。
5. 提示词中的建议文件名不是强制架构。若当前代码已演进，先验证再沿已有边界实现，禁止并行造第二套 Runtime。

最短调用方式也可以是：

```text
请执行 docs/optimization-implementation-prompts.md 中的 R2.3。先完整阅读“通用执行协议”、该提示词、
docs/optimization-progress.md 和路线图对应阶段，然后直接实施、测试并更新交接记录；不要只给方案。
```

## 2. 通用执行协议

每个会话都必须遵守以下约束：

- 先检查仓库指令、当前工作区、相关源码、测试和最近交接记录；保留用户已有改动，不回退无关文件。
- 先用代码证明现状。路线图、README、进度文件和提示词出现冲突时，按“可运行源码与 migration -> 测试 ->
  当前文档 -> 计划”的顺序判断，并在交接记录中说明。
- 本会话要完成可运行的纵向小切片，不只输出设计。只修改当前提示词需要的边界，不提前实现下一编号。
- 新增状态必须有 migration 和旧库兼容测试；新增事件必须经过中心脱敏；新增配置必须有默认值、校验、
  示例和敏感字段处理。不得把 key、生产数据、私有 DDL 或可识别 Trace 写入仓库。
- 保持 `EduAgentService` 唯一服务入口，保留 LangGraph、Plan/Evidence、事务写工具、lease/fencing、Artifact
  和 Trace 真相源。不得为了模仿 Hermes 引入第二套 Agent Loop、状态库或大而全 Provider 框架。
- 兼容当前离线 mock 和 OpenAI-compatible Chat Completions。没有明确消费者时不增加抽象；没有真实多 key
  需求时不建设凭据池；没有真实平台契约时不读取或猜测生产表。
- 测试必须覆盖成功、失败、取消/重试或恢复边界中与本会话相关的部分。优先跑专项测试；改动共享契约后
  再跑全量测试。需要绑定回环端口或访问 Docker 时，按环境权限正常申请，不通过规避测试来制造绿灯。
- 只有阶段收口会话运行完整 `zsh scripts/accept_stage8.sh`。普通会话不得无意义重复昂贵验收，但必须运行
  足以证明该切片的 ruff、pytest 和必要故障注入。
- 每次结束前追加更新 `docs/optimization-progress.md`：编号、日期/commit、实际改动、migration/配置变化、
  测试命令与结果、残余风险、下一编号。不得改写历史记录或把 `not_run/not_verified` 写成通过。
- 最终回复固定给出：完成内容、关键设计决定、验证结果、未验证项、下一提示词。若阻塞，给出证据和最小
  解阻条件；不要以“时间不够”代替工程结论。

`docs/optimization-progress.md` 至少维护以下结构：

```text
# Optimization Progress
## Current
- last_completed_prompt: R0.0
- next_prompt: R0.1
- baseline_commit: unavailable
- stage_gate: not_started

## Session Log
### <prompt-id> - YYYY-MM-DD
- changes:
- migrations/config:
- verification:
- residual_risks:
- next:
```

## 3. 阶段索引

| 阶段 | 会话 | 入口条件 | 阶段出口 |
|---|---:|---|---|
| R0 可信基线 | 4 | 当前源码可读取 | Git/CI/lineage/单入口验收证据可信 |
| R1 Provider Gateway | 5 | R0 gate 通过 | 两种 API mode、路由容错和审计协议通过 |
| R2 Event/Journal/Stream | 7 | R1 gate 通过 | 真流式、中断、增量持久化和五崩溃窗通过 |
| R3 Tool Runtime | 6 | R2 gate 通过 | Manifest、Canonical Provider、参数治理和安全并发通过 |
| R4 Context/Budget/Drain | 6 | R3 gate 通过 | 压缩恢复、全树预算、停机和备份恢复通过 |
| R5 候选版 | 5 | R4 gate 通过 | 独立真实模型证据、部署、演示和发布审计完成 |

## 4. R0：可信、可追溯基线

### R0.1 基线盘点与跨会话账本

```text
你正在执行 EduAgent 优化路线 R0.1。本会话只建立可信基线和交接账本，不改 Provider、Agent Loop、
工具行为或数据库业务语义。

先完整阅读 docs/optimization-implementation-prompts.md 的通用执行协议、
docs/product-optimization-roadmap.md 的 R0、docs/architecture.md、docs/production-runtime.md、
pyproject.toml、scripts/accept_stage7.sh、scripts/accept_stage8.sh 和现有评测入口。检查是否存在 AGENTS.md、
Git 元数据、未提交改动、Python/uv 版本、lockfile、当前 artifacts 和可用测试环境。

必须完成：
1. 创建 docs/optimization-progress.md，按通用模板记录真实 Git/环境状态；没有 .git 时保持
   baseline_commit=unavailable，不得伪造 commit、remote 或历史，也不要擅自 git init。
2. 运行 ruff 和全量 pytest 建立基线；真实 HTTP 测试若仅因沙箱禁止绑定 127.0.0.1 失败，应在获准环境复跑
   并分别记录两次结果。检查 artifacts/system-eval.json 的 commit/config hash/not_run/not_verified 口径。
3. 盘点 R0 每个门禁已有证据、缺口、建议修改文件和风险，写入进度文件；不要另建重复路线文档。
4. 确认 README、architecture、demo-script 的公开完整验收入口都指向 accept_stage8.sh，Stage 7 只作为内部回归。

停止条件：进度文件能让一个全新会话准确复现当前基线，所有命令结果有数字和失败分类，Git 缺失等阻塞被
如实记录。本会话不声称 R0 已完成。更新进度账本，结束时明确下一提示词为 R0.2。
```

### R0.2 干净环境与单一验收入口

```text
你正在执行 R0.2。本会话只修复“干净环境可复现”和最高阶段单入口验收，不建设 CI 或修改运行时功能。
先阅读通用执行协议、R0.1 交接和当前 acceptance 脚本，实际验证它们的调用图，不能只按文档推断。

必须完成：
1. 让新环境的准备步骤显式、幂等且使用 uv.lock；去掉 accept_stage8.sh 对“用户事先恰好拥有某个 .venv”
   的隐式假设。缺少 uv、Python 不兼容、lock 漂移时应快速给出可操作错误。
2. 保持 accept_stage8.sh 为唯一公开完整门禁，并让它显式包含前序回归；Stage 7 可以独立调试，但不能在
   README 或演示文档中再次成为完整入口。避免同一全量测试无理由重复多次，若保留重复要说明回归边界。
3. 所有临时状态使用安全的独立目录并有有界清理策略；不得删除用户 artifacts、数据库或工作区文件。
4. 为脚本调用关系、失败传播、无预生成 edu.db 和无真实凭据增加可自动化验证；同步最小运行说明。

验证至少包括 shell 语法、相关专项测试，以及从明确准备步骤到离线门禁的 dry-run/可控执行。若 Docker 后端
不可用，必须继续保留 sandbox=not_verified 的诚实降级，不能伪装通过。更新进度账本，下一提示词 R0.3。
```

### R0.3 CI、供应链和评测 provenance

```text
你正在执行 R0.3。本会话建立 secret-free CI 和可信评测 provenance；不做 Provider Gateway 或真实模型调用。
先阅读通用协议、R0.2 交接、pyproject.toml/uv.lock、数据审计脚本和 scripts/eval_system.py。

必须完成：
1. 增加 GitHub Actions 或仓库现有平台等价 CI：固定受支持 Python，uv sync --frozen，ruff，全量 pytest，
   离线综合评测和敏感数据边界审计。CI 必须显式清空模型/平台凭据且不依赖本机数据库、预建 .venv 或 Docker。
2. 校验 lock 漂移和依赖安装；不要为了“版本更多”扩展 Python/OS 矩阵。缓存只能优化速度，不能成为正确性前提。
3. 收口评测 provenance：真实 Git 仓中记录 commit，始终记录 config hash、seed、模型/模式和环境；无 Git 时
   继续写 unavailable，但候选版/发布模式必须将其视为未过门禁。不得通过常量或环境变量伪造 commit。
4. 为 provenance、脱敏和 CI 离线性增加测试。workflow、日志和 artifact 不得打印 key 或私有路径内容。

运行 workflow 的本地等价命令和全量测试。若当前目录没有 Git 元数据，可完成代码与 CI，但必须在进度文件把
R0 gate 标为 blocked_on_git_metadata，并给出用户需要提供的最小条件。更新进度账本，下一提示词 R0.4。
```

### R0.4 数据 lineage 与 R0 收口

```text
你正在执行 R0.4。本会话补齐 Train/Dev/Test lineage 并对 R0 做独立收口；不得开始 R1。
先阅读通用协议、R0.1-R0.3 交接、edu_agent/eval、现有任务生成逻辑、docs/eval.md 和数据审计边界。

必须完成：
1. 为评测样本建立稳定 id、来源、版本、split 和意图模板族 lineage；切分必须按模板族或等价语义组隔离，
   不能先随机行切分再声称无泄漏。只使用现有合成/公开数据，不下载大型数据集。
2. 增加自动检查：跨 split 重复、模板族重叠、缺失 provenance、敏感字段和非确定生成均导致门禁失败。
3. 明确 oracle/mock 只验证 harness，真实模型结果单列；保存结果时保留脱敏失败轨迹、配置 hash 和重复运行信息。
4. 审核 R0 全部文档与实现，只把已有测试证据支持的能力标为完成；修正过期命令和口径。

收口验证：运行新增 lineage 测试、全量 pytest、ruff、数据审计和完整 accept_stage8.sh。记录 Docker/真实模型
未验证项，不把它们混入离线失败。只有 commit 可追溯、CI 配置成立、单入口门禁通过且 split 无泄漏时，才在
进度文件标记 R0 gate=passed；否则停在 R0.4 并列出未满足项。更新进度账本，通过后下一提示词 R1.1。
```

## 5. R1：Provider Gateway 与恢复策略

### R1.1 Provider 契约与解析规则

```text
你正在执行 R1.1。前置条件是 R0 gate=passed。本会话只建立窄 Provider Gateway 契约和配置解析，不接入
Responses、不改变流式协议、不重写 ResilientEngine。

先阅读通用协议、R1 路线、R0 交接、edu_agent/engine、runtime/config.py、service.py 和现有 engine 测试。
用源码确认 get_engine/config 的实际入口后再决定文件位置。

必须完成：
1. 定义小而稳定的 ApiMode、ProviderSpec、ProviderCapabilities、ResolvedRoute 和 adapter 协议；区分协议模式、
   厂商/部署、endpoint、model 与 CredentialRef，任何 repr/事件不得包含凭据。
2. mode 解析优先级固定为：显式配置 -> 注册元数据 -> 受信任官方 host 规则 -> 保守 chat_completions 默认。
   自定义/本地 endpoint 不得仅凭模糊域名推断成官方 Provider。
3. 在 turn 开始形成不可变 route identity，规范化 endpoint 只用于隔离/审计且不得丢失必要 path 语义。
4. 保持旧 config 和 EDU_AGENT_* 环境变量兼容；为冲突、未知 mode、恶意 URL、默认值和凭据脱敏增加测试，
   更新 config.example.toml。

本会话不发真实网络请求。运行 engine/config 专项测试、ruff 和受影响回归，更新进度文件。下一提示词 R1.2。
```

### R1.2 Chat Completions 适配器迁移

```text
你正在执行 R1.2。本会话把现有 OpenAI-compatible Chat Completions 收到 R1.1 Gateway 后面，并保持现有
Engine.chat、mock、vLLM/通义兼容行为；不要实现 Responses 或 streaming。

先阅读通用协议、R1.1 交接、engine/base.py、openai_compat.py、mock.py、service/get_engine 和相关测试。

必须完成：
1. 实现 chat_completions adapter，将规范化请求和响应映射为现有 EngineResponse/ToolCall；usage、finish_reason、
   model、空 content、多 tool call 和字符串 arguments 不能丢失。
2. 让 Gateway 选择该 adapter，并保留同步 chat 兼容面，使 Agent 图、eval 和 mock 不需要同时重写。
3. 用注入 fake client 或本地 fake transport 验证真实 wire shape、tools 为空/非空、超时和 SDK 异常传播；测试不得
   访问公网，也不能只 mock Gateway 自己的返回值。
4. 对旧 OpenAICompatEngine 给出清晰迁移路径，删除或薄化重复请求逻辑，避免两套实现长期漂移。

运行 engine、agent、runtime 相关专项测试及全量 pytest；共享契约变化必须无回归。更新交接，下一提示词 R1.3。
```

### R1.3 最小 Responses 适配器

```text
你正在执行 R1.3。本会话只增加一个 OpenAI Responses API mode adapter，不扩展 Anthropic、Gemini、消息平台
Gateway 或多厂商认证。

先阅读通用协议、R1.2 交接和当前 Gateway/adapter 测试。基于当前锁定 SDK 的真实类型或 fake HTTP wire shape
实现，不能凭记忆猜字段；必要时先检查已安装 SDK，但测试保持离线。

必须完成：
1. 把当前内部 messages/tools 请求映射到最小 Responses 请求，并把 text、function call、arguments、usage、
   completion status/model 规范化为与 Chat Completions 等价的 EngineResponse。
2. 明确 capability：tool calling、structured output、usage、streaming（本阶段仍关闭）和上下文限制；不支持的
   组合在发请求前失败，而不是收到 Provider 400 后猜测。
3. fixture 覆盖单/多 function call、交错 text、缺失 usage、incomplete/error、未知 output item 和坏 arguments。
4. 增加同一语义 fixture 在两种 api_mode 下得到等价内部 tool call 的契约测试；route/Trace 不记录输入秘密。

保持同步 Agent Loop 不变。运行两种 adapter 专项与全量回归，更新交接，下一提示词 R1.4。
```

### R1.4 Retry-After、限流和 per-route breaker

```text
你正在执行 R1.4。本会话收口 Provider 尝试策略：错误分类、Retry-After、jitter、有界并发和 per-route circuit
breaker；暂不做 fallback 兼容选择、凭据池或流式重试。

先阅读通用协议、R1.3 交接、engine/resilient.py、Provider 事件存储和现有故障测试。

必须完成：
1. 保持错误分类可测试，区分 connection/timeout/429/5xx 与 auth/permission/invalid/context overflow/output cap。
   只有明确瞬态错误可重试；解析 Retry-After 秒数和 HTTP-date，并设置可配置上限。
2. 使用可注入 clock/sleeper/random 的 jittered backoff，测试不能真实 sleep。服务端 Retry-After 优先级和上限要明确。
3. 在规范 route identity 粒度维护并发限制与 breaker，half-open 只能有受控探测；一个 endpoint 的故障不能打开
   另一个 endpoint/model 的 breaker。避免无限增长的全局字典，定义回收或有界生命周期。
4. 每次 attempt 记录序号、route、failure kind、delay、breaker 状态和 usage，统一脱敏；永不记录 key、完整敏感正文。

用确定性并发/时钟测试覆盖 429、HTTP-date、401、400、context overflow、half-open 竞争和隔离。运行全量回归，
更新进度文件。下一提示词 R1.5。
```

### R1.5 Capability fallback 与 R1 收口

```text
你正在执行 R1.5。本会话实现 capability-safe fallback，并独立验收整个 R1；不要开始 token streaming。

先阅读通用协议、R1.1-R1.4 交接、当前 Gateway/ResilientEngine/Trace 实现和 R1 门禁。

必须完成：
1. fallback 只在策略允许的 failure kind 触发，并验证 api mode、tool calling、structured output、上下文和其他
   当前请求所需 capability。401/403/普通 400/output cap 不得靠盲切模型掩盖；context overflow 留给 R4。
2. turn 开始冻结 primary/fallback route，Trace 记录选择原因和切换；fallback 后旧 attempt 不得覆盖 usage 或终态。
3. 没有多 key 真实需求时保持单 CredentialRef，不实现轮换池。配置错误应启动失败，不在运行中静默降级。
4. 增加 fake Provider 故障矩阵，证明两种 API mode 的等价 tool call、Retry-After、per-route breaker、兼容与不兼容
   fallback、attempt 审计和 key 脱敏。

运行 ruff、Gateway/engine/Trace 专项、全量 pytest，并执行与外网无关的 R1 fake-server 验收。复核 README 不把
R2-R5 写成已实现。更新进度账本；只有 R1 门禁全部通过才记 R1 gate=passed，下一提示词 R2.1；否则留在 R1.5。
```

## 6. R2：真流式、增量 Journal 与恢复边界

### R2.1 RunEvent v2 与单写者事件协议

```text
你正在执行 R2.1。前置条件是 R1 gate=passed。本会话只定义 typed RunEvent v2、序列和发布协议；不修改
Provider 为流式，不改变消息持久化时机。

先阅读通用协议、R2 路线、R1 交接、observability/events.py、trace.py、api.py、service.py 和当前事件测试。
盘点 RuntimeEvent v1 的生产者和消费者，设计兼容迁移，禁止另建第二套 Trace 真相源。

必须完成：
1. 定义最小稳定事件族：run phase、text.delta、tool_call.delta、usage、plan.updated、tool.started/completed、
   context.compacted、fallback.activated、completed、error。字段至少包括 schema version、run/session、attempt、
   单调 sequence、时间、fencing/writer identity 和脱敏 payload。
2. 明确传输事件与持久恢复状态的边界：EventBus 负责进程内发布/订阅，TraceRepository 投影审计事件，后续
   RunJournal 保存恢复游标；EventBus 不能成为数据库或可恢复队列。
3. 一个 run/attempt 的 sequence 分配必须线程安全；并发生产者可乱序完成，但消费者能以 sequence 判定顺序，
   terminal 后拒绝新 delta。定义有界订阅缓冲、慢消费者和取消语义，不能无限占内存。
4. 保持 RuntimeEvent v1 查询/导出兼容；为 schema 校验、脱敏、单调性、慢消费者、terminal fence 和并发发布
   增加确定性测试。

本会话使用 fake producer，不接真实 Provider 流。运行事件/Trace 专项和全量回归，更新交接。下一提示词 R2.2。
```

### R2.2 RunJournal Schema 与稳定游标

```text
你正在执行 R2.2。本会话实现 RunJournal 的持久 schema、migration 和原子 API；不改变 Agent Loop 的实际提交点。

先阅读通用协议、R2.1 交接、StateStore schema/migration/lease/fencing、runs/messages/tool_events/operations 的
现有真相源。先写恢复状态表和不变量说明，再编码。

必须完成：
1. 定义 RunPhase 与合法转换：accepted -> planning -> model -> tools -> verifying -> finalizing -> terminal，允许
   明确的 cancelled/failed 分支。journal 保存 loop cursor、model attempt、event sequence、tool manifest hash、
   frozen provider route、context checkpoint、budget snapshot 和最后稳定边界。
2. 不复制 Plan、Evidence、ToolOperation、Artifact 或完整 Trace；journal 只引用它们。为旧数据库增加幂等 migration，
   支持进程中断后重开，schema version 不可倒退。
3. 所有 compare-and-set 更新校验 run/session/actor/tenant、合法 phase、fencing token 和单调 cursor；旧 worker、
   重复写、跳跃转换必须结构化失败。终态不可重新进入执行态。
4. 提供恢复决策所需的只读 snapshot API，并为新库、旧库 migration、重复 migration、并发 CAS、旧 fence、损坏/
   未知 phase 增加测试。不得用静默默认掩盖无法证明的恢复状态。

运行 state/journal 专项和现有 recovery/trace 回归，更新进度账本。下一提示词 R2.3。
```

### R2.3 Assistant Tool Envelope 与结果增量提交

```text
你正在执行 R2.3。本会话把 assistant tool-call envelope 和每个 tool result 移到 Agent Loop 的稳定边界增量提交；
不实现最终消息 finalizer、Provider streaming 或并发工具。

先阅读通用协议、R2.2 交接、agent/graph.py、service.chat/run_agent、StateStore.append_messages、ToolOperation、
runtime/tool_executor.py 和现有消息配对测试。画出当前批量追加路径，确认不会重复提交。

必须完成：
1. 模型返回 tool calls 后，在任何工具执行前原子保存唯一 assistant envelope（含 run_id、attempt、call ids、
   manifest/route/cursor 关联）；持久化成功后才进入 tools phase。
2. 每个工具结束、超时、取消或结构化拒绝后立即追加恰好一个配对 tool result，并更新 cursor。写工具结果必须引用
   既有 ToolOperation；提交状态不确定时不得重执行。
3. append API 支持 idempotency/唯一约束，使同一 call 重放不会产生第二条消息；禁止孤立 result、重复 call id、
   跨 run 配对和旧 fencing token 提交。保持模型下一轮看到的消息顺序与原有行为一致。
4. 移除 service 层对同一 assistant/tool 消息的事后重复追加，同时兼容无 tool call 的普通回答（仍由后续 finalizer 收口）。
5. 用 fault injection 覆盖 envelope 前后、只读工具结果前后、已提交写 operation 后重入，并验证配对、顺序、幂等副作用。

此阶段工具仍顺序执行。运行 agent/runtime/transaction/recovery 专项与全量 pytest，更新交接。下一提示词 R2.4。
```

### R2.4 幂等 TurnFinalizer

```text
你正在执行 R2.4。本会话建立唯一 TurnFinalizer，统一最终消息、Plan 验证、usage/budget 和 run 终态；不实现流式。

先阅读通用协议、R2.3 交接、service.chat 的 try/except/finally、agent 最终返回、Plan verifier、finish_run、lease
释放、provider usage 和 API request replay。列出目前所有成功/失败/取消收尾路径后再收口。

必须完成：
1. 实现可重复调用的 TurnFinalizer，固定顺序：关闭/标记未配对工具 call -> 完成 Plan/Evidence 验证 -> 提交唯一
   final assistant message（仅成功需要）-> 结算 usage/budget -> 标记 terminal -> 触发后处理钩子 -> 有界 cleanup。
2. 以数据库唯一键/CAS 保证重复 finalizer、恢复 worker 与旧 worker 竞争时只产生一个终态和一条最终消息；不能仅靠
   进程内布尔值。取消、预算耗尽、模型失败、manual_review 分别有稳定 stop_reason。
3. lease 释放和 API request completion 必须发生在可证明的 terminal 之后；finalizer 中途崩溃可从 cursor 继续，
   已完成步骤不重做。后处理钩子失败只记录审计事件，不反转主 turn 成功。
4. 删除散落的重复 finish/append/usage 逻辑，保持现有 ChatResult 和 API 幂等响应兼容。
5. 测试重复调用、每个 finalizer 子步骤后崩溃、取消/失败、两个 worker 竞争、最终消息后恢复和后台钩子失败。

运行 service/state/plan/API recovery 专项与全量回归，更新交接。下一提示词 R2.5。
```

### R2.5 Provider 真流与同步聚合兼容层

```text
你正在执行 R2.5。本会话在 Provider Gateway 增加真正的流事件迭代器，并让同步 chat 通过聚合它保持兼容；
不修改 HTTP SSE，也不实现完整 CancellationToken 传播。

先阅读通用协议、R2.4 交接、R1 两个 adapter、Engine/EngineResponse、RunEvent v2 和 SDK 当前锁定版本。
依据真实 SDK/wire fixture 实现，不凭字段猜测。

必须完成：
1. 定义内部 ProviderStreamEvent：text delta、tool call id/name/arguments delta、usage、completed、error；包含 route、
   attempt 和 provider event id。处理 UTF-8/多字节文本、多个交错 tool call、空块和仅终块 usage。
2. 为 Chat Completions 与 Responses adapter 实现流式解析；tool arguments 只在完成后进入 JSON/Schema 校验，半段 JSON
   不得执行工具。未知事件要可审计地忽略或失败，策略必须有测试。
3. 同步 chat() 聚合该事件流为与 R1 一致的 EngineResponse，使现有 eval/mock/Agent 调用方不必一起迁移；不得维护
   一套同步请求和一套流请求的重复核心解析。
4. 明确重试边界：首个可见 delta 前的瞬态错误可按 R1 策略重试；对客户端已发送 delta 后的失败不得无提示拼接
   另一个模型输出。先记录 error/attempt，具体 SSE terminal 行为在 R2.6 完成。
5. 用 fake stream fixture 覆盖碎片化 arguments、交错 calls、usage、流中断、旧 attempt 迟到和同步聚合等价性。

不访问公网。运行 provider/engine/event 专项与全量回归，更新交接。下一提示词 R2.6。
```

### R2.6 HTTP SSE、统一中断与 Writer Fence

```text
你正在执行 R2.6。本会话让 API SSE 直接消费 R2.5 真流，并将统一 CancellationToken 贯穿 Provider、Agent、工具、
子 Agent 与代码执行边界；不新增崩溃恢复策略。

先阅读通用协议、R2.5 交接、api.py::_stream_chat、RuntimeManager cancel/lease、tool_executor、delegation、
code_execution Provider 和现有真实 socket 测试。

必须完成：
1. SSE 映射 typed RunEvent，至少输出 accepted、text.delta、tool.started/completed、plan.updated、usage、completed/error；
   保留 keepalive 仅作连接保活，不能再把它当 streaming 能力。事件 id 使用单调 sequence，支持合理的断线语义。
2. 每个 run 只有一个带 fencing token/attempt 的 StreamWriter；fallback 前流、恢复前 owner 和 terminal 后 delta 均被拒绝。
   并发生产事件可以到达，但写 socket 必须单 writer，不能多个线程直接写 handler。
3. 客户端断开、显式 cancel 和 deadline 共用 CancellationToken，传给 Provider adapter、模型循环、工具 executor、
   delegation 和 sandbox。无法强杀的同步 SDK 返回后必须检查 token/fence，禁止提交迟到结果。
4. 定义背压和关闭：有界队列、慢客户端策略、writer 异常、Provider 未停时的有界清理；不要泄漏线程或 run lease。
5. 真实 127.0.0.1:0 socket 测试首 delta 顺序、断流取消延迟、半个 tool JSON 中断、fallback 旧流迟到、terminal 后
   无 delta、慢消费者和重复 cancel。环境沙箱阻止绑定时按权限复跑，不能改成纯 mock 代替。

运行 API/stream/cancel/sandbox/delegation 专项与全量回归，更新交接。下一提示词 R2.7。
```

### R2.7 五崩溃窗恢复与 R2 收口

```text
你正在执行 R2.7。本会话完成恢复决策、五个崩溃窗和 R2 独立门禁；不得开始工具并发。

先阅读通用协议、R2.1-R2.6 交接、RunJournal、TurnFinalizer、ToolOperation、startup recovery 和路线中的恢复合同。
为每个稳定 cursor 写出 continue/replay-read/reuse-operation/manual-review/terminal-replay 决策表并落实为代码。

必须完成：
1. 恢复只从已声明稳定边界继续：无法证明完成的只读调用可重放；写调用查询 operation/idempotency 状态，committed
   复用回执，prepared/状态不确定按现有事务合同恢复或进入 manual_review，绝不盲执行。
2. 为模型返回后、tool-call envelope 后、只读结果后、写提交后、最终消息后五个窗口增加进程重开级 fault fixture。
   使用持久 SQLite 重新构造 Service，不能只在同一对象捕获异常。
3. 验证恢复后 tool call/result 完整配对、final 唯一、event sequence/cursor 单调、route/manifest/budget 不漂移、
   旧 writer/fence 失效、写副作用不重复、API request replay 字节契约仍成立。
4. 提供可演示的 recovery 脚本或扩展现有 runtime_recovery_demo，输出脱敏决策和 Trace，不靠读取内部表手工解释。
5. 审核文档：只有真实 delta、统一中断、journal/finalizer 和五窗测试均通过时，才把对应能力改为已实现。

运行 ruff、全部 R2 故障测试、全量 pytest、真实 socket 测试和 accept_stage8.sh。真实 Provider 网络流不是离线门禁，
但两种 mode 的 wire fixture 必须通过。更新进度账本；全部满足才记 R2 gate=passed，下一提示词 R3.1；否则留在 R2.7。
```

## 7. R3：工具目录、参数治理与安全并发

### R3.1 不可变 ToolManifest

```text
你正在执行 R3.1。前置条件是 R2 gate=passed。本会话只扩展工具元数据并冻结 run 级 ToolManifest；不迁移
SQLite 工具、不修复参数、不并发执行。

先阅读通用协议、R3 路线、R2 交接、tools/registry.py/schemas.py、extensions.py、MCP provider、Plan 的工具裁剪、
runtime/tool_executor.py、security 和 RunJournal manifest hash 字段。先盘点全部 16 个内置工具及条件式 RAG 工具。

必须完成：
1. 扩展 ToolSpec/定义 ToolManifestEntry，至少包含 source、version、canonical schema hash、capability、risk、effect、
   parallel_safe、resource key 规则、timeout、allowed roles 和字段级数据分类。effect 使用明确枚举，不能靠工具名猜。
2. 注册时完整校验 JSON Schema、handler 契约、名称/source 冲突和元数据组合；未知插件默认最高风险、非并发，
   未声明 capability 不暴露。schema hash 使用确定 canonical form，字典顺序不影响结果。
3. 在 run 开始按 actor/tenant/role/course、模型 capability、RAG 和代码执行健康状态生成并冻结 manifest，将 hash 写入
   journal/Trace。Plan allowed_tools 只能收窄；executor 仍逐次复验 ACL、审批和健康状态。
4. 同一 run 中插件注册变化不能改变已冻结工具面；恢复时 hash 不一致应拒绝执行或走明确兼容决策，不能静默漂移。
5. 测试 16 个工具元数据完整性、hash 稳定、冲突注册、角色/capability 裁剪、动态 registry 变化和恢复不匹配。

保持工具顺序执行及原参数行为。运行 tools/plan/MCP/plugin/security 专项与全量回归，更新交接。下一提示词 R3.2。
```

### R3.2 Canonical Teaching Provider 与只读切片

```text
你正在执行 R3.2。本会话建立教学领域 Provider 防腐层，并先迁移查询/分析/知识图谱只读切片到 SyntheticProvider；
不要实现真实 TeachingPlatformProvider，也不要迁移事务写工具。

先阅读通用协议、R3.1 交接、tools 下全部只读实现、data/db.py、knowledge/provider.py、registry dispatch 和相关测试。
明确区分本阶段“教学数据 Provider”和 R1“模型 Provider Gateway”，命名和文档不能混淆。

必须完成：
1. 从实际工具语义抽出最小 Canonical Query/Result/Error 契约，覆盖 list/query/search/progress/analysis/knowledge path
   的现有业务切片；规范化结果不暴露 SQLite Row、表名或生产 ORM。
2. 实现 registry-backed SQLite SyntheticProvider，使用 connection factory；正常调用由每个 worker/调用自己获取连接，
   仅现有事务边界需要时显式传入受控连接。不得在线程间共享 sqlite3.Connection。
3. 将对应工具 handler 薄化为 schema/context -> canonical provider -> ToolResult 映射，使 Agent 图和 ToolManifest 不感知
   SQLite。保持现有返回 JSON、ACL、course scope、Evidence/citation 语义兼容。
4. 建立 Provider contract test 基类，用 SyntheticProvider 和一个纯 fake adapter 运行同一只读用例，覆盖分页/空结果、
   状态映射、scope 拒绝、错误分类和确定顺序；fake 只证明契约，不冒充真实平台。
5. 不复制整个生产数据模型，不读取本机教学平台数据库，不把 private DDL 加入测试 fixture。

运行只读工具、RAG/Plan/Evidence 和 contract 专项及全量回归，更新交接。下一提示词 R3.3。
```

### R3.3 写工具契约与 Provider 收口

```text
你正在执行 R3.3。本会话完成剩余教学工具的 Canonical Provider 适配和 16 工具契约矩阵；不做参数修复或并发。

先阅读通用协议、R3.2 交接、runtime/transactions.py/tool_executor.py、ops_tools.py、ai_tools.py、代码执行 Provider、
outbox/compensation 和事务测试。先确认哪些工具是教学写入、条件写入或独立执行能力。

必须完成：
1. 为 create_exam、generate_paper、batch_grade、assign_homework、generate_questions(save_to_bank) 等补最小 canonical
   command/receipt/error 契约，但写入仍必须经过既有审批、ToolOperation、幂等业务键、同库事务/outbox 和补偿状态机。
   SyntheticProvider 不得绕开 executor 直接提交副作用。
2. `run_code` 保持独立 CodeExecutionProvider capability，不错误塞进教学数据库抽象；纯生成不保存与保存题库要按实际
   effect 分流。条件写入在参数验证后才能判断，不能依赖模型自述。
3. 让 16 个内置工具通过统一 ToolProvider/ToolResult 边界；替换为 contract fake 时 Agent 图不改。保持 MCP 本地/远端
   返回形状和操作回执兼容。
4. 扩展契约矩阵覆盖成功、业务拒绝、审批缺失、重复 request/idempotency、commit 后崩溃、manual_review、outbox
   重投去重和 scope。fake Provider 不能跳过 executor 的安全门。
5. 删除已被 Provider 取代的重复 SQL 调度层，但不要大规模重写稳定 SQL 本身；记录未来 TeachingPlatform 映射所需
   capability，不实现真实连接。

运行全部工具、事务、MCP、Agent/Evidence 和全量测试，更新交接。下一提示词 R3.4。
```

### R3.4 参数规范化、校验与 Repair Audit

```text
你正在执行 R3.4。本会话实现 schema-guided、无歧义的工具参数规范化和完整校验；不做工具并发。

先阅读通用协议、R3.3 交接、当前 parse_tool_arguments/_validate_value、全部 JSON Schema、ToolManifest 数据分类、
ToolOperation 幂等键和坏参数测试。先定义允许转换表和禁止猜测表。

必须完成：
1. 三层管线：严格解析 JSON object -> schema-guided deterministic normalization -> 完整 JSON Schema 语义校验。
   禁止未知字段，正确处理 required、null、integer/number（避免 bool 误当 int）、boolean、array/object、enum、范围和长度。
2. 只允许可证明且可逆理解的转换，例如无额外字符的 `"42" -> 42`、严格 `"true"/"false"`、JSON 字符串形式的
   array/object；是否允许转换由字段 schema 明确控制。ID、自由文本、近似 enum、日期、学号前导零和业务默认值不猜。
3. 写/条件写参数采用更严格策略；规范化后再计算 effect/resource/idempotency，不允许修复改变审批语义。坏 JSON 不做
   字符串补括号式猜测，继续结构化回灌，单个 call 最多消耗一次参数重试预算。
4. Repair audit 记录 JSON Pointer、原类型/目标类型、规则 id 和结果；敏感原值只存脱敏摘要/hash，Trace 不泄漏正文。
5. 建立坏参数 corpus，覆盖嵌套对象、额外字段、边界数值、Unicode 文本、前导零、NaN/Infinity、超深/超大输入、
   写参数和恶意 JSON；验证 deterministic、预算有界且 handler 永远收不到未验证参数。

运行参数/工具/事务/Trace 专项与全量回归，更新交接。下一提示词 R3.5。
```

### R3.5 连续 Segment 的安全有界并发

```text
你正在执行 R3.5。本会话只为同一 assistant 回合实现 ToolBatchPlanner 和安全并发；不要并发写工具、审批、代码
执行、未知插件，也不要修改子 Agent 委派并发。

先阅读通用协议、R3.4 交接、agent/graph.py::tools_node、ToolManifest/effect/resource keys、ToolExecutor、RunBudget、
CancellationToken、SQLite connection factory 和消息增量提交不变量。

必须完成：
1. 按模型原始 call 顺序切连续 segment。只有参数已验证、effect=read、parallel_safe=true、resource key 无冲突且 Provider
   capability 允许的调用进入同一并发 segment；write/conditional-write/approval/code/interactive/unknown/坏参数形成 barrier。
2. 使用配置化小型 worker 上限和每调用 timeout。每个 worker 获取独立 SQLite connection/context；CancellationToken、
   actor/tenant/course、fencing、route/manifest 和 Trace context 必须传播，不能依赖线程局部偶然继承。
3. tool.started/completed 可按实际时间发布，但追加给模型的 tool result 和增量 journal cursor 必须按原 call 顺序；
   每个失败、超时、取消也产生恰好一个配对结果，不能因 fail-fast 留孤立 call。
4. model/tool/budget 扣减并发原子化；预算不足时确定性选择可启动调用，不能超卖。取消后迟到 worker 结果由 fence 拒绝。
5. 测试串行/并行结果等价、可重复 P95 加速 fixture、最大并发、读写 barrier、同资源冲突、连接隔离、预算竞争、
   中途取消、一个 worker 失败和结果顺序。不要用 sleep 时序作为唯一断言，使用 barrier/event 控制。

运行 batch/tools/journal/transaction/cancel 专项与全量回归，更新交接。下一提示词 R3.6。
```

### R3.6 Plugin/MCP 安全边界与 R3 收口

```text
你正在执行 R3.6。本会话验证本地 registry、entry point plugin 和 MCP 在新 Manifest/Provider/并发合同下的一致性，
并收口 R3；不得开始上下文压缩优化。

先阅读通用协议、R3.1-R3.5 交接、extensions.py、mcp/client.py/server.py、动态 RAG/代码工具注册和 R3 门禁。

必须完成：
1. plugin/MCP 都必须提供可验证 source/version/schema hash/effect/capability；缺失或冲突默认拒绝注册，不能自动标记
   parallel_safe。远端 MCP 返回仍经过本地参数校验、ACL、数据分类、超时/取消和结果预算。
2. 会话/run manifest 冻结后，MCP 重连、插件热变更或 schema 漂移不能改变当前执行面；恢复 hash 不匹配走明确错误。
3. capability/role/course 裁剪在模型可见 schema 和 executor 二次鉴权两处一致，测试恶意 Provider 绕过、名称抢占、
   schema collision、伪造 effect、超大结果和断线迟到结果。
4. 增加串行/并行对照报告或测试指标，证明只读 fixture 有稳定收益且写副作用、ACL 泄漏、孤立 tool result 为 0。
5. 更新架构/演示/README，只把已通过的 Manifest、规范化和安全并发写成已实现；真实 TeachingPlatformProvider 仍是 L1。

运行 ruff、全部 R3 专项、全量 pytest、MCP demo 和 accept_stage8.sh。只有 16 工具契约、Manifest 冻结、参数 corpus、
并发/串行等价与安全边界全部通过，更新进度账本，才记 R3 gate=passed，下一提示词 R4.1；否则留在 R3.6。
```

## 8. R4：上下文、全树预算与长期运行

### R4.1 可解释 Context Accounting

```text
你正在执行 R4.1。前置条件是 R3 gate=passed。本会话只建立准确、可解释的 context accounting 和输出预留；
不触发新压缩策略，不实现 overflow retry。

先阅读通用协议、R4 路线、R3 交接、runtime/context.py/context_engine.py/config.py、Engine usage、Provider capability、
Agent 消息构建和当前上下文测试。列出 system、tool schema、history、current input 和 output reserve 的现有口径。

必须完成：
1. 定义 ContextBreakdown，分别统计 system prompt、冻结 ToolManifest schema、历史消息、当前 user turn、Plan/Evidence
   注入、工具结果和最大输出预留；总预算不能只用 `len/4` 或遗漏 tools。
2. 计数优先级：Provider 返回的实际 usage 用于校准/结算；已知模型 tokenizer 用于请求前计数；无法获得时使用明确版本、
   可校准且保守的 estimator。不要为了支持所有模型引入重型 tokenizer 矩阵。
3. ResolvedRoute/Capabilities 声明上下文和最大输出限制；配置值不能超过 Provider 能力。当前 user 输入单独就超限时返回
   专门错误，不能靠删除 system/tool schema 强行发送。
4. breakdown、估算误差和决策进入脱敏 Trace；不记录完整 prompt。保持 system prompt 字节稳定和工具原子组不拆分。
5. 测试中英文本、tool schema 增长、多 tool call/result、Plan、不同 route limit、未知 tokenizer、输出预留和估算校准。

运行 context/provider/agent 专项与全量回归，更新交接。下一提示词 R4.2。
```

### R4.2 Artifact 优先与可追溯 Checkpoint

```text
你正在执行 R4.2。本会话升级压缩前的大结果外置和 checkpoint provenance；不改变压缩触发阈值，也不处理 Provider
context overflow。

先阅读通用协议、R4.1 交接、runtime/artifacts.py、context_engine.py、StateStore.compact_messages/checkpoint schema、
Plan/Evidence/citation/operation receipt 和数据分类规则。

必须完成：
1. 压缩前优先把超过预算的大 tool result 存入现有 scoped Artifact，并在消息中保留 typed reference、hash、preview、
   classification 和必要业务回执；不能重复创建第二个 blob store，也不能把敏感原文放进 preview。
2. checkpoint 保存 source message sequence range、每条或整体 source hash、策略/estimator 版本、summary hash、token
   before/after、保留项清单、Artifact/citation/operation 引用和创建 run。旧原文软归档可恢复，不物理删除。
3. 明确保留 system、当前 turn、未完成 Plan、审批/operation receipt、citation、Artifact ref、用户明确约束和未配对工具组；
   checkpoint 不得把不同 tenant/actor/session 内容合并。
4. 为旧 checkpoint schema 增加 migration/兼容读取；hash 不符、Artifact 缺失或 scope 不匹配时结构化失败并可审计，
   不静默继续生成答案。
5. 测试大工具结果、敏感 preview、原子 tool 组、引用恢复、hash 篡改、跨 scope、旧库 migration 和重复 checkpoint。

运行 artifact/context/state/security 专项及全量回归，更新交接。下一提示词 R4.3。
```

### R4.3 压缩反抖动、保真评测与 Overflow Recovery

```text
你正在执行 R4.3。本会话完成可恢复压缩策略、保真评测和一次 Provider context-overflow recovery；不实现预算总账。

先阅读通用协议、R4.2 交接、ContextBreakdown/checkpoint、Provider FailureKind、Agent model attempt journal 和 docs/eval.md。

必须完成：
1. 使用 trigger/release 双阈值、最小回收量和冷却规则，避免每轮压缩；优先归档已完成旧 exchange，大结果先变
   Artifact ref，再对剩余旧内容做确定性摘要。默认不做持续改写每轮历史的 micro-compaction。
2. 摘要必须保留用户约束、实体/课程 scope、未完成 Plan、operation/approval 状态、citation 与 Artifact ref；自由文本
   摘要不得跨 scope。若确定性摘要不足，可设计可选模型摘要，但离线默认和恢复不能依赖外部模型。
3. 建立 context fidelity corpus 和指标：关键约束/实体/operation/citation 保真、scope leak、压缩率、重复触发率和估算误差。
   门槛配置在测试/评测中，不以几个手写例子代替。
4. Provider 明确返回 input context overflow 时，执行至多一次“重新计数 -> checkpoint/压缩 -> 同冻结 route 重试”，并写
   journal/Trace；output cap、invalid request、当前输入本身过长和第二次 overflow 直接分类失败，不进入循环或 fallback 伪装。
5. 测试阈值抖动、无可压内容、关键项过大、一次恢复成功、第二次 overflow、finalizer/cancel 竞争和重启后不重复压缩。

运行 context/fidelity/provider/recovery 专项及全量回归，更新交接。下一提示词 R4.4。
```

### R4.4 持久 RunBudgetLedger 与全树结算

```text
你正在执行 R4.4。本会话把父级主 Loop、planner、压缩、fallback、工具和所有 child 纳入一个持久预算总账；
不实现进程 drain。

先阅读通用协议、R4.3 交接、runtime budget/models/manager、agent graph 调用计数、delegation IterationBudget 与
delegation_roots 持久化、Provider usage、ToolBatchPlanner 和 RunJournal。保留现有更严格 child 预留语义。

必须完成：
1. 定义 RunBudgetLedger 的 root identity、维度和单位：model calls、tool calls、input/output/total tokens、cost、wall time，
   明确哪些内部操作收费/计次。不得把 Hermes 的独立 child 新预算当作共享预算实现。
2. 新增幂等 migration 和原子 reserve/commit/release API。child 启动前预留上限，结束按实际结算释放余量；父级模型、
   planner、压缩、retry/fallback attempt 和并发工具使用同一 root ledger，不能各有隐藏总账。
3. 每次扣减绑定稳定 operation/attempt id，重放不会二次收费；恢复加载已用/预留额度，不能刷新预算。并发 reserve
   防超卖，预算耗尽给出确定 stop_reason，并由 finalizer 只结算一次。
4. Provider usage 缺失时使用 R4.1 估算并标记 estimated；cost 依赖显式、版本化价格配置，未知价格保持 unknown，不能写 0
   冒充免费。Trace 只记录聚合用量，不泄漏 prompt。
5. 测试父级+两 child、planner/压缩/fallback、并发 reserve、child 失败释放、进程重开、重复 finalizer、未知 usage/cost、
   多 root 隔离和各维度耗尽。

运行 budget/delegation/provider/journal/transaction 专项与全量回归，更新交接。下一提示词 R4.5。
```

### R4.5 进程 Lifecycle、Readiness 与有限 Drain

```text
你正在执行 R4.5。本会话实现进程级 starting/running/draining/stopped、readiness 和安全停机；不做数据库备份工具。

先阅读通用协议、R4.4 交接、API server 启停、RuntimeManager lease/heartbeat/cancel、Scheduler、MCP/code execution
close 行为、TurnFinalizer 和 startup recovery。区分进程 lifecycle 与单 run/session 状态。

必须完成：
1. 建立线程安全 LifecycleController：starting 完成 migration/必要健康检查后才能 running；draining 后 readiness 失败并
   拒绝新 chat/job，但 liveness 在进程仍可收尾时保持正常；状态转换单调且可审计。
2. SIGTERM/显式 shutdown 进入 draining，在配置 deadline 内等待已有 run/finalizer、Scheduler claim、MCP/Provider stream；
   超时后发 cancellation，持久化未完成 run 为可恢复状态并执行有界 final flush。不能无限 join 或先删 lease。
3. API/Scheduler 都在接收边界复验 lifecycle，竞态下已 accepted 的 run 有清晰归属。旧 worker/迟到 callback 继续由
   fencing 拒绝。shutdown hook 重复调用幂等，后台 review 尚未实现时不要增加它。
4. health/readiness 响应只暴露安全聚合状态，包括 migration、state DB 可写、draining 和必要 Provider 健康；外部模型短暂
   故障是否影响 readiness 要有明确策略，不能泄漏 endpoint/key。
5. 用可注入 clock/event 测试正常 drain、deadline、接收竞态、Scheduler、阻塞 Provider/工具、重复信号、final flush 失败、
   重启恢复和无线程/lease 泄漏。增加真实 HTTP lifecycle 测试。

运行 lifecycle/API/scheduler/recovery/cancel 专项及全量回归，更新交接。下一提示词 R4.6。
```

### R4.6 SQLite 备份恢复、Retention/GC 与 R4 收口

```text
你正在执行 R4.6。本会话完成 migration/在线备份恢复、retention/GC、存储故障演练并收口 R4；不得开始候选版包装。

先阅读通用协议、R4.1-R4.5 交接、StateStore 连接/WAL/migration、Artifact store、cleanup API、RunJournal/operations/
outbox/checkpoints 和 R4 门禁。先定义哪些状态可删、何时可删以及引用关系。

必须完成：
1. 使用 SQLite 官方 backup API 或等价一致快照，在不复制未提交/WAL 不一致状态的前提下生成可校验备份；备份 manifest
   记录 schema version、时间、hash 和安全环境信息，不包含凭据。目标路径显式且不能覆盖未知用户文件。
2. 提供恢复到空目录/新 state path 的命令或脚本，恢复前校验 hash/schema/integrity，拒绝降级或覆盖活跃数据库；恢复后
   自动跑 migration，并验证 session/run/journal/operation/artifact 引用。不要提供危险的宽路径删除命令。
3. retention/GC 只清理超过策略且 terminal、无 pending operation/outbox、无恢复/审计保留需求的记录；按外键顺序、批量
   有界、dry-run 可审计。Artifact 只有无引用且过期才删，manual_review 和活跃 checkpoint 不得清除。
4. 演练磁盘满、只读 DB、损坏备份、写入中断和 migration 中断；主链返回稳定错误，不能部分 final、刷新 budget 或重复副作用。
5. 更新运维与架构文档，明确 SQLite 只保证共享同一文件的本机 Worker，不宣称跨主机共识。

收口验证：ruff、全部 R4 专项、全量 pytest、长会话 fidelity、SIGTERM/drain、备份到空目录恢复 integrity、GC dry-run 和
accept_stage8.sh。更新进度账本；所有门禁满足才记 R4 gate=passed，下一提示词 R5.1；否则留在 R4.6。
```

## 9. R5：秋招可交付候选版

### R5.1 候选版验收编排与证据清单

```text
你正在执行 R5.1。前置条件是 R4 gate=passed。本会话只收口候选版验收编排、报告 schema 和证据清单；不访问
真实模型、不部署服务、不实现新 Runtime 功能。

先阅读通用协议、R5 路线、R0-R4 交接、accept_stage8.sh、eval_system.py、数据审计、Trace benchmark 和所有专项
验收。逐项确认 R1-R4 的测试是否真正进入最高阶段门禁，不能因为文件存在就算已覆盖。

必须完成：
1. 让一条公开命令完成环境检查、ruff、全量/关键故障测试、API/Demo、数据边界审计、10k Trace 基准、离线评测和
   脱敏报告；可以调用分项脚本，但只有一个公开入口，失败码必须正确传播且不重复运行昂贵步骤。
2. 扩展稳定、版本化的系统评测报告，分别展示 Agent/Plan、Provider route/retry、stream/cancel、journal/recovery、
   ToolManifest/并发、context、budget、transaction、sandbox、performance、provenance 和数据边界。每项使用
   passed/failed/not_run/not_verified，禁止用缺失字段冒充成功。
3. 报告记录 commit、config hash、seed、API mode、模型 route、schema/tool manifest 版本、测试环境和耗时；中心脱敏后
   再落盘。候选版模式下 commit unavailable、lineage 泄漏或必需离线项 not_run 都应失败。
4. 建立 evidence checklist，将每个 README 技术表述链接到源码、专项测试和验收报告字段；计划中或仅 fake/oracle 的
   能力不得进入“已实现/真实验证”。
5. 更新 docs/eval.md 和进度文件，明确 Docker、真实模型、私有平台的独立验证边界。

运行报告 schema/acceptance 专项和一次不访问外网的最高阶段验收。Docker 不可用可为 not_verified，但离线核心项必须绿。
更新交接，下一提示词 R5.2。
```

### R5.2 固定真实模型独立评测

```text
你正在执行 R5.2。本会话只完成一个固定真实模型 route 的独立 Test 评测和结果分析；不调参训练、不更换多个 Provider、
不使用真实学生数据。若外部调用会产生费用或当前没有明确授权/凭据，先完成离线 preflight，然后报告阻塞并停在 R5.2，
不得发起调用、伪造结果或把 fake fixture 写成真实模型通过。

先阅读通用协议、R5.1 交接、docs/eval.md、lineage、Provider Gateway、预算/成本配置、现有真实模型 eval runner 和脱敏规则。

必须完成：
1. 冻结且记录一个候选 route（provider/api_mode/model/endpoint identity，不含 key）、temperature、seed（若 Provider 支持）、
   max output、并发、超时、预算和代码版本。运行前做 capability、网络、费用上限、数据无敏感项和 Test lineage preflight。
2. 只对从未用于 prompt/repair/开发调参的独立 Test 运行；至少按评测设计重复足够次数，报告均值、方差/区间、成功率、
   tool precision/recall、Plan/Evidence、恢复安全、首 token/总延迟、tokens 和实际/未知 cost。不要用一次最好结果。
3. 保存每次运行的脱敏原始 JSON 和失败 Trace 引用，区分模型失败、Provider 故障、harness bug 和环境 not_verified。
   失败样本不能在本会话回填 Test prompt 后重跑并仍称 holdout。
4. 与离线 oracle/fake 分栏对比，给出诚实结论和最多三个有证据的后续改进；不在本会话启动 SFT/DPO 或修改核心行为。
5. 把结果接入 R5.1 报告；凭据永不写入配置、数据库、shell 历史示例、Trace 或最终回复。

只有真实请求确实完成、原始证据可审计且报告不再是 not_run 时才将 real_model 标为 verified。更新交接，下一提示词
R5.3；若阻塞，记录所需 CredentialRef/费用授权/网络条件并要求下个会话重新执行 R5.2。
```

### R5.3 容器化、Health/Readiness 与最小运行手册

```text
你正在执行 R5.3。本会话交付可复现 API 容器和最小生产运行手册；不新增前端、Kubernetes、多机数据库或平台 Provider。

先阅读通用协议、R5.2 交接、API server/config、LifecycleController、SQLite/Artifact/backup、代码执行部署文件、当前
health/readiness 和 secret 配置。先写威胁/运行边界，再选择仓库已有容器模式。

必须完成：
1. 增加最小、多阶段、非 root 的 API 镜像构建；依赖使用 lockfile，镜像不包含 .git、.env、凭据、私有数据、生成数据库、
   dpo dumps 或测试缓存。持久 state/artifact 使用明确挂载，默认只监听安全地址/配置。
2. 容器启动执行受控 migration/preflight，health/readiness 映射 R4 lifecycle；SIGTERM 有足够 stop grace 进入 drain。
   不把外部模型瞬时波动错误地当作进程死亡，也不能在 DB 只读/迁移失败时 ready。
3. 提供最小 compose 或等价本机部署，只包含 EduAgent 必需服务；Jobe/Docker code execution 保持可选且默认关闭，不能把
   Docker socket 无限制挂入 Agent 容器。
4. 编写运行手册：配置/secret 注入、启动、健康检查、日志/Trace、备份、恢复、升级 migration、回滚、drain、磁盘告警和
   常见 Provider 故障。所有命令使用明确安全路径，不包含真实 key。
5. 测试镜像内非 root、无凭据/私有文件、只读 rootfs 可行性、持久卷、restart、SIGTERM drain、backup/restore 和 API smoke。
   若当前环境不能运行 Docker，构建/E2E 标 not_verified 并保留静态验证，不能宣称部署已验收。

运行配置/部署专项、全量回归和允许环境下的容器 smoke，更新交接。下一提示词 R5.4。
```

### R5.4 十分钟演示与故障复盘

```text
你正在执行 R5.4。本会话只打造可重复的 10 分钟候选版演示和面试讲解证据；不加 UI，不为了演示硬编码成功结果。

先阅读通用协议、R5.3 交接、docs/demo-script.md/interview-guide.md、现有 demos、Trace inspector 和 R5 成品判断标准。
选择固定 seed 合成任务，不能读取私有平台或依赖临时外网数据。

必须完成：
1. 演示主线按时间预算展示：Provider route/api mode -> 真 text delta -> 两个安全只读工具并发 -> Plan/Evidence ->
   审批后的幂等写 -> 用户中断或注入崩溃 -> 恢复 -> Trace/预算/上下文复盘。每一步必须由真实运行事件证明。
2. 提供一个正常路径和一个故障路径的可重复脚本；故障注入使用显式测试开关且默认关闭，不能污染生产配置或通过
   `sleep` 猜时序。写操作使用合成库和稳定 idempotency key，可重复演示不累积副作用。
3. Trace inspector 能回答选了哪个 route、为何 retry/fallback、哪些调用并发、参数是否规范化、哪里压缩、恢复选择、
   父子预算如何结算；输出经过 scope/脱敏，不直接要求面试官读 SQLite 表。
4. 更新 demo-script 和 interview-guide：明确“已实现、fixture/离线验证、真实模型已验证、尚未验证”四类口径，保留失败
   情况下的讲解路径，不把网络或 Docker 偶发性藏起来。
5. 给演示脚本增加 smoke/快照断言，校验关键事件而非整段脆弱文本；在目标机器测量时间但不把一次耗时写成 SLA。

完整跑一遍正常与故障演示，保存脱敏示例报告并运行相关回归。更新交接，下一提示词 R5.5。
```

### R5.5 发布审计与 R5 最终收口

```text
你正在执行 R5.5。这是 R0-R5 最终收口会话，只修复阻止候选版发布的问题；不得顺手实现 L1 真实平台、L2 Memory/Skill、
L3 Curator，也不得自行 push、发布镜像、部署外部环境或创建 release/tag，除非用户另行明确授权。

先阅读通用协议、全部进度交接、路线图“秋招成品判断标准”、README/architecture/eval/demo/interview/runbook、CI、
系统评测报告和当前工作区。以源码和可复现证据建立最终审计表。

必须完成：
1. 逐条审计 R0-R5 门禁、README 技术表述、配置默认、安全边界、migration、兼容性和本地链接；删除或降级无证据声明。
   检查仓库不包含 key、cookie、私有 DDL/数据、可识别 Trace、生成数据库/大 dump 或容器构建秘密。
2. 在干净、明确环境运行 ruff、全量 pytest、accept_stage8.sh、正常/故障演示、数据审计、Trace benchmark、备份恢复和
   容器 smoke（若可用）。核对报告 commit/config/manifest/schema、真实模型证据和所有 not_verified。
3. 审核关键故障：429/fallback、断流、五崩溃窗、重复写、manifest 漂移、坏参数、并发 barrier、context overflow、
   全树预算耗尽、SIGTERM drain、磁盘/备份失败。发现回归应做最小修复并重跑相关门禁，不以文档解释代替修复。
4. 生成 docs/release-readiness.md：候选版本、复现命令、证据链接、已验证环境、残余风险、明确非目标、回滚步骤和面试可说/
   不可说边界。不要写不存在的线上用户、吞吐或生产 SLA。
5. 更新进度文件 `docs/optimization-progress.md`：只有必需门禁全过才写 R5 gate=passed；Docker/私有平台等可选项保留真实状态。R5 后建议只从
   L1/L2/L3 选一个有真实需求的方向，不能默认三个并行。

最终回复给出候选版是否 ready 的二元结论、测试数字、证据文件、残余 not_verified 和最小下一步。若任何必需项失败，
结论必须是 not ready，并停在 R5.5；不得为了完成提示词降低门禁。
```

## 10. 范围边界

完成本文件不等于必须继续做路线图 `L1-L3`。秋招候选版达到 R5 后，真实教学平台、Memory/人工 Skill 或受控 Curator
应按真实需求三选一，而不是继续堆功能。尤其不要在 R0-R5 中提前加入：

- Telegram/Discord/Slack 消息 Gateway、桌面端、通用浏览器/终端和多媒体工具全集。
- Anthropic/Gemini 等完整 Provider 矩阵、OAuth 或大型 credential pool。
- 自动激活 Skill、自动修改生产代码、后台执行教学写操作或递归委派。
- 生产学生数据、未经授权的私有 DDL，以及 XES/MOOCCube/CodeNet 等大规模数据下载。
- 跨主机 SQLite 共识、强杀任意第三方 SDK、exactly-once 等当前架构无法诚实保证的表述。

这些范围只有在 R5 已通过、出现明确用户故事、威胁模型、维护预算和独立验收门禁后，才应另写提示词。
