from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime
from types import SimpleNamespace

import pytest

from edu_agent.engine import (
    ApiMode,
    CircuitOpenError,
    CredentialRef,
    Engine,
    EngineResponse,
    FailureKind,
    GatewayEngine,
    ProviderGateway,
    ProviderCapabilities,
    ResponsesAPIError,
    ProviderSpec,
    ResilientEngine,
    RouteStateCapacityError,
    RouteStateRegistry,
    classify_failure,
    get_engine,
    parse_retry_after,
)
from edu_agent.runtime.config import ModelConfig
from edu_agent.runtime.security import redact_sensitive
from edu_agent.state import StateStore


class APIConnectionError(Exception):
    pass


class APITimeoutError(Exception):
    pass


class LengthFinishReasonError(Exception):
    pass


class HTTPError(Exception):
    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        headers: dict[str, str] | None = None,
        code: str | None = None,
        body: dict | None = None,
    ):
        self.status_code = status_code
        self.code = code
        self.body = body
        self.response = SimpleNamespace(status_code=status_code, headers=headers or {})
        super().__init__(message)


@dataclass
class FakeTime:
    monotonic_value: float = 0.0
    epoch_value: float = 1_787_347_200.0

    def __post_init__(self):
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.monotonic_value

    def epoch(self) -> float:
        return self.epoch_value

    def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.advance(delay)

    def advance(self, delay: float) -> None:
        self.monotonic_value += delay
        self.epoch_value += delay


class RoutedEngine(Engine):
    def __init__(
        self,
        behavior,
        *,
        endpoint: str = "https://gateway.example/v1",
        model: str = "model-a",
        provider: str = "custom",
        deployment: str | None = "prod-a",
        credential_env: str = "EDU_AGENT_API_KEY",
        api_mode: ApiMode = ApiMode.CHAT_COMPLETIONS,
        capabilities: ProviderCapabilities | None = ProviderCapabilities(
            context_window_tokens=16_384
        ),
    ):
        self.route = ProviderGateway().begin_turn(
            ProviderSpec(
                model=model,
                endpoint=endpoint,
                api_mode=api_mode,
                provider=provider,
                deployment=deployment,
                credential=CredentialRef(credential_env),
                capabilities=capabilities,
            )
        )
        self.name = f"{provider}:{model}"
        self.behavior = behavior
        self.calls = 0

    def begin_turn_routes(self):
        return (self.route,)

    def chat(self, messages, tools):
        self.calls += 1
        return self.behavior(self.calls, messages, tools)


def _raise(error: Exception):
    raise error


def _fail_once(error: Exception, *, usage: dict | None = None):
    def behavior(call, _messages, _tools):
        if call == 1:
            raise error
        return EngineResponse(content="ok", usage=usage or {"total_tokens": 3})

    return behavior


@pytest.mark.parametrize(
    ("error", "kind", "retryable"),
    [
        (APIConnectionError("offline"), FailureKind.CONNECTION, True),
        (APITimeoutError("timed out"), FailureKind.TIMEOUT, True),
        (HTTPError(429, "limited"), FailureKind.RATE_LIMIT, True),
        (
            HTTPError(429, "quota", code="insufficient_quota"),
            FailureKind.RATE_LIMIT,
            False,
        ),
        (HTTPError(503, "unavailable"), FailureKind.SERVER, True),
        (HTTPError(401, "bad key"), FailureKind.AUTHENTICATION, False),
        (HTTPError(403, "forbidden"), FailureKind.PERMISSION, False),
        (HTTPError(400, "bad request"), FailureKind.INVALID_REQUEST, False),
        (
            HTTPError(
                400,
                "request rejected",
                body={"error": {"code": "context_length_exceeded"}},
            ),
            FailureKind.CONTEXT_OVERFLOW,
            False,
        ),
        (
            ValueError("Responses 输入在发请求前已超过 route context window (2/1)"),
            FailureKind.CONTEXT_OVERFLOW,
            False,
        ),
        (LengthFinishReasonError("truncated"), FailureKind.OUTPUT_CAP, False),
        (ResponsesAPIError("failed", code="server_error"), FailureKind.SERVER, True),
        (
            ResponsesAPIError("failed", code="context_length_exceeded"),
            FailureKind.CONTEXT_OVERFLOW,
            False,
        ),
        (
            ResponsesAPIError("failed", code="max_output_tokens"),
            FailureKind.OUTPUT_CAP,
            False,
        ),
        (RuntimeError("opaque provider failure"), FailureKind.UNKNOWN, False),
    ],
)
def test_failure_classification_is_explicit(error, kind, retryable):
    decision = classify_failure(error)
    assert decision.kind is kind
    assert decision.retryable is retryable


