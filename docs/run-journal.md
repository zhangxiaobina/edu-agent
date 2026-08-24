# RunJournal、TurnFinalizer 与进程重开恢复合同（R2.2-R4.4）

`RunJournal` 是运行恢复的最小持久游标，不是 Plan、Evidence、ToolOperation、Artifact 或 Trace
的替代品。它只保存这些对象的 ID 引用，以及下一次恢复所需的确定性边界。

进程内 R2.1 `RunEvent` 的七个传输 phase 枚举保持兼容；本文件中的 `RunPhase` 是持久 journal 类型，额外
包含不可逆的 `cancelled`/`failed` 分支。

## 恢复状态表

| 当前 phase | 允许的下一 phase | 恢复含义 |
|---|---|---|
| `accepted` | `planning`, `finalizing`, `cancelled`, `failed` | 请求已接受；setup 失败可直接进入统一收尾 |
| `planning` | `model`, `finalizing`, `cancelled`, `failed` | Plan 由 `plan_id` 引用；失败收尾不再另写终态 |
| `model` | `tools`, `finalizing`, `cancelled`, `failed` | 当前模型 attempt 和冻结 route 已保存；迟到结果必须先过 fence |
| `tools` | `verifying`, `finalizing`, `cancelled`, `failed` | finalizer 先关闭未配对 call；不确定写操作不能盲重放 |
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
- `budget_snapshot_json`：经过有限 JSON 校验的兼容投影；R4.4 后只引用/展示同一 root ledger 的快照，journal
  不重新结算预算；
- `stable_boundary`：最后一个可以安全重开的声明边界；未知或损坏值不能被默认修复；
- `fencing_token`、`writer_id` 和 `schema_version`：写者身份与 schema 版本。

journal 不保存消息正文、Plan/Evidence/Operation/Artifact payload，也不建立 Trace 副本。Trace 仍从
现有业务表投影。

## R2.7 稳定 cursor 决策表

恢复入口先读取 journal、finalizer、工具 call/result 配对和 `ToolOperation` 引用，再产生确定性、脱敏且进入
Trace 的 `RecoveryDecision`。只有下表声明的稳定 cursor 可以自动继续；journal 缺失、损坏、未知 boundary、
冻结 route/manifest 不一致、预算快照不合法或 ledger root identity 不一致均 fail closed 到 `manual-review`。

| stable cursor | 可证明的持久状态 | decision | 恢复行为 |
|---|---|---|---|
| `accepted` | request/run 已创建 | `continue` | 从 planning 继续 |
| `plan_committed` | Plan 引用和冻结输入已提交 | `continue` | 从 model 继续，不重建另一份 Plan |
| `model_attempt_started` | attempt、route、manifest 与 root ledger operation 已冻结；模型完整返回未提交 | `continue` | 使用冻结身份重入 model；同 operation 重放不重复扣减，迟到旧结果先过 fence |
| `assistant_envelope_committed` | envelope 完整，pending call 为只读且无 result | `replay-read` | 只重放该只读调用并提交唯一配对 result |
| `assistant_envelope_committed` | pending 写 call 尚无 operation | `continue` | handler 尚未进入；按原幂等键 prepare |
| `assistant_envelope_committed` | operation 为 `prepared/approved/failed` | `continue` | 只恢复同一个 operation，沿既有事务合同执行 |
| `assistant_envelope_committed` | operation 为 `committed` | `reuse-operation` | 读取持久回执并提交唯一配对 result，不再执行副作用 |
| `assistant_envelope_committed` | operation 为 `executing/compensating/compensated/manual_review` 或未知 | `manual-review` | 禁止自动调用 handler，等待人工对账 |
| `assistant_envelope_committed` | envelope 已无 pending call | `continue` | 进入 verifying/model |
| `tool_result_committed` | 已提交 result 完整配对；仍有 pending call | 与上述 pending call 规则相同 | 已提交 result 不重做，只处理第一个 pending call |
| `tool_result_committed` | 所有 call 均有唯一 result | `continue` | 进入 verifying/model |
| `verification_committed` | Plan/Evidence 复验 cursor 已提交 | `continue` | 进入下一 model attempt 或 finalizer |
| `final_message_committed` | 唯一 final assistant 已提交，finalizer 尚未 terminal | `continue` | 只继续 usage/budget、terminal、hooks 和 cleanup |
| `terminal` | completed finalizer/run 已持久化 | `terminal-replay` | 从 finalizer 重建 `ChatResult`/API response |
| `cancelled` | interrupted finalizer/run 已持久化 | `terminal-replay` | 重建相同失败结果，不回到执行态 |
| `failed` | failed finalizer/run 已持久化 | `terminal-replay` | 重建相同失败结果，不回到执行态 |

`continue` 不表示从头执行：它只进入该 cursor 对应的下一节点。恢复前重新计算当前工具面和 Provider route，
必须与 journal 的冻结 hash/脱敏 route 完全一致；预算从 `RunBudgetLedger` 加载原 identity、limits、used、reserved、
stop reason 和冻结价目，journal snapshot 只做 root 一致性复验，不能按新 Service 配置归零。

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
取消结果；最终 assistant 和 terminal 由下述 R2.4 finalizer 独占。

