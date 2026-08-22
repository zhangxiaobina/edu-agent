from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from edu_agent.data import db, generate
from edu_agent.engine.base import Engine, EngineResponse, ToolCall
from edu_agent.observability import (
    RunEventBus,
    RunEventType,
    RunEventWriterRejected,
    RunStreamWriter,
)
from edu_agent.runtime.cancellation import CancellationToken
from edu_agent.runtime.config import (
    AppConfig,
    MemoryConfig,
    PlanningConfig,
    RuntimeConfig,
    SecurityConfig,
    StorageConfig,
)
from edu_agent.runtime.manager import RuntimeManager
from edu_agent.runtime.models import RunContext
from edu_agent.runtime.recovery import (
    RecoveryAction,
    RecoveryManualReviewRequired,
    STABLE_CURSOR_DECISION_TABLE,
)
from edu_agent.runtime.transactions import (
    ProcessCrashFaultInjector,
    SimulatedProcessCrash,
    TransactionalToolRuntime,
    approval_scope,
    idempotency_key,
    payload_hash,
)
from edu_agent.service import EduAgentService
from edu_agent.state import RunStableBoundary, StateStore
from edu_agent.tools import registry
from edu_agent.tools.registry import ToolSpec


ACTOR = "teacher-r2"
TENANT = "school-r2"
SESSION = "session-r2"
RUN = "run-r2"


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 8, 22, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class ReadProvider:
    def __init__(self, calls: list[str]):
        self.calls = calls
        self.spec = ToolSpec(
            schema={
                "name": "read_once",
                "description": "Read one deterministic value.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
            handler=lambda connection, **arguments: {},
            category="query",
        )

    def openai_tools(self, **kwargs):
        return [{"type": "function", "function": self.spec.schema}]

    def get_spec(self, name):
        return self.spec if name == "read_once" else None

    def dispatch(self, name, arguments, conn=None):
        self.calls.append(name)
        return {"value": len(self.calls)}


class OneToolEngine(Engine):
    name = "r2-recovery-tool-engine"

    def __init__(self, *, write: bool = False):
        self.write = write
        self.calls = 0

    def chat(self, messages, tools):
        self.calls += 1
        call_id = "write-call" if self.write else "read-call"
        if any(message.get("tool_call_id") == call_id for message in messages):
            return EngineResponse(content="recovered-answer")
        if self.write:
            return EngineResponse(
                tool_calls=[
                    ToolCall(
                        call_id,
                        "create_exam",
                        {
                            "exam_name": "r2-recovery-exam",
                            "class_id": 3,
                            "course_id": 1,
                        },
                    )
                ]
            )
        return EngineResponse(tool_calls=[ToolCall(call_id, "read_once", {})])


class FinalAnswerEngine(Engine):
    name = "r2-recovery-final-engine"

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools):
        self.calls += 1
        return EngineResponse(content="final-before-crash")


def _config(tmp_path) -> AppConfig:
    return AppConfig(
        runtime=RuntimeConfig(
            max_model_calls=8,
            max_tool_calls=8,
            compression_enabled=False,
            session_lease_seconds=0.2,
            session_heartbeat_seconds=0.05,
            run_stall_seconds=0.4,
        ),
        planning=PlanningConfig(enabled=False),
        memory=MemoryConfig(enabled=False),
        security=SecurityConfig(
            require_write_approval=False,
            default_role="admin",
        ),
        storage=StorageConfig(
            state_path=str(tmp_path / "state.db"),
            artifact_path=str(tmp_path / "artifacts"),
        ),
    )


