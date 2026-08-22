from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..observability.redaction import RedactionPolicy
from ..state import (
    RunJournalCorrupt,
    RunJournalNotFound,
    RunPhase,
    RunStableBoundary,
)


class RecoveryAction(str, Enum):
    CONTINUE = "continue"
    REPLAY_READ = "replay-read"
    REUSE_OPERATION = "reuse-operation"
    MANUAL_REVIEW = "manual-review"
    TERMINAL_REPLAY = "terminal-replay"


_SAFE_OPERATION_STATES = frozenset({"prepared", "approved", "failed"})
_UNCERTAIN_OPERATION_STATES = frozenset(
    {"executing", "compensating", "compensated", "manual_review"}
)
_TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "interrupted"})
_TERMINAL_PHASES = frozenset(
    {RunPhase.TERMINAL, RunPhase.CANCELLED, RunPhase.FAILED}
)

STABLE_CURSOR_DECISION_TABLE: dict[
    RunStableBoundary,
    frozenset[RecoveryAction],
] = {
    RunStableBoundary.ACCEPTED: frozenset({RecoveryAction.CONTINUE}),
    RunStableBoundary.PLAN_COMMITTED: frozenset({RecoveryAction.CONTINUE}),
    RunStableBoundary.MODEL_ATTEMPT_STARTED: frozenset({RecoveryAction.CONTINUE}),
    RunStableBoundary.ASSISTANT_ENVELOPE_COMMITTED: frozenset(
        {
            RecoveryAction.CONTINUE,
            RecoveryAction.REPLAY_READ,
            RecoveryAction.REUSE_OPERATION,
            RecoveryAction.MANUAL_REVIEW,
        }
    ),
    RunStableBoundary.TOOL_RESULT_COMMITTED: frozenset(
        {
            RecoveryAction.CONTINUE,
            RecoveryAction.REPLAY_READ,
            RecoveryAction.REUSE_OPERATION,
            RecoveryAction.MANUAL_REVIEW,
        }
    ),
    RunStableBoundary.VERIFICATION_COMMITTED: frozenset({RecoveryAction.CONTINUE}),
    RunStableBoundary.FINAL_MESSAGE_COMMITTED: frozenset({RecoveryAction.CONTINUE}),
    RunStableBoundary.TERMINAL: frozenset({RecoveryAction.TERMINAL_REPLAY}),
    RunStableBoundary.CANCELLED: frozenset({RecoveryAction.TERMINAL_REPLAY}),
    RunStableBoundary.FAILED: frozenset({RecoveryAction.TERMINAL_REPLAY}),
}


@dataclass(frozen=True)
class RecoveryDecision:
    run_id: str
    session_id: str
    action: RecoveryAction
    reason: str
    next_step: str
    phase: str | None = None
    stable_boundary: str | None = None
    loop_cursor: int | None = None
    model_attempt: int | None = None
    event_sequence: int = 0
    finalizer_cursor: int | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    operation_id: str | None = None
    operation_status: str | None = None
    tool_manifest_hash: str | None = None
    frozen_provider_route: dict[str, Any] | None = None
    budget_snapshot: dict[str, Any] | None = None

    @property
    def resumable(self) -> bool:
        return self.action is not RecoveryAction.MANUAL_REVIEW

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "action": self.action.value,
            "reason": self.reason,
            "next_step": self.next_step,
            "phase": self.phase,
            "stable_boundary": self.stable_boundary,
            "loop_cursor": self.loop_cursor,
            "model_attempt": self.model_attempt,
            "event_sequence": self.event_sequence,
            "finalizer_cursor": self.finalizer_cursor,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "operation_id": self.operation_id,
            "operation_status": self.operation_status,
            "tool_manifest_hash": self.tool_manifest_hash,
            "frozen_provider_route": copy.deepcopy(self.frozen_provider_route),
            "budget_snapshot": copy.deepcopy(self.budget_snapshot),
        }

    def to_safe_dict(self) -> dict[str, Any]:
        return RedactionPolicy().redact(self.to_dict())


class RecoveryManualReviewRequired(RuntimeError):
    def __init__(self, decision: RecoveryDecision):
        super().__init__(
            f"run {decision.run_id} requires manual review: {decision.reason}"
        )
        self.decision = decision


