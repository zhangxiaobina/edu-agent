# Optimization Progress

## Current

- last_completed_prompt: R1.3
- next_prompt: R1.4
- baseline_commit: 8d5d2a15bb107c90dcada53018b65728371c6d88
- stage_gate: in_progress
- stage_gate_reason: R0 gate 保持 passed；R1.3 已接入最小同步 Responses adapter，并通过双 mode 离线
  wire/契约/Trace 脱敏和全量回归，但 Retry-After/jitter、per-route breaker 与 capability fallback 尚待
  R1.4-R1.5，不能提前把 R1 总门禁标为 passed

## Baseline Reproduction

基线采集时间为 `2026-08-21T17:32:04+0800`，工作目录记为
`<repo-root>`。本节记录的是 R0.1 开始时的真实环境；不要用后续会话结果覆盖，后续结果应
追加到 Session Log。

### Repository

- 仓库指令：根目录存在受 Git 跟踪的 `AGENTS.md`。
- Git 元数据：存在 `.git`，仓库根记为 `<repo-root>`；没有执行 `git init`。
- 分支与提交：`main`，HEAD 为
  `8c645099ce27b9a3f00c5ea755ab3108c8f67dad`（`8c64509`，
  `docs: add project collaboration rules`，提交时间 `2026-08-21T17:25:39+08:00`）。
- 上游：`origin/main`；`HEAD...@{upstream}` 为 ahead `0`、behind `0`。`origin` 为
  `https://github.com/zhangxiaobina/edu-agent.git`，仓库本地配置了
  `http.proxy=http://127.0.0.1:7890`。
- 初始工作区：`git status --porcelain=v1` 输出 0 条，已暂存、未暂存和未跟踪改动均为 0。
- 本会话改动：只新增本文件；未修改 Provider、Agent Loop、工具、migration、配置或数据库业务语义。

### Runtime And Dependencies

| 项目 | 实测值 | 结论 |
|---|---|---|
| OS | macOS 26.5.2，Darwin 25.5.0，arm64 | 本机基线，不代表 CI 平台 |
| 系统 `python3` | 3.9.6 | 不满足 `pyproject.toml` 的 `>=3.10`，不可用于项目命令 |
| `.python-version` | `3.12` | 已跟踪，README 将 3.12 定为日常开发版本 |
| `.venv/bin/python` / `uv run` | CPython 3.12.13 | 当前测试解释器可用 |
| SQLite | 3.50.4 | 由当前 CPython 提供 |
| uv | 0.11.16 | Homebrew arm64 构建 |
| pytest | 9.1.1 | 当前 `.venv` 实际版本 |
| ruff | 0.15.18 | 当前 `.venv` 实际版本 |
| `uv.lock` | version 1、revision 3、`requires-python >=3.10`、91 个 package 条目 | 存在且受 Git 跟踪 |

- `pyproject.toml` SHA-256：
  `79f0ecbba840c2c75725fb3fbcba030357fbfa3292fcbfd445467ba898640359`。
- `uv.lock` SHA-256：
  `532efde753958567081d3a4957302e3b2695cd69c0f7fbe97db27c9a42b3a65c`。
- `UV_CACHE_DIR` 未设置时，受限环境中的 uv 因无法访问
  `<user-cache>/uv` 而以 `Operation not permitted` 失败。以下复现命令统一显式使用
  `UV_CACHE_DIR=/tmp/edu-agent-uv-cache`。
- `UV_CACHE_DIR=/tmp/edu-agent-uv-cache uv lock --check`：退出码 0，4 ms 内解析 91 个包。
- `UV_CACHE_DIR=/tmp/edu-agent-uv-cache uv pip check`：退出码 0，检查 62 个已安装包，0 个依赖冲突。
- 当前存在可执行 `.venv/bin/python`，但 `scripts/accept_stage8.sh` 以
  `test -x .venv/bin/python` 将该预置环境作为隐式前提；干净 clone 尚不能直接一条验收命令完成准备。
- 本机只发现忽略的 `dpo_dumps/edu_expanded.db`（5,787,648 bytes，未跟踪）；没有受 Git 跟踪的
  `.db/.sqlite/.sqlite3`。`.gitignore` 同时排除数据库、DPO dump、`.env`、key、venv 和缓存。
- 环境变量名检查未发现 `EDU_AGENT_*`、`OPENAI_*`、`DASHSCOPE_*` 或 `VLLM_*`；这只说明本次进程
  没有匹配名称，不等价于完成秘密扫描。

### Baseline Commands

在仓库根目录复现：

```bash
export UV_CACHE_DIR=/tmp/edu-agent-uv-cache
uv lock --check
uv pip check
uv run --frozen ruff check .
uv run --frozen python -m pytest tests -q
```

