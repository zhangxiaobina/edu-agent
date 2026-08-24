from __future__ import annotations

import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any


class LifecycleState(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    DRAINING = "draining"
    STOPPED = "stopped"


class LifecycleRejected(RuntimeError):
    """New work was offered after this process stopped accepting it."""

    error_code = "PROCESS_NOT_READY"

    def __init__(self, state: LifecycleState):
        self.state = state
        super().__init__(f"process is {state.value} and is not accepting new work")


class LifecycleStartupError(RuntimeError):
    pass


@dataclass(frozen=True)
class LifecycleTransition:
    sequence: int
    from_state: str | None
    to_state: str
    reason: str
    occurred_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "reason": self.reason,
            "occurred_at": self.occurred_at,
        }


@dataclass(frozen=True)
class ShutdownReport:
    state: str
    normal_drained: bool
    cancellation_requested: bool
    recoverable_runs: int
    active_remaining: int
    flush_succeeded: bool
    flush_timed_out: bool
    resource_close_failures: tuple[str, ...]
    elapsed_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "normal_drained": self.normal_drained,
            "cancellation_requested": self.cancellation_requested,
            "recoverable_runs": self.recoverable_runs,
            "active_remaining": self.active_remaining,
            "flush_succeeded": self.flush_succeeded,
            "flush_timed_out": self.flush_timed_out,
            "resource_close_failures": list(self.resource_close_failures),
            "elapsed_seconds": self.elapsed_seconds,
        }


class LifecycleAdmission:
    """An atomic ownership ticket for work accepted before draining."""

    def __init__(self, controller: LifecycleController, admission_id: str, kind: str):
        self._controller = controller
        self._admission_id = admission_id
        self.kind = kind
        self._closed = False
        self._lock = threading.Lock()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def controller(self) -> LifecycleController:
        return self._controller

    def add_cancel_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        if not callable(callback):
            raise TypeError("cancel callback must be callable")
        return self._controller._add_cancel_callback(self._admission_id, callback)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._controller._release(self._admission_id)

    def __enter__(self) -> LifecycleAdmission:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


@dataclass
class _AdmissionRecord:
    kind: str
    accepted_at: float
    cancel_callbacks: dict[str, Callable[[], None]]
    cancel_requested: bool = False


