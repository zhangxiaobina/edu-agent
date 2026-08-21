"""引擎工厂：按环境变量 EDU_AGENT_ENGINE 选择实现（默认 openai 兼容端点）。

  EDU_AGENT_ENGINE=mock    使用离线确定性 mock（需传 policy，主要供测试/编排自检）
  EDU_AGENT_ENGINE=openai  使用 OpenAI 兼容端点（通义 / vLLM / 算法仓 W4A16）
"""
from __future__ import annotations

import os

from .base import Engine, EngineResponse, ToolCall
from .mock import MockEngine
from .openai_compat import OpenAICompatEngine
from .resilient import CircuitBreaker, FailureKind, ResilientEngine, classify_failure

__all__ = [
    "Engine",
    "EngineResponse",
    "ToolCall",
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
            return OpenAICompatEngine(**kwargs)
        primary = OpenAICompatEngine(
            base_url=config.base_url,
            api_key=os.environ.get("EDU_AGENT_API_KEY", "EMPTY"),
            model=config.model,
            temperature=config.temperature,
            timeout=config.timeout_seconds,
            **kwargs,
        )
        fallback = None
        if config.fallback_model:
            fallback = OpenAICompatEngine(
                base_url=config.fallback_base_url or config.base_url,
                api_key=os.environ.get("EDU_AGENT_FALLBACK_API_KEY", "EMPTY"),
                model=config.fallback_model,
                temperature=config.temperature,
                timeout=config.timeout_seconds,
            )
            fallback.name = f"openai:{config.fallback_model}"
        return ResilientEngine(
            primary,
            max_retries=config.max_retries,
            fallback=fallback,
            failure_threshold=config.circuit_failure_threshold,
            cooldown_seconds=config.circuit_cooldown_seconds,
        )
    raise ValueError(f"未知引擎类型：{kind}")