结果与失败分类：

| 命令/环境 | 退出码 | 数字结果 | 分类 |
|---|---:|---|---|
| `uv run --frozen ruff check .`，受限环境 | 0 | 0 diagnostics | 通过 |
| `uv run --frozen python -m pytest tests -q`，受限环境 | 1 | 169 collected；165 passed、4 failed；8.04 s | 环境限制：4 项均在绑定 `127.0.0.1:0` 时得到 `PermissionError: [Errno 1] Operation not permitted` |
| 同一全量 pytest 命令，获准的非沙箱环境 | 0 | 169 passed、0 failed；8.21 s | 通过；确认前一次仅为回环端口绑定限制 |

首次失败的 4 项为：

- `tests/test_observability_api.py::test_http_server_serves_openapi_and_authenticated_chat`
- `tests/test_stage8_boundaries_recovery_trace.py::test_real_http_auth_replay_conflict_content_type_and_scope`
- `tests/test_stage8_boundaries_recovery_trace.py::test_real_http_sse_order_disconnect_cancels_and_rejects_late_commit`
- `tests/test_stage8_boundaries_recovery_trace.py::test_real_http_sse_emits_accepted_before_completed`

四项堆栈都止于 `ThreadingHTTPServer.server_bind -> socket.bind`，没有其他代码失败。非沙箱复跑的是
同一条全量命令，不是跳过或改写测试后的替代结果。

Docker CLI 29.4.3 存在，但当前 Docker daemon socket 不存在；`docker version` 和 `docker ps` 均退出
1，因此当前会话的真实 Docker/Jobe sandbox 为未验证环境能力，不是 pytest 失败。本会话按通用协议不运行
阶段收口用的完整 `zsh scripts/accept_stage8.sh`，也没有运行真实模型评测。

### Existing Evaluation Artifacts

`artifacts/system-eval.json` 和 `artifacts/trace-scaling.json` 均受 Git 跟踪，但生成于当前 Git 历史建立
之前；它们在提交 `c62c5da76a96403975897c851dd31b66459555e8` 中被导入，不能反推其生成源码。

`artifacts/system-eval.json`：

- 文件 SHA-256：`8a40eacfcbb9faa3dd7da61ae84803ffc2761bfb8e956194794c21e2a45c37d6`。
- schema `edu-agent.system-eval.v2`，生成时间 `2026-08-18T03:02:15.467244+00:00`，Python 3.12.13，
  模式 `offline_oracle`，seed 42。
- `commit="unavailable"`。当前目录虽已有 Git，这份历史 artifact 仍不可追溯到生成 commit，不能作为
  R0 发布证据；不得用当前 HEAD 回填。
- 顶层 `config_hash` 为 64 位 SHA-256
  `5be2a535691c4c908c0a7895066a139a4b25c81d13ddc492cb31283038250a96`，非空且格式有效。当前
  `scripts/eval_system.py` 的口径只绑定 seed、agent source、sandbox backend 和 Python 版本，并不绑定
  源码 commit、lockfile、任务集或完整 AppConfig，故“有 hash”不等于完整 provenance。
- Agent/RAG/reliability/transaction/API recovery/trace scaling/multi-agent/performance/sandbox 顶层状态均为
  `verified`。Agent 是 19-task 离线 oracle，只证明 harness；不能解释为真实模型能力。
- `performance.real_model.status="not_run"` 且 metrics 为 null；
  `rag.semantic.status="not_enabled"`；
  `rag.hybrid.status="not_verified_without_semantic_provider"` 且 metrics 为 null。三者口径诚实，未被
  汇入“已验证真实能力”。
- Sandbox 是历史 `real_backend_report`，15/15 cases passed，绑定 digest 后端；当前 daemon 不可用且
  artifact 缺 commit，因此不能把该历史结果当成本会话实时 Docker 验证。
- 内嵌 trace scaling 使用另一组配置，hash 为
  `05604cad1ff6cb4e51d35adced2d7f4ffba0cd93864d5e3bc6f387608b66c266`；与独立 10k artifact 的
  hash 不同是配置不同，不应混为同一运行。

`artifacts/trace-scaling.json`：

- 文件 SHA-256：`0f67332ed7b5f2575863dcaeee6ccba6701c64a92a6d384f874d1257e1278fe7`。
- 生成时间 `2026-08-18T03:01:58.008734+00:00`；配置为 seed 42、10,000 events、page size 100，
  config hash 为 `3e1efb906014cd88a1d7b481fe51890e56ddc7121a9e803bfe1296b4409eb50b`。
- 3/3 assertions 为 true；10,000 indexed、10,001 projected/exported、101 pages、最大每页 101 rows。
- `commit="not_available"`，与 system eval 的 `unavailable` 用词还不一致；同样不满足发布 provenance。

