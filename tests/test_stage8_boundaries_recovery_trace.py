from __future__ import annotations

import json
import socket
import threading
import time
import tracemalloc
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection

import pytest

from edu_agent.api import DemoTokenAuth, EduAgentApi, Principal, make_http_server
from edu_agent.data_audit import audit_paths
from edu_agent.engine.base import EngineResponse
from edu_agent.engine.mock import MockEngine
from edu_agent.observability import OptionalTelemetryExporter, RedactionPolicy, TraceRepository
from edu_agent.runtime.config import ApiConfig, AppConfig, StorageConfig
from edu_agent.runtime.models import RunContext
from edu_agent.service import EduAgentService
from edu_agent.state import StateStore


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 18, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


def _service(tmp_path, *, clock=None, policy=None, request_lease_seconds=1.0):
    config = AppConfig(
        storage=StorageConfig(
            state_path=str(tmp_path / "state.db"),
            artifact_path=str(tmp_path / "artifacts"),
        ),
        api=ApiConfig(
            request_lease_seconds=request_lease_seconds,
            request_retention_seconds=10,
            failed_request_retention_seconds=5,
            request_gc_batch_size=20,
        ),
    )
    state = StateStore(config.state_path, clock=clock)
    return EduAgentService(
        MockEngine(policy or (lambda messages, tools, step: EngineResponse(content="done"))),
        config=config,
        state_store=state,
    )


def _api(service):
    return EduAgentApi(
        service,
        authenticator=DemoTokenAuth({
            "alice-token": Principal("alice", "school-a", "teacher"),
            "bob-token": Principal("bob", "school-a", "teacher"),
        }),
    )


