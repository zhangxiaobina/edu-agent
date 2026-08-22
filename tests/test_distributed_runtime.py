from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta

import pytest

from edu_agent.data import db, generate
from edu_agent.engine.base import Engine, EngineResponse, ToolCall
from edu_agent.planning.runtime import PlanCoordinator, PlanningOptions
from edu_agent.runtime.artifacts import ArtifactStore
from edu_agent.runtime.config import (
    AppConfig,
    MemoryConfig,
    RuntimeConfig,
    StorageConfig,
)
from edu_agent.runtime.models import RunContext
from edu_agent.runtime.tool_executor import ExecutionPolicy, PolicyToolExecutor
from edu_agent.service import EduAgentService
from edu_agent.state import FencingTokenRejected, SessionLeaseUnavailable, StateStore
from edu_agent.tools import registry
from edu_agent.tools.registry import ToolSpec


class MutableClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


def _context(session_id: str, run_id: str) -> RunContext:
    return RunContext.create(
        session_id=session_id,
        run_id=run_id,
        actor_id="teacher-1",
        tenant_id="school-1",
        role="teacher",
        course_ids={1},
    )


def _queued(store: StateStore, session_id: str, run_id: str) -> RunContext:
    context = _context(session_id, run_id)
    store.ensure_session(
        session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        role=context.role,
        course_ids=context.course_ids,
    )
    store.enqueue_run(context, request_text=f"request:{run_id}")
    return context


def _claim(store: StateStore, context: RunContext, owner: str, lease_seconds=30) -> dict:
    claim = store.acquire_session_lease(
        session_id=context.session_id,
        run_id=context.run_id,
        owner_id=owner,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        lease_seconds=lease_seconds,
    )
    context.bind_runtime_control(
        lease_owner=owner,
        fencing_token=claim["fencing_token"],
        control_check=lambda boundary: store.assert_run_writable(
            context,
            boundary=boundary,
        ),
    )
    return claim


def test_two_store_instances_single_flight_same_session_and_parallel_sessions(tmp_path):
    path = tmp_path / "state.db"
    first = StateStore(path)
    second = StateStore(path)
    first_context = _queued(first, "shared", "run-1")
    second_context = _queued(second, "shared", "run-2")
    other_context = _queued(second, "other", "run-3")

    _claim(first, first_context, "instance-a")
    with pytest.raises(SessionLeaseUnavailable):
        _claim(second, second_context, "instance-b")
    other_claim = _claim(second, other_context, "instance-b")

    assert other_claim["active_run_id"] == "run-3"
    assert first.get_session_status(
        "shared",
        actor_id="teacher-1",
        tenant_id="school-1",
    )["current_owner"] == "instance-a"


def test_reclaim_increments_fence_and_rejects_old_owner_writes(tmp_path):
    clock = MutableClock(datetime(2026, 8, 17, tzinfo=UTC))
    path = tmp_path / "state.db"
    first = StateStore(path, clock=clock)
    second = StateStore(path, clock=clock)
    old_context = _queued(first, "shared", "old-run")
    first_claim = _claim(first, old_context, "instance-a", lease_seconds=5)
    clock.advance(6)
    new_context = _queued(second, "shared", "new-run")
    second_claim = _claim(second, new_context, "instance-b", lease_seconds=5)

    assert second_claim["fencing_token"] > first_claim["fencing_token"]
    with pytest.raises(FencingTokenRejected):
        first.append_messages(
            "shared",
            [{"role": "assistant", "content": "stale"}],
            context=old_context,
        )
    second.append_messages(
        "shared",
        [{"role": "assistant", "content": "fresh"}],
        context=new_context,
    )
    assert [item["content"] for item in first.get_messages("shared")] == ["fresh"]
    assert first.get_run_status(
        "old-run",
        actor_id="teacher-1",
        tenant_id="school-1",
    )["status"] == "abandoned"