现有评测入口为 `scripts/eval_demo.py`、`eval_subset.py`、`eval_ablation.py`、
`eval_plan_ablation.py`、`eval_retrieval.py` 和综合入口 `eval_system.py`。oracle、真实 OpenAI-compatible
engine、RAG 与系统分栏入口彼此独立；当前没有真实模型运行证据。

### Public Acceptance Entry

公开文档口径已一致，无需在 R0.1 修改：

- `README.md` 的“一键离线验收”指向 `zsh scripts/accept_stage8.sh`。
- `docs/architecture.md` 的“完整一键门禁”指向同一命令。
- `docs/demo-script.md` 的最终完整门禁指向同一命令，并明确 Stage 8 是唯一对外完整门禁。
- `scripts/accept_stage8.sh` 末尾调用 `zsh scripts/accept_stage7.sh`；Stage 7 在公开文档中只描述为内部
  回归边界，没有第二个公开完整验收命令。

以上是 R0.1 当时的文档与调用图快照，不代表当时干净 clone 门禁已通过；R0.2 后新增的自动契约测试与
完整验收结果见下方 Session Log 和当前门禁清单。

## R0 Gate Inventory

| R0 门禁/交付 | 状态 | 已有证据 | 缺口 | 建议修改文件（后续会话） | 风险 |
|---|---|---|---|---|---|
| 真实 Git 根与评测 commit provenance | met | 共享 provenance 只读真实 Git；candidate/release 拒绝无 Git、dirty 或状态不可判定；`0561205` clean clone 的 system/Trace 都记录同一真实 commit、`dirty=false`、gate passed | 无 R0 离线缺口 | 保持 provenance 回归与 candidate/release 门禁 | development artifact 仍不能替代 candidate 证据 |
| 干净 clone 到验收一条命令 | met_locally | `0561205` 的新 clone 无 `.venv`、DB、缓存；frozen sync 安装 62 包，运行时 data 源码和 `schema.sql` 可导入；完整 Stage 8 退出 0 | GitHub 托管 runner 尚未实际执行 | 推送后观察首次远程 run | 首次 cache miss 仍依赖包源可用，缓存不是正确性前提 |
| 单一公开完整验收入口 | met_locally | README、architecture、demo-script 只公开 Stage 8；动态测试证明它调用 Stage 7 一次、失败上抛、全量 pytest 只跑一次；clean clone 完整入口退出 0 | 无 R0 离线缺口 | 继续运行 `tests/test_acceptance_scripts.py` | 新增阶段若绕过契约测试可能再次分叉 |
| 固定 Python、lockfile 与依赖兼容 | met_locally_ci_contract | 单一 Ubuntu 24.04/Python 3.12；uv 固定 0.11.16；`uv lock --check` 解析 91 包，frozen sync 安装 62 包，`uv pip check` 0 冲突；三个 action 固定到 40 位 SHA | GitHub 托管执行尚未观察 | 保持 workflow 为 CI 真相源 | 包源/Actions 服务可用性是外部条件 |
| Secret-free CI 与供应链门禁 | met_by_contract | workflow 清空模型/平台凭据，checkout 不持久化 token；offline ruff/pytest/lineage/system eval/Trace/audit 顺序成立；静态契约和本地 clean clone 同序命令通过 | GitHub 托管 job 尚未实际运行 | 推送后观察首次远程 run，不扩 Python/OS 矩阵 | 本地 macOS 不能替代 Ubuntu runner 实测，托管 CI 保持 `not_verified` |
| CI 不依赖 `.venv`、本机 DB、API key 或预生成 `edu.db` | met_locally | workflow 在 sync 前拒绝预建环境和数据库；clean clone 无这些输入；5 个 `edu_agent/data` 运行时源已跟踪并有防忽略回归，生成 DB/缓存仍被排除 | GitHub 托管 clean-room 尚未实测 | 持续运行 clean-checkout 契约 | 未来若新增外部路径，空凭据与离线契约需同步扩展 |
| 评测报告 hash 与未运行口径 | met_locally | clean candidate system v4/Trace v2 config hash 为 `494294e8…`/`1f5ccacf…`；三份 artifact 审计 0 findings；oracle=`harness_only`、real model=`not_run`、sandbox=`not_verified` | 真实模型、Docker 按口径未验证，不是离线 gate 失败 | 真实运行以后独立保存，不回填 oracle 指标 | 未经 candidate gate 的 development JSON 只能作为开发证据 |
| Train/Dev/Test 按模板族隔离的 lineage | met | 73 条样本在族定义时分为 Train 55/Dev 12/Test 6；manifest `163e5d23…`；两次生成 hash `40a0a59d…` 一致；重复、族/语义组重叠、缺 provenance、敏感字段和非确定生成反例均失败 | 自动化不能发现所有未标注的自然语言改写 | 新模板必须先人工归语义组再进入 corpus | 错误 semantic group 标注仍需代码审查发现 |
| R0 总门禁 | passed | 实现提交 `042e229` 与 clean-checkout 修复 `0561205` 可追溯；clean candidate provenance、无泄漏 lineage、CI 契约、ruff、数据审计和唯一 Stage 8 全部通过；全量 196 passed | 托管 CI、Docker、真实模型和 semantic provider 明确未验证，不混入离线失败 | 下一提示词为 R1.1；开始 R1 前先确认远端同步 | 外部环境能力仍须在具备条件时单列验证 |

