# 项目协作规则

## Git 提交约定

- 当用户将“为我提交”作为操作指令说出时，视为要求把当前工作区中本次任务相关的改动提交到 GitHub，而不只是创建本地提交。仅讨论或引用这四个字时不触发提交。
- 执行提交前，先检查工作区状态和改动内容，保留用户已有改动，不擅自还原、删除或覆盖。
- 根据改动范围运行必要的测试或检查，并确认不会提交密钥、环境变量、数据库、缓存、虚拟环境或其他不应入库的文件。
- 默认暂存本次任务相关的全部改动；用户未指定提交信息时，根据实际改动生成简洁、准确的提交信息。
- 创建本地提交后，将当前分支推送到已配置的 `origin`。本项目的 Git 网络连接使用仓库本地配置的代理。
- 推送完成后，核验本地分支与远程分支一致，并报告提交号、分支、推送结果和测试结果。
- 没有改动时直接说明，无需创建空提交。
- 遇到测试失败、疑似敏感信息、认证失败、远程未配置、合并冲突，或需要强制推送、改写历史等破坏性操作时，暂停自动流程并明确说明；未经用户明确授权，不执行破坏性操作。

## 规则维护

- 在后续开发中，如果项目结构、验证命令、部署方式或长期协作约定发生变化，应同步维护本文件。
- 本文件只记录长期有效的项目规则，不记录一次性任务状态或临时信息。

## CI 与离线验收

- CI 固定使用 Python 3.12 和锁定版本的 uv；不扩展 Python/OS 矩阵，依赖安装必须通过
  `uv lock --check`、`uv sync --frozen` 和 `uv pip check`。
- 依赖同步完成后，ruff、全量 pytest、综合评测、Trace 基准和数据边界审计必须以 `uv run --frozen --offline`
  运行；CI 不使用真实模型凭据、本机数据库、预建虚拟环境或 Docker。
- 评测样本必须携带 stable lineage；Train/Dev/Test 按意图模板族或等价语义组隔离。CI 与 Stage 8 均运行
  `scripts/audit_eval_lineage.py`，跨 split 重复、族重叠、缺 provenance、敏感字段或非确定生成必须失败。
- 本地完整门禁仍以 `zsh scripts/accept_stage8.sh` 为唯一公开入口；CI 契约见
  `.github/workflows/ci.yml`，评测报告的 candidate/release 模式必须通过真实 Git provenance 门禁。

## 进程生命周期

- API 进程必须通过 `LifecycleController` 维持 `starting/running/draining/stopped` 单调状态；`SIGTERM` 和显式
  shutdown 先进入 draining，停止接收新 chat/Scheduler claim，再按 `[lifecycle]` deadline 有界收尾。
- 探针固定为无需认证的 `/health/live` 与 `/health/ready`，只返回聚合状态；不得暴露 Provider endpoint、凭据、
  异常原文或业务数据。draining 期间 liveness 保持成功、readiness 必须失败。
- 超时停机必须先持久化未完成 run 的恢复建议并保留 session lease/fencing 边界，不能靠无限 join、提前删除 lease
  或接受迟到 worker 回调完成停机。

## API 容器部署

- API 本机部署固定参考 `deploy/api/Dockerfile`、`deploy/docker-compose.yml` 和
  `docs/production-deployment.md`；镜像多阶段、非 root、依赖必须来自 `uv.lock`，状态/Artifact/备份只能通过明确挂载持久化。
- Compose 默认只启动 API；Jobe/Docker code execution 不随 API 启动，API 容器不得挂载 Docker socket。无 Docker daemon 时，
  `scripts/container_smoke.py` 必须保留静态 `verified` 与运行项 `not_verified` 的区分，不得把静态检查写成部署验收。
