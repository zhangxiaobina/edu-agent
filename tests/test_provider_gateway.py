from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from edu_agent.engine import (
    ApiMode,
    CredentialRef,
    Engine,
    EngineResponse,
    GatewayEngine,
    ModeSource,
    ProviderAdapter,
    ProviderCapabilities,
    ProviderGateway,
    ProviderMetadata,
    ProviderSpec,
    ResilientEngine,
    ResolvedRoute,
    ResponsesAdapter,
    get_engine,
    normalize_endpoint,
)
from edu_agent.runtime.config import (
    AppConfig,
    ModelConfig,
    PlanningConfig,
    StorageConfig,
    load_config,
)
from edu_agent.service import EduAgentService


def _spec(**overrides) -> ProviderSpec:
    values = {
        "model": "qwen-plus",
        "endpoint": "https://gateway.example/v1",
    }
    values.update(overrides)
    return ProviderSpec(**values)


def test_adapter_protocol_and_contracts_are_small_and_runtime_checkable():
    class FakeAdapter:
        api_mode = ApiMode.CHAT_COMPLETIONS
        capabilities = ProviderCapabilities()

        def chat(self, route, messages, tools):
            return EngineResponse(content=f"{route.model}:{len(messages)}:{len(tools)}")

    adapter = FakeAdapter()
    assert isinstance(adapter, ProviderAdapter)
    route = ProviderGateway().begin_turn(_spec())
    assert adapter.chat(route, [{}], []).content == "qwen-plus:1:0"


def test_mode_resolution_priority_is_explicit_registry_official_then_default():
    registered_capabilities = ProviderCapabilities(
        structured_output=True,
        context_window_tokens=32_000,
    )
    registry = {
        "registered": ProviderMetadata(
            ApiMode.RESPONSES,
            capabilities=registered_capabilities,
        ),
        "openai": ProviderMetadata(ApiMode.RESPONSES),
    }
    gateway = ProviderGateway(registry)

    explicit = gateway.begin_turn(
        _spec(provider="registered", api_mode=ApiMode.CHAT_COMPLETIONS)
    )
    assert (explicit.api_mode, explicit.mode_source) == (
        ApiMode.CHAT_COMPLETIONS,
        ModeSource.EXPLICIT,
    )
    assert explicit.capabilities is registered_capabilities

    registered = gateway.begin_turn(
        _spec(provider="openai", endpoint="https://api.openai.com/v1")
    )
    assert (registered.api_mode, registered.mode_source) == (
        ApiMode.RESPONSES,
        ModeSource.REGISTRY,
    )

    official = ProviderGateway(registry={}).begin_turn(
        _spec(endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1")
    )
    assert (official.provider, official.mode_source) == (
        "dashscope",
        ModeSource.OFFICIAL_HOST,
    )

    default = ProviderGateway(registry={}).begin_turn(
        _spec(endpoint="http://127.0.0.1:8000/v1")
    )
    assert (default.api_mode, default.provider, default.mode_source) == (
        ApiMode.CHAT_COMPLETIONS,
        "custom",
        ModeSource.DEFAULT,
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://api.openai.com.evil.example/v1",
        "https://openai-compatible.example/v1",
        "http://localhost:8000/v1",
        "http://127.0.0.1:8000/openai/v1",
    ],
)
def test_custom_and_local_endpoints_are_not_fuzzily_inferred(endpoint):
    route = ProviderGateway().begin_turn(_spec(endpoint=endpoint))
    assert route.provider == "custom"
    assert route.mode_source is ModeSource.DEFAULT


def test_official_provider_conflict_fails_closed():
    with pytest.raises(ValueError, match="冲突"):
        ProviderGateway().begin_turn(
            _spec(provider="dashscope", endpoint="https://api.openai.com/v1")
        )


def test_self_hosted_or_unknown_provider_requires_explicit_endpoint():
    with pytest.raises(ValueError, match="必须显式配置 endpoint"):
        ProviderGateway().begin_turn(ProviderSpec(model="local-model", provider="vllm"))
    with pytest.raises(ValueError, match="必须显式配置 endpoint"):
        ProviderGateway().begin_turn(ProviderSpec(model="model", provider="unregistered"))


