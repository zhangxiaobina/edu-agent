# Optimization Progress

## Current

- last_completed_prompt: R3.4
- next_prompt: R3.5
- baseline_commit: 8d5d2a15bb107c90dcada53018b65728371c6d88
- stage_gate: in_progress
- stage_gate_reason: R0-R2 顶层门禁保持 passed；R3.1-R3.4 的 ToolManifest、Canonical Teaching Provider、16 工具契约矩阵和 schema-guided 参数治理已通过，R3 总门禁仍需 R3.5 安全并发与 R3.6 插件/MCP 收口

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

### R1.4 - 2026-08-22

- commit/evidence: 会话从与 `origin/main` 同步的 R1.3 提交
  `096a86e8e49c8bf38c215d3efb15ec84cd5e9e44` 开始；R1.4 实现 commit 以包含本交接记录的 Git 提交为准，
  不在提交内容中自引用无法预先确定的哈希。开始时工作区干净，未覆盖用户改动。
- failure/retry policy: `FailureKind` 增加 `output_cap`，分类器按异常类型、HTTP status 和结构化 error code
  区分 connection/timeout/429/5xx、auth/permission/invalid/context overflow/output cap/unknown；只有明确
  瞬态项可重试，`insufficient_quota` 等终态 429 也快速失败。`ResponsesAPIError` 的 server/context/output
  code 纳入同一分类。`Retry-After` 同时支持整数秒和 HTTP-date；合法服务端值完全覆盖本地 full-jitter
  指数退避并受独立上限约束，非法值回落本地策略。monotonic clock、wall clock、sleeper 和 random source
  均可注入，测试不真实等待。两个默认 OpenAI SDK adapter 显式使用 `max_retries=0`，避免隐式 HTTP 尝试
  绕过本层次数、等待和审计；显式注入 client/factory 仍由调用方控制。
- route isolation/lifecycle: 新增实例所有的 `RouteStateRegistry`，按冻结 `ResolvedRoute.identity` 维护
  `BoundedSemaphore` 和 `CircuitBreaker`；endpoint、model、deployment/API mode 不同的 route 互不污染。
  breaker 以带 generation 的 permit 原子提交状态，过期并发结果不能关闭新一代 breaker；half-open 同 route
  只放行一个受控探测。注册表有固定容量和空闲 TTL，活跃 lease 与 TTL 内 degraded/open 状态不可被容量
  淘汰；全为受保护状态时 fail closed，健康 LRU 或 TTL 到期状态才可回收，因此无进程级无限字典。
- events/security: 每个实际 primary/fallback Provider attempt 新增一个 `provider_attempt` 事件，统一包含序号、
  脱敏 route、failure kind、retryable、实际 delay/source、breaker 前后状态和有界纯数值 usage；失败正文、请求
  messages、key 和 credential 环境变量名从不进入 payload。事件在 sink 前和 SQLite 写入时双重经过共享分类器；
  标准 prompt/completion/cached/reasoning token 字段加入精确 metric 词表，字符串 usage 元数据在韧性层先丢弃。
- migrations/config: 无数据库 migration，复用 `provider_events.details_json`，旧库兼容；无依赖、lockfile、环境
  变量或凭据格式变化。`ModelConfig`/`config.example.toml` 新增带默认值和启动校验的
  `retry_base_delay_seconds=1`、`retry_max_delay_seconds=8`、`retry_after_max_seconds=60`、
  `route_max_concurrency=4`、`route_state_capacity=128`、`route_state_ttl_seconds=900`；TTL 必须大于 breaker
  cooldown。本会话没有增加 fallback 兼容选择、凭据池、流式或上下文压缩重试。
- verification: R1.4/旧韧性/Chat/Responses/Gateway 专项 `90 passed (1.76s)`，覆盖 429 秒数与上限、HTTP-date、
  非法 header 的确定性 jitter、401、403、400、context overflow、output cap、Responses 内嵌错误、并发上限、
  half-open 竞争、endpoint/model 隔离、容量/TTL、SDK 隐式重试关闭以及 SQLite attempt usage/key/正文脱敏。
  最终显式清空模型及平台凭据、mock/local、`--frozen --offline` 全量在获准回环环境
  `276 passed (13.56s)`；全仓 offline ruff 0 diagnostics；`uv lock --check` 解析 91 packages，`uv pip check`
  检查 62 packages/0 conflicts；示例配置可解析，`git diff --check` 通过。所有新增 Provider 测试均离线。
- not_verified: 没有访问公网、真实 Provider/模型或发送真实凭据，没有验证 Docker/Jobe、semantic provider、
  GitHub-hosted CI 或外部注入 SDK client 的重试配置；普通切片按协议未运行阶段收口用 `accept_stage8.sh`。
- residual_risks: 当前同步 semaphore 等待尚未接 R2 cancellation；显式注入 client/factory 若自行开启重试，
  其内部请求不受本层逐 attempt 审计。最重要的是旧 fallback 触发语义本轮刻意未改变，401/403/普通 400/
  context overflow 等虽不会 retry，配置 fallback 时仍可能切换；failure-kind 策略与 capability-safe fallback、
  切换原因和完整 R1 fake-server 门禁必须在 R1.5 收口。R1 总门禁保持 `in_progress`。
- next: R1.5，基于 R1.1-R1.4 交接实现 failure-kind 与 capability-safe fallback，验证两种 mode 的完整故障矩阵
  和 attempt 审计并独立收口 R1；不得开始 token streaming、凭据池或 R2 工作。

### R1.5 - 2026-08-22

- commit/evidence: 会话从 R1.4 提交 `4f94fd1ca35f55e345707f3ad25d02553c7747d8` 开始；本次工作区开始时
  `main...origin/main` 同步且无改动。实现未创建本地提交，后续提交哈希不在本交接中预填。
- changes: `ProviderGateway` 新增结构化 `ProviderRequestRequirements`、effective capability 合并、无网络
  adapter 预检和稳定 capability gap；请求需求由当前同步 `messages/tools` 推导 tool calling、strict structured
  output、API mode、非流式和保守 context token 估算。`ChatCompletionsAdapter`/`ResponsesAdapter` 在 SDK client
  创建和 HTTP 前拒绝不支持的 tool、strict schema、已知 context overflow 或坏 mode 请求。未知 context 不当作
  无限，Provider fallback 必须在配置中声明 `fallback_context_window_tokens`。
- fallback/route: `ResilientEngine` 在 turn 起点冻结 primary/fallback `ResolvedRoute`，运行时通过 context
  使用冻结 route，即使 engine 后续重配置也不改变该 turn。fallback 仅允许 connection/timeout/可恢复 429/5xx
  或 circuit-open；auth/permission/普通 400/context overflow/output cap/unknown 和不兼容 capability 均记录
  `fallback_rejected` 后保留 primary 原错。`route_selected`、`route_resolved`、`fallback_activated`、
  `fallback_rejected`、`provider_result_selected` 与逐 attempt 事件包含选择/拒绝原因、route、failure kind、
  compatibility、attempt 序号和脱敏数值 usage。胜出 response 使用深拷贝副本追加 runtime/fallback metadata，
  旧 attempt 不能覆盖最终 usage 或终态。
- credentials/config: 每条 route 继续只有单一 `CredentialRef`；未实现 key pool、轮换、quarantine 或运行中
  静默降级。fallback URL/mode/context 字段孤立、未知 mode、缺少已知 context 上限、相同 route identity 或
  primary/fallback route metadata 不完整时在构造/启动阶段失败。`config.example.toml` 仅记录环境变量名和示例
  context 上限，不保存 key。
