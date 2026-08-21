# EduAgent 面试讲解指南

## 3 分钟介绍

第一分钟讲问题：教学 Agent 的难点不是接模型，而是长链早停、写操作重放、跨租户检索、并发会话和
故障复盘。EduAgent 用 16 个教学工具与固定 seed 合成数据重建真实 LMS 工作流，但把真实学生数据和
凭据设为红线。

第二分钟讲主线：`EduAgentService` 是唯一入口；复杂任务由 PlanGraph 分解，步骤只有绑定真实工具事件、
完整 Artifact 或验真 citation 才能完成。写工具进入 operation/outbox 状态机；Scheduler 至少一次执行
依靠稳定业务键和消费去重。共享 SQLite 文件上的 session lease 与单调 fencing token 阻止旧 Worker
提交，不把它讲成跨主机共识。

第三分钟讲可验证性：Trace Repository 不重写运行时，而是把 runs、plan/evidence、provider、operation、
scheduler、subagent、sandbox 等现有状态投影成 `RuntimeEvent v1`。CLI/API 在 owner scope 和二次脱敏后
分页导出。73 条合成评测样本先按模板族分成 Train/Dev/Test 并通过泄漏/确定性门禁；综合评测把
oracle/mock、真实模型和真实代码执行后端分栏，失败保留带 config hash 与 repeat id 的脱敏轨迹。

## 五个最难问题与源码证据

### 1. 模型提前回答时，怎样确定任务真的完成？

`edu_agent/planning/models.py` 校验 DAG；`planning/runtime.py` 推进 ready step；
`planning/verifier.py` 只接受真实事件/Artifact/citation；`agent/graph.py` 在最终回答前执行全局完成门。
代价是复杂任务多一次 planner 调用，简单和 irrelevance 任务通过复杂度门绕过。

### 2. Scheduler 重试为何不会重复建考试？

`runtime/transactions.py` 用稳定 idempotency key + payload hash 绑定 operation；教学业务写、committed 与
outbox 同库事务提交。Outbox 可以重复投递，消费者按 event id 去重。保证是至少一次 + 幂等，不是
exactly-once；跨外部 LMS 时仍需适配器提供同等键与状态查询。

### 3. 两个进程同时处理一个 session，谁能写？

`state/store.py::acquire_session_lease` 在 `BEGIN IMMEDIATE` 中领取并递增 fencing token；
`RuntimeManager` 心跳续租；所有关键提交调用 `_assert_fence`。旧 Worker 即使从阻塞调用恢复，也会在提交
边界被拒绝。范围只到共享同一 SQLite 文件的本机 Worker。

### 4. Trace 怎样统一多张表且保持分页有界？

同库 trigger 在业务事务里追加可重建 `trace_event_index`，源表仍是唯一业务真相。`observability/trace.py`
以 `timestamp/source priority/fencing sequence/event id` 做 keyset 查询；cursor 绑定 owner/filter/snapshot
并防篡改。每页只取 `page_size+1` 行，JSON/JSONL 再逐页输出。网络分块只是传输方式；内部有界查询由
专项 benchmark 的 rows-loaded、SQL queries 和 tracemalloc 证据单独证明。

### 5. 如何证明没有越权或泄密？

`api.py` 的 Principal 只来自 Authenticator；Service/Store 读取按 actor+tenant 复验，角色控制 Artifact
正文和 schedule。密钥类自由文本在主要持久化入口先脱敏，`observability/redaction.py` 导出时再处理
密钥和学生直接标识；授权教学业务库仍保留教学所需的学生原始数据。`tests/test_observability_api.py`
用 canary 扫 SQLite、JSON(L)、日志和 Artifact。自由文本 PII 仍需部署方 DLP，不能从有限正则推导
“绝不泄漏”。

## 从 Hermes 借鉴与没有复制的部分

借鉴了 monitoring event envelope、trajectory 导出、中心脱敏、context breakdown/usage pricing 的“口径
必须可解释”，以及 session lifecycle 中 crash/restart 状态要显式呈现的原则。基于
Hermes 当前源码的后续选择性迁移路线见
[`product-optimization-roadmap.md`](product-optimization-roadmap.md)。

没有复制 Hermes 的通用终端/浏览器/媒体工具全集、消息平台 UI、自动把对话写长期记忆、默认外发遥测，
也没有复制一份并行的 Agent Runtime。原因是教育域的权限、citation、教学写操作和学生数据边界更重要，
扩大永久工具面会增加攻击面与 Schema 负担。

## Trade-off

| 主题 | 当前选择 | 获得 | 代价 |
|---|---|---|---|
| 一致性 | 单 SQLite lease + fencing | 本机跨进程可测、迁移简单 | 无跨区域共识，写吞吐受 SQLite 限制 |
| 幂等 | payload hash + 预分配 run + request lease | 首次/重放共用落库响应，四崩溃窗可对账 | uncertain 写需人工处理；不是 exactly-once |
| 缓存/上下文 | 稳定 system prompt、checkpoint 压缩 | 缓存友好、历史可恢复 | 确定性摘要有信息损失 |
| 安全 | 执行层复验、默认关闭代码执行/OTLP | Prompt 绕过不能扩大能力 | 配置和适配器代码更复杂 |
| 评测 | oracle/mock/real 分栏 | 不把框架满分冒充模型能力 | 没有真实端点时无法给模型结论 |

## 仍未解决的边界

- SQLite lease 不解决多主数据库、网络分区或跨区域共识。
- 协作取消不能强杀任意阻塞的第三方 SDK；只能在返回后拒绝过期提交。
- 真实语义检索、当前真实模型 PlanGraph 消融未在本阶段运行。
- Docker E2E 结论只适用于记录中固定 digest、宿主和配置；Docker socket 仍是管理员信任根。
- Trace 索引为同库可重建投影；更大部署仍需分区、冷存储和索引保留策略。
- API uncertain 写不会自动恢复，需要运维审查 ToolOperation 与外部系统结果。
- Demo auth 只用于本地，不包含生产登录、密钥轮换、组织成员生命周期和审计对接。
- 正则脱敏不能识别所有自由文本 PII；真实数据部署需要分类、DLP、保留和删除策略。

## API Request 恢复口径

- claim 后未建 run：同一 request/payload 领取原预分配 run，不生成第二个 run。
- run 正在执行：若 session lease 仍活跃，返回可重试 in-progress，不抢占或重复执行。
- run 已完成未落 response：从持久 run/messages 重建规范化响应，再提交 response hash。
- response 已落库客户端未收到：按持久 status/content-type/headers/body 重放并标记 replay。
- operation 为 executing/manual_review：request 进入 uncertain，必须人工核对，不盲重放写操作。
