from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest

from edu_agent.engine import (
    ApiMode,
    ChatCompletionsAdapter,
    GatewayEngine,
    ProviderCapabilities,
    ProviderGateway,
    ProviderSpec,
    ProviderStreamAggregator,
    ProviderStreamEvent,
    ProviderStreamEventType,
    ProviderStreamProtocolError,
    ResilientEngine,
    ResponsesAdapter,
    aggregate_provider_stream,
)
from edu_agent.runtime.cancellation import CancellationRequested, CancellationToken


FIXTURES = Path(__file__).parent / "fixtures" / "provider_streams"


class FragmentedByteStream(httpx.SyncByteStream):
    def __init__(self, payload: bytes):
        marker = "你".encode()
        split = payload.index(marker) + 1
        self.chunks = (
            payload[:17],
            payload[17:split],
            payload[split : split + 1],
            payload[split + 1 :],
        )

    def __iter__(self):
        yield from self.chunks


class UnreadJSONByteStream(httpx.SyncByteStream):
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def __iter__(self):
        yield self.payload


class InterruptedByteStream(httpx.SyncByteStream):
    def __init__(self, request: httpx.Request, *chunks: bytes):
        self.request = request
        self.chunks = chunks

    def __iter__(self):
        yield from self.chunks
        raise httpx.ReadError("stream disconnected", request=self.request)