def test_retry_after_seconds_override_jitter_and_are_capped():
    fake_time = FakeTime()
    events = []
    engine = RoutedEngine(
        _fail_once(HTTPError(429, "limited", headers={"Retry-After": "120"}))
    )

    response = ResilientEngine(
        engine,
        max_retries=1,
        failure_threshold=3,
        sleeper=fake_time.sleep,
        clock=fake_time.monotonic,
        wall_clock=fake_time.epoch,
        random_source=lambda: pytest.fail("jitter must not run for Retry-After"),
        retry_after_max_seconds=15,
        event_sink=events.append,
    ).chat([{"role": "user", "content": "secret body"}], [])

    assert response.content == "ok"
    assert fake_time.sleeps == [15]
    attempts = [event for event in events if event["event"] == "provider_attempt"]
    assert len(attempts) == 2
    assert attempts[0]["details"]["failure_kind"] == "rate_limit"
    assert attempts[0]["details"]["delay_seconds"] == 15
    assert attempts[0]["details"]["delay_source"] == "retry_after"
    assert attempts[1]["details"]["usage"] == {"total_tokens": 3}


def test_retry_after_http_date_uses_injected_wall_clock():
    fake_time = FakeTime(epoch_value=1_787_347_200.0)
    retry_at = format_datetime(
        datetime.fromtimestamp(fake_time.epoch_value + 23, tz=UTC),
        usegmt=True,
    )
    engine = RoutedEngine(
        _fail_once(HTTPError(429, "limited", headers={"retry-after": retry_at}))
    )

    ResilientEngine(
        engine,
        max_retries=1,
        sleeper=fake_time.sleep,
        clock=fake_time.monotonic,
        wall_clock=fake_time.epoch,
        random_source=lambda: pytest.fail("HTTP-date must override jitter"),
        retry_after_max_seconds=60,
    ).chat([], [])

    assert fake_time.sleeps == [23]
    assert parse_retry_after(retry_at, now=fake_time.epoch_value - 23, max_delay_seconds=10) == 10


def test_invalid_retry_after_falls_back_to_deterministic_full_jitter():
    fake_time = FakeTime()
    events = []
    engine = RoutedEngine(
        _fail_once(HTTPError(503, "unavailable", headers={"Retry-After": "later"}))
    )

    ResilientEngine(
        engine,
        max_retries=1,
        sleeper=fake_time.sleep,
        clock=fake_time.monotonic,
        wall_clock=fake_time.epoch,
        random_source=lambda: 0.25,
        retry_base_delay_seconds=2,
        retry_max_delay_seconds=8,
        event_sink=events.append,
    ).chat([], [])

    assert fake_time.sleeps == [0.5]
    first = next(event for event in events if event["event"] == "provider_attempt")
    assert first["details"]["delay_source"] == "exponential_full_jitter"


def test_invalid_injected_random_still_audits_completed_provider_attempt():
    events = []
    routed = RoutedEngine(lambda *_: _raise(APIConnectionError("offline")))
    resilient = ResilientEngine(
        routed,
        max_retries=1,
        failure_threshold=3,
        random_source=lambda: 2.0,
        event_sink=events.append,
    )

    with pytest.raises(ValueError, match="random_source"):
        resilient.chat([], [])

    attempts = [event for event in events if event["event"] == "provider_attempt"]
    assert len(attempts) == 1
    assert attempts[0]["details"]["failure_kind"] == "connection"
    assert attempts[0]["details"]["delay_seconds"] == 0


@pytest.mark.parametrize(
    "error",
    [
        HTTPError(401, "api_key=sk-auth-canary-12345678"),
        HTTPError(400, "invalid body: private full text"),
        HTTPError(400, "maximum context window length exceeded"),
    ],
)
def test_terminal_request_failures_never_retry_or_sleep(error):
    sleeps = []
    engine = RoutedEngine(lambda *_: _raise(error))
    resilient = ResilientEngine(
        engine,
        max_retries=4,
        sleeper=sleeps.append,
        random_source=lambda: pytest.fail("terminal failures must not jitter"),
    )

    with pytest.raises(type(error)):
        resilient.chat([], [])

    assert engine.calls == 1
    assert sleeps == []
    assert resilient.breaker.state == "closed"


