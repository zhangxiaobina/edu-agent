from __future__ import annotations

import math
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ..engine.gateway import (
    ApiMode,
    CredentialRef,
    ProviderCapabilities,
    ProviderSpec,
)


_KNOWN_MODEL_LIMITS = {
    "qwen-plus": (131_072, 8_192),
}


@dataclass(frozen=True)
class ModelConfig:
    provider: str = "openai"
    model: str = "qwen-plus"
    base_url: str | None = None
    timeout_seconds: float = 1800.0
    temperature: float = 0.0
    max_retries: int = 2
    retry_base_delay_seconds: float = 1.0
    retry_max_delay_seconds: float = 8.0
    retry_after_max_seconds: float = 60.0
    route_max_concurrency: int = 4
    route_state_capacity: int = 128
    route_state_ttl_seconds: float = 900.0
    circuit_failure_threshold: int = 3
    circuit_cooldown_seconds: float = 30.0
    fallback_model: str | None = None
    fallback_base_url: str | None = None
    fallback_api_mode: ApiMode | str | None = None
    fallback_context_window_tokens: int | None = None
    fallback_max_output_tokens: int | None = None
    fallback_tokenizer: str | None = None
    api_mode: ApiMode | str | None = None
    vendor: str | None = None
    deployment: str | None = None
    endpoint: str | None = None
    credential_env: str = "EDU_AGENT_API_KEY"
    context_window_tokens: int | None = None
    max_output_tokens: int | None = None
    tokenizer: str | None = None

    def __post_init__(self) -> None:
        known_limits = _KNOWN_MODEL_LIMITS.get(self.model.lower())
        if known_limits is not None:
            if self.context_window_tokens is None:
                object.__setattr__(self, "context_window_tokens", known_limits[0])
            if self.max_output_tokens is None:
                object.__setattr__(self, "max_output_tokens", known_limits[1])
            if self.context_window_tokens > known_limits[0]:
                raise ValueError(
                    "model.context_window_tokens 不能超过已知 Provider 能力"
                )
            if self.max_output_tokens > known_limits[1]:
                raise ValueError(
                    "model.max_output_tokens 不能超过已知 Provider 能力"
                )
        if (
            isinstance(self.max_retries, bool)
            or not isinstance(self.max_retries, int)
            or self.max_retries < 0
        ):
            raise ValueError("model.max_retries 必须是非负整数")
        for name in (
            "route_max_concurrency",
            "route_state_capacity",
            "circuit_failure_threshold",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"model.{name} 必须是正整数")
        for name in (
            "retry_base_delay_seconds",
            "retry_max_delay_seconds",
            "retry_after_max_seconds",
            "circuit_cooldown_seconds",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value < 0
            ):
                raise ValueError(f"model.{name} 必须是有限非负数")
        if (
            isinstance(self.route_state_ttl_seconds, bool)
            or not isinstance(self.route_state_ttl_seconds, (int, float))
            or not math.isfinite(float(self.route_state_ttl_seconds))
            or self.route_state_ttl_seconds <= 0
        ):
            raise ValueError("model.route_state_ttl_seconds 必须是有限正数")
        if self.retry_max_delay_seconds < self.retry_base_delay_seconds:
            raise ValueError(
                "model.retry_max_delay_seconds 必须不小于 retry_base_delay_seconds"
            )
        if self.route_state_ttl_seconds <= self.circuit_cooldown_seconds:
            raise ValueError(
                "model.route_state_ttl_seconds 必须大于 circuit_cooldown_seconds"
            )
        if self.endpoint is not None and self.base_url is not None:
            raise ValueError("model.endpoint 与兼容字段 model.base_url 不能同时配置")
        effective_endpoint = self.endpoint or self.base_url
        spec = ProviderSpec(
            model=self.model,
            endpoint=effective_endpoint,
            api_mode=self.api_mode,
            provider=self.vendor,
            deployment=self.deployment,
            credential=CredentialRef(self.credential_env),
        )
        object.__setattr__(self, "api_mode", spec.api_mode)
        if (
            self.context_window_tokens is not None
            and self.max_output_tokens is not None
            and self.max_output_tokens > self.context_window_tokens
        ):
            raise ValueError("model.max_output_tokens 不能超过 context_window_tokens")
        ProviderCapabilities(
            streaming=True,
            context_window_tokens=self.context_window_tokens,
            max_output_tokens=self.max_output_tokens,
            tokenizer=self.tokenizer,
        )
        if self.fallback_model is None and (
            self.fallback_base_url is not None
            or self.fallback_api_mode is not None
            or self.fallback_context_window_tokens is not None
            or self.fallback_max_output_tokens is not None
            or self.fallback_tokenizer is not None
        ):
            raise ValueError(
                "model.fallback_* 字段需要 fallback_model"
            )
        if self.fallback_model is not None:
            if self.fallback_context_window_tokens is None:
                raise ValueError(
                    "model.fallback_context_window_tokens 必须为 fallback 声明已知上下文上限"
                )
            if self.fallback_max_output_tokens is None:
                raise ValueError(
                    "model.fallback_max_output_tokens 必须为 fallback 声明已知输出上限"
                )
            fallback_known_limits = _KNOWN_MODEL_LIMITS.get(self.fallback_model.lower())
            if (
                fallback_known_limits is not None
                and self.fallback_context_window_tokens > fallback_known_limits[0]
            ):
                raise ValueError(
                    "model.fallback_context_window_tokens 不能超过已知 Provider 能力"
                )
            if (
                fallback_known_limits is not None
                and self.fallback_max_output_tokens > fallback_known_limits[1]
            ):
                raise ValueError(
                    "model.fallback_max_output_tokens 不能超过已知 Provider 能力"
                )
            if self.fallback_max_output_tokens > self.fallback_context_window_tokens:
                raise ValueError(
                    "model.fallback_max_output_tokens 不能超过 fallback_context_window_tokens"
                )
            fallback_spec = ProviderSpec(
                model=self.fallback_model,
                endpoint=self.fallback_base_url or effective_endpoint,
                api_mode=self.fallback_api_mode,
                credential=CredentialRef("EDU_AGENT_FALLBACK_API_KEY"),
                capabilities=ProviderCapabilities(
                    streaming=True,
                    context_window_tokens=self.fallback_context_window_tokens,
                    max_output_tokens=self.fallback_max_output_tokens,
                    tokenizer=self.fallback_tokenizer,
                ),
            )
            object.__setattr__(self, "fallback_api_mode", fallback_spec.api_mode)
        if (
            not isinstance(self.provider, str)
            or not self.provider
            or self.provider != self.provider.strip()
        ):
            raise ValueError("model.provider 必须是非空引擎标识")

    @property
    def configured_endpoint(self) -> str | None:
        return self.endpoint or self.base_url

    def provider_spec(self, environ: Mapping[str, str] | None = None) -> ProviderSpec:
        source = os.environ if environ is None else environ
        declared = any(
            value is not None
            for value in (
                self.context_window_tokens,
                self.max_output_tokens,
                self.tokenizer,
            )
        )
        return ProviderSpec(
            model=self.model,
            endpoint=self.configured_endpoint or source.get("EDU_AGENT_BASE_URL") or None,
            api_mode=self.api_mode or source.get("EDU_AGENT_API_MODE") or None,
            provider=self.vendor or source.get("EDU_AGENT_PROVIDER") or None,
            deployment=self.deployment or source.get("EDU_AGENT_DEPLOYMENT") or None,
            credential=CredentialRef(self.credential_env),
            capabilities=(
                ProviderCapabilities(
                    streaming=True,
                    context_window_tokens=self.context_window_tokens,
                    max_output_tokens=self.max_output_tokens,
                    tokenizer=self.tokenizer,
                )
                if declared
                else None
            ),
        )


