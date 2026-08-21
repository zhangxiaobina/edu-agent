"""可替换工具调用引擎的抽象接口。

Agent 编排层只依赖本接口；具体实现可在「离线确定性 mock」与「OpenAI 兼容端点
（通义千问 / vLLM / 算法仓 W4A16 Qwen3-14B）」之间无缝切换。

统一对齐 OpenAI chat-completions 的 tool-calling 协议：engine.chat(messages, tools)
返回 EngineResponse(content | tool_calls)，与真实端点的 message 结构一致，
因此同一套 Agent 既能跑 mock 也能跑真模型。
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .gateway import ResolvedRoute


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict | str


@dataclass
class EngineResponse:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    finish_reason: str | None = None
    model: str | None = None

    def to_assistant_message(self) -> dict:
        """转成 OpenAI 格式的 assistant 消息（含 tool_calls）。"""
        msg: dict = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {
                     "name": tc.name,
                     "arguments": tc.arguments
                     if isinstance(tc.arguments, str)
                     else json.dumps(tc.arguments, ensure_ascii=False),
                 }}
                for tc in self.tool_calls
            ]
        return msg


class Engine(ABC):
    """工具调用引擎接口。"""

    name: str = "engine"

    @abstractmethod
    def chat(self, messages: list[dict], tools: list[dict]) -> EngineResponse:
        """给定对话历史与工具 schema，返回下一步（工具调用或最终回答）。"""
        raise NotImplementedError

    def begin_turn_routes(self) -> tuple[ResolvedRoute, ...]:
        """Freeze provider routes before a turn; engines without providers return none."""
        return ()
