# EduAgent · 可恢复、可审计的教学 Agent Runtime

EduAgent 解决的不是“再包一层聊天界面”，而是教学 Agent 的工程失效：多步任务提前结束、写工具重试产生重复副作用、并发 Worker 污染同一会话、检索越权，以及故障发生后无法还原过程。它以 `EduAgentService` 为唯一运行入口，在同一条执行链上组合 Plan/Evidence、课程 RAG、事务写工具、SQLite lease/fencing、受限子 Agent、可选隔离代码执行 Provider 和统一 Trace。

```text
HTTP / Scheduler / Demo
        -> EduAgentService -> Agent Loop -> Engine / Tool Provider
                |                 |
                +-> StateStore <- +-> Plan / Evidence / Operation / Artifact
                +-> TraceRepository -> Inspector / JSON(L) / opt-in OTLP
```

核心不变量：system prompt 在会话内保持字节稳定；tool call/result 原子配对；权限、课程范围、审批、预算在执行层复验；同 session 由 SQLite lease 单飞并以 fencing token 拒绝旧 Worker；写入是“至少一次调度 + 幂等业务键/消费”，不宣称 exactly-once；trace 导出前再次执行 owner scope 与中心脱敏。

技术亮点：16 个教学工具复用本地/MCP `ToolProvider -> ToolResult` 窄边界；每个 run 冻结带 source/version/schema hash/effect/capability 的 `ToolManifest`，模型可见面与 executor 二次鉴权使用同一快照；10 个查询/分析/图谱切片和 5 个教学 command 共用 canonical `TeachingDataProvider`，替换契约 fake 不改 Agent 图或 Manifest；`run_code` 独立依赖 `CodeExecutionProvider`。PlanGraph 只接受真实工具事件、完整 Artifact 或验真的 citation；教学写入的业务变更、operation 和 outbox 在同一教学库事务提交；Trace Repository 只投影已有运行状态。

数据红线：仓库只使用固定 seed 的合成教学数据和公开材料；真实学生数据、业务代码、API key、cookie、审批秘密不进入仓库、trace 或 Artifact preview。代码执行默认关闭，只有同一真实后端完成健康、能力与 E2E attestation 后才暴露 `run_code`。

唯一完整验收入口如下。机器只需预先安装 `uv`；脚本会先按 `.python-version` 和 `uv.lock` 幂等准备
Python/依赖（首次运行可能下载），再以离线模式执行 Stage 8、前序回归和一次全量测试：

```bash
zsh scripts/accept_stage8.sh
```

该命令执行 Train/Dev/Test lineage 泄漏门禁、数据边界审计、专项/全量测试、10k Trace 基准、核心 Demo
和 Stage 7 回归，并生成 `artifacts/eval-lineage.json` 与离线综合评测。综合报告中的 oracle/mock、真实模型
和真实代码执行后端严格分栏。运行期数据库、缓存和中间报告位于本次私有临时目录，成功或失败都会有界
清理；不会读取或覆盖 `edu_agent/data/edu.db`。Docker 后端不可用时报告保持 `sandbox=not_verified`，
该离线入口不发送模型请求，因此其 system report 的真实模型栏保持 `not_run`。R5.2 独立真实运行报告与
候选 provenance 分开核对；两者都不伪装成另一类证据。架构与边界见 [`docs/architecture.md`](docs/architecture.md)，
现场演示见 [`docs/demo-script.md`](docs/demo-script.md)。

当前 R5.5 二元发布结论为 **ready**：Stage 8 candidate 与固定 R5.2 真实模型 candidate 已在同一 clean
commit `fb1eeb6073694409f0c2c48ef34916f420e9fdab` 上通过 provenance 和数据边界审计。该结论表示
R5 门禁完成，不表示已经发布镜像、部署、创建 release/tag，或验证了仍明确为 `not_verified` 的外部能力。最终审计与
残余风险见
[`docs/release-readiness.md`](docs/release-readiness.md)。

GitHub Actions 使用单一 Ubuntu / Python 3.12 环境，按 `uv.lock` frozen 安装后离线运行 ruff、全量
pytest、lineage 泄漏门禁、综合评测、10k Trace 和敏感数据审计。workflow 显式清空模型/平台凭据，不使用预建 `.venv`、
本机数据库或 Docker；上传证据前要求 candidate provenance 和 artifact 边界审计通过。

