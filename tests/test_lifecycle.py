from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection
from pathlib import Path

import pytest

import edu_agent.service as service_module
from edu_agent.api import DemoTokenAuth, EduAgentApi, Principal, make_http_server
from edu_agent.engine.base import Engine, EngineResponse, ToolCall
from edu_agent.engine.mock import MockEngine
from edu_agent.runtime.config import (
    AppConfig,
    CodeExecutionConfig,
    LifecycleConfig,
    StorageConfig,
)
from edu_agent.runtime.lifecycle import (
    LifecycleController,
    LifecycleRejected,
    LifecycleState,
)
from edu_agent.runtime.models import RunContext
from edu_agent.scheduler import JobStore, Scheduler
from edu_agent.service import EduAgentService
from edu_agent.state import StateStore
from edu_agent.tools.registry import ToolSpec
from edu_agent.runtime.transactions import InjectedFault, NamedFaultInjector
from edu_agent.tools import registry


class _FakeClock:
    def __init__(self) -> None:
        self.value = 10.0

    def __call__(self) -> float:
        return self.value


class _AdvancingEvent:
    def __init__(self, clock: _FakeClock) -> None:
        self.clock = clock
        self.signalled = False

    def set(self) -> None:
        self.signalled = True

    def clear(self) -> None:
        self.signalled = False

    def wait(self, timeout: float) -> bool:
        self.clock.value += timeout
        signalled = self.signalled
        self.signalled = False
        return signalled


class _GateEngine(Engine):
    name = "gate"

    def __init__(self, *, cooperative: bool) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.cooperative = cooperative

    def chat(self, messages, tools, *, cancellation_token=None):
        del messages, tools
        self.entered.set()
        while not self.release.wait(0.005):
            if self.cooperative and cancellation_token is not None:
                cancellation_token.checkpoint("gate.engine")
        return EngineResponse(content="done")


class _ExternalHealthEngine(Engine):
    name = "external-health-is-non-blocking"
    endpoint = "https://model.invalid/private"
    api_key = "must-not-appear"

    def health_check(self):
        raise OSError("temporary external outage at private endpoint")

    def chat(self, messages, tools):
        return EngineResponse(content="done")


class _ToolEngine(Engine):
    name = "blocking-tool"

    def chat(self, messages, tools):
        if any(message.get("role") == "tool" for message in messages):
            return EngineResponse(content="done")
        return EngineResponse(tool_calls=[ToolCall("tool-1", "blocking_read", {})])


