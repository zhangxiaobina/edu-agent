"""OpenAI-compatible Chat Completions provider adapter.

The adapter owns the SDK boundary.  Callers use the normalized ``Engine``
request shape and receive the repository's ``EngineResponse`` shape, while
the provider-specific request/response objects stay here.  ``chat`` is a
compatibility aggregation over one normalized stream parser.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from ..runtime.cancellation import CancellationRequested, CancellationToken
from .base import EngineResponse, ToolCall
from .gateway import (
    ApiMode,
    ProviderCapabilities,
    ProviderCapabilityError,
    ResolvedRoute,
    estimate_request_tokens,
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


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _required_delta(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ProviderStreamProtocolError(
            "provider delta 必须是非空 text",
            code="provider_stream_invalid_delta",
        )
    return value


def _dump_usage(value: Any) -> dict:
    """Copy SDK usage objects without depending on a particular SDK model."""
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
    # A provider may return an empty string while it is still assembling a
    # call.  Preserve that value; ``value or "{}"`` silently changes it.
    if value is None:
        return "{}"
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return dict(value)
    # OpenAI-compatible SDKs normally expose a string.  Keep an unusual raw
    # scalar representable rather than attempting lossy JSON repair here.
    return str(value)


class ChatCompletionsAdapter:
    """Map normalized provider calls to ``client.chat.completions.create``."""

    api_mode = ApiMode.CHAT_COMPLETIONS
    capabilities = ProviderCapabilities(streaming=True)

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

    def build_request(
        self,
        route: ResolvedRoute,
        messages: list[dict],
        tools: list[dict],
        *,
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Build the shared Chat Completions request before stream controls."""
        if route.api_mode is not self.api_mode:
            raise ValueError(
                f"ChatCompletionsAdapter 不能处理 {route.api_mode.value} route"
            )
        if tools and (
            not self.capabilities.tool_calling or not route.capabilities.tool_calling
        ):
            raise ValueError("当前 Chat Completions route 不支持 tool calling")
        if any(
            isinstance(tool, Mapping)
            and isinstance(tool.get("function"), Mapping)
            and tool["function"].get("strict") is True
            for tool in tools
        ) and (
            not self.capabilities.structured_output
            or not route.capabilities.structured_output
        ):
            raise ProviderCapabilityError(("structured_output",))
        context_limit = route.capabilities.context_window_tokens
        if (
            context_limit is not None
            and estimate_request_tokens(
                messages,
                tools,
                model=route.model,
                tokenizer=route.capabilities.tokenizer,
            )
            + (max_output_tokens or 0)
            > context_limit
        ):
            raise ProviderCapabilityError(("context_window",))
        output_limit = route.capabilities.max_output_tokens
        if max_output_tokens is not None and (
            output_limit is not None and max_output_tokens > output_limit
        ):
            raise ProviderCapabilityError(("max_output_tokens",))
        request: dict[str, Any] = {
            "model": route.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if tools:
            request["tools"] = tools
            request["tool_choice"] = "auto"
        if max_output_tokens is not None:
            request["max_tokens"] = max_output_tokens
        return request

    def validate_request(
        self,
        route: ResolvedRoute,
        messages: list[dict],
        tools: list[dict],
        *,
        max_output_tokens: int | None = None,
    ) -> None:
        self.build_request(
            route,
            messages,
            tools,
            max_output_tokens=max_output_tokens,
        )

    def chat(
        self,
        route: ResolvedRoute,
        messages: list[dict],
        tools: list[dict],
        *,
        cancellation_token: CancellationToken | None = None,
        max_output_tokens: int | None = None,
    ) -> EngineResponse:
        return aggregate_provider_stream(
            self.stream_events(
                route,
                messages,
                tools,
                attempt=1,
                cancellation_token=cancellation_token,
                max_output_tokens=max_output_tokens,
            )
        )

    def stream_events(
        self,
        route: ResolvedRoute,
        messages: list[dict],
        tools: list[dict],
        *,
        attempt: int = 1,
        cancellation_token: CancellationToken | None = None,
        max_output_tokens: int | None = None,
    ) -> Iterator[ProviderStreamEvent]:
        if cancellation_token is not None:
            cancellation_token.checkpoint("chat_completions.before_request")
        request = self.build_request(
            route,
            messages,
            tools,
            max_output_tokens=max_output_tokens,
        )
        if not (self.capabilities.streaming and route.capabilities.streaming):
            raise ProviderCapabilityError(("streaming",))
        request.update(stream=True, stream_options={"include_usage": True})
        raw_stream = None
        try:
            raw_stream = self._client_for(route).chat.completions.create(**request)
            if cancellation_token is not None:
                cancellation_token.checkpoint("chat_completions.after_request")
        except CancellationRequested:
            close = getattr(raw_stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
            raise
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
        unregister_close = (
            cancellation_token.register(lambda _: close())
            if cancellation_token is not None and callable(close)
            else lambda: None
        )
        try:
            json_response = read_stream_json_response(raw_stream)
        except CancellationRequested:
            unregister_close()
            raise
        except Exception as error:
            unregister_close()
            if cancellation_token is not None:
                cancellation_token.checkpoint("chat_completions.read_response")
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
                unregister_close()
                if callable(close):
                    close()
            return

        terminal: tuple[str, str | None, str | None] | None = None
        content_seen = False
        visible = False
        try:
            for raw_chunk in raw_stream:
                if cancellation_token is not None:
                    cancellation_token.checkpoint("chat_completions.chunk")
                events, chunk_terminal, chunk_content_seen = self._chunk_events(
                    raw_chunk,
                    route=route,
                    attempt=attempt,
                )
                content_seen = content_seen or chunk_content_seen
                for event in events:
                    visible = visible or event.is_delta
                    yield event
                    if event.is_terminal:
                        return
                if chunk_terminal is not None:
                    if terminal is not None and chunk_terminal[0] != terminal[0]:
                        yield provider_stream_error_event(
                            route=route,
                            attempt=attempt,
                            error=ProviderStreamProtocolError(
                                "Chat Completions stream 返回冲突 finish_reason",
                                code="provider_stream_conflicting_finish_reason",
                            ),
                            provider_event_id=chunk_terminal[2],
                        )
                        return
                    terminal = chunk_terminal
        except CancellationRequested:
            raise
        except Exception as error:
            yield provider_stream_error_event(
                route=route,
                attempt=attempt,
                error=error,
                retryable=not visible,
            )
            return
        finally:
            unregister_close()
            if callable(close):
                close()

        if terminal is None:
            yield provider_stream_error_event(
                route=route,
                attempt=attempt,
                error=ProviderStreamProtocolError(
                    "Chat Completions stream 在 terminal 前中断",
                    code="provider_stream_interrupted",
                ),
                retryable=not visible,
            )
            return
        finish_reason, model, provider_event_id = terminal
        yield ProviderStreamEvent(
            ProviderStreamEventType.COMPLETED,
            route=route,
            attempt=attempt,
            provider_event_id=provider_event_id,
            provider_event_type="chat.completion.chunk",
            finish_reason=finish_reason,
            model=model or route.model,
            content_default="" if content_seen else None,
        )

    def _full_response_events(
        self,
        response: Any,
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
                provider_event_id=_optional_text(_field(response, "id")),
            )
            return
        event_id = _optional_text(_field(response, "id"))
        if normalized.content:
            yield ProviderStreamEvent(
                ProviderStreamEventType.TEXT_DELTA,
                route=route,
                attempt=attempt,
                provider_event_id=event_id,
                provider_event_type="chat.completion",
                delta=normalized.content,
            )
        for index, call in enumerate(normalized.tool_calls):
            yield ProviderStreamEvent(
                ProviderStreamEventType.TOOL_CALL_ID_DELTA,
                route=route,
                attempt=attempt,
                provider_event_id=event_id,
                provider_event_type="chat.completion",
                delta=call.id,
                tool_call_index=index,
            )
            yield ProviderStreamEvent(
                ProviderStreamEventType.TOOL_CALL_NAME_DELTA,
                route=route,
                attempt=attempt,
                provider_event_id=event_id,
                provider_event_type="chat.completion",
                delta=call.name,
                tool_call_index=index,
            )
            arguments = (
                call.arguments
                if isinstance(call.arguments, str)
                else json.dumps(call.arguments, ensure_ascii=False)
            )
            if arguments:
                yield ProviderStreamEvent(
                    ProviderStreamEventType.TOOL_CALL_ARGUMENTS_DELTA,
                    route=route,
                    attempt=attempt,
                    provider_event_id=event_id,
                    provider_event_type="chat.completion",
                    delta=arguments,
                    tool_call_index=index,
                )
        if normalized.usage:
            yield ProviderStreamEvent(
                ProviderStreamEventType.USAGE,
                route=route,
                attempt=attempt,
                provider_event_id=event_id,
                provider_event_type="chat.completion",
                usage=normalized.usage,
            )
        yield ProviderStreamEvent(
            ProviderStreamEventType.COMPLETED,
            route=route,
            attempt=attempt,
            provider_event_id=event_id,
            provider_event_type="chat.completion",
            finish_reason=normalized.finish_reason or (
                "tool_calls" if normalized.tool_calls else "stop"
            ),
            model=normalized.model or route.model,
            content_default="" if normalized.content == "" else None,
        )

    @classmethod
    def normalize_response(
        cls,
        response: Any,
        *,
        route: ResolvedRoute | None = None,
    ) -> EngineResponse:
        choices = _field(response, "choices", ()) or ()
        if not choices:
            raise ValueError("Chat Completions 响应缺少 choices")
        choice = choices[0]
        message = _field(choice, "message", {}) or {}
        raw_tool_calls = _field(message, "tool_calls", ()) or ()
        tool_calls: list[ToolCall] = []
        for raw_call in raw_tool_calls:
            function = _field(raw_call, "function", {}) or {}
            tool_calls.append(
                ToolCall(
                    id=_field(raw_call, "id", ""),
                    name=_field(function, "name", ""),
                    arguments=_tool_arguments(_field(function, "arguments")),
                )
            )
        model = _field(response, "model", _MISSING)
        if model is _MISSING:
            model = route.model if route is not None else None
        return EngineResponse(
            content=_field(message, "content"),
            tool_calls=tool_calls,
            usage=_dump_usage(_field(response, "usage")),
            finish_reason=_field(choice, "finish_reason"),
            model=model,
        )

    stream = stream_events
    stream_chat = stream_events

    def _chunk_events(
        self,
        chunk: Any,
        *,
        route: ResolvedRoute,
        attempt: int,
    ) -> tuple[
        list[ProviderStreamEvent],
        tuple[str, str | None, str | None] | None,
        bool,
    ]:
        provider_event_id = _optional_text(_field(chunk, "id"))
        provider_event_type = _field(chunk, "object", "chat.completion.chunk")
        if provider_event_type not in {None, "chat.completion.chunk"}:
            return (
                list(
                    self._unknown_event(
                        route=route,
                        attempt=attempt,
                        provider_event_id=provider_event_id,
                        provider_event_type=str(provider_event_type),
                        raw=chunk,
                    )
                ),
                None,
                False,
            )

        events: list[ProviderStreamEvent] = []
        usage = _dump_usage(_field(chunk, "usage"))
        if usage:
            events.append(
                ProviderStreamEvent(
                    ProviderStreamEventType.USAGE,
                    route=route,
                    attempt=attempt,
                    provider_event_id=provider_event_id,
                    provider_event_type="chat.completion.chunk",
                    usage=usage,
                )
            )
        choices = _field(chunk, "choices", ()) or ()
        if not choices and not usage:
            events.append(
                ProviderStreamEvent(
                    ProviderStreamEventType.IGNORED,
                    route=route,
                    attempt=attempt,
                    provider_event_id=provider_event_id,
                    provider_event_type="chat.completion.chunk",
                    metadata={"reason": "empty_chunk"},
                )
            )

        terminal: tuple[str, str | None, str | None] | None = None
        content_seen = False
        for position, choice in enumerate(choices):
            choice_index = _field(choice, "index", position)
            if choice_index != 0:
                events.extend(
                    self._unknown_event(
                        route=route,
                        attempt=attempt,
                        provider_event_id=provider_event_id,
                        provider_event_type="chat.choice",
                        raw=choice,
                        error=ProviderStreamProtocolError(
                            "normalized EngineResponse 只支持 choice index 0",
                            code="provider_stream_multiple_choices",
                        ),
                    )
                )
                continue
            delta = _field(choice, "delta", {}) or {}
            content = _field(delta, "content")
            if isinstance(content, str):
                content_seen = True
                if content:
                    events.append(
                        ProviderStreamEvent(
                            ProviderStreamEventType.TEXT_DELTA,
                            route=route,
                            attempt=attempt,
                            provider_event_id=provider_event_id,
                            provider_event_type="chat.completion.chunk",
                            delta=content,
                        )
                    )
            elif content is not None:
                events.extend(
                    self._unknown_event(
                        route=route,
                        attempt=attempt,
                        provider_event_id=provider_event_id,
                        provider_event_type="chat.content",
                        raw=content,
                    )
                )

            raw_calls = _field(delta, "tool_calls", ()) or ()
            if not raw_calls:
                legacy_call = _field(delta, "function_call")
                if legacy_call is not None:
                    raw_calls = ({"index": 0, "function": legacy_call},)
            for raw_call in raw_calls:
                index = _field(raw_call, "index")
                if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                    events.extend(
                        self._unknown_event(
                            route=route,
                            attempt=attempt,
                            provider_event_id=provider_event_id,
                            provider_event_type="chat.tool_call",
                            raw=raw_call,
                            error=ProviderStreamProtocolError(
                                "Chat tool call delta 缺少合法 index",
                                code="provider_stream_invalid_tool_index",
                            ),
                        )
                    )
                    continue
                call_type = _field(raw_call, "type")
                if call_type not in {None, "function"}:
                    events.extend(
                        self._unknown_event(
                            route=route,
                            attempt=attempt,
                            provider_event_id=provider_event_id,
                            provider_event_type=str(call_type),
                            raw=raw_call,
                        )
                    )
                    continue
                call_id = _field(raw_call, "id")
                function = _field(raw_call, "function", {}) or {}
                name = _field(function, "name")
                arguments = _field(function, "arguments")
                for event_type, value in (
                    (ProviderStreamEventType.TOOL_CALL_ID_DELTA, call_id),
                    (ProviderStreamEventType.TOOL_CALL_NAME_DELTA, name),
                    (ProviderStreamEventType.TOOL_CALL_ARGUMENTS_DELTA, arguments),
                ):
                    if value is not None and value != "":
                        events.append(
                            ProviderStreamEvent(
                                event_type,
                                route=route,
                                attempt=attempt,
                                provider_event_id=provider_event_id,
                                provider_event_type="chat.completion.chunk",
                                delta=_required_delta(value),
                                tool_call_index=index,
                            )
                        )
            finish_reason = _field(choice, "finish_reason")
            if finish_reason:
                terminal = (
                    _required_delta(finish_reason),
                    _optional_text(_field(chunk, "model")) or route.model,
                    provider_event_id,
                )
        if not any(event.is_delta for event in events) and terminal is None and choices:
            events.append(
                ProviderStreamEvent(
                    ProviderStreamEventType.IGNORED,
                    route=route,
                    attempt=attempt,
                    provider_event_id=provider_event_id,
                    provider_event_type="chat.completion.chunk",
                    metadata={"reason": "empty_delta"},
                )
            )
        return events, terminal, content_seen

    def _unknown_event(
        self,
        *,
        route: ResolvedRoute,
        attempt: int,
        provider_event_id: str | None,
        provider_event_type: str,
        raw: Any,
        error: Exception | None = None,
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
            protocol_error = error or ProviderStreamProtocolError(
                f"未知 Chat Completions stream event: {provider_event_type}",
                code="provider_stream_unknown_event",
            )
            yield provider_stream_error_event(
                route=route,
                attempt=attempt,
                error=protocol_error,
                provider_event_id=provider_event_id,
                provider_event_type=provider_event_type,
            )
            return
        yield ProviderStreamEvent(
            ProviderStreamEventType.IGNORED,
            route=route,
            attempt=attempt,
            provider_event_id=provider_event_id,
            provider_event_type=provider_event_type,
            metadata={"reason": "unknown_event_ignored"},
        )


__all__ = ["ChatCompletionsAdapter"]