- fake-provider evidence: 新增 `scripts/accept_r1_fake_provider.py` 与 `tests/test_r1_fake_provider_acceptance.py`。
  fake server 只绑定 `127.0.0.1`，通过锁定 OpenAI SDK 的真实 `/chat/completions` 与 `/responses` wire，显式
  `trust_env=False`，不访问公网或机器代理；覆盖双 mode 等价双 tool call、Retry-After=7、共享 registry 的
  per-route breaker 隔离、Responses compatible fallback、tool/strict/context/mode 不兼容拒绝、401/403/400/
  context terminal 拒绝、output cap 不切换、attempt/胜出审计和 key/credential ref/body 脱敏。
- documentation: README、`docs/architecture.md`、`docs/production-runtime.md` 更新为已验证的 R1 capability-safe
  fallback 口径；继续明确当前不是 token streaming，R2 RunEvent/Journal/TurnFinalizer、R3 ToolManifest、R4
  context/budget/drain、R5 真实模型候选版仍在路线中。
- migrations/config: 无 migration、无依赖或 lockfile 变化；新增配置字段只有 `fallback_api_mode` 与必填的
  `fallback_context_window_tokens`，旧 primary/fallback 环境变量兼容且仍不记录凭据值。
- verification: `uv run --frozen --offline ruff check .` 0 diagnostics；R1 Gateway/adapter/Resilient/Trace/旧
  runtime 专项 `125 passed (3.92s)`；最终显式清空模型与平台凭据的 Stage 8 离线全量
  `299 passed (14.91s)`；独立 `scripts/accept_r1_fake_provider.py` 输出 `gate=passed`、两种 mode、
  `equivalent_tool_calls=2`、`retry_after_seconds=7`、`attempt_events=12`、breaker isolation、compatible
  fallback 和四类 incompatibility gaps；`zsh scripts/accept_stage8.sh` 通过，lineage 通过、data boundary
  findings=0、10k Trace 三项断言通过、Stage 7 regression 通过。`git diff --check` 通过，Stage 8 生成的
  development artifacts 已恢复为会话开始版本。
- not_verified: 没有访问公网、真实 Provider/model 或发送真实凭据；Docker/Jobe 由 Stage 8 按既有契约记录
  `sandbox=not_verified`，真实模型评测为 `not_run`；未验证 GitHub-hosted CI 或外部注入 SDK client 自带重试。
- residual_risks: 当前仍是同步 `Engine.chat`，没有 token streaming、R2 journal/finalizer、取消贯穿 Provider、
  多 key 轮换、真实厂商 model-specific limit 自动发现或 R4 context overflow recovery。配置 fallback 要求已知
  context 上限，部署方必须提供真实且不低估的声明；Provider adapter/route capability 仍是部署声明，不是远端
  自动探测。
- gate: `R1 gate=passed`。证据覆盖两种 API mode、Retry-After、per-route breaker、兼容/不兼容 fallback、
  attempt 审计、usage/终态 ownership 和 key 脱敏；本阶段没有开始 token streaming。
- next: R2.1，先定义 typed `RunEvent v2`、单调 sequence、单 writer 发布协议；不得回头把 R2-R5 能力写成当前
  已实现，也不得引入凭据池或跳过 R2 前置门禁。

### R2.1 - 2026-08-22

- commit/evidence: 会话从与 `origin/main` 同步的 R1.5 提交
  `e0be594cbffeaa3563e6a12adad486063ac13172` 开始，开始时工作区无改动。实现未创建本地提交，后续提交
  哈希不在本交接中预填；前置 `R1 gate=passed` 已由源码、测试和上一交接共同确认。
- v1 inventory/compatibility: `RuntimeEvent v1` 的生产侧仍是 runs/messages/session lease、Provider attempt、
  Plan/Evidence、tool/operation、Artifact、delegation、scheduler/audit 等持久业务表及其同事务
  `trace_event_index` trigger；消费侧仍是 `TraceRepository`、Service/API 查询和 JSON/JSONL 导出、CLI inspector、
  benchmark 及可选 telemetry。`SCHEMA_VERSION=edu-agent.runtime-event.v1`、查询 cursor、owner scope、导出 envelope
  均未改变；`TraceRepository` 明确只投影持久审计/业务状态，不读取 EventBus，因此没有第二套 Trace 真相源。
- run-event schema: 在现有 observability 边界新增 `edu-agent.run-event.v2`，稳定 envelope 包含 event/schema、
  run/session、attempt、sequence、UTC timestamp、writer/fencing identity 和 payload。typed family 覆盖
  `run.phase`（accepted/planning/model/tools/verifying/finalizing/terminal）、`text.delta`、
  `tool_call.delta`、`usage`、`plan.updated`、`tool.started/completed`、`context.compacted`、
  `fallback.activated`、`completed/error`；反序列化拒绝缺失/未知字段、坏类型、naive time 和非有限 JSON。
  payload 在发布前经过注入的共享 `RedactionPolicy`，`RunEvent` 构造边界再执行默认 fail-closed 脱敏。
- publication protocol: `RunEventBus` 在一个临界区内按 `(run_id, attempt)` 校验 writer、分配连续 sequence 并
  fan-out；同 token 不允许不同 writer，更高 fencing token 接管后延续 sequence，旧 handle 的结果被拒绝。
  `completed/error` 原子设置 terminal tombstone，此后拒绝所有新事件和新 writer 接管。`sequence_start` 只作为
  未来 RunJournal 恢复注入点；总线本身 future-only，不落库、不回放，也不拥有恢复 cursor。
- backpressure/cancellation: 每个订阅 buffer、活跃订阅数和 stream state 数均有固定上限并 fail closed；单个
  buffer 满时只断开该慢消费者、清空其不完整队列并返回 `SlowConsumerError`，不阻塞生产者或其他消费者。
  主动取消会唤醒阻塞 waiter，但只取消订阅，不等同于 run cancellation；terminal 订阅可排空已接收事件后关闭。
  terminal tombstone 不做静默容量淘汰，只在 EventBus 关闭时统一释放。
- scope/migrations/config: 没有数据库 migration、依赖、lockfile、环境变量或应用配置变化；没有修改
  `api.py`、`service.py`、Provider adapter、Agent Loop、StateStore schema 或消息提交路径。本会话只使用 fake
  producer；当前 Provider 仍同步，SSE 仍为 `accepted/keepalive/completed`，RunJournal/真实 delta/统一取消分别
  留给 R2.2/R2.5/R2.6。
- verification: fake RunEvent 协议 `12 passed (0.15s)`；RunEvent + observability/Trace/Stage 8 Trace + Provider
  审计兼容专项 `129 passed (4.03s)`，覆盖完整事件族、schema/UTC/JSON 校验、中心脱敏、sequence seed、多个
  并发 producer handle、writer takeover、terminal fence、慢消费者、waiter 取消、future-only 无 replay、总容量
  和 v1 envelope/query/export；全仓 `uv run --frozen --offline ruff check .` 0 diagnostics；显式清空模型/平台凭据
  并禁用外部 pytest plugin 的全量离线回归 `311 passed (15.64s)`；`git diff --check` 通过。普通切片按通用协议
  未运行 R2 阶段收口用 `accept_stage8.sh`。
- not_verified: 未访问公网、真实 Provider/model、Docker/Jobe、GitHub-hosted CI 或真实 token stream；未验证
  跨进程事件传输，因为 EventBus 的合同明确仅为进程内。R2.1 不声称 SSE 真流、断线恢复或增量消息提交。
- residual_risks: 默认 stream/subscription 容量目前只在 EventBus 构造处定义，待真正接入 Service 生命周期时需
  根据并发上限配置和监控；terminal tombstone 为保证 late delta fence 保留到 bus close，达到 stream cap 会显式
  拒绝新 stream，而不会淘汰安全状态。恢复 sequence 的持久原子性尚不存在，必须由 R2.2 RunJournal 完成。
