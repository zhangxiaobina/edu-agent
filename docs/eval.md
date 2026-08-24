# EduAgent 评测框架与证据边界

评测代码位于 `edu_agent/eval/`。当前只使用仓库已有的合成教学数据生成器，没有下载外部大型数据集。
所有可作为结果证据的任务必须先通过 lineage 门禁；oracle/mock 与真实模型结果分栏，不能合并成一个
模型能力分数。

## 1. Train/Dev/Test lineage

完整 corpus 共 73 条，分配发生在任务族定义阶段，不做随机行切分：

| Split | 数量 | 来源与用途 |
|---|---:|---|
| Train | 55 | seed 42；六个历史多步族、48 条 DPO 派生变体及一个直接训练子意图，只用于训练/训练诊断 |
| Dev | 12 | seed 42；历史冻结题中未进入 Train 的族，只用于 prompt、Plan 和阈值迭代 |
| Test | 6 | seed 314、5 个班、每班 3 门课；R0.4 新增意图族，只用于最终模型评测 |

每个 `EvalTask` 都携带 `edu-agent.eval-lineage.v1`：

- `sample_id`：由 source、version、意图模板族和规范化任务声明计算的稳定 SHA-256 id；
- `source` / `version` / `seed` / `generator`：样本来源与生成版本；
- `split` / `intent_template_family` / `semantic_group`：切分与等价语义隔离边界；
- `content_hash` / `query_hash` / `deterministic`：内容、规范化 query 和确定性声明。

六个 DPO 派生模板与历史原题虽然从不同函数加载，但共享意图族，所以全部归 Train。历史 19 题此前
已用于 prompt/Plan 实验，不能在事后改称独立 Test。新的 Test 使用同一合成生成器的独立 seed、实体
分布和意图族，覆盖学习进度、图谱邻接/技能目录、班级规模比较、考试最高分和学生跨考试记录；它不与
Train/Dev 共享模板族或等价语义组。题库筛选和考试列表虽可换筛选条件改写，但与 Dev 意图等价，因此
没有放入 Test。

运行完整门禁：

```bash
uv run --frozen --offline python scripts/audit_eval_lineage.py \
  --output artifacts/eval-lineage.json
```

审计会从两组全新临时数据库各生成一次完整 corpus。以下任一情况都会退出非零：缺 provenance、稳定
id/hash 不一致、跨 split sample/query 重复、模板族重叠、等价语义组重叠、敏感字段、split 缺失或两次
生成 hash 不同。同一入口还会生成两次 R4.3 context fidelity corpus，并对它执行相同的 provenance、族/
等价语义组、scope、敏感字段和确定性门禁；任一子报告失败都会使顶层 gate 失败。输出 manifest 不保存 query
或 case 自由文本，只保存身份、来源、族、split 和 hash。对应反例测试在 `tests/test_eval_lineage.py` 和
`tests/test_context_fidelity.py`。

## 2. Harness 与指标

任务继续覆盖 single、multi-step、parallel、relevance 和 irrelevance。指标口径为：

- 工具选择 F1：期望调用与实际调用按工具名对齐，多余调用降低 precision；
- 参数准确率：只校验任务声明的参数，支持 possible-answer 与 `ANY`；
- 轨迹成功率：必需工具按依赖顺序出现，且最终回答满足关键事实；
- relevance 准确率：该调用时调用、不该调用时不调用；
- 步骤完成率、提前结束率、模型/工具调用数：用于多步与成本诊断。

`run_eval()` 在执行模型前验证所给任务的 lineage；缺失或不合法的 lineage 会抛出
`LineageValidationError`，不会生成看似有效的指标。

## 2.1 Context fidelity（R4.3）

上下文压缩使用独立的确定性 `edu-agent.context-fidelity.v1` corpus，不把几条手写样例当作门禁。
`build_context_fidelity_corpus()` 生成 scope 隔离的 Train/Dev/Test 样本，并为每条样本绑定稳定
`sample_id`、意图族和合成 provenance；`validate_context_fidelity_corpus()` 会拒绝稳定 ID/hash 不一致、
跨 split 内容/族/等价语义组/scope 重叠、缺 provenance、敏感字段、split 缺失或两次生成不一致。默认离线
评测不调用模型：

```bash
uv run --frozen --offline python scripts/eval_context_fidelity.py \
  --output artifacts/context-fidelity.json \
  --thresholds tests/fixtures/context_fidelity_thresholds.json
```