@pytest.mark.parametrize(
    "error",
    [
        HTTPError(401, "bad key"),
        HTTPError(403, "forbidden"),
        HTTPError(400, "invalid request"),
        HTTPError(400, "context window exceeded"),
        LengthFinishReasonError("output capped"),
        HTTPError(429, "quota", code="insufficient_quota"),
        RuntimeError("opaque failure"),
    ],
)
def test_fallback_policy_rejects_terminal_and_unknown_failure_kinds(error):
    events = []
    primary = RoutedEngine(lambda *_: _raise(error))
    fallback = RoutedEngine(
        lambda *_: EngineResponse(content="must-not-run"),
        model="fallback-model",
    )
    resilient = ResilientEngine(
        primary,
        fallback=fallback,
        max_retries=0,
        event_sink=events.append,
    )

    with pytest.raises(type(error)) as caught:
        resilient.chat([], [])

    assert caught.value is error
    assert fallback.calls == 0
    rejected = next(event for event in events if event["event"] == "fallback_rejected")
    assert rejected["details"]["reason"] == "failure_policy_denied"
    assert rejected["details"]["fallback_attempted"] is False


@pytest.mark.parametrize(
    "error",
    [
        APIConnectionError("offline"),
        APITimeoutError("timed out"),
        HTTPError(429, "limited"),
        HTTPError(503, "unavailable"),
    ],
)
def test_fallback_policy_allows_only_explicit_transient_failure_kinds(error):
    primary = RoutedEngine(lambda *_: _raise(error))
    fallback = RoutedEngine(
        lambda *_: EngineResponse(content="fallback-ok"),
        model="fallback-model",
    )

    response = ResilientEngine(
        primary,
        fallback=fallback,
        max_retries=0,
    ).chat([], [])

    assert response.content == "fallback-ok"
    assert fallback.calls == 1
    assert response.usage["primary_failure"] == classify_failure(error).kind.value


@pytest.mark.parametrize(
    ("fallback_mode", "fallback_capabilities", "tools", "expected_gap"),
    [
        (
            ApiMode.CHAT_COMPLETIONS,
            ProviderCapabilities(tool_calling=False),
            [{"type": "function", "function": {"name": "query"}}],
            "tool_calling",
        ),
        (
            ApiMode.CHAT_COMPLETIONS,
            ProviderCapabilities(structured_output=False),
            [
                {
                    "type": "function",
                    "function": {"name": "query", "strict": True},
                }
            ],
            "structured_output",
        ),
        (
            ApiMode.CHAT_COMPLETIONS,
            ProviderCapabilities(context_window_tokens=1),
            [],
            "context_window",
        ),
        (
            ApiMode.RESPONSES,
            ProviderCapabilities(),
            [],
            "api_mode_validation",
        ),
    ],
)
def test_incompatible_fallback_is_rejected_before_provider_call(
    fallback_mode,
    fallback_capabilities,
    tools,
    expected_gap,
):
    events = []
    primary = RoutedEngine(lambda *_: _raise(APIConnectionError("offline")))
    fallback = RoutedEngine(
        lambda *_: EngineResponse(content="must-not-run"),
        model="fallback-model",
        api_mode=fallback_mode,
        capabilities=fallback_capabilities,
    )
    resilient = ResilientEngine(
        primary,
        fallback=fallback,
        max_retries=0,
        event_sink=events.append,
    )

    with pytest.raises(APIConnectionError):
        resilient.chat([{"role": "user", "content": "request"}], tools)

    assert fallback.calls == 0
    rejected = next(event for event in events if event["event"] == "fallback_rejected")
    assert rejected["details"]["reason"] == "capability_mismatch"
    assert expected_gap in rejected["details"]["compatibility"]["gaps"]


