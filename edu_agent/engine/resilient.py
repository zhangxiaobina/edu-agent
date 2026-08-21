from __future__ import annotations

import contextlib
import contextvars
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import Enum

from .base import Engine, EngineResponse
from .gateway import ResolvedRoute


class FailureKind(str, Enum):
    CONNECTION = "connection"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    SERVER = "server"
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    INVALID_REQUEST = "invalid_request"
    CONTEXT_OVERFLOW = "context_overflow"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FailureDecision:
    kind: FailureKind
    retryable: bool


def classify_failure(error: Exception) -> FailureDecision:
    name = type(error).__name__
    status = getattr(error, "status_code", None)
    text = str(error).lower()
    if "context" in text and any(word in text for word in ("length", "window", "maximum")):
        return FailureDecision(FailureKind.CONTEXT_OVERFLOW, False)
    if name == "APIConnectionError":
        return FailureDecision(FailureKind.CONNECTION, True)
    if name == "APITimeoutError" or isinstance(error, TimeoutError):
        return FailureDecision(FailureKind.TIMEOUT, True)
    if name == "RateLimitError" or status == 429:
        return FailureDecision(FailureKind.RATE_LIMIT, True)
    if name == "AuthenticationError" or status == 401:
        return FailureDecision(FailureKind.AUTHENTICATION, False)
    if name == "PermissionDeniedError" or status == 403:
        return FailureDecision(FailureKind.PERMISSION, False)
    if name in {"BadRequestError", "UnprocessableEntityError"} or status in {400, 404, 422}:
        return FailureDecision(FailureKind.INVALID_REQUEST, False)
    if name == "InternalServerError" or isinstance(status, int) and status >= 500:
        return FailureDecision(FailureKind.SERVER, True)
    return FailureDecision(FailureKind.UNKNOWN, False)


class CircuitOpenError(RuntimeError):
    pass


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown_seconds = max(0.0, cooldown_seconds)
        self.clock = clock
        self.failures = 0
        self.opened_at: float | None = None
        self.half_open_in_flight = False
        self._lock = threading.Lock()

    def _state_unlocked(self) -> str:
        if self.opened_at is None:
            return "closed"
        if self.clock() - self.opened_at >= self.cooldown_seconds:
            return "half_open"
        return "open"

    @property
    def state(self) -> str:
        with self._lock:
            return self._state_unlocked()

    @property
    def degraded(self) -> bool:
        with self._lock:
            return self.failures > 0 or self.opened_at is not None

    def allow(self) -> bool:
        with self._lock:
            state = self._state_unlocked()
            if state == "closed":
                return True
            if state == "open" or self.half_open_in_flight:
                return False
            self.half_open_in_flight = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self.failures = 0
            self.opened_at = None
            self.half_open_in_flight = False

    def record_failure(self) -> bool:
        with self._lock:
            self.half_open_in_flight = False
            self.failures += 1
            was_open = self.opened_at is not None
            if self.failures >= self.failure_threshold:
                self.opened_at = self.clock()
            return not was_open and self.opened_at is not None


class ResilientEngine(Engine):
    def __init__(
        self,
        primary: Engine,
        *,
        max_retries: int = 2,
        fallback: Engine | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        event_sink: Callable[[dict], None] | None = None,
    ):
        self.primary = primary
        self.fallback = fallback
        self.max_retries = max(0, max_retries)
        self.sleeper = sleeper
        self.breaker = CircuitBreaker(failure_threshold, cooldown_seconds, clock=clock)
        self.event_sink = event_sink
        self.name = f"resilient:{primary.name}"
        self._run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
            "edu_agent_provider_run_id",
            default=None,
        )

    @contextlib.contextmanager
    def runtime_context(self, run_id: str) -> Iterator[None]:
        token = self._run_id.set(run_id)
        try:
            yield
        finally:
            self._run_id.reset(token)

    def begin_turn_routes(self) -> tuple[ResolvedRoute, ...]:
        routes = self.primary.begin_turn_routes()
        if self.fallback is not None:
            routes += self.fallback.begin_turn_routes()
        return routes

    def chat(self, messages: list[dict], tools: list[dict]) -> EngineResponse:
        attempts = 0
        last_error: Exception | None = None
        if self.breaker.allow():
            for attempt in range(self.max_retries + 1):
                attempts += 1
                try:
                    response = self.primary.chat(messages, tools)
                    recovered = self.breaker.degraded
                    self.breaker.record_success()
                    if recovered:
                        self._event("primary_recovered", attempt=attempts)
                    response.usage = {**response.usage, "runtime_attempts": attempts}
                    return response
                except Exception as error:
                    last_error = error
                    decision = classify_failure(error)
                    self._event(
                        "provider_failure",
                        attempt=attempts,
                        error=error,
                        details={"kind": decision.kind.value, "retryable": decision.retryable},
                    )
                    if attempt >= self.max_retries or not decision.retryable:
                        break
                    delay = min(2**attempt, 8)
                    self._event("retry_scheduled", attempt=attempts, details={"delay": delay})
                    self.sleeper(delay)
            assert last_error is not None
            decision = classify_failure(last_error)
            if decision.retryable and self.breaker.record_failure():
                self._event("circuit_opened", attempt=attempts, error=last_error)
        else:
            last_error = CircuitOpenError("primary provider circuit is open")
            self._event("primary_skipped", attempt=0, error=last_error)

        if self.fallback is not None:
            self._event("fallback_activated", attempt=attempts + 1, error=last_error)
            response = self.fallback.chat(messages, tools)
            response.usage = {
                **response.usage,
                "runtime_attempts": attempts + 1,
                "fallback_used": True,
                "primary_failure": classify_failure(last_error).kind.value,
                "circuit_state": self.breaker.state,
            }
            return response
        raise last_error

    def _event(
        self,
        event: str,
        *,
        attempt: int,
        error: Exception | None = None,
        details: dict | None = None,
    ) -> None:
        if self.event_sink is None:
            return
        self.event_sink(
            {
                "run_id": self._run_id.get(),
                "provider": self.primary.name,
                "event": event,
                "attempt": attempt,
                "error_class": type(error).__name__ if error else None,
                "details": details or {},
            }
        )