- gate: `R2.1 passed`；R2 总门禁保持 `in_progress`，不得把本协议写成 Provider/SSE 已流式化。
- next: R2.2，实现 RunJournal 持久 schema、migration、原子 cursor API 和旧库兼容；不改变 Agent Loop 的实际
  提交点，也不得提前进入 assistant/tool 增量提交。

### R2.2 - 2026-08-22

- commit/evidence: 会话从 R2.1 交接的 `e0be594cbffeaa3563e6a12adad486063ac13172` 开始；期间 R2.1
  变更已由当前 HEAD `aec84a5e722b9adc34f0daf75901eddaa4a4b251`（`feat: define typed RunEvent v2 protocol`）
  固化。工作区仍有本会话及用户既有文档改动，本会话未回退或覆盖，未创建 R2.2 本地提交。
- changes: 新增 `edu_agent/state/journal.py` 的持久 `RunPhase`（主链加明确 `cancelled/failed` 终态分支）、
  合法转换、结构化错误、只读 `RunJournalSnapshot` 和薄 `RunJournal` facade。`StateStore` 增加严格
  `create/initialize`、compare-and-set 和只读 snapshot API；CAS 在同一 `BEGIN IMMEDIATE` 中校验
  run/session/actor/tenant、revision/phase、loop cursor、model attempt、event sequence、writer/fence 和
  真相表引用。terminal/cancelled/failed 不可回到执行态，旧 owner、重复/跳跃写和游标回退不会静默成功。
- schema/migration: 新增幂等 `009_run_journal` migration 与 `run_journals` 单行表；只保存 Plan、Evidence、
  ToolOperation、Artifact、context checkpoint/tool event 的 ID 引用，以及冻结 route、manifest hash、预算
  快照和最后稳定边界，不复制正文或 Trace。SQLite `PRAGMA user_version=9` 与未来版本拒绝逻辑防止 schema
  倒退；列/表创建和 marker 可在进程中断后重开补齐。无依赖、lockfile、环境变量或 Agent Loop 提交点变化。
- documentation: 新增 [`docs/run-journal.md`](run-journal.md) 恢复状态表与不变量；同步 architecture、production
  runtime 和 README，明确 RunJournal 已持久化但尚未接入 Agent Loop/Provider/SSE。
- verification: 新增 `tests/test_run_journal.py` 16 项，覆盖新库、旧库迁移、重复 migration、显式 scope、跨
  run 引用、合法/跳跃/重复/终态转换、单调 cursor、并发 CAS、旧/过期 fence、future schema、损坏 JSON/phase
  与循环 JSON 拒绝。`uv run --frozen --offline ruff check .` 通过；journal 专项为 `16 passed (0.28s)`；
  state/event/plan/RAG/distributed 回归 `56 passed`；recovery/Trace/真实 HTTP 专项在受限沙箱中仅 4 个
  loopback bind 权限失败，获准环境同命令 `24 passed (3.58s)`；显式清空凭据并禁用外部 plugin 的全量离线
  pytest 为 `327 passed (15.41s)`；`git diff --check` 通过。
- not_verified: 未运行阶段收口 `zsh scripts/accept_stage8.sh`、真实 Provider/model、Docker/Jobe、GitHub-hosted
  CI、跨进程 EventBus 或 R2.3 之后的真实 delta/SSE/取消/五崩溃窗；本会话没有改变这些能力的状态口径。
- residual_risks: `RunJournal` 目前是可独立调用的持久 API，消息 envelope、tool result 增量提交和 finalizer 仍由
  后续 R2.3/R2.4 接入；旧数据库若包含未知更高 schema marker 会按设计拒绝启动，需要对应新代码迁移。
- gate: `R2.2 passed`；R2 总门禁保持 `in_progress`。
- next: R2.3，在 Agent Loop 工具执行前接入 assistant envelope 和每个 tool result 的原子增量提交；不得在本阶段
  回头实现 Provider streaming、HTTP SSE 或最终消息 finalizer。

### R2.3 - 2026-08-22

- commit/evidence: 会话从与 `origin/main` 同步的 R2.2 提交
  `da81969a136893b51550c3ae643ec5dd5e22b28b`（`feat: add persistent run journal`）开始，开始时工作区干净；
  完成验收后收到用户提交指令，R2.3 改动作为单一提交推送到 `origin/main`；准确提交哈希以 Git 历史为准，
  不在提交内容中预填自引用哈希。
- changes: 新增 `AgentLoopJournal`，在完整模型响应返回后、任何工具执行前，将唯一 assistant tool-call envelope、
  call ids、model attempt、manifest/route 和 loop cursor 原子写入；持久化成功后才进入顺序 tools phase。每个
  result（成功、超时、取消或结构化拒绝）分别与 call 按原索引配对，并和 journal cursor/event sequence 在同一
  `BEGIN IMMEDIATE` 中提交。取消会关闭同一已声明 envelope 中尚未执行的 calls，但不会执行剩余 handler。
- idempotency/recovery: `(run_id, model_attempt)` envelope、`(run_id, tool_call_id)` call 和
  `messages(run_id, idempotency_key)` 均有唯一约束；精确重放返回既有消息，payload 漂移、孤立/乱序 result、
  重复 call id、跨 run 配对、错误 tool/attempt、越权 operation 与旧 fencing token 均结构化失败。journal 停在
  `tools/verifying` 时，`run_agent` 合并已提交协议消息并从对应节点重入，保持 envelope/result/下一轮 model 的
  原顺序；service 不再事后批量追加相同 tool 协议消息，只保留普通 assistant 的兼容追加路径。
- write safety: result 若引用写操作，必须绑定真实 `ToolOperation` 并在 payload 中携带相同 operation id。
  `committed` operation 重入只复用原结果；`executing/manual_review` 在 graph 和事务 runtime 两层拒绝再次进入
  handler，审批重入也不能把 `executing` 降回 `approved`。显式 replay scope 可跨 run 复用同一 operation 回执，
  但各 run 的 call/result 仍独立配对。
- schema/migration: 新增幂等 `010_agent_tool_messages`、`agent_tool_envelopes`、`agent_tool_calls`，并给
  `messages` 增加 `idempotency_key/model_attempt/loop_cursor`；SQLite `PRAGMA user_version=10`。旧库消息原样
  保留，重复 migration 和 marker 幂等，call 状态/result 引用有数据库 `CHECK`，缺列 schema fail closed。tool
  result JSON 在持久化前按结构化字段脱敏。无依赖、lockfile、环境变量或配置项变化。
- fault injection: 覆盖 envelope 提交前/后、只读 result 提交前/后、已 committed 写 operation 到 result 之间；
  验证前置故障无半条消息、后置故障只保留一个已提交对象、只读未证明结果可重放、已提交写副作用与 operation
  均唯一。另覆盖多 call 取消配对、timeout/INVALID_JSON、跨 run operation replay 和不确定写 handler 零调用。
- documentation: 更新 README、architecture、production runtime 与 RunJournal 合同；后者增加旧 service 批量
  追加与新 Agent Loop 增量路径对照图。文档明确最终消息 finalizer、Provider streaming、HTTP SSE 和工具并发
  仍未实现。