def test_route_identity_is_immutable_and_preserves_endpoint_path_semantics():
    endpoint = "HTTPS://API.OPENAI.COM:443/compatible-mode/v1/"
    route = ProviderGateway().begin_turn(_spec(endpoint=endpoint, deployment="Prod-A"))
    other = ProviderGateway().begin_turn(
        _spec(endpoint="https://api.openai.com/v1/", deployment="Prod-A")
    )

    assert route.endpoint == endpoint
    assert route.normalized_endpoint == "https://api.openai.com/compatible-mode/v1/"
    assert route.deployment == "Prod-A"
    assert normalize_endpoint(endpoint) == route.normalized_endpoint
    assert route.identity != other.identity
    with pytest.raises(FrozenInstanceError):
        route.model = "changed"

    with pytest.raises(ValueError, match="不一致"):
        ResolvedRoute(
            api_mode=route.api_mode,
            provider=route.provider,
            deployment=route.deployment,
            endpoint=route.endpoint,
            normalized_endpoint="https://api.openai.com/v1",
            model=route.model,
            capabilities=route.capabilities,
            mode_source=route.mode_source,
            credential=route.credential,
        )


def test_capability_and_credential_contracts_reject_invalid_values():
    with pytest.raises(ValueError, match="必须是 bool"):
        ProviderCapabilities(streaming="yes")
    with pytest.raises(ValueError, match="必须大于 0"):
        ProviderCapabilities(context_window_tokens=True)
    with pytest.raises(ValueError, match="名称无效"):
        CredentialRef("not-an-environment-variable")


@pytest.mark.parametrize(
    "endpoint",
    [
        "file:///tmp/provider.sock",
        "javascript:alert(1)",
        "https://user:embedded-secret@example.com/v1",
        "https://example.com/v1?api_key=embedded-secret",
        "https://example.com/v1#embedded-secret",
        "https://example.com\\@api.openai.com/v1",
        "https://example.com/%0d%0aInjected",
        "https://api%2eopenai.com/v1",
        "https://example.com/\x00hidden",
        "https://exa mple.com/v1",
    ],
)
def test_malicious_endpoint_is_rejected_without_echoing_secrets(endpoint):
    with pytest.raises(ValueError) as caught:
        ProviderSpec(model="qwen-plus", endpoint=endpoint)
    assert "embedded-secret" not in str(caught.value)


def test_unknown_mode_is_rejected_without_echoing_input():
    unknown = "secret-looking-unknown-mode"
    with pytest.raises(ValueError, match="chat_completions 或 responses") as caught:
        ProviderSpec(model="qwen-plus", api_mode=unknown)
    assert unknown not in str(caught.value)


def test_credential_values_never_enter_repr_identity_or_event(monkeypatch):
    canary = "canary-provider-key-9382"
    monkeypatch.setenv("R1_PROVIDER_CREDENTIAL", canary)
    credential = CredentialRef("R1_PROVIDER_CREDENTIAL")
    spec = _spec(credential=credential)
    route = ProviderGateway().begin_turn(spec)

    assert credential.resolve() == canary
    rendered = "\n".join(
        (repr(credential), repr(spec), repr(route), json.dumps(route.to_event()))
    )
    assert canary not in rendered
    assert "R1_PROVIDER_CREDENTIAL" not in rendered
    assert "credential" not in route.to_event()
    assert canary not in repr(route.identity)


def test_route_rejects_credential_material_in_audited_fields(monkeypatch):
    canary = "route-field-canary-5127"
    monkeypatch.setenv("R1_PROVIDER_CREDENTIAL", canary)
    with pytest.raises(ValueError, match="不得包含凭据") as caught:
        _spec(
            endpoint=f"https://gateway.example/{canary}/v1",
            credential=CredentialRef("R1_PROVIDER_CREDENTIAL"),
        )
    assert canary not in str(caught.value)


def test_model_config_keeps_legacy_base_url_and_parses_new_fields(tmp_path):
    legacy_path = tmp_path / "legacy.toml"
    legacy_path.write_text(
        """
[model]
provider = "openai"
model = "legacy-model"
base_url = "http://127.0.0.1:8000/v1"
""".strip(),
        encoding="utf-8",
    )
    legacy = load_config(legacy_path).model
    assert legacy.provider == "openai"
    assert legacy.configured_endpoint == "http://127.0.0.1:8000/v1"
    assert legacy.api_mode is None

    current_path = tmp_path / "current.toml"
    current_path.write_text(
        """
[model]
provider = "openai"
model = "gpt-example"
endpoint = "https://api.openai.com/v1"
api_mode = "responses"
vendor = "openai"
deployment = "prod-a"
""".strip(),
        encoding="utf-8",
    )
    current = load_config(current_path).model
    assert current.api_mode is ApiMode.RESPONSES
    assert current.provider_spec({}).provider == "openai"
    assert current.provider_spec({}).deployment == "prod-a"
    assert current.provider_spec({}).credential == CredentialRef("EDU_AGENT_API_KEY")


