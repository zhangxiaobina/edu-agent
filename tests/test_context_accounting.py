from __future__ import annotations

import hashlib
import json

import pytest

from edu_agent.agent.prompts import SYSTEM_PROMPT
from edu_agent.api import DemoTokenAuth, EduAgentApi, Principal
from edu_agent.engine import (
    ApiMode,
    ChatCompletionsAdapter,
    ProviderCapabilities,
    ProviderCapabilityError,
    ProviderGateway,
    ProviderRequestRequirements,
    ProviderSpec,
    ResponsesAdapter,
    capability_gaps,
)
from edu_agent.engine.base import Engine, EngineResponse
from edu_agent.engine.mock import MockEngine
from edu_agent.planning import ModelPlanGenerator
from edu_agent.planning.planner import PLANNER_SYSTEM_PROMPT
from edu_agent.runtime.config import (
    AppConfig,
    MemoryConfig,
    ModelConfig,
    PlanningConfig,
    RuntimeConfig,
    StorageConfig,
)
from edu_agent.runtime.context import (
    ContextAccountant,
    ContextAccountingSession,
    ContextBudgetExceeded,
    ContextManager,
    ContextRouteLimits,
    CurrentUserInputTooLarge,
    OutputReserveExceeded,
)
from edu_agent.runtime.context_engine import CheckpointContextEngine
from edu_agent.runtime.models import RunContext
from edu_agent.runtime.security import redact_sensitive
from edu_agent.service import EduAgentService
from edu_agent.tokenization import (
    CONSERVATIVE_ESTIMATOR_NAME,
    CONSERVATIVE_ESTIMATOR_VERSION,
    TokenizerRegistry,
)


def _tool(name: str = "lookup", *, description: str = "Look up records") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    }


def _route(
    model: str = "fixture-model",
    *,
    provider: str = "fixture",
    context_window_tokens: int = 4_096,
    max_output_tokens: int = 512,
    tokenizer: str | None = None,
) -> ContextRouteLimits:
    return ContextRouteLimits(
        provider=provider,
        model=model,
        context_window_tokens=context_window_tokens,
        max_output_tokens=max_output_tokens,
        tokenizer=tokenizer,
    )


def _measure(
    accountant: ContextAccountant,
    *,
    messages: list[dict],
    tools: list[dict] | None = None,
    route: ContextRouteLimits | None = None,
    user: str = "current",
    reserve: int = 128,
    limit: int = 4_096,
    **kwargs,
):
    return accountant.breakdown(
        messages=messages,
        tools=tools or [],
        route=route or _route(),
        configured_context_limit_tokens=limit,
        max_output_reserve_tokens=reserve,
        current_user_turn=user,
        **kwargs,
    )


@pytest.mark.parametrize("text", ["a" * 120, "上下文核算" * 24])
def test_unknown_tokenizer_uses_versioned_conservative_estimator_for_english_and_chinese(
    text,
):
    breakdown = _measure(
        ContextAccountant(),
        messages=[
            {"role": "system", "content": "stable"},
            {"role": "user", "content": text},
        ],
        user=text,
    )

    assert breakdown.estimator_method == "versioned_estimator"
    assert breakdown.estimator_name == CONSERVATIVE_ESTIMATOR_NAME
    assert breakdown.estimator_version == CONSERVATIVE_ESTIMATOR_VERSION
    assert breakdown.tokenizer_fallback_reason == "tokenizer_unknown"
    assert breakdown.current_user_turn_tokens > len(text) // 4


