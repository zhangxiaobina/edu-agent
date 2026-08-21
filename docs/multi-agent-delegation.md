# 受控教学委派

当前实现将复杂的只读教学分析拆成可恢复的父子 Run。委派不是通用的
`spawn_agent` 工具，而是 `TeachingDelegationService` 暴露的三个具体消费者：

- `analyze_classes`：并行分析多个班级或考试；
- `retrieve_chapters`：并行检索多个课程章节并保留 citation；
- `build_intervention`：并行执行成绩分析、薄弱知识诊断和资源检索。

## 运行边界

每个 child 都有独立 session、RunContext、工具面和工具/模型/Token/成本预算。child
只接收 task、明确传入的 evidence/citation 和固定能力说明，不复制父级对话或长期记忆。
父级的 actor、tenant、role、course scope 在 SQLite 中持久绑定；新的 child scope 只能是
父级 scope 的子集。写工具、代码执行和任意递归委派不在该工具面开放。

`delegation_roots` 保存 root 预算预留和聚合 usage，`delegation_runs` 保存 parent/root
lineage、depth、task key、状态、lease、结果 Artifact 及失败/取消原因。相同
`(parent_run_id, task_key)` 恢复时复用已完成 child，不重复消耗预算。Worker 领取采用
SQLite `BEGIN IMMEDIATE` 与 lease，过期 worker 会被标记为 `WORKER_LEASE_EXPIRED`，不会
留下可继续消耗资源的 orphan run。

## 结果与失败策略

`SubtaskResult` 是结构化结果，包含 summary、evidence ids、citations、artifacts、usage
和 warnings。父级 `ParentEvidenceVerifier` 会重新查询 tool event、Artifact 完整性和
citation ACL；子级自述不能充当证据。批次显式选择 `fail_fast`、`best_effort` 或
`required_quorum`，父级取消会传播给 running/queued child，超时会变成 terminal
`timed_out`。

## 适用与不适用

适合彼此独立、只读且有明确 scope 的班级/考试分析、课程章节检索和干预前置诊断。只有
一个 SQL 查询、需要共享完整对话上下文、必须顺序修改业务数据或调度开销超过任务耗时的
工作不值得委派；写操作仍须经过现有的审批、幂等和事务运行时。

## 验证与演示

```bash
uv run --frozen python -m pytest tests/test_multi_agent_delegation.py -q
uv run --frozen python scripts/multi_agent_demo.py
```

专项测试使用项目合成 SQLite 学情库与可控延迟，比较并行/串行耗时，并覆盖 scope、预算、
取消、超时、部分成功、lease 恢复、Artifact/citation ACL 和 task-key 恢复。它不把 mock
模型性能当作线上性能，也不用于证明独立的代码隔离能力。
