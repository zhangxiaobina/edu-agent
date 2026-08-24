from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

from ..data_classification import redact_text
from .base import EngineResponse, ToolCall
from ..runtime.cancellation import (
    CancellationToken,
    accepts_cancellation_token,
    accepts_keyword_argument,
)

if TYPE_CHECKING:
    from .gateway import ResolvedRoute


class ProviderStreamEventType(str, Enum):
    TEXT_DELTA = "text.delta"
    TOOL_CALL_ID_DELTA = "tool_call.id.delta"
    TOOL_CALL_NAME_DELTA = "tool_call.name.delta"
    TOOL_CALL_ARGUMENTS_DELTA = "tool_call.arguments.delta"
    USAGE = "usage"
    COMPLETED = "completed"
    ERROR = "error"
    IGNORED = "ignored"


class ProviderStreamProtocolError(RuntimeError):
    """The provider stream cannot be normalized without guessing."""

    def __init__(self, message: str, *, code: str = "provider_stream_protocol_error"):
        self.code = code
        super().__init__(message)


def read_stream_json_response(stream: Any) -> Mapping[str, Any] | None:
    """Read a non-SSE JSON body returned to an SDK streaming request.

    Some compatibility endpoints ignore ``stream=true`` and return one regular
    JSON response. The OpenAI SDK still exposes a Stream whose underlying
    ``httpx.Response`` has not been read, so accessing ``response.content`` is
    not safe until ``read()`` has consumed that same response.
    """
    response = getattr(stream, "response", None)
    if response is None:
        return None
    headers = getattr(response, "headers", None)
    get_header = getattr(headers, "get", None)
    content_type = get_header("content-type", "") if callable(get_header) else ""
    if not isinstance(content_type, str):
        return None
    media_type = content_type.partition(";")[0].strip().lower()
    if media_type != "application/json" and not media_type.endswith("+json"):
        return None
    read = getattr(response, "read", None)
    if not callable(read):
        raise ProviderStreamProtocolError(
            "provider JSON stream response 无法读取",
            code="provider_stream_unreadable_json_response",
        )
    body = read()
    if not isinstance(body, (bytes, bytearray, memoryview)):
        raise ProviderStreamProtocolError(
            "provider JSON stream response body 必须是 bytes",
            code="provider_stream_invalid_json_response",
        )
    try:
        decoded = json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderStreamProtocolError(
            "provider JSON stream response 无效",
            code="provider_stream_invalid_json_response",
        ) from error
    if not isinstance(decoded, Mapping):
        raise ProviderStreamProtocolError(
            "provider JSON stream response 必须是 object",
            code="provider_stream_invalid_json_response",
        )
    return decoded