- verification: 新增 `tests/test_agent_tool_messages.py`，独立专项 `17 passed (1.46s)`；agent/runtime/transaction/
  recovery/RunEvent/HTTP 组合在受限环境为 `101 passed/4 failed`，四项均止于 `127.0.0.1` socket bind 的
  `PermissionError: [Errno 1]`，获准环境同组合最终为 `105 passed (5.77s)`。一次获准中间复跑中，既有 canary
  检测用例因随机 scope/event id 命中手机号正则而误报；失败轮的数据库/artifact/export canary 断言均已通过，
  该用例随即单跑 `1 passed`，最终组合与全量也通过。`uv run --frozen --offline ruff check .` 通过；显式清空
  模型/平台凭据并禁用外部 pytest plugin 的全量离线回归 `344 passed (17.26s)`；`git diff --check` 通过。
- not_verified: 普通切片未运行阶段收口 `zsh scripts/accept_stage8.sh`；未访问真实 Provider/model、Docker/Jobe、
  GitHub-hosted CI、真实 token stream 或跨进程 EventBus。
- residual_risks: 最终普通 assistant、usage/budget 结算和 run terminal 仍不是一个幂等原子 finalizer，崩溃后完整
  终态恢复留给 R2.4/R2.7；Provider 仍同步聚合，通用 CancellationToken 与 HTTP writer fence 留给 R2.5/R2.6。
  R2.3 的 fault fixture 验证持久 SQLite 上的边界重入，但尚未声称完成 R2.7 的五窗口进程重开门禁。既有敏感数据
  检测器仍可能把随机十六进制 ID 中偶现的连续数字误判为手机号，本次未扩大范围修改该 R0/R2.1 测试工具。
- gate: `R2.3 passed`；R2 总门禁保持 `in_progress`。
- next: R2.4，沿 service `try/except/finally`、Plan verifier、`finish_run` 和 lease 边界实现唯一幂等
  `TurnFinalizer`；不得提前实现 Provider streaming、HTTP SSE 或并发工具。

### R2.4 - 2026-08-22

- commit/evidence: 会话从与 `origin/main` 同步的 R2.3 提交
  `c59f67521cd6c66a9bdd222ddf259915df7bca22`（`feat: persist agent tool messages incrementally`）开始；
  R2.4 当前仅在工作区实现，未创建本地提交或推送。普通切片不以 development artifact 作为发布证据，误跑
  Stage 8 产生的时间、性能和 dirty-HEAD 噪声未保留。
- path inventory: 旧成功路径由 service 先追加普通 assistant、再 `finish_run(completed)`；Plan 非完成由相同
  路径写 failed；取消在 `_chat_turn` 与 `_chat_turn_impl` 各有一套 `finish_run(interrupted)`；setup/model 异常
  另写 `finish_run(failed)`；恢复发现已提交 assistant 时再次直接 finish；RuntimeManager finally 无条件释放 lease；
  API 正常、异常和外层 future 异常均可调用 request completion。这些入口现统一到 durable finalizer，legacy
  `finish_run`/terminal `append_messages` 在 finalizer 存在时拒绝旁路。
- finalizer: 新增每 run 唯一的 `turn_finalizers` 与单调 cursor：`tools_closed -> plan_verified ->
  final_message_committed -> usage_settled -> terminal -> hooks_done -> cleanup_done`。每步使用 SQLite
  `BEGIN IMMEDIATE`、revision/cursor CAS 和当前 lease fence；最终 assistant 使用
  `final-assistant:<run_id>` 唯一键。重复调用读取首个持久 candidate，恢复 worker 从 cursor 继续，旧 worker
  在更高 fencing token 接管后不能提交。
- outcomes: 仅 `completed` 写最终 assistant；无最终消息的伪成功 fail closed 为 `model_failed`。取消稳定映射为
  `interrupted`，预算耗尽为 `budget_exceeded`，Provider/模型异常为 `model_failed`，不确定写或 verifier 不可用为
  `manual_review`。terminal 事务同时提交 journal、runs 和 finalizer；若取消/不确定写在消息提交后获胜，消息
  原子标为 inactive。Provider usage、budget、Trace、Plan 与 context payload 由首个 finalizer candidate 持久化。
- recovery/order: pending finalizer 不被 stalled/API lease recovery 改写为 abandoned；lease 只在 durable terminal
  后释放，API request completion 以 request 已绑定 run 为准复验 run/finalizer terminal，拒绝省略 run id 绕过和
  run 绑定漂移。terminal 后 response 提交前崩溃可由 `recover_chat_result` 从 finalizer 重建兼容 `ChatResult`。
  后处理与 cleanup 使用持久唯一 claim；失败/超时只写脱敏审计，不反转主 turn。
- coverage: `tests/test_turn_finalizer.py` 覆盖重复调用、每个 cursor 后崩溃、首 candidate ownership、legacy bypass、
  verifier 异常、取消/预算/模型/manual-review、同 token 竞争、更高 token 接管、最终消息后取消、lease/API
  顺序、request 绑定、后台钩子失败、cleanup 超时/竞争和 service setup/model failure。
- schema/config: 增加 `011_turn_finalizer` migration、`turn_finalizers`/`turn_finalizer_hooks` 表和
  `runs.usage_json/stop_reason` 列；正式提升 `PRAGMA user_version=11`，使 R2.3 旧二进制明确拒绝新库。旧库可重复
  初始化且未来数字 schema 仍拒绝降级。无依赖、lockfile、环境变量或配置项变化。
- documentation: README、architecture、production runtime 和 RunJournal 合同已更新为 R2.4 现状；明确当前
  Provider 仍同步聚合，HTTP SSE 仍只有 `accepted/keepalive/completed`，没有提前实现流式或并发工具。
- verification: `tests/test_turn_finalizer.py` 为 `33 passed (1.36s)`；journal/tool-message/Plan/runtime/distributed
  组合为 `81 passed (2.40s)`；observability/API recovery/真实回环 HTTP 组合为 `24 passed (4.12s)`。显式清空
  模型与平台凭据、禁用外部 pytest plugin 的完整离线回归为 `377 passed (18.22s)`。`uv lock --check`、
  `uv pip check`、全仓 ruff 与 `git diff --check` 均通过。按普通切片协议未重跑阶段收口入口
  `zsh scripts/accept_stage8.sh`。
- not_verified: 未访问真实 Provider/model、Docker/Jobe、GitHub-hosted CI 或真实 token stream；这些能力没有因
  R2.4 的同步 finalizer 被误标为已验证。
- residual_risks: post-hook/cleanup 的 durable claim 提供 at-most-once 调用选择；若外部副作用完成后、完成记录前
  进程崩溃，系统不会自动重放，需由钩子自身的业务幂等/对账处理。R2.4 只验证持久 SQLite cursor 重入；完整
  五崩溃窗、事件 sequence 与进程重开决策总门禁仍属于 R2.7。
- gate: `R2.4 passed`；R2 总门禁保持 `in_progress`。
- next: R2.5，在 Provider Gateway 增加真实流事件迭代器，并让同步 `chat()` 聚合该流保持兼容；不得修改 HTTP
  SSE 或提前实现完整 CancellationToken 传播。

### R2.5 - 2026-08-22

- commit/evidence: 会话从 `46d2b82e48a0b407ed1875a6a98f796d8c148150`（`feat: add durable turn finalizer`，
  `main` 与 `origin/main` 同步）开始；本次未创建本地提交或推送。工作区中的改动均属于 R2.5，未覆盖用户已有
  文件；`HEAD...@{upstream}` 仍为 ahead `0`、behind `0`。
- provider event contract: 新增内部 `ProviderStreamEvent` 与 `ProviderStreamAggregator`。事件包含冻结
  `ResolvedRoute`、attempt、provider event id/type，覆盖 text delta、tool call id/name/arguments delta、
  usage、completed、error 和可审计 ignored。聚合器支持多个交错 tool call、空块和仅终块 usage；只有对应
  completed 后才物化 `ToolCall`，半段 JSON 不进入 JSON/Schema 校验或工具执行。
