from __future__ import annotations

import json
from pathlib import Path

import httpx
import openai

from edu_agent.engine import (
    ApiMode,
    ChatCompletionsAdapter,
    GatewayEngine,
    ProviderGateway,
    ProviderSpec,
    ResponsesAdapter,
)


FIXTURE = Path(__file__).parent / "fixtures" / "provider_tool_call_equivalence.json"


def _calls(response) -> list[tuple[str, str, dict | str]]:
    return [(call.id, call.name, call.arguments) for call in response.tool_calls]


def test_same_semantic_fixture_normalizes_to_equivalent_internal_tool_calls():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    clients = {}
    engines = {}
    for mode, adapter_type in (
        (ApiMode.CHAT_COMPLETIONS, ChatCompletionsAdapter),
        (ApiMode.RESPONSES, ResponsesAdapter),
    ):
        client = openai.OpenAI(
            api_key="contract-key",
            base_url="https://provider.example/v1",
            max_retries=0,
            http_client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda request, mode=mode: httpx.Response(
                        200,
                        json=fixture[mode.value],
                    )
                )
            ),
        )
        clients[mode] = client
        engines[mode] = GatewayEngine(
            ProviderGateway(adapters={mode: adapter_type(client)}),
            ProviderSpec(
                model="equivalent-model",
                endpoint="https://provider.example/v1",
                api_mode=mode,
            ),
        )

    messages = [{"role": "user", "content": "Check both"}]
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
        chat = engines[ApiMode.CHAT_COMPLETIONS].chat(messages, tools)
        responses = engines[ApiMode.RESPONSES].chat(messages, tools)
    finally:
        for client in clients.values():
            client.close()

    assert _calls(chat) == _calls(responses)
    assert chat.content == responses.content == "Checking both."
    assert chat.finish_reason == responses.finish_reason == "tool_calls"
    assert chat.model == responses.model == "equivalent-model"