def test_heartbeat_prevents_reclaim_at_original_expiry(tmp_path):
    clock = MutableClock(datetime(2026, 8, 17, tzinfo=UTC))
    path = tmp_path / "state.db"
    first = StateStore(path, clock=clock)
    second = StateStore(path, clock=clock)
    active = _queued(first, "shared", "active-run")
    claim = _claim(first, active, "instance-a", lease_seconds=5)
    clock.advance(4)
    assert first.heartbeat_session_lease(
        session_id="shared",
        run_id="active-run",
        owner_id="instance-a",
        fencing_token=claim["fencing_token"],
        lease_seconds=5,
    )
    clock.advance(2)
    contender = _queued(second, "shared", "contender")
    with pytest.raises(SessionLeaseUnavailable):
        _claim(second, contender, "instance-b", lease_seconds=5)


class BlockingEngine(Engine):
    name = "blocking"

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def chat(self, messages, tools):
        self.entered.set()
        assert self.release.wait(3)
        return EngineResponse(content="must-be-discarded")


class TwoToolEngine(Engine):
    name = "two-tool"

    def __init__(self):
        self.step = 0

    def chat(self, messages, tools):
        if self.step == 0:
            self.step += 1
            return EngineResponse(tool_calls=[ToolCall("call-1", "first", {})])
        if self.step == 1:
            self.step += 1
            return EngineResponse(tool_calls=[ToolCall("call-2", "second", {})])
        return EngineResponse(content="done")


class QueryProvider:
    def __init__(self, handlers):
        self.handlers = handlers
        self.specs = {
            name: ToolSpec(
                schema={
                    "name": name,
                    "description": name,
                    "parameters": {"type": "object", "properties": {}},
                },
                handler=lambda conn, **kwargs: {},
                category="query",
            )
            for name in handlers
        }

    def openai_tools(self, **kwargs):
        return [
            {"type": "function", "function": spec.schema}
            for spec in self.specs.values()
        ]

    def get_spec(self, name):
        return self.specs.get(name)

    def dispatch(self, name, arguments, conn=None):
        return self.handlers[name]()


def _service(tmp_path, engine, *, provider=None, approval_handler=None):
    config = AppConfig(
        runtime=RuntimeConfig(
            max_model_calls=8,
            max_tool_calls=8,
            session_lease_seconds=30,
            session_heartbeat_seconds=10,
            run_stall_seconds=90,
        ),
        memory=MemoryConfig(enabled=False),
        storage=StorageConfig(state_path=str(tmp_path / "state.db")),
    )
    return EduAgentService(
        engine,
        config=config,
        tools_provider=provider,
        approval_handler=approval_handler,
    )


def _run_chat(service, results, errors):
    try:
        results.append(
            service.chat(
                "执行测试",
                actor_id="teacher-1",
                tenant_id="school-1",
                role="teacher",
            )
        )
    except Exception as error:
        errors.append(error)


def test_cancel_while_model_inflight_discards_response(tmp_path):
    engine = BlockingEngine()
    service = _service(tmp_path, engine)
    results, errors = [], []
    thread = threading.Thread(target=_run_chat, args=(service, results, errors))
    thread.start()
    assert engine.entered.wait(2)
    run_id = service.runtime_manager.active_runs()[0]["run_id"]
    assert service.cancel_run(run_id, actor_id="teacher-1", tenant_id="school-1")
    engine.release.set()
    thread.join(3)

    assert errors == []
    assert results[0].stop_reason == "interrupted"
    assert results[0].context["estimated_tokens"] > 0
    assert "must-be-discarded" not in str(service.state_store.get_messages(results[0].session_id))


def test_cancel_between_tool_calls_stops_second_call(tmp_path):
    service_ref = {}
    seen = []

    def first():
        seen.append("first")
        service = service_ref["service"]
        run_id = service.runtime_manager.active_runs()[0]["run_id"]
        service.cancel_run(run_id, actor_id="teacher-1", tenant_id="school-1")
        return {"value": 1}

    def second():
        seen.append("second")
        return {"value": 2}

    service = _service(
        tmp_path,
        TwoToolEngine(),
        provider=QueryProvider({"first": first, "second": second}),
    )
    service_ref["service"] = service
    result = service.chat(
        "执行测试",
        actor_id="teacher-1",
        tenant_id="school-1",
        role="teacher",
    )

    assert result.stop_reason == "interrupted"
    assert seen == ["first"]
    protocol = service.state_store.get_run_messages(result.run_id)
    assert [message["role"] for message in protocol] == [
        "user",
        "assistant",
        "tool",
    ]
    assert [message["tool_call_id"] for message in protocol[2:]] == ["call-1"]
    assert all(
        json.loads(message["content"])["error"]["code"] == "CANCELLED"
        for message in protocol[2:]
    )