## Session Log

### R0.1 - 2026-08-21

- changes: 新建本交接账本；记录 Git/环境/artifact/test 基线和 R0 门禁差距；确认 Stage 8 是唯一公开
  完整验收入口。
- migrations/config: 无 migration；无运行时、Provider、Agent Loop、工具、数据库语义或配置改动。
- verification: ruff 0 diagnostics；受限环境 pytest 165 passed/4 loopback-bind failures；获准环境同命令
  169 passed/0 failed；lock check 91 packages；installed dependency check 62 packages/0 conflicts。
- not_verified: 完整 `accept_stage8.sh`、Docker/Jobe 实时 E2E、真实模型、真实 semantic provider、CI、
  clean-clone bootstrap 和 Train/Dev/Test lineage。本项是诚实未运行/未具备，不记为通过。
- residual_risks: 当前 artifacts 缺生成 commit；Stage 8 隐式依赖 `.venv`；无 CI；config hash 覆盖面窄；
  无模板族 split lineage；历史 Docker verified 不能代表当前 daemon 状态。
- next: R0.2，修复干净环境准备与 Stage 8 单一验收入口的可复现性；不得提前建设 CI 或进入 R1。

### R0.2 - 2026-08-21

- changes: 新增 `scripts/prepare_acceptance.sh` 与共享 shell 辅助层；Stage 8 自动校验 `uv`、
  `.python-version`、lock 漂移并执行 frozen sync，随后所有业务命令使用 `uv --offline`；Stage 8 显式调用
  Stage 7 一次并只运行一次全量 pytest，Stage 7 保留专项回归、Demo、Docker 尝试和综合评测；新增脚本
  调用图、失败传播、幂等准备、无预生成库、无真实凭据及清理契约测试；同步 README/architecture/demo。
- migrations/config: 无 migration、运行时、Provider、Agent Loop、工具或数据库语义改动；`pyproject.toml`、
  `uv.lock` 和 `.python-version` 未改。验收强制 mock/local、清空真实 Provider 凭据，合成库、demo 状态、
  uv/ruff/pytest 临时状态和 sandbox 报告均位于 `mktemp -d` 私有目录，成功、失败和信号退出均有界清理；
  只保留既有 `artifacts/system-eval.json` 与 `artifacts/trace-scaling.json` 输出契约。
- verification: 四个 shell 文件 `zsh -n` 通过，全仓 ruff 0 diagnostics；`tests/test_acceptance_scripts.py` 8 passed，覆盖 Stage 8 ->
  Stage 7 动态调用、Stage 7/8 失败码传播和成功/失败/TERM 清理、全量测试唯一调用、缺 uv/Python、lock 漂移、幂等准备、
  无 `.venv`/`edu.db`、凭据清除及 dry-run；真实空 venv 按 lock 安装 62 包并成功导入；完整
  `zsh scripts/accept_stage8.sh` 退出 0，专项 21 passed、Stage 7 observability 11 passed、全量 177 passed，
  数据审计 1 file/0 findings，10k Trace 3/3 assertions true，核心 Demo 与离线综合评测通过。
- not_verified: 本机 Docker daemon/backend 不可用，实时 sandbox 保持 `not_verified`；真实模型为 `not_run`，
  semantic provider 未启用；CI、远程 clean clone、供应链 provenance 和 Train/Dev/Test lineage 未验证。
- residual_risks: 本次报告由未提交工作区生成，虽记录当前 HEAD
  `8c645099ce27b9a3f00c5ea755ab3108c8f67dad`，仍未绑定 dirty 状态、lock/task/config 全量 hash，不能作为
  发布 provenance；首次准备要求预装 `uv` 并可能需要网络下载；R0 总门禁仍为 `not_passed`。
- next: R0.3，建立 secret-free CI、供应链校验和可信评测 provenance；不得提前进入 R0.4 或 R1。

### R0.3 - 2026-08-21