## 这是什么 / 为什么

工具的入参与语义按教学平台常见的考试、班级、题库、知识图谱和代码执行边界抽象；仓库只保留规范化工具契约和合成数据，不包含私有平台源码、DDL 或数据。私有 `TeachingPlatformProvider` 尚未实现。

## 工具集（16 个，五类，规范化教学契约）

| 类别 | 工具 | 规范化端点语义 |
|---|---|---|
| 查询 | `query_student_scores` 查成绩 | `GET /teacher/v1/exams/{examId}/results` |
| | `list_exams` 列考试 | `GET /teacher/v1/exams` |
| | `get_class_roster` 班级名单 | `GET /teacher/v1/classes/{classId}/students` |
| | `search_questions` 搜题 | `GET /teacher/v1/questions` |
| | `get_learning_progress` 学习进度 | `GET /student/v1/learning-progress/...` |
| 知识图谱 | `query_knowledge_graph` 图谱查询 | Neo4j `:KnowledgePoint` + 先修/相关/相似 关系 |
| 分析 | `analyze_class_errors` 班级错题Top | `GET /teacher/v1/grading/error-analysis/class/{classId}/top` |
| | `diagnose_weak_points` 薄弱诊断 | `.../student/{studentId}/weak-points` |
| | `get_score_distribution` 成绩分布 | `GET /teacher/v1/exams/{examId}/score-statistics` |
| 操作 | `create_exam` 建考试 | `POST /teacher/v1/exams` |
| | `generate_paper` 组卷 | `POST /paper-generation/auto` |
| | `batch_grade` 批量判分 | `POST /teacher/v1/exams/{examId}/batch-grade` |
| | `assign_homework` 布置作业 | `POST /teacher/v1/homeworks` |
| AI·执行 | `generate_questions` AI出题 | `POST /teacher/v1/ai-questions/generate` |
| | `recommend_study_path` 学习路径 | 知识图谱 shortestPath（cost=Σ(1−weight)） |
| | `run_code` 隔离代码执行 | `POST /coding/execute/{lang}`（Provider 与离线契约已测；当前真实 Docker 状态见当次报告） |
| 条件式知识检索 | `retrieve_course_materials` 版本化课件检索 | 仅知识库启用且存在时暴露；tenant/course 双层 ACL |

## 典型 demo 任务（多工具多步）

> 「三班这次 Python 考试谁不及格、普遍错在哪个知识点、给薄弱的同学各推 3 道练习题」
> → `list_exams` → `query_student_scores` → `analyze_class_errors` → `query_knowledge_graph` → `recommend_study_path` / `search_questions`

## 架构

```
合成数据层                 教学领域防腐层          工具/编排层             模型层
┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
│ SQLite + 图算法    │←──│ SyntheticProvider│←──│ ToolProvider/Agent│──▶│ ProviderGateway   │
│ 固定 seed 可复现   │   │ Query / Command  │   │ Plan / Evidence  │   │ Chat/Responses    │
└──────────────────┘   └──────────────────┘   └──────────────────┘   └──────────────────┘
```

- **教学领域 + 工具层**：`SyntheticProvider` 执行 10 个 canonical query 和 5 个 canonical command，handler 只映射 schema/context 与原 JSON。`create_exam`/`batch_grade`/`assign_homework` 以及保存题库的 `generate_questions` 只能由 executor 签发 `ToolOperation`，Provider 复验同库事务、payload/idempotency/scope 和未过期审批，不自行 commit。纯出题与 `generate_paper` 不建 operation；`run_code` 不经教学库抽象。真实 `TeachingPlatformProvider` 尚未实现，仍属于候选版后的 L1 私有平台接入。
- **引擎层**：`EDU_AGENT_ENGINE` 环境变量切换（离线 mock / 通义千问 / 本地 vLLM / 算法仓 W4A16 端点），同一张图复用。
- **工具来源可切换**：默认本地直调；同一批工具也可暴露为 **MCP server**（stdio 传输），Agent 经 **MCP 协议**往返调用（`MCPToolProvider` 与本地 registry 同契约，图 / 引擎均无需改动）。MCP/entry-point plugin 的 discovery 必须通过 source/version/schema hash/effect/capability 信任目录；缺失、冲突、名称抢占或 schema 漂移 fail closed。每次远端调用仍在本地做参数规范化、ACL/course scope、超时/取消和结果预算检查。见 `edu_agent/mcp/` 与 `scripts/mcp_demo.py`。
- **冻结 Manifest 与安全并发**：run/session 绑定 Manifest 后，插件热变更、MCP 重连或断线迟到结果不能扩大执行面；恢复 hash 不匹配返回明确错误。连续的、已验证的只读调用才可进入有界 worker segment，写入、审批、代码、未知 effect、插件/MCP 未明确 opt-in 或资源冲突调用保持 barrier，结果仍按原 call 顺序提交。
- **可靠 Agent Runtime**：`EduAgentService` 统一管理身份/租户、模型与工具预算、短期会话、
  FTS5 长期记忆、上下文窗口、角色工具面、写操作审批、运行轨迹和审计；插件与 MCP 共用
  ToolProvider 契约，SQLite Scheduler 使用租约领取计划任务。详见
  [`docs/production-runtime.md`](docs/production-runtime.md)。
