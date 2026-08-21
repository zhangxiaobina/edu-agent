from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest

from edu_agent.engine import (
    ApiMode,
    CredentialRef,
    GatewayEngine,
    ProviderCapabilities,
    ProviderGateway,
    ProviderSpec,
    ResponsesAdapter,
    ResponsesAPIError,
    get_engine,
)
from edu_agent.observability.trace import TraceRepository
from edu_agent.runtime.config import AppConfig, PlanningConfig, StorageConfig
from edu_agent.service import EduAgentService


FIXTURES = Path(__file__).parent / "fixtures" / "responses"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _spec(**overrides) -> ProviderSpec:
    values = {
        "model": "route-model",
        "endpoint": "https://provider.example/v1",
        "api_mode": ApiMode.RESPONSES,
    }
    values.update(overrides)
    return ProviderSpec(**values)


def _sdk_client(handler, *, timeout: float = 9.5):
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return openai.OpenAI(
        api_key="wire-test-key",
        base_url="https://provider.example/v1",
        timeout=timeout,
        max_retries=0,
        http_client=http_client,
    )


def _tool(name: str = "list_exams", *, strict=None) -> dict:
    function = {
        "name": name,
        "description": f"Call {name}",
        "parameters": {"type": "object", "properties": {}},
    }
    if strict is not None:
        function["strict"] = strict
    return {"type": "function", "function": function}


