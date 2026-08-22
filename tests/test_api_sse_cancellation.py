from __future__ import annotations

import json
import socket
import threading
import time
from http.client import HTTPConnection

import pytest

from edu_agent.api import DemoTokenAuth, EduAgentApi, Principal, make_http_server
from edu_agent.engine import (
    ApiMode,
    GatewayEngine,
    ProviderCapabilities,
    ProviderGateway,
    ProviderSpec,
    ProviderStreamEvent,
    ProviderStreamEventType,
    ResilientEngine,
    aggregate_provider_stream,
)
from edu_agent.observability import (
    RunEventBus,
    RunEventTerminalError,
    RunEventType,
    RunEventWriterRejected,
    RunStreamWriterRegistry,
)
from edu_agent.planning.runtime import plan_spec_for_calls
from edu_agent.runtime.cancellation import CancellationToken
from edu_agent.runtime.config import ApiConfig, AppConfig, StorageConfig
from edu_agent.service import EduAgentService
from edu_agent.tools.registry import ToolSpec


class ScriptedAdapter:
    api_mode = ApiMode.CHAT_COMPLETIONS
    capabilities = ProviderCapabilities(
        streaming=True,
        context_window_tokens=16_384,
    )

    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = 0

    def chat(self, route, messages, tools, *, cancellation_token=None):
        return aggregate_provider_stream(
            self.stream_events(
                route,
                messages,
                tools,
                cancellation_token=cancellation_token,
            )
        )

    def stream_events(
        self,
        route,
        messages,
        tools,
        *,
        attempt=1,
        cancellation_token=None,
    ):
        self.calls += 1
        yield from self.behavior(
            self.calls,
            route,
            attempt,
            cancellation_token,
        )