def test_registered_model_tokenizer_counts_every_request_component_without_prompt_content():
    registry = TokenizerRegistry()
    registry.register(
        "fixture-vocab",
        lambda text: len(text.encode("utf-8")),
        version="fixture-v1",
    )
    accountant = ContextAccountant(
        tokenizer_registry=registry,
        estimator_safety_factor=1,
        tokenizer_safety_factor=1,
    )
    system = "stable-system"
    user = "请继续分析"
    injection = "<context_checkpoint>已归档事实</context_checkpoint>"
    current_wire = f"{injection}\n\n{user}"
    plan_message = {"role": "developer", "content": "plan step: collect evidence"}
    base_tool = _tool(description="Look up records")
    scoped_tool = _tool(description="Plan step s1: collect evidence. Look up records")
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "previous question"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-a",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"query":"a"}'},
                },
                {
                    "id": "call-b",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"query":"b"}'},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call-a", "name": "lookup", "content": "A"},
        {"role": "tool", "tool_call_id": "call-b", "name": "lookup", "content": "B"},
        plan_message,
        {"role": "user", "content": current_wire},
    ]

    breakdown = _measure(
        accountant,
        messages=messages,
        tools=[scoped_tool],
        route=_route(tokenizer="fixture-vocab"),
        user=user,
        current_user_wire_content=current_wire,
        memory_checkpoint_injection=injection,
        base_tool_schema=[base_tool],
        plan_evidence_messages=[plan_message],
        tool_manifest_hash="manifest-fixture",
    )

    categories = (
        breakdown.system_prompt_tokens,
        breakdown.tool_schema_tokens,
        breakdown.history_message_tokens,
        breakdown.current_user_turn_tokens,
        breakdown.plan_evidence_tokens,
        breakdown.tool_result_tokens,
        breakdown.memory_checkpoint_tokens,
        breakdown.protocol_overhead_tokens,
    )
    assert all(value > 0 for value in categories[:-1])
    assert sum(categories) == breakdown.estimated_input_tokens
    assert breakdown.total_reserved_tokens == breakdown.estimated_input_tokens + 128
    assert breakdown.estimator_method == "model_tokenizer"
    assert breakdown.estimator_version == "fixture-v1"
    assert breakdown.system_prompt_bytes == len(system.encode("utf-8"))
    assert breakdown.system_prompt_sha256 == hashlib.sha256(system.encode()).hexdigest()
    assert breakdown.tool_manifest_hash == "manifest-fixture"
    assert user not in json.dumps(breakdown.to_trace(), ensure_ascii=False)


def test_tool_schema_growth_is_visible_in_breakdown():
    accountant = ContextAccountant()
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "current"},
    ]
    small = _measure(accountant, messages=messages, tools=[_tool()])
    large = _measure(
        accountant,
        messages=messages,
        tools=[_tool(description="schema field " * 400)],
    )

    assert large.tool_schema_bytes > small.tool_schema_bytes
    assert large.tool_schema_tokens > small.tool_schema_tokens
    assert large.estimated_input_tokens > small.estimated_input_tokens
    assert large.tool_schema_sha256 != small.tool_schema_sha256