class RunRecoveryPlanner:
    """Derive one fail-closed action from durable recovery truth."""

    def __init__(self, state_store, tools_provider):
        self.state_store = state_store
        self.tools_provider = tools_provider

    def decide(
        self,
        run_id: str,
        *,
        actor_id: str,
        tenant_id: str,
    ) -> RecoveryDecision:
        run = self.state_store.get_run_status(
            run_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
        )
        if run is None:
            raise KeyError(f"run does not exist: {run_id}")
        session_id = str(run["session_id"])
        finalizer = self.state_store.get_turn_finalizer(
            run_id,
            session_id=session_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
        )
        try:
            journal = self.state_store.get_run_journal_snapshot(
                run_id,
                session_id=session_id,
                actor_id=actor_id,
                tenant_id=tenant_id,
            )
        except RunJournalNotFound:
            journal = None
        except RunJournalCorrupt as error:
            return RecoveryDecision(
                run_id=run_id,
                session_id=session_id,
                action=RecoveryAction.MANUAL_REVIEW,
                reason=f"run journal is corrupt: {error}",
                next_step="operator-inspection",
            )

        common = self._journal_fields(journal)
        if journal is not None:
            budget_error = self._budget_error(journal.budget_snapshot)
            if budget_error is not None:
                return RecoveryDecision(
                    run_id=run_id,
                    session_id=session_id,
                    action=RecoveryAction.MANUAL_REVIEW,
                    reason=budget_error,
                    next_step="operator-inspection",
                    **common,
                )
        if finalizer is not None:
            if finalizer.terminal:
                return RecoveryDecision(
                    run_id=run_id,
                    session_id=session_id,
                    action=RecoveryAction.TERMINAL_REPLAY,
                    reason="durable turn finalizer is terminal",
                    next_step="rebuild-chat-result",
                    finalizer_cursor=finalizer.cursor,
                    **common,
                )
            return RecoveryDecision(
                run_id=run_id,
                session_id=session_id,
                action=RecoveryAction.CONTINUE,
                reason="durable turn finalizer has an incomplete cursor",
                next_step=f"finalizer:{finalizer.step}",
                finalizer_cursor=finalizer.cursor,
                **common,
            )

        if run["status"] in _TERMINAL_RUN_STATUSES:
            return RecoveryDecision(
                run_id=run_id,
                session_id=session_id,
                action=RecoveryAction.TERMINAL_REPLAY,
                reason="run is already terminal",
                next_step="rebuild-chat-result",
                **common,
            )
        if journal is None:
            return RecoveryDecision(
                run_id=run_id,
                session_id=session_id,
                action=RecoveryAction.MANUAL_REVIEW,
                reason="non-terminal run has no declared stable journal boundary",
                next_step="operator-inspection",
            )
        if journal.phase in _TERMINAL_PHASES:
            return RecoveryDecision(
                run_id=run_id,
                session_id=session_id,
                action=RecoveryAction.TERMINAL_REPLAY,
                reason="run journal is terminal",
                next_step="rebuild-chat-result",
                **common,
            )
        if run.get("recovery_recommendation") == "manual_review":
            return RecoveryDecision(
                run_id=run_id,
                session_id=session_id,
                action=RecoveryAction.MANUAL_REVIEW,
                reason="startup recovery found an uncertain write operation",
                next_step="operator-inspection",
                **common,
            )
        if journal.phase is RunPhase.TOOLS:
            pending = [
                call
                for call in self.state_store.list_tool_call_records(
                    run_id=run_id,
                    session_id=session_id,
                    actor_id=actor_id,
                    tenant_id=tenant_id,
                )
                if call.get("status") == "pending"
            ]
            if pending:
                return self._pending_tool_decision(
                    run_id=run_id,
                    session_id=session_id,
                    actor_id=actor_id,
                    tenant_id=tenant_id,
                    call=pending[0],
                    common=common,
                )
        return RecoveryDecision(
            run_id=run_id,
            session_id=session_id,
            action=RecoveryAction.CONTINUE,
            reason=self._continue_reason(journal.stable_boundary),
            next_step=self._next_step(journal.phase),
            **common,
        )

    @staticmethod
    def _journal_fields(journal) -> dict[str, Any]:
        if journal is None:
            return {}
        return {
            "phase": journal.phase.value,
            "stable_boundary": journal.stable_boundary.value,
            "loop_cursor": journal.loop_cursor,
            "model_attempt": journal.model_attempt,
            "event_sequence": journal.event_sequence,
            "tool_manifest_hash": journal.tool_manifest_hash,
            "frozen_provider_route": journal.frozen_provider_route,
            "budget_snapshot": journal.budget_snapshot,
        }

    @staticmethod
    def _budget_error(budget: dict[str, Any]) -> str | None:
        required = (
            "model_calls",
            "max_model_calls",
            "tool_calls",
            "max_tool_calls",
        )
        for field in required:
            value = budget.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return f"journal budget field {field} is invalid"
        if (
            budget["model_calls"] > budget["max_model_calls"]
            or budget["tool_calls"] > budget["max_tool_calls"]
            or budget["max_model_calls"] == 0
            or budget["max_tool_calls"] == 0
        ):
            return "journal budget counters are inconsistent"
        return None

    def _pending_tool_decision(
        self,
        *,
        run_id: str,
        session_id: str,
        actor_id: str,
        tenant_id: str,
        call: dict[str, Any],
        common: dict[str, Any],
    ) -> RecoveryDecision:
        call_id = str(call["tool_call_id"])
        tool_name = str(call["tool_name"])
        operation = self.state_store.get_tool_operation_for_call(
            run_id=run_id,
            tool_call_id=call_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
        )
        if operation is not None:
            status = str(operation["status"])
            operation_fields = {
                "tool_call_id": call_id,
                "tool_name": tool_name,
                "operation_id": str(operation["operation_id"]),
                "operation_status": status,
            }
            if status == "committed":
                return RecoveryDecision(
                    run_id=run_id,
                    session_id=session_id,
                    action=RecoveryAction.REUSE_OPERATION,
                    reason="write operation has a committed idempotent receipt",
                    next_step="pair-committed-operation-result",
                    **operation_fields,
                    **common,
                )
            if status in _SAFE_OPERATION_STATES:
                return RecoveryDecision(
                    run_id=run_id,
                    session_id=session_id,
                    action=RecoveryAction.CONTINUE,
                    reason=(
                        "write operation has not crossed the uncertain executing boundary"
                    ),
                    next_step="resume-existing-operation",
                    **operation_fields,
                    **common,
                )
            if status in _UNCERTAIN_OPERATION_STATES:
                return RecoveryDecision(
                    run_id=run_id,
                    session_id=session_id,
                    action=RecoveryAction.MANUAL_REVIEW,
                    reason=f"write operation state {status} is not safe to replay",
                    next_step="operator-inspection",
                    **operation_fields,
                    **common,
                )
            return RecoveryDecision(
                run_id=run_id,
                session_id=session_id,
                action=RecoveryAction.MANUAL_REVIEW,
                reason=f"write operation has unknown state {status}",
                next_step="operator-inspection",
                **operation_fields,
                **common,
            )

        try:
            arguments = json.loads(call["arguments_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            arguments = None
        spec = (
            self.tools_provider.get_spec(tool_name)
            if hasattr(self.tools_provider, "get_spec")
            else None
        )
        if spec is None or not isinstance(arguments, dict):
            return RecoveryDecision(
                run_id=run_id,
                session_id=session_id,
                action=RecoveryAction.MANUAL_REVIEW,
                reason="pending tool effect cannot be classified from the frozen envelope",
                next_step="operator-inspection",
                tool_call_id=call_id,
                tool_name=tool_name,
                **common,
            )
        try:
            mutating = bool(spec.is_mutating(arguments))
        except Exception:
            return RecoveryDecision(
                run_id=run_id,
                session_id=session_id,
                action=RecoveryAction.MANUAL_REVIEW,
                reason="pending tool mutation policy could not be evaluated",
                next_step="operator-inspection",
                tool_call_id=call_id,
                tool_name=tool_name,
                **common,
            )
        if mutating:
            return RecoveryDecision(
                run_id=run_id,
                session_id=session_id,
                action=RecoveryAction.CONTINUE,
                reason="write call has no prepared operation, so its handler was not entered",
                next_step="prepare-write-operation",
                tool_call_id=call_id,
                tool_name=tool_name,
                **common,
            )
        return RecoveryDecision(
            run_id=run_id,
            session_id=session_id,
            action=RecoveryAction.REPLAY_READ,
            reason="read result has no durable paired result and may be replayed",
            next_step="replay-read-tool",
            tool_call_id=call_id,
            tool_name=tool_name,
            **common,
        )

    @staticmethod
    def _continue_reason(boundary: RunStableBoundary) -> str:
        return {
            RunStableBoundary.ACCEPTED: "request acceptance is the last stable boundary",
            RunStableBoundary.PLAN_COMMITTED: "persistent plan boundary is complete",
            RunStableBoundary.MODEL_ATTEMPT_STARTED: (
                "model completion was not durably proven; continue the frozen attempt"
            ),
            RunStableBoundary.ASSISTANT_ENVELOPE_COMMITTED: (
                "tool envelope is durable and has no pending calls"
            ),
            RunStableBoundary.TOOL_RESULT_COMMITTED: (
                "all committed tool results will be reused"
            ),
            RunStableBoundary.VERIFICATION_COMMITTED: (
                "verification/finalizer boundary is durable"
            ),
            RunStableBoundary.FINAL_MESSAGE_COMMITTED: (
                "unique final message is durable; continue finalizer settlement"
            ),
            RunStableBoundary.TERMINAL: "terminal state is durable",
            RunStableBoundary.CANCELLED: "cancelled state is durable",
            RunStableBoundary.FAILED: "failed state is durable",
        }[boundary]

    @staticmethod
    def _next_step(phase: RunPhase) -> str:
        return {
            RunPhase.ACCEPTED: "planning",
            RunPhase.PLANNING: "model",
            RunPhase.MODEL: "model",
            RunPhase.TOOLS: "complete-tool-batch",
            RunPhase.VERIFYING: "verify-or-model",
            RunPhase.FINALIZING: "finalizer",
            RunPhase.TERMINAL: "rebuild-chat-result",
            RunPhase.CANCELLED: "rebuild-chat-result",
            RunPhase.FAILED: "rebuild-chat-result",
        }[phase]


__all__ = [
    "RecoveryAction",
    "RecoveryDecision",
    "RecoveryManualReviewRequired",
    "RunRecoveryPlanner",
    "STABLE_CURSOR_DECISION_TABLE",
]
