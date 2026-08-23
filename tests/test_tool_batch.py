from __future__ import annotations

import contextvars
import json
import sqlite3
import statistics
import threading
import time
from collections.abc import Callable

import pytest

from edu_agent.agent.graph import run_agent
from edu_agent.engine.base import Engine, EngineResponse, ToolCall
from edu_agent.runtime.cancellation import CancellationRequested, CancellationToken
from edu_agent.runtime.config import RuntimeConfig
from edu_agent.runtime.models import BudgetExceeded, IterationBudget, RunContext
from edu_agent.runtime.tool_batch import ToolBatchPlanner
from edu_agent.runtime.tool_executor import ExecutionPolicy, PolicyToolExecutor
from edu_agent.state import StateStore
from edu_agent.tools.manifest import ToolEffect, ToolManifest
from edu_agent.tools.registry import ToolSpec


def _handler(connection, **arguments):
    return arguments


def _spec(
    name: str,
    *,
    effect: ToolEffect = ToolEffect.READ,
    parallel_safe: bool = True,
    resource_keys: tuple[str, ...] = ("/resource",),
    mutation_parameters: frozenset[str] = frozenset(),
    mutating: bool = False,
    timeout: float = 2.0,
    risk_level: str = "low",
    capability: str = "test.read",
) -> ToolSpec:
    properties = {"resource": {"type": "string"}}
    required = ["resource"]
    if mutation_parameters:
        properties["save"] = {"type": "boolean"}
        required.append("save")
    return ToolSpec(
        schema={
            "name": name,
            "description": name,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
        handler=_handler,
        category="query",
        risk_level=risk_level,
        source="builtin:test.tool_batch",
        version="1.0.0",
        capability=capability,
        effect=effect,
        parallel_safe=parallel_safe,
        resource_keys=resource_keys,
        mutation_parameters=mutation_parameters,
        mutating=mutating,
        timeout=timeout,
    )


class ControlledProvider:
    def __init__(
        self,
        specs: dict[str, ToolSpec],
        behavior: Callable[[str, dict, RunContext, sqlite3.Connection | None], dict],
        *,
        parallel_capability: bool = True,
    ):
        self.specs = specs
        self.behavior = behavior
        self.parallel_capability = parallel_capability

    def openai_tools(self, **kwargs):
        return [
            {"type": "function", "function": spec.schema}
            for spec in self.specs.values()
        ]

    def get_spec(self, name):
        return self.specs.get(name)

    def supports_parallel_tool_calls(self, name, *, context=None, entry=None):
        return self.parallel_capability

    def dispatch_with_context(
        self,
        name,
        arguments,
        context,
        conn=None,
        *,
        manifest=None,
    ):
        return self.behavior(name, arguments, context, conn)


class BatchEngine(Engine):
    name = "tool-batch-test"

    def __init__(self, calls: list[ToolCall]):
        self.calls = calls
        self.model_calls = 0

    def chat(self, messages, tools):
        self.model_calls += 1
        if any(message.get("role") == "tool" for message in messages):
            return EngineResponse(content="done")
        return EngineResponse(tool_calls=self.calls)


def _context(
    *,
    max_tool_calls: int = 16,
    cancellation_token: CancellationToken | None = None,
) -> RunContext:
    return RunContext.create(
        session_id="batch-session",
        run_id="batch-run",
        actor_id="teacher-1",
        tenant_id="school-1",
        role="teacher",
        course_ids={7, 8},
        max_model_calls=4,
        max_tool_calls=max_tool_calls,
        cancellation_token=cancellation_token,
    )


def _manifest(provider: ControlledProvider, context: RunContext) -> ToolManifest:
    return ToolManifest(
        tuple(spec.to_manifest_entry() for spec in provider.specs.values()),
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        role=context.role,
        course_ids=context.course_ids,
    )


def _run(
    provider: ControlledProvider,
    calls: list[ToolCall],
    *,
    context: RunContext | None = None,
    max_workers: int = 4,
    timeout: float = 2.0,
    state_store: StateStore | None = None,
    connection_factory=None,
):
    context = context or _context()
    manifest = _manifest(provider, context)
    executor = PolicyToolExecutor(
        provider,
        policy=ExecutionPolicy.legacy_demo(),
        state_store=state_store,
        manifest=manifest,
    )
    return run_agent(
        "batch",
        BatchEngine(calls),
        tools_provider=provider,
        run_context=context,
        tool_executor=executor,
        state_store=state_store,
        tool_manifest=manifest,
        tool_batch_max_workers=max_workers,
        tool_call_timeout_seconds=timeout,
        tool_connection_factory=connection_factory,
    )


def _tool_payloads(result: dict) -> list[dict]:
    return [
        json.loads(message["content"])
        for message in result["messages"]
        if message.get("role") == "tool"
    ]


def _calls(name: str, count: int) -> list[ToolCall]:
    return [
        ToolCall(f"call-{index}", name, {"resource": f"r-{index}"})
        for index in range(count)
    ]


def _start(target):
    box: dict[str, object] = {}

    def run():
        try:
            box["result"] = target()
        except BaseException as error:
            box["error"] = error

    thread = threading.Thread(target=run)
    thread.start()
    return thread, box


def _activate(store: StateStore, context: RunContext) -> None:
    store.ensure_session(
        context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        role=context.role,
        course_ids=context.course_ids,
    )
    store.enqueue_run(context, request_text="batch")
    claim = store.acquire_session_lease(
        session_id=context.session_id,
        run_id=context.run_id,
        owner_id="batch-worker",
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        lease_seconds=60,
    )
    context.bind_runtime_control(
        lease_owner="batch-worker",
        fencing_token=int(claim["fencing_token"]),
        control_check=lambda boundary: store.assert_run_writable(
            context,
            boundary=boundary,
        ),
    )
    store.start_run(
        run_id=context.run_id,
        session_id=context.session_id,
        model="batch-test",
        context_tokens=0,
        omitted_messages=0,
        context=context,
    )


def test_tool_batch_runtime_config_is_small_and_validated():
    config = RuntimeConfig()
    assert 1 <= config.tool_batch_max_workers <= 8
    assert config.tool_call_timeout_seconds > 0
    with pytest.raises(ValueError, match="tool_batch_max_workers"):
        RuntimeConfig(tool_batch_max_workers=0)
    with pytest.raises(ValueError, match="tool_call_timeout_seconds"):
        RuntimeConfig(tool_call_timeout_seconds=0)


def test_planner_splits_effect_bad_arguments_capability_and_resource_conflicts():
    specs = {
        "read": _spec("read"),
        "conditional": _spec(
            "conditional",
            effect=ToolEffect.CONDITIONAL_WRITE,
            parallel_safe=False,
            mutation_parameters=frozenset({"save"}),
            resource_keys=("/resource", "/save"),
        ),
    }
    provider = ControlledProvider(specs, lambda *args: {})
    context = _context()
    planner = ToolBatchPlanner(
        provider,
        _manifest(provider, context),
        max_call_timeout_seconds=1,
    )
    segments = planner.plan(
        [
            {"id": "a", "function": {"name": "read", "arguments": {"resource": "x"}}},
            {"id": "b", "function": {"name": "read", "arguments": {"resource": "y"}}},
            {
                "id": "c",
                "function": {
                    "name": "conditional",
                    "arguments": {"resource": "z", "save": False},
                },
            },
            {"id": "d", "function": {"name": "read", "arguments": "{"}},
            {"id": "e", "function": {"name": "read", "arguments": {"resource": "x"}}},
            {"id": "f", "function": {"name": "read", "arguments": {"resource": "x"}}},
            {"id": "g", "function": {"name": "read", "arguments": {"resource": "z"}}},
        ],
        context,
    )

    assert [(segment.mode, [call.call_id for call in segment.calls]) for segment in segments] == [
        ("parallel", ["a", "b"]),
        ("barrier", ["c"]),
        ("barrier", ["d"]),
        ("parallel", ["e"]),
        ("parallel", ["f", "g"]),
    ]
    assert segments[1].calls[0].barrier_reason == "effect:conditional_write"
    assert segments[2].calls[0].barrier_reason == "invalid_arguments"

    provider.parallel_capability = False
    denied = planner.plan(
        [{"id": "h", "function": {"name": "read", "arguments": {"resource": "q"}}}],
        context,
    )
    assert denied[0].mode == "barrier"
    assert denied[0].calls[0].barrier_reason == "provider_capability"


def test_write_approval_code_interactive_unknown_and_unregistered_tools_are_barriers():
    specs = {
        "write": _spec(
            "write",
            effect=ToolEffect.WRITE,
            parallel_safe=False,
            mutating=True,
            risk_level="high",
        ),
        "approval": _spec("approval", effect=ToolEffect.APPROVAL, parallel_safe=False),
        "code": _spec(
            "code",
            effect=ToolEffect.CODE_EXECUTION,
            parallel_safe=False,
            risk_level="critical",
            capability="code_execution",
        ),
        "interactive": _spec(
            "interactive",
            effect=ToolEffect.INTERACTIVE,
            parallel_safe=False,
        ),
        "unknown": _spec(
            "unknown",
            effect=ToolEffect.UNKNOWN,
            parallel_safe=False,
            risk_level="critical",
        ),
    }
    provider = ControlledProvider(specs, lambda *args: {})
    context = _context()
    planner = ToolBatchPlanner(provider, _manifest(provider, context), max_call_timeout_seconds=1)
    calls = [
        {
            "id": name,
            "function": {"name": name, "arguments": {"resource": name}},
        }
        for name in specs
    ] + [
        {
            "id": "missing",
            "function": {"name": "missing", "arguments": {"resource": "missing"}},
        }
    ]
    segments = planner.plan(calls, context)
    assert all(segment.mode == "barrier" and len(segment.calls) == 1 for segment in segments)
    assert [segment.calls[0].barrier_reason for segment in segments] == [
        "effect:write",
        "effect:approval",
        "effect:code_execution",
        "effect:interactive",
        "effect:unknown",
        "unknown_tool",
    ]


def test_parallel_and_serial_results_are_equivalent_and_keep_call_order():
    provider = ControlledProvider(
        {"read": _spec("read")},
        lambda name, arguments, context, connection: {
            "resource": arguments["resource"],
            "actor": context.actor_id,
        },
    )
    calls = _calls("read", 4)
    serial = _tool_payloads(_run(provider, calls, max_workers=1))
    parallel = _tool_payloads(_run(provider, calls, max_workers=4))

    def core(payload):
        return payload["ok"], payload["data"], payload["error"]

    assert [core(item) for item in parallel] == [core(item) for item in serial]
    assert [item["data"]["resource"] for item in parallel] == [f"r-{i}" for i in range(4)]
    assert all(item["meta"]["tool_batch_parallel"] for item in parallel)
    assert not any(item["meta"]["tool_batch_parallel"] for item in serial)


def test_max_concurrency_is_bounded_without_starting_queued_calls_early():
    lock = threading.Lock()
    release = threading.Event()
    first_wave_entered = threading.Event()
    third_entered = threading.Event()
    active = 0
    maximum = 0
    entered = 0

    def behavior(name, arguments, context, connection):
        nonlocal active, maximum, entered
        with lock:
            active += 1
            entered += 1
            maximum = max(maximum, active)
            if entered == 2:
                first_wave_entered.set()
            if entered == 3:
                third_entered.set()
        assert release.wait(2)
        with lock:
            active -= 1
        return {"resource": arguments["resource"]}

    provider = ControlledProvider({"read": _spec("read")}, behavior)
    thread, box = _start(lambda: _run(provider, _calls("read", 4), max_workers=2))
    assert first_wave_entered.wait(2)
    assert not third_entered.is_set()
    assert maximum == 2
    release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert "error" not in box
    assert maximum == 2


def test_worker_connections_context_route_and_contextvars_are_isolated(tmp_path):
    database = tmp_path / "workers.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE marker(value INTEGER)")
        connection.execute("INSERT INTO marker VALUES (1)")

    rendezvous = threading.Barrier(2)
    lock = threading.Lock()
    connection_ids: set[int] = set()
    context_ids: set[int] = set()
    worker_threads: set[int] = set()
    snapshots: list[tuple] = []
    trace_marker = contextvars.ContextVar("tool_batch_trace_marker", default=None)
    marker_token = trace_marker.set("trace-123")

    def behavior(name, arguments, context, connection):
        assert connection is not None
        with lock:
            connection_ids.add(id(connection))
            context_ids.add(id(context))
            worker_threads.add(threading.get_ident())
            snapshots.append(
                (
                    context.actor_id,
                    context.tenant_id,
                    context.course_ids,
                    context.fencing_token,
                    context.tool_manifest_hash,
                    context.provider_route,
                    context.trace_context,
                    trace_marker.get(),
                )
            )
        rendezvous.wait(timeout=2)
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == 1
        return {"resource": arguments["resource"]}

    provider = ControlledProvider({"read": _spec("read")}, behavior)
    try:
        result = _run(
            provider,
            _calls("read", 2),
            max_workers=2,
            connection_factory=lambda: sqlite3.connect(database),
        )
    finally:
        trace_marker.reset(marker_token)
    assert all(payload["ok"] for payload in _tool_payloads(result))
    assert len(connection_ids) == len(context_ids) == len(worker_threads) == 2
    assert all(snapshot[:3] == ("teacher-1", "school-1", frozenset({7, 8})) for snapshot in snapshots)
    assert all(
        snapshot[4]
        and snapshot[5]
        and snapshot[6]
        and snapshot[7] == "trace-123"
        for snapshot in snapshots
    )