- **API 容器与最小运行手册**：`deploy/api/Dockerfile` 使用锁定依赖构建多阶段、非 root、默认只读
  rootfs 的 API 镜像；`deploy/docker-compose.yml` 只启动 API，状态/Artifact/备份边界显式挂载，Jobe 和
  Docker code execution 默认关闭。迁移 preflight、health/readiness、SIGTERM drain、备份恢复、升级回滚和
  容量/Provider 故障处置见 [`docs/production-deployment.md`](docs/production-deployment.md)。当前无 Docker
  daemon 的环境只记录静态验证，容器 E2E 保持 `not_verified`。
- **PlanGraph + Evidence Verifier**：只为真正复杂的多步教学任务生成严格 DAG；步骤按依赖推进，
  真实 `tool_event`、完整性校验通过的 Artifact 或 citation 才能完成步骤。模型提前回答会被确定性门禁拦截，
  重试或计划预算耗尽后返回 `blocked/budget_exceeded` 与缺失证据；轻量任务不增加 planner 调用。
- **教学 RAG + 引用**：固定 seed 合成课件带 course/chapter/knowledge point/version/section/chunk id；
  SQLite FTS5 提供离线 sparse 基线，可选语义 Provider 失败时记录事件并降级。检索前后都执行
  tenant/course/document ACL，Plan 最终门禁复验 citation、作用域及同句主张关系。
- **长会话可靠性链**：`RuntimeManager` 以进程内锁 + SQLite session lease 保证同 session 跨进程
  单飞；heartbeat、单调 fencing token、actor/tenant cancel 和启动恢复阻止旧 worker 污染新 owner。
  可插拔 `ContextEngine` 将旧历史软归档为带 source/summary hash 和 scope provenance 的可恢复 checkpoint，
  system prompt 保持稳定；超大工具结果按单结果/整轮预算写入 tenant/actor/session 隔离的 Artifact，模型只看到
  typed reference 与脱敏 preview，并以 SHA-256 校验完整性。该保证限于
  共享同一 SQLite 文件的本机 Worker，不宣称跨主机或跨区域共识。
- **进程 Lifecycle 与有限 Drain**：`LifecycleController` 在 migration、SQLite 回滚写探测和启用的本地
  Code Execution/MCP Provider 健康后才从 `starting` 进入 `running`。`SIGTERM` 或显式 shutdown 原子进入
  `draining`，readiness 立即失败并拒绝新 chat/Scheduler claim，liveness 在有界收尾期间保持成功。deadline
  到期后统一取消 Provider stream、工具和 Scheduler runner；仍未完成的 run 先写成可恢复 `abandoned` 并保留
  lease/fencing，再执行有界 WAL flush。外部模型短暂故障不参与 readiness，仍由 route breaker/fallback 处理。
- **一致备份、受控恢复与 GC**：SQLite backup API 生成带 schema/hash/安全环境 manifest 的一致快照，Artifact
  按索引逐个复验并打包；restore 只接受新目录或空目录，先校验再 migration 和引用复验。Retention 默认 dry-run，
  `manual_review`、pending operation/outbox、恢复状态、hold 和活跃 checkpoint 都会阻塞；详见
  [`docs/storage-operations.md`](docs/storage-operations.md)。