def test_multiple_tool_calls_and_results_are_kept_or_omitted_as_one_atomic_group():
    history = [
        {"role": "user", "content": "old question"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-a",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                },
                {
                    "id": "call-b",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-a",
            "name": "lookup",
            "content": "甲" * 800,
        },
        {
            "role": "tool",
            "tool_call_id": "call-b",
            "name": "lookup",
            "content": "乙" * 800,
        },
        {"role": "assistant", "content": "old answer"},
    ]

    small = ContextManager(token_budget=256, output_reserve_tokens=32).prepare(
        system_prompt="stable",
        history=history,
        user_message="continue",
    )
    large = ContextManager(token_budget=4_096, output_reserve_tokens=32).prepare(
        system_prompt="stable",
        history=history,
        user_message="continue",
    )

    assert not any(message.get("role") == "tool" for message in small.messages)
    calls = next(message for message in large.messages if message.get("tool_calls"))
    call_ids = {call["id"] for call in calls["tool_calls"]}
    result_ids = {
        message["tool_call_id"]
        for message in large.messages
        if message.get("role") == "tool"
    }
    assert call_ids == result_ids == {"call-a", "call-b"}
    assert large.breakdown.tool_result_tokens > 0
    ContextManager.validate_tool_pairs(large.messages)


def test_current_user_has_a_dedicated_error_but_system_and_tools_remain_uncuttable():
    manager = ContextManager(token_budget=256, output_reserve_tokens=64)
    with pytest.raises(CurrentUserInputTooLarge) as caught:
        manager.prepare(
            system_prompt="stable-system",
            history=[],
            user_message="超" * 400,
            tools=[_tool()],
        )
    breakdown = caught.value.breakdown
    assert breakdown.current_user_turn_tokens + 64 > 256
    assert breakdown.system_prompt_tokens > 0
    assert breakdown.tool_schema_tokens > 0

    with pytest.raises(ContextBudgetExceeded) as fixed_caught:
        manager.prepare(
            system_prompt="stable-system",
            history=[],
            user_message="short",
            tools=[_tool(description="schema field " * 2_000)],
        )
    assert not isinstance(fixed_caught.value, CurrentUserInputTooLarge)
    assert fixed_caught.value.breakdown.tool_schema_tokens > 0


def test_route_context_and_output_limits_drive_distinct_decisions():
    accountant = ContextAccountant()
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "ok"},
    ]
    tools = [_tool(description="schema field " * 400)]
    roomy = _measure(
        accountant,
        messages=messages,
        tools=tools,
        route=_route(context_window_tokens=4_096, max_output_tokens=1_024),
        user="ok",
    )
    narrow = _measure(
        accountant,
        messages=messages,
        tools=tools,
        route=_route(context_window_tokens=512, max_output_tokens=256),
        user="ok",
    )
    output_limited = _measure(
        accountant,
        messages=messages,
        route=_route(context_window_tokens=4_096, max_output_tokens=64),
        user="ok",
    )

    assert roomy.decision == "send"
    assert narrow.decision == "context_over_limit"
    assert narrow.effective_context_limit_tokens == 512
    assert output_limited.decision == "output_reserve_exceeds_provider"
    with pytest.raises(OutputReserveExceeded):
        ContextManager(token_budget=4_096, output_reserve_tokens=128).prepare(
            system_prompt="system",
            history=[],
            user_message="ok",
            route=_route(context_window_tokens=4_096, max_output_tokens=64),
        )

    fallback_gaps = capability_gaps(
        ProviderRequestRequirements(
            api_modes=frozenset({ApiMode.CHAT_COMPLETIONS}),
            tool_calling=False,
            structured_output=False,
            usage=False,
            streaming=False,
            context_tokens=128,
            max_output_tokens=64,
        ),
        ProviderCapabilities(context_window_tokens=512),
        api_mode=ApiMode.CHAT_COMPLETIONS,
        require_known_context=True,
        require_known_output=True,
    )
    assert "max_output_tokens_unknown" in fallback_gaps


def test_configured_budgets_cannot_exceed_declared_provider_capabilities():
    with pytest.raises(ValueError, match="已知 Provider"):
        ModelConfig(model="qwen-plus", context_window_tokens=131_073)
    with pytest.raises(ValueError, match="已知 Provider"):
        ModelConfig(model="qwen-plus", max_output_tokens=8_193)
    with pytest.raises(ValueError, match="context_token_budget"):
        AppConfig(
            model=ModelConfig(
                model="custom-model",
                context_window_tokens=512,
                max_output_tokens=128,
            ),
            runtime=RuntimeConfig(context_token_budget=1_024, output_token_reserve=64),
        )
    with pytest.raises(ValueError, match="output_token_reserve"):
        AppConfig(
            model=ModelConfig(
                model="custom-model",
                context_window_tokens=512,
                max_output_tokens=64,
            ),
            runtime=RuntimeConfig(context_token_budget=256, output_token_reserve=128),
        )
    with pytest.raises(ValueError, match="max_output_tokens"):
        ProviderCapabilities(context_window_tokens=128, max_output_tokens=256)


