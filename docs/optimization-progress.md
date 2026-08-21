# Optimization Progress

## Current

- last_completed_prompt: R0.3
- next_prompt: R0.4
- baseline_commit: 8c645099ce27b9a3f00c5ea755ab3108c8f67dad
- stage_gate: not_passed
- stage_gate_reason: R0.4 的 73 条 stable lineage、split 泄漏门禁、CI 接线和唯一 Stage 8 入口已在本地
  通过；但 R0.1-R0.4 实现仍未提交，development artifact 的 commit 指向不包含这些改动的旧 HEAD 且
  `git.dirty=true`，不满足“本次实现 commit 可追溯”，故不得标记 R0 passed 或进入 R1

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
| 真实 Git 根与评测 commit provenance | not_met_for_r0_close | 共享 provenance 只读真实 Git；candidate/release 拒绝无 Git、dirty 或状态不可判定；伪造环境变量测试通过；R0.3 clean snapshot 曾证明 gate 可通过 | 本次 R0.1-R0.4 改动未提交；当前 system/Trace artifact 只能记录旧 HEAD `8c64509`、`dirty=true`、development，旧 commit 不包含本次实现 | 获得提交授权后提交相关改动，在 clean commit 上重跑 candidate/CI 证据 | dirty development artifact 不能证明当前实现属于所记录 commit |
| 干净 clone 到验收一条命令 | met_locally | Stage 8 自动准备；R0.3 以无 `.venv`/数据库/缓存的临时 Git snapshot frozen 安装 62 包并跑完 candidate eval/audit；R0.4 当前工作区唯一入口再次退出 0 | 当前 R0.4 改动尚无 clean commit 可供 clone；GitHub 托管 runner 尚未实际执行 | 提交后由同一 workflow/Stage 8 在 clean commit 复核 | 首次依赖准备仍需要包源网络；缓存不是正确性前提 |
| 单一公开完整验收入口 | met_locally | README、architecture、demo-script 只公开 Stage 8；动态测试证明它调用 Stage 7 一次、失败上抛、全量 pytest 只跑一次；R0.4 完整 `zsh scripts/accept_stage8.sh` 退出 0 | 后续阶段仍需持续保留文档/调用图契约 | 继续运行 `tests/test_acceptance_scripts.py` | 新增阶段若绕过契约测试可能再次分叉 |
| 固定 Python、lockfile 与依赖兼容 | met_locally_ci_contract | 单一 Ubuntu 24.04/Python 3.12；uv 固定 0.11.16；`uv lock --check` 解析 91 包，frozen sync 安装 62 包，`uv pip check` 0 冲突；三个 action 固定到远端解析的 40 位 SHA | GitHub 托管执行尚未观察 | R0.4 继续以同一 workflow 为 CI 真相源 | 包源/Actions 服务可用性是外部条件，cache miss 不改变命令语义 |
| Secret-free CI 与供应链门禁 | met_by_contract | `.github/workflows/ci.yml` 清空模型/平台凭据，checkout 不持久化 token；offline ruff/pytest/lineage/system eval/Trace/audit 顺序成立；CI 静态契约测试和本地同序 clean snapshot 通过 | GitHub 托管 job 尚未实际运行 | 提交后观察首次远程 run，不扩 Python/OS 矩阵 | 本地 macOS 验证不能代替 Ubuntu runner 实测，故托管 CI 保持 not_verified |
| CI 不依赖 `.venv`、本机 DB、API key 或预生成 `edu.db` | met_locally | workflow 在 sync 前拒绝 `.venv` 和任意预存数据库；临时 snapshot 不含这些输入；全量命令显式 mock/local、空凭据、无 Docker，并在 sync 后 uv offline 运行 | GitHub 托管 clean-room 尚未实测 | R0.4 复核首次远程运行 | 未来测试若新增外部网络路径，CI 契约测试和空凭据仍需同步扩展 |
| 评测报告 hash 与未运行口径 | met_locally | system v4/Trace v2 共用 provenance；config hash 绑定 seed/model/mode、lock、实现源 hash和 lineage manifest hash；三份 artifact 审计 0 findings；oracle=`harness_only`、real model=`not_run`、sandbox=`not_verified` | 当前仓库 artifact 是 dirty development snapshot，不是候选证据 | clean commit 后生成 candidate artifact | 未经 candidate gate 的本地 JSON 只能作为开发证据 |
| Train/Dev/Test 按模板族隔离的 lineage | met_locally | 73 条现有合成样本在族定义时分为 Train 55/Dev 12/Test 6；历史 DPO/原题等价族同归 Train；Test 用 seed 314 和人工复核的新意图；两次生成 hash 相同，跨 split sample/query、模板族、语义组、缺 provenance、敏感字段和非确定生成反例均会失败 | 语义等价组依赖受审计的显式标注，自动化不能发现所有自然语言改写；当前实现尚未提交 | 保持 lineage 审计为 CI/Stage 8 强制门禁，任何模板新增先归族再入 split | 后续若错误标注新模板的 semantic group，精确 hash 检查不能代替人工语义复核 |
| R0 总门禁 | not_passed | CI 配置契约成立；唯一 Stage 8 本地退出 0；lineage manifest `163e5d2…` 无跨 split 泄漏；全量 195 tests 与 ruff/审计通过 | 本次实现未进入可追溯 commit，当前 artifact 的旧 HEAD 与 dirty 状态不能证明源码版本；托管 CI、Docker 和真实模型仍未运行，其中后三者按 R0 口径是诚实未验证项，不混入离线失败 | 停在 R0.4；提交后在 clean commit 上重跑 candidate/CI 门禁，满足后才改为 passed 和 R1.1 | 现在进入 R1 会让 R0 证据无法对应源码 commit |

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