def test_fallback_rejects_unknown_context_after_known_primary_limit():
    events = []
    primary = RoutedEngine(
        lambda *_: _raise(APIConnectionError("offline")),
        capabilities=ProviderCapabilities(context_window_tokens=4096),
    )
    fallback = RoutedEngine(
        lambda *_: EngineResponse(content="must-not-run"),
        model="fallback-model",
        capabilities=ProviderCapabilities(),
    )
    resilient = ResilientEngine(
        primary,
        fallback=fallback,
        max_retries=0,
        event_sink=events.append,
    )

    with pytest.raises(APIConnectionError):
        resilient.chat([{"role": "user", "content": "small"}], [])

    rejected = next(event for event in events if event["event"] == "fallback_rejected")
    compatibility = rejected["details"]["compatibility"]
    assert compatibility["context_check"] == "unknown_unverified"
    assert "context_window_unknown" in compatibility["gaps"]
    assert fallback.calls == 0


def test_fallback_tool_capability_includes_historical_tool_envelopes():
    events = []
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-history",
                    "type": "function",
                    "function": {"name": "query", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-history",
            "content": "{}",
        },
    ]
    primary = RoutedEngine(lambda *_: _raise(APIConnectionError("offline")))
    fallback = RoutedEngine(
        lambda *_: EngineResponse(content="must-not-run"),
        model="fallback-model",
        capabilities=ProviderCapabilities(
            tool_calling=False,
            context_window_tokens=16_384,
        ),
    )
    resilient = ResilientEngine(
        primary,
        fallback=fallback,
        max_retries=0,
        event_sink=events.append,
    )

    with pytest.raises(APIConnectionError):
        resilient.chat(messages, [])

    rejected = next(event for event in events if event["event"] == "fallback_rejected")
    compatibility = rejected["details"]["compatibility"]
    assert compatibility["requirements"]["tool_calling"] is True
    assert "tool_calling" in compatibility["gaps"]
    assert fallback.calls == 0


def test_fallback_winner_owns_terminal_usage_without_mutating_attempt_response():
    events = []
    primary_error = APIConnectionError("offline")
    primary_error.usage = {"prompt_tokens": 99}
    fallback_response = EngineResponse(
        content="fallback-ok",
        usage={"prompt_tokens": 2, "completion_tokens": 1},
    )
    primary = RoutedEngine(lambda *_: _raise(primary_error))
    fallback = RoutedEngine(lambda *_: fallback_response, model="fallback-model")

    response = ResilientEngine(
        primary,
        fallback=fallback,
        max_retries=0,
        event_sink=events.append,
    ).chat([], [])

    assert response is not fallback_response
    assert fallback_response.usage == {"prompt_tokens": 2, "completion_tokens": 1}
    assert response.usage["prompt_tokens"] == 2
    assert 99 not in response.usage.values()
    selected = [event for event in events if event["event"] == "provider_result_selected"]
    assert len(selected) == 1
    assert selected[0]["details"]["route_role"] == "fallback"
    assert selected[0]["details"]["superseded_attempts"] == 1


def test_turn_uses_frozen_gateway_route_even_if_engine_is_reconfigured():
    calls = []

    class Adapter:
        api_mode = ApiMode.CHAT_COMPLETIONS
        capabilities = ProviderCapabilities()

        def chat(self, route, messages, tools):
            calls.append(route.identity)
            return EngineResponse(content=route.model)

    gateway = ProviderGateway(adapters={ApiMode.CHAT_COMPLETIONS: Adapter()})
    engine = GatewayEngine(
        gateway,
        ProviderSpec(
            model="frozen-model",
            endpoint="https://frozen.example/v1",
            api_mode=ApiMode.CHAT_COMPLETIONS,
        ),
    )
    resilient = ResilientEngine(engine, max_retries=0)

    with resilient.runtime_context("run-frozen"):
        frozen = resilient.begin_turn_routes()[0]
        engine._configure_route(
            gateway,
            ProviderSpec(
                model="changed-model",
                endpoint="https://changed.example/v1",
                api_mode=ApiMode.CHAT_COMPLETIONS,
            ),
        )
        response = resilient.chat([], [])

    assert response.content == "frozen-model"
    assert calls == [frozen.identity]


