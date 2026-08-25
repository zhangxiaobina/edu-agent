from __future__ import annotations

import contextlib
import contextvars
import copy
import math
import random as random_module
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Generator, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any

import httpx

from ..runtime.cancellation import (
    CancellationRequested,
    CancellationToken,
    accepts_cancellation_token,
    accepts_keyword_argument,
    call_with_cancellation,
)
from .base import Engine, EngineResponse
from .gateway import (
    ApiMode,
    ProviderCapabilities,
    ProviderCapabilityError,
    ProviderRequestRequirements,
    ResolvedRoute,
    RouteIdentity,
    capability_gaps,
    infer_request_requirements,
)
from .streaming import (
    ProviderStreamAggregator,
    ProviderStreamEvent,
    ProviderStreamEventType,
    ProviderStreamProtocolError,
    aggregate_provider_stream,
    provider_stream_error_event,
)


class FailureKind(str, Enum):
    CONNECTION = "connection"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    SERVER = "server"
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    INVALID_REQUEST = "invalid_request"
    CONTEXT_OVERFLOW = "context_overflow"
    OUTPUT_CAP = "output_cap"
    CIRCUIT_OPEN = "circuit_open"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FailureDecision:
    kind: FailureKind
    retryable: bool
    status_code: int | None = None

    @property
    def fallback_allowed(self) -> bool:
        return self.kind is FailureKind.CIRCUIT_OPEN or (
            self.retryable
            and self.kind
            in {
                FailureKind.CONNECTION,
                FailureKind.TIMEOUT,
                FailureKind.RATE_LIMIT,
                FailureKind.SERVER,
            }
        )


_CONTEXT_CODES = frozenset(
    {
        "context_length_exceeded",
        "context_window_exceeded",
        "input_too_long",
        "maximum_context_length",
        "prompt_too_long",
    }
)
_OUTPUT_CAP_CODES = frozenset(
    {
        "length_finish_reason",
        "max_output_tokens",
        "max_tokens_exceeded",
        "output_limit_exceeded",
    }
)
_RATE_LIMIT_CODES = frozenset({"rate_limit_exceeded", "too_many_requests"})
_TERMINAL_RATE_LIMIT_CODES = frozenset(
    {"billing_hard_limit_reached", "insufficient_quota", "quota_exceeded"}
)
_AUTHENTICATION_CODES = frozenset({"authentication_error", "invalid_api_key"})
_PERMISSION_CODES = frozenset({"insufficient_permissions", "permission_denied"})
_SERVER_CODES = frozenset({"internal_error", "server_error", "temporarily_unavailable"})


def _status_code(error: Exception) -> int | None:
    for candidate in (
        getattr(error, "status_code", None),
        getattr(getattr(error, "response", None), "status_code", None),
    ):
        if isinstance(candidate, bool):
            continue
        try:
            status = int(candidate)
        except (TypeError, ValueError):
            continue
        if 100 <= status <= 599:
            return status
    return None


def _error_markers(error: Exception) -> set[str]:
    values: list[Any] = [
        getattr(error, "code", None),
        getattr(error, "type", None),
        getattr(error, "response_status", None),
    ]
    body = getattr(error, "body", None)
    if isinstance(body, Mapping):
        nested = body.get("error", body)
        if isinstance(nested, Mapping):
            values.extend((nested.get("code"), nested.get("type")))
    return {
        str(value).strip().lower()
        for value in values
        if isinstance(value, str) and value.strip()
    }


def classify_failure(error: Exception) -> FailureDecision:
    """Classify failures without treating an unknown exception as transient."""
    name = type(error).__name__
    status = _status_code(error)
    markers = _error_markers(error)
    text = str(error).lower()[:4096]

    if name == "CircuitOpenError":
        return FailureDecision(FailureKind.CIRCUIT_OPEN, False, status)
    error_gaps = getattr(error, "gaps", ())
    if isinstance(error, ProviderCapabilityError) and "context_window" in error_gaps:
        return FailureDecision(FailureKind.CONTEXT_OVERFLOW, False, status)
    if isinstance(error, ProviderCapabilityError) and "max_output_tokens" in error_gaps:
        return FailureDecision(FailureKind.OUTPUT_CAP, False, status)
    if name in {"LengthFinishReasonError", "OutputCapError"} or markers & _OUTPUT_CAP_CODES:
        return FailureDecision(FailureKind.OUTPUT_CAP, False, status)
    context_limit_text = (
        "context_length_exceeded" in text
        or ("context window" in text or "context length" in text)
        and any(
            word in text
            for word in ("exceed", "limit", "maximum", "too long", "超过", "超出")
        )
    )
    if markers & _CONTEXT_CODES or context_limit_text:
        return FailureDecision(FailureKind.CONTEXT_OVERFLOW, False, status)
    if (
        name == "APITimeoutError"
        or isinstance(error, (TimeoutError, httpx.TimeoutException))
        or status == 408
    ):
        return FailureDecision(FailureKind.TIMEOUT, True, status)
    if name in {"APIConnectionError", "ConnectError", "NetworkError"} or isinstance(
        error,
        (ConnectionError, httpx.TransportError),
    ):
        return FailureDecision(FailureKind.CONNECTION, True, status)
    if markers & _TERMINAL_RATE_LIMIT_CODES:
        return FailureDecision(FailureKind.RATE_LIMIT, False, status)
    if name == "RateLimitError" or status == 429 or markers & _RATE_LIMIT_CODES:
        return FailureDecision(FailureKind.RATE_LIMIT, True, status)
    if name == "AuthenticationError" or status == 401 or markers & _AUTHENTICATION_CODES:
        return FailureDecision(FailureKind.AUTHENTICATION, False, status)
    if name == "PermissionDeniedError" or status == 403 or markers & _PERMISSION_CODES:
        return FailureDecision(FailureKind.PERMISSION, False, status)
    if (
        name == "InternalServerError"
        or status is not None
        and 500 <= status <= 599
        or markers & _SERVER_CODES
    ):
        return FailureDecision(FailureKind.SERVER, True, status)
    if (
        name in {"BadRequestError", "NotFoundError", "UnprocessableEntityError"}
        or status is not None
        and 400 <= status <= 499
        or isinstance(error, ValueError)
    ):
        return FailureDecision(FailureKind.INVALID_REQUEST, False, status)
    return FailureDecision(FailureKind.UNKNOWN, False, status)


def is_provider_context_overflow(error: Exception) -> bool:
    """Identify a provider-reported input overflow eligible for one recovery.

    Local accounting failures and capability preflight errors use the same
    ``FailureKind`` for routing purposes, but they must never trigger a retry
    after deleting history.  Keep this stricter predicate at the recovery
    boundary rather than weakening the general failure classifier.
    """

    if bool(getattr(error, "stream_visible", False)):
        return False
    try:
        from ..runtime.context import ContextBudgetExceeded

        if isinstance(error, ContextBudgetExceeded):
            return False
    except ImportError:
        pass
    if isinstance(error, ProviderCapabilityError):
        return False
    decision = classify_failure(error)
    if decision.kind is not FailureKind.CONTEXT_OVERFLOW:
        return False
    markers = _error_markers(error)
    status = _status_code(error)
    if not markers and status is None:
        return False
    text = str(error).lower()[:4096]
    return bool(
        markers & _CONTEXT_CODES
        or "context_length_exceeded" in text
        or "context window" in text
        or "context length" in text
        or "prompt too long" in text
    )


