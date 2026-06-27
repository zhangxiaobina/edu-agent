# EduAgent · 全域教学教务多工具 Agent

> 一个 mirror 真实在线教学平台 (LMS) 工具形态的多工具 Agent：用 **LangGraph** 编排 ~15 个教学教务工具（查询 / 知识图谱 / 分析 / 操作 / AI·执行），由一个**可替换的工具调用引擎**驱动。引擎默认接通义千问 / 任意 OpenAI 兼容端点，目标是接入 [function-calling-sft](https://github.com/zhangxiaobina/function-calling-sft) 自微调 + W4A16 量化的 **Qwen3-14B**（vLLM 本地端点）当"大脑"。

## 这是什么 / 为什么

工具的入参与语义**对照一套真实的 Spring Boot 教学平台**（~60 个 Controller、Neo4j 知识图谱设计、AI 出题、Jobe 代码沙箱）抽取而来，因此工具形态贴近生产系统，而非凭空捏造。

**红线（务必遵守）**：本仓库**只用合成 / 公开数据**。真实学生数据、真实业务代码、任何密钥**绝不进入**本仓库。合成教学库（学生 / 班级 / 课程 / 题库 / 考试 / 成绩 / 知识图谱）由脚本**可复现地**生成（固定随机种子），`.gitignore` 已排除生成的数据库文件。

## 工具集（~15 个，五类，mirror 真实 Controller）

| 类别 | 工具 | mirror 的真实端点（语义来源） |
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
| | `run_code` 沙箱跑代码 | `POST /coding/execute/{lang}`（Jobe） |

## 典型 demo 任务（多工具多步）

> 「三班这次 Python 考试谁不及格、普遍错在哪个知识点、给薄弱的同学各推 3 道练习题」
> → `list_exams` → `query_student_scores` → `analyze_class_errors` → `query_knowledge_graph` → `recommend_study_path` / `search_questions`

## 架构

```
合成数据层 (零依赖, stdlib)        工具层               编排层                引擎层 (可替换)
┌──────────────────────┐   ┌──────────────┐   ┌──────────────┐   ┌────────────────────┐
│ SQLite 关系库         │←──│ 15 个工具      │←──│ LangGraph     │──▶│ vLLM W4A16 Qwen3-14B │
│ 内存知识图谱 (Dijkstra)│   │ + OpenAI schema│   │ ReAct/supervisor│   │ 或 通义千问/任意 API   │
└──────────────────────┘   └──────────────┘   └──────────────┘   └────────────────────┘
```

- **数据 + 工具层**：纯标准库，离线可跑、可复现（`sqlite3` + 纯 Python 加权最短路）。
- **引擎层**：`EDU_AGENT_ENGINE` 环境变量切换（离线 mock / 通义千问 / 本地 vLLM / 算法仓 W4A16 端点），同一张图复用。
- **工具来源可切换**：默认本地直调；同一批工具也可暴露为 **MCP server**（stdio 传输），Agent 经 **MCP 协议**往返调用（`MCPToolProvider` 与本地 registry 同契约，图 / 引擎均无需改动）。见 `edu_agent/mcp/` 与 `scripts/mcp_demo.py`。

## 目录结构

```
edu-agent/
├── README.md
├── LICENSE                       Apache-2.0
├── pyproject.toml / uv.lock      依赖（langgraph / openai / pytest / ruff）
├── docs/
│   ├── eval.md                   agentic 评测方法学 + 真实 before/after 结果
│   └── 进度.md                   开发进度日志（构建过程与关键取舍）
├── edu_agent/
│   ├── data/                     合成数据层（零依赖）
│   │   ├── schema.sql            教学库表结构
│   │   ├── generate.py           固定种子、字节级可复现地生成合成库
│   │   ├── db.py                 连接 / 查询封装
│   │   └── kg.py                 内存知识图谱（mirror Neo4j 设计 + 纯 stdlib 加权最短路）
│   ├── tools/                    工具层（~15 个工具，五类）
│   │   ├── schemas.py            OpenAI function 格式工具定义（入参对照真实 Controller）
│   │   ├── {query,analysis,kg,ops,ai}_tools.py   各类工具实现 fn(conn, **params)->dict
│   │   └── registry.py           dispatch + openai_tools 导出
│   ├── engine/                   可替换工具调用引擎
│   │   ├── base.py               引擎抽象接口
│   │   ├── mock.py               离线确定性 mock（不联网、无 key）
│   │   └── openai_compat.py      OpenAI 兼容适配器（通义千问 / 本地 vLLM / W4A16）
│   ├── agent/                    LangGraph 编排
│   │   ├── graph.py              ReAct 循环 + reflect 反幻觉兜底 + should_continue 门槛
│   │   ├── prompts.py            系统提示（含多步执行纪律）
│   │   └── demo_policy.py        旗舰任务的动态决策策略
│   ├── mcp/                      MCP 集成（工具经 MCP 协议对外/被调）
│   │   ├── server.py            把 16 工具暴露为 MCP server（stdio；复用 registry，逻辑不重写）
│   │   ├── client.py            MCPToolProvider —— 与 registry 同契约、经 MCP 协议调用
│   │   └── __init__.py          get_tool_provider()（EDU_AGENT_TOOLSOURCE=local/mcp）
│   └── eval/                     引擎无关 agentic 评测
│       ├── tasks.py              5 类 19 任务（锚定 seed-42 库）
│       ├── metrics.py            工具选择 F1 / 参数准确率 / 轨迹成功率 / relevance
│       ├── oracle.py             离线确定性回放（验证 harness 本身）
│       └── harness.py            run_eval(tasks, make_engine) 运行器
├── scripts/
│   ├── demo_trajectory.py        纯工具层五工具闭环 demo（零依赖）
│   ├── agent_demo.py             LangGraph 多工具 Agent（离线 mock 引擎）
│   ├── mcp_demo.py               工具经 MCP server + MCP 协议被 Agent 调用
│   ├── eval_demo.py              agentic 评测（oracle / 真引擎）
│   ├── eval_ablation.py          修复 before/after 两档对照（一次出对照）
│   ├── eval_subset.py            子集快测（调参用）
│   └── debug_trace.py            打印完整消息序列定位失败轨迹
└── tests/                        test_tools / test_agent / test_eval / test_mcp（32/32 通过）
```

> 合成数据库 `edu_agent/data/edu.db` 由 `generate.py` 可复现地生成，已被 `.gitignore` 排除，不入库。

## 快速开始

```bash
# 1. 生成合成教学库（可复现，固定种子；产物在 .gitignore 内）
python -m edu_agent.data.generate

# 2. 冒烟测试：每个工具都能跑、返回 mirror 形态数据（零依赖，无需联网）
python tests/test_tools.py

# 3. 脚本化多工具轨迹 demo（纯工具层，零依赖）
python scripts/demo_trajectory.py
```

LangGraph Agent 编排（需 venv：`uv sync`）：

```bash
# 离线 mock 引擎跑通编排循环（不需 key、不联网）
uv run python scripts/agent_demo.py
uv run python -m pytest tests/ -q        # 32/32（工具 + Agent 编排 + 评测框架 + MCP 集成）

# 工具经 MCP 协议被 Agent 调用（起 MCP server 子进程，stdio 传输；同样不需 key、不联网）
uv run python scripts/mcp_demo.py

# agentic 评测（离线 oracle 验证框架；接真引擎用同一 harness 出真数）
uv run python scripts/eval_demo.py                  # 离线（无 key）
uv run python scripts/eval_demo.py --engine openai  # 接真引擎，配下方环境变量

# 切换到真实引擎（通义千问 / 本地 vLLM / 算法仓 W4A16 Qwen3-14B）：
export EDU_AGENT_ENGINE=openai
export EDU_AGENT_BASE_URL=...   # 如 https://dashscope.aliyuncs.com/compatible-mode/v1 或 http://127.0.0.1:8000/v1
export EDU_AGENT_API_KEY=...    # vLLM 本地可填占位
export EDU_AGENT_MODEL=...      # 如 qwen-plus / Qwen/Qwen3-14B
# 同一张 LangGraph 图无需改代码：edu_agent.engine.get_engine() → run_agent(task, engine)
```

## agentic 评测（口径对齐 BFCL V4）

自建一套**引擎无关**的多工具评测（`edu_agent/eval/`，方法学见 [`docs/eval.md`](docs/eval.md)）：
5 类共 19 个任务（`single` / `multi_step` / `parallel` / `relevance` / `irrelevance`），全部
锚定 seed-42 可复现合成库。指标：**轨迹成功率**（multi-turn 式整段判定）、**工具选择 F1**、
**参数准确率**（AST 式 possible-answer 匹配）、**relevance 判对率**。

离线用确定性 oracle 回放期望轨迹**验证框架本身**（任务加载 / 工具执行回灌 / 指标计算正确
且能区分对错，见 `tests/test_eval.py`）；**真实模型能力须接真引擎后用同一 `run_eval` 跑出。**

## 与算法仓的连接

- **算法仓** [`function-calling-sft`](https://github.com/zhangxiaobina/function-calling-sft)：把 Qwen3-14B 微调成更强的工具调用引擎，BFCL V4 出 before/after，再 W4A16 量化 + vLLM 部署。
- **本仓 (应用仓)**：把那个量化模型当工具调用大脑，搭成撑得住的多工具 Agent。
- 两仓互相印证：一层证明"会微调/评测/压缩部署一个工具调用模型"，一层证明"会把它搭成真实场景的多工具应用"。

## 主要结论（定性 · 真实跑出 · 可复现）

> 用 `scripts/eval_demo.py --engine openai` 接 **算法仓自微调 + W4A16 量化的 Qwen3-14B**（单卡 vLLM 端点）跑出；
> 19 任务锚定 seed-42 合成库，温度 0。本节只给定性结论，复现后可在本地拿到逐项精确数字（见 `docs/eval.md`）。

**① 三档 agentic 对照（base 未微调 / 微调 fp16 / 微调+W4A16，同机同套 19 任务）**

一个**反直觉但可复现**的结论：

- **base（未微调）多步推理本就强**，旗舰多步任务完成度最高；
- **窄域单轮 FC-SFT 提升了工具选择与 relevance 判断，却以多步链式推理为代价**——多步任务成功率明显下降；
- **再叠加 W4A16 量化**，长链路上误差累积进一步放大。

抓失败轨迹定位根因：SFT 把 `<think>` 思考链压成空块、模型在第二跳**直接编造结果**（成绩分布 / 学习路径 / 题号）
而不调用对应工具（「拿到部分结果就早停 + 工具调用幻觉」），量化再放大长链误差。
这也**修正了「量化零损失」的边界**：在 BFCL 单轮 AST 口径成立，多步 agentic 任务上有额外损伤。

**② 编排层修复（强制中间反思兜底）在部署档 W4A16 上的 before → after**

针对上面的根因，在 LangGraph 编排层加：强化的多步执行纪律提示（A）+ 早停时注入一次
**反幻觉自检兜底**、逼模型核对「每条数据是否来自工具真实返回、否则重新调工具」（B）+ 反螺旋调用上限；
兜底触发门槛设为「**已调过工具才生效**」，从而**零干扰** irrelevance（寒暄 / 越域）任务。

同端点 before→after（2 次完整跑一致）的定性结果：

- **轨迹成功率明显提升**、**多步任务完成数提升**、**relevance 判对率提升到满分**；
- **如实记录代价**：反幻觉兜底引入额外 / 重复调用，**工具调用精确率明显下降**（召回反升）。

> **诚实边界**：编排兜底**只能缓解、不能根治**——它救得回「该继续却早停」，救不了模型层面的
> 选错工具 / 不敢调写操作 / 长链路自信编造。残余几个未达成任务（旗舰多步链、组卷→建考、
> 学生诊断→路径、某 relevance 任务过度搜题）即属此类，根治需更强 SFT 数据（多跳 FC）或
> plan-and-execute 显式分解。个别多步任务受 vLLM 非完全确定性影响在边界处轻微波动。

**③ 从模型层根治：DPO 偏好对齐（脚手架）**

编排兜底是推理层的上限；要从模型层根治，可把本仓多步任务的轨迹按档位 dump 出来，
配成「真实逐跳调用的成功轨迹（chosen） vs 编造中间结果的失败轨迹（rejected）」偏好对，
在算法仓做 DPO。`scripts/dump_trajectories.py` 即用于 dump 某模型档的**原生**多步轨迹
（`--nudges 0` 关闭编排兜底，取模型层真实行为）：

```bash
# 接某档 vLLM 端点，dump 多步任务原生轨迹 → dpo_dumps/traj_<tag>.jsonl（不入库）
EDU_AGENT_ENGINE=openai EDU_AGENT_BASE_URL=http://127.0.0.1:8000/v1 EDU_AGENT_API_KEY=dummy \
EDU_AGENT_MODEL=<model> uv run python scripts/dump_trajectories.py --tag base
```

偏好对配对 / 校验 / DPO 训练在算法仓
[`function-calling-sft`](https://github.com/zhangxiaobina/function-calling-sft)（见其 `docs/dpo.md`）。
仓库只提供脚手架，不分发轨迹 / 偏好数据；数据够了即可照此复现。

## License

Apache-2.0。合成数据生成逻辑与工具 schema 为原创；语义对照的真实平台代码**不包含**在本仓库内。
