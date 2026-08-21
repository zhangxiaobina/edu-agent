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
    ProviderCapabilities,
    ProviderGateway,
    ProviderMetadata,
    ProviderSpec,
    ResolvedRoute,
    RouteIdentity,
    normalize_endpoint,
)
from .mock import MockEngine
from .openai_compat import OpenAICompatEngine
from .resilient import CircuitBreaker, FailureKind, ResilientEngine, classify_failure

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
    "ProviderCapabilities",
    "ProviderGateway",
    "ProviderMetadata",
    "ProviderSpec",
    "ResolvedRoute",
    "RouteIdentity",
    "normalize_endpoint",
    "MockEngine",
    "OpenAICompatEngine",
    "ResilientEngine",
    "CircuitBreaker",
    "FailureKind",
    "classify_failure",
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
            adapter = _chat_completions_adapter(
                temperature=temperature,
                timeout=timeout,
                kwargs=kwargs,
            )
            gateway = ProviderGateway(
                adapters={ApiMode.CHAT_COMPLETIONS: adapter}
            )
            return GatewayEngine(gateway, spec, name="openai")
        spec = config.provider_spec()
        adapter = _chat_completions_adapter(
            temperature=config.temperature,
            timeout=config.timeout_seconds,
            kwargs=kwargs,
        )
        gateway = ProviderGateway(adapters={ApiMode.CHAT_COMPLETIONS: adapter})
        primary = GatewayEngine(gateway, spec, name="openai")
        fallback = None
        if config.fallback_model:
            fallback_spec = ProviderSpec(
                model=config.fallback_model,
                endpoint=config.fallback_base_url or spec.endpoint,
                api_mode=ApiMode.CHAT_COMPLETIONS,
                credential=CredentialRef("EDU_AGENT_FALLBACK_API_KEY"),
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
            failure_threshold=config.circuit_failure_threshold,
            cooldown_seconds=config.circuit_cooldown_seconds,
        )
    raise ValueError(f"未知引擎类型：{kind}")


def _chat_completions_adapter(
    *,
    temperature: float,
    timeout: float,
    kwargs: dict,
) -> ChatCompletionsAdapter:
    client = kwargs.pop("client", None)
    client_factory = kwargs.pop("client_factory", None)
    api_key = kwargs.pop("api_key", None)
    if kwargs:
        unknown = ", ".join(sorted(kwargs))
        raise TypeError(f"OpenAI-compatible engine 不支持参数：{unknown}")
    return ChatCompletionsAdapter(
        client,
        client_factory=client_factory,
        api_key=api_key,
        temperature=temperature,
        timeout=timeout,
    )