@dataclass(frozen=True)
class ProviderStreamEvent:
    """One normalized provider event before RunEvent/SSE publication.

    Tool-call deltas are deliberately incomplete and non-executable. Only
    ``ProviderStreamAggregator`` creates ``ToolCall`` objects, after the provider
    emits a completed event for the same route and attempt.
    """

    event_type: ProviderStreamEventType | str
    route: ResolvedRoute
    attempt: int
    provider_event_id: str | None
    provider_event_type: str | None = None
    delta: str | None = None
    tool_call_index: int | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None
    model: str | None = None
    content_default: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    error: Exception | None = field(default=None, repr=False, compare=False)
    retryable: bool = False
    continuation: Literal["retry", "fallback"] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            event_type = ProviderStreamEventType(self.event_type)
        except (TypeError, ValueError) as error:
            raise ValueError("provider stream event_type 无效") from error
        object.__setattr__(self, "event_type", event_type)
        if not hasattr(self.route, "identity"):
            raise ValueError("provider stream route 无效")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt <= 0:
            raise ValueError("provider stream attempt 必须是正整数")
        for name in ("provider_event_id", "provider_event_type"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"provider stream {name} 必须是非空字符串或 None")
        if not isinstance(self.usage, Mapping):
            raise ValueError("provider stream usage 必须是 object")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("provider stream metadata 必须是 object")
        object.__setattr__(self, "usage", copy.deepcopy(dict(self.usage)))
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))

        delta_types = {
            ProviderStreamEventType.TEXT_DELTA,
            ProviderStreamEventType.TOOL_CALL_ID_DELTA,
            ProviderStreamEventType.TOOL_CALL_NAME_DELTA,
            ProviderStreamEventType.TOOL_CALL_ARGUMENTS_DELTA,
        }
        tool_delta_types = delta_types - {ProviderStreamEventType.TEXT_DELTA}
        if event_type in delta_types:
            if not isinstance(self.delta, str) or not self.delta:
                raise ValueError("provider stream delta 必须是非空字符串")
        elif self.delta is not None:
            raise ValueError("非 delta provider stream event 不得携带 delta")
        if event_type in tool_delta_types:
            if (
                isinstance(self.tool_call_index, bool)
                or not isinstance(self.tool_call_index, int)
                or self.tool_call_index < 0
            ):
                raise ValueError("tool call delta 必须携带非负 index")
        elif self.tool_call_index is not None:
            raise ValueError("非 tool call delta 不得携带 tool_call_index")

        if event_type is ProviderStreamEventType.USAGE:
            if not self.usage:
                raise ValueError("usage event 不得为空")
        elif self.usage:
            raise ValueError("非 usage event 不得携带 usage")
        if event_type is ProviderStreamEventType.COMPLETED:
            if not isinstance(self.finish_reason, str) or not self.finish_reason:
                raise ValueError("completed event 必须携带 finish_reason")
            if self.content_default is not None and not isinstance(self.content_default, str):
                raise ValueError("completed content_default 必须是 text 或 None")
        elif any(
            value is not None
            for value in (self.finish_reason, self.model, self.content_default)
        ):
            raise ValueError("只有 completed event 可以携带完成元数据")
        if event_type is ProviderStreamEventType.ERROR:
            if not isinstance(self.error_code, str) or not self.error_code:
                raise ValueError("error event 必须携带 error_code")
            if not isinstance(self.error_message, str) or not self.error_message:
                raise ValueError("error event 必须携带 error_message")
        elif any(
            value is not None for value in (self.error_code, self.error_message, self.error)
        ) or self.retryable or self.continuation is not None:
            raise ValueError("只有 error event 可以携带错误或重试元数据")

    @property
    def is_delta(self) -> bool:
        return self.event_type in {
            ProviderStreamEventType.TEXT_DELTA,
            ProviderStreamEventType.TOOL_CALL_ID_DELTA,
            ProviderStreamEventType.TOOL_CALL_NAME_DELTA,
            ProviderStreamEventType.TOOL_CALL_ARGUMENTS_DELTA,
        }

    @property
    def is_terminal(self) -> bool:
        return self.event_type is ProviderStreamEventType.COMPLETED or (
            self.event_type is ProviderStreamEventType.ERROR
            and self.continuation is None
        )