def test_budget_reservation_selects_original_prefix_and_is_atomic_under_race():
    rendezvous = threading.Barrier(2)
    invoked: list[str] = []
    lock = threading.Lock()

    def behavior(name, arguments, context, connection):
        with lock:
            invoked.append(arguments["resource"])
        rendezvous.wait(timeout=2)
        return {"resource": arguments["resource"]}

    provider = ControlledProvider({"read": _spec("read")}, behavior)
    context = _context(max_tool_calls=2)
    payloads = _tool_payloads(
        _run(provider, _calls("read", 4), context=context, max_workers=4)
    )
    assert sorted(invoked) == ["r-0", "r-1"]
    assert [payload["error"]["code"] if payload["error"] else None for payload in payloads] == [
        None,
        None,
        "BUDGET_EXCEEDED",
        "BUDGET_EXCEEDED",
    ]
    assert context.budget.usage()["tool_calls"] == 2

    budget = IterationBudget(max_model_calls=3, max_tool_calls=3)
    start = threading.Barrier(9)
    reservations: list[int] = []

    def reserve():
        start.wait(timeout=2)
        value = budget.reserve_tool_calls(1)
        with lock:
            reservations.append(value)

    workers = [threading.Thread(target=reserve) for _ in range(8)]
    for worker in workers:
        worker.start()
    start.wait(timeout=2)
    for worker in workers:
        worker.join(2)
    assert sum(reservations) == 3
    assert budget.usage()["tool_calls"] == 3

    model_start = threading.Barrier(9)
    model_results: list[bool] = []

    def consume_model():
        model_start.wait(timeout=2)
        try:
            budget.consume_model_call()
        except BudgetExceeded:
            consumed = False
        else:
            consumed = True
        with lock:
            model_results.append(consumed)

    model_workers = [threading.Thread(target=consume_model) for _ in range(8)]
    for worker in model_workers:
        worker.start()
    model_start.wait(timeout=2)
    for worker in model_workers:
        worker.join(2)
    assert sum(model_results) == 3
    assert budget.usage()["model_calls"] == 3


