from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from ..runtime.cancellation import Cancellation, CancellationToken
from .events import (
    RunEvent,
    RunEventBus,
    RunEventPublisher,
    RunEventTerminalError,
    RunEventType,
    RunEventWriterRejected,
)


def _event_type_value(event: Any) -> str:
    value = getattr(event, "event_type", "")
    return str(getattr(value, "value", value))


def _route_label(route: Any) -> str:
    if isinstance(route, Mapping):
        payload = dict(route)
    else:
        to_event = getattr(route, "to_event", None)
        payload = to_event() if callable(to_event) else {}
    if payload:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    identity = getattr(route, "identity", None)
    return str(identity or route or "unknown-route")


class RunStreamWriter:
    """The only producer-side writer for one API run attempt.

    Producers may call this object concurrently. Publication, owner takeover,
    cancellation and terminal transition are serialized under one lock before
    the existing RunEventBus assigns sequence numbers.
    """

    def __init__(
        self,
        bus: RunEventBus,
        *,
        run_id: str,
        attempt: int,
        cancellation_token: CancellationToken,
        writer_id: str | None = None,
        sequence_reserver: Callable[..., int] | None = None,
    ):
        self.bus = bus
        self.run_id = run_id
        self.attempt = int(attempt)
        self.cancellation_token = cancellation_token
        self.writer_id = writer_id or uuid.uuid4().hex
        self._sequence_reserver = sequence_reserver
        self._lock = threading.RLock()
        self._publisher: RunEventPublisher | None = None
        self._session_id: str | None = None
        self._fencing_token = -1
        self._terminal = False
        self._aborted = False
        self._producer_open = True
        self._provider_attempt = 0
        self._provider_visible = False
        self._provider_route: Any = None
        self._pending_cancellation: Cancellation | None = None
        self._terminal_replay = False
        self._unregister_cancel = cancellation_token.register(self.cancel)

    @property
    def bound(self) -> bool:
        with self._lock:
            return self._publisher is not None

    @property
    def terminal(self) -> bool:
        with self._lock:
            return self._terminal

    @property
    def fencing_token(self) -> int | None:
        with self._lock:
            return self._fencing_token if self._fencing_token >= 0 else None

    def bind(
        self,
        *,
        session_id: str,
        fencing_token: int,
        sequence_start: int | None = None,
        terminal_replay: bool = False,
    ) -> None:
        pending: Cancellation | None = None
        with self._lock:
            if self._aborted:
                raise RunEventWriterRejected("stream writer is aborted")
            if self._terminal:
                raise RunEventTerminalError("stream writer is terminal")
            if self._session_id is not None and self._session_id != session_id:
                raise RunEventWriterRejected("stream writer cannot change session_id")
            if fencing_token < self._fencing_token:
                raise RunEventWriterRejected("stream writer fencing token moved backwards")
            if self._publisher is not None and fencing_token == self._fencing_token:
                if terminal_replay != self._terminal_replay:
                    raise RunEventWriterRejected("stream writer cannot change replay mode")
                return
            sequence_allocator = None
            if self._sequence_reserver is not None:
                def reserve_sequence(current, event_type):
                    return self._sequence_reserver(
                        run_id=self.run_id,
                        session_id=session_id,
                        fencing_token=fencing_token,
                        current_sequence=current,
                        event_type=event_type.value,
                        terminal_replay=terminal_replay,
                    )

                sequence_allocator = reserve_sequence
            publisher = self.bus.publisher(
                run_id=self.run_id,
                session_id=session_id,
                attempt=self.attempt,
                writer_id=self.writer_id,
                fencing_token=fencing_token,
                sequence_start=sequence_start,
                sequence_allocator=sequence_allocator,
            )
            previous = self._publisher
            self._publisher = publisher
            self._session_id = session_id
            self._fencing_token = fencing_token
            self._terminal_replay = terminal_replay
            if previous is not None:
                previous.close()
            pending = self._pending_cancellation
            self._pending_cancellation = None
        if pending is not None:
            self.cancel(pending)

    def publish(
        self,
        event_type: RunEventType | str,
        payload: Mapping[str, Any] | None = None,
    ) -> RunEvent:
        with self._lock:
            if not self._producer_open or self._aborted:
                raise RunEventWriterRejected("stream writer no longer accepts producer events")
            self.cancellation_token.checkpoint("stream.before_publish")
            return self._publish_locked(event_type, payload)

    def provider_event(self, event: Any) -> RunEvent | None:
        with self._lock:
            if not self._producer_open or self._aborted:
                raise RunEventWriterRejected("stream writer no longer accepts provider events")
            self.cancellation_token.checkpoint("provider.stream.before_publish")
            event_type = _event_type_value(event)
            if event_type == "ignored":
                return None
            provider_attempt = int(getattr(event, "attempt", 0) or 0)
            if provider_attempt < self._provider_attempt:
                return None
            if provider_attempt > self._provider_attempt:
                if self._provider_visible:
                    raise RunEventWriterRejected(
                        "provider attempt changed after a visible delta"
                    )
                self._provider_attempt = provider_attempt
                self._provider_route = getattr(event, "route", None)
            common = {
                "provider_attempt": provider_attempt,
                "provider_event_id": getattr(event, "provider_event_id", None),
            }
            continuation = getattr(event, "continuation", None)
            if event_type == "error" and continuation in {"retry", "fallback"}:
                if self._provider_visible:
                    raise RunEventWriterRejected(
                        "provider continuation is forbidden after a visible delta"
                    )
                published = None
                if continuation == "fallback":
                    metadata = dict(getattr(event, "metadata", {}) or {})
                    published = self._publish_locked(
                        RunEventType.FALLBACK_ACTIVATED,
                        {
                            "from_route": _route_label(getattr(event, "route", None)),
                            "to_route": _route_label(metadata.get("fallback_route")),
                            "reason": str(
                                metadata.get("failure_kind") or "provider_failure"
                            ),
                            **common,
                        },
                    )
                self._provider_attempt = provider_attempt + 1
                self._provider_visible = False
                self._provider_route = None
                return published
            if event_type == "text.delta":
                self._provider_visible = True
                return self._publish_locked(
                    RunEventType.TEXT_DELTA,
                    {"delta": getattr(event, "delta", ""), **common},
                )
            if event_type.startswith("tool_call."):
                self._provider_visible = True
                kind = event_type.removeprefix("tool_call.").removesuffix(".delta")
                return self._publish_locked(
                    RunEventType.TOOL_CALL_DELTA,
                    {
                        "index": int(getattr(event, "tool_call_index", 0) or 0),
                        "delta": {"kind": kind, "value": getattr(event, "delta", "")},
                        **common,
                    },
                )
            if event_type == "usage":
                return self._publish_locked(
                    RunEventType.USAGE,
                    {**dict(getattr(event, "usage", {}) or {}), **common},
                )
            return None

    def complete(self, payload: Mapping[str, Any] | None = None) -> RunEvent:
        return self._terminate(RunEventType.COMPLETED, payload or {"stop_reason": "completed"})

    def fail(self, *, code: str, message: str, retryable: bool = False) -> RunEvent:
        return self._terminate(
            RunEventType.ERROR,
            {"code": code, "message": message, "retryable": bool(retryable)},
        )

    def cancel(self, cancellation: Cancellation) -> None:
        code = {
            "client_disconnect": "CLIENT_DISCONNECTED",
            "deadline": "DEADLINE_EXCEEDED",
        }.get(cancellation.source, "CANCELLED")
        with self._lock:
            self._producer_open = False
            if self._terminal or self._aborted:
                return
            if self._publisher is None:
                self._pending_cancellation = cancellation
                return
            try:
                self._terminate_locked(
                    RunEventType.ERROR,
                    {
                        "code": code,
                        "message": cancellation.reason,
                        "retryable": cancellation.source != "explicit",
                    },
                )
            except (RunEventTerminalError, RunEventWriterRejected):
                return

    def abort(self) -> None:
        with self._lock:
            self._aborted = True
            self._producer_open = False
            publisher = self._publisher
            self._publisher = None
            if publisher is not None:
                publisher.close()
        self._unregister_cancel()

    def close(self) -> None:
        with self._lock:
            self._producer_open = False
            publisher = self._publisher
            if publisher is not None:
                publisher.close()
        self._unregister_cancel()

    def _publish_locked(
        self,
        event_type: RunEventType | str,
        payload: Mapping[str, Any] | None,
    ) -> RunEvent:
        if self._terminal:
            raise RunEventTerminalError("stream writer is terminal")
        if self._publisher is None:
            raise RunEventWriterRejected("stream writer has not acquired a fencing token")
        return self._publisher.publish(event_type, payload)

    def _terminate(
        self,
        event_type: RunEventType,
        payload: Mapping[str, Any],
    ) -> RunEvent:
        with self._lock:
            return self._terminate_locked(event_type, payload)

    def _terminate_locked(
        self,
        event_type: RunEventType,
        payload: Mapping[str, Any],
    ) -> RunEvent:
        if self._terminal:
            raise RunEventTerminalError("stream writer is already terminal")
        if self._publisher is None:
            raise RunEventWriterRejected("stream writer has not acquired a fencing token")
        event = self._publisher.publish(event_type, payload)
        self._terminal = True
        self._producer_open = False
        return event


