from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .cancellation import CancellationToken


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class IterationBudget:
    max_model_calls: int = 12
    max_tool_calls: int = 24
    model_calls: int = 0
    tool_calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def consume_model_call(self) -> None:
        with self._lock:
            if self.model_calls >= self.max_model_calls:
                raise BudgetExceeded(
                    f"模型调用预算已耗尽（{self.model_calls}/{self.max_model_calls}）"
                )
            self.model_calls += 1

    def consume_tool_call(self) -> None:
        with self._lock:
            if self.tool_calls >= self.max_tool_calls:
                raise BudgetExceeded(
                    f"工具调用预算已耗尽（{self.tool_calls}/{self.max_tool_calls}）"
                )
            self.tool_calls += 1

    def usage(self) -> dict:
        return {
            "model_calls": self.model_calls,
            "max_model_calls": self.max_model_calls,
            "tool_calls": self.tool_calls,
            "max_tool_calls": self.max_tool_calls,
        }


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
