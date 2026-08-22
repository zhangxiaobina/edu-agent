from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import openai
import pytest

from edu_agent.engine import (
    ApiMode,
    ChatCompletionsAdapter,
    CredentialRef,
    GatewayEngine,
    OpenAICompatEngine,
    ProviderCapabilities,
    ProviderGateway,
    ProviderSpec,
    ResilientEngine,
    get_engine,
)
from edu_agent.runtime.config import ModelConfig


def _spec(**overrides) -> ProviderSpec:
    values = {
        "model": "route-model",
        "endpoint": "https://provider.example/v1",
        "api_mode": ApiMode.CHAT_COMPLETIONS,
    }
    values.update(overrides)
    return ProviderSpec(**values)


def _sdk_client(handler, *, timeout: float = 9.5):
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = openai.OpenAI(
        api_key="wire-test-key",
        base_url="https://provider.example/v1",
        timeout=timeout,
        max_retries=0,
        http_client=http_client,
    )
    return client


def test_gateway_chat_completions_preserves_wire_and_response_shape():
    requests: list[dict] = []
    responses = [
        {
            "id": "chatcmpl-null",
            "object": "chat.completion",
            "created": 1,
            "model": "response-null-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": None},
                    "finish_reason": "stop",
                }
            ],
        },
        {
            "id": "chatcmpl-tools",
            "object": "chat.completion",
            "created": 2,
            "model": "response-tool-model",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-empty",
                                "type": "function",
                                "function": {
                                    "name": "list_exams",
                                    "arguments": "",
                                },
                            },
                            {
                                "id": "call-json",
                                "type": "function",
                                "function": {
                                    "name": "query_student_scores",
                                    "arguments": '{"exam_id":"17"}',
                                },
                            },
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
            },
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            {
                "method": request.method,
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": json.loads(request.content),
                "timeout": request.extensions.get("timeout"),
            }
        )
        return httpx.Response(200, json=responses[len(requests) - 1])

    client = _sdk_client(handler)
    adapter = ChatCompletionsAdapter(client, temperature=0.25, timeout=9.5)
    gateway = ProviderGateway(adapters={ApiMode.CHAT_COMPLETIONS: adapter})
    engine = GatewayEngine(gateway, _spec(), name="wire")
    messages = [{"role": "user", "content": "hello"}]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "list_exams",
                "description": "List exams",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    try:
        without_tools = engine.chat(messages, [])
        with_tools = engine.chat(messages, tools)
    finally:
        client.close()

    assert requests[0]["method"] == "POST"
    assert requests[0]["url"] == "https://provider.example/v1/chat/completions"
    assert requests[0]["headers"]["authorization"] == "Bearer wire-test-key"
    assert requests[0]["body"] == {
        "messages": messages,
        "model": "route-model",
        "stream": True,
        "stream_options": {"include_usage": True},
        "temperature": 0.25,
    }
    assert set(requests[0]["timeout"].values()) == {9.5}
    assert requests[1]["body"] == {
        "messages": messages,
        "model": "route-model",
        "stream": True,
        "stream_options": {"include_usage": True},
        "temperature": 0.25,
        "tools": tools,
        "tool_choice": "auto",
    }

    assert without_tools.content is None
    assert without_tools.tool_calls == []
    assert without_tools.usage == {}
    assert without_tools.finish_reason == "stop"
    assert without_tools.model == "response-null-model"

    assert with_tools.content == ""
    assert [(call.id, call.name, call.arguments) for call in with_tools.tool_calls] == [
        ("call-empty", "list_exams", ""),
        ("call-json", "query_student_scores", '{"exam_id":"17"}'),
    ]
    assert with_tools.usage["prompt_tokens"] == 11
    assert with_tools.usage["completion_tokens"] == 7
    assert with_tools.usage["total_tokens"] == 18
    assert with_tools.finish_reason == "tool_calls"
    assert with_tools.model == "response-tool-model"
    assert with_tools.to_assistant_message()["tool_calls"][0]["function"][
        "arguments"
    ] == ""


def test_plain_provider_gateway_has_the_default_chat_adapter():
    gateway = ProviderGateway()
    adapter = gateway.adapter_for(gateway.begin_turn(_spec()))
    assert isinstance(adapter, ChatCompletionsAdapter)
    assert adapter.api_mode is ApiMode.CHAT_COMPLETIONS


def test_chat_tool_capability_fails_before_client_call():
    calls = 0

    class FakeCompletions:
        def create(self, **request):
            nonlocal calls
            calls += 1
            raise AssertionError("client must not be called")

    engine = GatewayEngine(
        ProviderGateway(
            adapters={
                ApiMode.CHAT_COMPLETIONS: ChatCompletionsAdapter(
                    SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
                )
            }
        ),
        _spec(capabilities=ProviderCapabilities(tool_calling=False)),
    )

    with pytest.raises(ValueError, match="不支持 tool calling"):
        engine.chat(
            [{"role": "user", "content": "hello"}],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "list_exams",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        )
    assert calls == 0


def test_chat_completions_timeout_propagates_sdk_exception():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("provider timed out", request=request)

    client = _sdk_client(handler, timeout=0.01)
    engine = GatewayEngine(
        ProviderGateway(
            adapters={ApiMode.CHAT_COMPLETIONS: ChatCompletionsAdapter(client)}
        ),
        _spec(),
    )
    try:
        with pytest.raises(openai.APITimeoutError) as caught:
            engine.chat([{"role": "user", "content": "slow"}], [])
    finally:
        client.close()

    assert calls == 1
    assert isinstance(caught.value.__cause__, httpx.ReadTimeout)


