from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection

import pytest

from edu_agent.api import DemoTokenAuth, EduAgentApi, Principal, make_http_server
from edu_agent.delegation import DelegationState, SubtaskStatus
from edu_agent.engine.base import EngineResponse, ToolCall
from edu_agent.engine.mock import MockEngine
from edu_agent.observability import (
    OptionalTelemetryExporter,
    RedactionPolicy,
    TraceRepository,
    build_opentelemetry_exporter,
    contains_sensitive_data,
)
from edu_agent.runtime.config import AppConfig, ObservabilityConfig, StorageConfig
from edu_agent.runtime.models import RunContext
from edu_agent.scheduler import JobStore
from edu_agent.service import EduAgentService


def _service(tmp_path, policy=None):
    return EduAgentService(
        MockEngine(policy or (lambda messages, tools, step: EngineResponse(content="done"))),
        config=AppConfig(
            storage=StorageConfig(
                state_path=str(tmp_path / "state.db"),
                artifact_path=str(tmp_path / "artifacts"),
            )
        ),
    )


def _api(service):
    return EduAgentApi(
        service,
        authenticator=DemoTokenAuth(
            {
                "alice-token": Principal("alice", "school-a", "teacher"),
                "bob-token": Principal("bob", "school-a", "teacher"),
                "mallory-token": Principal("alice", "school-b", "teacher"),
                "student-token": Principal("student", "school-a", "student"),
            }
        ),
    )


def _headers(token="alice-token", request_id=None):
    headers = {"Authorization": f"Bearer {token}"}
    if request_id:
        headers["X-Request-ID"] = request_id
    return headers


def _post(api, path, payload, *, token="alice-token", request_id=None):
    return api.dispatch(
        "POST",
        path,
        headers=_headers(token, request_id),
        body=json.dumps(payload).encode(),
    )


def test_runtime_trace_envelope_is_stable_ordered_paginated_and_read_only(tmp_path):
    def policy(messages, tools, step):
        if step == 0:
            return EngineResponse(
                tool_calls=[ToolCall(id="call-1", name="list_exams", arguments={})]
            )
        return EngineResponse(content="done")

    service = _service(tmp_path, policy)
    result = service.chat("list exams", actor_id="alice", tenant_id="school-a", role="teacher")
    before = service.state_store.count("messages")
    repository = TraceRepository(service.state_store)
    first = repository.list_events(
        actor_id="alice", tenant_id="school-a", run_id=result.run_id, limit=2
    )
    second = repository.list_events(
        actor_id="alice", tenant_id="school-a", run_id=result.run_id,
        cursor=first.next_cursor, limit=100,
    )
    all_events = first.events + second.events
    repeated = repository.list_events(
        actor_id="alice", tenant_id="school-a", run_id=result.run_id, limit=100
    ).events

    assert first.next_cursor == 2
    assert [event.event_id for event in all_events] == [event.event_id for event in repeated]
    assert [event.sequence for event in repeated] == list(range(1, len(repeated) + 1))
    assert all(event.schema_version == "edu-agent.runtime-event.v1" for event in repeated)
    assert all(event.actor_id == "alice" and event.tenant_id == "school-a" for event in repeated)
    assert any(event.event_type == "tool.completed" for event in repeated)
    assert service.state_store.count("messages") == before


def test_trace_filters_summary_and_streaming_exports(tmp_path):
    service = _service(tmp_path)
    result = service.chat("hello", actor_id="alice", tenant_id="school-a", role="teacher")
    service.state_store.record_provider_event(
        run_id=result.run_id,
        provider="test-provider",
        event="provider_failure",
        attempt=1,
        error_class="TimeoutError",
        details={"message": "temporary timeout"},
    )
    repository = TraceRepository(service.state_store)
    page = repository.list_events(
        actor_id="alice", tenant_id="school-a", run_id=result.run_id,
        provider="test-provider", error="timeout",
    )
    summary = repository.inspect_run(result.run_id, actor_id="alice", tenant_id="school-a")
    jsonl = "".join(repository.iter_export(
        actor_id="alice", tenant_id="school-a", run_id=result.run_id,
        format="jsonl", page_size=1,
    ))
    exported_json = "".join(repository.iter_export(
        actor_id="alice", tenant_id="school-a", run_id=result.run_id,
        format="json", page_size=1,
    ))

    assert len(page.events) == 1
    assert page.events[0].component == "provider"
    assert summary["summary"]["events"] >= 1
    assert summary["summary"]["recovery_recommendation"] == "none"
    assert len([line for line in jsonl.splitlines() if line]) == summary["summary"]["events"]
    assert json.loads(exported_json)["schema_version"] == "edu-agent.runtime-event.v1"


