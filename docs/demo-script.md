# EduAgent 10 分钟候选版演示

这是一条完整主线，不是多个 demo 的拼盘。默认只使用固定 seed `314` 的合成教学库和本地确定性模型
fixture；不读取私有平台，不请求临时外网数据，也不依赖 Docker。脚本每次重建自己名下的
`r54-state.db`、`r54-teaching.db` 和 `r54-artifacts`，因此重复运行不会累积考试或 operation。

演示前完成依赖同步；正式演示命令均使用锁定依赖和离线模式：

```bash
uv lock --check
uv run --frozen --offline python -m pytest -p no:cacheprovider \
  tests/test_r54_candidate_demo.py -q
```

专项测试比较关键事件快照，不比较最终回答全文。

## 0:00-0:45：范围和证据口径

先说明任务：“读取课程考试与名单，分析成绩后创建考试，并复盘写入证据”。模型 fixture 只决定何时发出
固定工具调用；route、流式传输、参数规范化、并发执行、Plan/Evidence、审批、事务写、journal、恢复、
Trace、上下文和预算都走正式 Runtime。

现场只使用下面四类口径：

| 类别 | 本演示可说内容 |
|---|---|
| 已实现 | Provider route/API mode、RunEvent text delta、只读工具并发、Plan/Evidence、审批幂等写、进程重开恢复、Trace review |
| fixture/离线验证 | seed `314` SyntheticProvider、本地确定性 ProviderAdapter、显式崩溃开关、无网络 smoke |
| 真实模型已验证 | R5.2 固定 DashScope `qwen-plus` Test split 三次重复；这是独立证据，不是本演示实时调用 |
| 尚未验证 | 私有 TeachingPlatformProvider、本机 Docker/Jobe runtime、本演示的 live 模型端点、跨主机 SQLite 共识 |

## 0:45-2:00：正常路径和真实 text delta

```bash
uv run --frozen --offline python scripts/r54_candidate_demo.py \
  --scenario normal \
  --work-dir /tmp/edu-agent-r54-normal \
  --report artifacts/r54-demo-normal.json
```

先看终端中所有 assertion 均为 `true`，再打开
[r54-demo-normal.json](../artifacts/r54-demo-normal.json)：

- `trace_review.route.resolved/selections/winners` 说明 primary 是
  `chat_completions`，fallback candidate 是 `responses`，正常路径 winner 是 primary。
- `stream.text_delta_count > 0` 来自真实 `RunEventBus -> RunStreamWriter -> subscription`；不是把最终文本拆成
  假 delta。
- `stream.sequence_monotonic=true`、`terminal_count=1` 证明单 writer 序列和唯一 terminal。

这里的“真实”指真实运行事件，不指真实外部模型；外部模型证据必须归入 R5.2。

## 2:00-3:30：两个只读工具并发和参数规范化

看同一报告：

- `trace_review.tools.segments` 把 `get_class_roster`、`list_exams` 放在同一个 `parallel` segment。
- `concurrency_proof` 使用两方 `threading.Barrier`，要求两个独立 worker 都进入 Provider 后才放行；
  timeout 只负责 fail closed，没有用 `sleep` 猜时序。
- `trace_review.argument_normalization` 只显示 JSON pointer、原/目标类型和
  `string_to_integer_v1` 规则；不导出原值。`class_id/course_id` 本来就是整数，只有 `page/page_size`
  走允许的无歧义修复。

写工具是 barrier，不与读取或其他写并发。

## 3:30-5:00：Plan/Evidence、审批和幂等写

看以下字段：

- `trace_review.plan_evidence.steps` 是 `inspect -> publish`；两个步骤均为 `completed`，Evidence 状态为
  `accepted`，并绑定真实 tool event。
- `trace_review.approval` 只有一条 `create_exam:approved`。
- `trace_review.writes` 只有一条 committed operation，并显示稳定 idempotency key 和 payload hash。
- `teaching_state` 中 exam、operation、approval 都是 `1`。

脚本使用固定 `replay_scope=r54:seed314:create-exam`。再次执行同一命令会先重建合成库，仍然只产生一个考试，
而不是依赖已有成功数据。

## 5:00-7:00：fallback、崩溃和恢复

故障注入只有显式场景会启用，默认正常路径关闭：