def test_chat_completions_http_error_propagates_sdk_exception():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "invalid request fixture",
                    "type": "invalid_request_error",
                    "param": "messages",
                    "code": "invalid_messages",
                }
            },
        )

    client = _sdk_client(handler)
    engine = GatewayEngine(
        ProviderGateway(
            adapters={ApiMode.CHAT_COMPLETIONS: ChatCompletionsAdapter(client)}
        ),
        _spec(),
    )
    try:
        with pytest.raises(openai.BadRequestError) as caught:
            engine.chat([{"role": "user", "content": "bad"}], [])
    finally:
        client.close()

    assert caught.value.status_code == 400
    assert caught.value.code == "invalid_messages"


def test_adapter_builds_sdk_client_from_route_credential_and_timeout(
    monkeypatch,
):
    constructors: list[dict] = []
    requests: list[dict] = []

    class FakeCompletions:
        def create(self, **request):
            requests.append(request)
            return {
                "model": "fake-response-model",
                "choices": [
                    {
                        "message": {"content": "ok", "tool_calls": []},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"total_tokens": 3},
            }

    class FakeOpenAI:
        def __init__(self, **kwargs):
            constructors.append(kwargs)
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    monkeypatch.setenv("R1_CHAT_CREDENTIAL", "credential-canary-4821")
    spec = _spec(credential=CredentialRef("R1_CHAT_CREDENTIAL"))
    adapter = ChatCompletionsAdapter(temperature=0.4, timeout=12.5)
    engine = GatewayEngine(
        ProviderGateway(adapters={ApiMode.CHAT_COMPLETIONS: adapter}),
        spec,
    )

    response = engine.chat([{"role": "user", "content": "hello"}], [])

    assert constructors == [
        {
            "base_url": "https://provider.example/v1",
            "api_key": "credential-canary-4821",
            "timeout": 12.5,
            "max_retries": 0,
        }
    ]
    assert requests[0]["temperature"] == 0.4
    assert response.usage == {"total_tokens": 3}


def test_legacy_engine_is_a_thin_gateway_compatibility_facade(monkeypatch):
    captured: list[dict] = []

    class FakeCompletions:
        def create(self, **request):
            captured.append(request)
            return {
                "model": "legacy-response-model",
                "choices": [
                    {
                        "message": {"content": "legacy-ok"},
                        "finish_reason": "stop",
                    }
                ],
            }

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )
    legacy = OpenAICompatEngine(
        base_url="http://127.0.0.1:8000/v1",
        api_key="local-placeholder",
        model="legacy-model",
        client=fake_client,
    )
    assert isinstance(legacy, GatewayEngine)
    assert legacy.gateway.adapter_for(legacy.route) is legacy._adapter
    assert legacy.chat([{"role": "user", "content": "hello"}], []).content == (
        "legacy-ok"
    )
    assert captured[0]["model"] == "legacy-model"

    for name in (
        "EDU_AGENT_API_MODE",
        "EDU_AGENT_PROVIDER",
        "EDU_AGENT_DEPLOYMENT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EDU_AGENT_ENGINE", "openai")
    preferred = get_engine(
        base_url="http://127.0.0.1:8000/v1",
        api_key="local-placeholder",
        model="preferred-model",
        client=fake_client,
    )
    assert type(preferred) is GatewayEngine
    assert preferred.model == "preferred-model"


def test_factory_keeps_dashscope_primary_and_vllm_fallback_compatible():
    clients: list[tuple[str, str, str]] = []
    requests: list[tuple[str, dict]] = []

    def client_factory(route):
        clients.append(
            (
                route.model,
                route.endpoint,
                route.credential.environment_variable,
            )
        )

        class FakeCompletions:
            def create(self, **request):
                requests.append((route.model, request))
                if route.model == "qwen-plus":
                    raise TimeoutError("dashscope fixture timeout")
                return {
                    "model": "Qwen/Qwen3-14B",
                    "choices": [
                        {
                            "message": {"content": "fallback-ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"total_tokens": 5},
                }

        return SimpleNamespace(
            chat=SimpleNamespace(completions=FakeCompletions())
        )

    config = ModelConfig(
        model="qwen-plus",
        endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1",
        vendor="dashscope",
        max_retries=0,
        fallback_model="Qwen/Qwen3-14B",
        fallback_base_url="http://127.0.0.1:8001/v1",
        fallback_context_window_tokens=32_768,
    )
    engine = get_engine(config, client_factory=client_factory)

    response = engine.chat([{"role": "user", "content": "hello"}], [])

    assert isinstance(engine, ResilientEngine)
    assert clients == [
        (
            "qwen-plus",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "EDU_AGENT_API_KEY",
        ),
        (
            "Qwen/Qwen3-14B",
            "http://127.0.0.1:8001/v1",
            "EDU_AGENT_FALLBACK_API_KEY",
        ),
    ]
    assert [request[1]["model"] for request in requests] == [
        "qwen-plus",
        "Qwen/Qwen3-14B",
    ]
    assert response.content == "fallback-ok"
    assert response.usage == {
        "total_tokens": 5,
        "runtime_attempts": 2,
        "fallback_used": True,
        "primary_failure": "timeout",
        "circuit_state": "closed",
    }