def test_legacy_toml_reaches_real_engine_factory_without_network(tmp_path, monkeypatch):
    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)
    config_path = tmp_path / "legacy.toml"
    config_path.write_text(
        """
[model]
provider = "openai"
model = "legacy-model"
base_url = "http://127.0.0.1:8000/v1"
""".strip(),
        encoding="utf-8",
    )
    engine = get_engine(load_config(config_path).model)
    route = engine.begin_turn_routes()[0]
    assert route.endpoint == "http://127.0.0.1:8000/v1"
    assert route.model == "legacy-model"
    assert route.api_mode is ApiMode.CHAT_COMPLETIONS


def test_model_config_defaults_and_endpoint_alias_conflict():
    config = ModelConfig()
    route = ProviderGateway().begin_turn(
        config.provider_spec(
            {
                "EDU_AGENT_BASE_URL": "",
                "EDU_AGENT_API_MODE": "",
                "EDU_AGENT_PROVIDER": "",
                "EDU_AGENT_DEPLOYMENT": "",
            }
        )
    )
    assert config.provider == "openai"
    assert config.model == "qwen-plus"
    assert route.api_mode is ApiMode.CHAT_COMPLETIONS
    assert route.mode_source is ModeSource.DEFAULT

    with pytest.raises(ValueError, match="不能同时配置"):
        ModelConfig(
            base_url="https://gateway.example/legacy/v1",
            endpoint="https://gateway.example/current/v1",
        )