def test_barrier_preserves_read_conditional_write_read_execution_order():
    order: list[str] = []

    def behavior(name, arguments, context, connection):
        order.extend((f"{name}.start", f"{name}.end"))
        return {"name": name}

    provider = ControlledProvider(
        {
            "before": _spec("before"),
            "conditional": _spec(
                "conditional",
                effect=ToolEffect.CONDITIONAL_WRITE,
                parallel_safe=False,
                mutation_parameters=frozenset({"save"}),
                resource_keys=("/resource", "/save"),
            ),
            "after": _spec("after"),
        },
        behavior,
    )
    result = _run(
        provider,
        [
            ToolCall("before", "before", {"resource": "a"}),
            ToolCall("write", "conditional", {"resource": "b", "save": False}),
            ToolCall("after", "after", {"resource": "c"}),
        ],
    )
    assert all(payload["ok"] for payload in _tool_payloads(result))
    assert order == [
        "before.start",
        "before.end",
        "conditional.start",
        "conditional.end",
        "after.start",
        "after.end",
    ]


def test_worker_failure_events_may_complete_out_of_order_but_results_and_journal_do_not(tmp_path):
    first_entered = threading.Event()
    release_first = threading.Event()
    second_completed_event = threading.Event()
    event_order: list[str] = []
    event_lock = threading.Lock()

    def behavior(name, arguments, context, connection):
        if arguments["resource"] == "first":
            first_entered.set()
            assert release_first.wait(2)
            return {"resource": "first"}
        raise ValueError("controlled worker failure")

    provider = ControlledProvider({"read": _spec("read")}, behavior)
    store = StateStore(tmp_path / "state.db")
    context = _context()
    _activate(store, context)

    def sink(event_type, payload):
        if event_type == "tool.completed":
            with event_lock:
                event_order.append(payload["tool_call_id"])
            if payload["tool_call_id"] == "second":
                second_completed_event.set()

    context.bind_event_sinks(run_event_sink=sink)
    calls = [
        ToolCall("first", "read", {"resource": "first"}),
        ToolCall("second", "read", {"resource": "second"}),
    ]
    thread, box = _start(
        lambda: _run(
            provider,
            calls,
            context=context,
            state_store=store,
            max_workers=2,
        )
    )
    assert first_entered.wait(2)
    assert second_completed_event.wait(2)
    release_first.set()
    thread.join(2)
    assert "error" not in box
    payloads = _tool_payloads(box["result"])
    assert [payload["data"] and payload["data"].get("resource") for payload in payloads] == [
        "first",
        None,
    ]
    assert payloads[1]["error"]["code"] == "TOOL_EXCEPTION"
    assert event_order[:2] == ["second", "first"]

    records = store.list_tool_call_records(
        run_id=context.run_id,
        session_id=context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
    )
    assert [record["tool_call_id"] for record in records] == ["first", "second"]
    assert [record["result_cursor"] for record in records] == sorted(
        record["result_cursor"] for record in records
    )