def _client(fixture_name: str, requests: list[dict]):
    payload = (FIXTURES / fixture_name).read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream; charset=utf-8"},
            stream=FragmentedByteStream(payload),
        )

    return openai.OpenAI(
        api_key="stream-fixture-key",
        base_url="https://provider.example/v1",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _engine(mode: ApiMode, adapter) -> GatewayEngine:
    return GatewayEngine(
        ProviderGateway(adapters={mode: adapter}),
        ProviderSpec(
            model="stream-model",
            endpoint="https://provider.example/v1",
            api_mode=mode,
        ),
    )


@pytest.mark.parametrize(
    ("mode", "adapter_type", "fixture_name"),
    [
        (ApiMode.CHAT_COMPLETIONS, ChatCompletionsAdapter, "chat_completions.sse"),
        (ApiMode.RESPONSES, ResponsesAdapter, "responses.sse"),
    ],
)
def test_real_sdk_wire_stream_handles_utf8_interleaved_calls_usage_and_sync_equivalence(
    mode,
    adapter_type,
    fixture_name,
):
    requests: list[dict] = []
    client = _client(fixture_name, requests)
    engine = _engine(mode, adapter_type(client))
    messages = [{"role": "user", "content": "检查两个调用"}]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "list_exams",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    try:
        events = list(engine.stream_chat(messages, tools, attempt=3))
        streamed = aggregate_provider_stream(events)
        synchronous = engine.chat(messages, tools)
    finally:
        client.close()

    assert requests[0]["stream"] is True
    assert requests[1]["stream"] is True
    if mode is ApiMode.CHAT_COMPLETIONS:
        assert requests[0]["stream_options"] == {"include_usage": True}
    assert streamed == synchronous
    assert streamed.content == "你好"
    assert [(call.id, call.name, call.arguments) for call in streamed.tool_calls] == [
        ("call-a", "list_exams", '{"course_id":7}'),
        ("call-b", "query_student_scores", '{"exam_id":17}'),
    ]
    assert streamed.usage["total_tokens"] == 18
    assert streamed.finish_reason == "tool_calls"
    assert all(event.route is engine.route for event in events)
    assert {event.attempt for event in events} == {3}
    assert all(event.provider_event_id for event in events)
    assert events[-1].event_type is ProviderStreamEventType.COMPLETED
    usage_index = next(
        index
        for index, event in enumerate(events)
        if event.event_type is ProviderStreamEventType.USAGE
    )
    assert usage_index < len(events) - 1
    arguments = [
        event
        for event in events
        if event.event_type is ProviderStreamEventType.TOOL_CALL_ARGUMENTS_DELTA
    ]
    expected_indexes = (
        [1, 2, 1, 2] if mode is ApiMode.RESPONSES else [0, 1, 0, 1]
    )
    assert [event.tool_call_index for event in arguments] == expected_indexes


@pytest.mark.parametrize(
    ("mode", "adapter_type", "body"),
    [
        (
            ApiMode.CHAT_COMPLETIONS,
            ChatCompletionsAdapter,
            {
                "id": "chatcmpl-json-fallback",
                "object": "chat.completion",
                "model": "json-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "json-body"},
                        "finish_reason": "stop",
                    }
                ],
            },
        ),
        (
            ApiMode.RESPONSES,
            ResponsesAdapter,
            {
                "id": "resp-json-fallback",
                "object": "response",
                "model": "json-model",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "json-body"}],
                    }
                ],
            },
        ),
    ],
)
def test_streaming_request_reuses_unread_json_response_without_second_request(
    mode,
    adapter_type,
    body,
):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "application/json; charset=utf-8"},
            stream=UnreadJSONByteStream(body),
        )

    client = openai.OpenAI(
        api_key="json-fixture-key",
        base_url="https://provider.example/v1",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    engine = _engine(mode, adapter_type(client))
    try:
        response = engine.chat([], [])
    finally:
        client.close()

    assert response.content == "json-body"
    assert response.model == "json-model"
    assert len(requests) == 1
    assert requests[0]["stream"] is True


@pytest.mark.parametrize(
    ("adapter_type", "client_factory"),
    [
        (
            ChatCompletionsAdapter,
            lambda endpoint: SimpleNamespace(
                chat=SimpleNamespace(completions=endpoint)
            ),
        ),
        (
            ResponsesAdapter,
            lambda endpoint: SimpleNamespace(responses=endpoint),
        ),
    ],
)
def test_adapter_closes_sync_sdk_stream_returned_after_cancellation(
    adapter_type,
    client_factory,
):
    token = CancellationToken()

    class LateStream:
        closed = False

        def close(self):
            self.closed = True

        def __iter__(self):
            raise AssertionError("cancelled late stream must not be consumed")

    stream = LateStream()

    class Endpoint:
        def create(self, **_request):
            token.cancel("deadline elapsed", source="deadline")
            return stream

    route = ProviderGateway().begin_turn(
        ProviderSpec(
            model="stream-model",
            endpoint="https://provider.example/v1",
            api_mode=adapter_type.api_mode,
        )
    )
    adapter = adapter_type(client_factory(Endpoint()))

    with pytest.raises(CancellationRequested, match="after_request"):
        list(adapter.stream_events(route, [], [], cancellation_token=token))
    assert stream.closed


def test_fragmented_tool_json_is_not_materialized_before_completed():
    requests: list[dict] = []
    client = _client("chat_completions.sse", requests)
    engine = _engine(ApiMode.CHAT_COMPLETIONS, ChatCompletionsAdapter(client))
    try:
        events = list(engine.stream_chat([], [], attempt=1))
    finally:
        client.close()

    aggregator = ProviderStreamAggregator()
    for event in events[:-1]:
        assert aggregator.feed(event) is None
        assert aggregator.response is None
    response = aggregator.feed(events[-1])

    assert response is not None
    assert response.tool_calls[0].arguments == '{"course_id":7}'


class APIConnectionError(Exception):
    pass


class ScriptedAdapter:
    api_mode = ApiMode.CHAT_COMPLETIONS
    capabilities = ProviderCapabilities(
        streaming=True,
        context_window_tokens=16_384,
    )

    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = 0

    def chat(self, route, messages, tools):
        return aggregate_provider_stream(self.stream_events(route, messages, tools))

    def stream_events(self, route, messages, tools, *, attempt=1):
        self.calls += 1
        yield from self.behavior(self.calls, route, attempt)


def _scripted_engine(adapter, *, model="primary", endpoint="https://primary.example/v1"):
    return GatewayEngine(
        ProviderGateway(adapters={ApiMode.CHAT_COMPLETIONS: adapter}),
        ProviderSpec(
            model=model,
            endpoint=endpoint,
            api_mode=ApiMode.CHAT_COMPLETIONS,
            capabilities=adapter.capabilities,
        ),
    )


def _error(route, attempt, event_id="error"):
    error = APIConnectionError("stream disconnected")
    return ProviderStreamEvent(
        ProviderStreamEventType.ERROR,
        route=route,
        attempt=attempt,
        provider_event_id=event_id,
        provider_event_type="fake.error",
        error_code="connection",
        error_message=str(error),
        error=error,
        retryable=True,
    )


def test_retry_before_visible_delta_ignores_late_old_attempt_and_sync_aggregates():
    fixture = json.loads((FIXTURES / "fake_attempts.json").read_text())[
        "retry_before_delta"
    ]

    def behavior(call, route, attempt):
        if call == 1:
            assert fixture[0]["type"] == "error"
            yield _error(route, attempt, "attempt-1-error")
            return
        yield ProviderStreamEvent(
            ProviderStreamEventType.TEXT_DELTA,
            route=route,
            attempt=attempt - 1,
            provider_event_id="attempt-1-late",
            provider_event_type="fake.delta",
            delta=fixture[1]["delta"],
        )
        yield ProviderStreamEvent(
            ProviderStreamEventType.TEXT_DELTA,
            route=route,
            attempt=attempt,
            provider_event_id="attempt-2-delta",
            provider_event_type="fake.delta",
            delta=fixture[2]["delta"],
        )
        yield ProviderStreamEvent(
            ProviderStreamEventType.COMPLETED,
            route=route,
            attempt=attempt,
            provider_event_id="attempt-2-completed",
            provider_event_type="fake.completed",
            finish_reason="stop",
            model=route.model,
        )

    audits: list[dict] = []
    adapter = ScriptedAdapter(behavior)
    engine = ResilientEngine(
        _scripted_engine(adapter),
        max_retries=1,
        sleeper=lambda _delay: None,
        random_source=lambda: 0.0,
        event_sink=audits.append,
    )

    events = list(engine.stream_chat([], []))
    response = aggregate_provider_stream(events)

    assert response.content == "selected"
    assert response.usage["runtime_attempts"] == 2
    assert [event.continuation for event in events if event.event_type is ProviderStreamEventType.ERROR] == ["retry"]
    assert any(
        event.event_type is ProviderStreamEventType.IGNORED
        and event.metadata["reason"] == "stale_attempt"
        for event in events
    )
    assert "late" not in response.content
    assert adapter.calls == 2
    assert any(
        event["event"] == "provider_stream_stale_event_ignored" for event in audits
    )

    adapter.calls = 0
    assert engine.chat([], []).content == "selected"


def test_real_sdk_transport_error_before_delta_retries_same_route():
    requests: list[dict] = []
    complete = (FIXTURES / "chat_completions.sse").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        stream = (
            InterruptedByteStream(request)
            if len(requests) == 1
            else FragmentedByteStream(complete)
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream; charset=utf-8"},
            stream=stream,
        )

    client = openai.OpenAI(
        api_key="transport-fixture-key",
        base_url="https://provider.example/v1",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    engine = ResilientEngine(
        _engine(ApiMode.CHAT_COMPLETIONS, ChatCompletionsAdapter(client)),
        max_retries=1,
        sleeper=lambda _delay: None,
        random_source=lambda: 0.0,
    )
    try:
        events = list(engine.stream_chat([], []))
    finally:
        client.close()

    assert len(requests) == 2
    assert aggregate_provider_stream(events).content == "你好"
    errors = [
        event for event in events if event.event_type is ProviderStreamEventType.ERROR
    ]
    assert [event.continuation for event in errors] == ["retry"]
    assert errors[0].metadata["failure_kind"] == "connection"


def test_real_sdk_transport_error_after_delta_is_terminal_without_retry():
    requests: list[dict] = []
    partial = (
        b'data: {"id":"chat-interrupted","object":"chat.completion.chunk",'
        b'"model":"stream-model","choices":[{"index":0,"delta":'
        b'{"content":"partial"},"finish_reason":null}]}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream; charset=utf-8"},
            stream=InterruptedByteStream(request, partial),
        )

    client = openai.OpenAI(
        api_key="transport-fixture-key",
        base_url="https://provider.example/v1",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    engine = ResilientEngine(
        _engine(ApiMode.CHAT_COMPLETIONS, ChatCompletionsAdapter(client)),
        max_retries=2,
        sleeper=lambda _delay: pytest.fail("visible stream must not retry"),
    )
    try:
        events = list(engine.stream_chat([], []))
    finally:
        client.close()

    assert len(requests) == 1
    assert [event.delta for event in events if event.is_delta] == ["partial"]
    assert events[-1].event_type is ProviderStreamEventType.ERROR
    assert events[-1].continuation is None
    assert events[-1].metadata["stream_visible"] is True
    with pytest.raises(httpx.ReadError, match="stream disconnected"):
        aggregate_provider_stream(events)


def test_failure_after_visible_delta_is_terminal_and_never_stitches_fallback():
    fixture = json.loads((FIXTURES / "fake_attempts.json").read_text())[
        "failure_after_delta"
    ]

    def primary_behavior(_call, route, attempt):
        yield ProviderStreamEvent(
            ProviderStreamEventType.TEXT_DELTA,
            route=route,
            attempt=attempt,
            provider_event_id="primary-delta",
            provider_event_type="fake.delta",
            delta=fixture[0]["delta"],
        )
        yield _error(route, attempt, "primary-error")

    def fallback_behavior(_call, route, attempt):
        yield ProviderStreamEvent(
            ProviderStreamEventType.TEXT_DELTA,
            route=route,
            attempt=attempt,
            provider_event_id="fallback-delta",
            provider_event_type="fake.delta",
            delta="must-not-appear",
        )
        yield ProviderStreamEvent(
            ProviderStreamEventType.COMPLETED,
            route=route,
            attempt=attempt,
            provider_event_id="fallback-completed",
            provider_event_type="fake.completed",
            finish_reason="stop",
            model=route.model,
        )

    primary_adapter = ScriptedAdapter(primary_behavior)
    fallback_adapter = ScriptedAdapter(fallback_behavior)
    audits: list[dict] = []
    engine = ResilientEngine(
        _scripted_engine(primary_adapter),
        fallback=_scripted_engine(
            fallback_adapter,
            model="fallback",
            endpoint="https://fallback.example/v1",
        ),
        max_retries=2,
        sleeper=lambda _delay: None,
        event_sink=audits.append,
    )

    events = list(engine.stream_chat([], []))

    assert [event.delta for event in events if event.is_delta] == ["partial"]
    assert events[-1].event_type is ProviderStreamEventType.ERROR
    assert events[-1].continuation is None
    assert events[-1].metadata["stream_visible"] is True
    assert primary_adapter.calls == 1
    assert fallback_adapter.calls == 0
    assert any(
        event["event"] == "fallback_rejected"
        and event["details"]["reason"] == "stream_already_visible"
        for event in audits
    )
    with pytest.raises(APIConnectionError):
        aggregate_provider_stream(events)


def test_transient_failure_before_delta_can_activate_compatible_fallback():
    def primary_behavior(_call, route, attempt):
        yield _error(route, attempt, "primary-error")

    def fallback_behavior(_call, route, attempt):
        yield ProviderStreamEvent(
            ProviderStreamEventType.TEXT_DELTA,
            route=route,
            attempt=attempt,
            provider_event_id="fallback-delta",
            provider_event_type="fake.delta",
            delta="fallback-selected",
        )
        yield ProviderStreamEvent(
            ProviderStreamEventType.COMPLETED,
            route=route,
            attempt=attempt,
            provider_event_id="fallback-completed",
            provider_event_type="fake.completed",
            finish_reason="stop",
            model=route.model,
        )

    primary_adapter = ScriptedAdapter(primary_behavior)
    fallback_adapter = ScriptedAdapter(fallback_behavior)
    engine = ResilientEngine(
        _scripted_engine(primary_adapter),
        fallback=_scripted_engine(
            fallback_adapter,
            model="fallback",
            endpoint="https://fallback.example/v1",
        ),
        max_retries=0,
    )

    events = list(engine.stream_chat([], []))
    response = aggregate_provider_stream(events)

    errors = [
        event for event in events if event.event_type is ProviderStreamEventType.ERROR
    ]
    assert [event.continuation for event in errors] == ["fallback"]
    assert response.content == "fallback-selected"
    assert response.usage["fallback_used"] is True
    assert response.usage["runtime_attempts"] == 2
    assert primary_adapter.calls == fallback_adapter.calls == 1


def test_unknown_responses_event_is_audited_and_policy_can_fail_closed():
    completed = {
        "type": "response.completed",
        "sequence_number": 2,
        "response": {
            "id": "resp-unknown",
            "model": "stream-model",
            "status": "completed",
            "output": [],
        },
    }

    class FakeResponses:
        def create(self, **_request):
            return iter(
                [
                    {"type": "response.future.delta", "sequence_number": 1},
                    completed,
                ]
            )

    route = ProviderGateway().begin_turn(
        ProviderSpec(
            model="stream-model",
            endpoint="https://provider.example/v1",
            api_mode=ApiMode.RESPONSES,
        )
    )
    audits: list[dict] = []
    ignoring = ResponsesAdapter(
        SimpleNamespace(responses=FakeResponses()),
        stream_event_sink=audits.append,
    )
    ignored_events = list(ignoring.stream_events(route, [], []))
    assert aggregate_provider_stream(ignored_events).content == ""
    assert ignored_events[0].event_type is ProviderStreamEventType.IGNORED
    assert audits[0]["provider_event_type"] == "response.future.delta"

    failing = ResponsesAdapter(
        SimpleNamespace(responses=FakeResponses()),
        unknown_event_policy="error",
    )
    with pytest.raises(ProviderStreamProtocolError, match="未知 Responses"):
        aggregate_provider_stream(failing.stream_events(route, [], []))


def test_unknown_chat_event_is_audited_and_policy_can_fail_closed():
    completed = {
        "id": "chat-unknown",
        "object": "chat.completion.chunk",
        "model": "stream-model",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }

    class FakeCompletions:
        def create(self, **_request):
            return iter(
                [
                    {"id": "future", "object": "chat.completion.future"},
                    completed,
                ]
            )

    route = ProviderGateway().begin_turn(
        ProviderSpec(model="stream-model", endpoint="https://provider.example/v1")
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    audits: list[dict] = []
    ignoring = ChatCompletionsAdapter(client, stream_event_sink=audits.append)
    ignored_events = list(ignoring.stream_events(route, [], []))
    assert aggregate_provider_stream(ignored_events).content is None
    assert ignored_events[0].event_type is ProviderStreamEventType.IGNORED
    assert audits[0]["provider_event_type"] == "chat.completion.future"

    failing = ChatCompletionsAdapter(client, unknown_event_policy="error")
    with pytest.raises(ProviderStreamProtocolError, match="未知 Chat Completions"):
        aggregate_provider_stream(failing.stream_events(route, [], []))


def test_stream_ending_without_terminal_emits_error_after_delta():
    class FakeCompletions:
        def create(self, **_request):
            return iter(
                [
                    {
                        "id": "chat-interrupted",
                        "object": "chat.completion.chunk",
                        "model": "stream-model",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "content": "partial",
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call-partial",
                                            "type": "function",
                                            "function": {
                                                "name": "list_exams",
                                                "arguments": '{"course_id":',
                                            },
                                        }
                                    ],
                                },
                                "finish_reason": None,
                            }
                        ],
                    }
                ]
            )

    route = ProviderGateway().begin_turn(
        ProviderSpec(model="stream-model", endpoint="https://provider.example/v1")
    )
    adapter = ChatCompletionsAdapter(
        SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    )
    events = list(adapter.stream_events(route, [], []))

    assert events[0].event_type is ProviderStreamEventType.TEXT_DELTA
    assert any(
        event.event_type is ProviderStreamEventType.TOOL_CALL_ARGUMENTS_DELTA
        and event.delta == '{"course_id":'
        for event in events
    )
    assert events[-1].event_type is ProviderStreamEventType.ERROR
    assert events[-1].retryable is False
    aggregator = ProviderStreamAggregator()
    for event in events[:-1]:
        assert aggregator.feed(event) is None
    assert aggregator.response is None
    with pytest.raises(ProviderStreamProtocolError, match="terminal"):
        aggregate_provider_stream(events)


def test_provider_error_event_redacts_route_credential_and_pii(monkeypatch):
    credential = "opaque-route-credential-value"
    monkeypatch.setenv("EDU_AGENT_API_KEY", credential)
    route = ProviderGateway().begin_turn(
        ProviderSpec(model="stream-model", endpoint="https://provider.example/v1")
    )
    from edu_agent.engine.streaming import provider_stream_error_event

    event = provider_stream_error_event(
        route=route,
        attempt=1,
        error=RuntimeError(
            f"credential={credential}; teacher@example.com; 13800138000"
        ),
    )

    assert credential not in event.error_message
    assert "teacher@example.com" not in event.error_message
    assert "13800138000" not in event.error_message
    assert event.error_message.count("[REDACTED]") == 3