def test_config_rejects_unknown_mode_and_malicious_url(tmp_path):
    unknown = tmp_path / "unknown.toml"
    unknown.write_text('[model]\napi_mode = "completions-ish"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="api_mode"):
        load_config(unknown)

    malicious = tmp_path / "malicious.toml"
    malicious.write_text(
        '[model]\nendpoint = "https://user:secret@example.com/v1"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="内嵌凭据") as caught:
        load_config(malicious)
    assert "secret" not in str(caught.value)


def test_legacy_environment_builds_chat_route_without_network(monkeypatch):
    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)
    canary = "legacy-env-key-3928"
    monkeypatch.setenv("EDU_AGENT_ENGINE", "openai")
    monkeypatch.setenv("EDU_AGENT_BASE_URL", "http://127.0.0.1:8000/v1")
    monkeypatch.setenv("EDU_AGENT_API_KEY", canary)
    monkeypatch.setenv("EDU_AGENT_MODEL", "legacy-env-model")
    monkeypatch.delenv("EDU_AGENT_API_MODE", raising=False)
    monkeypatch.delenv("EDU_AGENT_PROVIDER", raising=False)
    monkeypatch.delenv("EDU_AGENT_DEPLOYMENT", raising=False)

    engine = get_engine()
    route = engine.begin_turn_routes()[0]
    assert engine.base_url == "http://127.0.0.1:8000/v1"
    assert engine.model == "legacy-env-model"
    assert route.api_mode is ApiMode.CHAT_COMPLETIONS
    assert route.provider == "custom"
    assert canary not in repr(route)
    assert canary not in json.dumps(route.to_event())


def test_responses_mode_is_parsed_and_selects_responses_adapter(monkeypatch):
    monkeypatch.setenv("EDU_AGENT_ENGINE", "openai")
    monkeypatch.setenv("EDU_AGENT_API_MODE", "responses")
    monkeypatch.setenv("EDU_AGENT_BASE_URL", "https://api.openai.com/v1")
    engine = get_engine()

    route = engine.begin_turn_routes()[0]
    assert route.api_mode is ApiMode.RESPONSES
    assert isinstance(engine.gateway.adapter_for(route), ResponsesAdapter)


class _RoutedFakeEngine(Engine):
    def __init__(self, route: ResolvedRoute):
        self.route = route

    def begin_turn_routes(self) -> tuple[ResolvedRoute, ...]:
        return (self.route,)

    def chat(self, messages, tools):
        return EngineResponse(content="ok")


def test_service_freezes_and_audits_route_at_turn_start_without_credential(tmp_path, monkeypatch):
    canary = "turn-route-key-1729"
    monkeypatch.setenv("R1_TURN_CREDENTIAL", canary)
    route = ProviderGateway().begin_turn(
        _spec(credential=CredentialRef("R1_TURN_CREDENTIAL"), deployment="prod-a")
    )
    config = AppConfig(
        planning=PlanningConfig(enabled=False),
        storage=StorageConfig(state_path=str(tmp_path / "state.db")),
    )
    service = EduAgentService(_RoutedFakeEngine(route), config=config)

    result = service.chat("你好", actor_id="teacher-1")
    with service.state_store.connect() as connection:
        record = connection.execute(
            "SELECT provider, event, attempt, details_json FROM provider_events WHERE run_id=?",
            (result.run_id,),
        ).fetchone()

    assert record["provider"] == "custom"
    assert record["event"] == "route_resolved"
    assert record["attempt"] == 0
    assert json.loads(record["details_json"])["route_role"] == "primary"
    assert canary not in record["details_json"]


def test_trace_records_frozen_candidates_switch_reason_and_selected_result(
    tmp_path,
    monkeypatch,
):
    primary_key = "primary-trace-canary-4921"
    fallback_key = "fallback-trace-canary-7358"
    monkeypatch.setenv("R15_PRIMARY_CREDENTIAL", primary_key)
    monkeypatch.setenv("R15_FALLBACK_CREDENTIAL", fallback_key)

    class APIConnectionError(Exception):
        pass

    class Adapter:
        api_mode = ApiMode.CHAT_COMPLETIONS
        capabilities = ProviderCapabilities()

        def chat(self, route, messages, tools):
            if route.model == "primary-model":
                raise APIConnectionError("offline")
            return EngineResponse(content="fallback answer", usage={"total_tokens": 3})

    gateway = ProviderGateway(adapters={ApiMode.CHAT_COMPLETIONS: Adapter()})
    primary = GatewayEngine(
        gateway,
        ProviderSpec(
            model="primary-model",
            endpoint="https://primary.example/v1",
            api_mode=ApiMode.CHAT_COMPLETIONS,
            credential=CredentialRef("R15_PRIMARY_CREDENTIAL"),
            capabilities=ProviderCapabilities(
                context_window_tokens=16_384,
                max_output_tokens=4_096,
            ),
        ),
    )
    fallback = GatewayEngine(
        gateway,
        ProviderSpec(
            model="fallback-model",
            endpoint="https://fallback.example/v1",
            api_mode=ApiMode.CHAT_COMPLETIONS,
            credential=CredentialRef("R15_FALLBACK_CREDENTIAL"),
            capabilities=ProviderCapabilities(
                context_window_tokens=16_384,
                max_output_tokens=4_096,
            ),
        ),
    )
    config = AppConfig(
        planning=PlanningConfig(enabled=False),
        storage=StorageConfig(state_path=str(tmp_path / "state.db")),
    )
    service = EduAgentService(
        ResilientEngine(primary, fallback=fallback, max_retries=0),
        config=config,
    )

    result = service.chat("你好", actor_id="teacher-1")
    trace = service.trace_repository.list_events(
        actor_id="teacher-1",
        run_id=result.run_id,
        limit=100,
    ).to_dict()
    provider_events = [
        event
        for event in trace["events"]
        if event["component"] == "provider"
    ]
    route_events = [
        event
        for event in provider_events
        if event["attributes"]["event"] == "route_resolved"
    ]

    assert result.final_answer == "fallback answer"
    assert [
        event["attributes"]["details"]["selection_reason"]
        for event in route_events
    ] == ["configured_primary", "configured_fallback_candidate"]
    activated = next(
        event
        for event in provider_events
        if event["attributes"]["event"] == "fallback_activated"
    )
    assert activated["attributes"]["details"]["selection_reason"] == (
        "failure_policy_and_capabilities_allowed"
    )
    selected = [
        event
        for event in provider_events
        if event["attributes"]["event"] == "provider_result_selected"
    ]
    assert len(selected) == 1
    assert selected[0]["attributes"]["details"]["route_role"] == "fallback"
    rendered = json.dumps(trace, ensure_ascii=False)
    for secret in (
        primary_key,
        fallback_key,
        "R15_PRIMARY_CREDENTIAL",
        "R15_FALLBACK_CREDENTIAL",
    ):
        assert secret not in rendered
