"""OpenAI Responses API provider adapter.

The adapter translates the repository's Chat Completions-shaped conversation
history into Responses input items while keeping the public ``Engine.chat``
contract unchanged. ``chat`` aggregates the same normalized stream consumed by
future RunEvent/SSE wiring; text-format structured output remains unsupported.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from .base import EngineResponse, ToolCall
from .gateway import (
    ApiMode,
    ProviderCapabilities,
    ProviderCapabilityError,
    ResolvedRoute,
)
from .streaming import (
    ProviderStreamEvent,
    ProviderStreamEventType,
    ProviderStreamProtocolError,
    aggregate_provider_stream,
    provider_stream_error_event,
    read_stream_json_response,
)

_MISSING = object()


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _response_provider_event_id(value: Any, *, response_id: str | None) -> str | None:
    nested_response = _field(value, "response")
    nested_id = _field(nested_response, "id")
    if isinstance(nested_id, str) and nested_id:
        response_id = nested_id
    sequence = _field(value, "sequence_number")
    if isinstance(sequence, int) and not isinstance(sequence, bool):
        return f"{response_id or 'response'}:{sequence}"
    event_id = _field(value, "event_id")
    if isinstance(event_id, str) and event_id:
        return event_id
    return response_id


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


@dataclass
class _StreamingFunctionCall:
    item_id: str | None = None
    call_id: str = ""
    name: str = ""
    arguments: str = ""


class ResponsesAdapter:
    """Map normalized calls to one ``client.responses.create`` event stream."""

    api_mode = ApiMode.RESPONSES
    capabilities = ProviderCapabilities(
        tool_calling=True,
        structured_output=False,
        usage=True,
        streaming=True,
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
        unknown_event_policy: str = "ignore",
        stream_event_sink: Callable[[dict[str, Any]], None] | None = None,
    ):
        if client is not None and client_factory is not None:
            raise ValueError("client 与 client_factory 不能同时配置")
        self._client = client
        self._client_factory = client_factory
        self._api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        policy = str(unknown_event_policy).strip().lower()
        if policy not in {"ignore", "error", "fail"}:
            raise ValueError("unknown_event_policy 必须是 ignore 或 error")
        self.unknown_event_policy = "error" if policy == "fail" else policy
        self.stream_event_sink = stream_event_sink
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
                    max_retries=0,
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
        """Build the shared minimal Responses request before stream controls."""
        if route.api_mode is not self.api_mode:
            raise ValueError(f"ResponsesAdapter 不能处理 {route.api_mode.value} route")
        input_items = self._convert_messages(messages)
        response_tools = self._convert_tools(route, tools)
        estimated_tokens = _estimate_context_tokens(input_items, response_tools)
        context_limit = route.capabilities.context_window_tokens
        if context_limit is not None and estimated_tokens > context_limit:
            raise ProviderCapabilityError(("context_window",))
        request: dict[str, Any] = {
            "model": route.model,
            "input": input_items,
            "temperature": self.temperature,
        }
        if response_tools:
            request["tools"] = response_tools
            request["tool_choice"] = "auto"
        return request

    def validate_request(
        self,
        route: ResolvedRoute,
        messages: list[dict],
        tools: list[dict],
    ) -> None:
        self.build_request(route, messages, tools)

    def chat(
        self,
        route: ResolvedRoute,
        messages: list[dict],
        tools: list[dict],
    ) -> EngineResponse:
        return aggregate_provider_stream(
            self.stream_events(route, messages, tools, attempt=1)
        )

    def stream_events(
        self,
        route: ResolvedRoute,
        messages: list[dict],
        tools: list[dict],
        *,
        attempt: int = 1,
    ) -> Iterator[ProviderStreamEvent]:
        request = self.build_request(route, messages, tools)
        if not (self.capabilities.streaming and route.capabilities.streaming):
            raise ProviderCapabilityError(("streaming",))
        request["stream"] = True
        try:
            raw_stream = self._client_for(route).responses.create(**request)
        except Exception as error:
            yield provider_stream_error_event(
                route=route,
                attempt=attempt,
                error=error,
                retryable=True,
            )
            return

        if isinstance(raw_stream, Mapping):
            yield from self._full_response_events(raw_stream, route=route, attempt=attempt)
            return

        close = getattr(raw_stream, "close", None)
        try:
            json_response = read_stream_json_response(raw_stream)
        except Exception as error:
            if callable(close):
                close()
            yield provider_stream_error_event(
                route=route,
                attempt=attempt,
                error=error,
                retryable=True,
            )
            return
        if json_response is not None:
            try:
                yield from self._full_response_events(
                    json_response,
                    route=route,
                    attempt=attempt,
                )
            finally:
                if callable(close):
                    close()
            return

        calls: dict[int, _StreamingFunctionCall] = {}
        item_indexes: dict[str, int] = {}
        response_id: str | None = None
        response_model: str | None = None
        visible = False
        terminal = False
        try:
            for raw_event in raw_stream:
                events = list(
                    self._response_stream_events(
                        raw_event,
                        route=route,
                        attempt=attempt,
                        calls=calls,
                        item_indexes=item_indexes,
                        response_id=response_id,
                        response_model=response_model,
                    )
                )
                event_response = _field(raw_event, "response")
                event_type = _field(raw_event, "type")
                discovered_id = _field(event_response, "id")
                discovered_model = _field(event_response, "model")
                if isinstance(discovered_id, str) and discovered_id:
                    response_id = discovered_id
                if isinstance(discovered_model, str) and discovered_model:
                    response_model = discovered_model
                for event in events:
                    visible = visible or event.is_delta
                    terminal = terminal or event.is_terminal
                    yield event
                    if event.is_terminal:
                        return
                if event_type in {
                    "response.completed",
                    "response.incomplete",
                    "response.failed",
                    "error",
                }:
                    terminal = True
        except Exception as error:
            yield provider_stream_error_event(
                route=route,
                attempt=attempt,
                error=error,
                retryable=not visible,
            )
            return
        finally:
            if callable(close):
                close()

        if not terminal:
            yield provider_stream_error_event(
                route=route,
                attempt=attempt,
                error=ProviderStreamProtocolError(
                    "Responses stream 在 terminal 前中断",
                    code="provider_stream_interrupted",
                ),
                provider_event_id=response_id,
                retryable=not visible,
            )

    stream = stream_events
    stream_chat = stream_events

    def _response_stream_events(
        self,
        raw_event: Any,
        *,
        route: ResolvedRoute,
        attempt: int,
        calls: dict[int, _StreamingFunctionCall],
        item_indexes: dict[str, int],
        response_id: str | None,
        response_model: str | None,
    ) -> Iterator[ProviderStreamEvent]:
        event_type = _field(raw_event, "type")
        if not isinstance(event_type, str) or not event_type:
            yield from self._unknown_event(
                route=route,
                attempt=attempt,
                provider_event_id=None,
                provider_event_type="missing_type",
                raw=raw_event,
            )
            return
        provider_event_id = _response_provider_event_id(
            raw_event,
            response_id=response_id,
        )

        if event_type == "response.output_text.delta":
            delta = _field(raw_event, "delta")
            if not isinstance(delta, str):
                yield provider_stream_error_event(
                    route=route,
                    attempt=attempt,
                    error=ProviderStreamProtocolError(
                        "Responses output_text delta 必须是 text",
                        code="provider_stream_invalid_delta",
                    ),
                    provider_event_id=provider_event_id,
                    provider_event_type=event_type,
                )
            elif delta:
                yield ProviderStreamEvent(
                    ProviderStreamEventType.TEXT_DELTA,
                    route=route,
                    attempt=attempt,
                    provider_event_id=provider_event_id,
                    provider_event_type=event_type,
                    delta=delta,
                )
            else:
                yield self._ignored(
                    route,
                    attempt,
                    provider_event_id,
                    event_type,
                    "empty_delta",
                )
            return

        if event_type == "response.output_item.added":
            item = _field(raw_event, "item", {}) or {}
            item_type = _field(item, "type")
            if item_type == "function_call":
                yield from self._reconcile_function_call(
                    route=route,
                    attempt=attempt,
                    provider_event_id=provider_event_id,
                    provider_event_type=event_type,
                    output_index=_field(raw_event, "output_index"),
                    item_id=_field(item, "id"),
                    call_id=_field(item, "call_id"),
                    name=_field(item, "name"),
                    arguments=_field(item, "arguments"),
                    calls=calls,
                    item_indexes=item_indexes,
                )
            elif item_type == "message":
                yield self._ignored(
                    route,
                    attempt,
                    provider_event_id,
                    event_type,
                    "message_lifecycle",
                )
            else:
                yield from self._unknown_event(
                    route=route,
                    attempt=attempt,
                    provider_event_id=provider_event_id,
                    provider_event_type=f"{event_type}:{item_type}",
                    raw=item,
                )
            return

        if event_type == "response.function_call_arguments.delta":
            output_index = self._resolve_output_index(
                raw_event,
                item_indexes=item_indexes,
            )
            if output_index is None:
                yield provider_stream_error_event(
                    route=route,
                    attempt=attempt,
                    error=ProviderStreamProtocolError(
                        "Responses arguments delta 缺少合法 output_index/item_id",
                        code="provider_stream_invalid_tool_index",
                    ),
                    provider_event_id=provider_event_id,
                    provider_event_type=event_type,
                )
                return
            item_id = _field(raw_event, "item_id")
            state = self._bind_call(
                calls,
                item_indexes,
                output_index=output_index,
                item_id=item_id,
            )
            delta = _field(raw_event, "delta")
            if not isinstance(delta, str):
                yield provider_stream_error_event(
                    route=route,
                    attempt=attempt,
                    error=ProviderStreamProtocolError(
                        "Responses arguments delta 必须是 text",
                        code="provider_stream_invalid_delta",
                    ),
                    provider_event_id=provider_event_id,
                    provider_event_type=event_type,
                )
                return
            if delta:
                state.arguments += delta
                yield ProviderStreamEvent(
                    ProviderStreamEventType.TOOL_CALL_ARGUMENTS_DELTA,
                    route=route,
                    attempt=attempt,
                    provider_event_id=provider_event_id,
                    provider_event_type=event_type,
                    delta=delta,
                    tool_call_index=output_index,
                )
            else:
                yield self._ignored(
                    route,
                    attempt,
                    provider_event_id,
                    event_type,
                    "empty_delta",
                )
            return

        if event_type == "response.function_call_arguments.done":
            output_index = self._resolve_output_index(
                raw_event,
                item_indexes=item_indexes,
            )
            yield from self._reconcile_function_call(
                route=route,
                attempt=attempt,
                provider_event_id=provider_event_id,
                provider_event_type=event_type,
                output_index=output_index,
                item_id=_field(raw_event, "item_id"),
                call_id=None,
                name=_field(raw_event, "name"),
                arguments=_field(raw_event, "arguments"),
                calls=calls,
                item_indexes=item_indexes,
            )
            return

        if event_type == "response.output_item.done":
            item = _field(raw_event, "item", {}) or {}
            item_type = _field(item, "type")
            if item_type == "function_call":
                yield from self._reconcile_function_call(
                    route=route,
                    attempt=attempt,
                    provider_event_id=provider_event_id,
                    provider_event_type=event_type,
                    output_index=_field(raw_event, "output_index"),
                    item_id=_field(item, "id"),
                    call_id=_field(item, "call_id"),
                    name=_field(item, "name"),
                    arguments=_field(item, "arguments"),
                    calls=calls,
                    item_indexes=item_indexes,
                )
            elif item_type == "message":
                yield self._ignored(
                    route,
                    attempt,
                    provider_event_id,
                    event_type,
                    "message_lifecycle",
                )
            else:
                yield from self._unknown_event(
                    route=route,
                    attempt=attempt,
                    provider_event_id=provider_event_id,
                    provider_event_type=f"{event_type}:{item_type}",
                    raw=item,
                )
            return

        if event_type in {"response.completed", "response.incomplete"}:
            response = _field(raw_event, "response", {}) or {}
            response_error = _field(response, "error")
            status = _field(response, "status")
            if response_error is not None or status in {"failed", "cancelled"}:
                yield provider_stream_error_event(
                    route=route,
                    attempt=attempt,
                    error=ResponsesAPIError(
                        str(status or "failed"),
                        code=_field(response_error, "code"),
                    ),
                    provider_event_id=provider_event_id,
                    provider_event_type=event_type,
                )
                return
            for output_index, item in enumerate(_field(response, "output", ()) or ()):
                item_type = _field(item, "type")
                if item_type == "function_call":
                    yield from self._reconcile_function_call(
                        route=route,
                        attempt=attempt,
                        provider_event_id=provider_event_id,
                        provider_event_type=event_type,
                        output_index=output_index,
                        item_id=_field(item, "id"),
                        call_id=_field(item, "call_id"),
                        name=_field(item, "name"),
                        arguments=_field(item, "arguments"),
                        calls=calls,
                        item_indexes=item_indexes,
                    )
            usage = _normalize_usage(_field(response, "usage"))
            if usage:
                yield ProviderStreamEvent(
                    ProviderStreamEventType.USAGE,
                    route=route,
                    attempt=attempt,
                    provider_event_id=provider_event_id,
                    provider_event_type=event_type,
                    usage=usage,
                )
            if event_type == "response.incomplete" or status == "incomplete":
                reason = _field(_field(response, "incomplete_details"), "reason")
                finish_reason = {
                    "max_output_tokens": "length",
                    "content_filter": "content_filter",
                }.get(reason, "incomplete")
            else:
                finish_reason = "tool_calls" if calls else "stop"
            model = _field(response, "model") or response_model or route.model
            yield ProviderStreamEvent(
                ProviderStreamEventType.COMPLETED,
                route=route,
                attempt=attempt,
                provider_event_id=provider_event_id,
                provider_event_type=event_type,
                finish_reason=finish_reason,
                model=model,
                content_default="",
            )
            return

        if event_type in {"response.failed", "error"}:
            response = _field(raw_event, "response", {}) or {}
            response_error = _field(response, "error") or raw_event
            status = _field(response, "status") or "failed"
            yield provider_stream_error_event(
                route=route,
                attempt=attempt,
                error=ResponsesAPIError(
                    str(status),
                    code=_field(response_error, "code"),
                ),
                provider_event_id=provider_event_id,
                provider_event_type=event_type,
            )
            return

        if event_type in {
            "response.created",
            "response.in_progress",
            "response.queued",
            "response.content_part.added",
            "response.content_part.done",
            "response.output_text.done",
            "response.output_text.annotation.added",
        }:
            yield self._ignored(
                route,
                attempt,
                provider_event_id,
                event_type,
                "known_lifecycle_event",
            )
            return

        yield from self._unknown_event(
            route=route,
            attempt=attempt,
            provider_event_id=provider_event_id,
            provider_event_type=event_type,
            raw=raw_event,
        )

    def _reconcile_function_call(
        self,
        *,
        route: ResolvedRoute,
        attempt: int,
        provider_event_id: str | None,
        provider_event_type: str,
        output_index: Any,
        item_id: Any,
        call_id: Any,
        name: Any,
        arguments: Any,
        calls: dict[int, _StreamingFunctionCall],
        item_indexes: dict[str, int],
    ) -> Iterator[ProviderStreamEvent]:
        if isinstance(output_index, bool) or not isinstance(output_index, int) or output_index < 0:
            yield provider_stream_error_event(
                route=route,
                attempt=attempt,
                error=ProviderStreamProtocolError(
                    "Responses function call 缺少合法 output_index",
                    code="provider_stream_invalid_tool_index",
                ),
                provider_event_id=provider_event_id,
                provider_event_type=provider_event_type,
            )
            return
        try:
            state = self._bind_call(
                calls,
                item_indexes,
                output_index=output_index,
                item_id=item_id,
            )
            for event_type, attribute, value in (
                (ProviderStreamEventType.TOOL_CALL_ID_DELTA, "call_id", call_id),
                (ProviderStreamEventType.TOOL_CALL_NAME_DELTA, "name", name),
                (
                    ProviderStreamEventType.TOOL_CALL_ARGUMENTS_DELTA,
                    "arguments",
                    arguments,
                ),
            ):
                if value is None:
                    continue
                if not isinstance(value, str):
                    raise ProviderStreamProtocolError(
                        f"Responses function call {attribute} 必须是 text",
                        code="provider_stream_invalid_tool_call",
                    )
                current = getattr(state, attribute)
                if value == current:
                    continue
                if not value.startswith(current):
                    raise ProviderStreamProtocolError(
                        f"Responses function call {attribute} 与既有 delta 冲突",
                        code="provider_stream_tool_call_mismatch",
                    )
                suffix = value[len(current):]
                setattr(state, attribute, value)
                if suffix:
                    yield ProviderStreamEvent(
                        event_type,
                        route=route,
                        attempt=attempt,
                        provider_event_id=provider_event_id,
                        provider_event_type=provider_event_type,
                        delta=suffix,
                        tool_call_index=output_index,
                    )
        except ProviderStreamProtocolError as error:
            yield provider_stream_error_event(
                route=route,
                attempt=attempt,
                error=error,
                provider_event_id=provider_event_id,
                provider_event_type=provider_event_type,
            )

    @staticmethod
    def _bind_call(
        calls: dict[int, _StreamingFunctionCall],
        item_indexes: dict[str, int],
        *,
        output_index: int,
        item_id: Any,
    ) -> _StreamingFunctionCall:
        state = calls.setdefault(output_index, _StreamingFunctionCall())
        if item_id is not None:
            if not isinstance(item_id, str) or not item_id:
                raise ProviderStreamProtocolError(
                    "Responses function call item_id 无效",
                    code="provider_stream_invalid_tool_call",
                )
            prior = item_indexes.get(item_id)
            if prior is not None and prior != output_index:
                raise ProviderStreamProtocolError(
                    "Responses function call item_id 对应多个 output_index",
                    code="provider_stream_tool_call_mismatch",
                )
            if state.item_id is not None and state.item_id != item_id:
                raise ProviderStreamProtocolError(
                    "Responses output_index 对应多个 function call item",
                    code="provider_stream_tool_call_mismatch",
                )
            state.item_id = item_id
            item_indexes[item_id] = output_index
        return state

    @staticmethod
    def _resolve_output_index(
        event: Any,
        *,
        item_indexes: dict[str, int],
    ) -> int | None:
        output_index = _field(event, "output_index")
        if isinstance(output_index, int) and not isinstance(output_index, bool) and output_index >= 0:
            return output_index
        item_id = _field(event, "item_id")
        return item_indexes.get(item_id) if isinstance(item_id, str) else None

    def _unknown_event(
        self,
        *,
        route: ResolvedRoute,
        attempt: int,
        provider_event_id: str | None,
        provider_event_type: str,
        raw: Any,
    ) -> Iterator[ProviderStreamEvent]:
        details = {
            "event": "provider_stream_unknown_event",
            "route": route.to_event(),
            "attempt": attempt,
            "provider_event_id": provider_event_id,
            "provider_event_type": provider_event_type,
            "raw_type": type(raw).__name__,
        }
        if self.stream_event_sink is not None:
            try:
                self.stream_event_sink(details)
            except Exception:
                pass
        if self.unknown_event_policy == "error":
            yield provider_stream_error_event(
                route=route,
                attempt=attempt,
                error=ProviderStreamProtocolError(
                    f"未知 Responses stream event: {provider_event_type}",
                    code="provider_stream_unknown_event",
                ),
                provider_event_id=provider_event_id,
                provider_event_type=provider_event_type,
            )
            return
        yield self._ignored(
            route,
            attempt,
            provider_event_id,
            provider_event_type,
            "unknown_event_ignored",
        )

    @staticmethod
    def _ignored(
        route: ResolvedRoute,
        attempt: int,
        provider_event_id: str | None,
        provider_event_type: str,
        reason: str,
    ) -> ProviderStreamEvent:
        return ProviderStreamEvent(
            ProviderStreamEventType.IGNORED,
            route=route,
            attempt=attempt,
            provider_event_id=provider_event_id,
            provider_event_type=provider_event_type,
            metadata={"reason": reason},
        )

    def _full_response_events(
        self,
        response: Mapping[str, Any],
        *,
        route: ResolvedRoute,
        attempt: int,
    ) -> Iterator[ProviderStreamEvent]:
        try:
            normalized = self.normalize_response(response, route=route)
        except Exception as error:
            yield provider_stream_error_event(
                route=route,
                attempt=attempt,
                error=error,
                provider_event_id=_field(response, "id"),
            )
            return
        event_id = _field(response, "id")
        yield from self._normalized_response_events(
            normalized,
            route=route,
            attempt=attempt,
            event_id=event_id,
        )

    @staticmethod
    def _normalized_response_events(
        normalized: EngineResponse,
        *,
        route: ResolvedRoute,
        attempt: int,
        event_id: str | None,
    ) -> Iterator[ProviderStreamEvent]:
        if normalized.content:
            yield ProviderStreamEvent(
                ProviderStreamEventType.TEXT_DELTA,
                route=route,
                attempt=attempt,
                provider_event_id=event_id,
                provider_event_type="response.completed",
                delta=normalized.content,
            )
        for index, call in enumerate(normalized.tool_calls):
            for event_type, value in (
                (ProviderStreamEventType.TOOL_CALL_ID_DELTA, call.id),
                (ProviderStreamEventType.TOOL_CALL_NAME_DELTA, call.name),
                (
                    ProviderStreamEventType.TOOL_CALL_ARGUMENTS_DELTA,
                    call.arguments
                    if isinstance(call.arguments, str)
                    else json.dumps(call.arguments, ensure_ascii=False),
                ),
            ):
                if value:
                    yield ProviderStreamEvent(
                        event_type,
                        route=route,
                        attempt=attempt,
                        provider_event_id=event_id,
                        provider_event_type="response.completed",
                        delta=value,
                        tool_call_index=index,
                    )
        if normalized.usage:
            yield ProviderStreamEvent(
                ProviderStreamEventType.USAGE,
                route=route,
                attempt=attempt,
                provider_event_id=event_id,
                provider_event_type="response.completed",
                usage=normalized.usage,
            )
        yield ProviderStreamEvent(
            ProviderStreamEventType.COMPLETED,
            route=route,
            attempt=attempt,
            provider_event_id=event_id,
            provider_event_type="response.completed",
            finish_reason=normalized.finish_reason or (
                "tool_calls" if normalized.tool_calls else "stop"
            ),
            model=normalized.model or route.model,
            content_default="",
        )

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