def _non_negative_finite(value: float, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a non-negative finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be a non-negative finite number") from error
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{label} must be a non-negative finite number")
    return parsed


def parse_retry_after(
    value: object,
    *,
    now: float,
    max_delay_seconds: float,
) -> float | None:
    """Parse Retry-After delta-seconds or HTTP-date and clamp it to a local cap."""
    maximum = _non_negative_finite(max_delay_seconds, "max_delay_seconds")
    delay: float
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            delay = _non_negative_finite(value, "Retry-After")
        except ValueError:
            return None
    elif isinstance(value, str):
        rendered = value.strip()
        if re.fullmatch(r"[0-9]+", rendered):
            delay = float(rendered)
        else:
            try:
                parsed = parsedate_to_datetime(rendered)
            except (TypeError, ValueError, OverflowError):
                return None
            if parsed is None:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            try:
                delay = max(0.0, parsed.timestamp() - float(now))
            except (TypeError, ValueError, OverflowError, OSError):
                return None
    else:
        return None
    return min(delay, maximum)


def _header(headers: object, name: str) -> object | None:
    getter = getattr(headers, "get", None)
    if callable(getter):
        for candidate in (name, name.lower(), name.title()):
            value = getter(candidate)
            if value is not None:
                return value
    if isinstance(headers, Mapping):
        for key, value in headers.items():
            if isinstance(key, str) and key.lower() == name.lower():
                return value
    return None


def retry_after_from_error(
    error: Exception,
    *,
    now: float,
    max_delay_seconds: float,
) -> float | None:
    """Read Retry-After from SDK response/error headers without inspecting response bodies."""
    for source in (getattr(error, "response", None), error):
        value = _header(getattr(source, "headers", None), "Retry-After")
        if value is not None:
            return parse_retry_after(
                value,
                now=now,
                max_delay_seconds=max_delay_seconds,
            )
    return None


class CircuitOpenError(RuntimeError):
    pass


@dataclass(frozen=True)
class _CircuitPermit:
    generation: int
    state_before: str
    half_open_probe: bool


class CircuitBreaker:
    """Thread-safe consecutive-transient-failure breaker with one half-open probe."""

    def __init__(
        self,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        if isinstance(failure_threshold, bool) or not isinstance(failure_threshold, int):
            raise ValueError("failure_threshold must be a positive integer")
        if failure_threshold <= 0:
            raise ValueError("failure_threshold must be a positive integer")
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = _non_negative_finite(
            cooldown_seconds, "cooldown_seconds"
        )
        self.clock = clock
        self.failures = 0
        self.opened_at: float | None = None
        self.half_open_in_flight = False
        self._generation = 0
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

    def try_acquire(self) -> _CircuitPermit | None:
        with self._lock:
            state = self._state_unlocked()
            if state == "open" or state == "half_open" and self.half_open_in_flight:
                return None
            probe = state == "half_open"
            if probe:
                self.half_open_in_flight = True
            return _CircuitPermit(self._generation, state, probe)

    def allow(self) -> bool:
        """Compatibility facade for callers that do not need permit metadata."""
        return self.try_acquire() is not None

    def _permit_is_current(self, permit: _CircuitPermit | None) -> bool:
        return permit is None or permit.generation == self._generation

    def _record_success(self, permit: _CircuitPermit | None) -> tuple[bool, str]:
        with self._lock:
            if not self._permit_is_current(permit):
                return False, self._state_unlocked()
            recovered = self.failures > 0 or self.opened_at is not None
            if self.opened_at is not None:
                self._generation += 1
            self.failures = 0
            self.opened_at = None
            self.half_open_in_flight = False
            return recovered, "closed"

    def record_success(self, permit: _CircuitPermit | None = None) -> bool:
        return self._record_success(permit)[0]

    def record_non_retryable(self, permit: _CircuitPermit | None = None) -> None:
        """A terminal request error is not evidence that the route is unavailable."""
        self._record_success(permit)

    def _record_failure(self, permit: _CircuitPermit | None) -> tuple[bool, str]:
        with self._lock:
            if not self._permit_is_current(permit):
                return False, self._state_unlocked()
            previous_state = self._state_unlocked()
            self.half_open_in_flight = False
            self.failures += 1
            if self.opened_at is not None or self.failures >= self.failure_threshold:
                self.failures = max(self.failures, self.failure_threshold)
                self.opened_at = self.clock()
                self._generation += 1
                return previous_state != "open", "open"
            return False, "closed"

    def record_failure(self, permit: _CircuitPermit | None = None) -> bool:
        return self._record_failure(permit)[0]


class RouteStateCapacityError(RuntimeError):
    pass


@dataclass
class _RouteState:
    breaker: CircuitBreaker
    semaphore: threading.BoundedSemaphore
    last_used: float
    leases: int = 0


class RouteStateRegistry:
    """Bounded, instance-owned route state with idle expiry and lease-safe eviction."""

    def __init__(
        self,
        *,
        max_concurrency: int = 4,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
        capacity: int = 128,
        idle_ttl_seconds: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        for value, label in (
            (max_concurrency, "max_concurrency"),
            (capacity, "capacity"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{label} must be a positive integer")
        self.max_concurrency = max_concurrency
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = _non_negative_finite(
            cooldown_seconds, "cooldown_seconds"
        )
        self.capacity = capacity
        self.idle_ttl_seconds = _non_negative_finite(
            idle_ttl_seconds, "idle_ttl_seconds"
        )
        if self.idle_ttl_seconds <= 0:
            raise ValueError("idle_ttl_seconds must be greater than zero")
        if self.idle_ttl_seconds <= self.cooldown_seconds:
            raise ValueError("idle_ttl_seconds must be greater than cooldown_seconds")
        self.clock = clock
        # Validate breaker-specific configuration before the first route arrives.
        CircuitBreaker(failure_threshold, self.cooldown_seconds, clock=clock)
        self._states: OrderedDict[RouteIdentity, _RouteState] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def route_count(self) -> int:
        with self._lock:
            return len(self._states)

    def _prune_unlocked(self, now: float) -> None:
        expired = [
            identity
            for identity, state in self._states.items()
            if state.leases == 0 and now - state.last_used >= self.idle_ttl_seconds
        ]
        for identity in expired:
            self._states.pop(identity, None)

    def _reserve(self, identity: RouteIdentity) -> _RouteState:
        now = self.clock()
        with self._lock:
            self._prune_unlocked(now)
            state = self._states.get(identity)
            if state is None:
                if len(self._states) >= self.capacity:
                    evictable = next(
                        (
                            candidate
                            for candidate, item in self._states.items()
                            if item.leases == 0 and not item.breaker.degraded
                        ),
                        None,
                    )
                    if evictable is None:
                        raise RouteStateCapacityError(
                            "provider route state capacity is exhausted"
                        )
                    self._states.pop(evictable)
                state = _RouteState(
                    breaker=CircuitBreaker(
                        self.failure_threshold,
                        self.cooldown_seconds,
                        clock=self.clock,
                    ),
                    semaphore=threading.BoundedSemaphore(self.max_concurrency),
                    last_used=now,
                )
                self._states[identity] = state
            else:
                self._states.move_to_end(identity)
            state.leases += 1
            return state

    def _release(self, identity: RouteIdentity, state: _RouteState) -> None:
        with self._lock:
            current = self._states.get(identity)
            if current is not state:
                return
            state.leases -= 1
            state.last_used = self.clock()
            self._states.move_to_end(identity)

    @contextlib.contextmanager
    def lease(self, identity: RouteIdentity) -> Iterator[_RouteState]:
        state = self._reserve(identity)
        try:
            yield state
        finally:
            self._release(identity, state)


@dataclass(frozen=True)
class _RouteSnapshot:
    identity: RouteIdentity
    provider: str
    audit: dict[str, Any]
    route: ResolvedRoute | None
    api_mode: ApiMode | None
    capabilities: ProviderCapabilities | None


@dataclass(frozen=True)
class _RouteExecution:
    response: EngineResponse | None
    error: Exception | None
    attempts: int
    skipped: bool
    breaker_state: str


@dataclass(frozen=True)
class _StreamRouteExecution:
    response: EngineResponse | None
    error: Exception | None
    error_event: ProviderStreamEvent | None
    completed_event: ProviderStreamEvent | None
    attempts: int
    skipped: bool
    breaker_state: str
    visible: bool


@dataclass(frozen=True)
class _TurnRoutePlan:
    primary: _RouteSnapshot
    fallback: _RouteSnapshot | None


@dataclass(frozen=True)
class _FallbackCompatibility:
    compatible: bool
    gaps: tuple[str, ...]
    requirements: ProviderRequestRequirements
    api_mode: str
    context_check: str
    capabilities: dict[str, bool | int | str | None]

    def to_event(self) -> dict[str, Any]:
        return {
            "compatible": self.compatible,
            "gaps": list(self.gaps),
            "requirements": self.requirements.to_event(),
            "api_mode": self.api_mode,
            "context_check": self.context_check,
            "capabilities": self.capabilities,
        }


_USAGE_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
_OMIT = object()


def _audit_usage_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return _OMIT
    if isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else _OMIT
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:64]:
            if not isinstance(key, str) or _USAGE_KEY.fullmatch(key) is None:
                continue
            rendered = _audit_usage_value(item, depth=depth + 1)
            if rendered is not _OMIT:
                result[key] = rendered
        return result
    if isinstance(value, (list, tuple)):
        result = []
        for item in value[:32]:
            rendered = _audit_usage_value(item, depth=depth + 1)
            if rendered is not _OMIT:
                result.append(rendered)
        return result
    return _OMIT


def _audit_usage(usage: object) -> dict[str, Any]:
    rendered = _audit_usage_value(usage)
    return rendered if isinstance(rendered, dict) else {}


def _route_snapshot(engine: Engine) -> _RouteSnapshot:
    routes = engine.begin_turn_routes()
    if len(routes) > 1:
        raise ValueError("a provider engine must expose exactly one route")
    if routes:
        route = routes[0]
        if not isinstance(route, ResolvedRoute):
            raise ValueError("engine returned an invalid provider route")
        resolver = getattr(engine, "effective_capabilities", None)
        capabilities = resolver() if callable(resolver) else route.capabilities
        if not isinstance(capabilities, ProviderCapabilities):
            raise ValueError("engine returned invalid effective provider capabilities")
        return _RouteSnapshot(
            route.identity,
            route.provider,
            route.to_event(),
            route,
            route.api_mode,
            capabilities,
        )
    name = getattr(engine, "name", type(engine).__name__)
    provider = name if isinstance(name, str) and name else type(engine).__name__
    identity: RouteIdentity = ("legacy", "", "engine", "", provider)
    return _RouteSnapshot(
        identity,
        provider,
        {
            "route_identity": identity,
            "api_mode": "engine",
            "provider": provider,
            "deployment": None,
            "endpoint": None,
            "model": None,
        },
        None,
        None,
        None,
    )


def _response_with_usage(
    response: EngineResponse,
    additions: Mapping[str, Any],
) -> EngineResponse:
    current = copy.deepcopy(dict(response.usage)) if isinstance(response.usage, Mapping) else {}
    return replace(
        response,
        tool_calls=copy.deepcopy(response.tool_calls),
        usage={**current, **copy.deepcopy(dict(additions))},
    )


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
        wall_clock: Callable[[], float] = time.time,
        random_source: Callable[[], float] = random_module.random,
        retry_base_delay_seconds: float = 1.0,
        retry_max_delay_seconds: float = 8.0,
        retry_after_max_seconds: float = 60.0,
        route_max_concurrency: int = 4,
        route_state_capacity: int = 128,
        route_state_ttl_seconds: float = 900.0,
        route_registry: RouteStateRegistry | None = None,
        event_sink: Callable[[dict], None] | None = None,
    ):
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        self.primary = primary
        self.fallback = fallback
        self.max_retries = max_retries
        self.sleeper = sleeper
        self.clock = clock
        self.wall_clock = wall_clock
        self.random_source = random_source
        self.retry_base_delay_seconds = _non_negative_finite(
            retry_base_delay_seconds, "retry_base_delay_seconds"
        )
        self.retry_max_delay_seconds = _non_negative_finite(
            retry_max_delay_seconds, "retry_max_delay_seconds"
        )
        if self.retry_max_delay_seconds < self.retry_base_delay_seconds:
            raise ValueError(
                "retry_max_delay_seconds must be greater than or equal to the base delay"
            )
        self.retry_after_max_seconds = _non_negative_finite(
            retry_after_max_seconds, "retry_after_max_seconds"
        )
        self.route_registry = route_registry or RouteStateRegistry(
            max_concurrency=route_max_concurrency,
            failure_threshold=failure_threshold,
            cooldown_seconds=cooldown_seconds,
            capacity=route_state_capacity,
            idle_ttl_seconds=route_state_ttl_seconds,
            clock=clock,
        )
        self.event_sink = event_sink
        self.name = f"resilient:{primary.name}"
        self._run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
            "edu_agent_provider_run_id",
            default=None,
        )
        self._turn_route_plan: contextvars.ContextVar[_TurnRoutePlan | None] = (
            contextvars.ContextVar("edu_agent_provider_turn_routes", default=None)
        )
        self._validate_route_configuration()

    def _build_route_plan(self) -> _TurnRoutePlan:
        return _TurnRoutePlan(
            primary=_route_snapshot(self.primary),
            fallback=_route_snapshot(self.fallback) if self.fallback is not None else None,
        )

    def _validate_route_configuration(self) -> None:
        plan = self._build_route_plan()
        if plan.fallback is None:
            return
        if (plan.primary.route is None) != (plan.fallback.route is None):
            raise ValueError(
                "primary/fallback 必须同时提供可审计 Provider route metadata"
            )
        if plan.primary.identity == plan.fallback.identity:
            raise ValueError("primary/fallback route identity 不能相同")

    @property
    def breaker(self) -> CircuitBreaker:
        """Compatibility view of the current primary route breaker."""
        plan = self._turn_route_plan.get() or self._build_route_plan()
        route = plan.primary
        with self.route_registry.lease(route.identity) as state:
            return state.breaker

    @contextlib.contextmanager
    def runtime_context(self, run_id: str) -> Iterator[None]:
        plan = self._build_route_plan()
        run_token = self._run_id.set(run_id)
        route_token = self._turn_route_plan.set(plan)
        try:
            yield
        finally:
            self._turn_route_plan.reset(route_token)
            self._run_id.reset(run_token)

    def begin_turn_routes(self) -> tuple[ResolvedRoute, ...]:
        plan = self._turn_route_plan.get() or self._build_route_plan()
        snapshots = (plan.primary, plan.fallback)
        return tuple(
            snapshot.route
            for snapshot in snapshots
            if snapshot is not None and snapshot.route is not None
        )

    def capabilities_for_route(self, route: ResolvedRoute) -> ProviderCapabilities:
        plan = self._turn_route_plan.get() or self._build_route_plan()
        for snapshot in (plan.primary, plan.fallback):
            if snapshot is not None and snapshot.identity == route.identity:
                return snapshot.capabilities or route.capabilities
        raise ValueError("route is not part of the frozen resilient turn")

    @contextlib.contextmanager
    def _chat_route_plan(self) -> Iterator[_TurnRoutePlan]:
        active = self._turn_route_plan.get()
        if active is not None:
            yield active
            return
        plan = self._build_route_plan()
        token = self._turn_route_plan.set(plan)
        try:
            yield plan
        finally:
            self._turn_route_plan.reset(token)

    def _local_backoff(self, retry_index: int) -> float:
        if self.retry_base_delay_seconds == 0 or self.retry_max_delay_seconds == 0:
            return 0.0
        if retry_index >= 63:
            ceiling = self.retry_max_delay_seconds
        else:
            ceiling = min(
                self.retry_max_delay_seconds,
                self.retry_base_delay_seconds * 2.0**retry_index,
            )
        random_value = self.random_source()
        if (
            isinstance(random_value, bool)
            or not isinstance(random_value, (int, float))
            or not math.isfinite(float(random_value))
            or not 0 <= float(random_value) <= 1
        ):
            raise ValueError("random_source must return a finite number between 0 and 1")
        return ceiling * float(random_value)

    def _retry_delay(self, error: Exception, retry_index: int) -> tuple[float, str]:
        server_delay = retry_after_from_error(
            error,
            now=self.wall_clock(),
            max_delay_seconds=self.retry_after_max_seconds,
        )
        if server_delay is not None:
            return server_delay, "retry_after"
        return self._local_backoff(retry_index), "exponential_full_jitter"

    @staticmethod
    def _chat_frozen_route(
        engine: Engine,
        route: _RouteSnapshot,
        messages: list[dict],
        tools: list[dict],
        cancellation_token: CancellationToken | None = None,
        max_output_tokens: int | None = None,
    ) -> EngineResponse:
        if route.route is not None:
            routed_chat = getattr(engine, "chat_on_route", None)
            if callable(routed_chat):
                kwargs = {}
                if max_output_tokens is not None and accepts_keyword_argument(
                    routed_chat,
                    "max_output_tokens",
                ):
                    kwargs["max_output_tokens"] = max_output_tokens
                return call_with_cancellation(
                    routed_chat,
                    route.route,
                    messages,
                    tools,
                    cancellation_token=cancellation_token,
                    **kwargs,
                )
            current = _route_snapshot(engine)
            if current.identity != route.identity:
                raise RuntimeError("provider route changed after turn start")
        kwargs = {}
        if max_output_tokens is not None and accepts_keyword_argument(
            engine.chat,
            "max_output_tokens",
        ):
            kwargs["max_output_tokens"] = max_output_tokens
        return call_with_cancellation(
            engine.chat,
            messages,
            tools,
            cancellation_token=cancellation_token,
            **kwargs,
        )

    @staticmethod
    def _stream_frozen_route(
        engine: Engine,
        route: _RouteSnapshot,
        messages: list[dict],
        tools: list[dict],
        *,
        attempt: int,
        cancellation_token: CancellationToken | None = None,
        max_output_tokens: int | None = None,
    ) -> Iterator[ProviderStreamEvent]:
        if route.route is None:
            raise ProviderStreamProtocolError(
                "provider stream 需要可审计 ResolvedRoute",
                code="provider_stream_route_missing",
            )
        routed_stream = getattr(engine, "stream_chat_on_route", None)
        if callable(routed_stream):
            kwargs = {"attempt": attempt}
            if (
                cancellation_token is not None
                and accepts_cancellation_token(routed_stream)
            ):
                kwargs["cancellation_token"] = cancellation_token
            if max_output_tokens is not None and accepts_keyword_argument(
                routed_stream,
                "max_output_tokens",
            ):
                kwargs["max_output_tokens"] = max_output_tokens
            return iter(routed_stream(route.route, messages, tools, **kwargs))
        current = _route_snapshot(engine)
        if current.identity != route.identity:
            raise RuntimeError("provider route changed after turn start")
        stream = getattr(engine, "stream_chat", None)
        if not callable(stream):
            stream = getattr(engine, "stream_events", None)
        if not callable(stream):
            raise ProviderStreamProtocolError(
                "provider engine 缺少 stream_chat",
                code="provider_stream_unsupported",
            )
        kwargs = {"attempt": attempt}
        if cancellation_token is not None and accepts_cancellation_token(stream):
            kwargs["cancellation_token"] = cancellation_token
        if max_output_tokens is not None and accepts_keyword_argument(
            stream,
            "max_output_tokens",
        ):
            kwargs["max_output_tokens"] = max_output_tokens
        return iter(stream(messages, tools, **kwargs))

    @staticmethod
    def _acquire_with_cancellation(
        semaphore: threading.Semaphore,
        cancellation_token: CancellationToken | None,
        boundary: str,
    ) -> None:
        if cancellation_token is None:
            semaphore.acquire()
            return
        while not semaphore.acquire(timeout=0.05):
            cancellation_token.checkpoint(boundary)
        try:
            cancellation_token.checkpoint(boundary)
        except CancellationRequested:
            semaphore.release()
            raise

    @staticmethod
    def _supports_provider_stream(engine: Engine, route: _RouteSnapshot) -> bool:
        return (
            route.route is not None
            and route.capabilities is not None
            and route.capabilities.streaming
            and (
                callable(getattr(engine, "stream_chat_on_route", None))
                or callable(getattr(engine, "stream_chat", None))
                or callable(getattr(engine, "stream_events", None))
            )
        )

    @staticmethod
    def _fallback_compatibility(
        primary: _RouteSnapshot,
        fallback_engine: Engine,
        fallback: _RouteSnapshot,
        messages: list[dict],
        tools: list[dict],
        *,
        streaming: bool = False,
        max_output_tokens: int | None = None,
    ) -> _FallbackCompatibility:
        requirements = infer_request_requirements(
            messages,
            tools,
            model=fallback.route.model if fallback.route is not None else None,
            tokenizer=(
                fallback.capabilities.tokenizer
                if fallback.capabilities is not None
                else None
            ),
            max_output_tokens=max_output_tokens,
        )
        if streaming:
            requirements = replace(requirements, streaming=True)
        if fallback.route is None or fallback.capabilities is None:
            gaps = (
                ("capability_metadata",)
                if requirements.tool_calling or requirements.structured_output
                else ()
            )
            return _FallbackCompatibility(
                compatible=not gaps,
                gaps=gaps,
                requirements=requirements,
                api_mode="engine",
                context_check="engine_contract",
                capabilities={},
            )

        gaps = list(
            capability_gaps(
                requirements,
                fallback.capabilities,
                api_mode=fallback.route.api_mode,
                require_known_context=True,
                require_known_output=requirements.max_output_tokens is not None,
            )
        )
        validator = getattr(fallback_engine, "validate_request_on_route", None)
        validates_frozen_route = callable(validator)
        if not validates_frozen_route:
            validator = getattr(fallback_engine, "validate_request", None)
        if (
            primary.api_mode is not None
            and primary.api_mode is not fallback.api_mode
            and not callable(validator)
        ):
            gaps.append("api_mode_validation")
        if not gaps and callable(validator):
            try:
                if validates_frozen_route:
                    args = (fallback.route, messages, tools)
                else:
                    args = (messages, tools)
                kwargs = {}
                if max_output_tokens is not None and accepts_keyword_argument(
                    validator,
                    "max_output_tokens",
                ):
                    kwargs["max_output_tokens"] = max_output_tokens
                validator(*args, **kwargs)
            except ProviderCapabilityError as error:
                gaps.extend(error.gaps)
            except ValueError:
                gaps.append("api_mode_request_shape")

        fallback_context = fallback.capabilities.context_window_tokens
        if fallback_context is not None:
            required = requirements.context_tokens + (requirements.max_output_tokens or 0)
            context_check = "sufficient" if required <= fallback_context else "insufficient"
        else:
            context_check = "unknown_unverified"
        normalized_gaps = tuple(dict.fromkeys(gaps))
        return _FallbackCompatibility(
            compatible=not normalized_gaps,
            gaps=normalized_gaps,
            requirements=requirements,
            api_mode=fallback.route.api_mode.value,
            context_check=context_check,
            capabilities=fallback.capabilities.to_event(),
        )

    def _attempt_event(
        self,
        *,
        route: _RouteSnapshot,
        route_role: str,
        attempt: int,
        breaker_before: str,
        breaker_after: str,
        response: EngineResponse | None = None,
        error: Exception | None = None,
        decision: FailureDecision | None = None,
        delay: float = 0.0,
        delay_source: str | None = None,
    ) -> None:
        completion_kind = (
            FailureKind.OUTPUT_CAP
            if response is not None and response.finish_reason == "length"
            else None
        )
        failure_kind = decision.kind if decision is not None else completion_kind
        self._event(
            "provider_attempt",
            route=route,
            attempt=attempt,
            error=error,
            details={
                "status": "failed"
                if error is not None
                else ("output_cap" if completion_kind is not None else "ok"),
                "attempt_sequence": attempt,
                "route_role": route_role,
                "route": route.audit,
                "failure_kind": failure_kind.value if failure_kind is not None else None,
                "retryable": decision.retryable if decision is not None else False,
                "fallback_allowed": (
                    decision.fallback_allowed if decision is not None else False
                ),
                "delay_seconds": delay,
                "delay_source": delay_source,
                "breaker_state_before": breaker_before,
                "breaker_state": breaker_after,
                "usage": _audit_usage(
                    response.usage
                    if response is not None
                    else getattr(error, "usage", {})
                ),
            },
        )

    def _execute_route(
        self,
        engine: Engine,
        route: _RouteSnapshot,
        messages: list[dict],
        tools: list[dict],
        *,
        route_role: str,
        start_attempt: int,
        max_retries: int,
        cancellation_token: CancellationToken | None = None,
        max_output_tokens: int | None = None,
        run_budget=None,
    ) -> _RouteExecution:
        attempts = 0
        with self.route_registry.lease(route.identity) as state:
            for retry_index in range(max_retries + 1):
                self._acquire_with_cancellation(
                    state.semaphore,
                    cancellation_token,
                    "provider.semaphore.acquire",
                )
                permit: _CircuitPermit | None = None
                response: EngineResponse | None = None
                error: Exception | None = None
                decision: FailureDecision | None = None
                opened = False
                recovered = False
                breaker_after = "closed"
                try:
                    permit = state.breaker.try_acquire()
                    if permit is None:
                        circuit_error = CircuitOpenError(
                            f"{route_role} provider route circuit is open"
                        )
                        return _RouteExecution(
                            None,
                            circuit_error,
                            attempts,
                            True,
                            state.breaker.state,
                        )
                    attempts += 1
                    attempt_number = start_attempt + attempts
                    budget_attempt = (
                        run_budget.begin_provider_attempt(
                            attempt_sequence=attempt_number,
                            provider=route.provider,
                            model=(
                                route.route.model
                                if route.route is not None
                                else route.audit.get("model") or route.provider
                            ),
                            route_role=route_role,
                        )
                        if run_budget is not None
                        else None
                    )
                    try:
                        response = self._chat_frozen_route(
                            engine,
                            route,
                            messages,
                            tools,
                            cancellation_token,
                            max_output_tokens,
                        )
                    except CancellationRequested as caught:
                        error = caught
                        state.breaker.record_non_retryable(permit)
                        raise
                    except Exception as caught:
                        error = caught
                        decision = classify_failure(caught)
                        if decision.retryable:
                            opened, breaker_after = state.breaker._record_failure(permit)
                        else:
                            _, breaker_after = state.breaker._record_success(permit)
                    except BaseException as caught:
                        error = caught
                        state.breaker.record_non_retryable(permit)
                        raise
                    else:
                        recovered, breaker_after = state.breaker._record_success(permit)
                    finally:
                        if budget_attempt is not None:
                            run_budget.settle_provider_attempt(
                                budget_attempt,
                                (
                                    response.usage
                                    if response is not None
                                    else getattr(error, "usage", None)
                                ),
                                status="ok" if response is not None else "failed",
                            )
                finally:
                    state.semaphore.release()

                assert permit is not None
                attempt_number = start_attempt + attempts
                if error is None:
                    assert response is not None
                    self._attempt_event(
                        route=route,
                        route_role=route_role,
                        attempt=attempt_number,
                        breaker_before=permit.state_before,
                        breaker_after=breaker_after,
                        response=response,
                    )
                    if recovered:
                        self._event(
                            f"{route_role}_recovered",
                            route=route,
                            attempt=attempt_number,
                            details={"breaker_state": breaker_after},
                        )
                    return _RouteExecution(
                        response,
                        None,
                        attempts,
                        False,
                        breaker_after,
                    )

                assert decision is not None
                can_retry = (
                    decision.retryable
                    and retry_index < max_retries
                    and breaker_after != "open"
                    and not permit.half_open_probe
                )
                delay = 0.0
                delay_source = None
                delay_error: Exception | None = None
                if can_retry:
                    try:
                        delay, delay_source = self._retry_delay(error, retry_index)
                    except Exception as caught:
                        delay_error = caught
                self._attempt_event(
                    route=route,
                    route_role=route_role,
                    attempt=attempt_number,
                    breaker_before=permit.state_before,
                    breaker_after=breaker_after,
                    error=error,
                    decision=decision,
                    delay=delay,
                    delay_source=delay_source,
                )
                self._event(
                    "provider_failure",
                    route=route,
                    attempt=attempt_number,
                    error=error,
                    details={
                        "kind": decision.kind.value,
                        "retryable": decision.retryable,
                        "breaker_state": breaker_after,
                    },
                )
                if opened:
                    self._event(
                        "circuit_opened",
                        route=route,
                        attempt=attempt_number,
                        error=error,
                        details={"breaker_state": breaker_after},
                    )
                if delay_error is not None:
                    raise delay_error
                if not can_retry:
                    return _RouteExecution(
                        None,
                        error,
                        attempts,
                        False,
                        breaker_after,
                    )
                self._event(
                    "retry_scheduled",
                    route=route,
                    attempt=attempt_number,
                    details={
                        "delay_seconds": delay,
                        "delay_source": delay_source,
                        "breaker_state": breaker_after,
                    },
                )
                if cancellation_token is not None:
                    if cancellation_token.wait(delay):
                        cancellation_token.checkpoint("provider.retry_delay")
                else:
                    self.sleeper(delay)

        raise AssertionError("provider attempt loop exited unexpectedly")

    def _execute_route_stream(
        self,
        engine: Engine,
        route: _RouteSnapshot,
        messages: list[dict],
        tools: list[dict],
        *,
        route_role: str,
        start_attempt: int,
        max_retries: int,
        cancellation_token: CancellationToken | None = None,
        max_output_tokens: int | None = None,
        run_budget=None,
    ) -> Generator[ProviderStreamEvent, None, _StreamRouteExecution]:
        attempts = 0
        with self.route_registry.lease(route.identity) as state:
            for retry_index in range(max_retries + 1):
                self._acquire_with_cancellation(
                    state.semaphore,
                    cancellation_token,
                    "provider.stream.semaphore.acquire",
                )
                permit: _CircuitPermit | None = None
                response: EngineResponse | None = None
                completed_event: ProviderStreamEvent | None = None
                error: Exception | None = None
                error_event: ProviderStreamEvent | None = None
                decision: FailureDecision | None = None
                opened = False
                recovered = False
                breaker_after = "closed"
                visible = False
                iterator: Iterator[ProviderStreamEvent] | None = None
                try:
                    permit = state.breaker.try_acquire()
                    if permit is None:
                        circuit_error = CircuitOpenError(
                            f"{route_role} provider route circuit is open"
                        )
                        stream_attempt = max(1, start_attempt + attempts + 1)
                        assert route.route is not None
                        return _StreamRouteExecution(
                            None,
                            circuit_error,
                            provider_stream_error_event(
                                route=route.route,
                                attempt=stream_attempt,
                                error=circuit_error,
                                code=FailureKind.CIRCUIT_OPEN.value,
                            ),
                            None,
                            attempts,
                            True,
                            state.breaker.state,
                            False,
                        )
                    attempts += 1
                    attempt_number = start_attempt + attempts
                    budget_attempt = (
                        run_budget.begin_provider_attempt(
                            attempt_sequence=attempt_number,
                            provider=route.provider,
                            model=(
                                route.route.model
                                if route.route is not None
                                else route.audit.get("model") or route.provider
                            ),
                            route_role=route_role,
                        )
                        if run_budget is not None
                        else None
                    )
                    aggregator = ProviderStreamAggregator()
                    try:
                        iterator = self._stream_frozen_route(
                            engine,
                            route,
                            messages,
                            tools,
                            attempt=attempt_number,
                            cancellation_token=cancellation_token,
                            max_output_tokens=max_output_tokens,
                        )
                        for event in iterator:
                            if cancellation_token is not None:
                                cancellation_token.checkpoint("provider.stream.event")
                            if not isinstance(event, ProviderStreamEvent):
                                raise ProviderStreamProtocolError(
                                    "provider engine 产生了非 ProviderStreamEvent",
                                    code="provider_stream_invalid_event",
                                )
                            if (
                                event.attempt != attempt_number
                                or event.route.identity != route.identity
                            ):
                                self._event(
                                    "provider_stream_stale_event_ignored",
                                    route=route,
                                    attempt=attempt_number,
                                    details={
                                        "expected_attempt": attempt_number,
                                        "event_attempt": event.attempt,
                                        "provider_event_id": event.provider_event_id,
                                        "provider_event_type": event.provider_event_type,
                                        "event_route": event.route.to_event(),
                                    },
                                )
                                yield ProviderStreamEvent(
                                    ProviderStreamEventType.IGNORED,
                                    route=event.route,
                                    attempt=event.attempt,
                                    provider_event_id=event.provider_event_id,
                                    provider_event_type=event.provider_event_type,
                                    metadata={"reason": "stale_attempt"},
                                )
                                continue
                            if event.event_type is ProviderStreamEventType.ERROR:
                                error_event = event
                                error = event.error or ProviderStreamProtocolError(
                                    event.error_message or "provider stream failed",
                                    code=event.error_code or "provider_stream_error",
                                )
                                break
                            if (
                                event.event_type is ProviderStreamEventType.IGNORED
                                and event.metadata.get("reason")
                                == "unknown_event_ignored"
                            ):
                                self._event(
                                    "provider_stream_event_ignored",
                                    route=route,
                                    attempt=attempt_number,
                                    details={
                                        "reason": "unknown_event",
                                        "provider_event_id": event.provider_event_id,
                                        "provider_event_type": event.provider_event_type,
                                    },
                                )
                            if event.event_type is ProviderStreamEventType.COMPLETED:
                                response = aggregator.feed(event)
                                completed_event = event
                                break
                            aggregator.feed(event)
                            visible = visible or event.is_delta
                            yield event
                        if response is None and error is None:
                            error = ProviderStreamProtocolError(
                                "provider stream 在 completed 前结束",
                                code="provider_stream_interrupted",
                            )
                            assert route.route is not None
                            error_event = provider_stream_error_event(
                                route=route.route,
                                attempt=attempt_number,
                                error=error,
                                retryable=not visible,
                            )
                    except CancellationRequested as caught:
                        error = caught
                        state.breaker.record_non_retryable(permit)
                        if budget_attempt is not None:
                            run_budget.settle_provider_attempt(
                                budget_attempt,
                                aggregator.failure_usage(caught),
                                status="failed",
                            )
                        raise
                    except Exception as caught:
                        error = caught
                        assert route.route is not None
                        error_event = provider_stream_error_event(
                            route=route.route,
                            attempt=attempt_number,
                            error=caught,
                            retryable=not visible,
                        )
                    except BaseException as caught:
                        error = caught
                        state.breaker.record_non_retryable(permit)
                        if budget_attempt is not None:
                            run_budget.settle_provider_attempt(
                                budget_attempt,
                                aggregator.failure_usage(caught),
                                status="failed",
                            )
                        raise
                    finally:
                        close = getattr(iterator, "close", None)
                        if callable(close):
                            close()

                    if error is not None:
                        decision = classify_failure(error)
                        if decision.retryable:
                            opened, breaker_after = state.breaker._record_failure(permit)
                        else:
                            _, breaker_after = state.breaker._record_success(permit)
                    else:
                        recovered, breaker_after = state.breaker._record_success(permit)
                finally:
                    state.semaphore.release()

                assert permit is not None
                attempt_number = start_attempt + attempts
                if budget_attempt is not None:
                    run_budget.settle_provider_attempt(
                        budget_attempt,
                        (
                            response.usage
                            if response is not None
                            else aggregator.failure_usage(error)
                        ),
                        status="ok" if response is not None else "failed",
                    )
                if error is None:
                    assert response is not None and completed_event is not None
                    self._attempt_event(
                        route=route,
                        route_role=route_role,
                        attempt=attempt_number,
                        breaker_before=permit.state_before,
                        breaker_after=breaker_after,
                        response=response,
                    )
                    if recovered:
                        self._event(
                            f"{route_role}_recovered",
                            route=route,
                            attempt=attempt_number,
                            details={"breaker_state": breaker_after},
                        )
                    return _StreamRouteExecution(
                        response,
                        None,
                        None,
                        completed_event,
                        attempts,
                        False,
                        breaker_after,
                        visible,
                    )

                assert decision is not None and error_event is not None
                can_retry = (
                    not visible
                    and decision.retryable
                    and retry_index < max_retries
                    and breaker_after != "open"
                    and not permit.half_open_probe
                )
                delay = 0.0
                delay_source = None
                delay_error: Exception | None = None
                if can_retry:
                    try:
                        delay, delay_source = self._retry_delay(error, retry_index)
                    except Exception as caught:
                        delay_error = caught
                        can_retry = False
                self._attempt_event(
                    route=route,
                    route_role=route_role,
                    attempt=attempt_number,
                    breaker_before=permit.state_before,
                    breaker_after=breaker_after,
                    error=error,
                    decision=decision,
                    delay=delay,
                    delay_source=delay_source,
                )
                self._event(
                    "provider_failure",
                    route=route,
                    attempt=attempt_number,
                    error=error,
                    details={
                        "kind": decision.kind.value,
                        "retryable": decision.retryable,
                        "stream_visible": visible,
                        "breaker_state": breaker_after,
                    },
                )
                if opened:
                    self._event(
                        "circuit_opened",
                        route=route,
                        attempt=attempt_number,
                        error=error,
                        details={"breaker_state": breaker_after},
                    )
                if delay_error is not None:
                    error = delay_error
                    assert route.route is not None
                    error_event = provider_stream_error_event(
                        route=route.route,
                        attempt=attempt_number,
                        error=delay_error,
                    )
                if not can_retry:
                    return _StreamRouteExecution(
                        None,
                        error,
                        error_event,
                        None,
                        attempts,
                        False,
                        breaker_after,
                        visible,
                    )
                self._event(
                    "retry_scheduled",
                    route=route,
                    attempt=attempt_number,
                    details={
                        "delay_seconds": delay,
                        "delay_source": delay_source,
                        "breaker_state": breaker_after,
                        "stream_visible": False,
                    },
                )
                yield replace(
                    error_event,
                    retryable=True,
                    continuation="retry",
                    metadata={
                        **error_event.metadata,
                        "failure_kind": decision.kind.value,
                        "delay_seconds": delay,
                        "delay_source": delay_source,
                    },
                )
                if cancellation_token is not None and cancellation_token.wait(delay):
                    cancellation_token.checkpoint("provider.stream.retry_delay")
                elif cancellation_token is None:
                    self.sleeper(delay)

        raise AssertionError("provider stream attempt loop exited unexpectedly")

    def chat(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        cancellation_token: CancellationToken | None = None,
        max_output_tokens: int | None = None,
        run_budget=None,
    ) -> EngineResponse:
        with self._chat_route_plan() as route_plan:
            streamable = self._supports_provider_stream(
                self.primary,
                route_plan.primary,
            ) and (
                self.fallback is None
                or route_plan.fallback is not None
                and self._supports_provider_stream(self.fallback, route_plan.fallback)
            )
            if streamable:
                return aggregate_provider_stream(
                    self.stream_chat(
                        messages,
                        tools,
                        cancellation_token=cancellation_token,
                        max_output_tokens=max_output_tokens,
                        run_budget=run_budget,
                    )
                )
            return self._chat_sync(
                messages,
                tools,
                cancellation_token=cancellation_token,
                max_output_tokens=max_output_tokens,
                run_budget=run_budget,
            )

    def stream_chat(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        attempt: int = 1,
        cancellation_token: CancellationToken | None = None,
        max_output_tokens: int | None = None,
        run_budget=None,
    ) -> Iterator[ProviderStreamEvent]:
        if attempt != 1:
            raise ValueError("ResilientEngine stream attempt 必须从 1 开始")
        with self._chat_route_plan() as route_plan:
            primary_route = route_plan.primary
            if not self._supports_provider_stream(self.primary, primary_route):
                raise ProviderStreamProtocolError(
                    "primary engine 不支持 provider stream",
                    code="provider_stream_unsupported",
                )
            requirements = replace(
                infer_request_requirements(
                    messages,
                    tools,
                    model=(primary_route.route.model if primary_route.route else None),
                    tokenizer=(
                        primary_route.capabilities.tokenizer
                        if primary_route.capabilities is not None
                        else None
                    ),
                    max_output_tokens=max_output_tokens,
                ),
                streaming=True,
            )
            self._event(
                "route_selected",
                route=primary_route,
                attempt=0,
                details={
                    "route_role": "primary",
                    "selection_reason": "configured_primary",
                    "route": primary_route.audit,
                    "request_requirements": requirements.to_event(),
                    "fallback_configured": route_plan.fallback is not None,
                    "max_retries": self.max_retries,
                    "provider_stream": True,
                },
            )
            primary = yield from self._execute_route_stream(
                self.primary,
                primary_route,
                messages,
                tools,
                route_role="primary",
                start_attempt=0,
                max_retries=self.max_retries,
                cancellation_token=cancellation_token,
                max_output_tokens=max_output_tokens,
                run_budget=run_budget,
            )
            attempts = primary.attempts
            if primary.response is not None:
                additions = {"runtime_attempts": attempts}
                response = _response_with_usage(primary.response, additions)
                self._event(
                    "provider_result_selected",
                    route=primary_route,
                    attempt=attempts,
                    details={
                        "status": "selected",
                        "route_role": "primary",
                        "selection_reason": "primary_completed",
                        "selected_attempt": attempts,
                        "usage": _audit_usage(response.usage),
                        "provider_stream": True,
                    },
                )
                assert primary_route.route is not None
                yield ProviderStreamEvent(
                    ProviderStreamEventType.USAGE,
                    route=primary_route.route,
                    attempt=attempts,
                    provider_event_id=(
                        primary.completed_event.provider_event_id
                        if primary.completed_event is not None
                        else None
                    ),
                    provider_event_type="edu_agent.runtime_usage",
                    usage=additions,
                )
                assert primary.completed_event is not None
                yield primary.completed_event
                return

            last_error = primary.error
            error_event = primary.error_event
            assert last_error is not None and error_event is not None
            decision = classify_failure(last_error)
            if primary.skipped:
                self._event(
                    "primary_skipped",
                    route=primary_route,
                    attempt=attempts,
                    error=last_error,
                    details={
                        "breaker_state": primary.breaker_state,
                        "failure_kind": decision.kind.value,
                        "provider_stream": True,
                    },
                )

            if primary.visible:
                if self.fallback is not None and route_plan.fallback is not None:
                    self._event(
                        "fallback_rejected",
                        route=route_plan.fallback,
                        attempt=max(1, attempts),
                        error=last_error,
                        details={
                            "reason": "stream_already_visible",
                            "primary_failure": decision.kind.value,
                            "fallback_attempted": False,
                            "primary_route": primary_route.audit,
                            "fallback_route": route_plan.fallback.audit,
                        },
                    )
                yield replace(
                    error_event,
                    retryable=False,
                    continuation=None,
                    metadata={
                        **error_event.metadata,
                        "failure_kind": decision.kind.value,
                        "stream_visible": True,
                    },
                )
                return

            if self.fallback is None or route_plan.fallback is None:
                yield replace(
                    error_event,
                    retryable=decision.retryable,
                    continuation=None,
                    metadata={
                        **error_event.metadata,
                        "failure_kind": decision.kind.value,
                        "stream_visible": False,
                    },
                )
                return
            fallback_route = route_plan.fallback
            if not decision.fallback_allowed:
                self._event(
                    "fallback_rejected",
                    route=fallback_route,
                    attempt=attempts + 1,
                    error=last_error,
                    details={
                        "reason": "failure_policy_denied",
                        "primary_failure": decision.kind.value,
                        "primary_retryable": decision.retryable,
                        "fallback_attempted": False,
                        "primary_route": primary_route.audit,
                        "fallback_route": fallback_route.audit,
                        "provider_stream": True,
                    },
                )
                yield replace(
                    error_event,
                    retryable=decision.retryable,
                    continuation=None,
                    metadata={
                        **error_event.metadata,
                        "failure_kind": decision.kind.value,
                    },
                )
                return

            compatibility = self._fallback_compatibility(
                primary_route,
                self.fallback,
                fallback_route,
                messages,
                tools,
                streaming=True,
                max_output_tokens=max_output_tokens,
            )
            if not compatibility.compatible:
                self._event(
                    "fallback_rejected",
                    route=fallback_route,
                    attempt=attempts + 1,
                    error=last_error,
                    details={
                        "reason": "capability_mismatch",
                        "primary_failure": decision.kind.value,
                        "fallback_attempted": False,
                        "primary_route": primary_route.audit,
                        "fallback_route": fallback_route.audit,
                        "compatibility": compatibility.to_event(),
                        "provider_stream": True,
                    },
                )
                yield replace(
                    error_event,
                    retryable=decision.retryable,
                    continuation=None,
                    metadata={
                        **error_event.metadata,
                        "failure_kind": decision.kind.value,
                    },
                )
                return

            if not self._supports_provider_stream(self.fallback, fallback_route):
                unsupported = ProviderStreamProtocolError(
                    "fallback engine 不支持 provider stream",
                    code="provider_stream_unsupported",
                )
                assert fallback_route.route is not None
                yield provider_stream_error_event(
                    route=fallback_route.route,
                    attempt=attempts + 1,
                    error=unsupported,
                )
                return

            self._event(
                "fallback_activated",
                route=fallback_route,
                attempt=attempts + 1,
                error=last_error,
                details={
                    "selection_reason": "failure_policy_and_capabilities_allowed",
                    "primary_failure": decision.kind.value,
                    "primary_route": primary_route.audit,
                    "fallback_route": fallback_route.audit,
                    "compatibility": compatibility.to_event(),
                    "provider_stream": True,
                },
            )
            yield replace(
                error_event,
                retryable=True,
                continuation="fallback",
                metadata={
                    **error_event.metadata,
                    "failure_kind": decision.kind.value,
                    "fallback_route": fallback_route.audit,
                },
            )
            fallback = yield from self._execute_route_stream(
                self.fallback,
                fallback_route,
                messages,
                tools,
                route_role="fallback",
                start_attempt=attempts,
                max_retries=0,
                cancellation_token=cancellation_token,
                max_output_tokens=max_output_tokens,
                run_budget=run_budget,
            )
            attempts += fallback.attempts
            if fallback.response is not None:
                additions = {
                    "runtime_attempts": attempts,
                    "fallback_used": True,
                    "primary_failure": decision.kind.value,
                    "circuit_state": primary.breaker_state,
                }
                response = _response_with_usage(fallback.response, additions)
                self._event(
                    "provider_result_selected",
                    route=fallback_route,
                    attempt=attempts,
                    details={
                        "status": "selected",
                        "route_role": "fallback",
                        "selection_reason": "fallback_completed",
                        "selected_attempt": attempts,
                        "superseded_attempts": primary.attempts,
                        "usage": _audit_usage(response.usage),
                        "provider_stream": True,
                    },
                )
                assert fallback_route.route is not None
                yield ProviderStreamEvent(
                    ProviderStreamEventType.USAGE,
                    route=fallback_route.route,
                    attempt=attempts,
                    provider_event_id=(
                        fallback.completed_event.provider_event_id
                        if fallback.completed_event is not None
                        else None
                    ),
                    provider_event_type="edu_agent.runtime_usage",
                    usage=additions,
                )
                assert fallback.completed_event is not None
                yield fallback.completed_event
                return
            fallback_error = fallback.error
            fallback_error_event = fallback.error_event
            assert fallback_error is not None and fallback_error_event is not None
            if fallback.skipped:
                self._event(
                    "fallback_skipped",
                    route=fallback_route,
                    attempt=attempts,
                    error=fallback_error,
                    details={
                        "breaker_state": fallback.breaker_state,
                        "provider_stream": True,
                    },
                )
            yield replace(
                fallback_error_event,
                retryable=False,
                continuation=None,
                metadata={
                    **fallback_error_event.metadata,
                    "failure_kind": classify_failure(fallback_error).kind.value,
                    "stream_visible": fallback.visible,
                },
            )

    stream_events = stream_chat

    def _chat_sync(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        cancellation_token: CancellationToken | None = None,
        max_output_tokens: int | None = None,
        run_budget=None,
    ) -> EngineResponse:
        with self._chat_route_plan() as route_plan:
            primary_route = route_plan.primary
            requirements = infer_request_requirements(
                messages,
                tools,
                model=(primary_route.route.model if primary_route.route else None),
                tokenizer=(
                    primary_route.capabilities.tokenizer
                    if primary_route.capabilities is not None
                    else None
                ),
                max_output_tokens=max_output_tokens,
            )
            self._event(
                "route_selected",
                route=primary_route,
                attempt=0,
                details={
                    "route_role": "primary",
                    "selection_reason": "configured_primary",
                    "route": primary_route.audit,
                    "request_requirements": requirements.to_event(),
                    "fallback_configured": route_plan.fallback is not None,
                    "max_retries": self.max_retries,
                },
            )
            primary = self._execute_route(
                self.primary,
                primary_route,
                messages,
                tools,
                route_role="primary",
                start_attempt=0,
                max_retries=self.max_retries,
                cancellation_token=cancellation_token,
                max_output_tokens=max_output_tokens,
                run_budget=run_budget,
            )
            attempts = primary.attempts
            if primary.response is not None:
                response = _response_with_usage(
                    primary.response,
                    {"runtime_attempts": attempts},
                )
                self._event(
                    "provider_result_selected",
                    route=primary_route,
                    attempt=attempts,
                    details={
                        "status": "selected",
                        "route_role": "primary",
                        "selection_reason": "primary_completed",
                        "selected_attempt": attempts,
                        "usage": _audit_usage(response.usage),
                    },
                )
                return response

            last_error = primary.error
            assert last_error is not None
            decision = classify_failure(last_error)
            if primary.skipped:
                self._event(
                    "primary_skipped",
                    route=primary_route,
                    attempt=attempts,
                    error=last_error,
                    details={
                        "breaker_state": primary.breaker_state,
                        "failure_kind": decision.kind.value,
                    },
                )

            if self.fallback is None or route_plan.fallback is None:
                raise last_error
            fallback_route = route_plan.fallback
            if not decision.fallback_allowed:
                self._event(
                    "fallback_rejected",
                    route=fallback_route,
                    attempt=attempts + 1,
                    error=last_error,
                    details={
                        "reason": "failure_policy_denied",
                        "primary_failure": decision.kind.value,
                        "primary_retryable": decision.retryable,
                        "fallback_attempted": False,
                        "primary_route": primary_route.audit,
                        "fallback_route": fallback_route.audit,
                    },
                )
                raise last_error

            compatibility = self._fallback_compatibility(
                primary_route,
                self.fallback,
                fallback_route,
                messages,
                tools,
                max_output_tokens=max_output_tokens,
            )
            if not compatibility.compatible:
                self._event(
                    "fallback_rejected",
                    route=fallback_route,
                    attempt=attempts + 1,
                    error=last_error,
                    details={
                        "reason": "capability_mismatch",
                        "primary_failure": decision.kind.value,
                        "fallback_attempted": False,
                        "primary_route": primary_route.audit,
                        "fallback_route": fallback_route.audit,
                        "compatibility": compatibility.to_event(),
                    },
                )
                raise last_error

            self._event(
                "fallback_activated",
                route=fallback_route,
                attempt=attempts + 1,
                error=last_error,
                details={
                    "selection_reason": "failure_policy_and_capabilities_allowed",
                    "primary_failure": decision.kind.value,
                    "primary_route": primary_route.audit,
                    "fallback_route": fallback_route.audit,
                    "compatibility": compatibility.to_event(),
                },
            )
            fallback = self._execute_route(
                self.fallback,
                fallback_route,
                messages,
                tools,
                route_role="fallback",
                start_attempt=attempts,
                max_retries=0,
                cancellation_token=cancellation_token,
                max_output_tokens=max_output_tokens,
                run_budget=run_budget,
            )
            attempts += fallback.attempts
            if fallback.response is not None:
                response = _response_with_usage(
                    fallback.response,
                    {
                        "runtime_attempts": attempts,
                        "fallback_used": True,
                        "primary_failure": decision.kind.value,
                        "circuit_state": primary.breaker_state,
                    },
                )
                self._event(
                    "provider_result_selected",
                    route=fallback_route,
                    attempt=attempts,
                    details={
                        "status": "selected",
                        "route_role": "fallback",
                        "selection_reason": "fallback_completed",
                        "selected_attempt": attempts,
                        "superseded_attempts": primary.attempts,
                        "usage": _audit_usage(response.usage),
                    },
                )
                return response
            fallback_error = fallback.error
            assert fallback_error is not None
            if fallback.skipped:
                self._event(
                    "fallback_skipped",
                    route=fallback_route,
                    attempt=attempts,
                    error=fallback_error,
                    details={"breaker_state": fallback.breaker_state},
                )
            raise fallback_error

    def _event(
        self,
        event: str,
        *,
        route: _RouteSnapshot,
        attempt: int,
        error: Exception | None = None,
        details: dict | None = None,
    ) -> None:
        if self.event_sink is None:
            return
        from ..runtime.security import redact_sensitive

        payload = {
            "run_id": self._run_id.get(),
            "provider": route.provider,
            "event": event,
            "attempt": attempt,
            "error_class": type(error).__name__ if error else None,
            "details": details or {},
        }
        self.event_sink(redact_sensitive(payload))


__all__ = [
    "CircuitBreaker",
    "CircuitOpenError",
    "FailureDecision",
    "FailureKind",
    "ResilientEngine",
    "RouteStateCapacityError",
    "RouteStateRegistry",
    "classify_failure",
    "is_provider_context_overflow",
    "parse_retry_after",
    "retry_after_from_error",
]
