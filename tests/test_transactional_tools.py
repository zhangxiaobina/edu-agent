from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from edu_agent.data import db, generate
from edu_agent.planning.runtime import PlanCoordinator, PlanningOptions
from edu_agent.planning.verifier import EvidenceVerifier
from edu_agent.runtime.models import RunContext
from edu_agent.runtime.tool_executor import ExecutionPolicy, PolicyToolExecutor
from edu_agent.runtime.transactions import (
    IdempotentConsumer,
    InjectedFault,
    NamedFaultInjector,
    OutboxWorker,
    TransactionalToolRuntime,
    initialize_transaction_schema,
)
from edu_agent.state import StateStore
from edu_agent.tools import registry


def _context(**overrides) -> RunContext:
    values = {
        "session_id": "session-1",
        "actor_id": "teacher-1",
        "tenant_id": "school-1",
        "role": "admin",
        "course_ids": {1},
    }
    values.update(overrides)
    return RunContext.create(**values)


@pytest.fixture
def data_path(tmp_path):
    path = tmp_path / "edu.db"
    generate.build(seed=42, out_path=path)
    return path


def _executor(store, *, faults=None, approval=True):
    return PolicyToolExecutor(
        registry,
        policy=ExecutionPolicy(require_write_approval=True),
        approval_handler=lambda request: approval,
        state_store=store,
        transaction_runtime=TransactionalToolRuntime(
            state_store=store,
            fault_injector=faults,
        ),
    )


def _exam_args(name="事务考试"):
    return {"exam_name": name, "class_id": 3, "course_id": 1}


