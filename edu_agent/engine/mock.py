"""离线确定性 mock 引擎：用一个「代码大脑」(policy) 驱动 Agent 循环。

用途：在不联网、无 API key 的情况下验证 LangGraph 多工具编排循环
（模型↔工具↔模型）是否正确——尤其是能否根据上一步工具的真实返回
动态决定下一步调用。policy 是一个确定性函数，读取对话历史里累积的工具结果
来决策，因此能复现真实的「多步、依赖前序结果」轨迹。

注意：mock 不做语言理解，只是 LLM 大脑的离线替身；接入真模型后由模型自主决策。
"""
from __future__ import annotations

import json
from typing import Callable

from .base import Engine, EngineResponse, ToolCall

# policy 签名：(messages, tools, step) -> EngineResponse
Policy = Callable[[list[dict], list[dict], int], EngineResponse]


class MockEngine(Engine):
    name = "mock"

    def __init__(self, policy: Policy):
        self._policy = policy
        self._step = 0

    def chat(self, messages: list[dict], tools: list[dict]) -> EngineResponse:
        resp = self._policy(messages, tools, self._step)
        self._step += 1
        return resp


# ---------- 构造 EngineResponse 的便捷函数 ----------
def call(step: int, name: str, **arguments) -> EngineResponse:
    """发起一次工具调用。"""
    return EngineResponse(tool_calls=[ToolCall(id=f"call_{step}", name=name, arguments=arguments)])


def final(text: str) -> EngineResponse:
    """给出最终回答（结束循环）。"""
    return EngineResponse(content=text)


# ---------- 从对话历史里取最近一次某工具的返回（policy 决策用） ----------
def last_tool_result(messages: list[dict], tool_name: str) -> dict | None:
    for m in reversed(messages):
        if m.get("role") == "tool" and m.get("name") == tool_name:
            try:
                return json.loads(m["content"])
            except (json.JSONDecodeError, KeyError, TypeError):
                return None
    return None
