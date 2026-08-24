from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from edu_agent.data import db, generate
from edu_agent.delegation import (
    DelegationPolicy,
    DelegationRuntime,
    SubtaskStatus,
    TeachingSubtask,
    TeachingTaskKind,
)
from edu_agent.engine import (
    CredentialRef,
    Engine,
    EngineResponse,
    MockEngine,
    ProviderCapabilities,
    ProviderGateway,
    ProviderSpec,
    ProviderStreamEvent,
    ProviderStreamEventType,
    ResilientEngine,
)
from edu_agent.engine.streaming import aggregate_provider_stream, consume_provider_stream
from edu_agent.planning import ModelPlanGenerator
from edu_agent.runtime import (
    BUDGET_LEDGER_MIGRATION,
    BudgetAmounts,
    BudgetExceeded,
    BudgetIdentityError,
    BudgetLimits,
    BudgetOperationConflict,
    ModelPriceCatalog,
    RunBudgetLedger,
    RunContext,
)
from edu_agent.runtime.artifacts import ArtifactStore
from edu_agent.service import EduAgentService
from edu_agent.state import StateStore
from edu_agent.tools import registry


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 24, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, milliseconds: int) -> None:
        self.value += timedelta(milliseconds=milliseconds)


def _limits(**overrides) -> BudgetLimits:
    values = {
        "max_model_calls": 20,
        "max_tool_calls": 20,
        "max_input_tokens": 10_000,
        "max_output_tokens": 10_000,
        "max_total_tokens": 10_000,
        "max_cost_microusd": 1_000_000,
        "max_wall_time_ms": 60_000,
    }
    values.update(overrides)
    return BudgetLimits(**values)


def _ledger(
    store: StateStore,
    root_run_id: str,
    *,
    limits: BudgetLimits | None = None,
    pricing: ModelPriceCatalog | None = None,
) -> RunBudgetLedger:
    return RunBudgetLedger(
        store,
        root_run_id=root_run_id,
        session_id=f"session-{root_run_id}",
        actor_id="teacher-1",
        tenant_id="school-1",
        limits=limits or _limits(),
        pricing=pricing,
    )


def test_migration_is_idempotent_and_root_freezes_identity_and_prices(tmp_path):
    path = tmp_path / "state.db"
    store = StateStore(path)
    StateStore(path)
    pricing = ModelPriceCatalog(
        version="prices@v1",
        prices={
            "vendor:model-a": {
                "input_per_million_usd": "0.5",
                "output_per_million_usd": "1.25",
            }
        },
    )
    ledger = _ledger(store, "root", pricing=pricing)

    with store.connect() as connection:
        migration_count = connection.execute(
            "SELECT COUNT(*) FROM state_schema_migrations WHERE version=?",
            (BUDGET_LEDGER_MIGRATION,),
        ).fetchone()[0]
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(run_budget_ledgers)"
            ).fetchall()
        }
    assert migration_count == 1
    assert "pricing_json" in columns
    assert RunBudgetLedger.open(store, root_run_id="root").pricing.quote_microusd(
        provider="vendor",
        model="model-a",
        input_tokens=2,
        output_tokens=2,
    ) == 4

    with pytest.raises(BudgetIdentityError, match="root scope"):
        RunBudgetLedger(
            store,
            root_run_id="root",
            session_id="another-session",
            actor_id="teacher-1",
            tenant_id="school-1",
            pricing=pricing,
        )
    changed_prices = ModelPriceCatalog(
        version="prices@v1",
        prices={
            "vendor:model-a": {
                "input_per_million_usd": "0.6",
                "output_per_million_usd": "1.25",
            }
        },
    )
    with pytest.raises(BudgetIdentityError, match="prices cannot change"):
        RunBudgetLedger.open(store, root_run_id="root", pricing=changed_prices)
    assert ledger.snapshot()["pricing_version"] == "prices@v1"