- **模型与后台任务容错**：模型错误区分连接/超时/429/5xx 与 auth/权限/参数/上下文/输出上限，只重试明确
  瞬态故障；重试遵守有上限的 `Retry-After` 并使用 full jitter，并发和 breaker 按冻结 route 隔离，
  half-open 只放行一个探测。fallback 只接管策略允许的瞬态故障，并在发送前复验目标 API mode、tool calling、
  strict structured output 和已知上下文上限；401/403/普通 400/context overflow/output cap 保留原错，不盲切模型。
  turn 起点冻结候选 route，切换原因、胜出 attempt 和数值 usage 脱敏后关联 run_id 落库。Scheduler 支持幂等键、
  自动续租、指数退避、取消和 dead-letter 状态。
- **事务写工具**：`create_exam`、`assign_homework`、`batch_grade` 与
  `generate_questions(save_to_bank)` 进入持久 `ToolOperation` 状态机；业务写入、`committed` 和
  outbox 在同一教学库事务中提交。Scheduler 重试复用稳定写入键，outbox 为至少一次投递并由消费者
  按 event id 去重；可逆操作保存前置快照，不安全补偿进入 `manual_review`。
- **真隔离代码执行**：窄 Provider 接口支持 Jobe HTTP 与受限 Docker Engine；只有健康、能力完整、
  真实 E2E attested 的后端才暴露 `run_code`。固定 digest、禁网、无挂载、只读 rootfs、非 root、
  cgroup/rlimit、输出 Artifact 预算与运行中取消均在执行层复验。威胁模型和部署边界见
  [`docs/code-execution.md`](docs/code-execution.md)。
- **产品优化路线**：基于 Hermes 当前源码选择性补齐 Provider/API Mode、真流式与中断、
  增量 loop journal、安全工具并发、上下文恢复和全树预算；Memory/Skill、后台复盘与
  大型公开数据集后置，同时保留真实教学平台和私有数据边界，见
  [`docs/product-optimization-roadmap.md`](docs/product-optimization-roadmap.md)。按多个独立会话实施时，使用
  [`docs/optimization-implementation-prompts.md`](docs/optimization-implementation-prompts.md) 中的 33 段提示词。
- **统一 Trace 与薄 HTTP API**：`RuntimeEvent v1` 把现有状态表投影成稳定时间线；CLI 支持筛选、
  summary、plan/subagent tree 和流式 JSON/JSONL。标准库 HTTP 层只调用 `EduAgentService`，本地
  Demo auth、actor/tenant/role 复验、持久 request id 幂等、结构化错误与 SSE 断流取消均有专项测试。
  `RunEvent v2` 已定义进程内 typed transport、单调 sequence、writer fence 和有界慢消费者隔离；持久
  `RunJournal` 已提供幂等 migration、严格 CAS 和恢复 snapshot；Agent Loop 会在执行工具前原子提交唯一
  assistant tool-call envelope，并在每个工具返回、超时、取消或结构化拒绝后立即提交配对 result。持久
  `TurnFinalizer` 统一关闭未配对 call、复验 Plan/Evidence、提交唯一最终 assistant、结算 usage/budget、标记
  terminal 并执行有界后处理。Provider Gateway 已将 Chat Completions 与 Responses 的真实 SDK 流归一为
  text/tool/usage/completed/error 事件；同步 `chat()` 聚合相同事件流，首个 delta 后的失败不会重试或拼接
  fallback 输出。HTTP SSE 通过单个、绑定 API attempt 与 session lease fencing token 的 `RunStreamWriter`
  直接消费 Provider/Agent 事件，输出 accepted、text/tool/plan/usage 与 completed/error；keepalive 只在空闲时
  保活。断流、显式 cancel 与 deadline 共用 `CancellationToken`，并贯穿模型、工具、委派和 sandbox；不支持
  强杀的同步调用返回后仍必须通过 token/fence 才能提交。R2 恢复 planner 只从持久稳定 cursor 选择
  `continue/replay-read/reuse-operation/manual-review/terminal-replay`；进程重开会复验冻结 route/manifest、恢复
  原预算，并让旧 writer 的每次 publish 重新经过持久 fence。模型返回、envelope、只读 result、写 commit 和
  final message 五个窗口均用关闭旧 Service、重开同一 SQLite 的 fixture 验证。EventBus 仍不保存历史 delta；工具
  只对显式 `parallel_safe` 的无副作用只读 segment 使用有界并发，写/审批/代码和未知调用仍是 barrier，模型结果与
  journal 按原顺序提交。OTLP 默认关闭；只有安装
  `otel` extra 并显式配置 endpoint 后才尝试导出，失败不击穿主路径。

