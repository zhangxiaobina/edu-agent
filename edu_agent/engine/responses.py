"""Synchronous OpenAI Responses API provider adapter.

The adapter translates the repository's Chat Completions-shaped conversation
history into Responses input items while keeping the public ``Engine.chat``
contract unchanged. Provider streaming and text-format structured output stay
outside this R1.3 slice.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from typing import Any

from .base import EngineResponse, ToolCall
from .gateway import ApiMode, ProviderCapabilities, ResolvedRoute

_MISSING = object()


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _dump_usage(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        rendered = dump()
        return dict(rendered) if isinstance(rendered, Mapping) else {}
    dump = getattr(value, "dict", None)
    if callable(dump):
        rendered = dump()
        return dict(rendered) if isinstance(rendered, Mapping) else {}
    try:
        return dict(vars(value))
    except TypeError:
        return {}


def _tool_arguments(value: Any) -> dict | str:
    if value is None:
        return "{}"
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return dict(value)
    return str(value)


def _outgoing_arguments(value: Any) -> str:
    if value is None:
        return "{}"
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return json.dumps(dict(value), ensure_ascii=False)
    raise ValueError("Responses function call arguments 必须是 JSON 字符串或 object")


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} 必须是非空且无首尾空白的字符串")
    return value


def _normalize_usage(value: Any) -> dict:
    """Translate Responses token names to the existing Chat usage vocabulary."""
    usage = _dump_usage(value)
    aliases = {
        "input_tokens": "prompt_tokens",
        "input_tokens_details": "prompt_tokens_details",
        "output_tokens": "completion_tokens",
        "output_tokens_details": "completion_tokens_details",
    }
    normalized: dict[str, Any] = {}
    for key, item in usage.items():
        if item is not None:
            normalized[aliases.get(key, key)] = item
    return normalized


def _estimate_context_tokens(input_items: list[dict], tools: list[dict]) -> int:
    payload = json.dumps(
        {"input": input_items, "tools": tools},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return max(1, (len(payload) + 3) // 4)


class ResponsesAPIError(RuntimeError):
    """A terminal status returned inside a successful Responses HTTP exchange."""

    def __init__(
        self,
        status: str,
        *,
        code: str | None = None,
    ):
        self.response_status = status
        self.code = code
        detail = f"Responses API 返回终止状态 {status}"
        if code:
            detail += f" ({code})"
        super().__init__(detail)


class ResponsesAdapter:
    """Map normalized calls to ``client.responses.create`` without streaming."""

    api_mode = ApiMode.RESPONSES
    capabilities = ProviderCapabilities(
        tool_calling=True,
        structured_output=False,
        usage=True,
        streaming=False,
        context_window_tokens=None,
        max_output_tokens=None,
    )

    def __init__(
        self,
        client: Any | None = None,
        *,
        client_factory: Callable[[ResolvedRoute], Any] | None = None,
        api_key: str | None = None,
        temperature: float = 0.0,
        timeout: float = 1800.0,
    ):
        if client is not None and client_factory is not None:
            raise ValueError("client 与 client_factory 不能同时配置")
        self._client = client
        self._client_factory = client_factory
        self._api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self._clients: dict[tuple[str, str], Any] = {}
        self._clients_lock = threading.Lock()

    def _client_for(self, route: ResolvedRoute) -> Any:
        if self._client is not None:
            return self._client
        key = (route.normalized_endpoint, route.credential.environment_variable)
        cached = self._clients.get(key)
        if cached is not None:
            return cached
        with self._clients_lock:
            cached = self._clients.get(key)
            if cached is not None:
                return cached
            if self._client_factory is not None:
                client = self._client_factory(route)
            else:
                try:
                    from openai import OpenAI
                except ImportError as error:  # pragma: no cover - dependency is locked in CI
                    raise RuntimeError("需要 openai 包：uv pip install openai") from error
                client = OpenAI(
                    base_url=route.endpoint,
                    api_key=(self._api_key or route.credential.resolve() or "EMPTY"),
                    timeout=self.timeout,
                )
            self._clients[key] = client
            return client

    @classmethod
    def _convert_messages(cls, messages: list[dict]) -> list[dict]:
        input_items: list[dict] = []
        pending_calls: set[str] = set()
        for index, message in enumerate(messages):
            if not isinstance(message, Mapping):
                raise ValueError(f"Responses message[{index}] 必须是 object")
            role = message.get("role")
            if role == "tool":
                call_id = _required_string(
                    message.get("tool_call_id"),
                    f"Responses message[{index}].tool_call_id",
                )
                if call_id not in pending_calls:
                    raise ValueError(f"Responses message[{index}] 是孤立的 tool result")
                content = message.get("content", "")
                if not isinstance(content, str):
                    raise ValueError(f"Responses message[{index}].content 仅支持 text")
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": content,
                    }
                )
                pending_calls.remove(call_id)
                continue

            if role not in {"system", "developer", "user", "assistant"}:
                raise ValueError(f"Responses message[{index}].role 不受支持")
            if pending_calls:
                raise ValueError("Responses function calls 与 outputs 之间不能插入消息")
            content = message.get("content", "")
            if content is None and role == "assistant" and message.get("tool_calls"):
                content = ""
            if not isinstance(content, str):
                raise ValueError(f"Responses message[{index}].content 仅支持 text")

            raw_calls = message.get("tool_calls", [])
            if raw_calls is None:
                raw_calls = []
            if not isinstance(raw_calls, list):
                raise ValueError(f"Responses message[{index}].tool_calls 必须是 list")
            if raw_calls and role != "assistant":
                raise ValueError("Responses 仅允许 assistant message 携带 tool_calls")
            if content or not raw_calls:
                input_items.append({"role": role, "content": content})
            for call_index, raw_call in enumerate(raw_calls):
                if not isinstance(raw_call, Mapping):
                    raise ValueError("Responses tool call 必须是 object")
                if raw_call.get("type", "function") != "function":
                    raise ValueError("Responses adapter 仅支持 function tool call")
                function = raw_call.get("function")
                if not isinstance(function, Mapping):
                    raise ValueError("Responses function tool call 缺少 function")
                call_id = _required_string(
                    raw_call.get("id"),
                    f"Responses message[{index}].tool_calls[{call_index}].id",
                )
                if call_id in pending_calls:
                    raise ValueError(f"Responses function call id 重复：{call_id}")
                input_items.append(
                    {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": _required_string(
                            function.get("name"),
                            "Responses function call name",
                        ),
                        "arguments": _outgoing_arguments(function.get("arguments")),
                    }
                )
                pending_calls.add(call_id)
        if pending_calls:
            raise ValueError(f"Responses function calls 缺少 outputs：{sorted(pending_calls)}")
        return input_items

    @classmethod
    def _convert_tools(cls, route: ResolvedRoute, tools: list[dict]) -> list[dict]:
        if tools and (
            not cls.capabilities.tool_calling or not route.capabilities.tool_calling
        ):
            raise ValueError("当前 Responses route 不支持 tool calling")
        converted: list[dict] = []
        for index, tool in enumerate(tools):
            if not isinstance(tool, Mapping) or tool.get("type") != "function":
                raise ValueError("Responses adapter 仅支持 function tools")
            function = tool.get("function")
            if not isinstance(function, Mapping):
                raise ValueError(f"Responses tool[{index}] 缺少 function")
            strict = function.get("strict")
            if strict is not None and not isinstance(strict, bool):
                raise ValueError(f"Responses tool[{index}].function.strict 必须是 bool")
            if strict is True and (
                not cls.capabilities.structured_output
                or not route.capabilities.structured_output
            ):
                raise ValueError("当前 Responses adapter/route 未开启 structured output")
            parameters = function.get("parameters")
            if parameters is not None and not isinstance(parameters, Mapping):
                raise ValueError(f"Responses tool[{index}].function.parameters 必须是 object")
            response_tool: dict[str, Any] = {
                "type": "function",
                "name": _required_string(
                    function.get("name"),
                    f"Responses tool[{index}].function.name",
                ),
                "parameters": dict(parameters) if parameters is not None else None,
                "strict": strict,
            }
            description = function.get("description")
            if description is not None:
                if not isinstance(description, str):
                    raise ValueError(
                        f"Responses tool[{index}].function.description 必须是 text"
                    )
                response_tool["description"] = description
            converted.append(response_tool)
        return converted

    def build_request(
        self,
        route: ResolvedRoute,
        messages: list[dict],
        tools: list[dict],
    ) -> dict[str, Any]:
        """Build the exact minimal non-streaming Responses request payload."""
        if route.api_mode is not self.api_mode:
            raise ValueError(f"ResponsesAdapter 不能处理 {route.api_mode.value} route")
        input_items = self._convert_messages(messages)
        response_tools = self._convert_tools(route, tools)
        estimated_tokens = _estimate_context_tokens(input_items, response_tools)
        context_limit = route.capabilities.context_window_tokens
        if context_limit is not None and estimated_tokens > context_limit:
            raise ValueError(
                "Responses 输入在发请求前已超过 route context window "
                f"({estimated_tokens}/{context_limit})"
            )
        request: dict[str, Any] = {
            "model": route.model,
            "input": input_items,
            "temperature": self.temperature,
        }
        if response_tools:
            request["tools"] = response_tools
            request["tool_choice"] = "auto"
        return request

    def chat(
        self,
        route: ResolvedRoute,
        messages: list[dict],
        tools: list[dict],
    ) -> EngineResponse:
        request = self.build_request(route, messages, tools)
        response = self._client_for(route).responses.create(**request)
        return self.normalize_response(response, route=route)

    @classmethod
    def normalize_response(
        cls,
        response: Any,
        *,
        route: ResolvedRoute | None = None,
    ) -> EngineResponse:
        status = _field(response, "status")
        response_error = _field(response, "error")
        if response_error is not None or status in {
            "failed",
            "cancelled",
            "in_progress",
            "queued",
        }:
            raise ResponsesAPIError(
                str(status or "failed"),
                code=_field(response_error, "code"),
            )
        if status not in {None, "completed", "incomplete"}:
            raise ResponsesAPIError(str(status))

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for item in _field(response, "output", ()) or ():
            item_type = _field(item, "type")
            if item_type == "message":
                for content in _field(item, "content", ()) or ():
                    if _field(content, "type") != "output_text":
                        continue
                    text = _field(content, "text", "")
                    if not isinstance(text, str):
                        raise ValueError("Responses output_text.text 必须是 text")
                    text_parts.append(text)
            elif item_type == "function_call":
                call_id = _required_string(
                    _field(item, "call_id"),
                    "Responses function_call.call_id",
                )
                tool_calls.append(
                    ToolCall(
                        id=call_id,
                        name=_required_string(
                            _field(item, "name"),
                            "Responses function_call.name",
                        ),
                        arguments=_tool_arguments(_field(item, "arguments")),
                    )
                )

        if status == "incomplete":
            incomplete_reason = _field(_field(response, "incomplete_details"), "reason")
            finish_reason = {
                "max_output_tokens": "length",
                "content_filter": "content_filter",
            }.get(incomplete_reason, "incomplete")
        else:
            finish_reason = "tool_calls" if tool_calls else "stop"
        model = _field(response, "model", _MISSING)
        if model is _MISSING or model is None:
            model = route.model if route is not None else None
        return EngineResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            usage=_normalize_usage(_field(response, "usage")),
            finish_reason=finish_reason,
            model=model,
        )


__all__ = ["ResponsesAdapter", "ResponsesAPIError"]