- adapters/wire: `ChatCompletionsAdapter` 使用锁定 OpenAI SDK `2.43.0` 的 `ChatCompletionChunk` 字段和
  `stream_options.include_usage=true`；`ResponsesAdapter` 使用真实 `response.output_text.delta`、
  `response.output_item.added/done`、`response.function_call_arguments.delta/done`、`response.completed`/
  `incomplete`/`failed`/`error` 事件，并以 `output_index + item_id` 关联交错 calls。UTF-8 字节碎片、旧兼容
  endpoint 返回普通 JSON、unknown event ignore/error 两种策略均有 fixture/test；普通 JSON 兼容路径复用同一次
  `stream=true` 请求，不发第二次请求。
- gateway/resilience: 新增 `ProviderStreamAdapter`、Gateway/GatewayEngine stream entry points；已知
  OpenAI、DashScope、vLLM 与显式 custom route 可声明 streaming。`ResilientEngine` 复用 R1 breaker、semaphore、
  Retry-After、jitter、fallback capability 和 attempt audit。首个可见 delta 前的瞬态错误可 retry/fallback；
  已发送 delta 后的错误为终态，不拼接另一模型输出；旧 attempt/route 迟到事件变为审计 ignored。真实
  `httpx` timeout/transport stream error 已纳入 R1 failure classification，错误正文按 credential/PII 脱敏。
- compatibility: adapter 与 ResilientEngine 的同步 `chat()` 都聚合同一事件迭代器为原有 `EngineResponse`；
  legacy、Mock、eval 和 Agent 调用方不需要迁移，也没有第二套同步核心解析。HTTP SSE、Agent-to-RunEvent 映射、
  完整 CancellationToken 传播和 writer fence 接线未在本阶段实现，留给 R2.6。
- fixtures/tests: 新增 `tests/fixtures/provider_streams/`（Chat/Responses SSE 与 fake attempts）及
  `tests/test_provider_streaming.py`，覆盖碎片化 arguments、交错 calls、usage、流中断、旧 attempt 迟到、
  retry/fallback 边界、同步聚合等价性、unknown policy、未预读普通 JSON 和真实 SDK transport error。
- verification: `uv lock --check`、`uv pip check`、全仓 `uv run --frozen --offline ruff check .` 与
  `git diff --check` 均通过；provider/engine/event 专项与 fake-provider 组合为 `130 passed`，loopback
  `tests/test_r1_fake_provider_acceptance.py` 在获准本机环境 `1 passed`，独立 `scripts/accept_r1_fake_provider.py`
  输出 `gate=passed`；显式清空模型/平台凭据、禁用外部 pytest plugin 的全量离线回归为 `393 passed`。
- not_verified: 未访问公网、真实 Provider/model、Docker/Jobe、GitHub-hosted CI 或真实生产 HTTP SSE；只使用
  本地锁定 SDK 类型、wire fixture、fake provider 和 loopback transport。未运行 R2 阶段收口入口
  `zsh scripts/accept_stage8.sh`。
- residual_risks: Provider stream 已可迭代但 Service/Agent 仍同步消费聚合结果；Responses structured text output、
  完整 CancellationToken、HTTP SSE terminal 语义、RunEvent writer fence、跨进程恢复和五崩溃窗仍未完成。Provider
  capabilities 依赖部署声明，不是远端自动探测；SDK/端点若违反已知 wire 契约会按 unknown/error 策略审计或 fail closed。
- gate: `R2.5 passed`；R2 总门禁保持 `in_progress`，不能把 Provider 真流误写成 HTTP SSE 已流式化。
- next: R2.6，将 R2.5 Provider 事件接入 HTTP SSE，映射 RunEvent v2，贯穿统一取消与 writer fence；不重复实现
  Provider 核心解析，也不把 SSE terminal 行为提前扩展到本阶段。

### R2.6 - 2026-08-22

- commit/evidence: 会话从 `72d52509ddde022e8c01d16e85ba59dc96e41f38`（`feat: add provider event streaming`，
  `main` 与 `origin/main` 同步）开始；本次未创建本地提交或推送。开始时工作区干净，当前改动均属于 R2.6，
  未覆盖用户已有文件。
- SSE/RunEvent: `EduAgentApi._stream_chat` 直接订阅 R2.5 Provider/Agent 真流并发送完整 RunEvent v2 envelope；
  SSE event id 使用单调 sequence，覆盖 accepted、text/tool-call delta、tool started/completed、plan updated、
  usage、fallback 和 completed/error。keepalive 只在队列空闲时保活。HTTP handler 是唯一 socket writer；并发
  producer 只写有界 `RunEventBus` subscription。
- writer/fence: 新增每个 run/API attempt 唯一的 `RunStreamWriter` 与 registry，取得 session lease 后绑定真实
  fencing token。writer 内锁串行化发布并拒绝旧 Provider attempt、fallback 前一 attempt 的迟到流、被替换 API
  owner、取消后 producer 与 terminal 后 delta；新 attempt 会关闭旧 writer 并取消旧 token。completed/error
  是唯一 terminal，后续事件不可进入 socket。
- cancellation/lifecycle: 新增线程安全、幂等、支持 deadline/父子传播/关闭回调的 `CancellationToken`。客户端
  断流、`POST /v1/runs/{id}/cancel` 与 deadline 走同一 token，并传入 Gateway/两种 adapter、ResilientEngine、
  Agent/Planner、ToolExecutor/事务提交检查、delegation child 和代码执行 Provider。Provider stream cancel 会关闭
  可关闭的 SDK iterator；无法强杀的同步调用在返回后检查 token 与 lease fence，禁止迟到提交。RuntimeManager
  在 token 取消时停止 heartbeat，terminal 后释放 lease；API 对慢消费者、writer/socket 异常和未停止 Provider
  使用有界队列、写超时与有界 request cleanup。
- migrations/config: 无数据库 migration、无依赖、无环境变量或 AppConfig 变更；API 新增的 buffer、keepalive、
  cleanup 与 socket write timeout 均有保守构造默认值。不新增崩溃恢复策略。
- fixtures/tests: 新增 `tests/test_api_sse_cancellation.py` 的真实 `127.0.0.1:0` 测试，覆盖 accepted/首 delta
  顺序、单调 id、plan/tool lifecycle、半段 tool JSON 断流、fallback 旧流迟到、terminal 后 delta、deadline、
  慢消费者和重复 cancel；新增 CancellationToken 直接合同、同步 sandbox 迟到结果拒绝及父 token 取消委派树测试。
- verification: `uv lock --check`、`uv pip check`、全仓 ruff 与 `git diff --check` 通过；显式清空模型/平台
  凭据并禁用外部 pytest plugin 后，API/stream/cancel/sandbox/delegation/runtime 专项为 `179 passed`，全量
  离线回归为 `409 passed`。受限沙箱内首轮专项除回环测试外 `165 passed`，7 个失败均为绑定
  `127.0.0.1:0` 时的 `PermissionError: [Errno 1] Operation not permitted`；按协议在获准本机回环环境复跑
  同组测试后全部通过。
- not_verified: 未访问公网或真实 Provider/model，未运行真实 Docker/Jobe、GitHub-hosted CI 或生产反向代理；
  未运行 R2 阶段收口入口 `zsh scripts/accept_stage8.sh`。真实 socket 与锁定 SDK/wire fixture 已验证。
- residual_risks: EventBus 仍是进程内 future-only transport，不提供跨进程或 `Last-Event-ID` 历史 replay；同步
  SDK 只能协作取消并等待自身 timeout，但返回结果会被 token/fence 拒绝。五崩溃窗、进程重开恢复决策和 R2
  独立总门禁留给 R2.7；工具仍顺序执行。