HTTP 进程探针为无需认证的 `GET /health/live` 和 `GET /health/ready`；响应只含 lifecycle、必要检查布尔值和
活动工作数量等安全聚合字段，不返回 endpoint、key 或 Provider 错误原文。

## 目录结构

```
edu-agent/
├── README.md
├── LICENSE                       Apache-2.0
├── pyproject.toml / uv.lock      依赖（langgraph / openai / pytest / ruff）
├── deploy/
│   ├── api/                      Dockerfile、entrypoint、无凭据容器配置样例
│   └── docker-compose.yml        仅 API 的本机部署（Jobe 单独可选）
├── docs/
│   ├── architecture.md           系统边界、状态机与故障恢复
│   ├── production-runtime.md     当前可靠 Runtime 实现
│   ├── production-deployment.md  API 容器、Compose 与最小生产运行手册
│   ├── eval.md                   agentic 评测方法学
│   ├── product-optimization-roadmap.md  Runtime、Provider、真实平台与受控演进路线
│   └── optimization-implementation-prompts.md  R0-R5 分会话实施提示词
├── edu_agent/
│   ├── data/                     合成数据层（零依赖）
│   │   ├── schema.sql            教学库表结构
│   │   ├── generate.py           固定种子、字节级可复现地生成合成库
│   │   ├── db.py                 连接 / 查询封装
│   │   └── kg.py                 内存知识图谱（mirror Neo4j 设计 + 纯 stdlib 加权最短路）
│   ├── tools/                    工具层（16 个工具，五类）
│   │   ├── schemas.py            OpenAI function 格式工具定义（入参对照真实 Controller）
│   │   ├── {query,analysis,kg,ops,ai}_tools.py   schema/context 到 canonical Provider 的薄适配
│   │   └── registry.py           dispatch + openai_tools 导出
│   ├── engine/                   可替换工具调用引擎
│   │   ├── base.py               引擎抽象接口
│   │   ├── gateway.py            Provider route、adapter 选择与同步 Engine facade
│   │   ├── chat_completions.py   OpenAI-compatible Chat Completions wire adapter
│   │   ├── responses.py          OpenAI Responses API 同步 wire adapter
│   │   ├── mock.py               离线确定性 mock（不联网、无 key）
│   │   └── openai_compat.py      旧构造参数兼容薄层（请求逻辑已迁入 Gateway adapter）
│   ├── agent/                    LangGraph 编排
│   │   ├── graph.py              ReAct + ready step + 确定性完成门禁
│   │   ├── prompts.py            系统提示（含多步执行纪律）
│   │   └── demo_policy.py        旗舰任务的动态决策策略
│   ├── planning/                 PlanGraph / planner / coordinator / evidence verifier
│   ├── knowledge/                合成课件、FTS5、hybrid fusion、citation verifier
│   ├── runtime/transactions.py   写工具 operation / outbox / 补偿事务
│   ├── mcp/                      MCP 集成（工具经 MCP 协议对外/被调）
│   │   ├── server.py            把 16 工具暴露为 MCP server（stdio；复用 registry，逻辑不重写）
│   │   ├── client.py            MCPToolProvider —— 与 registry 同契约、经 MCP 协议调用
│   │   └── __init__.py          get_tool_provider()（EDU_AGENT_TOOLSOURCE=local/mcp）
│   └── eval/                     引擎无关 agentic 评测
│       ├── tasks*.py             Train/Dev 历史集 + 独立 Test 意图（73 条合成样本）
│       ├── lineage.py            稳定 id / provenance / 模板族 split 泄漏门禁
│       ├── metrics.py            工具选择 F1 / 参数准确率 / 轨迹成功率 / relevance
│       ├── oracle.py             离线确定性回放（验证 harness 本身）
│       └── harness.py            run_eval(tasks, make_engine) 运行器
├── scripts/
│   ├── demo_trajectory.py        纯工具层五工具闭环 demo（零依赖）
│   ├── agent_demo.py             LangGraph 多工具 Agent（离线 mock 引擎）
│   ├── mcp_demo.py               工具经 MCP server + MCP 协议被 Agent 调用
│   ├── eval_demo.py              agentic 评测（oracle / 真引擎）
│   ├── eval_ablation.py          修复 before/after 两档对照（一次出对照）
│   ├── eval_plan_ablation.py     PlanGraph 严格 before/after（oracle/真实端点）
│   ├── plan_runtime_demo.py      早停被拦截、证据绑定、最终完成
│   ├── rag_runtime_demo.py       hybrid 降级、带引用回答、ACL 拒绝
│   ├── eval_retrieval.py         Recall/MRR/nDCG/citation/ACL 离线评测
│   ├── transactional_tools_demo.py  Scheduler 重放、outbox 去重、补偿
│   ├── runtime_recovery_demo.py  跨实例 lease、fencing、取消与恢复
│   ├── r2_recovery_demo.py       稳定 cursor 决策、进程重开与脱敏 Trace
│   ├── code_sandbox_demo.py      Docker/Jobe 真后端资源与逃逸验收
│   ├── container_preflight.py    容器启动前迁移、完整性与可写性门禁
│   ├── container_smoke.py        容器静态检查与可选 throw-away Docker smoke
│   ├── eval_subset.py            子集快测（调参用）
│   └── debug_trace.py            打印完整消息序列定位失败轨迹
└── tests/                        工具 / Agent / Plan / Eval / MCP / Runtime / Scheduler
```

