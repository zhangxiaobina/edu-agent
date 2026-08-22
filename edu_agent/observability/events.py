from __future__ import annotations

import copy
import json
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from .redaction import RedactionPolicy

# RuntimeEvent v1 is the durable-state Trace projection and remains the public
# query/export schema. RunEvent v2 is the transient in-process transport schema.
SCHEMA_VERSION = "edu-agent.runtime-event.v1"
RUNTIME_EVENT_SCHEMA_VERSION = SCHEMA_VERSION
RUN_EVENT_SCHEMA_VERSION = "edu-agent.run-event.v2"
_SEQUENCE_ALLOCATOR_UNSET = object()


@dataclass(frozen=True)
class RuntimeEvent:
    """Versioned, owner-scoped event projected from persistent runtime state."""

    event_id: str
    timestamp: str
    sequence: int
    run_id: str | None
    root_run_id: str | None
    parent_run_id: str | None
    session_id: str | None
    actor_id: str
    tenant_id: str
    component: str
    event_type: str
    status: str | None = None
    duration_ms: float | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RunPhase(str, Enum):
    ACCEPTED = "accepted"
    PLANNING = "planning"
    MODEL = "model"
    TOOLS = "tools"
    VERIFYING = "verifying"
    FINALIZING = "finalizing"
    TERMINAL = "terminal"


class RunEventType(str, Enum):
    RUN_PHASE = "run.phase"
    TEXT_DELTA = "text.delta"
    TOOL_CALL_DELTA = "tool_call.delta"
    USAGE = "usage"
    PLAN_UPDATED = "plan.updated"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    CONTEXT_COMPACTED = "context.compacted"
    FALLBACK_ACTIVATED = "fallback.activated"
    COMPLETED = "completed"
    ERROR = "error"


TERMINAL_RUN_EVENT_TYPES = frozenset({
    RunEventType.COMPLETED,
    RunEventType.ERROR,
})
DELTA_RUN_EVENT_TYPES = frozenset({
    RunEventType.TEXT_DELTA,
    RunEventType.TOOL_CALL_DELTA,
})


class RunEventValidationError(ValueError):
    """The event envelope or typed payload does not match RunEvent v2."""


class RunEventProtocolError(RuntimeError):
    """Base class for in-process publication protocol failures."""


class RunEventBusClosed(RunEventProtocolError):
    pass


class RunEventCapacityError(RunEventProtocolError):
    pass


class RunEventWriterRejected(RunEventProtocolError):
    pass


class RunEventTerminalError(RunEventProtocolError):
    pass


class SubscriptionClosed(RunEventProtocolError):
    pass


class SubscriptionCancelled(SubscriptionClosed):
    pass


class SlowConsumerError(SubscriptionCancelled):
    pass


def _non_empty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RunEventValidationError(f"{field_name} must be a non-empty string")
    return value


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RunEventValidationError(f"{field_name} must be a positive integer")
    return value


def _non_negative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RunEventValidationError(f"{field_name} must be a non-negative integer")
    return value


def _validate_timestamp(value: Any) -> str:
    timestamp = _non_empty_string(value, "timestamp")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise RunEventValidationError("timestamp must be an ISO-8601 datetime") from error
    if parsed.utcoffset() is None:
        raise RunEventValidationError("timestamp must include a timezone offset")
    return parsed.astimezone(UTC).isoformat()


def _require_payload_string(payload: Mapping[str, Any], name: str) -> None:
    _non_empty_string(payload.get(name), f"payload.{name}")


def _validate_typed_payload(event_type: RunEventType, payload: Mapping[str, Any]) -> None:
    if event_type is RunEventType.RUN_PHASE:
        try:
            RunPhase(payload.get("phase"))
        except (TypeError, ValueError) as error:
            allowed = ", ".join(phase.value for phase in RunPhase)
            raise RunEventValidationError(f"payload.phase must be one of: {allowed}") from error
    elif event_type is RunEventType.TEXT_DELTA:
        _require_payload_string(payload, "delta")
    elif event_type is RunEventType.TOOL_CALL_DELTA:
        if "delta" not in payload:
            raise RunEventValidationError("payload.delta is required")
        call_id = payload.get("tool_call_id")
        index = payload.get("index")
        valid_call_id = isinstance(call_id, str) and bool(call_id.strip())
        valid_index = isinstance(index, int) and not isinstance(index, bool) and index >= 0
        if not valid_call_id and not valid_index:
            raise RunEventValidationError(
                "tool_call.delta requires payload.tool_call_id or a non-negative payload.index"
            )
    elif event_type is RunEventType.USAGE:
        if not payload:
            raise RunEventValidationError("usage payload must not be empty")
    elif event_type is RunEventType.PLAN_UPDATED:
        _require_payload_string(payload, "plan_id")
    elif event_type in {RunEventType.TOOL_STARTED, RunEventType.TOOL_COMPLETED}:
        _require_payload_string(payload, "tool_call_id")
        _require_payload_string(payload, "tool_name")
    elif event_type is RunEventType.CONTEXT_COMPACTED:
        _require_payload_string(payload, "checkpoint_id")
    elif event_type is RunEventType.FALLBACK_ACTIVATED:
        _require_payload_string(payload, "from_route")
        _require_payload_string(payload, "to_route")
        _require_payload_string(payload, "reason")
    elif event_type is RunEventType.ERROR:
        _require_payload_string(payload, "code")
        _require_payload_string(payload, "message")