def test_output_reserve_is_sent_using_each_provider_wire_field():
    gateway = ProviderGateway()
    capabilities = ProviderCapabilities(
        streaming=True,
        context_window_tokens=4_096,
        max_output_tokens=512,
    )
    chat_route = gateway.begin_turn(
        ProviderSpec(
            model="fixture-chat",
            endpoint="https://provider.example/v1",
            api_mode=ApiMode.CHAT_COMPLETIONS,
            provider="fixture",
            capabilities=capabilities,
        )
    )
    responses_route = gateway.begin_turn(
        ProviderSpec(
            model="fixture-responses",
            endpoint="https://provider.example/v1",
            api_mode=ApiMode.RESPONSES,
            provider="fixture",
            capabilities=capabilities,
        )
    )
    messages = [{"role": "user", "content": "hello"}]

    chat_request = ChatCompletionsAdapter().build_request(
        chat_route,
        messages,
        [],
        max_output_tokens=256,
    )
    responses_request = ResponsesAdapter().build_request(
        responses_route,
        messages,
        [],
        max_output_tokens=256,
    )
    assert chat_request["max_tokens"] == 256
    assert responses_request["max_output_tokens"] == 256
    with pytest.raises(ProviderCapabilityError, match="max_output_tokens"):
        ChatCompletionsAdapter().build_request(
            chat_route,
            messages,
            [],
            max_output_tokens=1_024,
        )


def test_provider_actual_usage_settles_error_and_only_calibrates_upward():
    registry = TokenizerRegistry()
    registry.register("fixture-vocab", len, version="fixture-v1")
    accountant = ContextAccountant(
        tokenizer_registry=registry,
        estimator_safety_factor=1,
        tokenizer_safety_factor=1,
        calibration_margin=1.05,
    )
    route = ContextRouteLimits(
        provider="fixture",
        model="fixture-model",
        context_window_tokens=4_096,
        max_output_tokens=512,
        tokenizer="fixture-vocab",
        route_identity=(
            "fixture",
            "deployment-a",
            "chat_completions",
            "https://provider-a.example/v1",
            "fixture-model",
        ),
    )
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "current"},
    ]
    before = _measure(accountant, messages=messages, route=route)
    actual_input = before.base_estimated_input_tokens * 2
    settlement = accountant.settle(
        before,
        {
            "input_tokens": actual_input,
            "output_tokens": 20,
            "total_tokens": actual_input + 20,
        },
    )
    after = _measure(accountant, messages=messages, route=route)

    assert settlement.source == "provider_actual"
    assert settlement.actual_minus_estimate_tokens == actual_input - before.estimated_input_tokens
    assert settlement.absolute_percentage_error == pytest.approx(0.5)
    assert settlement.calibration_factor_after == pytest.approx(2.1)
    assert after.calibration_factor == settlement.calibration_factor_after
    assert after.estimated_input_tokens > before.estimated_input_tokens
    other_route = ContextRouteLimits(
        provider="fixture",
        model="fixture-model",
        context_window_tokens=4_096,
        max_output_tokens=512,
        tokenizer="fixture-vocab",
        route_identity=(
            "fixture",
            "deployment-b",
            "responses",
            "https://provider-b.example/v1",
            "fixture-model",
        ),
    )
    other = _measure(accountant, messages=messages, route=other_route)
    assert other.calibration_factor == 1
    assert other.route_identity_sha256 != after.route_identity_sha256
    estimated = accountant.settle(after, None)
    assert estimated.source == "estimated"
    assert estimated.actual_input_tokens is None


class _PlanningEngine(Engine):
    name = "planning-fixture"

    def __init__(self):
        self.max_output_tokens = None
        self.messages = []

    def chat(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        max_output_tokens: int | None = None,
    ) -> EngineResponse:
        self.max_output_tokens = max_output_tokens
        self.messages = messages
        return EngineResponse(
            content=json.dumps(
                {
                    "goal": "collect evidence",
                    "steps": [
                        {
                            "id": "s1",
                            "goal": "list exams",
                            "depends_on": [],
                            "allowed_tools": ["list_exams"],
                            "expected_tools": ["list_exams"],
                            "completion_conditions": [
                                {"kind": "tool_success", "tool": "list_exams"}
                            ],
                        }
                    ],
                }
            ),
            usage={
                "prompt_tokens": 400,
                "completion_tokens": 20,
                "total_tokens": 420,
                "fallback_used": True,
            },
            model="fallback-versioned-alias",
        )