def test_canary_secret_absent_from_sqlite_trace_log_and_artifact_preview(tmp_path, caplog):
    canary = "CANARY_SECRET_stage7-never-export"

    def policy(messages, tools, step):
        if step == 0:
            return EngineResponse(
                tool_calls=[ToolCall(
                    id="call-1",
                    name="list_exams",
                    arguments={"api_key": canary},
                )]
            )
        return EngineResponse(content=f"password={canary}")

    service = _service(tmp_path, policy)
    with caplog.at_level(logging.INFO):
        result = service.chat(
            f"token={canary}", actor_id="alice", tenant_id="school-a", role="teacher"
        )
        logging.getLogger("stage7-test").info("secret=%s", RedactionPolicy().redact_text(canary))
    context = RunContext.create(
        session_id=result.session_id, run_id="artifact-run", actor_id="alice",
        tenant_id="school-a", role="teacher",
    )
    artifact = service.artifact_store.write_text(
        f'{{"password":"{canary}","preview":"api_key={canary}"}}',
        context=context,
        kind="canary",
        metadata={"approval_secret": canary},
    )
    service.state_store.compact_messages(
        result.session_id,
        summary=f"password={canary}",
        message_count=1,
        estimated_tokens_before=10,
    )
    memory_id = service.state_store.add_memory(
        actor_id="alice",
        tenant_id="school-a",
        content=f"secret={canary}",
    )
    assert service.state_store.update_memory(
        memory_id,
        actor_id="alice",
        tenant_id="school-a",
        content=f"token={canary}",
    )
    service.state_store.begin_api_request(
        actor_id="alice",
        tenant_id="school-a",
        request_id="canary-request",
        request_hash="canary-hash",
        run_id=result.run_id,
    )
    service.state_store.finish_api_request(
        actor_id="alice",
        tenant_id="school-a",
        request_id="canary-request",
        status="failed",
        error={"message": f"password={canary}"},
    )
    now = datetime.now(UTC)
    jobs = JobStore(service.state_store)
    job_id = jobs.create(
        actor_id="alice",
        tenant_id="school-a",
        role="teacher",
        name=f"secret={canary}",
        prompt=f"token={canary}",
        next_run_at=now - timedelta(seconds=1),
    )
    job = jobs.claim_due(worker_id="canary-worker", now=now)[0]
    assert job["id"] == job_id
    jobs.complete(
        job,
        worker_id="canary-worker",
        success=False,
        error=f"api_key={canary}",
        now=now,
    )
    failed_context = RunContext.create(
        session_id=result.session_id,
        run_id="failed-run",
        actor_id="alice",
        tenant_id="school-a",
        role="teacher",
        course_ids={1},
    )
    service.state_store.enqueue_run(failed_context, request_text="safe request")
    service.state_store.finish_run(
        failed_context.run_id,
        status="failed",
        budget={"input_tokens": 3},
        error=f"password={canary}",
        recovery_reason=f"secret={canary}",
    )
    invalid_plan = service.state_store.create_invalid_plan(
        run_id=failed_context.run_id,
        session_id=failed_context.session_id,
        actor_id=failed_context.actor_id,
        tenant_id=failed_context.tenant_id,
        goal=f"token={canary}",
        failure_reason=f"password={canary}",
        max_iterations=1,
    )
    service.state_store.update_plan(
        invalid_plan["id"], failure_reason=f"api_key={canary}"
    )
    delegation = DelegationState(service.state_store)
    delegated = delegation.create_batch(
        parent_context=RunContext.create(
            session_id=result.session_id,
            run_id=result.run_id,
            actor_id="alice",
            tenant_id="school-a",
            role="teacher",
            course_ids={1},
        ),
        entries=[{
            "task_spec": {
                "task_key": "canary-child",
                "kind": "class_analysis",
                "task": f"password={canary}",
                "arguments": {"api_key": canary},
                "course_ids": [1],
                "plan_step_id": None,
            },
            "input": {"messages": [{"role": "user", "content": f"secret={canary}"}]},
            "role": "teacher",
            "model": "deterministic-readonly-v1",
            "allowed_tools": [],
            "allowed_categories": [],
            "can_delegate": False,
        }],
        root_budget={
            "max_model_calls": 2,
            "max_tool_calls": 2,
            "max_tokens": 100,
            "max_cost_usd": 1.0,
        },
        child_budget={
            "max_model_calls": 1,
            "max_tool_calls": 1,
            "max_tokens": 50,
            "max_cost_usd": 0.5,
        },
        max_depth=1,
        max_children_per_parent=1,
    )[0]
    claimed = delegation.claim(
        delegated["id"],
        worker_owner="canary-worker",
        max_concurrency=1,
        lease_seconds=30,
    )
    assert claimed is not None
    finished = delegation.finish(
        delegated["id"],
        worker_owner="canary-worker",
        status=SubtaskStatus.completed,
        usage={
            "model_calls": 1,
            "tool_calls": 0,
            "input_tokens": 3,
            "output_tokens": 2,
            "total_tokens": 5,
            "estimated_cost_usd": 0.0,
        },
        result={"summary": f"secret={canary}"},
    )
    assert finished["usage"]["input_tokens"] == 3
    api = _api(service)
    api_result = _post(
        api,
        "/v1/chat",
        {"message": f"token={canary}"},
        request_id="canary-chat",
    )
    api_replay = _post(
        api,
        "/v1/chat",
        {"message": f"token={canary}"},
        request_id="canary-chat",
    )
    assert api_result.status == api_replay.status == 200
    assert api_result.body == api_replay.body
    assert canary not in json.dumps(api_result.body, ensure_ascii=False)
    raw_database = (tmp_path / "state.db").read_bytes()
    raw_artifact = (tmp_path / "artifacts" / "school-a" / "alice" / result.session_id).joinpath(
        f"{artifact.id}-canary.json"
    ).read_bytes()
    repository = TraceRepository(service.state_store, redaction=RedactionPolicy((canary,)))
    exported = "".join(repository.iter_export(
        actor_id="alice", tenant_id="school-a", run_id=result.run_id,
        format="jsonl", page_size=1,
    ))

    assert canary.encode() not in raw_database
    assert canary.encode() not in raw_artifact
    assert canary not in exported
    assert canary not in caplog.text
    assert not contains_sensitive_data(json.loads(exported.splitlines()[0]), secrets=(canary,))