def provider_stream_error_event(
    *,
    route: ResolvedRoute,
    attempt: int,
    error: Exception,
    provider_event_id: str | None = None,
    provider_event_type: str | None = None,
    code: str | None = None,
    retryable: bool = False,
    continuation: Literal["retry", "fallback"] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ProviderStreamEvent:
    resolved_code = code or getattr(error, "code", None) or type(error).__name__
    credential = None
    credential_ref = getattr(route, "credential", None)
    resolve_credential = getattr(credential_ref, "resolve", None)
    if callable(resolve_credential):
        try:
            candidate = resolve_credential()
        except Exception:
            candidate = None
        if isinstance(candidate, str) and candidate:
            credential = candidate
    redaction_options = {
        "include_pii": True,
        "literal_secrets": (credential,) if credential is not None else (),
    }
    rendered_message = redact_text(
        str(error) or type(error).__name__,
        **redaction_options,
    )
    return ProviderStreamEvent(
        ProviderStreamEventType.ERROR,
        route=route,
        attempt=attempt,
        provider_event_id=provider_event_id,
        provider_event_type=provider_event_type,
        error_code=redact_text(str(resolved_code), **redaction_options),
        error_message=rendered_message,
        error=error,
        retryable=retryable,
        continuation=continuation,
        metadata=dict(metadata or {}),
    )


@dataclass
class _ToolCallParts:
    call_id: list[str] = field(default_factory=list)
    name: list[str] = field(default_factory=list)
    arguments: list[str] = field(default_factory=list)


class ProviderStreamAggregator:
    """Incrementally aggregate one retry/fallback-aware provider stream."""

    def __init__(self) -> None:
        self._content: list[str] = []
        self._tool_calls: dict[int, _ToolCallParts] = {}
        self._usage: dict[str, Any] = {}
        self._route_identity: object | None = None
        self._attempt: int | None = None
        self._visible = False
        self._response: EngineResponse | None = None
        self._awaiting_continuation = False

    @property
    def visible(self) -> bool:
        return self._visible

    @property
    def completed(self) -> bool:
        return self._response is not None

    @property
    def response(self) -> EngineResponse | None:
        return self._response

    def _reset_attempt(self) -> None:
        self._content.clear()
        self._tool_calls.clear()
        self._usage.clear()
        self._route_identity = None
        self._attempt = None
        self._visible = False
        self._awaiting_continuation = True

    def _bind(self, event: ProviderStreamEvent) -> None:
        identity = event.route.identity
        if self._attempt is None:
            self._attempt = event.attempt
            self._route_identity = identity
            self._awaiting_continuation = False
            return
        if event.attempt != self._attempt or identity != self._route_identity:
            raise ProviderStreamProtocolError(
                "provider stream route/attempt 在未声明 retry/fallback 时发生变化",
                code="provider_stream_attempt_mismatch",
            )

    def feed(self, event: ProviderStreamEvent) -> EngineResponse | None:
        if not isinstance(event, ProviderStreamEvent):
            raise TypeError("provider stream 只能聚合 ProviderStreamEvent")
        if self._response is not None:
            raise ProviderStreamProtocolError(
                "provider stream completed 后仍收到事件",
                code="provider_stream_event_after_completed",
            )
        if event.event_type is ProviderStreamEventType.IGNORED:
            return None
        if event.event_type is ProviderStreamEventType.ERROR:
            if event.continuation is not None:
                if self._visible:
                    raise ProviderStreamProtocolError(
                        "provider 已输出 delta 后不得 retry/fallback",
                        code="provider_stream_retry_after_delta",
                    )
                self._reset_attempt()
                return None
            if event.error is not None:
                if self._visible:
                    try:
                        setattr(event.error, "stream_visible", True)
                    except Exception:
                        pass
                raise event.error
            raise ProviderStreamProtocolError(
                event.error_message or "provider stream failed",
                code=event.error_code or "provider_stream_error",
            )

        self._bind(event)
        if event.event_type is ProviderStreamEventType.TEXT_DELTA:
            self._content.append(event.delta or "")
            self._visible = True
        elif event.event_type in {
            ProviderStreamEventType.TOOL_CALL_ID_DELTA,
            ProviderStreamEventType.TOOL_CALL_NAME_DELTA,
            ProviderStreamEventType.TOOL_CALL_ARGUMENTS_DELTA,
        }:
            assert event.tool_call_index is not None
            parts = self._tool_calls.setdefault(event.tool_call_index, _ToolCallParts())
            target = {
                ProviderStreamEventType.TOOL_CALL_ID_DELTA: parts.call_id,
                ProviderStreamEventType.TOOL_CALL_NAME_DELTA: parts.name,
                ProviderStreamEventType.TOOL_CALL_ARGUMENTS_DELTA: parts.arguments,
            }[event.event_type]
            target.append(event.delta or "")
            self._visible = True
        elif event.event_type is ProviderStreamEventType.USAGE:
            self._usage.update(copy.deepcopy(event.usage))
        elif event.event_type is ProviderStreamEventType.COMPLETED:
            tool_calls: list[ToolCall] = []
            seen_call_ids: set[str] = set()
            for index in sorted(self._tool_calls):
                parts = self._tool_calls[index]
                call_id = "".join(parts.call_id)
                name = "".join(parts.name)
                if not call_id or not name:
                    raise ProviderStreamProtocolError(
                        f"provider tool call[{index}] 缺少完整 id/name",
                        code="provider_stream_incomplete_tool_call",
                    )
                if call_id in seen_call_ids:
                    raise ProviderStreamProtocolError(
                        "provider stream 返回重复 tool call id",
                        code="provider_stream_duplicate_tool_call",
                    )
                seen_call_ids.add(call_id)
                tool_calls.append(
                    ToolCall(
                        id=call_id,
                        name=name,
                        arguments="".join(parts.arguments),
                    )
                )
            content = "".join(self._content) if self._content else event.content_default
            self._response = EngineResponse(
                content=content,
                tool_calls=tool_calls,
                usage=copy.deepcopy(self._usage),
                finish_reason=event.finish_reason,
                model=event.model,
            )
            return self._response
        return None

    def result(self) -> EngineResponse:
        if self._response is None:
            raise ProviderStreamProtocolError(
                "provider stream 未产生 completed event",
                code="provider_stream_missing_completed",
            )
        return self._response


def aggregate_provider_stream(events: Iterable[ProviderStreamEvent]) -> EngineResponse:
    aggregator = ProviderStreamAggregator()
    for event in events:
        aggregator.feed(event)
    return aggregator.result()


def consume_provider_stream(
    engine: Any,
    messages: list[dict],
    tools: list[dict],
    *,
    cancellation_token: CancellationToken | None = None,
    max_output_tokens: int | None = None,
    event_sink: Callable[[ProviderStreamEvent], None] | None = None,
) -> EngineResponse:
    """Consume one engine stream while exposing the exact normalized events."""

    stream = getattr(engine, "stream_chat", None) if event_sink is not None else None
    if not callable(stream) and event_sink is not None:
        stream = getattr(engine, "stream_events", None)
    if not callable(stream):
        chat = engine.chat
        if cancellation_token is not None:
            cancellation_token.checkpoint("model.before_sync_call")
        kwargs = {}
        if cancellation_token is not None and accepts_cancellation_token(chat):
            kwargs["cancellation_token"] = cancellation_token
        if max_output_tokens is not None and accepts_keyword_argument(chat, "max_output_tokens"):
            kwargs["max_output_tokens"] = max_output_tokens
        response = chat(messages, tools, **kwargs)
        if cancellation_token is not None:
            cancellation_token.checkpoint("model.after_sync_call")
        return response

    kwargs = {"attempt": 1}
    if cancellation_token is not None and accepts_cancellation_token(stream):
        kwargs["cancellation_token"] = cancellation_token
    if max_output_tokens is not None and accepts_keyword_argument(stream, "max_output_tokens"):
        kwargs["max_output_tokens"] = max_output_tokens
    iterator = iter(stream(messages, tools, **kwargs))
    aggregator = ProviderStreamAggregator()
    try:
        for event in iterator:
            if cancellation_token is not None:
                cancellation_token.checkpoint("provider.stream.receive")
            if event_sink is not None:
                event_sink(event)
            aggregator.feed(event)
    except Exception as error:
        if aggregator.visible:
            try:
                setattr(error, "stream_visible", True)
            except Exception:
                pass
        raise
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            close()
    if cancellation_token is not None:
        cancellation_token.checkpoint("provider.stream.completed")
    return aggregator.result()


__all__ = [
    "ProviderStreamAggregator",
    "ProviderStreamEvent",
    "ProviderStreamEventType",
    "ProviderStreamProtocolError",
    "aggregate_provider_stream",
    "provider_stream_error_event",
    "read_stream_json_response",
]
