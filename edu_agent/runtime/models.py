from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .cancellation import CancellationToken
from .budget import (
    BudgetAmounts,
    BudgetExceeded,
    BudgetOperationConflict,
    RunBudgetLedger,
)


@dataclass(frozen=True)
class ProviderBudgetAttempt:
    operation_id: str
    provider: str
    model: str
    estimated_input_tokens: int
    estimated_output_tokens: int
    already_committed: bool
    started_at: float


@dataclass
class IterationBudget:
    max_model_calls: int = 12
    max_tool_calls: int = 24
    model_calls: int = 0
    tool_calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _ledger: RunBudgetLedger | None = field(default=None, repr=False, compare=False)
    _owner_run_id: str | None = field(default=None, repr=False, compare=False)
    _reservation_operation_id: str | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _anonymous_model_calls: int = field(default=0, repr=False, compare=False)
    _anonymous_tool_calls: int = field(default=0, repr=False, compare=False)
    _scope_local: threading.local = field(
        default_factory=threading.local,
        repr=False,
        compare=False,
    )

    @property
    def ledger(self) -> RunBudgetLedger | None:
        return self._ledger

    def bind_ledger(
        self,
        ledger: RunBudgetLedger,
        *,
        owner_run_id: str,
        reservation_operation_id: str | None = None,
    ) -> None:
        if self._ledger is not None and self._ledger is not ledger:
            if self._ledger.root_run_id != ledger.root_run_id:
                raise RuntimeError("iteration budget cannot be rebound to another root ledger")
        self._ledger = ledger
        self._owner_run_id = owner_run_id
        self._reservation_operation_id = reservation_operation_id
        self._sync(ledger.snapshot())

    def _sync(self, snapshot: dict) -> None:
        if self._ledger is not None and self._reservation_operation_id is not None:
            local = self._ledger.owner_snapshot(self._owner_run_id or "local")
            self.model_calls = int(local["model_calls"])
            self.tool_calls = int(local["tool_calls"])
            return
        self.model_calls = int(snapshot.get("model_calls", self.model_calls))
        self.tool_calls = int(snapshot.get("tool_calls", self.tool_calls))
        self.max_model_calls = int(snapshot.get("max_model_calls", self.max_model_calls))
        self.max_tool_calls = int(snapshot.get("max_tool_calls", self.max_tool_calls))

    def _operation_id(self, kind: str) -> str:
        owner = self._owner_run_id or "local"
        if kind == "model":
            self._anonymous_model_calls += 1
            ordinal = self._anonymous_model_calls
        else:
            self._anonymous_tool_calls += 1
            ordinal = self._anonymous_tool_calls
        return f"{owner}:{kind}:legacy:{ordinal}"

    @contextmanager
    def model_scope(
        self,
        operation_id: str,
        *,
        breakdowns: list[object] | tuple[object, ...] = (),
        component: str,
    ):
        previous = getattr(self._scope_local, "model", None)
        estimates = []
        for breakdown in breakdowns:
            estimates.append(
                {
                    "provider": str(getattr(breakdown, "provider", "")),
                    "model": str(getattr(breakdown, "model", "")),
                    "input_tokens": int(
                        getattr(breakdown, "estimated_input_tokens", 0)
                    ),
                    "output_tokens": int(
                        getattr(breakdown, "max_output_reserve_tokens", 0)
                    ),
                }
            )
        self._scope_local.model = {
            "operation_id": operation_id,
            "component": component,
            "estimates": estimates,
        }
        try:
            yield
        finally:
            self._scope_local.model = previous

    def begin_provider_attempt(
        self,
        *,
        attempt_sequence: int,
        provider: str,
        model: str | None,
        route_role: str,
    ) -> ProviderBudgetAttempt | None:
        if self._ledger is None:
            self.consume_model_call()
            return None
        if (
            self._reservation_operation_id is not None
            and self.model_calls >= self.max_model_calls
        ):
            raise BudgetExceeded("model_calls", self.usage())
        scope = getattr(self._scope_local, "model", None)
        if scope is None:
            operation_prefix = self._operation_id("model")
            component = "model"
            estimates = []
        else:
            operation_prefix = scope["operation_id"]
            component = scope["component"]
            estimates = scope["estimates"]
        resolved_model = str(model or provider)
        estimate = next(
            (
                item
                for item in estimates
                if item["provider"] == provider and item["model"] == resolved_model
            ),
            next(
                (item for item in estimates if item["model"] == resolved_model),
                estimates[0] if estimates else None,
            ),
        )
        input_tokens = int(estimate["input_tokens"]) if estimate else 0
        output_tokens = int(estimate["output_tokens"]) if estimate else 0
        cost = self._ledger.pricing.quote_microusd(
            provider=provider,
            model=resolved_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        operation_id = f"{operation_prefix}:provider-attempt:{attempt_sequence}"
        existing = self._ledger.operation(operation_id)
        if existing is not None and existing["status"] == "released":
            raise BudgetOperationConflict(
                f"provider budget attempt was already released: {operation_id}"
            )
        already_committed = bool(existing and existing["status"] == "committed")
        snapshot = self._ledger.reserve(
            operation_id,
            owner_run_id=self._owner_run_id or self._ledger.root_run_id,
            kind="model_attempt",
            amount=BudgetAmounts(
                model_calls=1,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                cost_microusd=cost or 0,
            ),
            cost_known=cost is not None,
            metadata={
                "attempt_id": operation_id,
                "attempt_sequence": attempt_sequence,
                "component": component,
                "provider": provider,
                "model": resolved_model,
                "route_role": route_role,
                "estimated": True,
            },
            parent_operation_id=self._reservation_operation_id,
        )
        self._sync(snapshot)
        stop_reason = snapshot.get("stop_reason")
        if isinstance(stop_reason, str) and stop_reason.startswith("budget_exhausted:"):
            dimension = stop_reason.split(":", 1)[1]
            raise BudgetExceeded(dimension, snapshot)
        return ProviderBudgetAttempt(
            operation_id=operation_id,
            provider=provider,
            model=resolved_model,
            estimated_input_tokens=input_tokens,
            estimated_output_tokens=output_tokens,
            already_committed=already_committed,
            started_at=time.monotonic(),
        )

    def settle_provider_attempt(
        self,
        attempt: ProviderBudgetAttempt | None,
        usage: object,
        *,
        status: str,
    ) -> None:
        if attempt is None or self._ledger is None or attempt.already_committed:
            return
        source = usage if isinstance(usage, Mapping) else {}

        def token_value(*names: str) -> int | None:
            for name in names:
                value = source.get(name)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    return value
            return None

        input_tokens = token_value("prompt_tokens", "input_tokens")
        output_tokens = token_value("completion_tokens", "output_tokens")
        total_tokens = token_value("total_tokens")
        if input_tokens is None and total_tokens is not None and output_tokens is not None:
            input_tokens = max(0, total_tokens - output_tokens)
        if output_tokens is None and total_tokens is not None and input_tokens is not None:
            output_tokens = max(0, total_tokens - input_tokens)
        complete_actual = input_tokens is not None and output_tokens is not None
        if input_tokens is None:
            input_tokens = attempt.estimated_input_tokens
        if output_tokens is None:
            output_tokens = attempt.estimated_output_tokens
        if total_tokens is None or complete_actual:
            total_tokens = input_tokens + output_tokens
        cost = self._ledger.pricing.quote_microusd(
            provider=attempt.provider,
            model=attempt.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        snapshot = self._ledger.commit(
            attempt.operation_id,
            actual=BudgetAmounts(
                model_calls=1,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                cost_microusd=cost or 0,
            ),
            usage_source="provider_actual" if complete_actual else "estimated",
            cost_known=cost is not None,
            metadata={"status": status},
        )
        self._sync(snapshot)
        exhausted = snapshot.get("stop_reason")
        if isinstance(exhausted, str) and exhausted.startswith("budget_exhausted:"):
            error = BudgetExceeded(exhausted.split(":", 1)[1], snapshot)
            error.usage = dict(source)
            raise error

    def consume_model_call(self, operation_id: str | None = None) -> None:
        with self._lock:
            if self._ledger is not None:
                if (
                    self._reservation_operation_id is not None
                    and self.model_calls >= self.max_model_calls
                ):
                    raise BudgetExceeded(
                        "model_calls",
                        {
                            "model_calls": self.model_calls,
                            "max_model_calls": self.max_model_calls,
                        },
                    )
                resolved = operation_id or self._operation_id("model")
                self._ledger.reserve(
                    resolved,
                    owner_run_id=self._owner_run_id or self._ledger.root_run_id,
                    kind="model_attempt",
                    amount=BudgetAmounts(model_calls=1),
                    parent_operation_id=self._reservation_operation_id,
                )
                snapshot = self._ledger.commit(
                    resolved,
                    actual=BudgetAmounts(model_calls=1),
                    usage_source="none",
                    cost_known=True,
                )
                self._sync(snapshot)
                return
            if self.model_calls >= self.max_model_calls:
                raise BudgetExceeded(
                    "model_calls",
                    {
                        "model_calls": self.model_calls,
                        "max_model_calls": self.max_model_calls,
                    },
                )
            self.model_calls += 1

    def consume_tool_call(self, operation_id: str | None = None) -> None:
        if self.reserve_tool_calls(1, operation_ids=[operation_id] if operation_id else None) != 1:
            raise BudgetExceeded("tool_calls", self.usage())

    def reserve_tool_calls(
        self,
        requested: int,
        *,
        operation_ids: list[str | None] | tuple[str | None, ...] | None = None,
    ) -> int:
        """Atomically reserve an original-order prefix of a tool segment."""

        if isinstance(requested, bool) or not isinstance(requested, int) or requested < 0:
            raise ValueError("requested tool calls must be a non-negative integer")
        if operation_ids is not None and len(operation_ids) != requested:
            raise ValueError("operation_ids must match requested tool calls")
        with self._lock:
            if self._ledger is not None:
                accepted = 0
                available = (
                    max(0, self.max_tool_calls - self.tool_calls)
                    if self._reservation_operation_id is not None
                    else requested
                )
                for index in range(min(requested, available)):
                    operation_id = (
                        operation_ids[index]
                        if operation_ids is not None and operation_ids[index]
                        else self._operation_id("tool")
                    )
                    try:
                        self._ledger.reserve(
                            operation_id,
                            owner_run_id=self._owner_run_id or self._ledger.root_run_id,
                            kind="tool_call",
                            amount=BudgetAmounts(tool_calls=1),
                            parent_operation_id=self._reservation_operation_id,
                        )
                        snapshot = self._ledger.commit(
                            operation_id,
                            actual=BudgetAmounts(tool_calls=1),
                            usage_source="none",
                            cost_known=True,
                        )
                    except BudgetExceeded:
                        self._sync(self._ledger.snapshot())
                        break
                    accepted += 1
                    self._sync(snapshot)
                return accepted
            accepted = min(requested, max(0, self.max_tool_calls - self.tool_calls))
            self.tool_calls += accepted
            return accepted

    def usage(self) -> dict:
        with self._lock:
            if self._ledger is not None:
                snapshot = self._ledger.snapshot()
                self._sync(snapshot)
                if self._reservation_operation_id is not None:
                    local = self._ledger.owner_snapshot(
                        self._owner_run_id or self._ledger.root_run_id
                    )
                    return {
                        **local,
                        "max_model_calls": self.max_model_calls,
                        "max_tool_calls": self.max_tool_calls,
                        "root_run_id": self._ledger.root_run_id,
                    }
                return snapshot
            return {
                "model_calls": self.model_calls,
                "max_model_calls": self.max_model_calls,
                "tool_calls": self.tool_calls,
                "max_tool_calls": self.max_tool_calls,
            }

    def check_limits(self, *, persist: bool = False) -> None:
        if self._ledger is not None:
            self._ledger.check_limits(persist=persist)

    def finalize(self, operation_id: str) -> dict:
        if self._ledger is None:
            return self.usage()
        snapshot = self._ledger.finalize(operation_id)
        self._sync(snapshot)
        return snapshot


@dataclass
class RunContext:
    session_id: str
    actor_id: str
    role: str
    tenant_id: str = "default"
    course_ids: frozenset[int] = field(default_factory=frozenset)
    replay_scope: str | None = None
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    budget: IterationBudget = field(default_factory=IterationBudget)
    lease_owner: str | None = None
    fencing_token: int | None = None
    cancellation_token: CancellationToken = field(
        default_factory=CancellationToken,
        repr=False,
        compare=False,
    )
    _control_check: Callable[[str], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _run_event_sink: Callable[[str, dict], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _provider_event_sink: Callable[[object], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _tool_manifest: object | None = field(default=None, repr=False, compare=False)
    _tool_manifest_hash_override: str | None = field(default=None, repr=False, compare=False)
    _provider_route: object | None = field(default=None, repr=False, compare=False)
    _trace_context: object | None = field(default=None, repr=False, compare=False)
    _context_accounting: object | None = field(default=None, repr=False, compare=False)
    _argument_retry_calls: set[str] = field(default_factory=set, repr=False, compare=False)
    _argument_retry_lock: threading.Lock = field(
        default_factory=threading.Lock,
        repr=False,
        compare=False,
    )

    def bind_runtime_control(
        self,
        *,
        lease_owner: str,
        fencing_token: int,
        control_check: Callable[[str], None],
    ) -> None:
        """Bind the persistent run identity after the session lease is claimed."""
        self.lease_owner = lease_owner
        self.fencing_token = fencing_token
        self._control_check = control_check

    def check_control(self, boundary: str) -> None:
        """Cooperative cancellation/fencing checkpoint used by the agent loop."""
        self.cancellation_token.checkpoint(boundary)
        self.budget.check_limits()
        if self._control_check is not None:
            self._control_check(boundary)

    def bind_event_sinks(
        self,
        *,
        run_event_sink: Callable[[str, dict], None] | None = None,
        provider_event_sink: Callable[[object], None] | None = None,
    ) -> None:
        self._run_event_sink = run_event_sink
        self._provider_event_sink = provider_event_sink

    @property
    def streams_events(self) -> bool:
        return self._run_event_sink is not None or self._provider_event_sink is not None

    def emit_run_event(self, event_type: str, payload: dict | None = None) -> None:
        self.cancellation_token.checkpoint("run_event.before_publish")
        if self._run_event_sink is not None:
            self._run_event_sink(event_type, dict(payload or {}))

    def emit_provider_event(self, event: object) -> None:
        self.cancellation_token.checkpoint("provider_event.before_publish")
        if self._provider_event_sink is not None:
            self._provider_event_sink(event)

    def consume_argument_retry_budget(self, tool_call_id: str | None) -> bool:
        """Charge at most one argument-repair/retry unit to one tool call.

        Calls without a provider id are scoped to their already charged tool
        call ordinal, so standalone executor use remains bounded as well.
        """

        key = tool_call_id or f"anonymous:{self.budget.tool_calls}"
        with self._argument_retry_lock:
            if key in self._argument_retry_calls:
                return False
            self._argument_retry_calls.add(key)
            return True

    def argument_retry_count(self, tool_call_id: str) -> int:
        with self._argument_retry_lock:
            return int(tool_call_id in self._argument_retry_calls)

    def bind_control_check(self, control_check: Callable[[str], None]) -> None:
        """Bind a cooperative child control check without a session lease.

        Delegated runs have their own persisted worker lease, rather than
        borrowing the parent's session lease.  Keeping this separate from
        ``bind_runtime_control`` prevents a child from accidentally inheriting
        the parent's fencing token.
        """
        self._control_check = control_check

    @property
    def tool_manifest(self):
        return self._tool_manifest

    @property
    def tool_manifest_hash(self) -> str | None:
        manifest = self._tool_manifest
        return getattr(manifest, "manifest_hash", None) if manifest is not None else None

    def bind_tool_manifest(self, manifest) -> None:
        """Freeze the tool surface for this run; a second identity is rejected."""

        manifest_hash = getattr(manifest, "manifest_hash", None)
        if not isinstance(manifest_hash, str) or not manifest_hash.strip():
            raise ValueError("run tool manifest must expose a non-empty manifest_hash")
        matches_context = getattr(manifest, "matches_context", None)
        if callable(matches_context) and not matches_context(self):
            from ..state import RunJournalIdentityError

            raise RunJournalIdentityError(
                "tool manifest scope does not match run context",
                run_id=self.run_id,
            )
        existing = self._tool_manifest
        if existing is not None:
            same_hash = getattr(existing, "manifest_hash", None) == manifest_hash
            same_entries = False
            existing_entries = getattr(existing, "entries", None)
            incoming_entries = getattr(manifest, "entries", None)
            if same_hash and existing_entries is not None and incoming_entries is not None:
                try:
                    existing_by_name = {item.name: item for item in existing_entries}
                    incoming_by_name = {item.name: item for item in incoming_entries}
                    same_entries = (
                        existing_by_name.keys() == incoming_by_name.keys()
                        and all(
                            existing_by_name[name].to_dict() == incoming_by_name[name].to_dict()
                            and (
                                getattr(existing_by_name[name], "handler", None)
                                is getattr(incoming_by_name[name], "handler", None)
                            )
                            for name in existing_by_name
                        )
                    )
                except (AttributeError, TypeError, ValueError):
                    same_entries = False
            if not same_hash or not same_entries:
                from ..state import RunJournalIdentityError

                raise RunJournalIdentityError(
                    "run tool manifest cannot be replaced after freeze",
                    run_id=self.run_id,
                )
        self._tool_manifest = manifest

    @property
    def provider_route(self):
        return deepcopy(self._provider_route)

    def bind_provider_route(self, route: object) -> None:
        """Freeze the redacted provider route propagated to tool workers."""

        frozen = deepcopy(route)
        if self._provider_route is not None and self._provider_route != frozen:
            from ..state import RunJournalIdentityError

            raise RunJournalIdentityError(
                "run provider route cannot be replaced after freeze",
                run_id=self.run_id,
            )
        self._provider_route = frozen

    @property
    def trace_context(self):
        return deepcopy(self._trace_context)

    def bind_trace_context(self, trace_context: object) -> None:
        """Bind an explicit trace carrier for worker propagation."""

        frozen = deepcopy(trace_context)
        if self._trace_context is not None and self._trace_context != frozen:
            from ..state import RunJournalIdentityError

            raise RunJournalIdentityError(
                "run trace context cannot be replaced after freeze",
                run_id=self.run_id,
            )
        self._trace_context = frozen

    @property
    def context_accounting(self):
        return self._context_accounting

    def bind_context_accounting(self, accounting) -> None:
        if self._context_accounting is not None and self._context_accounting is not accounting:
            raise RuntimeError("run context accounting session cannot be replaced")
        self._context_accounting = accounting

    def for_tool_worker(self, *, cancellation_token: CancellationToken) -> RunContext:
        """Build an explicit worker context without relying on thread-local state."""

        worker = RunContext(
            session_id=self.session_id,
            actor_id=self.actor_id,
            role=self.role,
            tenant_id=self.tenant_id,
            course_ids=self.course_ids,
            replay_scope=self.replay_scope,
            run_id=self.run_id,
            started_at=self.started_at,
            budget=self.budget,
            lease_owner=self.lease_owner,
            fencing_token=self.fencing_token,
            cancellation_token=cancellation_token,
        )
        worker._control_check = self._control_check
        worker._run_event_sink = self._run_event_sink
        worker._provider_event_sink = self._provider_event_sink
        worker._tool_manifest = self._tool_manifest
        worker._tool_manifest_hash_override = self._tool_manifest_hash_override
        worker._provider_route = deepcopy(self._provider_route)
        worker._trace_context = deepcopy(self._trace_context)
        worker._argument_retry_calls = self._argument_retry_calls
        worker._argument_retry_lock = self._argument_retry_lock
        return worker

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        actor_id: str,
        role: str,
        tenant_id: str = "default",
        course_ids: set[int] | frozenset[int] | None = None,
        replay_scope: str | None = None,
        run_id: str | None = None,
        max_model_calls: int = 12,
        max_tool_calls: int = 24,
        cancellation_token: CancellationToken | None = None,
    ) -> RunContext:
        return cls(
            session_id=session_id,
            actor_id=actor_id,
            role=role,
            tenant_id=tenant_id,
            course_ids=frozenset(course_ids or ()),
            replay_scope=replay_scope,
            run_id=run_id or uuid.uuid4().hex,
            budget=IterationBudget(
                max_model_calls=max_model_calls,
                max_tool_calls=max_tool_calls,
            ),
            cancellation_token=cancellation_token or CancellationToken(),
        )