@dataclass(frozen=True)
class RunEvent:
    """Typed, redacted event transported by RunEventBus within one process."""

    event_type: RunEventType
    run_id: str
    session_id: str
    attempt: int
    sequence: int
    timestamp: str
    writer_id: str
    fencing_token: int
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    schema_version: str = RUN_EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RUN_EVENT_SCHEMA_VERSION:
            raise RunEventValidationError(
                f"schema_version must be {RUN_EVENT_SCHEMA_VERSION!r}"
            )
        try:
            event_type = RunEventType(self.event_type)
        except (TypeError, ValueError) as error:
            raise RunEventValidationError(f"unsupported run event type: {self.event_type!r}") from error
        object.__setattr__(self, "event_type", event_type)
        _non_empty_string(self.event_id, "event_id")
        _non_empty_string(self.run_id, "run_id")
        _non_empty_string(self.session_id, "session_id")
        _non_negative_int(self.attempt, "attempt")
        _positive_int(self.sequence, "sequence")
        object.__setattr__(self, "timestamp", _validate_timestamp(self.timestamp))
        _non_empty_string(self.writer_id, "writer_id")
        _non_negative_int(self.fencing_token, "fencing_token")
        if not isinstance(self.payload, Mapping):
            raise RunEventValidationError("payload must be an object")
        if any(not isinstance(key, str) for key in self.payload):
            raise RunEventValidationError("payload keys must be strings")
        redacted = RedactionPolicy().redact(copy.deepcopy(dict(self.payload)))
        try:
            json.dumps(redacted, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise RunEventValidationError("payload must contain only finite JSON values") from error
        object.__setattr__(self, "payload", redacted)
        _validate_typed_payload(event_type, redacted)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "schema_version": self.schema_version,
            "event_type": self.event_type.value,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "attempt": self.attempt,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "writer_id": self.writer_id,
            "fencing_token": self.fencing_token,
            "payload": copy.deepcopy(self.payload),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RunEvent:
        if not isinstance(value, Mapping):
            raise RunEventValidationError("run event must be an object")
        if any(not isinstance(key, str) for key in value):
            raise RunEventValidationError("run event field names must be strings")
        required = {
            "event_id", "schema_version", "event_type", "run_id", "session_id",
            "attempt", "sequence", "timestamp", "writer_id", "fencing_token", "payload",
        }
        missing = sorted(required - set(value))
        unknown = sorted(set(value) - required)
        if missing:
            raise RunEventValidationError(f"missing run event fields: {', '.join(missing)}")
        if unknown:
            raise RunEventValidationError(f"unknown run event fields: {', '.join(unknown)}")
        try:
            return cls(**dict(value))
        except TypeError as error:
            raise RunEventValidationError(str(error)) from error


@dataclass
class _RunEventStreamState:
    session_id: str
    writer_id: str
    fencing_token: int
    sequence: int = 0
    terminal_event_type: RunEventType | None = None
    sequence_allocator: Callable[[int, RunEventType], int] | None = field(
        default=None,
        repr=False,
    )


class RunEventSubscription(Iterator[RunEvent]):
    """A bounded future-only subscription. Cancellation never cancels the run."""

    def __init__(
        self,
        bus: RunEventBus,
        *,
        subscription_id: str,
        run_id: str,
        attempt: int,
        buffer_size: int,
    ):
        self._bus = bus
        self.subscription_id = subscription_id
        self.run_id = run_id
        self.attempt = attempt
        self.buffer_size = buffer_size
        self._buffer: deque[RunEvent] = deque()
        self._condition = threading.Condition(bus._lock)
        self._cancel_error: type[SubscriptionCancelled] | None = None
        self._close_reason: str | None = None
        self._terminal = False

    @property
    def cancelled(self) -> bool:
        with self._bus._lock:
            return self._cancel_error is not None

    @property
    def close_reason(self) -> str | None:
        with self._bus._lock:
            return self._close_reason

    def qsize(self) -> int:
        with self._bus._lock:
            return len(self._buffer)

    def get(self, timeout: float | None = None) -> RunEvent:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative")
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while True:
                if self._cancel_error is not None:
                    raise self._cancel_error(self._close_reason or "subscription cancelled")
                if self._buffer:
                    return self._buffer.popleft()
                if self._terminal:
                    raise SubscriptionClosed(self._close_reason or "run event stream is terminal")
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("timed out waiting for a run event")
                self._condition.wait(remaining)

    def get_nowait(self) -> RunEvent:
        return self.get(timeout=0)

    def cancel(self) -> None:
        self._bus._cancel_subscription(
            self,
            reason="subscription cancelled by consumer",
            error_type=SubscriptionCancelled,
        )

    def __next__(self) -> RunEvent:
        try:
            return self.get()
        except SlowConsumerError:
            raise
        except SubscriptionClosed as error:
            raise StopIteration from error

    def __enter__(self) -> RunEventSubscription:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.cancel()

    def _offer_locked(self, event: RunEvent) -> None:
        if self._cancel_error is not None or self._terminal:
            return
        if len(self._buffer) >= self.buffer_size:
            self._cancel_locked(
                reason=f"subscriber buffer limit exceeded ({self.buffer_size})",
                error_type=SlowConsumerError,
            )
            return
        self._buffer.append(event)
        self._condition.notify_all()

    def _mark_terminal_locked(self) -> None:
        if self._cancel_error is not None:
            return
        self._terminal = True
        self._close_reason = "run event stream reached completed/error"
        self._condition.notify_all()

    def _cancel_locked(
        self,
        *,
        reason: str,
        error_type: type[SubscriptionCancelled],
    ) -> None:
        if self._cancel_error is not None or self._terminal:
            return
        self._cancel_error = error_type
        self._close_reason = reason
        self._buffer.clear()
        self._bus._subscriptions.pop(self.subscription_id, None)
        self._condition.notify_all()


class RunEventPublisher:
    """Writer identity bound to one run/attempt stream."""

    def __init__(
        self,
        bus: RunEventBus,
        *,
        run_id: str,
        session_id: str,
        attempt: int,
        writer_id: str,
        fencing_token: int,
    ):
        self._bus = bus
        self.run_id = run_id
        self.session_id = session_id
        self.attempt = attempt
        self.writer_id = writer_id
        self.fencing_token = fencing_token
        self._closed = False
        self._guard = threading.Lock()

    def publish(
        self,
        event_type: RunEventType | str,
        payload: Mapping[str, Any] | None = None,
        *,
        timestamp: datetime | str | None = None,
    ) -> RunEvent:
        with self._guard:
            if self._closed:
                raise RunEventWriterRejected("publisher is closed")
            return self._bus.publish(
                event_type,
                run_id=self.run_id,
                session_id=self.session_id,
                attempt=self.attempt,
                writer_id=self.writer_id,
                fencing_token=self.fencing_token,
                payload=payload,
                timestamp=timestamp,
            )

    def close(self) -> None:
        with self._guard:
            self._closed = True

    def __enter__(self) -> RunEventPublisher:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class RunEventBus:
    """Bounded process-local fan-out with per-run/attempt writer fencing.

    The bus intentionally has no event history, database writes, replay API, or
    recovery cursor. A future RunJournal may seed ``sequence_start`` when a
    publisher is opened after recovery; it remains the durable cursor owner.
    """

    def __init__(
        self,
        *,
        max_buffer_size: int = 128,
        max_streams: int = 4096,
        max_subscriptions: int = 1024,
        redaction: RedactionPolicy | None = None,
    ):
        for name, value in (
            ("max_buffer_size", max_buffer_size),
            ("max_streams", max_streams),
            ("max_subscriptions", max_subscriptions),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.max_buffer_size = max_buffer_size
        self.max_streams = max_streams
        self.max_subscriptions = max_subscriptions
        self.redaction = redaction or RedactionPolicy()
        self._lock = threading.RLock()
        self._streams: dict[tuple[str, int], _RunEventStreamState] = {}
        self._subscriptions: dict[str, RunEventSubscription] = {}
        self._closed = False

    def publisher(
        self,
        *,
        run_id: str,
        session_id: str,
        attempt: int,
        writer_id: str,
        fencing_token: int,
        sequence_start: int | None = None,
        sequence_allocator: Callable[[int, RunEventType], int] | None = None,
    ) -> RunEventPublisher:
        _non_empty_string(run_id, "run_id")
        _non_empty_string(session_id, "session_id")
        _non_negative_int(attempt, "attempt")
        _non_empty_string(writer_id, "writer_id")
        _non_negative_int(fencing_token, "fencing_token")
        if sequence_start is not None and (
            isinstance(sequence_start, bool)
            or not isinstance(sequence_start, int)
            or sequence_start < 0
        ):
            raise RunEventValidationError("sequence_start must be a non-negative integer")
        with self._lock:
            self._ensure_open_locked()
            self._activate_writer_locked(
                run_id=run_id,
                session_id=session_id,
                attempt=attempt,
                writer_id=writer_id,
                fencing_token=fencing_token,
                sequence_start=sequence_start,
                sequence_allocator=sequence_allocator,
            )
        return RunEventPublisher(
            self,
            run_id=run_id,
            session_id=session_id,
            attempt=attempt,
            writer_id=writer_id,
            fencing_token=fencing_token,
        )

    open_publisher = publisher

    def publish(
        self,
        event_type: RunEventType | str,
        *,
        run_id: str,
        session_id: str,
        attempt: int,
        writer_id: str,
        fencing_token: int,
        payload: Mapping[str, Any] | None = None,
        timestamp: datetime | str | None = None,
    ) -> RunEvent:
        _non_empty_string(run_id, "run_id")
        _non_empty_string(session_id, "session_id")
        _non_negative_int(attempt, "attempt")
        _non_empty_string(writer_id, "writer_id")
        _non_negative_int(fencing_token, "fencing_token")
        try:
            typed_event = RunEventType(event_type)
        except (TypeError, ValueError) as error:
            raise RunEventValidationError(f"unsupported run event type: {event_type!r}") from error
        if payload is not None and not isinstance(payload, Mapping):
            raise RunEventValidationError("payload must be an object")
        if payload is not None and any(not isinstance(key, str) for key in payload):
            raise RunEventValidationError("payload keys must be strings")
        event_timestamp = self._event_timestamp(timestamp)
        with self._lock:
            self._ensure_open_locked()
            state = self._activate_writer_locked(
                run_id=run_id,
                session_id=session_id,
                attempt=attempt,
                writer_id=writer_id,
                fencing_token=fencing_token,
                sequence_start=None,
                sequence_allocator=_SEQUENCE_ALLOCATOR_UNSET,
            )
            if state.terminal_event_type is not None:
                raise RunEventTerminalError(
                    f"run {run_id} attempt {attempt} is already terminal "
                    f"({state.terminal_event_type.value})"
                )
            redacted_payload = self.redaction.redact(copy.deepcopy(dict(payload or {})))
            sequence = state.sequence + 1
            if state.sequence_allocator is not None:
                try:
                    sequence = state.sequence_allocator(state.sequence, typed_event)
                except RunEventProtocolError:
                    raise
                except Exception as error:
                    raise RunEventWriterRejected(
                        "persistent stream sequence or fence allocation was rejected"
                    ) from error
                if (
                    isinstance(sequence, bool)
                    or not isinstance(sequence, int)
                    or sequence <= state.sequence
                ):
                    raise RunEventValidationError(
                        "persistent sequence allocator did not advance monotonically"
                    )
            event = RunEvent(
                event_type=typed_event,
                run_id=run_id,
                session_id=session_id,
                attempt=attempt,
                sequence=sequence,
                timestamp=event_timestamp,
                writer_id=writer_id,
                fencing_token=fencing_token,
                payload=redacted_payload,
            )
            state.sequence = event.sequence
            matching = [
                subscription
                for subscription in tuple(self._subscriptions.values())
                if subscription.run_id == run_id and subscription.attempt == attempt
            ]
            for subscription in matching:
                subscription._offer_locked(event)
            if typed_event in TERMINAL_RUN_EVENT_TYPES:
                state.terminal_event_type = typed_event
                for subscription in matching:
                    subscription._mark_terminal_locked()
                    self._subscriptions.pop(subscription.subscription_id, None)
            return event

    def subscribe(
        self,
        *,
        run_id: str,
        attempt: int,
        buffer_size: int | None = None,
    ) -> RunEventSubscription:
        _non_empty_string(run_id, "run_id")
        _non_negative_int(attempt, "attempt")
        capacity = self.max_buffer_size if buffer_size is None else buffer_size
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("buffer_size must be a positive integer")
        if capacity > self.max_buffer_size:
            raise ValueError("buffer_size cannot exceed max_buffer_size")
        with self._lock:
            self._ensure_open_locked()
            subscription = RunEventSubscription(
                self,
                subscription_id=uuid.uuid4().hex,
                run_id=run_id,
                attempt=attempt,
                buffer_size=capacity,
            )
            state = self._streams.get((run_id, attempt))
            if state is not None and state.terminal_event_type is not None:
                subscription._mark_terminal_locked()
            else:
                if len(self._subscriptions) >= self.max_subscriptions:
                    raise RunEventCapacityError(
                        f"active subscription limit reached ({self.max_subscriptions})"
                    )
                self._subscriptions[subscription.subscription_id] = subscription
            return subscription

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for subscription in tuple(self._subscriptions.values()):
                subscription._cancel_locked(
                    reason="run event bus is closed",
                    error_type=SubscriptionCancelled,
                )
            self._subscriptions.clear()
            self._streams.clear()

    def _cancel_subscription(
        self,
        subscription: RunEventSubscription,
        *,
        reason: str,
        error_type: type[SubscriptionCancelled],
    ) -> None:
        with self._lock:
            subscription._cancel_locked(reason=reason, error_type=error_type)

    def _ensure_open_locked(self) -> None:
        if self._closed:
            raise RunEventBusClosed("run event bus is closed")

    def _activate_writer_locked(
        self,
        *,
        run_id: str,
        session_id: str,
        attempt: int,
        writer_id: str,
        fencing_token: int,
        sequence_start: int | None,
        sequence_allocator: Callable[[int, RunEventType], int] | None | object,
    ) -> _RunEventStreamState:
        key = (run_id, attempt)
        state = self._streams.get(key)
        if state is None:
            if len(self._streams) >= self.max_streams:
                raise RunEventCapacityError(
                    f"run event stream limit reached ({self.max_streams})"
                )
            state = _RunEventStreamState(
                session_id=session_id,
                writer_id=writer_id,
                fencing_token=fencing_token,
                sequence=sequence_start or 0,
                sequence_allocator=(
                    None
                    if sequence_allocator is _SEQUENCE_ALLOCATOR_UNSET
                    else sequence_allocator
                ),
            )
            self._streams[key] = state
            return state
        if state.session_id != session_id:
            raise RunEventValidationError("a run/attempt cannot change session_id")
        if sequence_start is not None and sequence_start != state.sequence:
            raise RunEventValidationError(
                f"sequence_start {sequence_start} does not match current sequence {state.sequence}"
            )
        if fencing_token < state.fencing_token:
            raise RunEventWriterRejected(
                f"writer fencing token {fencing_token} is older than {state.fencing_token}"
            )
        if fencing_token == state.fencing_token and writer_id != state.writer_id:
            raise RunEventWriterRejected(
                "a different writer cannot reuse the active fencing token"
            )
        if fencing_token > state.fencing_token:
            if state.terminal_event_type is not None:
                raise RunEventTerminalError(
                    f"terminal run {run_id} attempt {attempt} cannot acquire a new writer"
                )
            state.fencing_token = fencing_token
            state.writer_id = writer_id
            state.sequence_allocator = (
                None
                if sequence_allocator is _SEQUENCE_ALLOCATOR_UNSET
                else sequence_allocator
            )
        elif sequence_allocator is not _SEQUENCE_ALLOCATOR_UNSET:
            state.sequence_allocator = sequence_allocator
        return state

    @staticmethod
    def _event_timestamp(value: datetime | str | None) -> str:
        if value is None:
            return datetime.now(UTC).isoformat()
        if isinstance(value, datetime):
            if value.utcoffset() is None:
                raise RunEventValidationError("timestamp must include a timezone offset")
            return value.isoformat()
        return _validate_timestamp(value)

    def __enter__(self) -> RunEventBus:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


# Short aliases keep the transport vocabulary convenient for callers while
# preserving the explicit RunEvent names used by the roadmap and docs.
RunEventV2 = RunEvent
RunEventV2Type = RunEventType
EventBus = RunEventBus
EventSubscription = RunEventSubscription