重入时，已提交 result 不再执行工具；未提交的只读结果可以重放。写调用先查询既有 `ToolOperation`：
`committed` 只复用原回执，`executing/manual_review` 返回不可用且不会调用业务 handler。带显式 replay scope 的
跨 run 幂等 operation 可以被新 call 引用，但 call/result 自身始终在各自 run 内配对。

## R2.4 统一收尾协议

`011_turn_finalizer` 将 SQLite `user_version` 提升到 11。每个 run 最多一行 `turn_finalizers`；主键、
`revision` 与单调 `cursor` 共同控制恢复，固定步骤为：

```text
open -> tools_closed -> plan_verified -> final_message_committed
     -> usage_settled -> terminal -> hooks_done -> cleanup_done
```

`tools_closed` 为所有 pending call 写确定性的配对关闭结果；`plan_verified` 复验持久 Plan/Evidence，验证器不可用
或结果不确定时转为 `manual_review`。只有 `completed` 分支可在
`messages(run_id, idempotency_key='final-assistant:<run_id>')` 提交最终 assistant；随后才将 Provider usage 与
预算快照结算到 run。response usage 仍从已持久的胜出 Provider 事件和异常 usage 恢复；预算则由 root ledger
保留包括失败 retry/fallback 在内的全部 attempt，不因重启归零。
`terminal` 在同一个 SQLite 事务中提交 journal 的 `terminal/cancelled/failed`、
`runs.status/stop_reason` 与 finalizer terminal。稳定原因包括 `completed`、`interrupted`、
`budget_exhausted:<dimension>`、旧兼容 `budget_exceeded`、`model_failed` 和 `manual_review`。

finalizer 在任一步骤后崩溃时，恢复 worker 取得更高 fencing token 后从已提交 cursor 继续；旧 worker 的下一次
写入被 lease fence 拒绝。取消或不确定写若在最终消息提交后获胜，terminal 事务会将该消息标为 inactive，避免
失败分支暴露成功回答。`turn_finalizer_hooks` 的唯一键用于 claim 后处理和 cleanup；钩子失败只写审计，不反转
主 turn。cleanup 有调用时限，超时也只记录审计并完成收尾 cursor。

API request completion 与 session lease release 都要求先观察到 finalizer/run terminal。finalizer cursor 小于
`terminal` 时，request lease 过期和 stalled-run 扫描只标记 `resume_finalizer`，不会伪造 `abandoned` 终态；
API response 尚未提交时先继续未完成的 hooks/cleanup，再从 terminal finalizer 重建原 `ChatResult`；已提交时
继续按原 response hash 重放。

## R4.4 root ledger 恢复边界

幂等 `014_run_budget_ledger` 将 SQLite `user_version` 提升到 14。`run_budget_ledgers` 以 root run 为主键并冻结
session/actor/tenant、limits、pricing version 与具体价目；`run_budget_operations` 以稳定 operation/attempt id 保存
reserve/commit/release 状态和请求指纹。所有变更使用 `BEGIN IMMEDIATE`，并发恢复不能超卖。

child allocation 是 root reservation 的转移，不是新的共享预算。Provider/tool operation commit、child 终态差额
结算和余量释放都可重放；未知 usage 按 R4.1 估算，未知价格保持 unknown。TurnFinalizer 在 `usage_settled` cursor
使用唯一 `budget-finalizer:<root_run_id>` 释放残留预留并冻结墙钟；若在 ledger final 与 cursor 提交之间崩溃，恢复
重复同一 finalizer id 后继续，不会二次结算。

## R2.7 sequence 与进程 fence

`012_r2_recovery` 将 SQLite `user_version` 提升到 12，并在 `runs.stream_event_sequence` 保存传输 sequence
高水位。`RunStreamWriter` 在事件对 socket 可见前通过状态库预留 sequence，每次预留都重新验证 run scope、
当前 session lease 和 fencing token；进程重开时从 stream/journal 两个高水位的最大值继续。因此旧进程即使
仍持有内存 publisher，也会在下一次 publish 时被持久 fence 拒绝。terminal replay 只允许 terminal run 使用
token `0` 生成新的 replay envelope，不恢复历史 delta，也不把 EventBus 变成持久队列。

五个进程级 fault fixture 分别在模型返回后、tool-call envelope 后、只读 result 后、写 operation commit 后和
最终消息后抛出绕过进程内 finalizer 的 `BaseException`，关闭第一个 Service，再用同一 SQLite 文件构造新
Service。验收同时检查 call/result 配对、唯一 final、cursor/sequence 单调、冻结 route/manifest/budget、旧
writer/fence、写副作用唯一和 API request 字节重放。

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
7. **恢复身份冻结**：resume 前复验 tool manifest hash、脱敏 Provider route 与 ledger root identity，并加载持久
   limits/used/reserved/stop reason/price；任一不一致都不得用新进程的默认配置覆盖。
8. **事件高水位持久化**：可见 RunEvent 的 sequence 先落到 run 高水位，journal 提交与恢复 writer 都只能从
   两个高水位的最大值向前推进；旧 token 每次 publish 都重新过状态库 fence。