def test_plan_injection_is_accounted_and_fallback_usage_settles_the_selected_route():
    accountant = ContextAccountant()
    session = ContextAccountingSession(
        accountant,
        routes=(
            _route("primary-model", provider="primary", context_window_tokens=20_000),
            _route("fallback-model", provider="fallback", context_window_tokens=20_000),
        ),
        configured_context_limit_tokens=20_000,
        max_output_reserve_tokens=256,
    )
    context = RunContext.create(
        session_id="plan-session",
        actor_id="teacher",
        role="teacher",
    )
    context.bind_context_accounting(session)
    engine = _PlanningEngine()

    plan = ModelPlanGenerator(engine).generate(
        "list the exams and cite evidence",
        context=context,
        available_tools={"list_exams"},
        max_steps=4,
    )
    records = session.records()
    request_records = [
        record for record in records if record["event"] == "context_request_accounted"
    ]
    settlement = next(
        record["settlement"]
        for record in records
        if record["event"] == "context_usage_settled"
    )

    assert plan.steps[0].id == "s1"
    assert len(request_records) == 2
    assert all(record["breakdown"]["plan_evidence_tokens"] > 0 for record in request_records)
    assert settlement["source"] == "provider_actual"
    assert settlement["provider"] == "fallback"
    assert engine.max_output_tokens == 256
    assert engine.messages[0]["content"].startswith(f"{PLANNER_SYSTEM_PROMPT}\n\n")
    assert [message["role"] for message in engine.messages] == ["system", "user"]


def test_context_trace_contains_only_accounting_metadata_and_provider_error(tmp_path):
    secret_prompt = "student-private-context-8f9142"
    engine = MockEngine(
        lambda messages, tools, step: EngineResponse(
            content="done",
            usage={"prompt_tokens": 777, "completion_tokens": 12, "total_tokens": 789},
            model="mock",
        )
    )
    service = EduAgentService(
        engine,
        config=AppConfig(
            runtime=RuntimeConfig(
                context_token_budget=50_000,
                output_token_reserve=256,
                compression_enabled=False,
            ),
            planning=PlanningConfig(enabled=False),
            memory=MemoryConfig(enabled=False),
            storage=StorageConfig(state_path=str(tmp_path / "state.db")),
        ),
    )
    try:
        result = service.chat(
            secret_prompt,
            actor_id="teacher",
            tenant_id="school",
            role="teacher",
        )
        with service.state_store.connect() as connection:
            rows = connection.execute(
                """
                SELECT event, details_json FROM provider_events
                WHERE run_id=? AND event LIKE 'context_%'
                ORDER BY id
                """,
                (result.run_id,),
            ).fetchall()
    finally:
        service.close()

    details = [json.loads(row["details_json"]) for row in rows]
    rendered = json.dumps(details, ensure_ascii=False)
    events = [row["event"] for row in rows]
    assert "context_prepared" in events
    assert "context_request_accounted" in events
    assert "context_usage_settled" in events
    assert secret_prompt not in rendered
    assert SYSTEM_PROMPT not in rendered
    assert any(item.get("breakdown", {}).get("decision") == "send" for item in details)
    assert "absolute_percentage_error" in rendered
    assert result.context["breakdown"]["tool_schema_tokens"] > 0
    assert result.context["breakdown"]["system_prompt_bytes"] == len(
        SYSTEM_PROMPT.encode("utf-8")
    )