- commit/worktree: 项目真实 HEAD 仍为 `8c645099ce27b9a3f00c5ea755ab3108c8f67dad`，本会话包含
  R0.2 与 R0.3 未提交改动，故受跟踪 artifacts 如实记录 `git.dirty=true`、development gate
  `not_enforced`；未用常量、`GITHUB_SHA` 或其他环境变量替代 commit。
- changes: 新增单一 Ubuntu 24.04/Python 3.12 GitHub Actions job，固定 uv 0.11.16 和三个 action 的
  40 位 SHA；依次执行无预建状态检查、lock/frozen sync/pip check、offline ruff、全量 pytest、candidate
  综合评测、10k Trace、敏感数据审计及审计后 artifact 上传。新增共享 provenance，统一 system v3/Trace
  v2 的 commit/dirty/config hash/seed/model/mode/environment 与 candidate/release 门禁；失败轨迹、报告、
  审计位置和验收命令日志移除凭据、PII 与私有绝对路径。Stage 8 数据审计改为发现即失败。
- migrations/config: 无 migration、Provider Gateway、真实模型调用、Agent Loop、工具或数据库业务语义改动；
  `pyproject.toml`、`uv.lock`、`.python-version` 均未改。新增 `.github/workflows/ci.yml`，并在 `AGENTS.md`
  固化 CI/离线验收长期约定；`ci-artifacts/` 仅为忽略的本地/CI 输出目录。
- verification: `uv lock --check` 解析 91 包；无 `.venv`/数据库的临时 clean Git snapshot 用 uv managed
  CPython 3.12.13 frozen 安装 62 包，`uv pip check` 0 冲突；全仓 ruff 0 diagnostics；shell syntax 通过；
  全量 pytest 在允许回环绑定的同一 clean snapshot 为 187 passed/0 failed（17.78 s）。candidate system
  eval 与 10k Trace 均记录临时真实 HEAD、dirty=false、provenance gate=passed，Trace 3/3 assertions true；
  两份 artifact 数据边界审计 2 files/0 findings。当前项目 artifacts 记录真实项目 HEAD、dirty=true，
  config hash 非空，credential/PII/private_path 审计同为 2 files/0 findings；当前工作区显式空凭据的离线
  全量复跑为 187 passed/0 failed（13.41 s）；临时 snapshot 已清理。
- not_verified: GitHub 托管 Ubuntu job 尚未因本会话未提交/推送而实际运行；Docker/Jobe 仍不可用，sandbox
  保持 `not_verified`；真实模型为 `not_run`，semantic provider 未启用；R0.4 lineage 未执行。本会话按普通
  切片协议未重复运行阶段收口用完整 Stage 8，使用了 workflow 的本地同序等价命令和全量测试。
- residual_risks: 本地 clean snapshot 证明 workflow 命令、candidate 门禁和无秘密输入契约成立，但不能
  冒充 GitHub-hosted Ubuntu 运行；当前受跟踪 JSON 是 dirty development 证据，提交后的 candidate artifact
  才能作为候选证据。R0 总门禁仍因模板族 lineage 缺失而 `not_passed`。
- next: R0.4，建立稳定 sample lineage、按意图模板族隔离 Train/Dev/Test、增加泄漏/缺 provenance/
  非确定生成门禁，并独立收口 R0；不得提前进入 R1。

### R0.4 - 2026-08-21

- commit/evidence: R0.1-R0.4 实现提交为 `042e229e1ec5ac998182f0cde1996627acc1f16f`；提交后
  clean clone 暴露 `.gitignore` 的宽泛 `data/` 误排除了 `edu_agent/data` 运行时源码，修复提交
  `0561205baad715b1c1742c619ff3c755bac7a076` 将 5 个源码/SQL 文件纳入 Git，并增加防回归契约；
  `edu.db` 与缓存继续忽略。所有 candidate 和最终 Stage 8 证据均来自后一个提交的真实 clean clone。
- changes: 新增 `edu-agent.eval-lineage.v1`、完整 corpus builder 和 seed-314 Test builder；每条样本保存 stable
  sample id、source/version/seed/generator、split、intent template family、semantic group、content/query hash
  和 deterministic 声明。历史六个 DPO 派生族与对应原题保守同归 Train；历史实验题只归 Dev；Test 的六个
  新意图在任务声明时归组，未做随机行切分，并排除与 Dev 等价的题库筛选/考试列表改写。`run_eval` 执行前
  强制 lineage，独立审计两次重建 corpus；Stage 8/CI 在结果生成前做 lineage 门禁、生成后做 artifact
  数据边界审计。system eval 升级 v4，仅运行 Test oracle 并标 `harness_only/not_measured`；真实模型 runner
  单列结果、重复 run、config/manifest hash 和脱敏失败轨迹。