@dataclass(frozen=True)
class RuntimeConfig:
    max_model_calls: int = 12
    max_tool_calls: int = 24
    tool_batch_max_workers: int = 4
    tool_call_timeout_seconds: float = 120.0
    context_token_budget: int = 12_000
    output_token_reserve: int | None = None
    recent_message_limit: int = 80
    compression_enabled: bool = True
    compression_trigger_ratio: float = 0.7
    compression_release_ratio: float | None = None
    # A positive default prevents a checkpoint from being created when it does
    # not materially reduce the request.  Direct ContextEngine users retain
    # the legacy zero default for compatibility; service-created engines use
    # this runtime policy.
    compression_min_reclaim_tokens: int = 256
    compression_cooldown_turns: int = 1
    compression_cooldown_seconds: float = 0.0
    compression_keep_recent: int = 12
    compression_summary_max_chars: int = 4_000
    tool_result_inline_chars: int = 12_000
    tool_result_preview_chars: int = 1_500
    tool_turn_budget_chars: int = 32_000
    session_lease_seconds: float = 30.0
    session_heartbeat_seconds: float = 10.0
    run_stall_seconds: float = 90.0

    def __post_init__(self) -> None:
        if self.output_token_reserve is None:
            object.__setattr__(
                self,
                "output_token_reserve",
                min(2_048, max(1, self.context_token_budget // 4)),
            )
        for name in ("context_token_budget", "output_token_reserve"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"runtime {name} 必须是正整数")
        if self.context_token_budget < 256:
            raise ValueError("runtime context_token_budget 不能小于 256")
        if self.output_token_reserve >= self.context_token_budget:
            raise ValueError(
                "runtime output_token_reserve 必须小于 context_token_budget"
            )
        if (
            isinstance(self.compression_trigger_ratio, bool)
            or not isinstance(self.compression_trigger_ratio, (int, float))
            or not math.isfinite(float(self.compression_trigger_ratio))
            or not 0 < self.compression_trigger_ratio <= 1
        ):
            raise ValueError("runtime compression_trigger_ratio 必须在 (0, 1] 内")
        if self.compression_release_ratio is None:
            object.__setattr__(
                self,
                "compression_release_ratio",
                max(0.05, float(self.compression_trigger_ratio) - 0.15),
            )
        if (
            isinstance(self.compression_release_ratio, bool)
            or not isinstance(self.compression_release_ratio, (int, float))
            or not math.isfinite(float(self.compression_release_ratio))
            or not 0 < self.compression_release_ratio <= self.compression_trigger_ratio
        ):
            raise ValueError(
                "runtime compression_release_ratio 必须在 (0, compression_trigger_ratio] 内"
            )
        if (
            isinstance(self.compression_min_reclaim_tokens, bool)
            or not isinstance(self.compression_min_reclaim_tokens, int)
            or self.compression_min_reclaim_tokens < 0
        ):
            raise ValueError("runtime compression_min_reclaim_tokens 必须是非负整数")
        if (
            isinstance(self.compression_cooldown_turns, bool)
            or not isinstance(self.compression_cooldown_turns, int)
            or self.compression_cooldown_turns < 0
        ):
            raise ValueError("runtime compression_cooldown_turns 必须是非负整数")
        if (
            isinstance(self.compression_cooldown_seconds, bool)
            or not isinstance(self.compression_cooldown_seconds, (int, float))
            or not math.isfinite(float(self.compression_cooldown_seconds))
            or self.compression_cooldown_seconds < 0
        ):
            raise ValueError("runtime compression_cooldown_seconds 必须是有限非负数")
        if (
            isinstance(self.tool_batch_max_workers, bool)
            or not isinstance(self.tool_batch_max_workers, int)
            or not 1 <= self.tool_batch_max_workers <= 8
        ):
            raise ValueError("runtime tool_batch_max_workers 必须在 [1, 8] 内")
        if (
            isinstance(self.tool_call_timeout_seconds, bool)
            or not isinstance(self.tool_call_timeout_seconds, (int, float))
            or not math.isfinite(float(self.tool_call_timeout_seconds))
            or self.tool_call_timeout_seconds <= 0
        ):
            raise ValueError("runtime tool_call_timeout_seconds 必须是正有限数")
        if self.session_lease_seconds <= 0 or self.session_heartbeat_seconds <= 0:
            raise ValueError("runtime session lease 和 heartbeat 必须大于 0")
        if self.session_heartbeat_seconds >= self.session_lease_seconds:
            raise ValueError("runtime session heartbeat 必须小于 lease 时长")
        if self.run_stall_seconds <= self.session_lease_seconds:
            raise ValueError("runtime run_stall_seconds 必须大于 session lease 时长")


@dataclass(frozen=True)
class PlanningConfig:
    enabled: bool = True
    max_steps: int = 8
    max_step_retries: int = 2
    max_iterations: int = 12

    def __post_init__(self) -> None:
        if self.max_steps <= 0 or self.max_step_retries < 0 or self.max_iterations <= 0:
            raise ValueError("planning 配额必须为正数，max_step_retries 可以为 0")


@dataclass(frozen=True)
class MemoryConfig:
    enabled: bool = True
    max_recalled_items: int = 6
    max_item_chars: int = 800


@dataclass(frozen=True)
class KnowledgeConfig:
    enabled: bool = False
    path: str = "~/.edu-agent/knowledge.db"
    max_results: int = 5

    def __post_init__(self) -> None:
        if self.max_results <= 0 or self.max_results > 10:
            raise ValueError("knowledge.max_results 必须在 1 到 10 之间")


@dataclass(frozen=True)
class SecurityConfig:
    require_write_approval: bool = True
    allow_local_code_execution: bool = False
    default_role: str = "teacher"


@dataclass(frozen=True)
class CodeExecutionConfig:
    enabled: bool = False
    provider: str = "disabled"
    endpoint: str = "http://127.0.0.1:4000"
    image: str = ""
    docker_socket: str = "~/.docker/run/docker.sock"
    docker_python_path: str = "/usr/bin/python3"
    max_cpus: float = 1.0
    allowed_languages: tuple[str, ...] = ("python",)
    request_timeout_seconds: float = 15.0
    health_interval_seconds: float = 30.0
    max_source_bytes: int = 64 * 1024
    max_stdin_bytes: int = 64 * 1024
    max_cpu_time_seconds: int = 10
    max_wall_time_seconds: int = 15
    min_memory_mb: int = 384
    max_memory_mb: int = 1024
    max_output_bytes: int = 128 * 1024
    max_processes: int = 32
    max_file_size_mb: int = 32
    max_artifact_bytes: int = 256 * 1024
    network_policy: str = "disabled"
    token_env: str = "EDU_AGENT_JOBE_TOKEN"
    security_attested: bool = False

    def __post_init__(self) -> None:
        if self.provider not in {"disabled", "jobe", "docker"}:
            raise ValueError("code_execution.provider 必须是 disabled、jobe 或 docker")
        if self.enabled and self.provider == "disabled":
            raise ValueError("启用代码执行时必须配置真实 provider")
        if (
            self.provider == "docker"
            and re.fullmatch(r".+@sha256:[0-9a-fA-F]{64}", self.image) is None
        ):
            raise ValueError("Docker code_execution.image 必须固定到 sha256 digest")
        if self.docker_python_path != "/usr/bin/python3":
            raise ValueError("Docker provider 只允许固定容器 Python 路径")
        if not self.allowed_languages or any(not str(item).strip() for item in self.allowed_languages):
            raise ValueError("code_execution.allowed_languages 不能为空")
        if self.network_policy != "disabled":
            raise ValueError("当前代码执行 provider 只允许默认禁网")
        limits = (
            self.request_timeout_seconds, self.health_interval_seconds,
            self.max_source_bytes, self.max_stdin_bytes, self.max_cpu_time_seconds,
            self.max_wall_time_seconds, self.min_memory_mb, self.max_memory_mb,
            self.max_output_bytes,
            self.max_processes, self.max_file_size_mb,
            self.max_artifact_bytes,
            self.max_cpus,
        )
        if any(float(item) <= 0 for item in limits):
            raise ValueError("code_execution limits 必须大于 0")
        if self.min_memory_mb > self.max_memory_mb:
            raise ValueError("code_execution.min_memory_mb 不能超过 max_memory_mb")


@dataclass(frozen=True)
class ObservabilityConfig:
    """Local-first trace settings. OTLP is opt-in and failure-isolated."""

    otel_enabled: bool = False
    otlp_endpoint: str | None = None
    trace_page_size: int = 100
    export_preview_chars: int = 512

    def __post_init__(self) -> None:
        if self.trace_page_size <= 0 or self.trace_page_size > 500:
            raise ValueError("observability.trace_page_size 必须在 1 到 500 之间")
        if self.export_preview_chars <= 0:
            raise ValueError("observability.export_preview_chars 必须大于 0")
        if self.otel_enabled and not self.otlp_endpoint:
            raise ValueError("启用 OTLP 时必须提供 observability.otlp_endpoint")


@dataclass(frozen=True)
class ApiConfig:
    request_lease_seconds: float = 330.0
    request_retention_seconds: int = 7 * 24 * 60 * 60
    failed_request_retention_seconds: int = 24 * 60 * 60
    request_gc_batch_size: int = 200

    def __post_init__(self) -> None:
        if self.request_lease_seconds <= 0:
            raise ValueError("api.request_lease_seconds 必须大于 0")
        if self.request_retention_seconds <= 0 or self.failed_request_retention_seconds <= 0:
            raise ValueError("api request retention 必须大于 0")
        if self.request_gc_batch_size <= 0 or self.request_gc_batch_size > 10_000:
            raise ValueError("api.request_gc_batch_size 必须在 1 到 10000 之间")


@dataclass(frozen=True)
class StorageConfig:
    state_path: str = "~/.edu-agent/state.db"
    artifact_path: str | None = None


@dataclass(frozen=True)
class SchedulerConfig:
    enabled: bool = False
    lease_seconds: int = 300


@dataclass(frozen=True)
class TransactionConfig:
    approval_ttl_seconds: int = 900
    outbox_lease_seconds: int = 30

    def __post_init__(self) -> None:
        if self.approval_ttl_seconds <= 0 or self.outbox_lease_seconds <= 0:
            raise ValueError("transaction 租约和审批有效期必须大于 0")


@dataclass(frozen=True)
class DelegationConfig:
    enabled: bool = True
    max_depth: int = 1
    max_children_per_parent: int = 8
    max_concurrency: int = 3
    child_timeout_seconds: float = 30.0
    worker_lease_seconds: float = 45.0
    max_model_calls_per_child: int = 2
    max_tool_calls_per_child: int = 6
    max_tokens_per_child: int = 4_000
    max_cost_usd_per_child: float = 0.05
    max_root_model_calls: int = 16
    max_root_tool_calls: int = 48
    max_root_tokens: int = 32_000
    max_root_cost_usd: float = 0.40
    allowed_tool_categories: tuple[str, ...] = ("query", "analysis", "knowledge")
    allowed_models: tuple[str, ...] = ("deterministic-readonly-v1",)
    allowed_child_roles: tuple[str, ...] = ("student", "teacher")
    default_model: str = "deterministic-readonly-v1"
    allow_child_delegation: bool = False

    def __post_init__(self) -> None:
        if self.max_depth <= 0 or self.max_children_per_parent <= 0 or self.max_concurrency <= 0:
            raise ValueError("delegation 深度、fan-out 和并发必须大于 0")
        if self.child_timeout_seconds <= 0 or self.worker_lease_seconds <= self.child_timeout_seconds:
            raise ValueError("delegation worker lease 必须长于 child timeout")
        if self.default_model not in self.allowed_models:
            raise ValueError("delegation.default_model 必须位于 allowed_models")
        if not self.allowed_tool_categories or not self.allowed_models or not self.allowed_child_roles:
            raise ValueError("delegation 工具类别、模型和 child role 不能为空")

    def policy(self):
        from ..delegation.models import DelegationPolicy

        return DelegationPolicy(
            max_depth=self.max_depth,
            max_children_per_parent=self.max_children_per_parent,
            max_concurrency=self.max_concurrency,
            child_timeout_seconds=self.child_timeout_seconds,
            worker_lease_seconds=self.worker_lease_seconds,
            max_model_calls_per_child=self.max_model_calls_per_child,
            max_tool_calls_per_child=self.max_tool_calls_per_child,
            max_tokens_per_child=self.max_tokens_per_child,
            max_cost_usd_per_child=self.max_cost_usd_per_child,
            max_root_model_calls=self.max_root_model_calls,
            max_root_tool_calls=self.max_root_tool_calls,
            max_root_tokens=self.max_root_tokens,
            max_root_cost_usd=self.max_root_cost_usd,
            allowed_tool_categories=frozenset(self.allowed_tool_categories),
            allowed_models=frozenset(self.allowed_models),
            allowed_child_roles=frozenset(self.allowed_child_roles),
            default_model=self.default_model,
            allow_child_delegation=self.allow_child_delegation,
        )


@dataclass(frozen=True)
class AppConfig:
    model: ModelConfig = field(default_factory=ModelConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    planning: PlanningConfig = field(default_factory=PlanningConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    transaction: TransactionConfig = field(default_factory=TransactionConfig)
    delegation: DelegationConfig = field(default_factory=DelegationConfig)
    code_execution: CodeExecutionConfig = field(default_factory=CodeExecutionConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    api: ApiConfig = field(default_factory=ApiConfig)

    def __post_init__(self) -> None:
        declared_context = self.model.context_window_tokens
        if (
            declared_context is not None
            and self.runtime.context_token_budget > declared_context
        ):
            raise ValueError(
                "runtime.context_token_budget 不能超过 model.context_window_tokens"
            )
        declared_output = self.model.max_output_tokens
        if (
            declared_output is not None
            and self.runtime.output_token_reserve > declared_output
        ):
            raise ValueError(
                "runtime.output_token_reserve 不能超过 model.max_output_tokens"
            )
        fallback_context = self.model.fallback_context_window_tokens
        if (
            fallback_context is not None
            and self.runtime.context_token_budget > fallback_context
        ):
            raise ValueError(
                "runtime.context_token_budget 不能超过 fallback Provider 上下文能力"
            )
        fallback_output = self.model.fallback_max_output_tokens
        if (
            fallback_output is not None
            and self.runtime.output_token_reserve > fallback_output
        ):
            raise ValueError(
                "runtime.output_token_reserve 不能超过 fallback Provider 输出能力"
            )

    @property
    def state_path(self) -> Path:
        return Path(self.storage.state_path).expanduser()

    @property
    def artifact_path(self) -> Path:
        if self.storage.artifact_path:
            return Path(self.storage.artifact_path).expanduser()
        return self.state_path.parent / "artifacts"


def _section(data: dict, name: str, cls):
    values = data.get(name, {})
    if not isinstance(values, dict):
        raise ValueError(f"配置节 [{name}] 必须是 TOML table")
    allowed = set(cls.__dataclass_fields__)
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"配置节 [{name}] 包含未知字段：{sorted(unknown)}")
    return cls(**values)


def load_config(path: str | os.PathLike | None = None) -> AppConfig:
    config_path = Path(path).expanduser() if path else Path("~/.edu-agent/config.toml").expanduser()
    if not config_path.exists():
        return AppConfig()
    with config_path.open("rb") as config_file:
        data = tomllib.load(config_file)
    allowed_sections = {
        "model",
        "runtime",
        "planning",
        "memory",
        "knowledge",
        "security",
        "storage",
        "scheduler",
        "transaction",
        "delegation",
        "code_execution",
        "observability",
        "api",
    }
    unknown_sections = set(data) - allowed_sections
    if unknown_sections:
        raise ValueError(f"配置包含未知节：{sorted(unknown_sections)}")
    return AppConfig(
        model=_section(data, "model", ModelConfig),
        runtime=_section(data, "runtime", RuntimeConfig),
        planning=_section(data, "planning", PlanningConfig),
        memory=_section(data, "memory", MemoryConfig),
        knowledge=_section(data, "knowledge", KnowledgeConfig),
        security=_section(data, "security", SecurityConfig),
        storage=_section(data, "storage", StorageConfig),
        scheduler=_section(data, "scheduler", SchedulerConfig),
        transaction=_section(data, "transaction", TransactionConfig),
        delegation=_section(data, "delegation", DelegationConfig),
        code_execution=_section(data, "code_execution", CodeExecutionConfig),
        observability=_section(data, "observability", ObservabilityConfig),
        api=_section(data, "api", ApiConfig),
    )
