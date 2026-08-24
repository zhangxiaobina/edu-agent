"""引擎工厂：按环境变量 EDU_AGENT_ENGINE 选择实现（默认 openai 兼容端点）。

  EDU_AGENT_ENGINE=mock    使用离线确定性 mock（需传 policy，主要供测试/编排自检）
  EDU_AGENT_ENGINE=openai  使用 OpenAI 兼容端点（通义 / vLLM / 算法仓 W4A16）
"""
from __future__ import annotations

import os

from .base import Engine, EngineResponse, ToolCall
from .chat_completions import ChatCompletionsAdapter
from .gateway import (
    ApiMode,
    CredentialRef,
    GatewayEngine,
    ModeSource,
    ProviderAdapter,
    ProviderStreamAdapter,
    ProviderCapabilityError,
    ProviderCapabilities,
    ProviderGateway,
    ProviderMetadata,
    ProviderRequestRequirements,
    ProviderSpec,
    ResolvedRoute,
    RouteIdentity,
    capability_gaps,
    effective_capabilities,
    estimate_request_tokens,
    infer_request_requirements,
    normalize_endpoint,
)
from .mock import MockEngine
from .openai_compat import OpenAICompatEngine
from .responses import ResponsesAdapter, ResponsesAPIError
from .streaming import (
    ProviderStreamAggregator,
    ProviderStreamEvent,
    ProviderStreamEventType,
    ProviderStreamProtocolError,
    aggregate_provider_stream,
)
from .resilient import (
    CircuitBreaker,
    CircuitOpenError,
    FailureKind,
    ResilientEngine,
    RouteStateCapacityError,
    RouteStateRegistry,
    classify_failure,
    is_provider_context_overflow,
    parse_retry_after,
    retry_after_from_error,
)

__all__ = [
    "Engine",
    "EngineResponse",
    "ToolCall",
    "ApiMode",
    "ChatCompletionsAdapter",
    "CredentialRef",
    "GatewayEngine",
    "ModeSource",
    "ProviderAdapter",
    "ProviderStreamAdapter",
    "ProviderCapabilityError",
    "ProviderCapabilities",
    "ProviderGateway",
    "ProviderMetadata",
    "ProviderRequestRequirements",
    "ProviderSpec",
    "ResolvedRoute",
    "RouteIdentity",
    "capability_gaps",
    "effective_capabilities",
    "estimate_request_tokens",
    "infer_request_requirements",
    "normalize_endpoint",
    "MockEngine",
    "OpenAICompatEngine",
    "ResponsesAdapter",
    "ResponsesAPIError",
    "ProviderStreamAggregator",
    "ProviderStreamEvent",
    "ProviderStreamEventType",
    "ProviderStreamProtocolError",
    "aggregate_provider_stream",
    "ResilientEngine",
    "CircuitBreaker",
    "CircuitOpenError",
    "FailureKind",
    "RouteStateCapacityError",
    "RouteStateRegistry",
    "classify_failure",
    "is_provider_context_overflow",
    "parse_retry_after",
    "retry_after_from_error",
    "get_engine",
]


def get_engine(config=None, **kwargs) -> Engine:
    """根据 EDU_AGENT_ENGINE 返回引擎实例。

    mock 需通过 kwargs 传入 policy；openai 从环境变量读取端点配置。
    """
    kind = config.provider.lower() if config is not None else \
        os.environ.get("EDU_AGENT_ENGINE", "openai").lower()
    if kind == "mock":
        if "policy" not in kwargs:
            raise ValueError("mock 引擎需提供 policy 参数")
        return MockEngine(kwargs["policy"])
    if kind == "openai":
        if config is None:
            spec = ProviderSpec(
                model=kwargs.pop("model", None)
                or os.environ.get("EDU_AGENT_MODEL")
                or "qwen-plus",
                endpoint=kwargs.pop("base_url", None)
                or os.environ.get("EDU_AGENT_BASE_URL")
                or None,
                api_mode=os.environ.get("EDU_AGENT_API_MODE") or None,
                provider=os.environ.get("EDU_AGENT_PROVIDER") or None,
                deployment=os.environ.get("EDU_AGENT_DEPLOYMENT") or None,
                credential=CredentialRef("EDU_AGENT_API_KEY"),
            )
            temperature = kwargs.pop("temperature", 0.0)
            timeout = kwargs.pop("timeout", None)
            if timeout is None:
                timeout = float(os.environ.get("EDU_AGENT_TIMEOUT", "1800"))
            adapters = _openai_adapters(
                temperature=temperature,
                timeout=timeout,
                kwargs=kwargs,
            )
            gateway = ProviderGateway(adapters=adapters)
            return GatewayEngine(gateway, spec, name="openai")
        spec = config.provider_spec()
        adapters = _openai_adapters(
            temperature=config.temperature,
            timeout=config.timeout_seconds,
            kwargs=kwargs,
        )
        gateway = ProviderGateway(adapters=adapters)
        primary = GatewayEngine(gateway, spec, name="openai")
        fallback = None
        if config.fallback_model:
            fallback_spec = ProviderSpec(
                model=config.fallback_model,
                endpoint=config.fallback_base_url or spec.endpoint,
                api_mode=config.fallback_api_mode or primary.route.api_mode,
                credential=CredentialRef("EDU_AGENT_FALLBACK_API_KEY"),
                capabilities=ProviderCapabilities(
                    streaming=True,
                    context_window_tokens=config.fallback_context_window_tokens,
                    max_output_tokens=config.fallback_max_output_tokens,
                    tokenizer=config.fallback_tokenizer,
                ),
            )
            fallback = GatewayEngine(
                gateway,
                fallback_spec,
                name=f"openai:{config.fallback_model}",
            )
        return ResilientEngine(
            primary,
            max_retries=config.max_retries,
            fallback=fallback,
            retry_base_delay_seconds=config.retry_base_delay_seconds,
            retry_max_delay_seconds=config.retry_max_delay_seconds,
            retry_after_max_seconds=config.retry_after_max_seconds,
            route_max_concurrency=config.route_max_concurrency,
            route_state_capacity=config.route_state_capacity,
            route_state_ttl_seconds=config.route_state_ttl_seconds,
            failure_threshold=config.circuit_failure_threshold,
            cooldown_seconds=config.circuit_cooldown_seconds,
        )
    raise ValueError(f"未知引擎类型：{kind}")


def _openai_adapters(
    *,
    temperature: float,
    timeout: float,
    kwargs: dict,
) -> dict[ApiMode, ProviderAdapter]:
    client = kwargs.pop("client", None)
    client_factory = kwargs.pop("client_factory", None)
    api_key = kwargs.pop("api_key", None)
    if kwargs:
        unknown = ", ".join(sorted(kwargs))
        raise TypeError(f"OpenAI-compatible engine 不支持参数：{unknown}")
    common = {
        "client_factory": client_factory,
        "api_key": api_key,
        "temperature": temperature,
        "timeout": timeout,
    }
    return {
        ApiMode.CHAT_COMPLETIONS: ChatCompletionsAdapter(client, **common),
        ApiMode.RESPONSES: ResponsesAdapter(client, **common),
    }