def _service(
    tmp_path,
    clock,
    engine,
    provider,
    *,
    owner: str,
    fault_point: str | None = None,
) -> EduAgentService:
    store = StateStore(tmp_path / "state.db", clock=clock)
    fault = ProcessCrashFaultInjector(fault_point) if fault_point else None
    return EduAgentService(
        engine,
        config=_config(tmp_path),
        state_store=store,
        tools_provider=provider,
        runtime_manager=RuntimeManager(
            store,
            owner_id=owner,
            lease_seconds=0.2,
            heartbeat_seconds=0.05,
        ),
        loop_fault_injector=(
            fault
            if fault_point != "after_finalizer_final_message_committed"
            else None
        ),
        finalizer_fault_injector=(
            fault
            if fault_point == "after_finalizer_final_message_committed"
            else None
        ),
    )


@pytest.mark.parametrize(
    (
        "fault_point",
        "kind",
        "expected_action",
        "expected_boundary",
        "expected_read_calls",
    ),
    [
        (
            "after_model_response",
            "read",
            RecoveryAction.CONTINUE,
            RunStableBoundary.MODEL_ATTEMPT_STARTED,
            1,
        ),
        (
            "after_assistant_envelope_commit",
            "read",
            RecoveryAction.REPLAY_READ,
            RunStableBoundary.ASSISTANT_ENVELOPE_COMMITTED,
            1,
        ),
        (
            "after_read_tool_result_commit",
            "read",
            RecoveryAction.CONTINUE,
            RunStableBoundary.TOOL_RESULT_COMMITTED,
            1,
        ),
        (
            "after_write_operation_commit_before_result",
            "write",
            RecoveryAction.REUSE_OPERATION,
            RunStableBoundary.ASSISTANT_ENVELOPE_COMMITTED,
            0,
        ),
        (
            "after_finalizer_final_message_committed",
            "final",
            RecoveryAction.CONTINUE,
            RunStableBoundary.FINAL_MESSAGE_COMMITTED,
            0,
        ),
    ],
)
def test_five_process_reopen_crash_windows(
    tmp_path,
    fault_point,
    kind,
    expected_action,
    expected_boundary,
    expected_read_calls,
):
    clock = MutableClock()
    read_calls: list[str] = []
    business_path = tmp_path / "edu.db"
    if kind == "write":
        generate.build(seed=42, out_path=business_path)
    first_engine = FinalAnswerEngine() if kind == "final" else OneToolEngine(
        write=kind == "write"
    )
    first_provider = registry if kind == "write" else ReadProvider(read_calls)
    first = _service(
        tmp_path,
        clock,
        first_engine,
        first_provider,
        owner="worker-before-crash",
        fault_point=fault_point,
    )
    first_connection = db.connect(business_path) if kind == "write" else None
    try:
        with pytest.raises(SimulatedProcessCrash, match=fault_point):
            first.chat(
                "recover this turn",
                actor_id=ACTOR,
                tenant_id=TENANT,
                role="admin",
                course_ids={1},
                session_id=SESSION,
                run_id=RUN,
                db_conn=first_connection,
            )
    finally:
        if first_connection is not None:
            first_connection.close()
        first.close()

    crashed = StateStore(tmp_path / "state.db", clock=clock)
    crash_snapshot = crashed.get_run_journal_snapshot(
        RUN,
        session_id=SESSION,
        actor_id=ACTOR,
        tenant_id=TENANT,
    )
    assert crash_snapshot.stable_boundary is expected_boundary
    old_fence = crash_snapshot.fencing_token
    if kind == "write":
        with db.connect(business_path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM exams WHERE exam_name='r2-recovery-exam'"
            ).fetchone()[0] == 1

    clock.advance(1)
    recovered_engine = FinalAnswerEngine() if kind == "final" else OneToolEngine(
        write=kind == "write"
    )
    recovered_provider = registry if kind == "write" else ReadProvider(read_calls)
    recovered = _service(
        tmp_path,
        clock,
        recovered_engine,
        recovered_provider,
        owner="worker-after-reopen",
    )
    decision = recovered.get_recovery_decision(
        RUN,
        actor_id=ACTOR,
        tenant_id=TENANT,
    )
    assert decision.action is expected_action
    assert decision.stable_boundary == expected_boundary.value
    assert decision.tool_manifest_hash == crash_snapshot.tool_manifest_hash
    assert decision.frozen_provider_route == crash_snapshot.frozen_provider_route
    assert decision.budget_snapshot == crash_snapshot.budget_snapshot

    recovered_connection = db.connect(business_path) if kind == "write" else None
    try:
        result = recovered.resume_run(
            RUN,
            actor_id=ACTOR,
            tenant_id=TENANT,
            db_conn=recovered_connection,
        )
    finally:
        if recovered_connection is not None:
            recovered_connection.close()
    assert result.final_answer in {"recovered-answer", "final-before-crash"}
    assert len(read_calls) == expected_read_calls

    terminal = recovered.state_store.get_run_journal_snapshot(
        RUN,
        session_id=SESSION,
        actor_id=ACTOR,
        tenant_id=TENANT,
    )
    assert terminal.event_sequence > crash_snapshot.event_sequence
    assert terminal.loop_cursor >= crash_snapshot.loop_cursor
    assert terminal.fencing_token > old_fence
    assert terminal.tool_manifest_hash == crash_snapshot.tool_manifest_hash
    assert terminal.frozen_provider_route == crash_snapshot.frozen_provider_route
    assert terminal.budget_snapshot == result.budget
    assert result.budget["max_model_calls"] == 8
    assert result.budget["max_tool_calls"] == 8
    terminal_decision = recovered.get_recovery_decision(
        RUN,
        actor_id=ACTOR,
        tenant_id=TENANT,
    )
    assert terminal_decision.action is RecoveryAction.TERMINAL_REPLAY
    assert recovered.recover_chat_result(
        RUN,
        actor_id=ACTOR,
        tenant_id=TENANT,
    ) == result

    calls = recovered.state_store.list_tool_call_records(
        run_id=RUN,
        session_id=SESSION,
        actor_id=ACTOR,
        tenant_id=TENANT,
    )
    assert all(call["status"] == "completed" for call in calls)
    messages = recovered.state_store.get_run_messages(RUN)
    for call in calls:
        assert sum(
            message.get("role") == "tool"
            and message.get("tool_call_id") == call["tool_call_id"]
            for message in messages
        ) == 1
    with recovered.state_store.connect() as connection:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM messages
            WHERE run_id=? AND idempotency_key=? AND active=1
            """,
            (RUN, f"final-assistant:{RUN}"),
        ).fetchone()[0] == 1
    if kind == "write":
        with db.connect(business_path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM exams WHERE exam_name='r2-recovery-exam'"
            ).fetchone()[0] == 1
    trace = recovered.get_trace(actor_id=ACTOR, tenant_id=TENANT, limit=200)
    assert any(
        event["attributes"].get("action") == "run.recovery_decision"
        for event in trace["events"]
        if event["component"] == "security"
    )
    recovered.close()


def test_old_process_writer_is_fenced_and_recovery_sequence_is_monotonic(tmp_path):
    clock = MutableClock()
    path = tmp_path / "state.db"
    old_store = StateStore(path, clock=clock)
    context = RunContext.create(
        run_id=RUN,
        session_id=SESSION,
        actor_id=ACTOR,
        tenant_id=TENANT,
        role="admin",
    )
    old_store.ensure_session(
        SESSION,
        actor_id=ACTOR,
        tenant_id=TENANT,
        role="admin",
    )
    old_store.enqueue_run(context, request_text="writer fence")
    old_claim = old_store.acquire_session_lease(
        session_id=SESSION,
        run_id=RUN,
        owner_id="old-writer",
        actor_id=ACTOR,
        tenant_id=TENANT,
        lease_seconds=0.2,
    )
    old_bus = RunEventBus()
    old_writer = RunStreamWriter(
        old_bus,
        run_id=RUN,
        attempt=1,
        writer_id="api-old",
        cancellation_token=CancellationToken(),
        sequence_reserver=lambda **fields: old_store.reserve_run_event_sequence(
            actor_id=ACTOR,
            tenant_id=TENANT,
            **fields,
        ),
    )
    old_writer.bind(
        session_id=SESSION,
        fencing_token=int(old_claim["fencing_token"]),
        sequence_start=0,
    )
    first = old_writer.publish(RunEventType.RUN_PHASE, {"phase": "accepted"})

    clock.advance(1)
    new_store = StateStore(path, clock=clock)
    new_claim = new_store.acquire_session_lease(
        session_id=SESSION,
        run_id=RUN,
        owner_id="new-writer",
        actor_id=ACTOR,
        tenant_id=TENANT,
        lease_seconds=1,
    )
    new_bus = RunEventBus()
    new_writer = RunStreamWriter(
        new_bus,
        run_id=RUN,
        attempt=2,
        writer_id="api-new",
        cancellation_token=CancellationToken(),
        sequence_reserver=lambda **fields: new_store.reserve_run_event_sequence(
            actor_id=ACTOR,
            tenant_id=TENANT,
            **fields,
        ),
    )
    new_writer.bind(
        session_id=SESSION,
        fencing_token=int(new_claim["fencing_token"]),
        sequence_start=new_store.get_run_event_sequence(
            RUN,
            actor_id=ACTOR,
            tenant_id=TENANT,
        ),
    )
    second = new_writer.publish(
        RunEventType.RUN_PHASE,
        {"phase": "accepted", "status": "resumed"},
    )
    assert second.sequence > first.sequence
    assert int(new_claim["fencing_token"]) > int(old_claim["fencing_token"])
    with pytest.raises(RunEventWriterRejected):
        old_writer.publish(RunEventType.TEXT_DELTA, {"delta": "late"})


@pytest.mark.parametrize(
    ("operation_status", "expected_action"),
    [
        ("prepared", RecoveryAction.CONTINUE),
        ("executing", RecoveryAction.MANUAL_REVIEW),
    ],
)
def test_write_recovery_uses_existing_operation_state_contract(
    tmp_path,
    operation_status,
    expected_action,
):
    clock = MutableClock()
    business_path = tmp_path / "edu.db"
    generate.build(seed=42, out_path=business_path)
    first = _service(
        tmp_path,
        clock,
        OneToolEngine(write=True),
        registry,
        owner="operation-before-crash",
        fault_point="after_assistant_envelope_commit",
    )
    with db.connect(business_path) as connection:
        with pytest.raises(SimulatedProcessCrash):
            first.chat(
                "create through an operation",
                actor_id=ACTOR,
                tenant_id=TENANT,
                role="admin",
                course_ids={1},
                session_id=SESSION,
                run_id=RUN,
                db_conn=connection,
            )
    first.close()

    old_store = StateStore(tmp_path / "state.db", clock=clock)
    run = old_store.get_run_status(RUN, actor_id=ACTOR, tenant_id=TENANT)
    operation_context = RunContext.create(
        session_id=SESSION,
        run_id=RUN,
        actor_id=ACTOR,
        tenant_id=TENANT,
        role="admin",
    )
    operation_context.bind_runtime_control(
        lease_owner=run["owner_id"],
        fencing_token=int(run["fencing_token"]),
        control_check=lambda boundary: old_store.assert_run_writable(
            operation_context,
            boundary=boundary,
        ),
    )
    arguments = {
        "exam_name": "r2-recovery-exam",
        "class_id": 3,
        "course_id": 1,
    }
    runtime = TransactionalToolRuntime(state_store=old_store)
    with db.connect(business_path) as connection:
        operation = runtime.prepare(
            connection,
            key=idempotency_key(
                tenant_id=TENANT,
                actor_id=ACTOR,
                session_id=SESSION,
                run_id=RUN,
                plan_step_id=None,
                tool_call_id="write-call",
                tool_name="create_exam",
                arguments=arguments,
            ),
            digest=payload_hash("create_exam", arguments),
            tool_name="create_exam",
            arguments=arguments,
            context=operation_context,
            tool_call_id="write-call",
            plan_step_id=None,
            scope=approval_scope(
                tenant_id=TENANT,
                actor_id=ACTOR,
                tool_name="create_exam",
                arguments=arguments,
            ),
        )
        if operation_status == "executing":
            connection.execute(
                "UPDATE tool_operations SET status='executing' WHERE id=?",
                (operation["id"],),
            )
            connection.commit()
            operation = runtime.get_operation(
                connection,
                operation["id"],
                context=operation_context,
            )
            old_store.upsert_tool_operation_ref(
                operation,
                context=operation_context,
            )

    clock.advance(1)
    recovered = _service(
        tmp_path,
        clock,
        OneToolEngine(write=True),
        registry,
        owner="operation-after-reopen",
    )
    decision = recovered.get_recovery_decision(
        RUN,
        actor_id=ACTOR,
        tenant_id=TENANT,
    )
    assert decision.action is expected_action
    if operation_status == "prepared":
        with db.connect(business_path) as connection:
            result = recovered.resume_run(
                RUN,
                actor_id=ACTOR,
                tenant_id=TENANT,
                db_conn=connection,
            )
        assert result.final_answer == "recovered-answer"
        with db.connect(business_path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM exams WHERE exam_name='r2-recovery-exam'"
            ).fetchone()[0] == 1
    else:
        with pytest.raises(RecoveryManualReviewRequired):
            recovered.resume_run(
                RUN,
                actor_id=ACTOR,
                tenant_id=TENANT,
            )
        with db.connect(business_path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM exams WHERE exam_name='r2-recovery-exam'"
            ).fetchone()[0] == 0
    recovered.close()


def test_stable_cursor_decision_table_is_exhaustive():
    assert set(STABLE_CURSOR_DECISION_TABLE) == set(RunStableBoundary)
    assert STABLE_CURSOR_DECISION_TABLE[RunStableBoundary.TERMINAL] == {
        RecoveryAction.TERMINAL_REPLAY
    }
    assert RecoveryAction.REPLAY_READ in STABLE_CURSOR_DECISION_TABLE[
        RunStableBoundary.ASSISTANT_ENVELOPE_COMMITTED
    ]
    assert RecoveryAction.REUSE_OPERATION in STABLE_CURSOR_DECISION_TABLE[
        RunStableBoundary.TOOL_RESULT_COMMITTED
    ]


def test_incomplete_finalizer_with_invalid_budget_fails_closed(tmp_path):
    clock = MutableClock()
    first = _service(
        tmp_path,
        clock,
        FinalAnswerEngine(),
        ReadProvider([]),
        owner="invalid-budget-before-crash",
        fault_point="after_finalizer_final_message_committed",
    )
    with pytest.raises(SimulatedProcessCrash):
        first.chat(
            "persist final before corrupt budget",
            actor_id=ACTOR,
            tenant_id=TENANT,
            role="admin",
            session_id=SESSION,
            run_id=RUN,
        )
    first.close()

    with StateStore(tmp_path / "state.db", clock=clock).connect() as connection:
        connection.execute(
            "UPDATE run_journals SET budget_snapshot_json=? WHERE run_id=?",
            (
                '{"model_calls":9,"max_model_calls":8,'
                '"tool_calls":0,"max_tool_calls":8}',
                RUN,
            ),
        )

    clock.advance(1)
    recovered = _service(
        tmp_path,
        clock,
        FinalAnswerEngine(),
        ReadProvider([]),
        owner="invalid-budget-after-reopen",
    )
    decision = recovered.get_recovery_decision(
        RUN,
        actor_id=ACTOR,
        tenant_id=TENANT,
    )
    assert decision.action is RecoveryAction.MANUAL_REVIEW
    with pytest.raises(RecoveryManualReviewRequired):
        recovered.resume_run(
            RUN,
            actor_id=ACTOR,
            tenant_id=TENANT,
        )
    recovered.close()