def test_trace_scope_blocks_actor_tenant_session_and_run_idor(tmp_path):
    service = _service(tmp_path)
    result = service.chat("hello", actor_id="alice", tenant_id="school-a", role="teacher")
    repository = TraceRepository(service.state_store)
    for actor, tenant in (("bob", "school-a"), ("alice", "school-b")):
        with pytest.raises(PermissionError):
            repository.list_events(actor_id=actor, tenant_id=tenant, run_id=result.run_id)
        with pytest.raises(PermissionError):
            repository.list_events(actor_id=actor, tenant_id=tenant, session_id=result.session_id)


def test_api_auth_idempotency_status_and_cross_scope_denial(tmp_path):
    service = _service(tmp_path)
    api = _api(service)
    payload = {"message": "hello"}
    first = _post(api, "/v1/chat", payload, request_id="request-1")
    replay = _post(api, "/v1/chat", payload, request_id="request-1")
    conflict = _post(api, "/v1/chat", {"message": "changed"}, request_id="request-1")
    unauthenticated = api.dispatch("GET", f"/v1/runs/{first.body['run_id']}")

    assert first.status == replay.status == 200
    assert first.body["run_id"] == replay.body["run_id"]
    assert replay.headers == {"Idempotent-Replay": "true"}
    assert service.state_store.count("runs") == 1
    assert conflict.status == 409
    assert unauthenticated.status == 401
    for token in ("bob-token", "mallory-token"):
        denied_run = api.dispatch(
            "GET", f"/v1/runs/{first.body['run_id']}", headers=_headers(token)
        )
        denied_trace = api.dispatch(
            "GET", f"/v1/traces?run_id={first.body['run_id']}", headers=_headers(token)
        )
        assert denied_run.status == denied_trace.status == 403
    session = api.dispatch(
        "GET", f"/v1/sessions/{first.body['session_id']}", headers=_headers()
    )
    denied_session = api.dispatch(
        "GET", f"/v1/sessions/{first.body['session_id']}", headers=_headers("bob-token")
    )
    export = api.dispatch(
        "GET", f"/v1/traces/export?run_id={first.body['run_id']}&format=jsonl&limit=1",
        headers=_headers(),
    )
    denied_export = api.dispatch(
        "GET", f"/v1/traces/export?run_id={first.body['run_id']}",
        headers=_headers("mallory-token"),
    )
    assert session.status == 200 and denied_session.status == 403
    assert export.content_type.startswith("application/x-ndjson")
    assert json.loads(b"".join(export.body).splitlines()[0])["schema_version"]
    assert denied_export.status == 403


