from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from edu_agent.observability import (
    RUN_EVENT_SCHEMA_VERSION,
    SCHEMA_VERSION,
    EventBus,
    RunEvent,
    RunEventBus,
    RunEventCapacityError,
    RunEventTerminalError,
    RunEventType,
    RunEventValidationError,
    RunEventWriterRejected,
    RunPhase,
    RuntimeEvent,
    SlowConsumerError,
    SubscriptionCancelled,
    SubscriptionClosed,
    TraceRepository,
    contains_sensitive_data,
)
from edu_agent.observability.redaction import RedactionPolicy
from edu_agent.runtime.models import RunContext
from edu_agent.state import StateStore


def _publisher(bus: RunEventBus, **overrides):
    values = {
        "run_id": "run-1",
        "session_id": "session-1",
        "attempt": 1,
        "writer_id": "worker-1",
        "fencing_token": 7,
    }
    values.update(overrides)
    return bus.publisher(**values)


def test_run_event_v2_has_complete_minimal_typed_family_and_round_trips():
    assert {event_type.value for event_type in RunEventType} == {
        "run.phase",
        "text.delta",
        "tool_call.delta",
        "usage",
        "plan.updated",
        "tool.started",
        "tool.completed",
        "context.compacted",
        "fallback.activated",
        "completed",
        "error",
    }
    assert [phase.value for phase in RunPhase] == [
        "accepted", "planning", "model", "tools", "verifying", "finalizing", "terminal",
    ]

    payloads = [
        (RunEventType.RUN_PHASE, {"phase": "accepted"}),
        (RunEventType.TEXT_DELTA, {"delta": "hello"}),
        (RunEventType.TOOL_CALL_DELTA, {"index": 0, "delta": {"arguments": "{"}}),
        (RunEventType.USAGE, {"input_tokens": 3, "output_tokens": 2}),
        (RunEventType.PLAN_UPDATED, {"plan_id": "plan-1", "status": "running"}),
        (RunEventType.TOOL_STARTED, {"tool_call_id": "call-1", "tool_name": "list_exams"}),
        (RunEventType.TOOL_COMPLETED, {"tool_call_id": "call-1", "tool_name": "list_exams"}),
        (RunEventType.CONTEXT_COMPACTED, {"checkpoint_id": "checkpoint-1"}),
        (
            RunEventType.FALLBACK_ACTIVATED,
            {"from_route": "primary", "to_route": "fallback", "reason": "timeout"},
        ),
        (RunEventType.COMPLETED, {"stop_reason": "completed"}),
    ]
    bus = RunEventBus(max_buffer_size=len(payloads))
    subscription = bus.subscribe(run_id="run-1", attempt=1)
    publisher = _publisher(bus)
    published = [publisher.publish(event_type, payload) for event_type, payload in payloads]
    received = [subscription.get_nowait() for _ in published]

    assert [event.sequence for event in published] == list(range(1, len(published) + 1))
    assert [event.event_id for event in received] == [event.event_id for event in published]
    assert all(event.schema_version == RUN_EVENT_SCHEMA_VERSION for event in published)
    assert all(event.attempt == 1 and event.fencing_token == 7 for event in published)
    encoded = json.loads(json.dumps(published[0].to_dict()))
    assert RunEvent.from_dict(encoded) == published[0]
    with pytest.raises(SubscriptionClosed, match="completed/error"):
        subscription.get_nowait()

    failed = _publisher(
        bus,
        run_id="run-error",
        writer_id="worker-error",
        fencing_token=1,
    ).publish(RunEventType.ERROR, {"code": "FAKE_FAILURE", "message": "fixture error"})
    assert failed.event_type is RunEventType.ERROR

    preflight = _publisher(
        bus,
        run_id="run-preflight",
        attempt=0,
        writer_id="worker-preflight",
        fencing_token=0,
    ).publish(RunEventType.RUN_PHASE, {"phase": "accepted"})
    assert preflight.attempt == 0 and preflight.fencing_token == 0


def test_run_event_schema_rejects_unknown_missing_and_malformed_fields():
    timestamp = datetime.now(UTC).isoformat()
    valid = {
        "event_id": "event-1",
        "schema_version": RUN_EVENT_SCHEMA_VERSION,
        "event_type": "text.delta",
        "run_id": "run-1",
        "session_id": "session-1",
        "attempt": 1,
        "sequence": 1,
        "timestamp": timestamp,
        "writer_id": "worker-1",
        "fencing_token": 1,
        "payload": {"delta": "ok"},
    }

    with pytest.raises(RunEventValidationError, match="missing run event fields"):
        RunEvent.from_dict({key: value for key, value in valid.items() if key != "attempt"})
    with pytest.raises(RunEventValidationError, match="unknown run event fields"):
        RunEvent.from_dict(valid | {"extra": True})
    with pytest.raises(RunEventValidationError, match="schema_version"):
        RunEvent.from_dict(valid | {"schema_version": SCHEMA_VERSION})
    with pytest.raises(RunEventValidationError, match="unsupported run event type"):
        RunEvent.from_dict(valid | {"event_type": "token"})
    with pytest.raises(RunEventValidationError, match="positive integer"):
        RunEvent.from_dict(valid | {"sequence": 0})
    with pytest.raises(RunEventValidationError, match="timezone"):
        RunEvent.from_dict(valid | {"timestamp": "2026-08-22T12:00:00"})
    with pytest.raises(RunEventValidationError, match="payload.delta"):
        RunEvent.from_dict(valid | {"payload": {}})
    with pytest.raises(RunEventValidationError, match="finite JSON"):
        RunEvent.from_dict(valid | {"payload": {"delta": "ok", "score": float("nan")}})
    with pytest.raises(RunEventValidationError, match="field names"):
        RunEvent.from_dict(valid | {1: "bad"})