class LifecycleController:
    """Thread-safe, monotonic process lifecycle and admission controller."""

    _ORDER = {
        LifecycleState.STARTING: 0,
        LifecycleState.RUNNING: 1,
        LifecycleState.DRAINING: 2,
        LifecycleState.STOPPED: 3,
    }

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        event_factory: Callable[[], Any] | None = None,
        waiter: Callable[[Any, float], bool] | None = None,
        poll_interval_seconds: float = 0.05,
        audit_sink: Callable[[Mapping[str, Any]], None] | None = None,
    ):
        if poll_interval_seconds <= 0:
            raise ValueError("lifecycle poll interval must be positive")
        self._clock = clock or time.monotonic
        self._event_factory = event_factory or threading.Event
        self._waiter = waiter or (lambda event, timeout: bool(event.wait(timeout)))
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._lock = threading.RLock()
        self._changed = self._event_factory()
        self._state = LifecycleState.STARTING
        self._checks = {
            "migration": False,
            "state_db_writable": False,
            "required_providers": False,
        }
        self._provider_policy = "required_local_only_external_model_non_blocking"
        self._admissions: dict[str, _AdmissionRecord] = {}
        self._sequence = 0
        self._history: list[LifecycleTransition] = []
        self._audit_sink = audit_sink
        self._audit_failures = 0
        self._record_initial_state()

    @property
    def state(self) -> LifecycleState:
        with self._lock:
            return self._state

    def now(self) -> float:
        return float(self._clock())

    def set_audit_sink(self, sink: Callable[[Mapping[str, Any]], None]) -> None:
        if not callable(sink):
            raise TypeError("lifecycle audit sink must be callable")
        with self._lock:
            self._audit_sink = sink
            initial = self._history[0].to_dict() if self._history else None
        if initial is not None:
            self._emit_audit(initial, strict=True)

    def _record_initial_state(self) -> None:
        with self._lock:
            self._sequence += 1
            transition = LifecycleTransition(
                sequence=self._sequence,
                from_state=None,
                to_state=LifecycleState.STARTING.value,
                reason="process_initializing",
                occurred_at=self.now(),
            )
            self._history.append(transition)
            sink_present = self._audit_sink is not None
        if sink_present:
            self._emit_audit(transition.to_dict(), strict=False)

    def _emit_audit(self, record: Mapping[str, Any], *, strict: bool) -> None:
        with self._lock:
            sink = self._audit_sink
        if sink is None:
            return
        try:
            sink(dict(record))
        except Exception:
            with self._lock:
                self._audit_failures += 1
            if strict:
                raise

    def set_health(
        self,
        *,
        migration: bool | None = None,
        state_db_writable: bool | None = None,
        required_providers: bool | None = None,
    ) -> None:
        updates = {
            "migration": migration,
            "state_db_writable": state_db_writable,
            "required_providers": required_providers,
        }
        with self._lock:
            for name, value in updates.items():
                if value is not None:
                    self._checks[name] = bool(value)
            self._changed.set()

    def complete_startup(self) -> None:
        with self._lock:
            if self._state is LifecycleState.RUNNING:
                return
            if self._state is not LifecycleState.STARTING:
                raise LifecycleStartupError(
                    f"cannot complete startup from {self._state.value}"
                )
            failed = sorted(name for name, ready in self._checks.items() if not ready)
            if failed:
                raise LifecycleStartupError(
                    "startup health checks failed: " + ", ".join(failed)
                )
            self._transition(
                LifecycleState.RUNNING,
                reason="startup_checks_passed",
                strict_audit=True,
            )

    def fail_startup(self, reason: str = "startup_failed") -> None:
        self._transition(
            LifecycleState.STOPPED,
            reason=reason,
            strict_audit=False,
            idempotent_states=frozenset(
                {
                    LifecycleState.RUNNING,
                    LifecycleState.DRAINING,
                    LifecycleState.STOPPED,
                }
            ),
        )

    def begin_draining(self, reason: str = "shutdown_requested") -> bool:
        return self._transition(
            LifecycleState.DRAINING,
            reason=reason,
            strict_audit=False,
            idempotent_states=frozenset(
                {LifecycleState.DRAINING, LifecycleState.STOPPED}
            ),
        )

    def mark_stopped(self, reason: str = "shutdown_complete") -> bool:
        return self._transition(
            LifecycleState.STOPPED,
            reason=reason,
            strict_audit=False,
            idempotent_states=frozenset({LifecycleState.STOPPED}),
        )

    def _transition(
        self,
        target: LifecycleState,
        *,
        reason: str,
        strict_audit: bool,
        idempotent_states: frozenset[LifecycleState] = frozenset(),
    ) -> bool:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("lifecycle transition reason must be non-empty")
        with self._lock:
            source = self._state
            if source in idempotent_states:
                return False
            if self._ORDER[target] <= self._ORDER[source]:
                raise RuntimeError(
                    f"non-monotonic lifecycle transition {source.value}->{target.value}"
                )
            if source is LifecycleState.STARTING and target not in {
                LifecycleState.RUNNING,
                LifecycleState.DRAINING,
                LifecycleState.STOPPED,
            }:
                raise RuntimeError("invalid lifecycle transition")
            if source is LifecycleState.RUNNING and target is not LifecycleState.DRAINING:
                raise RuntimeError("running can only transition to draining")
            if source is LifecycleState.DRAINING and target is not LifecycleState.STOPPED:
                raise RuntimeError("draining can only transition to stopped")
            self._sequence += 1
            transition = LifecycleTransition(
                sequence=self._sequence,
                from_state=source.value,
                to_state=target.value,
                reason=reason.strip(),
                occurred_at=self.now(),
            )
            if strict_audit and self._audit_sink is not None:
                self._emit_audit(transition.to_dict(), strict=True)
            self._state = target
            self._history.append(transition)
            self._changed.set()
        if not strict_audit:
            self._emit_audit(transition.to_dict(), strict=False)
        return True

    def admit(self, kind: str) -> LifecycleAdmission:
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError("lifecycle admission kind must be non-empty")
        with self._lock:
            if self._state is not LifecycleState.RUNNING:
                raise LifecycleRejected(self._state)
            admission_id = uuid.uuid4().hex
            self._admissions[admission_id] = _AdmissionRecord(
                kind=kind.strip(),
                accepted_at=self.now(),
                cancel_callbacks={},
            )
            self._changed.set()
        return LifecycleAdmission(self, admission_id, kind.strip())

    def assert_admission(self, admission: LifecycleAdmission) -> None:
        if not isinstance(admission, LifecycleAdmission) or admission.controller is not self:
            raise ValueError("lifecycle admission belongs to a different controller")
        with self._lock:
            if admission.closed or admission._admission_id not in self._admissions:
                raise LifecycleRejected(self._state)

    def _add_cancel_callback(
        self,
        admission_id: str,
        callback: Callable[[], None],
    ) -> Callable[[], None]:
        callback_id = uuid.uuid4().hex
        invoke_now = False
        with self._lock:
            record = self._admissions.get(admission_id)
            if record is None:
                return lambda: None
            if record.cancel_requested:
                invoke_now = True
            else:
                record.cancel_callbacks[callback_id] = callback
        if invoke_now:
            callback()
            return lambda: None

        def unregister() -> None:
            with self._lock:
                current = self._admissions.get(admission_id)
                if current is not None:
                    current.cancel_callbacks.pop(callback_id, None)

        return unregister

    def _release(self, admission_id: str) -> None:
        with self._lock:
            self._admissions.pop(admission_id, None)
            self._changed.set()

    def cancel_active(self) -> int:
        callbacks: list[Callable[[], None]] = []
        with self._lock:
            for record in self._admissions.values():
                if record.cancel_requested:
                    continue
                record.cancel_requested = True
                callbacks.extend(record.cancel_callbacks.values())
                record.cancel_callbacks.clear()
            self._changed.set()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                continue
        return len(callbacks)

    def active_count(self) -> int:
        with self._lock:
            return len(self._admissions)

    def active_summary(self) -> dict[str, int]:
        with self._lock:
            return dict(Counter(record.kind for record in self._admissions.values()))

    def wait_for_idle(
        self,
        deadline: float,
        *,
        extra_active: Callable[[], int] | None = None,
    ) -> bool:
        while True:
            with self._lock:
                local_active = len(self._admissions)
                self._changed.clear()
            additional = max(0, int(extra_active())) if extra_active is not None else 0
            if local_active == 0 and additional == 0:
                return True
            remaining = float(deadline) - self.now()
            if remaining <= 0:
                return False
            self._waiter(self._changed, min(self._poll_interval_seconds, remaining))

    def health_snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = self._state
            checks = dict(self._checks)
            active = len(self._admissions)
            audit_ok = self._audit_failures == 0
        ready = state is LifecycleState.RUNNING and all(checks.values()) and audit_ok
        live = state is not LifecycleState.STOPPED
        return {
            "lifecycle": state.value,
            "live": live,
            "ready": ready,
            "checks": {
                **checks,
                "lifecycle_accepting": state is LifecycleState.RUNNING,
                "audit_writable": audit_ok,
            },
            "provider_policy": self._provider_policy,
            "active_work": active,
        }

    def transition_history(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(item.to_dict() for item in self._history)


__all__ = [
    "LifecycleAdmission",
    "LifecycleController",
    "LifecycleRejected",
    "LifecycleStartupError",
    "LifecycleState",
    "LifecycleTransition",
    "ShutdownReport",
]