def test_reserve_commit_release_are_atomic_idempotent_and_prompt_free(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = RunContext.create(
        session_id="session-root",
        run_id="root",
        actor_id="teacher-1",
        tenant_id="school-1",
        role="teacher",
    )
    store.ensure_session(
        context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        role=context.role,
    )
    store.enqueue_run(context, request_text="private prompt must not enter budget trace")
    ledger = _ledger(store, "root")
    requested = BudgetAmounts(
        model_calls=1,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        cost_microusd=30,
    )

    first = ledger.reserve(
        "model:root:1",
        owner_run_id="root",
        kind="model_attempt",
        amount=requested,
        metadata={"component": "agent_model", "prompt": "secret body"},
    )
    replay = ledger.reserve(
        "model:root:1",
        owner_run_id="root",
        kind="model_attempt",
        amount=requested,
        metadata={"component": "agent_model", "prompt": "different secret"},
    )
    assert replay["reserved"] == first["reserved"] == requested.to_dict()
    with pytest.raises(BudgetOperationConflict, match="another request"):
        ledger.reserve(
            "model:root:1",
            owner_run_id="root",
            kind="model_attempt",
            amount=BudgetAmounts(model_calls=2),
        )

    actual = BudgetAmounts(
        model_calls=1,
        input_tokens=7,
        output_tokens=2,
        total_tokens=9,
        cost_microusd=11,
    )
    committed = ledger.commit(
        "model:root:1",
        actual=actual,
        usage_source="provider_actual",
        cost_known=True,
    )
    committed_replay = ledger.commit(
        "model:root:1",
        actual=actual,
        usage_source="provider_actual",
        cost_known=True,
    )
    stable_dimensions = set(actual.to_dict()) - {"wall_time_ms"}
    assert {key: committed_replay[key] for key in stable_dimensions} == {
        key: committed[key] for key in stable_dimensions
    }
    ledger.reserve(
        "tool:root:cancelled",
        owner_run_id="root",
        kind="tool_call",
        amount=BudgetAmounts(tool_calls=1),
    )
    released = ledger.release("tool:root:cancelled", reason="cancelled")
    released_replay = ledger.release("tool:root:cancelled", reason="replay")
    assert released_replay["reserved"] == released["reserved"]
    assert released_replay["tool_calls"] == released["tool_calls"]
    assert released["model_calls"] == 1
    assert released["input_tokens"] == 7
    assert released["reserved"] == BudgetAmounts().to_dict()

    operation = ledger.operation("model:root:1")
    assert operation["metadata"] == {"component": "agent_model"}
    with store.connect() as connection:
        traces = connection.execute(
            "SELECT details_json FROM provider_events WHERE provider='budget'"
        ).fetchall()
    serialized = "\n".join(row["details_json"] for row in traces)
    assert "private prompt" not in serialized
    assert "secret body" not in serialized
    assert '"input_tokens":7' in serialized


def test_provider_missing_usage_uses_r41_estimate_and_unknown_price_is_null(tmp_path):
    store = StateStore(tmp_path / "state.db")
    pricing = ModelPriceCatalog(
        version="prices@v1",
        prices={
            "known:model-a": {
                "input_per_million_usd": 1,
                "output_per_million_usd": 2,
            }
        },
    )
    ledger = _ledger(store, "root", pricing=pricing)
    context = RunContext.create(
        session_id="session-root",
        run_id="root",
        actor_id="teacher-1",
        tenant_id="school-1",
        role="teacher",
    )
    context.budget.bind_ledger(ledger, owner_run_id="root")
    breakdown = SimpleNamespace(
        provider="known",
        model="model-a",
        estimated_input_tokens=7,
        max_output_reserve_tokens=3,
    )

    with context.budget.model_scope(
        "model:root:1",
        breakdowns=[breakdown],
        component="agent_model",
    ):
        attempt = context.budget.begin_provider_attempt(
            attempt_sequence=1,
            provider="known",
            model="model-a",
            route_role="primary",
        )
        context.budget.settle_provider_attempt(attempt, {}, status="ok")
    first = ledger.snapshot()
    assert (first["input_tokens"], first["output_tokens"], first["total_tokens"]) == (
        7,
        3,
        10,
    )
    assert first["estimated"] is True
    assert first["cost_usd"] == 0.000013

    with context.budget.model_scope(
        "model:root:2",
        breakdowns=[breakdown],
        component="fallback",
    ):
        attempt = context.budget.begin_provider_attempt(
            attempt_sequence=1,
            provider="unpriced",
            model="model-b",
            route_role="fallback",
        )
        assert ledger.snapshot()["cost_status"] == "unknown"
        context.budget.settle_provider_attempt(
            attempt,
            {"total_tokens": 8},
            status="ok",
        )
    snapshot = ledger.snapshot()
    assert snapshot["model_calls"] == 2
    assert snapshot["total_tokens"] == 18
    assert snapshot["cost_status"] == "unknown"
    assert snapshot["cost_usd"] is None
    assert snapshot["known_cost_usd"] == 0.000013
    operation = ledger.operation("model:root:2:provider-attempt:1")
    assert operation["usage_source"] == "estimated"
    assert operation["actual"]["cost_microusd"] is None


def test_concurrent_reserve_cannot_oversell(tmp_path):
    store = StateStore(tmp_path / "state.db")
    ledger = _ledger(store, "root", limits=_limits(max_tool_calls=4))

    def consume(index: int) -> bool:
        operation_id = f"tool:root:{index}"
        try:
            ledger.reserve(
                operation_id,
                owner_run_id="root",
                kind="tool_call",
                amount=BudgetAmounts(tool_calls=1),
            )
        except BudgetExceeded:
            return False
        ledger.commit(
            operation_id,
            actual=BudgetAmounts(tool_calls=1),
            usage_source="none",
            cost_known=True,
        )
        return True

    with ThreadPoolExecutor(max_workers=12) as pool:
        accepted = list(pool.map(consume, range(12)))

    assert sum(accepted) == 4
    snapshot = ledger.snapshot()
    assert snapshot["tool_calls"] == 4
    assert snapshot["reserved"]["tool_calls"] == 0
    assert snapshot["stop_reason"] == "budget_exhausted:tool_calls"
    successful = accepted.index(True)
    operation_id = f"tool:root:{successful}"
    ledger.reserve(
        operation_id,
        owner_run_id="root",
        kind="tool_call",
        amount=BudgetAmounts(tool_calls=1),
    )
    assert ledger.snapshot()["tool_calls"] == 4


@pytest.mark.parametrize(
    ("dimension", "limits", "amount"),
    [
        ("model_calls", _limits(max_model_calls=1), BudgetAmounts(model_calls=2)),
        ("tool_calls", _limits(max_tool_calls=1), BudgetAmounts(tool_calls=2)),
        ("input_tokens", _limits(max_input_tokens=10), BudgetAmounts(input_tokens=11)),
        ("output_tokens", _limits(max_output_tokens=10), BudgetAmounts(output_tokens=11)),
        ("total_tokens", _limits(max_total_tokens=10), BudgetAmounts(total_tokens=11)),
        ("cost_microusd", _limits(max_cost_microusd=1), BudgetAmounts(cost_microusd=2)),
    ],
)
def test_each_discrete_dimension_has_a_deterministic_stop_reason(
    tmp_path,
    dimension,
    limits,
    amount,
):
    ledger = _ledger(StateStore(tmp_path / "state.db"), "root", limits=limits)
    with pytest.raises(BudgetExceeded) as caught:
        ledger.reserve(
            f"exhaust:{dimension}",
            owner_run_id="root",
            kind="test",
            amount=amount,
        )
    assert caught.value.dimension == dimension
    assert caught.value.stop_reason == f"budget_exhausted:{dimension}"
    assert ledger.snapshot()["stop_reason"] == caught.value.stop_reason


def test_wall_time_exhaustion_persists_across_reopen(tmp_path):
    clock = MutableClock()
    path = tmp_path / "state.db"
    store = StateStore(path, clock=clock)
    ledger = _ledger(store, "root", limits=_limits(max_wall_time_ms=1_000))
    clock.advance(milliseconds=1_000)

    with pytest.raises(BudgetExceeded) as caught:
        ledger.check_limits()
    assert caught.value.stop_reason == "budget_exhausted:wall_time_ms"
    reopened = RunBudgetLedger.open(
        StateStore(path, clock=clock),
        root_run_id="root",
    )
    with pytest.raises(BudgetExceeded, match="wall_time_ms"):
        reopened.check_limits()
    assert reopened.snapshot()["wall_time_ms"] == 1_000


def test_reopen_keeps_usage_and_multiple_roots_are_isolated(tmp_path):
    path = tmp_path / "state.db"
    store = StateStore(path)
    limits = _limits(max_model_calls=1)
    first = _ledger(store, "root-a", limits=limits)
    second = _ledger(store, "root-b", limits=limits)
    first.reserve(
        "model:root-a:1",
        owner_run_id="root-a",
        kind="model_attempt",
        amount=BudgetAmounts(model_calls=1),
    )
    first.commit(
        "model:root-a:1",
        actual=BudgetAmounts(model_calls=1),
        usage_source="none",
        cost_known=True,
    )

    reopened = RunBudgetLedger.open(StateStore(path), root_run_id="root-a")
    reopened.reserve(
        "model:root-a:1",
        owner_run_id="root-a",
        kind="model_attempt",
        amount=BudgetAmounts(model_calls=1),
    )
    assert reopened.snapshot()["model_calls"] == 1
    with pytest.raises(BudgetExceeded, match="model_calls"):
        reopened.reserve(
            "model:root-a:2",
            owner_run_id="root-a",
            kind="model_attempt",
            amount=BudgetAmounts(model_calls=1),
        )
    assert second.snapshot()["model_calls"] == 0
    second.reserve(
        "model:root-b:1",
        owner_run_id="root-b",
        kind="model_attempt",
        amount=BudgetAmounts(model_calls=1),
    )
    assert second.snapshot()["reserved"]["model_calls"] == 1


def test_finalizer_releases_once_and_rejects_another_identity(tmp_path):
    store = StateStore(tmp_path / "state.db")
    ledger = _ledger(store, "root")
    ledger.reserve(
        "child:pending",
        owner_run_id="child",
        kind="child_reservation",
        amount=BudgetAmounts(model_calls=2, tool_calls=3),
    )

    first = ledger.finalize("budget-finalizer:root")
    replay = ledger.finalize("budget-finalizer:root")
    assert first == replay
    assert first["finalized"] is True
    assert first["reserved"] == BudgetAmounts().to_dict()
    with pytest.raises(BudgetOperationConflict, match="another finalizer"):
        ledger.finalize("budget-finalizer:other")
    with store.connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM run_budget_operations "
            "WHERE root_run_id='root' AND kind='finalizer'"
        ).fetchone()[0]
    assert count == 1