def test_gateway_responses_preserves_real_sdk_wire_and_normalizes_response():
    requests: list[dict] = []

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
        return httpx.Response(200, json=_fixture("single_function_call.json"))

    messages = [
        {"role": "system", "content": "System"},
        {"role": "user", "content": "First"},
        {
            "role": "assistant",
            "content": "Working.",
            "tool_calls": [
                {
                    "id": "history-a",
                    "type": "function",
                    "function": {"name": "list_exams", "arguments": "{}"},
                },
                {
                    "id": "history-b",
                    "type": "function",
                    "function": {
                        "name": "query_student_scores",
                        "arguments": {"exam_id": 17},
                    },
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "history-a",
            "name": "list_exams",
            "content": "{\"ok\":true}",
        },
        {
            "role": "tool",
            "tool_call_id": "history-b",
            "name": "query_student_scores",
            "content": "{\"rows\":[]}",
        },
        {"role": "user", "content": "Next"},
    ]
    tools = [_tool()]
    client = _sdk_client(handler)
    engine = GatewayEngine(
        ProviderGateway(
            adapters={ApiMode.RESPONSES: ResponsesAdapter(client, temperature=0.25)}
        ),
        _spec(),
        name="responses-wire",
    )
    try:
        response = engine.chat(messages, tools)
    finally:
        client.close()

    assert requests[0]["method"] == "POST"
    assert requests[0]["url"] == "https://provider.example/v1/responses"
    assert requests[0]["headers"]["authorization"] == "Bearer wire-test-key"
    assert set(requests[0]["timeout"].values()) == {9.5}
    assert requests[0]["body"] == {
        "model": "route-model",
        "input": [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "First"},
            {"role": "assistant", "content": "Working."},
            {
                "type": "function_call",
                "call_id": "history-a",
                "name": "list_exams",
                "arguments": "{}",
            },
            {
                "type": "function_call",
                "call_id": "history-b",
                "name": "query_student_scores",
                "arguments": "{\"exam_id\": 17}",
            },
            {
                "type": "function_call_output",
                "call_id": "history-a",
                "output": "{\"ok\":true}",
            },
            {
                "type": "function_call_output",
                "call_id": "history-b",
                "output": "{\"rows\":[]}",
            },
            {"role": "user", "content": "Next"},
        ],
        "temperature": 0.25,
        "tools": [
            {
                "type": "function",
                "name": "list_exams",
                "description": "Call list_exams",
                "parameters": {"type": "object", "properties": {}},
                "strict": None,
            }
        ],
        "tool_choice": "auto",
    }
    assert response.content == "Checking."
    assert [(call.id, call.name, call.arguments) for call in response.tool_calls] == [
        ("call-single", "list_exams", "{}")
    ]
    assert response.usage == {
        "prompt_tokens": 11,
        "prompt_tokens_details": {"cached_tokens": 2},
        "completion_tokens": 7,
        "completion_tokens_details": {"reasoning_tokens": 1},
        "total_tokens": 18,
    }
    assert response.finish_reason == "tool_calls"
    assert response.model == "response-single-model"


@pytest.mark.parametrize(
    ("fixture_name", "content", "calls", "usage", "finish_reason"),
    [
        (
            "multi_interleaved_missing_usage.json",
            "Before. Between. After.",
            [
                ("call-one", "list_exams", "{}"),
                ("call-two", "query_student_scores", '{"exam_id":17}'),
            ],
            {},
            "tool_calls",
        ),
        ("incomplete.json", "Partial", [], {"total_tokens": 8}, "length"),
        ("unknown_output_item.json", "Known text", [], {}, "stop"),
        (
            "bad_arguments.json",
            "",
            [("call-bad", "list_exams", "{not-json")],
            {},
            "tool_calls",
        ),
    ],
)
def test_responses_fixtures_cover_output_boundaries(
    fixture_name,
    content,
    calls,
    usage,
    finish_reason,
):
    client = _sdk_client(
        lambda request: httpx.Response(200, json=_fixture(fixture_name))
    )
    engine = GatewayEngine(
        ProviderGateway(adapters={ApiMode.RESPONSES: ResponsesAdapter(client)}),
        _spec(),
    )
    try:
        response = engine.chat([{"role": "user", "content": "hello"}], [])
    finally:
        client.close()

    assert response.content == content
    assert [(call.id, call.name, call.arguments) for call in response.tool_calls] == calls
    for key, value in usage.items():
        assert response.usage[key] == value
    if not usage:
        assert response.usage == {}
    assert response.finish_reason == finish_reason


def test_responses_failed_status_is_a_local_terminal_error():
    client = _sdk_client(
        lambda request: httpx.Response(200, json=_fixture("error.json"))
    )
    engine = GatewayEngine(
        ProviderGateway(adapters={ApiMode.RESPONSES: ResponsesAdapter(client)}),
        _spec(),
    )
    try:
        with pytest.raises(ResponsesAPIError, match="failed.*server_error") as caught:
            engine.chat([{"role": "user", "content": "hello"}], [])
    finally:
        client.close()

    assert caught.value.response_status == "failed"
    assert caught.value.code == "server_error"
    assert "fixture generation failed" not in str(caught.value)


def test_responses_capabilities_and_unsupported_combinations_fail_before_client():
    calls = 0

    class FakeResponses:
        def create(self, **request):
            nonlocal calls
            calls += 1
            raise AssertionError("client must not be called")

    adapter = ResponsesAdapter(SimpleNamespace(responses=FakeResponses()))
    assert adapter.capabilities == ProviderCapabilities(
        tool_calling=True,
        structured_output=False,
        usage=True,
        streaming=False,
        context_window_tokens=None,
        max_output_tokens=None,
    )

    no_tools = _spec(capabilities=ProviderCapabilities(tool_calling=False))
    tiny_context = _spec(
        capabilities=ProviderCapabilities(context_window_tokens=1)
    )
    engine_no_tools = GatewayEngine(
        ProviderGateway(adapters={ApiMode.RESPONSES: adapter}),
        no_tools,
    )
    engine_tiny_context = GatewayEngine(
        ProviderGateway(adapters={ApiMode.RESPONSES: adapter}),
        tiny_context,
    )

    with pytest.raises(ValueError, match="不支持 tool calling"):
        engine_no_tools.chat([{"role": "user", "content": "hello"}], [_tool()])
    with pytest.raises(ValueError, match="context window"):
        engine_tiny_context.chat([{"role": "user", "content": "long input"}], [])
    with pytest.raises(ValueError, match="仅支持 text"):
        adapter.chat(
            ProviderGateway().begin_turn(_spec()),
            [{"role": "user", "content": [{"type": "text", "text": "no"}]}],
            [],
        )
    with pytest.raises(ValueError, match="仅支持 function tools"):
        adapter.chat(
            ProviderGateway().begin_turn(_spec()),
            [{"role": "user", "content": "hello"}],
            [{"type": "web_search"}],
        )
    with pytest.raises(ValueError, match="structured output"):
        adapter.chat(
            ProviderGateway().begin_turn(_spec()),
            [{"role": "user", "content": "hello"}],
            [_tool(strict=True)],
        )
    assert calls == 0


def test_get_engine_selects_responses_adapter_without_changing_sync_surface(monkeypatch):
    captured: list[dict] = []

    class FakeResponses:
        def create(self, **request):
            captured.append(request)
            return _fixture("unknown_output_item.json")

    fake_client = SimpleNamespace(responses=FakeResponses())
    monkeypatch.setenv("EDU_AGENT_ENGINE", "openai")
    monkeypatch.setenv("EDU_AGENT_API_MODE", "responses")
    monkeypatch.setenv("EDU_AGENT_BASE_URL", "https://api.openai.com/v1")

    engine = get_engine(model="gpt-fixture", client=fake_client)
    response = engine.chat([{"role": "user", "content": "hello"}], [])

    assert engine.begin_turn_routes()[0].api_mode is ApiMode.RESPONSES
    assert response.content == "Known text"
    assert captured[0]["model"] == "gpt-fixture"


def test_responses_input_secret_never_enters_route_or_trace(tmp_path, monkeypatch):
    canary = "sk-responses-input-canary-4821"
    credential_env = "R1_RESPONSES_CREDENTIAL"
    provider_requests: list[dict] = []
    monkeypatch.setenv(credential_env, canary)

    class FakeResponses:
        def create(self, **request):
            provider_requests.append(request)
            return _fixture("unknown_output_item.json")

    route_spec = _spec(credential=CredentialRef(credential_env))
    engine = GatewayEngine(
        ProviderGateway(
            adapters={
                ApiMode.RESPONSES: ResponsesAdapter(
                    SimpleNamespace(responses=FakeResponses())
                )
            }
        ),
        route_spec,
    )
    service = EduAgentService(
        engine,
        config=AppConfig(
            planning=PlanningConfig(enabled=False),
            storage=StorageConfig(state_path=str(tmp_path / "state.db")),
        ),
    )

    result = service.chat(f"api_key={canary}", actor_id="teacher-1")
    route = engine.begin_turn_routes()[0]
    trace = TraceRepository(service.state_store).list_events(
        actor_id="teacher-1",
        tenant_id="default",
        run_id=result.run_id,
        limit=100,
    )
    with service.state_store.connect() as connection:
        provider_event = connection.execute(
            "SELECT details_json FROM provider_events WHERE run_id=?",
            (result.run_id,),
        ).fetchone()
        stored_messages = connection.execute(
            "SELECT content FROM messages WHERE run_id=? ORDER BY sequence",
            (result.run_id,),
        ).fetchall()

    assert canary in json.dumps(provider_requests, ensure_ascii=False)
    audited = json.dumps(
        {
            "route": route.to_event(),
            "trace": trace.to_dict(),
            "provider_event": provider_event["details_json"],
            "messages": [row["content"] for row in stored_messages],
        },
        ensure_ascii=False,
        default=str,
    )
    assert canary not in audited
    assert credential_env not in audited
