# 隔离代码执行

当前实现为 `run_code` 增加了窄 `CodeExecutionProvider` 接口、Jobe HTTP 适配器和受限 Docker
Engine Provider。生产默认仍关闭；只有后端健康、能力完整且管理员明确设置
`security_attested=true` 时，Registry 才会向允许角色暴露 Schema。执行层会再次复验同一门禁。

## 已验证后端

2026-08-17 在 macOS Docker Desktop（Apple Silicon，固定 amd64 镜像经 Rosetta）上，以下镜像
通过了 `scripts/code_sandbox_demo.py --provider docker --e2e --require-all`：

```text
xiaobiny/jobe-custom@sha256:173036eb3b5cdc2a2634da0bd70eba56d22efced2b2981568359cc2c6bf63bd4
```

Docker Provider 不调用本地 `subprocess`，也不拼接 `docker run`。它通过 Docker Engine Unix
socket 创建一次性容器，并固定以下安全字段：

- 镜像必须是完整 `@sha256:<64 hex>` digest，调用方不能选择镜像、命令、用户或工作目录。
- `NetworkMode=none` 与 `NetworkDisabled=true`；当前不支持出网 allowlist，请求开启网络会拒绝。
- 无 bind/volume，rootfs 只读；唯一可写面是带大小限制的 `noexec,nosuid,nodev` tmpfs。
- 运行用户为 `65534:65534`，丢弃全部 Linux capabilities，并启用 `no-new-privileges`。
- Docker cgroup/namespace 限制内存、swap、CPU、pids；容器内再限制 CPU 时间与单文件大小。
- stdout/stderr 先进入受限 tmpfs，返回时执行单流与总 Artifact 字节预算；超限为
  `output_limit`，不会把完整结果塞进上下文或 SQLite。
- 墙钟超时、运行取消或异常路径都会 kill，随后强制删除容器。

真实 E2E 覆盖：正常执行、CPU/墙钟超时、OOM、进程数、文件大小、宿主临时文件、宿主 home、
项目 `.env`、其他 tenant Artifact、宿主 `/etc/passwd` 内容、宿主写入、路径穿越、符号链接、
默认禁网、stdout/stderr 与总 Artifact 预算、运行中取消、容器删除和无残留运行容器。

同机 Jobe `http://127.0.0.1:4010` 的语言发现和 smoke run 健康，原生资源参数也有效；但真实
E2E 发现该教学容器可以访问外网，而且 Vanilla Jobe 没有单次 run 的取消 API。因此 Jobe
Provider 的 `security_attested` 必须保持 `false`，不会进入工具面。
`deploy/code-execution/docker-compose.jobe.yml` 可作为内网部署起点，但仅禁网仍不能补齐取消能力。

## 配置

默认配置安全关闭。Docker 示例：

```toml
[code_execution]
enabled = true
provider = "docker"
image = "xiaobiny/jobe-custom@sha256:173036eb3b5cdc2a2634da0bd70eba56d22efced2b2981568359cc2c6bf63bd4"
docker_socket = "~/.docker/run/docker.sock"
docker_python_path = "/usr/bin/python3"
max_cpus = 1.0
allowed_languages = ["python"]
network_policy = "disabled"
security_attested = true
```

只有对相同 Engine、镜像 digest、平台和限制运行真实 E2E 后才能设置最后一项。更换镜像、Docker
运行方式、宿主平台或安全配置会使既有验收失效，应重新运行：

```bash
uv run --frozen python scripts/code_sandbox_demo.py --provider docker --e2e --require-all
```

Jobe token 只从 `EDU_AGENT_JOBE_TOKEN` 读取。Docker socket、镜像和 limits 是非密钥设置；源码、
stdin 和输出不会写入配置。审批记录只保存 source/stdin/expected-output hash、language、args、
limits、network policy、scope 和过期时间。

## 运行时边界

- 后端失联、健康失败、未 attested 或缺少任一必需 capability 时，`run_code` 不可见且 direct
  dispatch 也 fail closed。
- `timeout`、`memory_limit`、`output_limit`、`security_denied`、`cancelled` 都是失败 ToolOutcome，
  不能成为 Plan 完成证据。
- 大结果经现有 `ToolResultBudget` 脱敏后进入 tenant/actor/session 隔离 Artifact；读取复验 owner、
  路径和 SHA-256。
- 主 Service 通过 `RunContext.check_control` 在 Docker 轮询期间传播 cancel/fencing。子 Agent 的
  工具面仍显式关闭代码执行能力。
- 独立 MCP server 没有 actor/session 审批上下文，因此既不列出也不执行 `run_code`。

## 威胁模型与信任假设

防护对象是不受信 Python 源码，而不是已控制 EduAgent 主进程或 Docker daemon 的攻击者。安全边界
依赖 Docker Engine、宿主/VM Linux 内核、默认 seccomp、固定镜像内容及管理员配置正确。Docker
socket 对持有者近似宿主 root 权限；生产应把执行 broker 放在独立主机/VM 或受控 rootless Engine，
不能把通用 Web 进程和宿主 Docker socket 混在同一信任域。

当前仍未覆盖：内核/容器运行时 0-day、微架构侧信道、恶意管理员、镜像供应链被同 digest 哈希算法
攻破、跨主机调度共识、网络 allowlist、Windows 原生容器、Linux 裸机与 Intel macOS 的重复验收。
Apple Silicon 当前使用 amd64/Rosetta，最低内存与 pids 需要为模拟开销留余量。Linux 通常可用
`/var/run/docker.sock`；Windows named pipe 尚未实现。任何未验证平台都必须保持 attestation 关闭。

上述状态、审批、审计和 Artifact 测试只使用 SQLite；没有连接或修改 MySQL。