def test_run_event_payload_is_centrally_redacted_before_delivery():
    canary = "CANARY_SECRET_r2-event-never-deliver"
    bus = RunEventBus(redaction=RedactionPolicy((canary,)))
    subscription = bus.subscribe(run_id="run-1", attempt=1)
    event = _publisher(bus).publish(
        RunEventType.TEXT_DELTA,
        {"delta": f"email=teacher@example.com api_key={canary}"},
    )
    delivered = subscription.get_nowait()
    serialized = json.dumps(delivered.to_dict(), ensure_ascii=False)

    assert event == delivered
    assert canary not in serialized
    assert "teacher@example.com" not in serialized
    assert "[REDACTED]" in delivered.payload["delta"]
    assert not contains_sensitive_data(delivered.to_dict(), secrets=(canary,))


def test_sequence_allocation_is_thread_safe_and_delivery_order_is_authoritative():
    total = 80
    bus = RunEventBus(max_buffer_size=total + 2)
    subscription = bus.subscribe(run_id="run-1", attempt=1)
    publisher = _publisher(bus, sequence_start=40)
    producer_handles = [publisher, *[_publisher(bus) for _ in range(11)]]
    base = datetime(2026, 8, 22, tzinfo=UTC)

    later_clock = publisher.publish(
        RunEventType.USAGE,
        {"model_calls": 0},
        timestamp=base + timedelta(seconds=1),
    )
    earlier_clock = publisher.publish(
        RunEventType.USAGE,
        {"model_calls": 1},
        timestamp=base,
    )
    assert later_clock.sequence == 41 and earlier_clock.sequence == 42
    assert later_clock.timestamp > earlier_clock.timestamp
    start = threading.Event()

    def produce(index: int) -> RunEvent:
        start.wait(timeout=1)
        return producer_handles[index % len(producer_handles)].publish(
            RunEventType.USAGE,
            {"model_calls": index},
            timestamp=base + timedelta(microseconds=index),
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(produce, index) for index in range(total)]
        start.set()
        published = [future.result(timeout=2) for future in futures]
    received = [subscription.get_nowait() for _ in range(total + 2)]

    assert sorted(event.sequence for event in published) == list(range(43, 43 + total))
    assert [event.sequence for event in received] == list(range(41, 43 + total))
    assert len({event.event_id for event in received}) == total + 2


def test_writer_fence_rejects_stale_publishers_and_preserves_sequence():
    bus = RunEventBus()
    first = _publisher(bus, fencing_token=3, sequence_start=8)
    assert first.publish(RunEventType.RUN_PHASE, {"phase": "model"}).sequence == 9

    with pytest.raises(RunEventWriterRejected, match="older"):
        _publisher(bus, writer_id="stale", fencing_token=2)
    with pytest.raises(RunEventWriterRejected, match="different writer"):
        _publisher(bus, writer_id="collision", fencing_token=3)

    replacement = _publisher(bus, writer_id="worker-2", fencing_token=4)
    with pytest.raises(RunEventWriterRejected, match="older"):
        first.publish(RunEventType.TEXT_DELTA, {"delta": "stale"})
    resumed = replacement.publish(RunEventType.TEXT_DELTA, {"delta": "current"})
    assert resumed.sequence == 10
    assert resumed.writer_id == "worker-2" and resumed.fencing_token == 4

    other_attempt = _publisher(bus, attempt=2, writer_id="worker-3", fencing_token=1)
    assert other_attempt.publish(RunEventType.RUN_PHASE, {"phase": "accepted"}).sequence == 1


def test_terminal_event_closes_subscription_and_rejects_late_delta():
    bus = RunEventBus(max_buffer_size=4)
    subscription = bus.subscribe(run_id="run-1", attempt=1)
    publisher = _publisher(bus)
    publisher.publish(RunEventType.TEXT_DELTA, {"delta": "done"})
    terminal = publisher.publish(RunEventType.COMPLETED, {"stop_reason": "completed"})

    assert subscription.get_nowait().event_type is RunEventType.TEXT_DELTA
    assert subscription.get_nowait() == terminal
    with pytest.raises(SubscriptionClosed):
        subscription.get_nowait()
    with pytest.raises(RunEventTerminalError, match="already terminal"):
        publisher.publish(RunEventType.TEXT_DELTA, {"delta": "late"})
    with pytest.raises(RunEventTerminalError):
        publisher.publish(RunEventType.ERROR, {"code": "LATE", "message": "duplicate"})