class _BlockingToolProvider:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.close_calls = 0
        self.spec = ToolSpec(
            schema={
                "name": "blocking_read",
                "description": "blocking read",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
            handler=lambda connection: {},
            category="query",
        )

    def openai_tools(self, **kwargs):
        del kwargs
        return [{"type": "function", "function": self.spec.schema}]

    def get_spec(self, name):
        return self.spec if name == "blocking_read" else None

    def dispatch_with_context(
        self,
        name,
        arguments,
        context,
        conn=None,
        *,
        manifest=None,
    ):
        del name, arguments, conn, manifest
        self.entered.set()
        self.release.wait(2)
        context.cancellation_token.checkpoint("blocking_tool.after_wait")
        return {"ok": True}

    def close(self) -> None:
        self.close_calls += 1
        self.release.set()


def _config(tmp_path, *, deadline: float = 0.3) -> AppConfig:
    return AppConfig(
        storage=StorageConfig(
            state_path=str(tmp_path / "state.db"),
            artifact_path=str(tmp_path / "artifacts"),
        ),
        lifecycle=LifecycleConfig(
            shutdown_deadline_seconds=deadline,
            cancellation_grace_seconds=deadline * 0.35,
            final_flush_seconds=deadline * 0.2,
            poll_interval_seconds=0.005,
        ),
    )


def _auth() -> DemoTokenAuth:
    return DemoTokenAuth(
        {"token": Principal("teacher", "school", "teacher")}
    )


def _post_chat(api: EduAgentApi, request_id: str, message: str = "hello"):
    return api.dispatch(
        "POST",
        "/v1/chat",
        headers={
            "Authorization": "Bearer token",
            "Content-Type": "application/json",
            "X-Request-ID": request_id,
        },
        body=json.dumps({"message": message}).encode(),
    )


def _wait_for_state(controller: LifecycleController, state: LifecycleState) -> None:
    deadline = time.monotonic() + 1
    while controller.state is not state and time.monotonic() < deadline:
        time.sleep(0.002)
    assert controller.state is state


def test_controller_requires_startup_health_and_uses_injected_clock_event():
    clock = _FakeClock()
    audits = []
    controller = LifecycleController(
        clock=clock,
        event_factory=lambda: _AdvancingEvent(clock),
        poll_interval_seconds=0.25,
        audit_sink=audits.append,
    )
    assert controller.health_snapshot()["live"] is True
    assert controller.health_snapshot()["ready"] is False

    controller.set_health(
        migration=True,
        state_db_writable=True,
        required_providers=True,
    )
    controller.complete_startup()
    admission = controller.admit("api.chat")
    cancelled = []
    admission.add_cancel_callback(lambda: cancelled.append(True))
    assert controller.begin_draining("test_signal") is True
    assert controller.health_snapshot()["ready"] is False
    assert controller.health_snapshot()["live"] is True
    with pytest.raises(LifecycleRejected):
        controller.admit("api.chat")
    assert controller.wait_for_idle(clock() + 1.0) is False
    assert clock() == pytest.approx(11.0)
    assert controller.cancel_active() == 1
    assert cancelled == [True]
    admission.close()
    assert controller.wait_for_idle(clock() + 1.0) is True
    controller.mark_stopped()
    assert [item["to_state"] for item in audits] == [
        "starting",
        "running",
        "draining",
        "stopped",
    ]


def test_concurrent_drain_requests_are_idempotent():
    controller = LifecycleController()
    controller.set_health(
        migration=True,
        state_db_writable=True,
        required_providers=True,
    )
    controller.complete_startup()
    barrier = threading.Barrier(8)
    outcomes = []
    errors = []

    def drain() -> None:
        try:
            barrier.wait()
            outcomes.append(controller.begin_draining("concurrent_signal"))
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=drain) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(1)

    assert errors == []
    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 7
    assert controller.state is LifecycleState.DRAINING
    assert [
        item["to_state"] for item in controller.transition_history()
    ].count("draining") == 1


def test_liveness_probe_does_not_refresh_blocking_readiness(tmp_path, monkeypatch):
    service = EduAgentService(
        MockEngine(lambda messages, tools, step: EngineResponse(content="done")),
        config=_config(tmp_path),
    )
    api = EduAgentApi(service, authenticator=_auth(), stream_cleanup_seconds=0.05)

    def blocked_refresh():
        raise AssertionError("liveness must not perform readiness I/O")

    monkeypatch.setattr(service, "_refresh_lifecycle_health", blocked_refresh)
    live = api.dispatch("GET", "/health/live")
    assert live.status == 200
    assert live.body == {
        "status": "ok",
        "lifecycle": "running",
        "live": True,
    }
    api.close()


def test_api_acceptance_race_normal_drain_and_no_session_lease_leak(tmp_path):
    engine = _GateEngine(cooperative=True)
    service = EduAgentService(engine, config=_config(tmp_path, deadline=0.6))
    api = EduAgentApi(service, authenticator=_auth(), stream_cleanup_seconds=0.05)
    responses = []
    request = threading.Thread(
        target=lambda: responses.append(_post_chat(api, "accepted-before-drain")),
        name="lifecycle-test-request",
    )
    request.start()
    assert engine.entered.wait(1)
    reports = []
    shutdown = threading.Thread(
        target=lambda: reports.append(service.shutdown()),
        name="lifecycle-test-shutdown",
    )
    shutdown.start()
    _wait_for_state(service.lifecycle, LifecycleState.DRAINING)

    rejected = _post_chat(api, "rejected-after-drain")
    assert rejected.status == 503
    assert rejected.body["error"]["code"] == "PROCESS_NOT_READY"
    engine.release.set()
    request.join(2)
    shutdown.join(2)
    assert not request.is_alive() and not shutdown.is_alive()
    assert responses[0].status == 200
    assert reports[0].normal_drained is True
    run_id = responses[0].body["run_id"]
    run = service.get_run_status(run_id, actor_id="teacher", tenant_id="school")
    session = service.get_session_status(
        run["session_id"], actor_id="teacher", tenant_id="school"
    )
    assert run["status"] == "completed"
    assert session["current_owner"] is None
    assert service.runtime_manager.active_runs() == []
    assert not any(
        thread.name.startswith("edu-agent-session-heartbeat-")
        for thread in threading.enumerate()
    )
    api.close()