def test_route_concurrency_is_bounded_without_real_sleep():
    lock = threading.Lock()
    release = threading.Event()
    two_active = threading.Event()
    active = 0
    max_active = 0

    def behavior(_call, _messages, _tools):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            if active == 2:
                two_active.set()
        assert release.wait(timeout=2)
        with lock:
            active -= 1
        return EngineResponse(content="ok")

    resilient = ResilientEngine(
        RoutedEngine(behavior),
        max_retries=0,
        route_max_concurrency=2,
    )
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(resilient.chat, [], []) for _ in range(3)]
        assert two_active.wait(timeout=2)
        assert max_active == 2
        release.set()
        assert [future.result(timeout=2).content for future in futures] == ["ok"] * 3

    assert max_active == 2


def test_half_open_allows_only_one_competing_probe():
    fake_time = FakeTime()
    events = []
    probe_started = threading.Event()
    release_probe = threading.Event()

    def behavior(call, _messages, _tools):
        if call == 1:
            raise APIConnectionError("open circuit")
        if call == 2:
            probe_started.set()
            assert release_probe.wait(timeout=2)
            return EngineResponse(content="probe-ok")
        pytest.fail("a competing half-open request reached the provider")

    routed = RoutedEngine(behavior)
    resilient = ResilientEngine(
        routed,
        max_retries=0,
        failure_threshold=1,
        cooldown_seconds=10,
        clock=fake_time.monotonic,
        route_max_concurrency=2,
        event_sink=events.append,
    )
    with pytest.raises(APIConnectionError):
        resilient.chat([], [])
    assert resilient.breaker.state == "open"
    fake_time.advance(10)

    with ThreadPoolExecutor(max_workers=2) as pool:
        probe = pool.submit(resilient.chat, [], [])
        assert probe_started.wait(timeout=2)
        competitor = pool.submit(resilient.chat, [], [])
        with pytest.raises(CircuitOpenError):
            competitor.result(timeout=2)
        release_probe.set()
        assert probe.result(timeout=2).content == "probe-ok"

    assert routed.calls == 2
    assert resilient.breaker.state == "closed"
    attempts = [event for event in events if event["event"] == "provider_attempt"]
    assert attempts[-1]["details"]["breaker_state_before"] == "half_open"
    assert attempts[-1]["details"]["breaker_state"] == "closed"


def test_breaker_isolated_by_endpoint_and_model():
    fake_time = FakeTime()
    registry = RouteStateRegistry(
        max_concurrency=2,
        failure_threshold=1,
        cooldown_seconds=30,
        capacity=8,
        idle_ttl_seconds=60,
        clock=fake_time.monotonic,
    )
    failing = ResilientEngine(
        RoutedEngine(
            lambda *_: _raise(APIConnectionError("route a down")),
            endpoint="https://a.example/v1",
            model="shared-model",
        ),
        max_retries=0,
        route_registry=registry,
    )
    other_endpoint = ResilientEngine(
        RoutedEngine(
            lambda *_: EngineResponse(content="endpoint-b"),
            endpoint="https://b.example/v1",
            model="shared-model",
        ),
        max_retries=0,
        route_registry=registry,
    )
    other_model = ResilientEngine(
        RoutedEngine(
            lambda *_: EngineResponse(content="model-b"),
            endpoint="https://a.example/v1",
            model="other-model",
        ),
        max_retries=0,
        route_registry=registry,
    )

    with pytest.raises(APIConnectionError):
        failing.chat([], [])
    assert failing.breaker.state == "open"
    assert other_endpoint.chat([], []).content == "endpoint-b"
    assert other_model.chat([], []).content == "model-b"
    assert other_endpoint.breaker.state == "closed"
    assert other_model.breaker.state == "closed"


def test_route_registry_capacity_and_idle_ttl_bound_lifetime():
    fake_time = FakeTime()
    registry = RouteStateRegistry(
        capacity=2,
        cooldown_seconds=1,
        idle_ttl_seconds=5,
        clock=fake_time.monotonic,
    )
    identities = [
        ("custom", "", "chat_completions", f"https://{index}.example/v1", "model")
        for index in range(4)
    ]
    for identity in identities[:3]:
        with registry.lease(identity):
            pass
    assert registry.route_count == 2

    fake_time.advance(5)
    with registry.lease(identities[3]):
        assert registry.route_count == 1
    assert registry.route_count == 1


