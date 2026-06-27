"""OpenAI 兼容端点引擎：接通义千问 DashScope / 本地 vLLM / 算法仓 W4A16 Qwen3-14B。

通过环境变量配置，不写死任何 key：
  EDU_AGENT_BASE_URL   端点(如 https://dashscope.aliyuncs.com/compatible-mode/v1
                       或 http://127.0.0.1:8000/v1)
  EDU_AGENT_API_KEY    API key（vLLM 本地可填任意占位）
  EDU_AGENT_MODEL      模型名（如 qwen-plus / Qwen/Qwen3-14B）

vLLM 起 Qwen3 时需 `--tool-call-parser hermes --enable-auto-tool-choice`，
其 /v1 接口即 OpenAI 兼容，故本适配器同时覆盖「通义 API」与「自部署 vLLM」两条路。
"""
from __future__ import annotations

import json
import os

from .base import Engine, EngineResponse, ToolCall


class OpenAICompatEngine(Engine):
    name = "openai"

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, temperature: float = 0.0,
                 timeout: float | None = None):
        try:
            from openai import OpenAI
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("需要 openai 包：uv pip install openai") from e
        self.base_url = base_url or os.environ.get("EDU_AGENT_BASE_URL")
        self.api_key = api_key or os.environ.get("EDU_AGENT_API_KEY", "EMPTY")
        self.model = model or os.environ.get("EDU_AGENT_MODEL", "qwen-plus")
        self.temperature = temperature
        # 慢端点（GB10 上 fp16 14B 单请求约几 tok/s，并发时更低）会让长 think 生成超过
        # openai 客户端默认 600s 而抛 APITimeoutError → 整条轨迹记为空。放宽到默认 1800s，
        # 可用 EDU_AGENT_TIMEOUT 覆盖。
        self.timeout = timeout if timeout is not None else \
            float(os.environ.get("EDU_AGENT_TIMEOUT", "1800"))
        self._client = OpenAI(base_url=self.base_url, api_key=self.api_key,
                              timeout=self.timeout)

    def chat(self, messages: list[dict], tools: list[dict]) -> EngineResponse:
        resp = self._client.chat.completions.create(
            model=self.model, messages=messages, tools=tools,
            tool_choice="auto", temperature=self.temperature,
        )
        msg = resp.choices[0].message
        tool_calls = []
        for tc in (msg.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
        return EngineResponse(content=msg.content, tool_calls=tool_calls)