- gate: `R2.6 passed`；R2 总门禁保持 `in_progress`，不得把本阶段描述成已完成五崩溃窗恢复。
- next: R2.7，基于 R2.1-R2.6 交接完成五崩溃窗、进程重开恢复决策、演示与 R2 独立门禁；不得开始工具并发。

### R2.7 - 2026-08-23

- commit/evidence: 会话从与 `origin/main` 同步且工作区干净的
  `658237e33e46fde4ffe6d40d0f11bcbc460ac4d6` 开始；本次未创建本地提交或推送。Stage 8 生成的本机时间、
  性能和 dirty-HEAD development artifact 已恢复为会话开始版本，未把瞬时数据保留为发布证据。
- recovery decisions: 新增 `RunRecoveryPlanner`、`RecoveryDecision` 与穷尽 `RunStableBoundary` 的
  `continue/replay-read/reuse-operation/manual-review/terminal-replay` 表。非终态 run 缺 journal、损坏/未知
  boundary、非法预算、无法分类的 effect 或未知 operation 状态均 fail closed；未配对只读结果可重放，写调用
  只查询已有 operation。`prepared/approved/failed` 沿原幂等事务恢复，`committed` 只复用回执，
  `executing/compensating/compensated/manual_review` 禁止自动执行。startup、普通 resume 和 API resume 均记录
  中心脱敏决策 Trace。
- frozen recovery identity: resume 前重新计算当前工具面与脱敏 Provider route，必须与 journal 的冻结
  manifest hash/route 一致；model/tool 计数及两个上限从持久预算 snapshot 恢复，非法或超限 snapshot 进入
  `manual-review`，不会用新 Service 默认配置归零。terminal finalizer 重建兼容 `ChatResult`，API request 仍按
  原 response hash 字节重放。
- process/fence: 新增 `runs.stream_event_sequence` 持久高水位；`RunStreamWriter` 在事件可见前原子预留
  sequence，每次 publish 都重新验证 run scope、当前 lease 和 fence。新进程从 stream/journal 最大高水位继续，
  旧进程内 writer 在接管后下一次发布被拒绝；terminal replay 只为已终态 run 使用 token `0` 生成新 envelope，
  EventBus 仍不保存历史 delta。
- five crash windows: `SimulatedProcessCrash(BaseException)` 绕过进程内异常收尾，分别注入模型返回后、assistant
  envelope 后、只读 result 后、写 operation commit 后和 final message 后。每个 fixture 都关闭第一个 Service，
  推进 lease 时钟，再用同一持久 SQLite 构造新 Service；验证 call/result 完整配对、final 唯一、event sequence/
  cursor 单调、route/manifest/budget 不漂移、旧 writer/fence 失效、写副作用唯一和 terminal replay 等价。
- migration/scripts/docs: 新增幂等 `012_r2_recovery` 并将 SQLite `PRAGMA user_version` 提升到 12；旧库列探测与
  migration marker 可重入，未来 schema 继续拒绝降级。新增只使用 Service 决策/resume/run status/Trace API 的
  `scripts/r2_recovery_demo.py`，输出中心脱敏决策、invariants 和 Trace，不查询内部表解释结果。新增内部
  `scripts/accept_r2.sh`，由唯一公开完整入口 `accept_stage8.sh` 调用；同步 RunJournal、architecture、production
  runtime、roadmap、README 和现场演示，只将已验证能力改成当前实现。
- verification: 全仓 `uv run --frozen --offline ruff check .` 通过；RunJournal/工具消息/finalizer/事务/五窗故障组
  `88 passed`；Chat Completions 与 Responses 两种真实 SDK wire fixture `2 passed`；显式清空模型/平台凭据、
  禁用外部 pytest plugin 的全量离线回归在获准回环环境 `420 passed (24.95s)`；真实 SSE/API socket 专项
  `22 passed (7.31s)`；最终独立 R2 gate 为 `141 passed (11.02s)` 且恢复 demo 全部 invariants 为 true。
  `zsh scripts/accept_stage8.sh` 最终通过：前置 lineage/acceptance/API recovery 组 `31 passed`、内嵌 R2 gate
  `141 passed`、observability 边界 `11 passed`、最终全量 `420 passed (23.85s)`，lineage 与两次敏感边界审计
  均无 finding；临时状态成功清理。
- not_verified: 未访问公网或真实 Provider/model，未运行 GitHub-hosted CI；Stage 8 未发现已启动的真实
  Docker/Jobe 后端，按既有合同记录 `sandbox=not_verified`。两种锁定 SDK/wire fixture 和真实本机 socket 已
  通过；真实 Provider 网络流本来就不是 R2 离线门禁，未被写成 verified。
- residual_risks: EventBus 仍是单进程 future-only transport，不提供 `Last-Event-ID` payload 历史或跨主机
  协调；同步 SDK 只能协作取消并依赖 timeout，迟到提交由 token/fence 阻止。当前工具调用仍严格顺序执行，
  未实现 R3 的不可变 ToolManifest 元数据扩展、参数规范化或安全并发。
- gate: `R2 passed`；R0-R2 顶层 stage gate 为 `passed`。
- next: R3.1，读取本交接、工具 registry/schema/插件/MCP、Plan 工具裁剪与 RunJournal manifest hash，冻结 run 级
  `ToolManifest`；保持工具顺序执行，不提前开始 R3.5 并发。

### R3.1 - 2026-08-23

- commit/evidence: 会话从 R2 gate=passed、工作区含本会话未提交改动的状态开始；本次未创建本地提交或推送，
  未覆盖用户既有改动。未新增 SQLite migration、配置项或依赖。
- inventory: 盘点并为 16 个内置工具建立显式 `source/version/schema_hash/capability/risk/effect/parallel_safe/`
  `resource_keys/timeout/allowed_roles/data_classification`；`retrieve_course_materials` 仅在 RAG provider 可用时
  附加，`run_code` 仅在隔离执行 provider 健康且能力完整时附加。工具执行仍严格按原 assistant call 顺序。
- manifest: 新增不可变 `ToolManifestEntry`/`ToolManifest` 和确定性 canonical JSON/schema hash。manifest hash 绑定
  actor、tenant、role、course scope 与按名称规范化的完整条目；Entry schema/分类/集合均冻结。Plan 的
  `allowed_tools` 只收窄已冻结面，`build_agent` 拒绝未冻结或改写 schema 的工具列表。
- registration: registry admission 完整校验 JSON Schema、同步 handler(conn, **parameters) 契约、稳定 name/source/version、
  resource pointer/template、字段分类、effect/risk/mutation/parallel 组合和冲突。未知插件默认 `critical`、
  `parallel_safe=false` 且 capability 缺失时不暴露；插件裸写/code-execution effect fail closed。健康探针异常、
  代码后端不完整或 RAG 暂不可用均隐藏/拒绝工具，不靠工具名推断 effect。
- runtime/recovery: Service/Agent run start 冻结 manifest，RunJournal 的 `tool_manifest_hash` 与
  `tool_manifest.frozen` audit/Trace 同时记录 hash、scope 和脱敏元数据；executor 每次复验 manifest membership、
  live schema/metadata/handler、ACL、course scope、审批和健康状态。插件注册、handler 替换、RAG/代码健康变化不能
  改变既有 run 工具面。恢复 hash 不符直接拒绝；仅对 R2 旧 schema-list hash 做显式、可审计的
  `tool_manifest.compatibility/legacy_schema_hash_accepted` 兼容决策，新 run 使用 richer manifest hash。