def test_mid_batch_cancellation_pairs_every_call_and_late_worker_cannot_commit(tmp_path):
    all_entered = threading.Event()
    release = threading.Event()
    returned = threading.Event()
    lock = threading.Lock()
    entered = 0

    def behavior(name, arguments, context, connection):
        nonlocal entered
        with lock:
            entered += 1
            if entered == 2:
                all_entered.set()
        assert release.wait(2)
        returned.set()
        return {"late": arguments["resource"]}

    provider = ControlledProvider({"read": _spec("read")}, behavior)
    token = CancellationToken()
    context = _context(cancellation_token=token)
    store = StateStore(tmp_path / "state.db")
    _activate(store, context)
    thread, box = _start(
        lambda: _run(
            provider,
            _calls("read", 2),
            context=context,
            state_store=store,
            max_workers=2,
        )
    )
    assert all_entered.wait(2)
    token.cancel("test cancellation", source="explicit")
    thread.join(2)
    assert not thread.is_alive()
    assert isinstance(box.get("error"), CancellationRequested)

    records = store.list_tool_call_records(
        run_id=context.run_id,
        session_id=context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
    )
    assert len(records) == 2
    assert all(record["status"] == "completed" for record in records)
    persisted_results = [
        message
        for message in store.get_run_messages(context.run_id)
        if message.get("role") == "tool"
    ]
    assert all(
        json.loads(message["content"])["error"]["code"] == "CANCELLED"
        for message in persisted_results
    )
    assert store.count("tool_events") == 0

    release.set()
    assert returned.wait(2)
    assert store.count("tool_events") == 0
    records_after = store.list_tool_call_records(
        run_id=context.run_id,
        session_id=context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
    )
    assert [record["result_hash"] for record in records_after] == [
        record["result_hash"] for record in records
    ]