def test_api_admission_wins_race_before_durable_request_claim(tmp_path, monkeypatch):
    service = EduAgentService(
        MockEngine(lambda messages, tools, step: EngineResponse(content="accepted")),
        config=_config(tmp_path, deadline=0.6),
    )
    api = EduAgentApi(service, authenticator=_auth(), stream_cleanup_seconds=0.05)
    original_claim = api._claim_request
    at_claim = threading.Event()
    release_claim = threading.Event()

    def delayed_claim(payload, principal, request_id):
        at_claim.set()
        assert release_claim.wait(1)
        return original_claim(payload, principal, request_id)

    monkeypatch.setattr(api, "_claim_request", delayed_claim)
    responses = []
    request = threading.Thread(
        target=lambda: responses.append(_post_chat(api, "claim-race")),
    )
    request.start()
    assert at_claim.wait(1)
    reports = []
    shutdown = threading.Thread(target=lambda: reports.append(service.shutdown()))
    shutdown.start()
    _wait_for_state(service.lifecycle, LifecycleState.DRAINING)
    release_claim.set()
    request.join(2)
    shutdown.join(2)
    assert not request.is_alive() and not shutdown.is_alive()
    assert responses[0].status == 200
    assert responses[0].body["final_answer"] == "accepted"
    assert reports[0].normal_drained is True
    assert service.state_store.count("runs") == 1
    api.close()


def test_deadline_fences_stubborn_provider_and_restart_resumes(tmp_path):
    engine = _GateEngine(cooperative=False)
    config = _config(tmp_path, deadline=0.16)
    first = EduAgentService(engine, config=config)
    failures = []

    def run_chat() -> None:
        try:
            first.chat("block", actor_id="teacher", tenant_id="school")
        except Exception as error:
            failures.append(error)

    worker = threading.Thread(target=run_chat, name="stubborn-provider-run")
    worker.start()
    assert engine.entered.wait(1)
    active = first.runtime_manager.active_runs()[0]
    report = first.shutdown()
    assert report.cancellation_requested is True
    assert report.recoverable_runs == 1
    assert report.active_remaining == 1
    run = first.get_run_status(
        active["run_id"], actor_id="teacher", tenant_id="school"
    )
    assert run["status"] == "abandoned"
    assert run["recovery_reason"] == "process_shutdown_deadline"
    assert run["current_owner"] is not None

    recovery_engine = _GateEngine(cooperative=True)
    recovery_engine.release.set()
    restarted = EduAgentService(recovery_engine, config=config)
    startup = {item["run_id"]: item for item in restarted.recovery_report}
    assert startup[active["run_id"]]["decision"]["action"] == "continue"

    engine.release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert failures
    assert first.runtime_manager.active_runs() == []
    resumed = restarted.resume_run(
        active["run_id"],
        actor_id="teacher",
        tenant_id="school",
    )
    assert resumed.final_answer == "done"
    terminal = restarted.get_run_status(
        active["run_id"], actor_id="teacher", tenant_id="school"
    )
    session = restarted.get_session_status(
        terminal["session_id"], actor_id="teacher", tenant_id="school"
    )
    assert terminal["status"] == "completed"
    assert session["current_owner"] is None
    restarted.close()


def test_blocking_tool_is_closed_cancelled_and_finalized_within_deadline(tmp_path):
    provider = _BlockingToolProvider()
    service = EduAgentService(
        _ToolEngine(),
        config=_config(tmp_path, deadline=0.3),
        tools_provider=provider,
    )
    results = []
    worker = threading.Thread(
        target=lambda: results.append(
            service.chat("use tool", actor_id="teacher", tenant_id="school")
        ),
        name="blocking-tool-run",
    )
    worker.start()
    assert provider.entered.wait(1)
    report = service.shutdown()
    worker.join(2)
    assert not worker.is_alive()
    assert report.cancellation_requested is True
    assert report.recoverable_runs == 0
    assert results[0].stop_reason == "interrupted"
    assert provider.close_calls >= 1
    status = service.get_run_status(
        results[0].run_id,
        actor_id="teacher",
        tenant_id="school",
    )
    session = service.get_session_status(
        results[0].session_id,
        actor_id="teacher",
        tenant_id="school",
    )
    assert status["status"] == "interrupted"
    assert session["current_owner"] is None