> 合成数据库 `edu_agent/data/edu.db` 由 `generate.py` 可复现地生成，已被 `.gitignore` 排除，不入库。

## 快速开始

项目统一使用 **uv + Python 3.12**。先按
[`uv` 官方安装说明](https://docs.astral.sh/uv/getting-started/installation/)安装 `uv`；
`.python-version` 固定 Python 3.12 系列，`.venv/` 由准备脚本在项目根目录创建，依赖版本由 `uv.lock` 锁定；不要使用系统 Python
或手工执行 `pip install`。`pyproject.toml` 中的 `requires-python >= 3.10` 是发行包的
兼容范围，不代表本项目日常开发应改用系统 Python 3.10/3.11。

```bash
# 1. 显式、幂等准备：检查 lock 漂移，按需安装 Python 3.12，再 frozen sync
zsh scripts/prepare_acceptance.sh

# 2. 确认当前项目解释器
uv run --frozen python --version

# 3. 生成合成教学库（可复现，固定种子；产物在 .gitignore 内）
uv run --frozen python -m edu_agent.data.generate

# 4. 运行完整测试
uv run --frozen python -m pytest tests/ -q
```

完整门禁会自动执行同一准备步骤，无需先运行上面的命令。`zsh scripts/accept_stage8.sh` 是唯一公开的完整
验收入口：它串联环境检查、ruff、R1-R4 回归/故障测试、API/Demo、10k Trace、离线评测、lineage 和脱敏数据
边界审计，并在 `artifacts/system-eval.json` 与 `artifacts/evidence-checklist.json` 留下证据。只检查调用图和将执行的命令时可运行
`zsh scripts/accept_stage8.sh --dry-run`；dry-run 不执行 Docker，也不会将其标为已验证。缺少 `uv`、
Python 不兼容或 `uv.lock` 漂移时，准备脚本会在业务测试前停止并给出修复命令。

日常开发不需要手动 `source .venv/bin/activate`，直接使用 `uv run --frozen ...`：

```bash
# 纯工具层五工具闭环 demo（不访问网络）
uv run --frozen python scripts/demo_trajectory.py

# 离线 mock 引擎跑通编排循环（不需 key、不联网）
uv run --frozen python scripts/agent_demo.py

# 可靠运行时纵向演示（记忆 + 压缩 checkpoint + 状态 + 调度）
uv run --frozen python scripts/production_runtime_demo.py

# PlanGraph：创建计划、拦截无证据早停、绑定真实工具事件并完成
uv run --frozen python scripts/plan_runtime_demo.py

# 教学 RAG：hybrid 请求降级 sparse、引用验真、课程 ACL
uv run --frozen python scripts/rag_runtime_demo.py

# 检索评测：stdout 为机器可读 JSON，stderr 为人类可读表格
uv run --frozen python scripts/eval_retrieval.py

# 事务写工具：Scheduler 重放只写一次、outbox 消费去重、作业补偿
uv run --frozen python scripts/transactional_tools_demo.py

# 跨进程运行控制：同 session 争抢、旧 owner fencing、取消与僵尸恢复
uv run --frozen python scripts/runtime_recovery_demo.py

# R2 稳定 cursor：崩溃 Service 重开、只读 replay、terminal replay 与脱敏 Trace
uv run --frozen --offline python scripts/r2_recovery_demo.py

# 隔离代码执行：固定 digest Docker 后端完整逃逸/资源/取消验收
uv run --frozen python scripts/code_sandbox_demo.py --provider docker --e2e --require-all

# 工具经 MCP 协议被 Agent 调用（起 MCP server 子进程，stdio 传输；同样不需 key、不联网）
uv run --frozen python scripts/mcp_demo.py

# agentic 评测：两次生成完整 corpus preflight 后只消费独立 Test
uv run --frozen --offline python scripts/eval_demo.py --engine oracle --repeats 2 \
  --output artifacts/oracle-harness-eval.json
uv run --frozen python scripts/eval_demo.py --engine openai --repeats 3 \
  --output artifacts/real-model-eval.json

# 历史 Train/Dev PlanGraph 诊断，不是独立 Test 证据
uv run --frozen python scripts/eval_plan_ablation.py --engine oracle
uv run --frozen python scripts/eval_plan_ablation.py --engine openai

# 切换到真实引擎（通义千问 / 本地 vLLM / 算法仓 W4A16 Qwen3-14B）：
export EDU_AGENT_ENGINE=openai
export EDU_AGENT_BASE_URL=...   # 如 https://dashscope.aliyuncs.com/compatible-mode/v1 或 http://127.0.0.1:8000/v1
export EDU_AGENT_API_KEY=...    # vLLM 本地可填占位
export EDU_AGENT_MODEL=...      # 如 qwen-plus / Qwen/Qwen3-14B
# 同一张 LangGraph 图无需改代码：edu_agent.engine.get_engine() → run_agent(task, engine)
```

## agentic 评测（口径对齐 BFCL V4）

自建一套**引擎无关**的多工具评测（`edu_agent/eval/`，方法学见 [`docs/eval.md`](docs/eval.md)）。
73 条合成样本在模板族定义阶段分为 Train 55 / Dev 12 / Test 6；Test 使用独立 seed、实体分布和新意图族，
不是随机行切分。门禁检查稳定 id、来源/版本、跨 split 重复、模板族/等价语义重叠、敏感字段和两次生成
一致性。指标包括轨迹成功率、工具选择 F1、参数准确率和 relevance 判对率。

离线用确定性 oracle 回放期望轨迹**验证框架本身**（任务加载 / 工具执行回灌 / 指标计算正确
且能区分对错，见 `tests/test_eval.py`）；**真实模型能力须接真引擎后用同一 `run_eval` 跑出。**

历史 PlanGraph 消融还报告步骤完成率、提前结束率和平均模型/工具调用数，但只消费 Train/Dev。当前 R5.2
candidate 真实运行数字以本地 `ci-artifacts/r52-real-model-eval.json` 为准：它包含独立 Test lineage、配置
hash、三次重复和脱敏 raw records，并与 Stage 8 candidate 绑定同一 clean commit
`fb1eeb6073694409f0c2c48ef34916f420e9fdab`。仓库内 `artifacts/r52-real-model-eval.json` 仅保留旧
`development/dirty` 运行记录。oracle 仍只验证 harness；Stage 8 离线报告的真实模型栏保持 `not_run`。

## 与算法仓的连接

- **算法仓** [`function-calling-sft`](https://github.com/zhangxiaobina/function-calling-sft)：把 Qwen3-14B 微调成更强的工具调用引擎，BFCL V4 出 before/after，再 W4A16 量化 + vLLM 部署。
- **本仓 (应用仓)**：把那个量化模型当工具调用大脑，搭成撑得住的多工具 Agent。
- 两仓互相印证：一层证明"会微调/评测/压缩部署一个工具调用模型"，一层证明"会把它搭成真实场景的多工具应用"。

## 历史实验记录（定性 · 非当前门禁证据）

> 下述观察来自 lineage 建立前的 seed-42 19-task 实验，不能作为当前候选版证据。
> 它们只保留为研究背景；R5.2 独立 Test 报告是另一份限定范围的 evidence，不能反向证明这些历史实验。

**① 历史三档 agentic 对照（base / fp16 / W4A16，同机同套 19 个 Train/Dev 任务）**

当时观察到：

- **base（未微调）多步推理本就强**，旗舰多步任务完成度最高；
- **窄域单轮 FC-SFT 提升了工具选择与 relevance 判断，却以多步链式推理为代价**——多步任务成功率明显下降；
- **再叠加 W4A16 量化**，长链路上误差累积进一步放大。

抓失败轨迹定位根因：SFT 把 `<think>` 思考链压成空块、模型在第二跳**直接编造结果**（成绩分布 / 学习路径 / 题号）
而不调用对应工具（「拿到部分结果就早停 + 工具调用幻觉」），量化再放大长链误差。
这也**修正了「量化零损失」的边界**：在 BFCL 单轮 AST 口径成立，多步 agentic 任务上有额外损伤。

**② 历史实验：强制中间反思兜底在部署档 W4A16 上的 before → after**

针对上面的根因，在 LangGraph 编排层加：强化的多步执行纪律提示（A）+ 早停时注入一次
**反幻觉自检兜底**、逼模型核对「每条数据是否来自工具真实返回、否则重新调工具」（B）+ 反螺旋调用上限；
兜底触发门槛设为「**已调过工具才生效**」，从而**零干扰** irrelevance（寒暄 / 越域）任务。

同端点 before→after（2 次完整跑一致）的定性结果：

- **轨迹成功率明显提升**、**多步任务完成数提升**、**relevance 判对率提升到满分**；
- **如实记录代价**：反幻觉兜底引入额外 / 重复调用，**工具调用精确率明显下降**（召回反升）。

> **诚实边界**：编排兜底**只能缓解、不能根治**——它救得回「该继续却早停」，救不了模型层面的
> 选错工具 / 不敢调写操作 / 长链路自信编造。残余几个未达成任务（旗舰多步链、组卷→建考、
> 学生诊断→路径、某 relevance 任务过度搜题）即属此类，根治需更强 SFT 数据（多跳 FC）或
> 现已实现 PlanGraph 显式分解与确定性证据门禁；个别多步任务受 vLLM 非完全确定性影响在边界处
> 轻微波动，新的 PlanGraph 真实端点严格消融仍未运行。

> 这组结果保留为历史研究结论。当前生产运行时已移除中途伪造 `user` 消息的 reflect 节点，
> 因为它污染会话角色语义并妨碍稳定上下文。现行实现使用稳定系统提示、结构化工具错误、
> 调用预算和安全执行器；`scripts/eval_ablation.py` 现在只比较旧/新系统提示词。

**③ 从模型层根治：DPO 偏好对齐（脚手架）**

历史编排兜底不是根治方案；要从模型层改进，可把本仓多步任务的轨迹按档位 dump 出来，
配成「真实逐跳调用的成功轨迹（chosen） vs 编造中间结果的失败轨迹（rejected）」偏好对，
在算法仓做 DPO。`scripts/dump_trajectories.py` 即用于 dump 某模型档的**原生**多步轨迹
（当前循环不再注入 synthetic-user reflect，直接记录模型层行为）：

```bash
# 接某档 vLLM 端点，dump 多步任务原生轨迹 → dpo_dumps/traj_<tag>.jsonl（不入库）
EDU_AGENT_ENGINE=openai EDU_AGENT_BASE_URL=http://127.0.0.1:8000/v1 EDU_AGENT_API_KEY=dummy \
EDU_AGENT_MODEL=<model> uv run --frozen python scripts/dump_trajectories.py --tag base
```

偏好对配对 / 校验 / DPO 训练在算法仓
[`function-calling-sft`](https://github.com/zhangxiaobina/function-calling-sft)（见其 `docs/dpo.md`）。
仓库只提供脚手架，不分发轨迹 / 偏好数据；数据够了即可照此复现。

## License

Apache-2.0。合成数据生成逻辑与工具 schema 为原创；语义对照的真实平台代码**不包含**在本仓库内。
