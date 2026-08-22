from __future__ import annotations

import hashlib
import json
from typing import Any

from ..observability.redaction import RedactionPolicy
from ..runtime.transactions import FaultInjector
from ..state import (
    RunJournalIdentityError,
    RunJournalNotFound,
    RunJournalTransitionError,
    RunPhase,
    RunStableBoundary,
)


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def tool_manifest_hash(tools: list[dict]) -> str:
    return hashlib.sha256(_canonical(tools).encode("utf-8")).hexdigest()


def frozen_route_shape(engine) -> dict[str, Any]:
    routes = engine.begin_turn_routes()
    return {
        "engine": getattr(engine, "name", type(engine).__name__),
        "routes": [route.to_event() for route in routes],
    }


class AgentLoopJournal:
    """Coordinates Agent Loop transitions without becoming another truth source."""

    def __init__(
        self,
        state_store,
        context,
        *,
        tools: list[dict],
        engine,
        context_checkpoint_id: str | None = None,
        fault_injector: FaultInjector | None = None,
    ):
        self.state_store = state_store
        self.context = context
        self.manifest_hash = tool_manifest_hash(tools)
        self.route = RedactionPolicy().redact(frozen_route_shape(engine))
        self.context_checkpoint_id = context_checkpoint_id
        self.faults = fault_injector or FaultInjector()
        self._model_calls_in_invocation = 0
        self.active = bool(
            state_store is not None
            and getattr(context, "lease_owner", None)
            and getattr(context, "fencing_token", None) is not None
        )
        self.snapshot = self._initialize() if self.active else None

    def read(self):
        return self.state_store.get_run_journal_snapshot(
            self.context.run_id,
            session_id=self.context.session_id,
            actor_id=self.context.actor_id,
            tenant_id=self.context.tenant_id,
        )

    def _initialize(self):
        try:
            snapshot = self.read()
        except RunJournalNotFound:
            snapshot = self.state_store.create_run_journal(
                self.context,
                tool_manifest_hash=self.manifest_hash,
                frozen_provider_route=self.route,
                budget_snapshot=self.context.budget.usage(),
                context_checkpoint_id=self.context_checkpoint_id,
            )
        if snapshot.tool_manifest_hash != self.manifest_hash:
            raise RunJournalIdentityError(
                "run tool manifest changed after journal initialization",
                run_id=self.context.run_id,
            )
        if _canonical(snapshot.frozen_provider_route) != _canonical(self.route):
            raise RunJournalIdentityError(
                "run provider route changed after journal initialization",
                run_id=self.context.run_id,
            )
        budget = snapshot.budget_snapshot
        for field in (
            "model_calls",
            "max_model_calls",
            "tool_calls",
            "max_tool_calls",
        ):
            value = budget.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RunJournalIdentityError(
                    "run budget snapshot is invalid",
                    run_id=self.context.run_id,
                    field=field,
                )
            setattr(self.context.budget, field, value)
        if (
            self.context.budget.model_calls > self.context.budget.max_model_calls
            or self.context.budget.tool_calls > self.context.budget.max_tool_calls
            or self.context.budget.max_model_calls == 0
            or self.context.budget.max_tool_calls == 0
        ):
            raise RunJournalIdentityError(
                "run budget snapshot counters are inconsistent",
                run_id=self.context.run_id,
            )
        return snapshot

    def _cas(
        self,
        *,
        phase: RunPhase,
        boundary: RunStableBoundary,
        loop_cursor: int | None = None,
        model_attempt: int | None = None,
        plan_id: str | None = None,
    ):
        current = self.read()
        kwargs = {}
        if plan_id is not None:
            kwargs["plan_id"] = plan_id
        self.snapshot = self.state_store.compare_and_set_run_journal(
            self.context,
            expected_revision=current.revision,
            expected_phase=current.phase,
            phase=phase,
            expected_loop_cursor=current.loop_cursor,
            loop_cursor=current.loop_cursor if loop_cursor is None else loop_cursor,
            expected_model_attempt=current.model_attempt,
            model_attempt=(
                current.model_attempt if model_attempt is None else model_attempt
            ),
            expected_event_sequence=current.event_sequence,
            event_sequence=current.event_sequence + 1,
            expected_fencing_token=current.fencing_token,
            stable_boundary=boundary,
            budget_snapshot=self.context.budget.usage(),
            **kwargs,
        )
        return self.snapshot

    def enter_planning(self, *, plan_id: str | None = None) -> None:
        if not self.active:
            return
        current = self.read()
        if current.phase is RunPhase.ACCEPTED:
            self._cas(
                phase=RunPhase.PLANNING,
                boundary=RunStableBoundary.PLAN_COMMITTED,
                plan_id=plan_id,
            )
            return
        if current.phase is RunPhase.PLANNING and plan_id is not None and current.plan_id is None:
            self._cas(
                phase=RunPhase.PLANNING,
                boundary=RunStableBoundary.PLAN_COMMITTED,
                loop_cursor=current.loop_cursor + 1,
                plan_id=plan_id,
            )

    def start_model_attempt(self) -> int:
        if not self.active:
            self._model_calls_in_invocation += 1
            return self._model_calls_in_invocation
        current = self.read()
        if self._model_calls_in_invocation == 0 and current.phase in {
            RunPhase.MODEL,
            RunPhase.TOOLS,
        }:
            attempt = current.model_attempt
        elif current.phase in {RunPhase.PLANNING, RunPhase.VERIFYING, RunPhase.MODEL}:
            attempt = current.model_attempt + 1
            self._cas(
                phase=RunPhase.MODEL,
                boundary=RunStableBoundary.MODEL_ATTEMPT_STARTED,
                loop_cursor=current.loop_cursor + 1,
                model_attempt=attempt,
            )
        else:
            raise RunJournalTransitionError(
                "journal is not at a model-call boundary",
                phase=current.phase.value,
                model_attempt=current.model_attempt,
            )
        self._model_calls_in_invocation += 1
        return attempt

    def append_envelope(self, message: dict, *, model_attempt: int) -> dict:
        if not self.active:
            return message
        self.faults.hit("before_assistant_envelope_commit")
        committed = self.state_store.append_assistant_tool_envelope(
            self.context,
            message,
            model_attempt=model_attempt,
        )
        self.snapshot = committed.journal
        self.faults.hit("after_assistant_envelope_commit")
        return committed.message

    def model_returned(self) -> None:
        self.faults.hit("after_model_response")

    def call_record(self, tool_call_id: str) -> dict | None:
        if not self.active:
            return None
        return self.state_store.get_tool_call_record(
            run_id=self.context.run_id,
            tool_call_id=tool_call_id,
            session_id=self.context.session_id,
            actor_id=self.context.actor_id,
            tenant_id=self.context.tenant_id,
        )

    def append_result(
        self,
        message: dict,
        *,
        model_attempt: int,
        operation_id: str | None = None,
        tool_event_id: int | None = None,
        allow_cancelled: bool = False,
    ) -> dict:
        if not self.active:
            return message
        point = (
            "after_write_operation_commit_before_result"
            if operation_id is not None
            else "before_read_tool_result_commit"
        )
        self.faults.hit("before_tool_result_commit")
        self.faults.hit(point)
        committed = self.state_store.append_tool_result(
            self.context,
            message,
            model_attempt=model_attempt,
            operation_id=operation_id,
            tool_event_id=tool_event_id,
            allow_cancelled=allow_cancelled,
        )
        self.snapshot = committed.journal
        self.faults.hit("after_tool_result_commit")
        if operation_id is None:
            self.faults.hit("after_read_tool_result_commit")
        return committed.message

    def complete_tool_batch(self, *, model_attempt: int) -> None:
        if not self.active:
            return
        self.snapshot = self.state_store.complete_tool_batch(
            self.context,
            model_attempt=model_attempt,
        )


__all__ = ["AgentLoopJournal", "frozen_route_shape", "tool_manifest_hash"]