def test_scheduler_drain_cancels_claim_and_late_attempt_is_fenced(tmp_path):
    state = StateStore(tmp_path / "scheduler.db")
    controller = LifecycleController(poll_interval_seconds=0.005)
    controller.set_health(
        migration=True,
        state_db_writable=True,
        required_providers=True,
    )
    controller.complete_startup()
    jobs = JobStore(state)
    now = datetime.now(UTC)
    job_id = jobs.create(
        actor_id="teacher",
        tenant_id="school",
        role="teacher",
        name="blocked",
        prompt="blocked",
        next_run_at=now,
    )
    entered = threading.Event()

    def blocked(job, *, cancellation_token=None):
        del job
        entered.set()
        cancellation_token.wait(2)
        cancellation_token.checkpoint("scheduler.runner")

    scheduler = Scheduler(
        state,
        blocked,
        worker_id="worker-1",
        lease_seconds=1,
        lifecycle=controller,
    )
    results = []
    worker = threading.Thread(target=lambda: results.extend(scheduler.tick()))
    worker.start()
    assert entered.wait(1)
    controller.begin_draining("test")
    assert controller.cancel_active() >= 1
    worker.join(2)
    assert not worker.is_alive()
    assert results == [{"job_id": job_id, "status": "draining"}]
    assert scheduler.tick() == []
    with state.connect() as connection:
        durable = dict(
            connection.execute(
                "SELECT * FROM scheduled_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        )
    assert durable["status"] == "running"
    assert durable["lease_owner"] == "worker-1"
    assert not any(
        thread.name.startswith("edu-agent-job-heartbeat-")
        for thread in threading.enumerate()
    )

    old_claim = durable
    reclaimed = jobs.claim_due(
        worker_id="worker-2",
        now=datetime.fromisoformat(durable["lease_until"]) + timedelta(seconds=1),
        lease_seconds=30,
    )[0]
    assert reclaimed["attempt_count"] == old_claim["attempt_count"] + 1
    assert jobs.complete(
        old_claim,
        worker_id="worker-1",
        success=True,
        result="late",
        now=datetime.fromisoformat(durable["lease_until"]) + timedelta(seconds=2),
    ) is False
    assert jobs.complete(
        reclaimed,
        worker_id="worker-2",
        success=True,
        result="current",
        now=datetime.fromisoformat(durable["lease_until"]) + timedelta(seconds=2),
    ) is True


def test_repeated_shutdown_and_final_flush_failure_are_bounded(tmp_path, monkeypatch):
    service = EduAgentService(
        MockEngine(lambda messages, tools, step: EngineResponse(content="done")),
        config=_config(tmp_path, deadline=0.2),
    )

    def fail_flush() -> None:
        raise OSError("disk flush failed")

    monkeypatch.setattr(service.state_store, "flush", fail_flush)
    first = service.shutdown()
    second = service.shutdown()
    assert first is second
    assert first.state == "stopped"
    assert first.flush_succeeded is False
    assert first.flush_timed_out is False
    transitions = service.lifecycle.transition_history()
    assert [item["to_state"] for item in transitions].count("draining") == 1
    assert [item["to_state"] for item in transitions].count("stopped") == 1
    with service.state_store.connect() as connection:
        durable = connection.execute(
            """
            SELECT decision FROM audit_events
            WHERE action='process.lifecycle_transition'
            ORDER BY id
            """
        ).fetchall()
    assert [row["decision"] for row in durable] == [
        "starting",
        "running",
        "draining",
        "stopped",
    ]


def test_incomplete_finalizer_is_persisted_for_resume_before_lease_release(tmp_path):
    service = EduAgentService(
        MockEngine(lambda messages, tools, step: EngineResponse(content="done")),
        config=_config(tmp_path, deadline=0.2),
        finalizer_fault_injector=NamedFaultInjector("after_finalizer_tools_closed"),
    )
    with pytest.raises(InjectedFault):
        service.chat("fail finalizer", actor_id="teacher", tenant_id="school")
    run = service.runtime_manager.active_runs()
    assert run == []
    with service.state_store.connect() as connection:
        pending = dict(
            connection.execute(
                """
                SELECT r.id, r.status, r.owner_id, f.cursor
                FROM runs r JOIN turn_finalizers f ON f.run_id=r.id
                WHERE f.cursor=1
                """
            ).fetchone()
        )
        lease_before = connection.execute(
            "SELECT active_run_id FROM session_leases WHERE active_run_id=?",
            (pending["id"],),
        ).fetchone()
    assert lease_before is not None
    report = service.shutdown()
    assert report.normal_drained is False
    assert report.recoverable_runs == 1
    status = service.get_run_status(
        pending["id"], actor_id="teacher", tenant_id="school"
    )
    assert status["status"] == "abandoned"
    assert status["recovery_recommendation"] == "resume_finalizer"
    assert status["current_owner"] is not None


def test_shutdown_recovery_preserves_existing_manual_review_and_lease(tmp_path):
    state = StateStore(tmp_path / "manual-review.db")
    context = RunContext.create(
        session_id="session",
        run_id="run",
        actor_id="teacher",
        tenant_id="school",
        role="teacher",
    )
    state.ensure_session(
        context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        role=context.role,
    )
    state.enqueue_run(context, request_text="manual review")
    state.acquire_session_lease(
        session_id=context.session_id,
        run_id=context.run_id,
        owner_id="process-owner",
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        lease_seconds=30,
    )
    state.upsert_tool_operation_ref(
        {
            "id": "operation",
            "idempotency_key": "operation-key",
            "payload_hash": "payload-hash",
            "tool_name": "write_tool",
            "tenant_id": context.tenant_id,
            "actor_id": context.actor_id,
            "session_id": context.session_id,
            "run_id": context.run_id,
            "status": "manual_review",
            "updated_at": state.now_iso(),
        }
    )

    marked = state.mark_owner_runs_recoverable(owner_id="process-owner")
    assert marked[0]["recovery_recommendation"] == "manual_review"
    assert marked[0]["manual_review_operations"] == ["operation"]
    operation = state.get_tool_operation_ref(
        "operation",
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
    )
    assert operation["status"] == "manual_review"
    session = state.get_session_status(
        context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
    )
    assert session["active_run_id"] == context.run_id
    assert session["current_owner"] == "process-owner"


def test_required_local_provider_blocks_startup_without_leaking_details(
    tmp_path,
    monkeypatch,
):
    class Health:
        def __init__(self, healthy: bool) -> None:
            self.healthy = healthy
            self.message = (
                "https://sandbox.invalid api_key=private" if not healthy else "healthy"
            )

    class Provider:
        def __init__(self) -> None:
            self.healthy = False

        def health_check(self, *, force=False):
            del force
            return Health(self.healthy)

    provider = Provider()
    monkeypatch.setattr(
        service_module,
        "build_code_execution_provider",
        lambda config: provider,
    )
    config = AppConfig(
        storage=StorageConfig(
            state_path=str(tmp_path / "state.db"),
            artifact_path=str(tmp_path / "artifacts"),
        ),
        lifecycle=LifecycleConfig(
            shutdown_deadline_seconds=0.2,
            cancellation_grace_seconds=0.07,
            final_flush_seconds=0.04,
            poll_interval_seconds=0.005,
        ),
        code_execution=CodeExecutionConfig(
            enabled=True,
            provider="docker",
            image="example/python@sha256:" + "a" * 64,
            security_attested=True,
        ),
    )
    service = EduAgentService(_ExternalHealthEngine(), config=config)
    api = EduAgentApi(service, authenticator=_auth(), stream_cleanup_seconds=0.05)
    try:
        readiness = api.dispatch("GET", "/health/ready")
        assert readiness.status == 503
        assert readiness.body["lifecycle"] == "starting"
        rendered = json.dumps(readiness.body)
        assert "sandbox.invalid" not in rendered
        assert "private" not in rendered
        with pytest.raises(LifecycleRejected):
            service.chat("not yet", actor_id="teacher", tenant_id="school")

        provider.healthy = True
        recovered = api.dispatch("GET", "/health/ready")
        assert recovered.status == 200
        assert recovered.body["lifecycle"] == "running"
    finally:
        api.close()
        registry.configure_code_execution(None)


def test_real_http_liveness_readiness_and_draining_rejection(tmp_path):
    service = EduAgentService(_ExternalHealthEngine(), config=_config(tmp_path))
    api = EduAgentApi(service, authenticator=_auth(), stream_cleanup_seconds=0.05)
    server = make_http_server(api, host="127.0.0.1", port=0)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request("GET", "/health/ready")
        ready = connection.getresponse()
        ready_body = json.loads(ready.read())
        assert ready.status == 200
        assert ready_body["ready"] is True
        assert ready_body["provider_policy"].endswith("external_model_non_blocking")
        rendered = json.dumps(ready_body)
        assert "model.invalid" not in rendered
        assert "must-not-appear" not in rendered

        service.lifecycle.begin_draining("http_test")
        connection.request("GET", "/health/ready")
        draining_ready = connection.getresponse()
        assert draining_ready.status == 503
        assert json.loads(draining_ready.read())["lifecycle"] == "draining"
        connection.request("GET", "/health/live")
        live = connection.getresponse()
        assert live.status == 200
        assert json.loads(live.read())["live"] is True

        connection.request(
            "POST",
            "/v1/chat",
            body=json.dumps({"message": "must reject"}).encode(),
            headers={
                "Authorization": "Bearer token",
                "Content-Type": "application/json",
                "X-Request-ID": "http-draining",
            },
        )
        rejected = connection.getresponse()
        assert rejected.status == 503
        assert json.loads(rejected.read())["error"]["code"] == "PROCESS_NOT_READY"
        assert service.state_store.count("runs") == 0
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        server_thread.join(2)
        api.close()


def test_api_server_sigterm_drains_and_stops_with_audited_transitions(tmp_path):
    root = Path(__file__).resolve().parents[1]
    state_path = tmp_path / "state.db"
    artifact_path = tmp_path / "artifacts"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            (
                "[storage]",
                f'state_path = "{state_path}"',
                f'artifact_path = "{artifact_path}"',
                "[lifecycle]",
                "shutdown_deadline_seconds = 1.0",
                "cancellation_grace_seconds = 0.2",
                "final_flush_seconds = 0.1",
                "poll_interval_seconds = 0.01",
            )
        ),
        encoding="utf-8",
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    environment = os.environ.copy()
    environment.update(
        {
            "EDU_AGENT_CONFIG": str(config_path),
            "EDU_AGENT_DEMO_TOKEN": "local-test-token",
            "EDU_AGENT_API_HOST": "127.0.0.1",
            "EDU_AGENT_API_PORT": str(port),
            "EDU_AGENT_API_KEY": "",
            "EDU_AGENT_FALLBACK_API_KEY": "",
            "PYTHONUNBUFFERED": "1",
        }
    )
    process = subprocess.Popen(
        [sys.executable, "scripts/api_server.py"],
        cwd=root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while True:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                raise AssertionError(f"API server exited before readiness: {stdout}\n{stderr}")
            try:
                connection = HTTPConnection("127.0.0.1", port, timeout=0.2)
                connection.request("GET", "/health/ready")
                response = connection.getresponse()
                body = json.loads(response.read())
                connection.close()
                if response.status == 200 and body["ready"] is True:
                    break
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise AssertionError("API server did not become ready")
            time.sleep(0.02)
        process.terminate()
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
    with StateStore(state_path, read_only=True).connect() as connection:
        transitions = [
            (row[0], json.loads(row[1]))
            for row in connection.execute(
                """
                SELECT decision, details_json FROM audit_events
                WHERE action='process.lifecycle_transition' ORDER BY id
                """
            )
        ]
    assert [item[0] for item in transitions] == [
        "starting",
        "running",
        "draining",
        "stopped",
    ]
    assert [(item[1]["from_state"], item[0]) for item in transitions[1:]] == [
        ("starting", "running"),
        ("running", "draining"),
        ("draining", "stopped"),
    ]
