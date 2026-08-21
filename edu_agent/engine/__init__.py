"""引擎工厂：按环境变量 EDU_AGENT_ENGINE 选择实现（默认 openai 兼容端点）。

  EDU_AGENT_ENGINE=mock    使用离线确定性 mock（需传 policy，主要供测试/编排自检）
  EDU_AGENT_ENGINE=openai  使用 OpenAI 兼容端点（通义 / vLLM / 算法仓 W4A16）
"""
from __future__ import annotations

import os

from .base import Engine, EngineResponse, ToolCall
from .gateway import (
    ApiMode,
    CredentialRef,
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
    "CredentialRef",
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
        gateway = ProviderGateway()
        if config is None:
            spec = ProviderSpec(
                model=kwargs.get("model") or os.environ.get("EDU_AGENT_MODEL") or "qwen-plus",
                endpoint=kwargs.get("base_url") or os.environ.get("EDU_AGENT_BASE_URL") or None,
                api_mode=os.environ.get("EDU_AGENT_API_MODE") or None,
                provider=os.environ.get("EDU_AGENT_PROVIDER") or None,
                deployment=os.environ.get("EDU_AGENT_DEPLOYMENT") or None,
                credential=CredentialRef("EDU_AGENT_API_KEY"),
            )
            _require_chat_completions(gateway.begin_turn(spec))
            engine = OpenAICompatEngine(**kwargs)
            engine.configure_provider_route(spec, gateway)
            return engine
        spec = config.provider_spec()
        _require_chat_completions(gateway.begin_turn(spec))
        primary = OpenAICompatEngine(
            base_url=spec.endpoint,
            api_key=spec.credential.resolve() or "EMPTY",
            model=config.model,
            temperature=config.temperature,
            timeout=config.timeout_seconds,
            **kwargs,
        )
        primary.configure_provider_route(spec, gateway)
        fallback = None
        if config.fallback_model:
            fallback_spec = ProviderSpec(
                model=config.fallback_model,
                endpoint=config.fallback_base_url or spec.endpoint,
                api_mode=ApiMode.CHAT_COMPLETIONS,
                credential=CredentialRef("EDU_AGENT_FALLBACK_API_KEY"),
            )
            fallback = OpenAICompatEngine(
                base_url=fallback_spec.endpoint,
                api_key=os.environ.get("EDU_AGENT_FALLBACK_API_KEY", "EMPTY"),
                model=config.fallback_model,
                temperature=config.temperature,
                timeout=config.timeout_seconds,
            )
            fallback.configure_provider_route(fallback_spec, gateway)
            fallback.name = f"openai:{config.fallback_model}"
        return ResilientEngine(
            primary,
            max_retries=config.max_retries,
            fallback=fallback,
            failure_threshold=config.circuit_failure_threshold,
            cooldown_seconds=config.circuit_cooldown_seconds,
        )
    raise ValueError(f"未知引擎类型：{kind}")


def _require_chat_completions(route: ResolvedRoute) -> None:
    if route.api_mode is not ApiMode.CHAT_COMPLETIONS:
        raise ValueError("当前 Provider adapter 仅支持 chat_completions")