报告分别统计用户关键约束、实体/课程 scope、operation/approval、citation 与 Artifact ref 保真，
以及 scope leak rate、摘要压缩比（`after/before`）、重复触发率和请求 token 估算绝对相对误差。阈值由
测试或评测命令传入 `assert_context_fidelity_thresholds(metrics, thresholds)`，实现不内置“几条例子
通过即合格”的门槛。观测值不是 corpus 预填答案：每条样本实际执行生产确定性摘要器、重启后的
`CheckpointContextEngine` 反抖动策略和 `ContextAccountant` fallback estimator。估算误差的对照值来自仅在
runner 内注册的固定 `utf8-bytes-div3@r4.3.v1` reference counter，安全因子均为 1；它是跨离线环境可复现的
代理口径，不冒充真实 Provider tokenizer 或账单 usage，也不触发 tokenizer/模型下载。CLI 可用
`--thresholds <json>` 对报告执行同一门禁；真实模型结果与该离线协议指标仍分栏保存。

## 3. Oracle/mock 边界

`oracle.py` 按声明轨迹确定性回放，mock 只覆盖故障和运行时协议。因此 oracle 接近满分仅证明任务加载、
工具执行回灌和指标计算能把正确轨迹判对；反例测试证明残缺、越界轨迹会降分。它们不衡量语言理解，
不代表任何真实模型能力。

离线 Test harness：

```bash
uv run --frozen --offline python scripts/eval_demo.py \
  --engine oracle --split test --repeats 2 \
  --output artifacts/oracle-harness-eval.json
```

保存结果时，`evidence.harness.scope` 固定为 `harness_only`，真实模型栏保持 `not_run`。

## 4. 真实模型结果

真实模型只通过已审计的 Test split 出正式结果。运行前脚本会重建完整 73 条 corpus 两次并执行泄漏、敏感
字段和确定性 preflight；然后才连接 OpenAI-compatible 端点：

```bash
export EDU_AGENT_ENGINE=openai
export EDU_AGENT_BASE_URL=...
export EDU_AGENT_API_KEY=...
export EDU_AGENT_MODEL=...
uv run --frozen python scripts/eval_demo.py \
  --engine openai --split test --repeats 3 \
  --output artifacts/real-model-eval.json
```

结果 schema `edu-agent.model-eval.v1` 单列 `evidence.real_model`，并保存真实 Git commit/dirty、配置 hash、
完整 lineage manifest hash、模型名、脱敏 endpoint hash、温度、seed、每次 run id、逐次指标、均值与方差。
失败轨迹写入同目录的 `*.failed-trajectories.jsonl`，每条保留 `config_hash`、`repeat_index`、`run_id` 和
sample lineage；credential/PII 字段被移除，值与私有路径被中心脱敏。candidate/release 模式还会拒绝无
Git、dirty 或状态不可判定的工作区，并强制使用 Test split 和持久化输出；Dev 只能生成 development
诊断，不能被标成正式模型证据。

本仓当前没有执行真实模型请求，`artifacts/system-eval.json` 中真实模型状态应保持 `not_run`。只有上述
请求实际完成且 artifact 通过数据边界审计后，才能报告真实模型指标。

## 5. 离线系统报告

```bash
uv run --frozen --offline python scripts/eval_system.py \
  --output artifacts/system-eval.json
```

综合报告使用 schema `edu-agent.system-eval.v4`，其 Agent 分栏只跑独立 Test oracle，并明确
`evidence_scope=harness_only`、`capability_claim=not_measured`。报告 config hash 绑定 lockfile、任务/lineage
实现和完整 manifest hash；lineage、oracle harness、真实模型、RAG、可靠性、事务、Trace、委派、性能和
Docker sandbox 均为独立状态。Docker 不可用记 `not_verified`，真实模型未运行记 `not_run`，两者都不混入
离线失败。

system/Trace provenance 只从当前源码根的真实 Git 元数据读取，不接受 `GITHUB_SHA` 等环境变量代替。
默认 development 模式如实记录 dirty；`--evidence-mode candidate|release` 会把缺失 commit、dirty 或 Git
状态不可读作为失败。

## 6. 历史诊断入口

以下入口仍可复现旧实验，但它们消费历史 Train/Dev 任务，不是独立 Test，也不应生成候选版结论：

```bash
uv run --frozen python scripts/eval_ablation.py
uv run --frozen python scripts/eval_plan_ablation.py --engine openai
uv run --frozen python scripts/eval_subset.py --split dev
uv run --frozen python scripts/eval_subset.py --split train --cats multi_step
```

历史定性结论是强化多步纪律能缓解早停，但可能增加重复调用；PlanGraph oracle 对照只证明计划路径和
成本统计可运行。它们均不能替代固定真实模型在独立 Test 上的重复评测。