class MutatingProvider(QueryProvider):
    transactional_base = None

    def __init__(self):
        super().__init__({"write": lambda: {}})
        self.specs["write"] = ToolSpec(
            schema={
                "name": "write",
                "description": "write",
                "parameters": {"type": "object", "properties": {}},
            },
            handler=lambda conn, **kwargs: {},
            category="operation",
            mutating=True,
        )


class WriteEngine(Engine):
    name = "write"

    def chat(self, messages, tools):
        return EngineResponse(tool_calls=[ToolCall("write-1", "write", {})])


def test_cancel_during_approval_wait_stops_before_write(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    def approve(request):
        entered.set()
        assert release.wait(3)
        return True

    service = _service(
        tmp_path,
        WriteEngine(),
        provider=MutatingProvider(),
        approval_handler=approve,
    )
    service.tools_provider.transactional_base = registry
    results, errors = [], []
    thread = threading.Thread(target=_run_chat, args=(service, results, errors))
    thread.start()
    assert entered.wait(2)
    run_id = service.runtime_manager.active_runs()[0]["run_id"]
    assert service.cancel_run(run_id, actor_id="teacher-1", tenant_id="school-1")
    release.set()
    thread.join(3)

    assert errors == []
    assert results[0].stop_reason == "interrupted"
    with service.state_store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM tool_operation_refs").fetchone()[0] == 1
        assert connection.execute(
            "SELECT status FROM tool_operation_refs"
        ).fetchone()[0] == "prepared"
    protocol = service.state_store.get_run_messages(results[0].run_id)
    assert [message["role"] for message in protocol] == [
        "user",
        "assistant",
        "tool",
    ]
    cancelled = json.loads(protocol[-1]["content"])
    assert cancelled["error"]["code"] == "CANCELLED"
    assert cancelled["meta"]["operation_id"]


def test_stalled_recovery_preserves_state_and_blocks_uncertain_write(tmp_path):
    clock = MutableClock(datetime(2026, 8, 17, tzinfo=UTC))
    store = StateStore(tmp_path / "state.db", clock=clock)
    safe = _queued(store, "safe", "safe-run")
    _claim(store, safe, "dead-instance", lease_seconds=5)
    plan = store.create_plan(
        run_id=safe.run_id,
        session_id=safe.session_id,
        actor_id=safe.actor_id,
        tenant_id=safe.tenant_id,
        spec={
            "goal": "recover",
            "steps": [
                {
                    "id": "step-1",
                    "goal": "write once",
                    "depends_on": [],
                    "allowed_tools": ["create_exam"],
                    "expected_tools": ["create_exam"],
                    "completion_conditions": [
                        {"kind": "tool_success", "tool": "create_exam"}
                    ],
                }
            ],
        },
        max_iterations=4,
        context=safe,
    )
    artifacts = ArtifactStore(tmp_path / "artifacts", store)
    artifact = artifacts.write_text(
        "recoverable",
        context=safe,
        kind="checkpoint",
    )
    business_path = tmp_path / "business.db"
    generate.build(seed=42, out_path=business_path)
    business = db.connect(business_path)
    first_write = PolicyToolExecutor(
        registry,
        policy=ExecutionPolicy(require_write_approval=False),
        state_store=store,
    ).execute(
        "create_exam",
        {"exam_name": "恢复考试", "class_id": 3, "course_id": 1},
        safe,
        conn=business,
        caller_idempotency_key="recovery-write-once",
    )
    assert first_write.ok
    uncertain = _queued(store, "uncertain", "uncertain-run")
    _claim(store, uncertain, "dead-instance", lease_seconds=5)
    store.upsert_tool_operation_ref(
        {
            "id": "uncertain-op",
            "idempotency_key": "key-2",
            "payload_hash": "hash-2",
            "tool_name": "write",
            "tenant_id": uncertain.tenant_id,
            "actor_id": uncertain.actor_id,
            "session_id": uncertain.session_id,
            "run_id": uncertain.run_id,
            "status": "executing",
            "updated_at": clock().isoformat(),
        },
        context=uncertain,
    )
    clock.advance(100)

    recovered = store.recover_stalled_runs(stall_timeout_seconds=90)
    by_run = {item["run_id"]: item for item in recovered}

    assert by_run["safe-run"]["recovery_recommendation"] == "resume_from_persistent_plan"
    assert by_run["uncertain-run"]["recovery_recommendation"] == "manual_review"
    assert store.count("artifacts") == 1
    with store.connect() as connection:
        assert connection.execute(
            "SELECT status FROM tool_operation_refs WHERE operation_id=?",
            (first_write.meta["operation_id"],),
        ).fetchone()[0] == "committed"
        assert connection.execute(
            "SELECT status FROM tool_operation_refs WHERE operation_id='uncertain-op'"
        ).fetchone()[0] == "manual_review"
    store.prepare_run_resume(
        "safe-run",
        actor_id="teacher-1",
        tenant_id="school-1",
    )
    with pytest.raises(RuntimeError, match="manual_review"):
        store.prepare_run_resume(
            "uncertain-run",
            actor_id="teacher-1",
            tenant_id="school-1",
        )
    resumed = _context("safe", "safe-run")
    resumed_claim = _claim(store, resumed, "replacement-instance", lease_seconds=5)
    assert resumed_claim["fencing_token"] > safe.fencing_token
    coordinator = PlanCoordinator(store, resumed, options=PlanningOptions())
    assert coordinator.plan is not None and coordinator.plan.id == plan["id"]
    assert artifacts.read_text(artifact.id, context=resumed) == "recoverable"
    replay = PolicyToolExecutor(
        registry,
        policy=ExecutionPolicy(require_write_approval=False),
        state_store=store,
    ).execute(
        "create_exam",
        {"exam_name": "恢复考试", "class_id": 3, "course_id": 1},
        resumed,
        conn=business,
        caller_idempotency_key="recovery-write-once",
    )
    assert replay.ok and replay.meta["idempotent_replay"] is True
    assert business.execute(
        "SELECT COUNT(*) FROM exams WHERE exam_name='恢复考试'"
    ).fetchone()[0] == 1
    business.close()


def test_compaction_and_new_message_keep_tool_pair_atomic(tmp_path):
    store_a = StateStore(tmp_path / "state.db")
    store_b = StateStore(tmp_path / "state.db")
    context = _queued(store_a, "shared", "run-1")
    _claim(store_a, context, "instance-a")
    messages = [
        {"role": "user", "content": "old"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "query", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "name": "query", "content": "{}"},
        {"role": "assistant", "content": "done"},
    ]
    store_a.append_messages("shared", messages, context=context)
    barrier = threading.Barrier(2)
    errors = []

    def compact():
        barrier.wait()
        try:
            store_a.compact_messages(
                "shared",
                summary="checkpoint",
                message_count=3,
                estimated_tokens_before=100,
                active_message_count=4,
                context=context,
            )
        except RuntimeError:
            pass
        except Exception as error:
            errors.append(error)

    def append():
        barrier.wait()
        try:
            store_b.append_messages(
                "shared",
                [{"role": "user", "content": "new"}],
                context=context,
            )
        except Exception as error:
            errors.append(error)

    first = threading.Thread(target=compact)
    second = threading.Thread(target=append)
    first.start()
    second.start()
    first.join(3)
    second.join(3)

    assert errors == []
    all_messages = store_a.get_messages("shared", include_compacted=True)
    assert all_messages == [*messages, {"role": "user", "content": "new"}]
    assert all_messages[1].get("tool_calls") and all_messages[2]["role"] == "tool"


def test_cross_tenant_cannot_cancel_or_observe_run(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _queued(store, "shared", "run-1")
    _claim(store, context, "instance-a")

    with pytest.raises(PermissionError):
        store.cancel_run("run-1", actor_id="intruder", tenant_id="school-1")
    with pytest.raises(PermissionError):
        store.get_run_status("run-1", actor_id="teacher-1", tenant_id="other-school")
    with pytest.raises(PermissionError):
        store.get_session_status(
            "shared",
            actor_id="intruder",
            tenant_id="school-1",
        )