def test_route_registry_preserves_degraded_state_until_ttl():
    fake_time = FakeTime()
    registry = RouteStateRegistry(
        failure_threshold=1,
        cooldown_seconds=1,
        capacity=1,
        idle_ttl_seconds=5,
        clock=fake_time.monotonic,
    )
    first = ("custom", "", "chat_completions", "https://a.example/v1", "model")
    second = ("custom", "", "chat_completions", "https://b.example/v1", "model")
    with registry.lease(first) as state:
        permit = state.breaker.try_acquire()
        assert permit is not None
        assert state.breaker.record_failure(permit) is True

    with pytest.raises(RouteStateCapacityError):
        with registry.lease(second):
            pass
    assert registry.route_count == 1

    fake_time.advance(5)
    with registry.lease(second):
        pass
    assert registry.route_count == 1


def test_attempt_events_persist_redacted_route_failure_delay_and_usage(tmp_path, monkeypatch):
    credential = "sk-attempt-canary-123456789"
    credential_env = "R14_ATTEMPT_CREDENTIAL"
    secret_body = "private complete provider body"
    monkeypatch.setenv(credential_env, credential)
    store = StateStore(tmp_path / "state.db")
    engine = RoutedEngine(
        _fail_once(
            HTTPError(429, f"api_key={credential}: {secret_body}", headers={"Retry-After": "2"}),
            usage={
                "prompt_tokens": 7,
                "completion_tokens": 3,
                "debug": secret_body,
                "api_key": credential,
                "details": {"cached_tokens": 1, "raw": secret_body},
            },
        ),
        credential_env=credential_env,
    )
    fake_time = FakeTime()
    resilient = ResilientEngine(
        engine,
        max_retries=1,
        failure_threshold=3,
        sleeper=fake_time.sleep,
        clock=fake_time.monotonic,
        wall_clock=fake_time.epoch,
        event_sink=lambda event: store.record_provider_event(**event),
    )

    with resilient.runtime_context("run-r14"):
        resilient.chat([{"role": "user", "content": secret_body}], [])

    with store.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM provider_events WHERE run_id=? AND event='provider_attempt' ORDER BY id",
            ("run-r14",),
        ).fetchall()
    assert len(rows) == 2
    first = json.loads(rows[0]["details_json"])
    second = json.loads(rows[1]["details_json"])
    assert first["attempt_sequence"] == 1
    assert first["failure_kind"] == "rate_limit"
    assert first["delay_seconds"] == 2
    assert first["breaker_state"] == "closed"
    assert tuple(first["route"]["route_identity"]) == engine.route.identity
    assert second["attempt_sequence"] == 2
    assert second["failure_kind"] is None
    assert second["usage"] == {
        "prompt_tokens": 7,
        "completion_tokens": 3,
        "details": {"cached_tokens": 1},
    }
    serialized = "\n".join(row["details_json"] for row in rows)
    assert credential not in serialized
    assert credential_env not in serialized
    assert secret_body not in serialized


def test_metric_redaction_preserves_numbers_but_rejects_unexpected_text():
    assert redact_sensitive(
        {
            "prompt_tokens": 7,
            "prompt_tokens_details": {"cached_tokens": 2},
            "completion_tokens": "private full text",
        }
    ) == {
        "prompt_tokens": 7,
        "prompt_tokens_details": {"cached_tokens": 2},
        "completion_tokens": "[REDACTED]",
    }
    assert redact_sensitive(
        {
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "query", "arguments": "{}"},
                }
            ]
        }
    )["tool_calls"][0]["function"]["name"] == "query"


def test_output_cap_response_is_audited_without_retry():
    events = []
    routed = RoutedEngine(
        lambda *_: EngineResponse(
            content="partial",
            finish_reason="length",
            usage={"completion_tokens": 10},
        )
    )
    response = ResilientEngine(
        routed,
        max_retries=3,
        event_sink=events.append,
    ).chat([], [])

    assert response.finish_reason == "length"
    assert routed.calls == 1
    attempt = next(event for event in events if event["event"] == "provider_attempt")
    assert attempt["details"]["failure_kind"] == "output_cap"
    assert attempt["details"]["retryable"] is False


