# EduAgent 10 分钟现场演示

演示前执行 `uv sync --frozen --extra dev --extra mcp`，确认只使用合成数据。所有 Python 命令都通过
`uv run --frozen`。如果真实 Docker 后端未启动，明确跳过该项并展示 `not_verified`，不要用 fake
provider 替代。

## 0:00-1:00：环境与问题

```bash
uv run --frozen python --version
uv run --frozen python -m edu_agent.data.generate
```

讲清主线：模型能调工具不等于运行可靠；演示重点是早停门禁、写入重放、恢复、Trace 和诚实评测。

## 1:00-3:00：正常多步链

```bash
uv run --frozen python scripts/agent_demo.py
uv run --frozen python scripts/plan_runtime_demo.py
```

指出真实 user turn 只有一个，工具调用与结果原子配对；Plan 第一次无证据回答被拦截，真实 tool event
绑定后才完成。

## 3:00-5:00：事务故障与恢复

```bash
uv run --frozen python scripts/transactional_tools_demo.py
uv run --frozen python scripts/runtime_recovery_demo.py
uv run --frozen --offline python scripts/r2_recovery_demo.py
```

展示 Scheduler 在“业务提交后进程失败”场景重试仍只有一个考试；outbox 重投但消费者只产生一次副作用；
再展示 session 争抢、旧 fencing token 被拒、取消和 stale run 恢复；第三条命令关闭崩溃 Service，用同一
SQLite 文件构造新 Service，并从公开 API 输出脱敏 `replay-read -> terminal-replay` 决策与 Trace。强调范围是
单 SQLite 文件，EventBus 不保存历史 delta。

## 5:00-6:30：受限子 Agent 与代码执行边界

```bash
uv run --frozen python scripts/multi_agent_demo.py
uv run --frozen python scripts/code_sandbox_demo.py --provider docker --e2e --require-all
```

子 Agent 只看到任务投影和收窄后的只读工具面。第二条命令必须连接真实后端；失败或无服务就把该项记为
未验证。通过时展示固定 digest、禁网、无挂载、资源上限、逃逸探针、取消与容器清理结果。

## 6:30-8:00：Trace Inspector

先生成包含 run、调度和 checkpoint 的状态：

```bash
EDU_AGENT_PRODUCTION_DEMO_STATE=/tmp/edu_agent_production_demo.db \
  uv run --frozen python scripts/production_runtime_demo.py
uv run --frozen python scripts/trace_inspector.py \
  --state /tmp/edu_agent_production_demo.db \
  --actor teacher-demo --tenant default --format summary --limit 50
```

按输出讲 timeline、budget、latency、plan/subagent tree、Artifact metadata 和 recovery recommendation。
再展示机器导出：

```bash
uv run --frozen python scripts/trace_inspector.py \
  --state /tmp/edu_agent_production_demo.db \
  --actor teacher-demo --tenant default --format jsonl --limit 10
```

说明导出先做 owner scope，再二次脱敏；Inspector 只读。

## 8:00-9:15：API 安全与幂等

另一个终端用本地配置启动：

```bash
EDU_AGENT_DEMO_TOKEN=local-only-demo uv run --frozen python scripts/api_server.py
```

请求使用占位教学内容，不使用真实学生数据：

```bash
curl -sS -H 'Authorization: Bearer local-only-demo' \
  -H 'X-Request-ID: demo-request-1' -H 'Content-Type: application/json' \
  -d '{"message":"列出课程考试"}' http://127.0.0.1:8080/v1/chat
curl -sS -H 'Authorization: Bearer local-only-demo' \
  'http://127.0.0.1:8080/v1/traces?run_id=<run_id>&limit=20'
```

重复第一条 request id 返回相同 run；换 payload 返回冲突。跨 actor/tenant 的拒绝由专项测试展示，避免在
现场放第二组真实凭据。

## 9:15-10:00：综合评测与收尾

先演示 lineage/数据审计、API recovery socket 契约和大 Trace：

```bash
uv run --frozen --offline python scripts/audit_eval_lineage.py \
  --output artifacts/eval-lineage.json
uv run --frozen python scripts/audit_data_boundaries.py \
  --fail-on-findings \
  /tmp/edu_agent_production_demo.db /tmp/edu_agent_production_demo.db-wal \
  /tmp/edu_agent_production_demo.db-shm artifacts
uv run --frozen python -m pytest \
  tests/test_stage8_boundaries_recovery_trace.py -q
uv run --frozen python scripts/benchmark_trace_scaling.py \
  --events 10000 --page-size 100 --output artifacts/trace-scaling.json
```

讲解 73 条样本按模板族分成 Train 55 / Dev 12 / Test 6，Test 使用独立 seed/实体/意图而非随机行切分；
lineage 审计会因跨 split 重复、族/等价语义重叠、缺 provenance、敏感字段或重复生成不一致而失败。数据
审计报告只给分类/位置/计数、不回显秘密；API 测试经过 `127.0.0.1:0` 真 socket，包含 run 完成但
response 未提交后的恢复与首次/重放字节一致；benchmark 证明每页读取不超过 page size+1，峰值内存不随
总历史线性增长。不要把本机一次耗时写成容量承诺。

最后运行完整门禁：

```bash
zsh scripts/accept_stage8.sh
```

该入口会先校验 `uv.lock`、按需准备 `.python-version` 指定的解释器和依赖；业务门禁随后使用离线模式。
运行期数据库和中间报告在本次私有临时目录中，并在成功或失败后清理。现场只检查调用图时可给同一入口增加
`--dry-run`，但 dry-run 不是通过证据。

打开该命令生成的 `artifacts/eval-lineage.json` 和 `artifacts/system-eval.json`，先确认 lineage gate，
再按 Agent、RAG、Reliability、Transaction、Multi-agent、Sandbox、Performance 分栏讲。Agent oracle
只证明独立 Test harness，真实模型为 `not_run`；没有传当次真实代码执行报告时 Sandbox 为 `not_verified`。
最后给出技术债：真实语义/模型评测、跨主机协调、强取消、生产认证、trace 冷存储和自由文本 DLP。

`accept_stage8.sh` 是唯一对外完整门禁；它会显式调用 Stage 7 内部回归边界，再运行一次全量测试。Stage 7
只保留用于独立调试，不作为第二个公开完整入口，也不重复全量测试。