def test_api_returns_structured_checkpoint_validation_error(tmp_path):
    service = _service(tmp_path)
    api = _api(service)
    service.state_store.ensure_session(
        "damaged-session",
        actor_id="alice",
        tenant_id="school-a",
        role="teacher",
        course_ids=set(),
    )
    service.state_store.append_messages(
        "damaged-session",
        [
            {"role": "user", "content": "archive"},
            {"role": "assistant", "content": "keep"},
        ],
    )
    checkpoint = service.state_store.compact_messages(
        "damaged-session",
        summary="trusted summary",
        message_count=1,
        estimated_tokens_before=10,
    )
    with service.state_store.connect() as connection:
        connection.execute(
            "UPDATE context_checkpoints SET summary='tampered' WHERE id=?",
            (checkpoint["id"],),
        )

    try:
        response = _post(
            api,
            "/v1/chat",
            {"message": "continue", "session_id": "damaged-session"},
            request_id="checkpoint-error-1",
        )
        assert response.status == 500
        assert response.body["error"]["code"] == (
            "CONTEXT_CHECKPOINT_SUMMARY_HASH_MISMATCH"
        )
    finally:
        api.close()
        service.close()


def test_http_server_serves_openapi_and_authenticated_chat(tmp_path):
    service = _service(tmp_path)
    api = _api(service)
    server = make_http_server(api, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request("GET", "/openapi.json")
        openapi = connection.getresponse()
        assert openapi.status == 200
        assert json.loads(openapi.read())["openapi"] == "3.1.0"

        connection.request(
            "POST",
            "/v1/chat",
            body=json.dumps({"message": "hello"}).encode(),
            headers={
                "Authorization": "Bearer alice-token",
                "Content-Type": "application/json",
                "X-Request-ID": "http-chat-1",
            },
        )
        chat = connection.getresponse()
        payload = json.loads(chat.read())
        assert chat.status == 200
        assert payload["final_answer"] == "done"
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        api.close()
        service.close()


def test_api_artifact_metadata_content_role_and_tenant_scope(tmp_path):
    service = _service(tmp_path)
    result = service.chat("hello", actor_id="alice", tenant_id="school-a", role="teacher")
    context = RunContext.create(
        session_id=result.session_id, run_id="artifact-run", actor_id="alice",
        tenant_id="school-a", role="teacher",
    )
    artifact = service.artifact_store.write_text(
        "x" * 200, context=context, kind="report", metadata={"title": "demo"}
    )
    api = _api(service)
    metadata = api.dispatch(
        "GET", f"/v1/artifacts/{artifact.id}", headers=_headers()
    )
    content = api.dispatch(
        "GET", f"/v1/artifacts/{artifact.id}/content?limit=25", headers=_headers()
    )
    denied = api.dispatch(
        "GET", f"/v1/artifacts/{artifact.id}", headers=_headers("bob-token")
    )
    denied_role = api.dispatch(
        "GET", f"/v1/artifacts/{artifact.id}/content", headers=_headers("student-token")
    )

    assert metadata.status == 200 and "path" not in metadata.body
    assert content.body["content"] == "x" * 25 and content.body["truncated"] is True
    assert denied.status in {403, 404}
    assert denied_role.status == 403


def test_api_schedule_is_idempotent_and_owner_scoped(tmp_path):
    service = _service(tmp_path)
    api = _api(service)
    payload = {
        "name": "weekly",
        "prompt": "summary",
        "next_run_at": datetime.now(UTC).isoformat(),
    }
    first = _post(api, "/v1/schedules", payload, request_id="schedule-1")
    second = _post(api, "/v1/schedules", payload, request_id="schedule-1")
    denied = _post(
        api, f"/v1/schedules/{first.body['id']}/cancel", {},
        token="bob-token", request_id="cancel-1",
    )
    cancelled = _post(
        api, f"/v1/schedules/{first.body['id']}/cancel", {}, request_id="cancel-2"
    )

    assert first.status == second.status == 201
    assert first.body["id"] == second.body["id"]
    assert service.state_store.count("scheduled_jobs") == 1
    assert denied.status == 403
    assert cancelled.status == 202 and cancelled.body["cancel_requested"] is True


def test_stream_disconnect_requests_cooperative_cancel(tmp_path, monkeypatch):
    service = _service(tmp_path)
    api = _api(service)
    cancelled = []
    monkeypatch.setattr(
        service,
        "cancel_run",
        lambda run_id, **scope: cancelled.append((run_id, scope)) or True,
    )
    response = _post(
        api,
        "/v1/chat",
        {"message": "hello", "stream": True},
        request_id="stream-1",
    )
    iterator = iter(response.body)
    accepted = next(iterator)
    iterator.close()

    assert response.content_type.startswith("text/event-stream")
    assert b"accepted" in accepted
    assert len(cancelled) == 1


def test_telemetry_default_off_and_failure_isolated():
    calls = []
    disabled = OptionalTelemetryExporter(
        exporter=lambda event: calls.append(event)
    ).export([])
    enabled = OptionalTelemetryExporter(
        enabled=True,
        exporter=lambda event: (_ for _ in ()).throw(RuntimeError("collector down")),
    ).export_payloads([{"event_id": "one"}])

    assert disabled.enabled is False and calls == []
    assert enabled.enabled is True and enabled.exported == 0
    assert "RuntimeError" in enabled.error
    assert ObservabilityConfig().otel_enabled is False
    assert build_opentelemetry_exporter(ObservabilityConfig()).enabled is False
    with pytest.raises(ValueError, match="otlp_endpoint"):
        ObservabilityConfig(otel_enabled=True)
    missing = build_opentelemetry_exporter(
        ObservabilityConfig(otel_enabled=True, otlp_endpoint="http://127.0.0.1:4318/v1/traces")
    ).export_payloads([{"event_type": "run.finished"}])
    assert missing.enabled is True and missing.exported == 0
    assert "otel" in missing.error.lower()


def test_legacy_database_migrates_api_request_table(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE sessions(id TEXT PRIMARY KEY, actor_id TEXT NOT NULL,
          tenant_id TEXT NOT NULL, title TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
          sequence INTEGER NOT NULL, role TEXT NOT NULL, content TEXT, name TEXT,
          tool_call_id TEXT, tool_calls_json TEXT, created_at TEXT NOT NULL,
          UNIQUE(session_id, sequence));
        CREATE TABLE scheduled_jobs(id TEXT PRIMARY KEY, actor_id TEXT NOT NULL,
          tenant_id TEXT NOT NULL, name TEXT NOT NULL, prompt TEXT NOT NULL,
          interval_seconds INTEGER, next_run_at TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
          lease_owner TEXT, lease_until TEXT, last_status TEXT, last_result TEXT,
          last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        """
    )
    connection.close()

    service = _service(tmp_path / "fresh")
    migrated = type(service.state_store)(path)
    assert migrated.count("api_requests") == 0
    with migrated.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM state_schema_migrations WHERE version='007_observability_api'"
        ).fetchone()