class ReadProvider:
    def __init__(self):
        self.calls = 0
        self.spec = ToolSpec(
            schema={
                "name": "read_once",
                "description": "read once",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
            handler=lambda **_arguments: {"value": 1},
            category="query",
        )

    def openai_tools(self, **_kwargs):
        return [{"type": "function", "function": self.spec.schema}]

    def get_spec(self, name):
        return self.spec if name == "read_once" else None

    def dispatch(self, name, arguments, conn=None):
        assert name == "read_once"
        self.calls += 1
        return {"value": self.calls}


def _gateway(adapter, *, model="stream-model", endpoint="https://stream.example/v1"):
    return GatewayEngine(
        ProviderGateway(adapters={ApiMode.CHAT_COMPLETIONS: adapter}),
        ProviderSpec(
            model=model,
            endpoint=endpoint,
            api_mode=ApiMode.CHAT_COMPLETIONS,
            capabilities=adapter.capabilities,
        ),
    )


def _text(route, attempt, delta, event_id="text"):
    return ProviderStreamEvent(
        ProviderStreamEventType.TEXT_DELTA,
        route=route,
        attempt=attempt,
        provider_event_id=event_id,
        provider_event_type="fixture.text.delta",
        delta=delta,
    )


def _usage(route, attempt, **usage):
    return ProviderStreamEvent(
        ProviderStreamEventType.USAGE,
        route=route,
        attempt=attempt,
        provider_event_id="usage",
        provider_event_type="fixture.usage",
        usage=usage,
    )


def _completed(route, attempt, *, finish_reason="stop"):
    return ProviderStreamEvent(
        ProviderStreamEventType.COMPLETED,
        route=route,
        attempt=attempt,
        provider_event_id="completed",
        provider_event_type="fixture.completed",
        finish_reason=finish_reason,
        model=route.model,
    )


def _tool_delta(route, attempt, event_type, delta):
    return ProviderStreamEvent(
        event_type,
        route=route,
        attempt=attempt,
        provider_event_id=f"tool-{event_type.value}",
        provider_event_type="fixture.tool.delta",
        delta=delta,
        tool_call_index=0,
    )


def _service(tmp_path, engine, *, tools_provider=None, plan_generator=None):
    config = AppConfig(
        storage=StorageConfig(
            state_path=str(tmp_path / "state.db"),
            artifact_path=str(tmp_path / "artifacts"),
        ),
        api=ApiConfig(request_lease_seconds=2),
    )
    return EduAgentService(
        engine,
        tools_provider=tools_provider,
        plan_generator=plan_generator,
        config=config,
    )


def _start(service, **api_options):
    api = EduAgentApi(
        service,
        authenticator=DemoTokenAuth(
            {"token": Principal("teacher", "school", "teacher")}
        ),
        stream_keepalive_seconds=0.02,
        stream_cleanup_seconds=0.25,
        **api_options,
    )
    server = make_http_server(api, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return api, server, thread


def _stop(api, service, server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    api.close()
    service.close()


def _request(server, request_id, payload=None):
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    connection.request(
        "POST",
        "/v1/chat",
        body=json.dumps(payload or {"message": "stream", "stream": True}).encode(),
        headers={
            "Authorization": "Bearer token",
            "Content-Type": "application/json",
            "X-Request-ID": request_id,
        },
    )
    return connection, connection.getresponse()


def _events(body: bytes):
    events = []
    for frame in body.split(b"\n\n"):
        fields = {}
        for line in frame.splitlines():
            if b":" not in line or line.startswith(b":"):
                continue
            key, value = line.split(b":", 1)
            fields[key.decode()] = value.lstrip().decode()
        if "event" in fields:
            fields["data"] = json.loads(fields["data"])
            events.append(fields)
    return events


def _read_event(response):
    frame = []
    while True:
        line = response.fp.readline()
        if not line:
            raise EOFError("SSE stream ended")
        if line == b"\n":
            if frame:
                parsed = _events(b"".join(frame) + b"\n")
                if parsed:
                    return parsed[0]
            frame = []
            continue
        if not line.startswith(b":"):
            frame.append(line)


def _wait_run(service, request_id, *, terminal=False, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        request = service.state_store.get_api_request(
            actor_id="teacher",
            tenant_id="school",
            request_id=request_id,
        )
        if request and request.get("run_id"):
            run = service.get_run_status(
                request["run_id"],
                actor_id="teacher",
                tenant_id="school",
            )
            if run and (not terminal or run["status"] in {"completed", "failed", "interrupted"}):
                return request, run
        time.sleep(0.01)
    raise AssertionError("run did not reach the requested state")


def test_real_socket_sse_maps_true_deltas_usage_and_monotonic_ids(tmp_path):
    def behavior(_call, route, attempt, _token):
        yield _text(route, attempt, "hel", "delta-1")
        yield _usage(route, attempt, input_tokens=3, output_tokens=2)
        yield _text(route, attempt, "lo", "delta-2")
        yield _completed(route, attempt)

    service = _service(tmp_path, _gateway(ScriptedAdapter(behavior)))
    api, server, thread = _start(service)
    connection, response = _request(server, "true-deltas")
    try:
        events = _events(response.read())
        names = [event["event"] for event in events]
        sequences = [int(event["id"]) for event in events]
        assert response.status == 200
        assert names[0] == "accepted"
        assert names.index("accepted") < names.index("text.delta") < names.index("completed")
        assert "usage" in names
        assert sequences == list(range(1, len(sequences) + 1))
        assert all(event["data"]["sequence"] == int(event["id"]) for event in events)
        assert "".join(
            event["data"]["payload"]["delta"]
            for event in events
            if event["event"] == "text.delta"
        ) == "hello"
    finally:
        connection.close()
        _stop(api, service, server, thread)


def test_real_socket_sse_maps_plan_and_tool_lifecycle(tmp_path):
    provider = ReadProvider()

    def behavior(call, route, attempt, _token):
        if call == 1:
            yield _tool_delta(
                route,
                attempt,
                ProviderStreamEventType.TOOL_CALL_ID_DELTA,
                "call-1",
            )
            yield _tool_delta(
                route,
                attempt,
                ProviderStreamEventType.TOOL_CALL_NAME_DELTA,
                "read_once",
            )
            yield _tool_delta(
                route,
                attempt,
                ProviderStreamEventType.TOOL_CALL_ARGUMENTS_DELTA,
                "{}",
            )
            yield _completed(route, attempt, finish_reason="tool_calls")
            return
        yield _text(route, attempt, "done")
        yield _completed(route, attempt)

    class PlanGenerator:
        def generate(self, task, *, context, available_tools, max_steps):
            return plan_spec_for_calls(task, [("step-1", "read_once", [])])

    service = _service(
        tmp_path,
        _gateway(ScriptedAdapter(behavior)),
        tools_provider=provider,
        plan_generator=PlanGenerator(),
    )
    api, server, thread = _start(service)
    connection, response = _request(
        server,
        "tool-plan",
        {"message": "先查成绩，再分析错题", "stream": True},
    )
    try:
        names = [event["event"] for event in _events(response.read())]
        assert "plan.updated" in names
        assert names.index("tool.started") < names.index("tool.completed")
        assert names[-1] == "completed"
        assert provider.calls == 1
    finally:
        connection.close()
        _stop(api, service, server, thread)


def test_real_socket_disconnect_cancels_half_tool_json_without_execution(tmp_path):
    partial_sent = threading.Event()
    provider_stopped = threading.Event()
    provider = ReadProvider()

    def behavior(_call, route, attempt, token):
        try:
            yield _tool_delta(
                route,
                attempt,
                ProviderStreamEventType.TOOL_CALL_ID_DELTA,
                "call-half",
            )
            yield _tool_delta(
                route,
                attempt,
                ProviderStreamEventType.TOOL_CALL_NAME_DELTA,
                "read_once",
            )
            partial_sent.set()
            yield _tool_delta(
                route,
                attempt,
                ProviderStreamEventType.TOOL_CALL_ARGUMENTS_DELTA,
                "{",
            )
            while not token.wait(0.01):
                pass
            token.checkpoint("fixture.half_tool")
        finally:
            provider_stopped.set()

    service = _service(
        tmp_path,
        _gateway(ScriptedAdapter(behavior)),
        tools_provider=provider,
    )
    api, server, thread = _start(service)
    connection, response = _request(server, "half-tool")
    try:
        assert _read_event(response)["event"] == "accepted"
        assert partial_sent.wait(1)
        started = time.monotonic()
        response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
        response.close()
        connection.close()
        _, run = _wait_run(service, "half-tool", terminal=True)
        assert time.monotonic() - started < 0.75
        assert run["status"] == "interrupted"
        assert provider_stopped.wait(0.5)
        assert provider.calls == 0
        assert service.state_store.get_tool_events(
            run_id=run["id"],
            session_id=run["session_id"],
        ) == []
    finally:
        _stop(api, service, server, thread)


def test_real_socket_fallback_rejects_late_primary_delta(tmp_path):
    primary_route = {}

    class ConnectionFailure(ConnectionError):
        pass

    def primary(_call, route, attempt, _token):
        primary_route["route"] = route
        error = ConnectionFailure("primary disconnected")
        yield ProviderStreamEvent(
            ProviderStreamEventType.ERROR,
            route=route,
            attempt=attempt,
            provider_event_id="primary-error",
            provider_event_type="fixture.error",
            error_code="connection",
            error_message=str(error),
            error=error,
            retryable=True,
        )

    def fallback(_call, route, attempt, _token):
        yield _text(primary_route["route"], attempt - 1, "late", "late-primary")
        yield _text(route, attempt, "selected", "fallback-current")
        yield _completed(route, attempt)

    engine = ResilientEngine(
        _gateway(ScriptedAdapter(primary), model="primary"),
        fallback=_gateway(
            ScriptedAdapter(fallback),
            model="fallback",
            endpoint="https://fallback.example/v1",
        ),
        max_retries=0,
    )
    service = _service(tmp_path, engine)
    api, server, thread = _start(service)
    connection, response = _request(server, "fallback")
    try:
        events = _events(response.read())
        names = [event["event"] for event in events]
        output = "".join(
            event["data"]["payload"]["delta"]
            for event in events
            if event["event"] == "text.delta"
        )
        assert "fallback.activated" in names
        assert output == "selected"
        assert "late" not in output
        assert names[-1] == "completed"
    finally:
        connection.close()
        _stop(api, service, server, thread)


def test_real_socket_explicit_and_duplicate_cancel_share_token(tmp_path):
    visible = threading.Event()

    def behavior(_call, route, attempt, token):
        yield _text(route, attempt, "started")
        visible.set()
        while not token.wait(0.01):
            pass
        token.checkpoint("fixture.wait")

    service = _service(tmp_path, _gateway(ScriptedAdapter(behavior)))
    api, server, thread = _start(service)
    stream_connection, response = _request(server, "explicit-cancel")
    cancel_connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    try:
        accepted = _read_event(response)
        assert accepted["event"] == "accepted"
        assert visible.wait(1)
        run_id = accepted["data"]["run_id"]
        results = []
        for _ in range(2):
            cancel_connection.request(
                "POST",
                f"/v1/runs/{run_id}/cancel",
                body=b"{}",
                headers={
                    "Authorization": "Bearer token",
                    "Content-Type": "application/json",
                },
            )
            cancel_response = cancel_connection.getresponse()
            results.append(json.loads(cancel_response.read())["cancel_requested"])
        assert results == [True, False]
        terminal = _read_event(response)
        while terminal["event"] not in {"completed", "error"}:
            terminal = _read_event(response)
        assert terminal["event"] == "error"
        assert terminal["data"]["payload"]["code"] == "CANCELLED"
        _, run = _wait_run(service, "explicit-cancel", terminal=True)
        assert run["status"] == "interrupted"
    finally:
        cancel_connection.close()
        response.close()
        stream_connection.close()
        _stop(api, service, server, thread)


def test_real_socket_deadline_and_slow_consumer_cancel_provider(tmp_path):
    deadline_stopped = threading.Event()

    def blocked(_call, _route, _attempt, token):
        try:
            while not token.wait(0.01):
                pass
            token.checkpoint("fixture.deadline")
            yield  # pragma: no cover
        finally:
            deadline_stopped.set()

    service = _service(tmp_path / "deadline", _gateway(ScriptedAdapter(blocked)))
    api, server, thread = _start(service)
    connection, response = _request(
        server,
        "deadline",
        {"message": "deadline", "stream": True, "timeout_seconds": 0.08},
    )
    try:
        events = _events(response.read())
        assert events[-1]["event"] == "error"
        assert events[-1]["data"]["payload"]["code"] == "DEADLINE_EXCEEDED"
        _, run = _wait_run(service, "deadline", terminal=True)
        assert run["status"] == "interrupted"
        assert deadline_stopped.wait(0.5)
    finally:
        connection.close()
        _stop(api, service, server, thread)

    slow_stopped = threading.Event()
    large_delta = "x" * (512 * 1024)

    def flood(_call, route, attempt, token):
        try:
            index = 0
            while not token.is_set():
                yield _text(route, attempt, large_delta, f"flood-{index}")
                index += 1
                time.sleep(0.001)
            token.checkpoint("fixture.slow_consumer")
        finally:
            slow_stopped.set()

    slow_service = _service(tmp_path / "slow", _gateway(ScriptedAdapter(flood)))
    slow_api, slow_server, slow_thread = _start(
        slow_service,
        stream_buffer_size=4,
        stream_write_timeout_seconds=0.1,
    )
    raw = socket.create_connection(("127.0.0.1", slow_server.server_port), timeout=2)
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
    request = json.dumps({"message": "flood", "stream": True}).encode()
    raw.sendall(
        b"POST /v1/chat HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Authorization: Bearer token\r\n"
        b"Content-Type: application/json\r\n"
        b"X-Request-ID: slow-consumer\r\n"
        + f"Content-Length: {len(request)}\r\n\r\n".encode()
        + request
    )
    try:
        assert slow_stopped.wait(2)
        _, run = _wait_run(slow_service, "slow-consumer", terminal=True)
        assert run["status"] == "interrupted"
    finally:
        raw.close()
        _stop(slow_api, slow_service, slow_server, slow_thread)


def test_real_socket_terminal_rejects_late_delta(tmp_path):
    def behavior(_call, route, attempt, _token):
        yield _text(route, attempt, "on-time")
        yield _completed(route, attempt)

    service = _service(tmp_path, _gateway(ScriptedAdapter(behavior)))
    original_chat = service.chat
    late_rejected = threading.Event()
    late_threads = []

    def chat_with_late_producer(*args, stream_writer=None, **kwargs):
        result = original_chat(*args, stream_writer=stream_writer, **kwargs)

        def publish_late():
            deadline = time.monotonic() + 1
            while not stream_writer.terminal and time.monotonic() < deadline:
                time.sleep(0.001)
            try:
                stream_writer.publish(
                    RunEventType.TEXT_DELTA,
                    {"delta": "late-after-terminal"},
                )
            except (RunEventTerminalError, RunEventWriterRejected):
                late_rejected.set()

        late_thread = threading.Thread(target=publish_late)
        late_threads.append(late_thread)
        late_thread.start()
        return result

    service.chat = chat_with_late_producer
    api, server, thread = _start(service)
    connection, response = _request(server, "terminal-late-delta")
    try:
        events = _events(response.read())
        assert late_rejected.wait(1)
        assert events[-1]["event"] == "completed"
        assert "late-after-terminal" not in json.dumps(events)
    finally:
        for late_thread in late_threads:
            late_thread.join(timeout=1)
        connection.close()
        _stop(api, service, server, thread)


def test_api_close_has_bounded_cleanup_for_stubborn_provider(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    def behavior(_call, route, attempt, _token):
        entered.set()
        release.wait(2)
        yield _text(route, attempt, "late")
        yield _completed(route, attempt)

    service = _service(tmp_path, _gateway(ScriptedAdapter(behavior)))
    api = EduAgentApi(
        service,
        authenticator=DemoTokenAuth(
            {"token": Principal("teacher", "school", "teacher")}
        ),
        stream_keepalive_seconds=0.01,
        stream_cleanup_seconds=0.05,
    )
    response = api.dispatch(
        "POST",
        "/v1/chat",
        headers={
            "Authorization": "Bearer token",
            "Content-Type": "application/json",
            "X-Request-ID": "bounded-api-close",
        },
        body=json.dumps({"message": "block", "stream": True}).encode(),
    )
    iterator = iter(response.body)
    try:
        assert _events(next(iterator))[0]["event"] == "accepted"
        assert entered.wait(1)
        started = time.monotonic()
        api.close()
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        iterator.close()
        deadline = time.monotonic() + 1
        while api._futures and time.monotonic() < deadline:
            time.sleep(0.01)
        service.close()
    assert not api._futures
    assert service.runtime_manager.active_runs() == []


def test_stream_writer_rejects_replaced_owner_and_terminal_delta():
    bus = RunEventBus(max_buffer_size=16)
    registry = RunStreamWriterRegistry(bus)
    first_token = CancellationToken()
    first = registry.open(
        run_id="run",
        attempt=1,
        writer_id="owner-1",
        cancellation_token=first_token,
    )
    first.bind(session_id="session", fencing_token=4)
    first.publish(RunEventType.RUN_PHASE, {"phase": "accepted"})
    replacement = registry.open(
        run_id="run",
        attempt=2,
        writer_id="owner-2",
        cancellation_token=CancellationToken(),
    )
    assert first_token.cancelled
    assert first_token.cancellation.source == "owner_replaced"
    with pytest.raises(RunEventWriterRejected):
        first.publish(RunEventType.TEXT_DELTA, {"delta": "stale owner"})
    replacement.bind(session_id="session", fencing_token=5)
    replacement.complete({"stop_reason": "completed"})
    with pytest.raises(RunEventTerminalError):
        replacement.complete({"stop_reason": "duplicate"})
    with pytest.raises(RunEventWriterRejected):
        replacement.provider_event(
            _text(type("Route", (), {"identity": "route"})(), 1, "late")
        )
    registry.close()
    bus.close()
