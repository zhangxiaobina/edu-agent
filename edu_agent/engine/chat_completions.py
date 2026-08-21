"""Synchronous OpenAI-compatible Chat Completions provider adapter.

The adapter owns the SDK boundary.  Callers use the normalized ``Engine``
request shape and receive the repository's ``EngineResponse`` shape, while
the provider-specific request/response objects stay here.  Streaming and the
Responses API intentionally do not belong to this module yet.
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from typing import Any

from .base import EngineResponse, ToolCall
from .gateway import (
    ApiMode,
    ProviderCapabilities,
    ProviderCapabilityError,
    ResolvedRoute,
    estimate_request_tokens,
)

_MISSING = object()


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


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
    capabilities = ProviderCapabilities()

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
                    max_retries=0,
                )
            self._clients[key] = client
            return client

    def build_request(
        self,
        route: ResolvedRoute,
        messages: list[dict],
        tools: list[dict],
    ) -> dict[str, Any]:
        """Build the exact non-streaming Chat Completions request payload."""
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
            and estimate_request_tokens(messages, tools) > context_limit
        ):
            raise ProviderCapabilityError(("context_window",))
        request: dict[str, Any] = {
            "model": route.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if tools:
            request["tools"] = tools
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
        request = self.build_request(route, messages, tools)
        # Do not catch SDK exceptions: callers (including ResilientEngine) use
        # their concrete types/status codes to classify retry behavior.
        response = self._client_for(route).chat.completions.create(**request)
        return self.normalize_response(response, route=route)

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


__all__ = ["ChatCompletionsAdapter"]