class APIConnectionError(Exception):
    pass


class RoutedEngine(Engine):
    def __init__(self, model: str, behavior) -> None:
        self.name = f"custom:{model}"
        self.route = ProviderGateway().begin_turn(
            ProviderSpec(
                model=model,
                provider="custom",
                endpoint=f"https://{model}.example/v1",
                credential=CredentialRef("TEST_PROVIDER_KEY"),
                capabilities=ProviderCapabilities(
                    streaming=False,
                    context_window_tokens=4_096,
                    max_output_tokens=256,
                ),
            )
        )
        self.behavior = behavior
        self.calls = 0

    def begin_turn_routes(self):
        return (self.route,)

    def chat(self, messages, tools):
        self.calls += 1
        return self.behavior(self.calls, messages, tools)


class RoutedStreamEngine(Engine):
    def __init__(self, model: str, behavior) -> None:
        self.name = f"custom:{model}"
        self.route = ProviderGateway().begin_turn(
            ProviderSpec(
                model=model,
                provider="custom",
                endpoint=f"https://{model}.example/v1",
                credential=CredentialRef("TEST_PROVIDER_KEY"),
                capabilities=ProviderCapabilities(
                    streaming=True,
                    context_window_tokens=4_096,
                    max_output_tokens=256,
                ),
            )
        )
        self.behavior = behavior

    def begin_turn_routes(self):
        return (self.route,)

    def stream_chat(self, messages, tools, *, attempt=1, **_kwargs):
        yield from self.behavior(self.route, attempt, messages, tools)

    def chat(self, messages, tools):
        return aggregate_provider_stream(self.stream_chat(messages, tools))


