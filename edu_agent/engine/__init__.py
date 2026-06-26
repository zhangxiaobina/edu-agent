"""引擎工厂：按环境变量 EDU_AGENT_ENGINE 选择实现（默认 openai 兼容端点）。

  EDU_AGENT_ENGINE=mock    使用离线确定性 mock（需传 policy，主要供测试/编排自检）
  EDU_AGENT_ENGINE=openai  使用 OpenAI 兼容端点（通义 / vLLM / 算法仓 W4A16）
"""
from __future__ import annotations

import os

from .base import Engine, EngineResponse, ToolCall
from .mock import MockEngine
from .openai_compat import OpenAICompatEngine

__all__ = ["Engine", "EngineResponse", "ToolCall", "MockEngine",
           "OpenAICompatEngine", "get_engine"]


def get_engine(**kwargs) -> Engine:
    """根据 EDU_AGENT_ENGINE 返回引擎实例。

    mock 需通过 kwargs 传入 policy；openai 从环境变量读取端点配置。
    """
    kind = os.environ.get("EDU_AGENT_ENGINE", "openai").lower()
    if kind == "mock":
        if "policy" not in kwargs:
            raise ValueError("mock 引擎需提供 policy 参数")
        return MockEngine(kwargs["policy"])
    if kind == "openai":
        return OpenAICompatEngine(**kwargs)
    raise ValueError(f"未知引擎类型：{kind}")
