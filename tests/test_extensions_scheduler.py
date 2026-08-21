from __future__ import annotations

from datetime import UTC, datetime, timedelta
import threading
import time

import pytest

from edu_agent.engine.base import Engine, EngineResponse
from edu_agent.engine.resilient import FailureKind, ResilientEngine, classify_failure
from edu_agent.extensions import PluginManager
from edu_agent.scheduler import JobStore, Scheduler
from edu_agent.state import StateStore


class FakeRegistry:
    def __init__(self):
        self.registered = []

    def register_tool(self, **kwargs):
        self.registered.append(kwargs)


class ExamplePlugin:
    @staticmethod
    def register(context):
        context.register_tool(
            name="school_calendar",
            schema={
                "name": "school_calendar",
                "description": "查询校历",
                "parameters": {"type": "object", "properties": {}},
            },
            handler=lambda conn, **kwargs: {"term": "2026-fall"},
            category="query",
        )


def test_plugin_manager_registers_without_core_edits():
    fake_registry = FakeRegistry()
    manager = PluginManager(registry_module=fake_registry)
    manager.load("example", ExamplePlugin)
    assert manager.loaded == ["example"]
    assert fake_registry.registered[0]["name"] == "school_calendar"


def test_registry_rejects_generic_mutating_plugin():
    from edu_agent.tools import registry

    with pytest.raises(ValueError, match="受控事务适配器"):
        registry.register_tool(
            name="unsafe_write_plugin",
            schema={
                "name": "unsafe_write_plugin",
                "description": "不安全写插件",
                "parameters": {"type": "object", "properties": {}},
            },
            handler=lambda connection: {"ok": True},
            category="operation",
            mutating=True,
        )


def test_scheduler_claims_once_and_persists_result(tmp_path):
    state = StateStore(tmp_path / "state.db")
    jobs = JobStore(state)
    now = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)
    job_id = jobs.create(
        actor_id="teacher-1",
        tenant_id="school-1",
        role="teacher",
        name="周报",
        prompt="生成三班周报",
        next_run_at=now - timedelta(seconds=1),
    )
    calls = []
    first = Scheduler(state, lambda job: calls.append(job["id"]) or "已生成", worker_id="w1")
    second = Scheduler(state, lambda job: "不应执行", worker_id="w2")

    assert first.tick(now=now) == [{"job_id": job_id, "status": "success", "result": "已生成"}]
    assert second.tick(now=now) == []
    assert calls == [job_id]
    with state.connect() as connection:
        row = connection.execute(
            "SELECT enabled, last_status, last_result FROM scheduled_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    assert dict(row) == {"enabled": 0, "last_status": "success", "last_result": "已生成"}


def test_scheduler_reschedules_interval_and_records_failure(tmp_path):
    state = StateStore(tmp_path / "state.db")
    now = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)
    job_id = JobStore(state).create(
        actor_id="teacher-1",
        tenant_id="school-1",
        role="teacher",
        name="风险扫描",
        prompt="扫描学困风险",
        next_run_at=now,
        interval_seconds=3600,
    )

    def fail(job):
        raise RuntimeError("模型端点不可用")

    result = Scheduler(state, fail, worker_id="w1").tick(now=now)
    assert result[0]["status"] == "retry_wait"
    with state.connect() as connection:
        row = connection.execute(
            "SELECT enabled, next_run_at, last_status, last_error FROM scheduled_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    assert row["enabled"] == 1
    assert row["last_status"] == "failed"
    assert "模型端点不可用" in row["last_error"]
    assert datetime.fromisoformat(row["next_run_at"]) == now + timedelta(minutes=1)


class APIConnectionError(Exception):
    pass


class FlakyEngine(Engine):
    name = "flaky"

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            raise APIConnectionError("temporary")
        return EngineResponse(content="ok")


def test_resilient_engine_retries_only_retryable_errors():
    engine = FlakyEngine()
    sleeps = []
    response = ResilientEngine(engine, max_retries=2, sleeper=sleeps.append).chat([], [])
    assert response.content == "ok"
    assert response.usage["runtime_attempts"] == 2
    assert sleeps == [1]


class BrokenEngine(Engine):
    name = "broken"

    def chat(self, messages, tools):
        raise ValueError("bad request")


def test_resilient_engine_does_not_retry_non_retryable_errors():
    with pytest.raises(ValueError, match="bad request"):
        ResilientEngine(BrokenEngine(), max_retries=3, sleeper=lambda _: None).chat([], [])


class AlwaysUnavailable(Engine):
    name = "primary"

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools):
        self.calls += 1
        raise APIConnectionError("offline")