- commit/worktree: `main` 与 `origin/main` 仍共同指向
  `8c645099ce27b9a3f00c5ea755ab3108c8f67dad`；R0.1-R0.4 相关源码、CI、文档和 artifacts 均在未提交
  dirty 工作区中。本会话未收到“为我提交”指令，故没有创建 commit 或 push，也没有把旧 HEAD 冒充为
  包含当前实现的 provenance。
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
- verification: 最终 lineage/eval 专项 17 passed；acceptance 契约专项 9 passed；全仓 ruff 0 diagnostics；
  显式空凭据、mock/local、offline 全量中间复核 194 passed，最终版由 Stage 8 内再次全量运行 195 passed；
  `uv lock --check` 解析 91 包，已装 62 包 `uv pip check` 无冲突。lineage 两次生成均为 73 条（Train
  55/Dev 12/Test 6），生成 hash 均为
  `40a0a59d5d909c425c995bbc5d267934911af7047d768594a34e9a27954a7b0b`，manifest hash 为
  `163e5d232270403fb846d61135fae000d5ba3d67705162d7588dbde27a68ab43`，全部检查通过。完整
  `zsh scripts/accept_stage8.sh` 最终退出 0：Stage 8 专项 30 passed、Stage 7 observability 11 passed、最终
  全量 195 passed，10k Trace 3/3 assertions true，三份 artifact 数据边界审计 3 files/0 findings。
  system v4、lineage、Trace artifact SHA-256 分别为 `0085801b…`、`bc21f4ec…`、`ff685e47…`；Dev 的
  candidate/release 请求在模型执行和文件写入前以退出码 2 拒绝。
- not_verified: `docker ps` 因 Docker socket 不存在而失败，system report 保持 `sandbox=not_verified`；未发
  真实模型请求，`real_model=not_run`；semantic provider 未启用；GitHub-hosted Ubuntu CI 尚未因改动未
  commit/push 而运行。这些外部/在线项没有计入离线失败或伪装成通过。
- residual_risks: 当前 development artifacts 诚实记录旧 HEAD、`git.dirty=true`、provenance gate
  `not_enforced`，不能作为当前实现的 candidate/release 证据；自然语言等价语义仍需维护者正确归组，hash
  只能自动发现精确内容/声明重叠。R0 总门禁唯一直接未满足项是本次实现尚无可追溯 commit；托管 CI、
  Docker 和真实模型继续作为明确未验证项记录。
- next: 保持 R0.4，不进入 R1。获得明确提交授权后提交并推送本次相关改动，在 clean commit/CI 上重跑
  candidate provenance 与唯一 Stage 8 门禁；只有这些证据成立才将 `R0 gate=passed`，下一提示词改为 R1.1。