def test_cancellation_after_envelope_commit_pairs_calls_without_starting_workers(tmp_path):
    token = CancellationToken()
    invoked = threading.Event()

    class CancelAfterEnvelopeStore(StateStore):
        def append_assistant_tool_envelope(self, *args, **kwargs):
            committed = super().append_assistant_tool_envelope(*args, **kwargs)
            token.cancel("cancel after envelope commit", source="explicit")
            return committed

    store = CancelAfterEnvelopeStore(tmp_path / "state.db")
    context = _context(cancellation_token=token)
    _activate(store, context)
    provider = ControlledProvider(
        {"read": _spec("read")},
        lambda name, arguments, worker_context, connection: invoked.set(),
    )

    with pytest.raises(CancellationRequested):
        _run(
            provider,
            _calls("read", 2),
            context=context,
            state_store=store,
            max_workers=2,
        )

    assert not invoked.is_set()
    assert context.budget.usage()["tool_calls"] == 0
    records = store.list_tool_call_records(
        run_id=context.run_id,
        session_id=context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
    )
    assert [record["tool_call_id"] for record in records] == ["call-0", "call-1"]
    assert all(record["status"] == "completed" for record in records)
    persisted_results = [
        json.loads(message["content"])
        for message in store.get_run_messages(context.run_id)
        if message.get("role") == "tool"
    ]
    assert [result["error"]["code"] for result in persisted_results] == [
        "CANCELLED",
        "CANCELLED",
    ]