def test_after_approval_before_business_crash_replays_without_duplicate(data_path, tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context()
    connection = db.connect(data_path)
    try:
        failed = _executor(
            store,
            faults=NamedFaultInjector("after_approval_before_business"),
        ).execute(
            "create_exam",
            _exam_args(),
            context,
            conn=connection,
            tool_call_id="call-create",
            caller_idempotency_key="exam:fall-2026",
        )
        assert failed.error["code"] == "TOOL_EXCEPTION"
        assert "after_approval_before_business" in failed.error["message"]
        assert connection.execute(
            "SELECT COUNT(*) FROM exams WHERE exam_name='事务考试'"
        ).fetchone()[0] == 0

        replayed = _executor(store).execute(
            "create_exam",
            _exam_args(),
            context,
            conn=connection,
            tool_call_id="call-create",
            caller_idempotency_key="exam:fall-2026",
        )
        assert replayed.ok is True
        assert connection.execute(
            "SELECT COUNT(*) FROM exams WHERE exam_name='事务考试'"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_business_write_before_commit_is_rolled_back_and_recoverable(data_path, tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context()
    connection = db.connect(data_path)
    try:
        failed = _executor(
            store,
            faults=NamedFaultInjector("after_business_write_before_operation_commit"),
        ).execute(
            "assign_homework",
            {
                "title": "事务作业",
                "course_id": 1,
                "class_ids": [3],
                "end_time": "2026-09-01T20:00:00+08:00",
            },
            context,
            conn=connection,
            tool_call_id="call-homework",
            caller_idempotency_key="homework:week-1",
        )
        assert failed.error["code"] == "TOOL_EXCEPTION"
        assert connection.execute(
            "SELECT COUNT(*) FROM homeworks WHERE title='事务作业'"
        ).fetchone()[0] == 0
        row = connection.execute(
            "SELECT status FROM tool_operations WHERE id=?",
            (failed.meta["operation_id"],),
        ).fetchone()
        assert row["status"] == "failed"
    finally:
        connection.close()


def test_committed_before_publish_replay_returns_original_result(data_path, tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context()
    connection = db.connect(data_path)
    try:
        failed = _executor(
            store,
            faults=NamedFaultInjector("after_operation_commit_before_outbox_publish"),
        ).execute(
            "create_exam",
            _exam_args(),
            context,
            conn=connection,
            tool_call_id="call-create",
            caller_idempotency_key="exam:publish-gap",
        )
        assert failed.error["code"] == "TOOL_EXCEPTION"
        assert connection.execute(
            "SELECT COUNT(*) FROM exams WHERE exam_name='事务考试'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT status FROM tool_operations WHERE id=?", (failed.meta["operation_id"],)
        ).fetchone()[0] == "committed"
        assert connection.execute("SELECT COUNT(*) FROM tool_outbox").fetchone()[0] == 1

        replayed = _executor(store).execute(
            "create_exam",
            _exam_args(),
            context,
            conn=connection,
            tool_call_id="call-create-retry",
            caller_idempotency_key="exam:publish-gap",
        )
        assert replayed.ok is True
        assert replayed.meta["idempotent_replay"] is True
        assert replayed.data["exam_id"] == connection.execute(
            "SELECT id FROM exams WHERE exam_name='事务考试'"
        ).fetchone()[0]
    finally:
        connection.close()


def test_same_key_different_payload_conflicts(data_path, tmp_path):
    store = StateStore(tmp_path / "state.db")
    connection = db.connect(data_path)
    try:
        first = _executor(store).execute(
            "create_exam",
            _exam_args("第一次"),
            _context(),
            conn=connection,
            caller_idempotency_key="business-key",
        )
        conflict = _executor(store).execute(
            "create_exam",
            _exam_args("参数已变化"),
            _context(session_id="session-2"),
            conn=connection,
            caller_idempotency_key="business-key",
        )
        assert first.ok is True
        assert conflict.error["code"] == "IDEMPOTENCY_CONFLICT"
        assert connection.execute(
            "SELECT COUNT(*) FROM exams WHERE exam_name IN ('第一次','参数已变化')"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_outbox_publish_ack_crash_is_at_least_once_and_consumer_deduplicates(
    data_path, tmp_path
):
    store = StateStore(tmp_path / "state.db")
    connection = db.connect(data_path)
    outcome = _executor(store).execute(
        "create_exam",
        _exam_args(),
        _context(),
        conn=connection,
        caller_idempotency_key="outbox-replay",
    )
    connection.close()
    published = []
    consumed = []

    def publish(event):
        published.append(event["event_id"])
        consumer_connection = db.connect(data_path)
        try:
            IdempotentConsumer.consume(
                consumer_connection,
                consumer_name="notifications",
                event=event,
                handler=lambda payload: consumed.append(payload["event_id"]),
            )
        finally:
            consumer_connection.close()

    worker = OutboxWorker(
        lambda: db.connect(data_path),
        worker_id="publisher-1",
        lease_seconds=0,
        fault_injector=NamedFaultInjector("after_outbox_publish_before_ack"),
    )
    with pytest.raises(InjectedFault):
        worker.run_once(publish)
    replay = OutboxWorker(
        lambda: db.connect(data_path), worker_id="publisher-2", lease_seconds=30
    ).run_once(publish)

    assert outcome.ok is True
    assert len(published) == 2
    assert published[0] == published[1]
    assert consumed == [published[0]]
    assert replay[0]["status"] == "published"


def test_two_workers_execute_same_key_once(data_path, tmp_path):
    store = StateStore(tmp_path / "state.db")
    barrier = threading.Barrier(2)

    def invoke(index):
        connection = db.connect(data_path)
        barrier.wait()
        try:
            return _executor(store).execute(
                "create_exam",
                _exam_args(),
                _context(session_id=f"worker-{index}"),
                conn=connection,
                tool_call_id=f"call-{index}",
                caller_idempotency_key="concurrent-exam",
            )
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(invoke, range(2)))
    connection = db.connect(data_path)
    try:
        assert all(outcome.ok for outcome in outcomes)
        assert sum(outcome.meta["idempotent_replay"] for outcome in outcomes) == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM exams WHERE exam_name='事务考试'"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_compensation_failure_resumes_and_owner_scope_is_enforced(data_path, tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context()
    connection = db.connect(data_path)
    outcome = _executor(store).execute(
        "assign_homework",
        {
            "title": "可补偿作业",
            "course_id": 1,
            "class_ids": [3],
            "end_time": "2026-09-01T20:00:00+08:00",
        },
        context,
        conn=connection,
        caller_idempotency_key="compensate-homework",
    )
    operation_id = outcome.meta["operation_id"]
    other = _context(actor_id="other-teacher")
    runtime = TransactionalToolRuntime(state_store=store)
    with pytest.raises(PermissionError):
        runtime.get_operation(connection, operation_id, context=other)
    with pytest.raises(PermissionError):
        runtime.get_compensation_snapshot(connection, operation_id, context=other)
    with pytest.raises(PermissionError):
        runtime.compensate(connection, operation_id, context=other)

    failing = TransactionalToolRuntime(
        state_store=store,
        fault_injector=NamedFaultInjector("during_compensation"),
    )
    with pytest.raises(InjectedFault):
        failing.compensate(connection, operation_id, context=context)
    assert runtime.get_operation(connection, operation_id, context=context)["status"] == "compensating"
    recovered = runtime.compensate(connection, operation_id, context=context)
    assert recovered["status"] == "compensated"
    assert recovered["compensation_attempts"] == 2
    assert connection.execute(
        "SELECT COUNT(*) FROM homeworks WHERE title='可补偿作业'"
    ).fetchone()[0] == 0
    connection.close()


def test_unsafe_exam_compensation_enters_manual_review(data_path, tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context()
    connection = db.connect(data_path)
    outcome = _executor(store).execute(
        "create_exam",
        _exam_args(),
        context,
        conn=connection,
        caller_idempotency_key="unsafe-exam",
    )
    exam_id = outcome.data["exam_id"]
    student_id = connection.execute("SELECT id FROM students ORDER BY id LIMIT 1").fetchone()[0]
    record_id = connection.execute("SELECT COALESCE(MAX(id),0)+1 FROM exam_records").fetchone()[0]
    connection.execute(
        """
        INSERT INTO exam_records(id, exam_id, student_id, status)
        VALUES (?, ?, ?, 0)
        """,
        (record_id, exam_id, student_id),
    )
    connection.commit()
    operation = TransactionalToolRuntime(state_store=store).compensate(
        connection, outcome.meta["operation_id"], context=context
    )
    assert operation["status"] == "manual_review"
    assert connection.execute("SELECT 1 FROM exams WHERE id=?", (exam_id,)).fetchone()
    connection.close()


def test_plan_evidence_accepts_only_committed_operation(data_path, tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context()
    spec = {
        "goal": "创建考试",
        "steps": [
            {
                "id": "create",
                "goal": "创建考试",
                "depends_on": [],
                "allowed_tools": ["create_exam"],
                "expected_tools": ["create_exam"],
                "completion_conditions": [{"kind": "tool_success", "tool": "create_exam"}],
            }
        ],
    }
    record = store.create_plan(
        run_id=context.run_id,
        session_id=context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        spec=spec,
        max_iterations=4,
    )
    coordinator = PlanCoordinator(store, context, options=PlanningOptions())
    step = coordinator.active_or_ready_step()
    connection = db.connect(data_path)
    outcome = _executor(store).execute(
        "create_exam",
        _exam_args(),
        context,
        conn=connection,
        tool_call_id="call-plan",
        plan_step_id=step.id,
        caller_idempotency_key="plan-exam",
    )
    verification = EvidenceVerifier(store, context, max_step_retries=2).verify_step(
        record["id"], step
    )
    assert verification.completed is True
    TransactionalToolRuntime(state_store=store).compensate(
        connection, outcome.meta["operation_id"], context=context
    )
    evidence = store.get_step_evidence(
        record["id"], step.id, actor_id=context.actor_id, tenant_id=context.tenant_id
    )
    assert evidence[-1]["status"] == "rejected"
    assert evidence[-1]["failure_reason"] == "OPERATION_COMPENSATED"
    connection.close()


def test_scheduler_replay_scope_stays_stable_across_runs(data_path, tmp_path):
    store = StateStore(tmp_path / "state.db")
    first = _executor(store).execute(
        "create_exam",
        _exam_args(),
        _context(session_id="scheduler-run-1", replay_scope="scheduled-job:j1:e1"),
        conn=db.connect(data_path),
        tool_call_id="model-call-a",
    )
    second_connection = db.connect(data_path)
    second = _executor(store).execute(
        "create_exam",
        _exam_args(),
        _context(session_id="scheduler-run-2", replay_scope="scheduled-job:j1:e1"),
        conn=second_connection,
        tool_call_id="model-call-b",
    )
    assert first.ok and second.ok
    assert second.meta["idempotent_replay"] is True
    assert second_connection.execute(
        "SELECT COUNT(*) FROM exams WHERE exam_name='事务考试'"
    ).fetchone()[0] == 1
    second_connection.close()


def test_old_business_database_is_migrated_in_place(tmp_path):
    path = tmp_path / "legacy-business.db"
    connection = db.connect(path)
    connection.execute("CREATE TABLE legacy_rows(id INTEGER PRIMARY KEY, value TEXT)")
    connection.execute("INSERT INTO legacy_rows(value) VALUES ('keep-me')")
    connection.commit()
    initialize_transaction_schema(connection)
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert connection.execute("SELECT value FROM legacy_rows").fetchone()[0] == "keep-me"
    assert {
        "tool_operations",
        "tool_approvals",
        "tool_outbox",
        "tool_consumer_events",
    } <= tables
    connection.close()


def test_approval_is_bound_to_hash_scope_expiry_and_approver(data_path, tmp_path):
    store = StateStore(tmp_path / "state.db")
    requests = []
    connection = db.connect(data_path)
    executor = PolicyToolExecutor(
        registry,
        policy=ExecutionPolicy(require_write_approval=True, approval_ttl_seconds=60),
        approval_handler=lambda request: requests.append(request) or True,
        state_store=store,
    )
    outcome = executor.execute(
        "create_exam",
        _exam_args(),
        _context(),
        conn=connection,
        caller_idempotency_key="approval-binding",
    )
    approval = connection.execute(
        "SELECT * FROM tool_approvals WHERE operation_id=?",
        (outcome.meta["operation_id"],),
    ).fetchone()
    assert outcome.ok is True
    assert approval["payload_hash"] == requests[0].payload_hash
    assert approval["scope"] == requests[0].scope
    assert approval["approver_id"] == "teacher-1"
    assert approval["expires_at"] == requests[0].expires_at
    connection.close()