class RunStreamWriterRegistry:
    """Fence old API attempts while retaining one writer per active run."""

    def __init__(self, bus: RunEventBus):
        self.bus = bus
        self._lock = threading.Lock()
        self._writers: dict[str, RunStreamWriter] = {}
        self._attempts: dict[str, int] = {}

    def open(
        self,
        *,
        run_id: str,
        attempt: int,
        writer_id: str,
        cancellation_token: CancellationToken,
        sequence_reserver: Callable[..., int] | None = None,
    ) -> RunStreamWriter:
        replaced: RunStreamWriter | None = None
        with self._lock:
            current_attempt = self._attempts.get(run_id, -1)
            if attempt < current_attempt:
                raise RunEventWriterRejected("stream attempt is older than the active attempt")
            current = self._writers.get(run_id)
            if current is not None:
                if attempt == current_attempt:
                    raise RunEventWriterRejected("run already has a stream writer for this attempt")
                current.abort()
                replaced = current
            writer = RunStreamWriter(
                self.bus,
                run_id=run_id,
                attempt=attempt,
                writer_id=writer_id,
                cancellation_token=cancellation_token,
                sequence_reserver=sequence_reserver,
            )
            self._writers[run_id] = writer
            self._attempts[run_id] = attempt
        if replaced is not None:
            replaced.cancellation_token.cancel(
                "stream writer was replaced by a newer attempt",
                source="owner_replaced",
            )
        return writer

    def release(self, writer: RunStreamWriter) -> None:
        with self._lock:
            if self._writers.get(writer.run_id) is writer:
                self._writers.pop(writer.run_id, None)
        writer.close()

    def close(self) -> None:
        with self._lock:
            writers = tuple(self._writers.values())
            self._writers.clear()
        for writer in writers:
            writer.abort()
