# RunJournal 恢复合同（R2.2 schema / R2.3 loop integration）

`RunJournal` 是运行恢复的最小持久游标，不是 Plan、Evidence、ToolOperation、Artifact 或 Trace
的替代品。它只保存这些对象的 ID 引用，以及下一次恢复所需的确定性边界。

进程内 R2.1 `RunEvent` 的七个传输 phase 枚举保持兼容；本文件中的 `RunPhase` 是持久 journal 类型，额外
包含不可逆的 `cancelled`/`failed` 分支。

## 恢复状态表

| 当前 phase | 允许的下一 phase | 恢复含义 |
|---|---|---|
| `accepted` | `planning`, `cancelled`, `failed` | 请求已接受；尚未声明计划或模型工作已完成 |
| `planning` | `model`, `cancelled`, `failed` | Plan 由 `plan_id` 引用；只能从已保存的 loop cursor 继续 |
| `model` | `tools`, `cancelled`, `failed` | 当前模型 attempt 和冻结 route 已保存；迟到结果必须先过 fence |
| `tools` | `verifying`, `cancelled`, `failed` | 工具边界由 tool event / operation ID 引用；不确定写操作不能盲重放 |
| `verifying` | `model`, `finalizing`, `cancelled`, `failed` | Evidence/Artifact 只通过 ID 复验；需要下一轮时回到 `model` 并推进 cursor |
| `finalizing` | `terminal`, `cancelled`, `failed` | 只允许完成已声明的收尾边界；重复收尾必须被 CAS 拒绝 |
| `terminal` | 无 | 成功终态；不可重新进入执行态 |
| `cancelled` | 无 | 取消终态；不可重新进入执行态 |
| `failed` | 无 | 失败终态；不可重新进入执行态 |

主成功路径严格为：

```text
accepted -> planning -> model -> tools -> verifying -> finalizing -> terminal
```

任何执行 phase 都可以明确进入 `cancelled` 或 `failed`，但这两个分支本身是终态，不能再转回主链或
`terminal`。`terminal`、`cancelled`、`failed` 都是不可重入的终态；`runs.status` 的业务状态仍由现有
`runs` 表负责，journal phase 不会取代它。

## 持久字段与边界

每个 `(run_id)` 只有一行 journal。行中保存：

- `loop_cursor`、`model_attempt`、`event_sequence`：非负且只能增加；CAS 必须带上期望值；
- `tool_manifest_hash`、`provider_route_json`：本次 run 冻结的目录摘要（由调用方选择的不可变 hash）和 route 审计形状，不含凭据；
- `context_checkpoint_id`、`plan_id`、`evidence_id`、`operation_id`、`artifact_id`、`last_tool_event_id`：
  只保存现有真相表的引用；
- `budget_snapshot_json`：经过有限 JSON 校验的预算快照；不在 journal 内重新结算预算；
- `stable_boundary`：最后一个可以安全重开的声明边界；未知或损坏值不能被默认修复；
- `fencing_token`、`writer_id` 和 `schema_version`：写者身份与 schema 版本。

journal 不保存消息正文、Plan/Evidence/Operation/Artifact payload，也不建立 Trace 副本。Trace 仍从
现有业务表投影。

## R2.3 工具消息提交协议

`010_agent_tool_messages` 不把消息正文复制到 journal，而是用两个规范化关系把现有 `messages` 表关联到稳定
cursor：

```text
R2.2 及以前：run_agent 完整返回
                    -> service 计算 generated messages
                    -> append_messages([assistant envelope, tool results, final assistant])

R2.3：agent_node -> append_assistant_tool_envelope() -> tools phase
                    -> tool 1 -> append_tool_result(call 1) -> cursor
                    -> tool 2 -> append_tool_result(call 2) -> cursor
                    -> ... -> service -> append_messages([final assistant only])
```

`append_messages()` 在已有 journal 的 run 上拒绝 assistant tool-call/tool result，故 Agent Loop 是这些协议消息的
唯一写入者；service 不再拥有事后批量追加入口，不会在成功返回时重复提交同一 envelope/result。

- `agent_tool_envelopes` 在 `(run_id, model_attempt)` 上唯一，引用 assistant 消息，并冻结 call ids、
  `tool_manifest_hash`、provider route、提交 cursor 和 fencing token；
- `agent_tool_calls` 在 `(run_id, tool_call_id)` 上唯一，保持模型声明的 `call_index`，从 `pending` 单向进入
  `completed`，并引用唯一 result 消息和可选 `ToolOperation`；
- `messages(run_id, idempotency_key)` 的唯一索引阻止同一 envelope/result 重放产生第二条消息。

Agent Loop 的稳定顺序为：模型返回完整 tool calls，原子写入 envelope/calls 并推进到 `tools`，然后才执行第
一个工具；每个工具返回、超时、取消或结构化拒绝后立即原子写入一个 result 并推进 cursor。任何 result 必须
在同一 run/attempt 找到未完成 call，且按 call index 提交；旧 fencing token、孤立 result、重复 call id、跨
run 配对和写 operation 引用不一致均结构化失败。取消会为已经声明且尚未执行的同 envelope calls 写入配对
取消结果，但不会提前实现最终 assistant 或 terminal finalizer。

重入时，已提交 result 不再执行工具；未提交的只读结果可以重放。写调用先查询既有 `ToolOperation`：
`committed` 只复用原回执，`executing/manual_review` 返回不可用且不会调用业务 handler。带显式 replay scope 的
跨 run 幂等 operation 可以被新 call 引用，但 call/result 自身始终在各自 run 内配对。

## 不变量

1. **作用域一致**：创建和每次 CAS 都同时验证 `run_id/session_id/actor_id/tenant_id`，并复验 `runs` 与
   `sessions` 的归属；缺失或不一致时结构化失败，不能用 `default` 或空值猜测。
2. **单写者 fence**：写入 token 必须是当前 lease token，或是已经由当前有效 lease 接管的更高 token；旧
   token、过期 lease 和不同 writer 都拒绝。token `0` 只允许无 lease 的初始 `accepted` 行。
3. **严格 CAS**：更新同时比较期望 phase、loop cursor、event sequence 和当前 fence。并发竞争、重复写和
   旧快照都返回结构化冲突，不会静默变成成功 no-op。
4. **单调性**：三个游标/attempt 均不可回退；一次成功 CAS 至少推进 phase 或一个游标。跳跃 phase、未知
   phase、未知 stable boundary 和终态重入均失败。
5. **可重开性**：migration 以 `CREATE IF NOT EXISTS`/列探测和幂等 marker 执行；进程在 marker 写入前
   中断时，重开会补齐缺失对象。数据库 schema version 高于当前代码时拒绝启动，绝不降级或覆盖。
6. **恢复诚实**：只读 snapshot 解码所有 JSON 和 phase；损坏值、未知 phase、缺失必需字段直接抛出
   `RunJournalCorrupt`，恢复层不得静默选择一个默认游标。
