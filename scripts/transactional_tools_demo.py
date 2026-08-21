"""阶段 3 离线演示：Scheduler 幂等写、outbox 重放去重和补偿。"""
from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from edu_agent.data import db, generate
from edu_agent.runtime.models import RunContext
from edu_agent.runtime.tool_executor import ExecutionPolicy, PolicyToolExecutor
from edu_agent.runtime.transactions import IdempotentConsumer, OutboxWorker, TransactionalToolRuntime
from edu_agent.scheduler import JobStore, Scheduler
from edu_agent.state import StateStore
from edu_agent.tools import registry


def main() -> None:
    directory = Path(tempfile.gettempdir())
    state_path = directory / "edu_agent_transaction_demo_state.db"
    data_path = directory / "edu_agent_transaction_demo_data.db"
    state_path.unlink(missing_ok=True)
    data_path.unlink(missing_ok=True)
    generate.build(seed=42, out_path=data_path)
    state = StateStore(state_path)
    jobs = JobStore(state)
    now = datetime.now(UTC)
    jobs.create(
        actor_id="teacher-demo",
        tenant_id="school-demo",
        role="teacher",
        name="秋季事务考试",
        prompt="创建秋季事务考试",
        next_run_at=now,
        max_attempts=2,
        retry_backoff_seconds=1,
        idempotency_key="fall-exam-job",
    )
    first_attempt = True
    operation_id = None

    def run_job(job: dict) -> str:
        nonlocal first_attempt, operation_id
        context = RunContext.create(
            session_id=f"scheduler-{job['attempt_count']}",
            actor_id=job["actor_id"],
            tenant_id=job["tenant_id"],
            role=job["role"],
            course_ids={1},
            replay_scope=f"scheduled-job:{job['id']}:{job['execution_key']}",
        )
        connection = db.connect(data_path)
        try:
            outcome = PolicyToolExecutor(
                registry,
                policy=ExecutionPolicy(require_write_approval=False),
                state_store=state,
            ).execute(
                "create_exam",
                {"exam_name": "秋季事务考试", "class_id": 3, "course_id": 1},
                context,
                conn=connection,
                tool_call_id=f"model-call-{job['attempt_count']}",
            )
            if not outcome.ok:
                raise RuntimeError(outcome.error)
            operation_id = outcome.meta["operation_id"]
            if first_attempt:
                first_attempt = False
                raise RuntimeError("模拟写入成功后 Scheduler 进程崩溃")
            return f"exam={outcome.data['exam_id']} replay={outcome.meta['idempotent_replay']}"
        finally:
            connection.close()

    scheduler = Scheduler(state, run_job, worker_id="transaction-demo", lease_seconds=30)
    first = scheduler.tick(now=now)
    second = scheduler.tick(now=now + timedelta(seconds=1))
    verify = db.connect(data_path)
    exam_count = verify.execute(
        "SELECT COUNT(*) FROM exams WHERE exam_name='秋季事务考试'"
    ).fetchone()[0]
    verify.close()

    published = []
    consumed = []

    def publish(event: dict) -> None:
        published.append(event["event_id"])
        consumer_connection = db.connect(data_path)
        try:
            IdempotentConsumer.consume(
                consumer_connection,
                consumer_name="demo-consumer",
                event=event,
                handler=lambda payload: consumed.append(payload["event_id"]),
            )
        finally:
            consumer_connection.close()

    worker = OutboxWorker(lambda: db.connect(data_path), worker_id="outbox-demo")
    worker.run_once(publish)
    event = published[0]
    duplicate_connection = db.connect(data_path)
    try:
        IdempotentConsumer.consume(
            duplicate_connection,
            consumer_name="demo-consumer",
            event={"event_id": event},
            handler=lambda payload: consumed.append(payload["event_id"]),
        )
    finally:
        duplicate_connection.close()

    homework_connection = db.connect(data_path)
    homework_context = RunContext.create(
        session_id="compensation-demo",
        actor_id="teacher-demo",
        tenant_id="school-demo",
        role="teacher",
        course_ids={1},
    )
    homework = PolicyToolExecutor(
        registry,
        policy=ExecutionPolicy(require_write_approval=False),
        state_store=state,
    ).execute(
        "assign_homework",
        {
            "title": "可补偿事务作业",
            "course_id": 1,
            "class_ids": [3],
            "end_time": "2026-09-01T20:00:00+08:00",
        },
        homework_context,
        conn=homework_connection,
        caller_idempotency_key="demo-homework",
    )
    compensated = TransactionalToolRuntime(state_store=state).compensate(
        homework_connection,
        homework.meta["operation_id"],
        context=homework_context,
    )
    homework_count = homework_connection.execute(
        "SELECT COUNT(*) FROM homeworks WHERE title='可补偿事务作业'"
    ).fetchone()[0]
    homework_connection.close()

    print(f"scheduler: {first[0]['status']} -> {second[0]['status']}")
    print(f"scheduler operation: {operation_id}; exam side effects: {exam_count}")
    print(f"outbox deliveries: {len(published)}; consumer side effects: {len(consumed)}")
    print(f"compensation: {compensated['status']}; homework side effects: {homework_count}")
    assert first[0]["status"] == "retry_wait"
    assert second[0]["status"] == "success"
    assert exam_count == 1
    assert len(consumed) == 1
    assert compensated["status"] == "compensated"
    assert homework_count == 0


if __name__ == "__main__":
    main()