def test_planner_compression_retry_and_fallback_share_one_ledger(tmp_path):
    store = StateStore(tmp_path / "state.db")
    pricing = ModelPriceCatalog(
        version="prices@v1",
        prices={
            name: {
                "input_per_million_usd": 1,
                "output_per_million_usd": 1,
            }
            for name in ("mock", "custom:primary", "custom:fallback")
        },
    )
    ledger = _ledger(store, "root", pricing=pricing)
    context = RunContext.create(
        session_id="session-root",
        run_id="root",
        actor_id="teacher-1",
        tenant_id="school-1",
        role="teacher",
    )
    context.budget.bind_ledger(ledger, owner_run_id="root")
    plan_spec = {
        "goal": "inspect exams",
        "steps": [
            {
                "id": "inspect",
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
    planner_engine = MockEngine(
        lambda *_: EngineResponse(
            content=json.dumps(plan_spec),
            usage={"prompt_tokens": 2, "completion_tokens": 1},
            model="mock",
        )
    )
    ModelPlanGenerator(planner_engine).generate(
        "complex task",
        context=context,
        available_tools={"list_exams"},
        max_steps=4,
    )
    EduAgentService._record_compression_budget(
        context,
        operation_id="compression:root:initial:1",
        status="compacted",
    )

    primary = RoutedEngine(
        "primary",
        lambda *_: (_ for _ in ()).throw(APIConnectionError("offline")),
    )
    fallback = RoutedEngine(
        "fallback",
        lambda *_: EngineResponse(
            content="fallback-ok",
            usage={"prompt_tokens": 1, "completion_tokens": 1},
            model="fallback",
        ),
    )
    resilient = ResilientEngine(
        primary,
        fallback=fallback,
        max_retries=1,
        sleeper=lambda _delay: None,
        random_source=lambda: 0,
    )
    with context.budget.model_scope(
        "model:root:agent:1",
        component="agent_model",
    ):
        response = consume_provider_stream(
            resilient,
            [{"role": "user", "content": "request"}],
            [],
            run_budget=context.budget,
        )

    assert response.content == "fallback-ok"
    assert primary.calls == 2
    assert fallback.calls == 1
    snapshot = ledger.snapshot()
    assert snapshot["model_calls"] == 4
    assert snapshot["tool_calls"] == 0
    with store.connect() as connection:
        rows = connection.execute(
            "SELECT kind, metadata_json FROM run_budget_operations "
            "WHERE root_run_id='root' ORDER BY operation_id"
        ).fetchall()
    operations = [(row["kind"], json.loads(row["metadata_json"])) for row in rows]
    assert sum(kind == "model_attempt" for kind, _ in operations) == 4
    assert any(kind == "deterministic_compression" for kind, _ in operations)
    fallback_roles = [
        metadata.get("route_role")
        for kind, metadata in operations
        if kind == "model_attempt" and metadata.get("component") == "agent_model"
    ]
    assert fallback_roles == ["primary", "primary", "fallback"]


def test_failed_stream_usage_is_charged_before_fallback(tmp_path):
    store = StateStore(tmp_path / "state.db")
    pricing = ModelPriceCatalog(
        version="prices@v1",
        prices={
            name: {
                "input_per_million_usd": 1,
                "output_per_million_usd": 1,
            }
            for name in ("custom:primary", "custom:fallback")
        },
    )
    ledger = _ledger(store, "root", pricing=pricing)
    context = RunContext.create(
        session_id="session-root",
        run_id="root",
        actor_id="teacher-1",
        tenant_id="school-1",
        role="teacher",
    )
    context.budget.bind_ledger(ledger, owner_run_id="root")

    def primary(route, attempt, _messages, _tools):
        yield ProviderStreamEvent(
            ProviderStreamEventType.USAGE,
            route=route,
            attempt=attempt,
            provider_event_id="primary-usage",
            usage={"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13},
        )
        error = APIConnectionError("primary disconnected after usage")
        yield ProviderStreamEvent(
            ProviderStreamEventType.ERROR,
            route=route,
            attempt=attempt,
            provider_event_id="primary-error",
            error_code="connection",
            error_message=str(error),
            error=error,
            retryable=True,
        )

    def fallback(route, attempt, _messages, _tools):
        yield ProviderStreamEvent(
            ProviderStreamEventType.USAGE,
            route=route,
            attempt=attempt,
            provider_event_id="fallback-usage",
            usage={"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        )
        yield ProviderStreamEvent(
            ProviderStreamEventType.COMPLETED,
            route=route,
            attempt=attempt,
            provider_event_id="fallback-completed",
            finish_reason="stop",
            model="fallback",
            content_default="fallback-ok",
        )

    resilient = ResilientEngine(
        RoutedStreamEngine("primary", primary),
        fallback=RoutedStreamEngine("fallback", fallback),
        max_retries=0,
    )
    with context.budget.model_scope(
        "model:root:agent:1",
        component="agent_model",
    ):
        response = consume_provider_stream(
            resilient,
            [{"role": "user", "content": "request"}],
            [],
            event_sink=lambda _event: None,
            run_budget=context.budget,
        )

    assert response.content == "fallback-ok"
    assert response.usage["prompt_tokens"] == 3
    snapshot = ledger.snapshot()
    assert snapshot["model_calls"] == 2
    assert (snapshot["input_tokens"], snapshot["output_tokens"]) == (14, 3)
    assert snapshot["total_tokens"] == 17
    assert snapshot["cost_usd"] == 0.000017
    primary_attempt = ledger.operation("model:root:agent:1:provider-attempt:1")
    assert primary_attempt["usage_source"] == "provider_actual"
    assert primary_attempt["actual"]["total_tokens"] == 13


def test_parent_and_two_children_settle_actual_usage_and_release_failure_cap(tmp_path):
    teaching_path = generate.build(seed=42, out_path=str(tmp_path / "teaching.db"))
    store = StateStore(tmp_path / "state.db")
    context = RunContext.create(
        session_id="parent-session",
        run_id="parent",
        actor_id="teacher-1",
        tenant_id="school-1",
        role="teacher",
        course_ids={1},
        max_model_calls=10,
        max_tool_calls=10,
    )
    ledger = _ledger(
        store,
        "parent",
        limits=_limits(
            max_model_calls=10,
            max_tool_calls=10,
            max_input_tokens=1_000,
            max_output_tokens=1_000,
            max_total_tokens=1_000,
        ),
    )
    context.budget.bind_ledger(ledger, owner_run_id="parent")
    context.budget.consume_model_call("model:parent:1")
    context.budget.consume_tool_call("tool:parent:1")
    policy = DelegationPolicy(
        max_children_per_parent=2,
        max_concurrency=2,
        child_timeout_seconds=2,
        worker_lease_seconds=3,
        max_model_calls_per_child=2,
        max_tool_calls_per_child=1,
        max_tokens_per_child=100,
        max_cost_usd_per_child=0.01,
        max_root_model_calls=10,
        max_root_tool_calls=10,
        max_root_tokens=1_000,
        max_root_cost_usd=1,
    )

    def child_runner(execution):
        execution.execute_tool(
            "list_exams",
            {"class_id": 1, "course_id": 1, "page_size": 1},
        )
        payload = {"summary": f"done:{execution.task.task_key}"}
        if execution.task.task_key == "completed":
            payload["usage"] = {"total_tokens": 8}
        if execution.task.task_key == "over-cap":
            payload["usage"] = {"tool_calls": 2}
        return payload

    runtime = DelegationRuntime(
        store,
        registry,
        artifact_store=ArtifactStore(tmp_path / "artifacts", store),
        policy=policy,
        connection_factory=lambda: db.connect(teaching_path),
        child_runner=child_runner,
    )
    tasks = [
        TeachingSubtask(
            task_key=key,
            kind=TeachingTaskKind.class_analysis,
            task=f"task {key}",
            arguments={"class_id": 1, "course_id": 1},
            course_ids={1},
        )
        for key in ("completed", "over-cap")
    ]
    try:
        result = runtime.delegate(context, tasks)
        tree = runtime.tree(context)
    finally:
        runtime.close()

    statuses = {item.task_key: item.status for item in result.results}
    assert statuses == {
        "completed": SubtaskStatus.completed,
        "over-cap": SubtaskStatus.failed,
    }
    snapshot = ledger.snapshot()
    assert snapshot["model_calls"] == 1
    assert snapshot["tool_calls"] == 4
    assert snapshot["total_tokens"] == 8
    assert snapshot["reserved"] == BudgetAmounts().to_dict()
    assert tree["usage"]["tool_calls"] == 4
    assert tree["reserved"]["tool_calls"] == 0
    for child in tree["nodes"]:
        reservation = ledger.operation(f"delegation:{child['id']}:reservation")
        assert reservation["status"] == "committed"
