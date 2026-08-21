from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime


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
    _control_check: Callable[[str], None] | None = field(
        default=None,
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
        if self._control_check is not None:
            self._control_check(boundary)

    def bind_control_check(self, control_check: Callable[[str], None]) -> None:
        """Bind a cooperative child control check without a session lease.

        Delegated runs have their own persisted worker lease, rather than
        borrowing the parent's session lease.  Keeping this separate from
        ``bind_runtime_control`` prevents a child from accidentally inheriting
        the parent's fencing token.
        """
        self._control_check = control_check

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
        )