def test_output_cap_response_never_activates_configured_fallback():
    events = []
    primary = RoutedEngine(
        lambda *_: EngineResponse(content="partial", finish_reason="length")
    )
    fallback = RoutedEngine(
        lambda *_: EngineResponse(content="must-not-run"),
        model="fallback-model",
    )

    response = ResilientEngine(
        primary,
        fallback=fallback,
        max_retries=0,
        event_sink=events.append,
    ).chat([], [])

    assert response.content == "partial"
    assert fallback.calls == 0
    assert not any(event["event"] == "fallback_activated" for event in events)


def test_model_resilience_config_defaults_validation_and_factory_wiring():
    defaults = ModelConfig(max_retries=0)
    assert defaults.retry_base_delay_seconds == 1.0
    assert defaults.retry_max_delay_seconds == 8.0
    assert defaults.retry_after_max_seconds == 60.0
    assert defaults.route_max_concurrency == 4
    assert defaults.route_state_capacity == 128
    assert defaults.route_state_ttl_seconds == 900.0

    with pytest.raises(ValueError, match="retry_max_delay_seconds"):
        ModelConfig(retry_base_delay_seconds=2, retry_max_delay_seconds=1)
    with pytest.raises(ValueError, match="route_max_concurrency"):
        ModelConfig(route_max_concurrency=0)
    with pytest.raises(ValueError, match="retry_after_max_seconds"):
        ModelConfig(retry_after_max_seconds=float("inf"))
    with pytest.raises(ValueError, match="route_state_ttl_seconds"):
        ModelConfig(route_state_ttl_seconds=30, circuit_cooldown_seconds=30)
    with pytest.raises(ValueError, match="需要 fallback_model"):
        ModelConfig(fallback_base_url="https://fallback.example/v1")
    with pytest.raises(ValueError, match="fallback_context_window_tokens"):
        ModelConfig(fallback_model="fallback")
    with pytest.raises(ValueError, match="api_mode"):
        ModelConfig(
            fallback_model="fallback",
            fallback_api_mode="unknown",
            fallback_context_window_tokens=16_384,
        )

    config = ModelConfig(
        max_retries=0,
        retry_base_delay_seconds=2,
        retry_max_delay_seconds=9,
        retry_after_max_seconds=17,
        route_max_concurrency=2,
        route_state_capacity=7,
        route_state_ttl_seconds=31,
        circuit_failure_threshold=5,
        circuit_cooldown_seconds=13,
    )
    resilient = get_engine(config, client=SimpleNamespace())
    assert isinstance(resilient, ResilientEngine)
    assert resilient.retry_base_delay_seconds == 2
    assert resilient.retry_max_delay_seconds == 9
    assert resilient.retry_after_max_seconds == 17
    assert resilient.route_registry.max_concurrency == 2
    assert resilient.route_registry.capacity == 7
    assert resilient.route_registry.idle_ttl_seconds == 31
    assert resilient.route_registry.failure_threshold == 5
    assert resilient.route_registry.cooldown_seconds == 13


def test_factory_fallback_mode_is_explicit_or_inherits_primary_at_startup():
    inherited = get_engine(
        ModelConfig(
            model="primary",
            endpoint="https://primary.example/v1",
            api_mode=ApiMode.RESPONSES,
            fallback_model="fallback",
            fallback_base_url="https://fallback.example/v1",
            fallback_context_window_tokens=16_384,
            max_retries=0,
        ),
        client=SimpleNamespace(),
    )
    assert [route.api_mode for route in inherited.begin_turn_routes()] == [
        ApiMode.RESPONSES,
        ApiMode.RESPONSES,
    ]

    explicit = get_engine(
        ModelConfig(
            model="primary",
            endpoint="https://primary.example/v1",
            api_mode=ApiMode.RESPONSES,
            fallback_model="fallback",
            fallback_base_url="https://fallback.example/v1",
            fallback_api_mode=ApiMode.CHAT_COMPLETIONS,
            fallback_context_window_tokens=16_384,
            max_retries=0,
        ),
        client=SimpleNamespace(),
    )
    assert [route.api_mode for route in explicit.begin_turn_routes()] == [
        ApiMode.RESPONSES,
        ApiMode.CHAT_COMPLETIONS,
    ]

    with pytest.raises(ValueError, match="identity 不能相同"):
        get_engine(
            ModelConfig(
                model="same-model",
                endpoint="https://same.example/v1",
                fallback_model="same-model",
                fallback_context_window_tokens=16_384,
                max_retries=0,
            ),
            client=SimpleNamespace(),
        )