def _socket_json(connection, method, target, *, token=None, request_id=None, payload=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if request_id:
        headers["X-Request-ID"] = request_id
    body = b""
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode()
    connection.request(method, target, body=body, headers=headers)
    response = connection.getresponse()
    data = response.read()
    decoded = (
        json.loads(data)
        if data and response.getheader("Content-Type", "").startswith("application/json")
        else None
    )
    return response, decoded, data


def _start_server(service):
    api = _api(service)
    server = make_http_server(api, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return api, server, thread


def _stop_server(api, service, server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    api.close()
    service.close()


def test_shared_classifier_preserves_scope_and_metrics_but_export_redacts_pii():
    payload = {
        "actor_id": "student-owner-7",
        "tenant_id": "school-a",
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
        "max_tokens": 100,
        "fencing_token": 9,
        "access_token": "secret-value",
        "student_id": "20260001",
        "student_name": "Alice",
        "email": "student@example.edu",
        "phone": "13800138000",
    }
    redacted = RedactionPolicy().redact(payload)
    assert redacted["actor_id"] == "student-owner-7"
    assert redacted["tenant_id"] == "school-a"
    assert [redacted[key] for key in (
        "input_tokens", "output_tokens", "total_tokens", "max_tokens", "fencing_token"
    )] == [11, 7, 18, 100, 9]
    assert all(redacted[key] == "[REDACTED]" for key in (
        "access_token", "student_id", "student_name", "email", "phone"
    ))

    exported = []
    result = OptionalTelemetryExporter(enabled=True, exporter=exported.append).export_payloads([payload])
    assert result.exported == 1
    assert exported == [redacted]


def test_read_only_history_audit_scans_sqlite_wal_json_log_and_artifact(tmp_path):
    state = StateStore(tmp_path / "state.db")
    with state.connect() as connection:
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute(
            "INSERT INTO audit_events(actor_id, tenant_id, action, resource, decision, details_json, created_at) "
            "VALUES ('a', 't', 'safe', 'safe', 'allow', '{}', ?)",
            (state.now_iso(),),
        )
    canary = "CANARY_SECRET_stage8-audit-only"
    (tmp_path / "events.jsonl").write_text(
        json.dumps({"approval_secret": canary, "input_tokens": 3}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "runtime.log").write_text(f"api_key={canary}\n", encoding="utf-8")
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    (artifact_dir / "sample.txt").write_text(
        "phone=13800138000 source=/Users/private-user/project/report.json",
        encoding="utf-8",
    )

    report = audit_paths([tmp_path / "state.db", tmp_path / "events.jsonl", tmp_path / "runtime.log", artifact_dir])
    serialized = json.dumps(report, ensure_ascii=False)
    assert report["mode"] == "read_only_dry_run"
    assert report["files_scanned"] >= 4
    assert report["totals"]["credential"] >= 2
    assert report["totals"]["student_pii"] >= 1
    assert report["totals"]["private_path"] >= 1
    assert canary not in serialized
    assert str(tmp_path) not in serialized
    assert all(finding["location"].startswith("file[") for finding in report["findings"])


def test_api_claim_before_run_reclaims_same_preallocated_run(tmp_path):
    clock = MutableClock()
    state = StateStore(tmp_path / "state.db", clock=clock)
    first = state.begin_api_request(
        actor_id="alice", tenant_id="school-a", request_id="claim-only",
        request_hash="hash", run_id="stable-run", owner_id="dead-owner", lease_seconds=1,
    )
    clock.advance(2)
    reclaimed = state.begin_api_request(
        actor_id="alice", tenant_id="school-a", request_id="claim-only",
        request_hash="hash", run_id="different-run", owner_id="new-owner", lease_seconds=1,
    )
    assert first["run_id"] == reclaimed["run_id"] == "stable-run"
    assert reclaimed["attempt"] == 2
    assert reclaimed["recovery_action"] == "execute"


def test_api_request_concurrent_claim_has_single_winner_and_stable_conflict(tmp_path):
    state = StateStore(tmp_path / "state.db")
    barrier = threading.Barrier(2)
    results = []

    def claim(owner):
        barrier.wait()
        results.append(state.begin_api_request(
            actor_id="alice", tenant_id="school-a", request_id="concurrent",
            request_hash="hash", run_id=f"run-{owner}", owner_id=owner, lease_seconds=10,
        ))

    threads = [threading.Thread(target=claim, args=(owner,)) for owner in ("one", "two")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    assert sorted(result["status"] for result in results) == ["claimed", "in_progress"]
    assert len({result["run_id"] for result in results}) == 1
    assert state.count("api_requests") == 1
    with pytest.raises(ValueError, match="different payload"):
        state.begin_api_request(
            actor_id="alice", tenant_id="school-a", request_id="concurrent",
            request_hash="changed", owner_id="three", lease_seconds=10,
        )


def test_api_stale_sweep_requires_matching_owner_and_scope(tmp_path):
    clock = MutableClock()
    state = StateStore(tmp_path / "state.db", clock=clock)
    state.begin_api_request(
        actor_id="alice", tenant_id="school-a", request_id="sweep",
        request_hash="hash", run_id="run", owner_id="old-owner", lease_seconds=1,
    )
    clock.advance(2)
    assert state.expire_api_request_leases(
        actor_id="alice", tenant_id="school-a", owner_id="other-owner"
    ) == 0
    assert state.expire_api_request_leases(
        actor_id="bob", tenant_id="school-a", owner_id="old-owner"
    ) == 0
    assert state.expire_api_request_leases(
        actor_id="alice", tenant_id="school-a", owner_id="old-owner"
    ) == 1
    assert state.get_api_request(
        actor_id="alice", tenant_id="school-a", request_id="sweep"
    )["status"] == "stale"


def test_api_running_run_waits_and_uncertain_write_requires_review(tmp_path):
    clock = MutableClock()
    state = StateStore(tmp_path / "state.db", clock=clock)
    context = RunContext.create(
        session_id="session", run_id="run", actor_id="alice",
        tenant_id="school-a", role="teacher",
    )
    state.ensure_session("session", actor_id="alice", tenant_id="school-a", role="teacher")
    state.enqueue_run(context, request_text="hello")
    state.acquire_session_lease(
        session_id="session", run_id="run", owner_id="runtime-owner",
        actor_id="alice", tenant_id="school-a", lease_seconds=20,
    )
    state.begin_api_request(
        actor_id="alice", tenant_id="school-a", request_id="running",
        request_hash="hash", run_id="run", owner_id="api-dead", lease_seconds=1,
    )
    clock.advance(2)
    waiting = state.begin_api_request(
        actor_id="alice", tenant_id="school-a", request_id="running",
        request_hash="hash", owner_id="api-new", lease_seconds=1,
    )
    assert waiting["status"] == "in_progress"
    assert waiting["recovery_action"] == "wait"

    with state.connect() as connection:
        connection.execute(
            """
            INSERT INTO tool_operation_refs(
                operation_id, idempotency_key, payload_hash, tool_name, tenant_id,
                actor_id, session_id, run_id, status, updated_at
            ) VALUES ('op', 'idem', 'payload', 'create_exam', 'school-a', 'alice',
                      'session', 'run', 'executing', ?)
            """,
            (state.now_iso(),),
        )
        connection.execute(
            "UPDATE session_leases SET expires_at=? WHERE active_run_id='run'",
            ((clock() - timedelta(seconds=1)).isoformat(),),
        )
    uncertain = state.begin_api_request(
        actor_id="alice", tenant_id="school-a", request_id="running",
        request_hash="hash", owner_id="api-third", lease_seconds=1,
    )
    assert uncertain["status"] == "uncertain"
    assert uncertain["error"]["code"] == "MANUAL_REVIEW_REQUIRED"


def test_completed_run_without_response_recovers_then_replays_identically(tmp_path):
    clock = MutableClock()
    service = _service(tmp_path, clock=clock)
    api = _api(service)
    payload = {"message": "hello"}
    request_hash = api._request_hash(payload)
    claim = service.begin_api_request(
        actor_id="alice", tenant_id="school-a", request_id="recover-response",
        request_hash=request_hash, run_id="recover-run", owner_id="dead-api",
        lease_seconds=1, retention_seconds=10,
    )
    assert service.start_api_request(
        actor_id="alice", tenant_id="school-a", request_id="recover-response",
        owner_id="dead-api", attempt=claim["attempt"],
    )
    original = service.chat(
        "hello", actor_id="alice", tenant_id="school-a", role="teacher",
        run_id="recover-run",
    )
    clock.advance(2)
    headers = {"Authorization": "Bearer alice-token", "X-Request-ID": "recover-response"}
    first = api.dispatch("POST", "/v1/chat", headers=headers, body=json.dumps(payload).encode())
    replay = api.dispatch("POST", "/v1/chat", headers=headers, body=json.dumps(payload).encode())
    assert first.status == replay.status == 200
    assert first.body == replay.body
    assert first.body["final_answer"] == original.final_answer
    assert replay.headers == {"Idempotent-Replay": "true"}
    record = service.state_store.get_api_request(
        actor_id="alice", tenant_id="school-a", request_id="recover-response"
    )
    assert record["response_hash"]
    api.close()
    service.close()


def test_api_stale_owner_commit_rejected_and_gc_is_owner_scoped(tmp_path):
    clock = MutableClock()
    state = StateStore(tmp_path / "state.db", clock=clock)
    first = state.begin_api_request(
        actor_id="alice", tenant_id="school-a", request_id="stale",
        request_hash="hash", run_id="run", owner_id="old", lease_seconds=1,
        retention_seconds=1,
    )
    assert state.start_api_request(
        actor_id="alice", tenant_id="school-a", request_id="stale",
        owner_id="old", attempt=first["attempt"],
    )
    clock.advance(2)
    second = state.begin_api_request(
        actor_id="alice", tenant_id="school-a", request_id="stale",
        request_hash="hash", owner_id="new", lease_seconds=5, retention_seconds=1,
    )
    with pytest.raises(RuntimeError, match="stale"):
        state.finish_api_request(
            actor_id="alice", tenant_id="school-a", request_id="stale",
            status="completed", response={"old": True}, owner_id="old", attempt=1,
        )
    assert state.start_api_request(
        actor_id="alice", tenant_id="school-a", request_id="stale",
        owner_id="new", attempt=second["attempt"],
    )
    state.finish_api_request(
        actor_id="alice", tenant_id="school-a", request_id="stale",
        status="failed", error={"code": "NO_RUN"}, owner_id="new",
        attempt=second["attempt"], retention_seconds=1,
    )
    clock.advance(2)
    assert state.gc_api_requests(actor_id="bob", tenant_id="school-a") == 0
    assert state.gc_api_requests(actor_id="alice", tenant_id="school-a") == 1


def test_real_http_auth_replay_conflict_content_type_and_scope(tmp_path):
    service = _service(tmp_path)
    api, server, thread = _start_server(service)
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    try:
        openapi, document, _ = _socket_json(connection, "GET", "/openapi.json")
        assert openapi.status == 200 and document["openapi"] == "3.1.0"
        first, first_body, first_raw = _socket_json(
            connection, "POST", "/v1/chat", token="alice-token",
            request_id="socket-request", payload={"message": "hello"},
        )
        replay, replay_body, replay_raw = _socket_json(
            connection, "POST", "/v1/chat", token="alice-token",
            request_id="socket-request", payload={"message": "hello"},
        )
        conflict, conflict_body, _ = _socket_json(
            connection, "POST", "/v1/chat", token="alice-token",
            request_id="socket-request", payload={"message": "changed"},
        )
        denied, denied_body, _ = _socket_json(
            connection, "GET", f"/v1/runs/{first_body['run_id']}", token="bob-token"
        )
        trace, trace_body, _ = _socket_json(
            connection, "GET", f"/v1/traces?run_id={first_body['run_id']}&limit=2",
            token="alice-token",
        )
        denied_trace, denied_trace_body, _ = _socket_json(
            connection, "GET", f"/v1/traces?run_id={first_body['run_id']}", token="bob-token"
        )
        export, _, export_raw = _socket_json(
            connection, "GET", f"/v1/traces/export?run_id={first_body['run_id']}&format=jsonl&limit=2",
            token="alice-token",
        )
        artifact = service.artifact_store.write_text(
            "artifact body",
            context=RunContext.create(
                session_id=first_body["session_id"], run_id=first_body["run_id"],
                actor_id="alice", tenant_id="school-a", role="teacher",
            ),
            kind="socket-test",
            metadata={"title": "socket"},
        )
        metadata, metadata_body, _ = _socket_json(
            connection, "GET", f"/v1/artifacts/{artifact.id}", token="alice-token"
        )
        denied_artifact, _, _ = _socket_json(
            connection, "GET", f"/v1/artifacts/{artifact.id}", token="bob-token"
        )
        assert first.status == replay.status == 200
        assert first_raw == replay_raw and first_body == replay_body
        assert first.getheader("Content-Type") == replay.getheader("Content-Type")
        assert replay.getheader("Idempotent-Replay") == "true"
        assert conflict.status == 409 and conflict_body["error"]["code"] == "CONFLICT"
        assert denied.status == 403 and denied_body["error"]["code"] == "SCOPE_DENIED"
        assert trace.status == 200 and trace_body["events"]
        assert denied_trace.status == 403 and denied_trace_body["error"]["code"] == "SCOPE_DENIED"
        assert export.status == 200 and export.getheader("Content-Type").startswith("application/x-ndjson")
        assert json.loads(export_raw.splitlines()[0])["schema_version"]
        assert metadata.status == 200 and metadata_body["id"] == artifact.id
        assert denied_artifact.status in {403, 404}
    finally:
        connection.close()
        _stop_server(api, service, server, thread)


def test_real_http_sse_order_disconnect_cancels_and_rejects_late_commit(tmp_path):
    entered = threading.Event()

    def slow_policy(messages, tools, step):
        entered.set()
        time.sleep(0.25)
        return EngineResponse(content="late answer")

    service = _service(tmp_path, policy=slow_policy, request_lease_seconds=5)
    api, server, thread = _start_server(service)
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    try:
        connection.request(
            "POST",
            "/v1/chat",
            body=json.dumps({"message": "slow", "stream": True}).encode(),
            headers={
                "Authorization": "Bearer alice-token",
                "Content-Type": "application/json",
                "X-Request-ID": "sse-disconnect",
            },
        )
        response = connection.getresponse()
        accepted = response.fp.readline() + response.fp.readline()
        assert response.status == 200
        assert b"event: accepted" in accepted
        assert entered.wait(timeout=1)
        response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
        response.close()
        connection.close()
        time.sleep(0.5)
        request = service.state_store.get_api_request(
            actor_id="alice", tenant_id="school-a", request_id="sse-disconnect"
        )
        run = service.get_run_status(
            request["run_id"], actor_id="alice", tenant_id="school-a"
        )
        assert run["status"] == "interrupted"
        assert all(
            message.get("content") != "late answer"
            for message in service.state_store.get_run_messages(request["run_id"])
        )
    finally:
        _stop_server(api, service, server, thread)


def test_real_http_sse_emits_accepted_before_completed(tmp_path):
    service = _service(tmp_path)
    api, server, thread = _start_server(service)
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    try:
        connection.request(
            "POST",
            "/v1/chat",
            body=json.dumps({"message": "fast", "stream": True}).encode(),
            headers={
                "Authorization": "Bearer alice-token",
                "Content-Type": "application/json",
                "X-Request-ID": "sse-order",
            },
        )
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200
        assert body.index(b"event: accepted") < body.index(b"event: completed")
    finally:
        connection.close()
        _stop_server(api, service, server, thread)


def _large_trace_state(tmp_path, events):
    state = StateStore(tmp_path / "trace.db")
    state.ensure_session("session", actor_id="alice", tenant_id="school-a", role="teacher")
    context = RunContext.create(
        session_id="session", run_id="run", actor_id="alice",
        tenant_id="school-a", role="teacher",
    )
    state.enqueue_run(context, request_text="trace")
    rows = [
        (
            "run", "provider", "attempt", None, index,
            json.dumps({"status": "ok", "input_tokens": index}),
            (datetime(2026, 8, 18, tzinfo=UTC) + timedelta(microseconds=index)).isoformat(),
        )
        for index in range(events)
    ]
    with state.connect() as connection:
        connection.executemany(
            """
            INSERT INTO provider_events(
                run_id, provider, event, error_class, attempt, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    return state


def test_trace_keyset_snapshot_cursor_scope_and_bounded_memory(tmp_path, monkeypatch):
    state = _large_trace_state(tmp_path, 4_000)
    repository = TraceRepository(state)
    monkeypatch.setattr(repository, "_project", lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("full projection must not be used")
    ))
    tracemalloc.start()
    cursor = None
    seen = set()
    pages = 0
    first_total = None
    first_cursor = None
    while True:
        page = repository.list_events(
            actor_id="alice", tenant_id="school-a", run_id="run",
            cursor=cursor, limit=73,
        )
        pages += 1
        first_total = page.total if first_total is None else first_total
        first_cursor = first_cursor or page.next_cursor
        for event in page.events:
            assert event.event_id not in seen
            seen.add(event.event_id)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(seen) == first_total == 4_001
    assert pages > 40
    assert repository.last_query_stats["rows_loaded"] <= 74
    assert repository.last_query_stats["sql_queries"] <= 3
    assert peak < 8 * 1024 * 1024

    with state.connect() as connection:
        connection.execute(
            """
            INSERT INTO provider_events(
                run_id, provider, event, error_class, attempt, details_json, created_at
            ) VALUES ('run', 'provider', 'late', NULL, 9999, '{}', ?)
            """,
            (datetime(2027, 1, 1, tzinfo=UTC).isoformat(),),
        )
    continued = repository.list_events(
        actor_id="alice", tenant_id="school-a", run_id="run",
        cursor=first_cursor, limit=73,
    )
    assert continued.total == first_total
    with pytest.raises(ValueError, match="invalid trace cursor"):
        repository.list_events(
            actor_id="alice", tenant_id="school-a", run_id="run",
            cursor=str(first_cursor) + "tampered", limit=10,
        )
    with pytest.raises(PermissionError):
        repository.list_events(
            actor_id="bob", tenant_id="school-a", run_id="run",
            cursor=first_cursor, limit=10,
        )


def test_trace_repository_can_query_through_read_only_state_store(tmp_path):
    state = _large_trace_state(tmp_path, 10)
    with state.connect() as connection:
        before = {
            row["name"]: connection.execute(f'SELECT COUNT(*) FROM "{row["name"]}"').fetchone()[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    readonly = StateStore(state.path, read_only=True)
    page = TraceRepository(readonly).list_events(
        actor_id="alice", tenant_id="school-a", run_id="run", limit=5
    )
    assert page.events
    with state.connect() as connection:
        after = {
            row["name"]: connection.execute(f'SELECT COUNT(*) FROM "{row["name"]}"').fetchone()[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert after == before