- tests/verification: `uv lock --check`、`uv run --frozen --offline uv pip check`、全仓 ruff 和 `git diff --check` 通过；
  ToolManifest/registry/Plan/MCP/RAG/plugin/code-execution/runtime/transaction/recovery 及扩展边界专项最终
  `184 passed`；获准本机 loopback 复跑 `tests/test_stage8_boundaries_recovery_trace.py` 为 `13 passed`；
  `zsh scripts/accept_stage8.sh` 完整通过，R2 内门禁 `141 passed`，最终全量离线回归 `439 passed`，数据边界和
  eval lineage audit 均无 findings。
- not_verified: 未访问公网或真实模型/Provider，未启动 Docker/Jobe；这些不属于本切片的离线通过条件。未迁移 SQLite
  工具、未实现参数规范化/Repair、未实现任何工具并发，仍留给后续 R3 提示词。
- residual_risks: R2 旧 schema-only hash 无法表达历史 metadata；当前只在 hash 与现行 schema list 完全匹配时接受，
  并写明确 compatibility audit，后续可在 R3.6 收口时淘汰该兼容分支。MCP 远端工具必须提供完整 metadata/hash，
  否则不进入 manifest。`ToolManifest` 只冻结当前进程 provider identity，跨主机 registry 发布/版本治理仍未实现。
- gate: `R3.1 passed`；R3 总门禁保持 `in_progress`，R0-R2 顶层 stage gate 仍为 `passed`。
- next: R3.2，建立 Canonical Teaching Provider，只迁移只读查询/分析/知识图谱切片；继续不做参数修复、并发或事务写工具迁移。

### R3.2 - 2026-08-23

- commit/evidence: 会话从与 `origin/main` 同步且工作区干净的
  `52ae6835e0e09fbaab7fefc5b984b1967fe23c3a` 开始；本次未创建本地提交或推送，未覆盖用户既有改动。
  未新增 SQLite migration、配置项或依赖，也未读取本机教学平台数据库或引入私有 DDL/生产数据 fixture。
- canonical contract: 新增独立的 `edu_agent.teaching` 教学数据边界，定义 10 种 `TeachingQueryKind`、
  `TeachingScope`、`PageRequest`、JSON-only `TeachingResult`、`TeachingProviderError` 及稳定的
  `invalid_query/not_found/scope_denied/unavailable/internal` 分类。结果构造会复制并拒绝存储专有对象；底层
  异常只保留异常类，不回显 SQL、表名或生产 ORM。模块和架构文档明确它不同于 R1 模型
  `ProviderGateway`，也不同于带 tenant/course/citation 语义的课件 `KnowledgeProvider`。
- synthetic provider: registry 默认持有 `SyntheticProvider(db.connect)`；成绩、考试、班级名单、题目、学习进度、
  错题、薄弱点、成绩分布、知识图谱和学习路径均移到该实现。普通调用每次通过 connection factory 获取并关闭
  自己的 `sqlite3.Connection`，线程测试验证四次 worker 调用得到四个独立连接；只有调用方显式传入受控连接时
  才复用且不关闭。Provider 内对显式 course 和仅携带 `exam_id` 的间接资源再次执行 scope 校验，所有列表增加
  确定 tie-breaker，状态统一映射但保持原工具 JSON 字段。
- tool/runtime compatibility: `query_tools`、`analysis_tools`、`kg_tools` handler 已薄化为 schema/context -> canonical
  query -> 原工具 JSON；registry 对这 10 个只读工具不再预开、提交或在线程间传递连接。Agent 图、
  ToolManifest/schema/hash、MCP、Plan/Evidence 和原 ACL/role/course 二次鉴权接口未感知 SQLite。RAG 检索与 citation
  生命周期继续走既有 `SQLiteKnowledgeProvider`，Evidence claim/citation 验证保持通过。仅被尚未迁移写工具使用的
  `_resolve_kp_uid` 明确保留到 R3.3；`generate_paper`、写工具、条件写入和代码执行均未迁移。
- contract tests: 新增共享 `TeachingProviderContract`，同一组 10 个只读用例同时运行 SyntheticProvider 与测试内
  纯 fake adapter；覆盖分页、空结果、考试状态映射、直接/间接 course scope 拒绝、
  `invalid/not_found/scope_denied` 分类、JSON canonical 边界和重复调用确定顺序。fake 只返回固定 canonical 数据，
  不含真实平台命名、网络或存储逻辑；另测 registry 替换 adapter 后 manifest hash 不变、context scope 仍下传，
  以及 SQLite unavailable 错误不泄露底层详情。
- verification: 改动前只读/RAG/Plan/Agent 基线 `56 passed`；最终 contract 专项 `10 passed`；只读工具、RAG/citation、
  Plan/Evidence、Manifest、MCP、Agent/Runtime、委派、eval 和事务防回归专项 `136 passed (9.10s)`；显式清空真实
  模型/平台凭据并禁用外部 pytest plugin 的全量离线回归 `449 passed (27.93s)`。`uv lock --check`、
  `uv pip check`、全仓 ruff 和 `git diff --check` 通过；对现有 `artifacts` 的只读数据边界审计扫描 3 个文件且
  `findings=[]`。
- not_verified: 未访问公网、真实模型、真实教学平台、生产 ORM/API 或 GitHub-hosted CI，未启动 Docker/Jobe；
  本阶段明确不实现 `TeachingPlatformProvider`。按通用协议，R3.2 不是阶段收口会话，未重复运行完整
  `zsh scripts/accept_stage8.sh`，不能用本次专项/全量 pytest 冒充 R3 总门禁。
- residual_risks: 当前 teaching contract 只覆盖关系查询、分析和图路径；写/条件写回执、幂等、outbox 与补偿仍需
  R3.3 在既有 `ToolOperation` 事务边界内收口。班级名单本身没有 course 参数，仍依赖既有角色 ACL 和平台未来的
  实体授权映射；SyntheticProvider 不虚构生产租户表。工具仍按 assistant call 顺序执行，尚未实现 R3.4 参数
  normalizer 或 R3.5 安全并发。
- gate: `R3.2 passed`；R3 总门禁保持 `in_progress`，R0-R2 顶层 stage gate 仍为 `passed`。
- next: R3.3，读取本交接和事务/写工具实现，为剩余教学工具建立 canonical command/receipt/error；保留审批、
  幂等、同库事务、outbox/补偿，不做参数修复或工具并发，也不把 `run_code` 塞入教学数据 Provider。

### R3.3 - 2026-08-23

- commit/evidence: 会话从与 `origin/main` 同步且工作区干净的
  `9f1dada6ba3fa1d27d0c2ad8378714c73641693f` 开始；本次未创建本地提交或推送，未覆盖用户既有改动。
- canonical commands: 新增 `TeachingCommandKind/Effect`、`TeachingCommand`、executor 签发的
  `TeachingOperationContext`、`TeachingReceipt/TeachingCommandResult` 和结构化 command 错误；
  `create_exam`、`generate_paper`、`batch_grade`、`assign_homework`、`generate_questions` 的稳定 SQL 实现迁入
  `SyntheticProvider`，原工具 handler 只保留 schema/context 到 canonical command/原 JSON 的薄映射，并删除
  已被 Provider 取代的重复 SQL 调度与 `_resolve_kp_uid`。
- write safety: 恒定写入和保存题库分支只能经 `PolicyToolExecutor -> dispatch_transactional`；Provider 在同一
  教学库连接内复验 operation executing 状态、tool/payload hash、idempotency key、approval scope 和未过期审批，
  自身不 commit。业务拒绝通过 typed error 穿过事务层并回滚；原 operation/outbox、commit 后回执重放、补偿和
  `manual_review` 状态机保持不变。SyntheticProvider 与 contract fake 的直接写调用均无法绕过 executor。