def test_cancellation_between_incremental_result_commits_leaves_no_pending_call(tmp_path):
    token = CancellationToken()

    class CancelAfterFirstResultStore(StateStore):
        def __init__(self, path):
            super().__init__(path)
            self.result_commits = 0

        def append_tool_result(self, *args, **kwargs):
            committed = super().append_tool_result(*args, **kwargs)
            self.result_commits += 1
            if self.result_commits == 1:
                token.cancel("cancel between result commits", source="explicit")
            return committed

    store = CancelAfterFirstResultStore(tmp_path / "state.db")
    context = _context(cancellation_token=token)
    _activate(store, context)
    provider = ControlledProvider(
        {"read": _spec("read")},
        lambda name, arguments, worker_context, connection: {
            "resource": arguments["resource"]
        },
    )
    thread, box = _start(
        lambda: _run(
            provider,
            _calls("read", 2),
            context=context,
            state_store=store,
            max_workers=2,
        )
    )
    thread.join(2)
    assert not thread.is_alive()
    assert isinstance(box.get("error"), CancellationRequested)
    records = store.list_tool_call_records(
        run_id=context.run_id,
        session_id=context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
    )
    assert len(records) == 2
    assert all(record["status"] == "completed" for record in records)


def test_timeout_pairs_call_aborts_later_barrier_and_rejects_late_value():
    entered = threading.Event()
    release = threading.Event()
    late_returned = threading.Event()
    later_started = threading.Event()

    def behavior(name, arguments, context, connection):
        if name == "slow":
            entered.set()
            assert release.wait(2)
            late_returned.set()
            return {"late": True}
        later_started.set()
        return {"later": True}

    provider = ControlledProvider(
        {
            "slow": _spec("slow", timeout=1),
            "later": _spec(
                "later",
                effect=ToolEffect.CONDITIONAL_WRITE,
                parallel_safe=False,
                mutation_parameters=frozenset({"save"}),
                resource_keys=("/resource", "/save"),
            ),
        },
        behavior,
    )
    thread, box = _start(
        lambda: _run(
            provider,
            [
                ToolCall("slow", "slow", {"resource": "slow"}),
                ToolCall("later", "later", {"resource": "later", "save": False}),
            ],
            timeout=0.05,
        )
    )
    assert entered.wait(2)
    thread.join(2)
    try:
        assert not thread.is_alive()
        assert "error" not in box
        payloads = _tool_payloads(box["result"])
        assert [payload["error"]["code"] for payload in payloads] == [
            "TOOL_TIMEOUT",
            "CANCELLED",
        ]
        assert not later_started.is_set()
        assert payloads[0]["data"] is None
    finally:
        release.set()
    assert late_returned.wait(2)


def test_repeatable_p95_fixture_shows_parallel_speedup_with_structural_concurrency():
    def sample(max_workers: int) -> tuple[list[float], int]:
        durations = []
        observed_max = 0
        for _ in range(7):
            gate = threading.Event()
            lock = threading.Lock()
            active = 0
            maximum = 0

            def behavior(name, arguments, context, connection):
                nonlocal active, maximum
                with lock:
                    active += 1
                    maximum = max(maximum, active)
                gate.wait(0.025)
                with lock:
                    active -= 1
                return {"resource": arguments["resource"]}

            provider = ControlledProvider({"read": _spec("read")}, behavior)
            started = time.perf_counter()
            result = _run(provider, _calls("read", 2), max_workers=max_workers)
            durations.append(time.perf_counter() - started)
            assert all(payload["ok"] for payload in _tool_payloads(result))
            observed_max = max(observed_max, maximum)
        return durations, observed_max

    serial, serial_max = sample(1)
    parallel, parallel_max = sample(2)
    serial_p95 = statistics.quantiles(serial, n=100, method="inclusive")[94]
    parallel_p95 = statistics.quantiles(parallel, n=100, method="inclusive")[94]
    assert serial_max == 1
    assert parallel_max == 2
    assert parallel_p95 < serial_p95 * 0.8
