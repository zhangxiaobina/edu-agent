# EduAgent 评测框架 · 方法学（口径对齐 BFCL V4）

> 目标：用一套**引擎无关**的 agentic 评测，衡量「把模型搭成多工具 Agent」的真实能力。
> 现在用离线确定性 oracle 验证框架本身；接真引擎（DashScope / 本地 vLLM / 算法仓 W4A16
> Qwen3-14B）后用**同一套 harness** 跑出真数，写入 README 指标区。

代码：`edu_agent/eval/`（`tasks.py` 任务集 · `metrics.py` 指标 · `oracle.py` 离线驱动 ·
`harness.py` 运行器）。跑：`uv run python scripts/eval_demo.py [--engine oracle|openai]`。

## 1. 任务集（5 类，19 个，锚定 seed-42 合成库）

| 类别 | 对照 BFCL | 数量 | 说明 |
|---|---|---|---|
| `single` | simple | 5 | 单工具调用，校验工具选择 + 参数 |
| `multi_step` | multi-turn | 6 | 多步依赖序列（含旗舰任务），整段达成才算成功 |
| `parallel` | parallel | 1 | 一轮内并行发起多次工具调用 |
| `relevance` | live relevance | 3 | 隐含需要查数据 → 该调工具（调哪个合理都算对） |
| `irrelevance` | irrelevance | 4 | 寒暄 / 纯概念 / 越域 → 一个工具都不该调 |

每个任务声明：用户 `query`、期望调用序列 `expected_tools`（含参数匹配器）、轨迹成功判据
`success`、relevance 标签 `should_call_tool`。动态 ID（exam_id、薄弱生 id）在 `build_tasks`
里从 seed-42 库实时解析为具体值，故任务集**对该库自洽、可复现**。

## 2. 指标（`metrics.py`，纯 stdlib）

- **工具选择 F1（召回 / 精确）** ≈ *BFCL AST 的工具名部分*：把期望调用按工具名贪心对齐到
  实际调用。召回 = 命中期望 / 期望数；精确 = 命中 / 实际数（**惩罚多余调用**）。
- **参数准确率 param accuracy** ≈ *BFCL AST 的参数部分*：对已对齐上的调用，只校验期望里
  **声明过**的参数，用 possible-answer（可接受值集合）或 `ANY`（仅需存在）匹配；模型多给的、
  未声明的可选参数不扣分。依赖前序结果的动态参数用 `ANY`，避免因取值合理但不同而误判。
- **轨迹成功率 trajectory success** ≈ *BFCL multi-turn（整段判定）*：必需工具按依赖顺序
  作为**子序列**出现 + 最终回答含关键事实（如有），整段 1/0。这是头条指标。
- **relevance 判对率** ≈ *BFCL relevance/irrelevance*：该调工具时调了、不该调时一个都没调即对。

汇总给总分 + 分类别明细（见 `format_report`）。

## 3. 离线 oracle 的意义与边界

`oracle.py` 按任务声明**确定性回放**期望轨迹（旗舰任务复用 `agent.demo_policy` 的**动态**
决策，证明 harness 也能评动态多步轨迹）。因此：

- **oracle 跑出的指标必然接近满分** —— 它只证明 harness（任务加载、工具执行回灌、指标
  计算）**正确且能区分对错**（见 `tests/test_eval.py`：喂残缺/越界轨迹时相应指标会下降）。
- **不代表任何模型能力。** 真实模型能力须接真引擎后用同一 `run_eval` 跑出。

## 4. 接真引擎出真数

```bash
export EDU_AGENT_ENGINE=openai
export EDU_AGENT_BASE_URL=...   # DashScope / 本地 vLLM / 算法仓 W4A16 端点
export EDU_AGENT_API_KEY=...
export EDU_AGENT_MODEL=...       # qwen-plus / Qwen/Qwen3-14B
uv run python scripts/eval_demo.py --engine openai
```

跑出后把四项指标写入下面第 5 节与 `README.md` 指标区（本仓已用算法仓 W4A16 端点跑出，见下）。

## 5. 结果（定性 · 部署档 W4A16 · 同机同端点）

端点：算法仓自微调 + W4A16 量化的 Qwen3-14B，单卡 vLLM（`--tool-call-parser hermes
--enable-auto-tool-choice`），温度 0；19 任务锚定 seed-42。本节只给定性结论；复现后可在本地拿到逐项精确数字。

**多步早停/幻觉的编排层修复 before→after**（同端点顺序跑，隔离「修复」单一变量；
`scripts/eval_ablation.py` 一次跑两档出对照）的定性结果：

- **轨迹成功率明显提升**、**多步任务完成数提升**、**relevance 判对率提升到满分**、工具召回提升；
- **代价**：反幻觉兜底引入额外 / 重复调用 → **工具调用精确率明显下降**（故工具选择 F1 略降）。

- **根因（抓失败轨迹证实，非框架 bug）**：SFT+量化后模型 `<think>` 为空、几乎不推理，第二跳
  **直接编造**结果（成绩分布 / 学习路径 / 组卷题号）而不调对应工具；连「自检」也跟着编「已完整回答」。
  故温和提示无效，需**强反幻觉兜底**（逼模型核对每条数据是否来自工具真实返回、否则重新调工具）。
- **修复设计**：见 `edu_agent/agent/prompts.py`（A·多步执行纪律）与 `agent/graph.py` 的
  `reflect` 节点 + `should_continue` 三重门槛（B·已调过工具才兜底 / 限强度 / 反螺旋上限）。
  门槛「已调过工具」保证 irrelevance 任务永不被打扰。
- **代价与边界（诚实）**：兜底引入额外/重复调用 → 工具精确率下降；且**只能缓解早停、
  救不了模型层面**的选错工具 / 不敢调写操作 / 长链自信编造（残余失败任务即属此类）。
  个别多步任务为边界任务，受 vLLM 非完全确定性在边界处轻微波动。
- **复现**：`uv run python scripts/eval_ablation.py`（before/after 两档）；
  `uv run python scripts/eval_demo.py --engine openai`（单档完整 19 任务）；
  `uv run python scripts/eval_subset.py --cats multi_step --nudges N`（快速子集调参）。