- migrations/config: 无数据库 migration，无 Provider Gateway、真实模型请求、Agent Loop、工具业务语义或
  运行时配置改动；`pyproject.toml`、`uv.lock`、`.python-version` 未改。只使用现有合成生成器的 seed 42
  与 314，没有下载外部数据集。CI 继续固定 Ubuntu 24.04/Python 3.12/uv 0.11.16，并增加 lineage artifact。
- verification: 修复后 lineage/eval/CI provenance 专项 18 passed；全仓 ruff 0 diagnostics；主工作区显式
  空凭据、mock/local、offline 全量在允许回环绑定的环境为 196 passed；受限环境的 4 项失败均止于
  `127.0.0.1:0` 的 `PermissionError`，没有混入离线代码失败。clean clone 从无 `.venv`/DB 状态完成 frozen
  安装 62 包且 `uv pip check` 0 冲突；lineage 两次生成均为 73 条（Train 55/Dev 12/Test 6），生成 hash
  均为 `40a0a59d5d909c425c995bbc5d267934911af7047d768594a34e9a27954a7b0b`，manifest hash 为
  `163e5d232270403fb846d61135fae000d5ba3d67705162d7588dbde27a68ab43`。clean candidate system/Trace
  均记录 commit `0561205`、`dirty=false`、provenance gate passed，config hash 分别为 `494294e8…` 和
  `1f5ccacf…`；lineage/system/Trace SHA-256 分别为 `bc21f4ec…`、`90bc660d…`、`7aeabd38…`，数据边界
  审计 3 files/0 findings。完整 `zsh scripts/accept_stage8.sh` 退出 0：Stage 8 专项 30 passed、Stage 7
  observability 11 passed、最终全量 196 passed，10k Trace 3/3 assertions true。
- not_verified: Docker daemon/backend 仍不可用，system report 保持 `sandbox=not_verified`；未发真实模型
  请求，`real_model=not_run`；semantic provider 未启用；GitHub-hosted Ubuntu CI 尚未实际观察。这些
  外部/在线项没有计入离线失败或伪装成通过。
- residual_risks: 自然语言等价语义仍需维护者正确归组，hash 只能自动发现精确内容或已声明分组的重叠；
  托管 CI、Docker、真实模型和 semantic provider 保持明确未验证。受跟踪的 development artifact 不替代
  clean candidate 证据。
- next: R0 gate 已通过；下一提示词为 R1.1。本会话只完成 R0 收口，不开始 R1。

### R1.1 - 2026-08-21

- commit/evidence: 会话从与 `origin/main` 同步的
  `8d5d2a15bb107c90dcada53018b65728371c6d88` 开始；R1.1 实现 commit 以包含本交接记录的 Git 提交为准，
  不在提交内容中自引用无法预先确定的哈希。开始前源码与 R0.4 交接均确认 R0 gate=passed。
- changes: 在 `edu_agent/engine/gateway.py` 增加 frozen `ApiMode`、`CredentialRef`、`ProviderSpec`、
  `ProviderCapabilities`、`ResolvedRoute`/`RouteIdentity`、注册元数据和窄 `ProviderAdapter` 协议。
  `ProviderGateway.begin_turn()` 固定执行“显式 mode -> 注册元数据 -> 精确受信任 HTTPS official host ->
  `chat_completions`”优先级；未知/本地/自定义 endpoint 保持 `custom`，不做模糊厂商推断。endpoint 校验拒绝
  userinfo、query/fragment、控制字符、编码 host 欺骗和非法 scheme；隔离 identity 只规范 scheme/host/default
  port 并保留 path 字节语义。`EduAgentService` 在 turn 起点冻结 primary/fallback route 并写入现有脱敏
  `provider_events`；凭据值和环境变量名均不进入 route repr、identity 或事件。旧 Chat adapter 仅附加 route
  元数据，`ResilientEngine` 只透传 route 冻结接口，未改重试、breaker、fallback 或流式行为；Responses mode
  可以解析，但当前工厂会在发请求前明确拒绝，留给 R1.3。
- migrations/config: 无数据库 migration，复用既有 `provider_events`；`pyproject.toml`、`uv.lock` 和 Python
  版本未改。`ModelConfig` 新增可选 `api_mode/vendor/deployment/endpoint/credential_env`，其中 `provider`
  继续作为旧 Engine 选择器，`base_url` 继续兼容但与 `endpoint` 同时出现会启动失败。保留
  `EDU_AGENT_ENGINE/BASE_URL/API_KEY/MODEL/FALLBACK_API_KEY`，空环境值按未设置处理；新增可选
  `EDU_AGENT_API_MODE/PROVIDER/DEPLOYMENT`。`config.example.toml` 使用新字段且只保存 credential 环境变量名。