def test_context_breakdown_survives_central_redaction_without_prompt_content():
    prompt = "private-current-turn-51cc72"
    breakdown = _measure(
        ContextAccountant(),
        messages=[
            {"role": "system", "content": "stable"},
            {"role": "user", "content": prompt},
        ],
        route=_route(tokenizer="unknown-tokenizer-v1"),
        user=prompt,
    )

    redacted = redact_sensitive(breakdown.to_trace())
    rendered = json.dumps(redacted, ensure_ascii=False)

    assert redacted["estimated_input_tokens"] == breakdown.estimated_input_tokens
    assert redacted["max_output_reserve_tokens"] == 128
    assert redacted["requested_tokenizer"] == "unknown-tokenizer-v1"
    assert redacted["tokenizer_fallback_reason"] == "tokenizer_unavailable"
    assert prompt not in rendered


def test_api_returns_dedicated_current_user_input_error(tmp_path):
    service = EduAgentService(
        MockEngine(lambda messages, tools, step: pytest.fail("oversized input must not be sent")),
        config=AppConfig(
            runtime=RuntimeConfig(
                context_token_budget=256,
                output_token_reserve=64,
                compression_enabled=False,
            ),
            planning=PlanningConfig(enabled=False),
            memory=MemoryConfig(enabled=False),
            storage=StorageConfig(state_path=str(tmp_path / "state.db")),
        ),
    )
    api = EduAgentApi(
        service,
        authenticator=DemoTokenAuth(
            {"teacher-token": Principal("teacher", "school", "teacher")}
        ),
    )
    try:
        response = api.dispatch(
            "POST",
            "/v1/chat",
            headers={
                "Authorization": "Bearer teacher-token",
                "X-Request-ID": "oversized-request",
            },
            body=json.dumps({"message": "超" * 500}).encode(),
        )
    finally:
        api.close()
        service.close()

    assert response.status == 413
    assert response.body["error"]["code"] == "CURRENT_USER_INPUT_TOO_LARGE"


def test_api_sse_returns_dedicated_current_user_input_error(tmp_path):
    service = EduAgentService(
        MockEngine(lambda messages, tools, step: pytest.fail("oversized input must not be sent")),
        config=AppConfig(
            runtime=RuntimeConfig(
                context_token_budget=256,
                output_token_reserve=64,
                compression_enabled=False,
            ),
            planning=PlanningConfig(enabled=False),
            memory=MemoryConfig(enabled=False),
            storage=StorageConfig(state_path=str(tmp_path / "state.db")),
        ),
    )
    api = EduAgentApi(
        service,
        authenticator=DemoTokenAuth(
            {"teacher-token": Principal("teacher", "school", "teacher")}
        ),
    )
    try:
        response = api.dispatch(
            "POST",
            "/v1/chat",
            headers={
                "Authorization": "Bearer teacher-token",
                "X-Request-ID": "oversized-stream-request",
            },
            body=json.dumps({"message": "超" * 500, "stream": True}).encode(),
        )
        frames = b"".join(response.body).split(b"\n\n")
    finally:
        api.close()
        service.close()

    events = []
    for frame in frames:
        fields = {}
        for line in frame.splitlines():
            if b":" in line and not line.startswith(b":"):
                key, value = line.split(b":", 1)
                fields[key.decode()] = value.lstrip().decode()
        if "event" in fields:
            fields["data"] = json.loads(fields["data"])
            events.append(fields)

    assert response.status == 200
    assert events[0]["event"] == "accepted"
    assert events[-1]["event"] == "error"
    assert events[-1]["data"]["payload"]["code"] == "CURRENT_USER_INPUT_TOO_LARGE"


def test_r41_keeps_the_preexisting_compaction_trigger_estimate():
    history = [
        {"role": "user", "content": "abc" * 20},
        {"role": "assistant", "content": "def" * 20},
    ]
    engine = CheckpointContextEngine(
        object(),
        token_budget=10_000,
        trigger_ratio=0.7,
        keep_recent=12,
    )
    result = engine.compact_if_needed("session", history)
    expected = sum(
        max(1, len(json.dumps(message, ensure_ascii=False, default=str)) // 4)
        for message in history
    )

    assert result.compacted_messages == 0
    assert result.estimated_tokens_before == expected