class FallbackEngine(Engine):
    name = "fallback"

    def chat(self, messages, tools):
        return EngineResponse(content="fallback-ok")


def test_resilient_engine_opens_circuit_and_skips_primary():
    primary = AlwaysUnavailable()
    events = []
    engine = ResilientEngine(
        primary,
        fallback=FallbackEngine(),
        max_retries=0,
        failure_threshold=1,
        cooldown_seconds=60,
        event_sink=events.append,
    )

    assert engine.chat([], []).content == "fallback-ok"
    assert engine.chat([], []).content == "fallback-ok"
    assert primary.calls == 1
    assert [event["event"] for event in events] == [
        "provider_failure",
        "circuit_opened",
        "fallback_activated",
        "primary_skipped",
        "fallback_activated",
    ]


def test_failure_classifier_distinguishes_context_and_auth_errors():
    class AuthenticationError(Exception):
        pass

    assert classify_failure(AuthenticationError("bad key")).kind == FailureKind.AUTHENTICATION
    context = classify_failure(ValueError("maximum context window length exceeded"))
    assert context.kind == FailureKind.CONTEXT_OVERFLOW
    assert context.retryable is False


def test_scheduler_idempotency_retry_dead_letter_and_cancel(tmp_path):
    state = StateStore(tmp_path / "state.db")
    jobs = JobStore(state)
    now = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)
    values = dict(
        actor_id="teacher-1",
        tenant_id="school-1",
        role="teacher",
        name="风险扫描",
        prompt="扫描学困风险",
        next_run_at=now,
        max_attempts=2,
        retry_backoff_seconds=10,
        idempotency_key="risk-scan-2026-08-17",
    )
    job_id = jobs.create(**values)
    assert jobs.create(**values) == job_id
    with pytest.raises(ValueError, match="不同的计划任务"):
        jobs.create(**{**values, "prompt": "不同任务"})

    failing = Scheduler(
        state,
        lambda job: (_ for _ in ()).throw(RuntimeError("upstream down")),
        worker_id="w1",
    )
    first = failing.tick(now=now)
    assert first[0]["status"] == "retry_wait"
    assert failing.tick(now=now + timedelta(seconds=9)) == []
    second = failing.tick(now=now + timedelta(seconds=10))
    assert second[0]["status"] == "dead_letter"

    cancel_id = jobs.create(
        actor_id="teacher-1",
        tenant_id="school-1",
        role="teacher",
        name="取消任务",
        prompt="不应执行",
        next_run_at=now,
    )
    assert jobs.cancel(cancel_id, actor_id="other", tenant_id="school-1") is False
    assert jobs.cancel(cancel_id, actor_id="teacher-1", tenant_id="school-1") is True
    assert Scheduler(state, lambda job: "不应执行", worker_id="w2").tick(now=now) == []


def test_scheduler_heartbeat_extends_only_current_lease(tmp_path):
    state = StateStore(tmp_path / "state.db")
    jobs = JobStore(state)
    now = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)
    job_id = jobs.create(
        actor_id="teacher-1",
        tenant_id="school-1",
        role="teacher",
        name="长任务",
        prompt="生成报告",
        next_run_at=now,
    )
    assert jobs.claim_due(worker_id="w1", now=now, lease_seconds=5)[0]["id"] == job_id
    assert jobs.heartbeat(job_id, worker_id="w2", now=now) is False
    assert jobs.heartbeat(job_id, worker_id="w1", now=now, lease_seconds=30) is True
    assert jobs.claim_due(worker_id="w2", now=now + timedelta(seconds=6)) == []


def test_scheduler_renews_lease_while_runner_is_active(tmp_path):
    state = StateStore(tmp_path / "state.db")
    jobs = JobStore(state)
    now = datetime.now(UTC)
    jobs.create(
        actor_id="teacher-1",
        tenant_id="school-1",
        role="teacher",
        name="慢任务",
        prompt="生成长报告",
        next_run_at=now,
    )
    entered = threading.Event()
    release = threading.Event()

    def slow(job):
        entered.set()
        release.wait(2)
        return "done"

    first = Scheduler(state, slow, worker_id="w1", lease_seconds=1)
    thread = threading.Thread(target=first.tick)
    thread.start()
    assert entered.wait(1)
    time.sleep(1.2)
    assert Scheduler(state, lambda job: "duplicate", worker_id="w2").tick() == []
    release.set()
    thread.join(2)