def test_slow_consumer_is_disconnected_without_blocking_other_consumers():
    bus = RunEventBus(max_buffer_size=2)
    slow = bus.subscribe(run_id="run-1", attempt=1, buffer_size=1)
    fast = bus.subscribe(run_id="run-1", attempt=1, buffer_size=2)
    publisher = _publisher(bus)

    publisher.publish(RunEventType.RUN_PHASE, {"phase": "accepted"})
    assert fast.get_nowait().sequence == 1
    second = publisher.publish(RunEventType.RUN_PHASE, {"phase": "model"})

    with pytest.raises(SlowConsumerError, match="buffer limit exceeded"):
        slow.get_nowait()
    assert slow.cancelled is True and slow.qsize() == 0
    assert fast.get_nowait() == second
    third = publisher.publish(RunEventType.RUN_PHASE, {"phase": "tools"})
    assert fast.get_nowait() == third


def test_subscription_cancel_wakes_waiter_but_does_not_cancel_run_stream():
    bus = RunEventBus()
    cancelled = bus.subscribe(run_id="run-1", attempt=1)
    active = bus.subscribe(run_id="run-1", attempt=1)
    waiting = threading.Event()

    def wait_for_cancel() -> str:
        waiting.set()
        with pytest.raises(SubscriptionCancelled, match="consumer"):
            cancelled.get(timeout=2)
        return "cancelled"

    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(wait_for_cancel)
        assert waiting.wait(timeout=1)
        cancelled.cancel()
        assert result.result(timeout=1) == "cancelled"

    event = _publisher(bus).publish(RunEventType.RUN_PHASE, {"phase": "accepted"})
    assert active.get_nowait() == event


def test_event_bus_is_future_only_and_has_no_replay_queue():
    bus = RunEventBus()
    publisher = _publisher(bus)
    assert publisher.publish(RunEventType.RUN_PHASE, {"phase": "accepted"}).sequence == 1

    late = bus.subscribe(run_id="run-1", attempt=1)
    with pytest.raises(TimeoutError):
        late.get_nowait()
    current = publisher.publish(RunEventType.RUN_PHASE, {"phase": "planning"})
    assert late.get_nowait() == current
    assert current.sequence == 2


def test_event_bus_bounds_active_subscriptions_and_stream_state():
    bus = RunEventBus(max_streams=1, max_subscriptions=1)
    subscription = bus.subscribe(run_id="run-1", attempt=1)
    with pytest.raises(RunEventCapacityError, match="subscription limit"):
        bus.subscribe(run_id="run-1", attempt=1)
    subscription.cancel()

    publisher = _publisher(bus)
    publisher.publish(RunEventType.COMPLETED, {"stop_reason": "completed"})
    with pytest.raises(RunEventCapacityError, match="stream limit"):
        _publisher(bus, run_id="run-2", writer_id="worker-2")
    with pytest.raises(RunEventTerminalError):
        publisher.publish(RunEventType.TEXT_DELTA, {"delta": "late"})


def test_event_bus_is_not_a_trace_repository_source(tmp_path):
    state = StateStore(tmp_path / "state.db")
    state.ensure_session(
        "session-1",
        actor_id="alice",
        tenant_id="school-a",
        role="teacher",
    )
    context = RunContext.create(
        session_id="session-1",
        run_id="run-1",
        actor_id="alice",
        tenant_id="school-a",
        role="teacher",
    )
    state.enqueue_run(context, request_text="fake event boundary")
    repository = TraceRepository(state)
    before = repository.list_events(
        actor_id="alice",
        tenant_id="school-a",
        run_id="run-1",
    ).events

    publisher = _publisher(RunEventBus())
    publisher.publish(RunEventType.RUN_PHASE, {"phase": "accepted"})
    publisher.publish(RunEventType.COMPLETED, {"stop_reason": "completed"})
    after = repository.list_events(
        actor_id="alice",
        tenant_id="school-a",
        run_id="run-1",
    ).events

    assert [event.event_id for event in after] == [event.event_id for event in before]
    assert all(event.schema_version == SCHEMA_VERSION for event in after)


def test_runtime_event_v1_schema_remains_query_export_contract():
    event = RuntimeEvent(
        event_id="trace-1",
        timestamp=datetime.now(UTC).isoformat(),
        sequence=1,
        run_id="run-1",
        root_run_id="run-1",
        parent_run_id=None,
        session_id="session-1",
        actor_id="alice",
        tenant_id="school-a",
        component="runtime",
        event_type="run.started",
    )

    assert SCHEMA_VERSION == "edu-agent.runtime-event.v1"
    assert RUN_EVENT_SCHEMA_VERSION == "edu-agent.run-event.v2"
    assert EventBus is RunEventBus
    assert event.to_dict()["schema_version"] == SCHEMA_VERSION