- verification: `tests/test_provider_gateway.py` 30 passed，覆盖四级 mode 优先级、冲突、未知 mode、精确官方
  host、自定义/本地 endpoint、路径隔离、非法 URL、不可变 identity、capability 校验、旧 TOML/环境变量、
  默认值、Responses 防误发、turn 起点事件和多组凭据 canary；与现有韧性专项合跑 41 passed。受限沙箱的
  首轮受影响回归为 108 passed/4 failed，四项均止于绑定 `127.0.0.1:0` 的 `PermissionError`；同一命令在
  获准环境为 112 passed。最终显式清空真实凭据、mock/local、`--frozen --offline` 全量在获准环境为
  226 passed（13.78 s）；全仓 `uv run --frozen --offline ruff check .` 为 0 diagnostics；示例配置可解析，
  `git diff --check` 通过。
- not_verified: 本会话没有发真实 Provider/公网请求，没有接入 Responses wire adapter，也没有验证真实模型、
  Docker、semantic provider 或 GitHub-hosted CI；普通切片按协议未重复运行阶段收口用 Stage 8。
- residual_risks: 当前 Chat Completions 仍由 `OpenAICompatEngine` 直接调用 SDK，route 尚未成为唯一 adapter
  调用路径；capability 目前是显式/注册声明，尚未用于 R1.5 fallback 兼容门禁；Retry-After、jitter、并发上限
  和 per-route breaker 仍按路线留给 R1.4。R1 总门禁保持 `in_progress`。
- next: R1.2，将现有 OpenAI-compatible Chat Completions 迁到本契约后的 adapter，并保持 Agent/同步返回和
  旧配置行为不变；不得提前接 Responses 或改变流式协议。

### R1.2 - 2026-08-22

- commit/evidence: 会话从 R1.1 提交 `279f46afa14de367ea34e0bd30e80a1b21abd5fd` 开始；R1.2 实现 commit
  以包含本交接记录的 Git 提交为准，不在提交内容中自引用无法预先确定的哈希。开始时工作区干净，未覆盖
  用户改动。
- changes: 新增 `ChatCompletionsAdapter`，唯一负责 normalized messages/tools 到 OpenAI-compatible
  `chat.completions.create` 的同步 wire 映射，以及 response 到 `EngineResponse/ToolCall` 的规范化；保留
  `usage`、`finish_reason`、`model`、`None`/空 content、多 tool call 和字符串 arguments。`ProviderGateway`
  增加默认 Chat adapter 注册、按 `ApiMode` 选择和 `GatewayEngine` 同步 facade；`get_engine` 的 primary/fallback
  均走同一 adapter/Gateway 路径，Agent、eval、mock 的 `Engine.chat` 面不变。`OpenAICompatEngine` 保留旧
  构造参数和 `configure_provider_route`，但已薄化为 Gateway 兼容层，删除重复 SDK 请求/响应逻辑。补充
  架构/README 的迁移说明。
- migrations/config: 无数据库 migration、无新环境变量、无依赖变更；不实现 Responses 或 streaming。
  adapter 支持注入 SDK client/client factory，按冻结 route endpoint/credential 创建 client，保留旧
  `EDU_AGENT_*`、DashScope/vLLM endpoint 和 fallback 配置行为。
- verification: 新增 `tests/test_chat_completions_adapter.py`，用 `httpx.MockTransport` 穿过真实 OpenAI
  SDK 验证 `/chat/completions` URL、Authorization、空/非空 tools、tool_choice、temperature、超时设置；
  fixture 覆盖空 content、空字符串 arguments、多 tool call、usage、finish reason/model，并验证
  `APITimeoutError`/`BadRequestError` 原样传播。adapter/provider/韧性专项 `48 passed`；Agent/Runtime/
  service/observability 组合专项在获准回环环境 `129 passed`；显式空凭据、mock/local、offline 全量
  `233 passed (13.62s)`；`uv lock --check`、`uv pip check`、全仓 offline ruff 和 `git diff --check` 均通过。
- not_verified: 没有访问公网或真实模型；Responses、streaming、Docker/Jobe、semantic provider 和托管 CI
  仍未运行。受限沙箱中 4 个既有 HTTP/SSE 测试因禁止绑定 `127.0.0.1:0` 无法验证，已在获准本机回环环境
  通过，不计为代码失败。
- residual_risks: adapter 当前只支持同步 Chat Completions；Provider capability 尚未用于跨 route fallback
  兼容性门禁，Retry-After/jitter/concurrency/breaker 细化留给 R1.4；真实厂商 wire 差异尚需 R1.3/R1.5
  的离线 fixture 继续覆盖。R1 总门禁保持 `in_progress`。
- next: R1.3，基于锁定 SDK/fake wire 实现最小 Responses adapter，并为 Chat/Responses 建立等价 tool-call
  契约；保持同步 Agent Loop，不实现 streaming 或额外厂商。