- effects/boundaries: 参数先经冻结 schema 校验，再由 `save_to_bank` 判定 `generate_questions` 是 pure 还是 write；
  `generate_paper` 是不落库的 read command。`run_code` 保持独立 `CodeExecutionProvider` capability，不创建教学库
  连接。新增正式 `ToolResult` 名称并保留 `ToolOutcome` 兼容别名，16 个内置工具均经统一
  `ToolProvider -> ToolResult` 边界；替换教学 contract fake 不改变 Agent 图或 ToolManifest，MCP 本地/远端返回
  形状与写入拒绝回执保持兼容。
- contracts/docs: 新增 16 工具 capability/effect/boundary 矩阵，并覆盖成功、业务拒绝、缺审批、重复 request/
  idempotency、commit 后崩溃恢复、`manual_review`、outbox 重投消费去重、直接与间接 course scope、fake Provider
  安全门和 Agent 替换。架构文档记录未来 `TeachingPlatformProvider` 所需 query/analysis/knowledge/write/content
  capability 映射，本阶段未实现真实连接。
- migrations/config: 无数据库 migration、依赖、环境变量或 AppConfig 变更；未实现 R3.4 参数修复或 R3.5 并发。
- verification: 最终工具、事务、MCP、Agent 工具消息、Plan/Evidence/RAG、Teaching Provider、16 工具矩阵、
  代码执行与 ToolManifest 显式组合回归 `159 passed (9.20s)`；显式清空真实模型/平台凭据、禁用外部 pytest
  plugin 的全量离线回归 `469 passed (28.81s)`。
  `uv lock --check`、`uv run --frozen --offline uv pip check`、全仓 ruff 和 `git diff --check` 均通过；只读数据
  边界审计扫描 3 个 artifact，`findings=[]`。
- not_verified: 未访问公网、真实模型、真实教学平台/生产 ORM/API、Docker/Jobe 或 GitHub-hosted CI。R3.3 不是
  阶段收口会话，按通用协议未运行完整 `zsh scripts/accept_stage8.sh`；不能把 synthetic/fake 合同测试写成真实
  TeachingPlatform 集成已验证。
- residual_risks: 未来平台适配仍需将业务 request/idempotency key、固定 receipt、实体级 scope、outbox/补偿能力
  映射到真实 API，并明确不支持同库事务时的失败与恢复语义。当前工具仍严格顺序执行；参数规范化/repair audit、
  并发资源冲突和远端插件/MCP 最终收口分别留给后续 R3 会话。
- gate: `R3.3 passed`；R3 总门禁保持 `in_progress`，R0-R2 顶层 stage gate 仍为 `passed`。
- next: R3.4，建立 schema-guided 参数规范化、单次 repair 与审计；保持工具顺序执行，不提前实现 R3.5 并发。

### R3.4 - 2026-08-23

- commit/evidence: 会话从与 `origin/main` 同步且工作区干净的
  `348f9582ff152f8b7297007686d5487cb974d27b` 开始；本次未创建本地提交或推送，当前改动均属于 R3.4。
- argument contract: 新增三层参数管线：有界严格 JSON object 解析、字段 Schema 通过
  `x-edu-agent-normalize` 显式授权的单遍确定性规范化、以及锁定 `jsonschema` 的完整 Draft 2020-12 语义校验。
  object Schema 默认递归补 `additionalProperties: false`，动态 map 必须提供 schema；重复 key、非 object 根、
  NaN/Infinity、bool-as-number、非 JSON Python 值、循环引用、未知字段和超字节/深度/节点/容器输入均 fail closed。
- normalization policy: 文档化允许转换表和禁止猜测表。只读/pure 字段可显式开放严格十进制 string -> integer/number、
  精确小写 string -> boolean、严格 JSON string -> array/object；ID、学号、自由文本、enum、日期、null/default 和
  malformed JSON 不猜。有限安全范围的积分 JSON number 在普通 `type: integer` 字段规范为 Python `int`，但仍受
  effect、ID、enum、敏感字段和审批语义策略约束。write/conditional-write/code/unknown 等 effect 不执行 repair；
  `allOf/anyOf/oneOf`、条件、引用定义和 `patternProperties` 内的 normalization 声明在注册期拒绝，避免分支猜测。
- execution/audit boundary: 规范化和完整校验在条件写实际 effect、resource key、ACL/审批、payload hash、
  approval scope、idempotency key、ToolOperation 和 handler 之前完成。每个 call id 最多记一次 argument retry/repair 预算；每个候选
  转换记录 JSON Pointer、源/目标类型、rule id、结果和 canonical SHA-256，不保存原值。失败参数只持久化类型/大小/hash
  摘要；成功参数使用已经验证的冻结 Manifest 字段分类脱敏，避免 provider 状态变化让 repair audit 或 Trace 泄漏正文。
- corpus/tests: 新增 26 条静态坏参数 corpus，并以程序化用例覆盖超大/超深/超宽、规范化后组合深度、Unicode、前导零、
  required/null、边界与排他范围、嵌套未知字段、恶意/重复/trailing JSON、非 JSON Python 值、循环引用、写/条件写严格
  策略、resource key、确定性、单 call retry 上限、敏感审计和 handler 零未验证调用。参数专项最终 `54 passed`；
  参数/工具/Manifest/16 工具矩阵/事务/Agent/工具消息/Trace/API 组合在受限环境 `153 passed`，另 4 项仅因禁止绑定
  `127.0.0.1:0` 失败，获准环境复跑 `4 passed`。
- migrations/config: 无数据库 migration、环境变量或 AppConfig 变更；新增显式运行时依赖 `jsonschema>=4.23` 并更新
  `uv.lock`，避免依赖偶然由其他包传递提供。没有实现工具并发。
- verification: 显式清空真实模型/平台凭据并禁用外部 pytest plugin 的最终全量离线回归，在受限环境为
  `511 passed, 12 failed`；12 项堆栈均止于本机回环 `socket.bind` 的 `PermissionError`，获准环境逐项复跑
  `12 passed`，当前代码逻辑总计 `523/523` 通过。`uv lock --check` 解析 91 包，`uv pip check` 检查 62 包且无冲突；
  全仓 Ruff 和 `git diff --check` 通过。lineage 两次确定生成均为 73 样本、hash
  `40a0a59d5d909c425c995bbc5d267934911af7047d768594a34e9a27954a7b0b`，Train/Dev/Test 为 55/12/6，全部检查通过；
  最终数据边界审计扫描 3 个既有 artifacts 与本次 lineage 输出，共 4 个文件且 0 findings。
- not_verified: 未访问公网、真实模型/Provider、真实教学平台/生产 ORM/API、Docker/Jobe 或 GitHub-hosted CI。
  R3.4 不是阶段收口会话，按通用协议未运行完整 `zsh scripts/accept_stage8.sh`；synthetic/fake 与本机回环证据不能写成
  真实平台或托管 CI 已验证。
- residual_risks: 当前 normalization 扩展刻意不解析实例相关的组合/引用/pattern 分支；这些位置仍由完整 JSON Schema
  校验，但若未来确需转换，必须先设计可静态证明的 schema resolution 合同。工具调用继续严格按 assistant 原顺序执行，
  尚未实现 worker 上限、连续 read segment、resource conflict barrier、并发预算原子化或结果顺序重排。
- gate: `R3.4 passed`；R3 总门禁保持 `in_progress`，R0-R2 顶层 stage gate 仍为 `passed`。
- next: R3.5，实现连续 segment 的安全有界只读并发、资源冲突 barrier、独立连接/取消/预算传播和原序结果提交；
  不并发 write、conditional-write、approval、code、interactive 或 unknown 工具。