```bash
uv run --frozen --offline python scripts/r54_candidate_demo.py \
  --scenario fault \
  --work-dir /tmp/edu-agent-r54-fault \
  --report artifacts/r54-demo-fault.json
```

按 [r54-demo-fault.json](../artifacts/r54-demo-fault.json) 讲：

- primary 在任何可见 delta 前发生 fixture transport failure；`max_retries=0`，因此
  `retry_decision_reason=retry_limit_exhausted`，没有伪造 retry。
- capability 检查允许 fallback，winner 切到 `responses`，随后仍产生真实 text delta。
- 显式故障点是 `after_write_operation_commit_before_result`：业务考试、ToolOperation 和 outbox 已提交，
  tool result 尚未进入 run journal。
- 测试时钟显式前进 1 秒后，新 Service 领取更高 fencing token；旧 stream writer 的迟到发布被拒绝。
- 恢复 Service 同时创建全新的模型 fixture；它只根据 StateStore 恢复出的 durable tool messages 推导下一阶段，
  不共享崩溃前的进程内 stage 计数器。
- 首个恢复选择为 `reuse-operation`，恢复后的同一次写标记 `idempotent_replay=true`；终态再次恢复选择
  `terminal-replay`。exam、operation、approval 仍各为 `1`。

不要把这讲成任意指令位置都能无损续跑；不确定写仍会进入 `manual-review`。

## 7:00-9:00：Trace、上下文和预算复盘

面试官无需读 SQLite：

```bash
uv run --frozen --offline python scripts/trace_inspector.py \
  --state /tmp/edu-agent-r54-fault/r54-state.db \
  --actor teacher-r54 \
  --tenant school-r54 \
  --run run-r54 \
  --format review
```

`review` 先校验 run 的 actor/tenant scope，再做字段最小化和二次脱敏。它直接回答：

- 选了哪个 route/API mode，哪个 route 最终获胜；
- 每个 `model_call` 内的失败是否 retry、为何不 retry、为何允许或拒绝 fallback；
- 哪些调用属于同一并发 segment；
- 哪些参数按什么规则规范化，且不显示原值；
- Plan/Evidence、审批和稳定写引用；
- checkpoint 在哪里触发、压缩前后估算和回收量；
- `reuse-operation -> terminal-replay` 的恢复边界；
- root 预算的 reservation/usage/finalization。

本 run 没有子 Agent，所以 `child_settlement=not_exercised`，不会伪造 child。专项测试另用真实
`RunBudgetLedger` fixture 断言有 child 时分别输出 `settled` 和 `outstanding`。

## 9:00-10:00：耗时、边界和收尾

两个报告都记录 `timing.elapsed_ms`，只代表这台目标机器的一次观测；不得写成 SLA、吞吐或容量承诺。
最后展示 `event_snapshot`：它只固定 route、事件类型、工具集合、规则、Plan 状态、恢复动作和预算结算，
不固定脆弱回答文本或动态 UUID。

R5.2 真实模型报告
[r52-real-model-eval.json](../artifacts/r52-real-model-eval.json) 只能这样描述：固定
`qwen-plus/chat_completions`、独立 Test 6 条、三次重复、trajectory success `1.0`、tool precision
约 `0.8889`、参数准确率约 `0.6667`；实际账单未知，live run 未注入恢复故障，不能扩展到其他 Provider
或真实学生数据。

## 失败时的讲解路径

- 离线依赖缺失：说明目标机器准备未完成；先在允许的准备阶段执行 frozen sync。不要临时换网络数据或删掉
  `--offline` 后把结果混入正式报告。
- 正常脚本 assertion 失败：保留 traceback 和 `/tmp/edu-agent-r54-normal`，指出缺失的真实事件；不要展示旧成功
  report 冒充本次结果。
- 故障脚本 assertion 失败：正常路径证据仍可单独讲，但恢复结论必须标为失败/尚未验证；不要关闭故障开关后宣称恢复通过。
- 外网或 R5.2 endpoint 不可用：本演示不需要联网。展示已保存报告并明确它的日期、固定 route 和局限，不现场重跑凑结果。
- Docker daemon 不可用：保持 Docker/Jobe 为 `not_verified`。它不阻塞这条 R5.4 合成演示，也不能由静态检查升级为运行验收。