### R1.3 - 2026-08-22

- commit/evidence: 会话从与 `origin/main` 同步的 R1.2 提交
  `2e4712c9dab51548c2cc760cc7e25781de647ade` 开始；R1.3 实现 commit 以包含本交接记录的 Git 提交为准，
  不在提交内容中自引用无法预先确定的哈希。开始时工作区干净，未覆盖用户改动。实现前直接检查锁定的
  `openai 2.43.0`：`Responses.create`、`FunctionToolParam`、`ResponseFunctionToolCall`、`Response.output/status`
  与 `ResponseUsage` 的生成签名/类型，而非凭记忆推断字段。
- changes: 新增同步 `ResponsesAdapter`，把内部 system/developer/user/assistant 消息映射为 Responses text
  input，把历史 Chat 形态 assistant tool call/result 映射为扁平 `function_call/function_call_output` item，
  并把嵌套 Chat function tools 转成 SDK 2.43.0 要求的扁平 function tool。响应按 output 顺序聚合交错
  `output_text`，使用 `call_id/name/arguments` 生成现有 `ToolCall`；坏 JSON arguments 原样保留给既有工具
  参数校验，未知 output item 忽略。Responses 的 input/output usage 名称映射到 Chat 的
  prompt/completion 语义，completed/incomplete status 映射为 `stop/tool_calls/length/content_filter`，响应内
  failed/cancelled/非终态由 `ResponsesAPIError` 明确失败，model 缺失时回落冻结 route。
- capabilities: adapter 明确声明 `tool_calling=true`、`usage=true`、`structured_output=false`、
  `streaming=false`，model-specific `context_window_tokens/max_output_tokens=None` 表示未知而非无限。route
  禁用 tool calling、strict schema、非 function tool、非文本 message、孤立 tool result 和超过已声明
  context window 的输入均在创建 SDK client/发 HTTP 前失败；Chat adapter 同步增加 route tool capability
  预检。`ProviderGateway` 与 `get_engine` 同时注册两种 mode，旧 `OpenAICompatEngine`、同步 Agent Loop、
  DashScope/vLLM Chat 路径和 fallback 形态不变。
- fixtures/contracts: 新增六组 Responses JSON fixture，覆盖单/多 function call、交错 text、缺失 usage、
  incomplete、response error、未知 output item 和坏 arguments；均通过 `httpx.MockTransport` 穿过真实锁定
  SDK。新增同一语义双 mode fixture，分别经 `/chat/completions` 与 `/responses` Gateway 路径得到完全等价
  的内部 tool-call 序列。Responses 端到端 canary 测试确认秘密进入 Provider input，但不进入冻结 route、
  SQLite provider event/持久化消息或导出 Trace，凭据环境变量名同样不落审计面。
- migrations/config: 无数据库 migration、无新环境变量、无依赖或 lockfile 变化；现有 `api_mode=responses`
  配置现在可执行。README/architecture 补充第二个 adapter、同步边界与 capability 口径。本会话未增加
  Anthropic、Gemini、消息平台 Gateway、多厂商认证、structured text output 或 streaming。
- verification: Responses/Chat/双 mode contract/Provider Gateway 专项 `48 passed (0.45s)`；显式清空模型及
  平台凭据、mock/local、`--frozen --offline` 全量 `244 passed (13.82s)`；全仓 offline ruff 为 0
  diagnostics。`uv lock --check` 解析 91 packages，`uv pip check` 检查 62 packages/0 conflicts；全部 JSON
  fixture 可解析，`git diff --check` 通过。所有 Provider 测试均离线，无真实模型请求。
- not_verified: 没有访问真实 Provider、模型或发送真实凭据，没有验证 Docker/Jobe、semantic provider、
  GitHub-hosted CI 或真实 OpenAI model-specific context/output limits。尝试只读获取官方 OpenAI Responses/
  function-calling 页面时站点返回 HTTP 403，因此协议字段的本会话证据是锁定 SDK 2.43.0 生成类型与离线
  fake HTTP wire；普通切片按协议未运行阶段收口用 `accept_stage8.sh`。
- residual_risks: Responses adapter 仅支持同步 text/function-call 小面；model-specific capability 仍需部署
  显式声明，未知限制不会被伪装成无限。`ResponsesAPIError` 的重试分类、Retry-After/jitter、并发上限和
  per-route breaker 留给 R1.4；跨 mode fallback capability 门禁留给 R1.5。R1 总门禁保持 `in_progress`。
- next: R1.4，读取本交接、`engine/resilient.py`、Provider 事件与现有故障测试，实现 Retry-After、确定性
  jitter、有界并发和 per-route circuit breaker；不得提前做 capability fallback、凭据池或流式重试。
