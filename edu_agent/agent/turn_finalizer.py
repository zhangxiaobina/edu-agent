"""The one durable owner of turn completion.

``TurnFinalizer`` is intentionally synchronous.  Provider streaming and SSE
are separate releases; this class only turns an already aggregated agent
result into one durable terminal record.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
import inspect
import threading
from typing import Any, Callable, Iterable, Mapping

from ..planning.models import PlanStatus
from ..state import RunPhase
from ..state.turn_finalizer import FINALIZER_CURSOR, TurnFinalizerRecord


SUCCESS_REASONS = frozenset({None, "completed"})
CANONICAL_STOP_REASONS = frozenset(
    {
        "completed",
        "interrupted",
        "cancelled",
        "budget_exceeded",
        "model_failed",
        "manual_review",
        "invalid",
        "blocked",
        "incomplete",
    }
)
_CLEANUP_HOOK_KEY = "__turn_finalizer_cleanup__"


@dataclass(frozen=True)
class FinalizationResult:
    run_id: str
    session_id: str
    status: str
    stop_reason: str
    final_answer: str | None
    trace: list[dict]
    budget: dict
    context: dict
    usage: list[dict]
    plan: dict | None
    final_message_id: int | None
    cursor: int

    @classmethod
    def from_record(cls, record: TurnFinalizerRecord) -> "FinalizationResult":
        return cls(
            run_id=record.run_id,
            session_id=record.session_id,
            status=record.terminal_status,
            stop_reason=record.stop_reason,
            final_answer=record.final_answer,
            trace=record.trace,
            budget=record.budget,
            context=record.context,
            usage=record.usage,
            plan=record.plan,
            final_message_id=record.final_message_id,
            cursor=record.cursor,
        )


class TurnFinalizer:
    """Resume-safe, database-CAS guarded turn finalization.

    The constructor accepts both the compact ``result=`` form used by the
    service and explicit fields, which keeps it convenient for recovery tests
    and for operators replaying a persisted run.
    """

    def __init__(
        self,
        state_store,
        context,
        *,
        result: Mapping[str, Any] | None = None,
        final_message: Mapping[str, Any] | None = None,
        final_answer: str | None = None,
        trace: list[dict] | None = None,
        usage: list[dict] | None = None,
        budget: dict | None = None,
        stop_reason: str | None = None,
        error: str | None = None,
        plan: dict | None = None,
        plan_coordinator=None,
        evidence_verifier=None,
        plan_verification_error: str | None = None,
        hooks: Iterable[Callable[..., Any]] | Mapping[str, Callable[..., Any]] | None = None,
        post_hooks: Iterable[Callable[..., Any]] | Mapping[str, Callable[..., Any]] | None = None,
        cleanup: Callable[..., Any] | None = None,
        cleanup_timeout_seconds: float = 1.0,
        fault_injector=None,
        finalizer_fault_injector=None,
        context_payload: dict | None = None,
    ):
        self.state_store = state_store
        self.context = context
        self.result = dict(result or {})
        self.final_message = dict(final_message) if final_message is not None else None
        self.final_answer = (
            final_answer
            if final_answer is not None
            else self.result.get("final_answer")
        )
        if self.final_message is not None:
            if (
                self.final_message.get("role") != "assistant"
                or self.final_message.get("tool_calls")
            ):
                raise ValueError("final message must be a plain assistant message")
            content = self.final_message.get("content")
            if content is not None and not isinstance(content, str):
                raise ValueError("final assistant content must be a string or null")
            # Persist one canonical role/content candidate.  Recovery can then
            # reconstruct exactly the same message from ``final_answer``
            # without retaining an arbitrary provider message shape.
            self.final_answer = content or ""
            self.final_message = {"role": "assistant", "content": self.final_answer}
        self.trace = list(trace if trace is not None else self.result.get("trace") or [])
        self.usage = list(usage if usage is not None else self.result.get("usage") or [])
        self.budget = dict(budget if budget is not None else self.result.get("budget") or context.budget.usage())
        self.stop_reason = stop_reason if stop_reason is not None else self.result.get("stop_reason")
        self.error = error
        self.plan = plan if plan is not None else self.result.get("plan")
        self.plan_coordinator = plan_coordinator
        self.evidence_verifier = evidence_verifier
        self.plan_verification_error = plan_verification_error
        self.hooks = post_hooks if post_hooks is not None else hooks
        self.cleanup = cleanup
        if cleanup_timeout_seconds <= 0:
            raise ValueError("cleanup_timeout_seconds must be positive")
        self.cleanup_timeout_seconds = float(cleanup_timeout_seconds)
        self.faults = finalizer_fault_injector or fault_injector
        self.context_payload = dict(context_payload or {})
        self._hook_map = self._normalize_hooks(self.hooks)

    @staticmethod
    def _normalize_hooks(hooks):
        if hooks is None:
            return {}
        if isinstance(hooks, Mapping):
            return {str(key): value for key, value in hooks.items()}
        return {
            getattr(hook, "__name__", f"hook-{index}"): hook
            for index, hook in enumerate(hooks)
        }

    @staticmethod
    def _invoke_compatible(callback, *args):
        """Call one- or two-argument callbacks without re-running internals.

        Catching ``TypeError`` around the call itself can execute a hook twice
        when the hook raised that error after an external side effect.  Bind
        the signature first and only then invoke it once.
        """
        try:
            signature = inspect.signature(callback)
        except (TypeError, ValueError):
            return callback(*args)
        try:
            signature.bind(*args)
        except TypeError:
            signature.bind(args[-1])
            return callback(args[-1])
        return callback(*args)

    def _hit(self, name: str) -> None:
        """Support the naming used by both loop and finalizer fault fixtures."""
        if self.faults is None or not hasattr(self.faults, "hit"):
            return
        # A fixture may use any one of these stable spellings.  ``hit`` is a
        # no-op for absent points, so probing aliases does not alter behavior.
        for point in (f"after_finalizer_{name}", f"finalizer.after_{name}", f"after_{name}"):
            self.faults.hit(point)

    @staticmethod
    def _canonical_stop_reason(reason: str | None, *, error: str | None = None) -> str:
        if reason == "completed":
            return "completed"
        if reason == "cancelled":
            # ``interrupted`` is the established ChatResult/API spelling;
            # journal phase remains the explicit ``cancelled`` branch.
            return "interrupted"
        if reason is None and not error:
            return "completed"
        if reason in CANONICAL_STOP_REASONS:
            return str(reason)
        text = f"{error or ''} {reason or ''}".lower()
        if "manual_review" in text or "uncertain" in text:
            return "manual_review"
        if "budget" in text or "quota" in text:
            return "budget_exceeded"
        if "cancel" in text or "interrupt" in text:
            return "interrupted"
        return "model_failed"

    @staticmethod
    def _terminal_status(stop_reason: str) -> str:
        return "completed" if stop_reason == "completed" else "interrupted" if stop_reason == "interrupted" else "failed"

    def _message(self) -> dict | None:
        if self.final_message is not None:
            return self.final_message
        if self.final_answer is None:
            return None
        return {"role": "assistant", "content": self.final_answer}

    def _record(self) -> TurnFinalizerRecord:
        record = self.state_store.get_turn_finalizer(
            self.context.run_id,
            session_id=self.context.session_id,
            actor_id=self.context.actor_id,
            tenant_id=self.context.tenant_id,
        )
        if record is None:
            reason = self._canonical_stop_reason(self.stop_reason, error=self.error)
            if reason == "completed" and self._message() is None:
                reason = "model_failed"
                self.error = self.error or "successful turn has no final assistant message"
            if reason == "interrupted":
                self.final_answer = None
            record = self.state_store.ensure_turn_finalizer(
                self.context,
                stop_reason=reason,
                terminal_status=self._terminal_status(reason),
                final_answer=self.final_answer,
                trace=self.trace,
                plan=self.plan,
                usage=self.usage,
                budget=self.budget,
                context_payload=self.context_payload,
                error=self.error,
            )
        else:
            # Recovery workers may only have the durable row.  Rehydrate all
            # candidate data before resuming at its cursor; never ask the
            # provider/model to regenerate a response that was already staged.
            self.final_answer = record.final_answer
            self.trace = list(record.trace)
            self.usage = list(record.usage)
            self.budget = dict(record.budget)
            self.plan = record.plan
            self.context_payload = dict(record.context)
            self.stop_reason = record.stop_reason
        return record

    def _close_unpaired(self, record: TurnFinalizerRecord) -> TurnFinalizerRecord:
        if record.cursor >= FINALIZER_CURSOR["tools_closed"]:
            return record
        reason = record.stop_reason
        # Existing R2.3 protocol rows are closed through the same paired append
        # API used by the loop.  Each call has a deterministic cancellation
        # payload and idempotency key, so a crash/retry cannot duplicate it.
        pending = self.state_store.list_tool_call_records(
            run_id=self.context.run_id,
            session_id=self.context.session_id,
            actor_id=self.context.actor_id,
            tenant_id=self.context.tenant_id,
        )
        for call in pending:
            if call.get("status") != "pending":
                continue
            operation_id = call.get("operation_id")
            if operation_id is None:
                operation = self.state_store.get_tool_operation_for_call(
                    run_id=self.context.run_id,
                    tool_call_id=call["tool_call_id"],
                    actor_id=self.context.actor_id,
                    tenant_id=self.context.tenant_id,
                )
                operation_id = operation.get("operation_id") if operation else None
            payload = {
                "ok": False,
                "data": None,
                "error": {
                    "code": "CANCELLED" if reason == "interrupted" else "FINALIZER_CLOSED",
                    "message": f"tool call closed while finalizing ({reason})",
                },
                "meta": {
                    "run_id": self.context.run_id,
                    "tool_call_id": call["tool_call_id"],
                    **({"operation_id": operation_id} if operation_id else {}),
                },
            }
            self.state_store.append_tool_result(
                self.context,
                {
                    "role": "tool",
                    "tool_call_id": call["tool_call_id"],
                    "name": call["tool_name"],
                    "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                },
                model_attempt=int(call["model_attempt"]),
                operation_id=operation_id,
                allow_cancelled=True,
                budget_snapshot=self.budget,
            )
        # A complete envelope can be moved to verifying once every pending
        # call has a paired result.  A model-only failure has no envelope and
        # therefore needs no synthetic journal transition here.
        snapshot = None
        try:
            snapshot = self.state_store.get_run_journal_snapshot(
                self.context.run_id,
                session_id=self.context.session_id,
                actor_id=self.context.actor_id,
                tenant_id=self.context.tenant_id,
            )
        except Exception:
            snapshot = None
        if snapshot is not None and snapshot.phase is RunPhase.TOOLS:
            self.state_store.complete_tool_batch(
                self.context,
                model_attempt=snapshot.model_attempt,
                allow_cancelled=True,
                budget_snapshot=self.budget,
            )
        record = self.state_store.advance_turn_finalizer(
            self.context,
            expected_cursor=0,
            next_cursor=1,
        )
        self._hit("tools_closed")
        return record

    def _verify_plan(self, record: TurnFinalizerRecord) -> TurnFinalizerRecord:
        if record.cursor >= FINALIZER_CURSOR["plan_verified"]:
            return record
        self._enter_finalizing()
        plan = self.plan
        verification: dict[str, Any] = {"checked": False, "ok": True}
        if self.plan is not None and (
            self.plan_coordinator is None or self.evidence_verifier is None
        ) and not self.plan_verification_error:
            self.plan_verification_error = "persisted plan verifier is unavailable"
        if self.plan_verification_error:
            verification = {
                "checked": True,
                "ok": False,
                "error": self.plan_verification_error,
            }
            if record.stop_reason == "completed":
                self.stop_reason = "manual_review"
                self.final_answer = None
                self.error = self.plan_verification_error
            record = self.state_store.advance_turn_finalizer(
                self.context,
                expected_cursor=record.cursor,
                next_cursor=2,
                fields={
                    **(
                        {
                            "stop_reason": "manual_review",
                            "terminal_status": "failed",
                            "final_answer": None,
                            "error": self.error,
                        }
                        if record.stop_reason == "completed"
                        else {}
                    ),
                    "verification_json": json.dumps(
                        verification, ensure_ascii=False, sort_keys=True
                    ),
                },
            )
            self._hit("plan_verified")
            return record
        if self.plan_coordinator is not None and self.evidence_verifier is not None:
            try:
                coordinator = self.plan_coordinator
                current = coordinator.result() or {}
                plan = current
                steps = coordinator.steps()
                answer = self.final_answer or ""
                status = current.get("status")
                complete = status == PlanStatus.completed.value
                if complete:
                    complete = self.evidence_verifier.plan_has_complete_evidence(
                        coordinator.plan.id,
                        steps,
                    ) and self.evidence_verifier.final_answer_citations_valid(
                        coordinator.plan.id,
                        steps,
                        answer,
                    )
                verification = {
                    "checked": True,
                    "ok": bool(complete or status in {
                        PlanStatus.invalid.value,
                        PlanStatus.blocked.value,
                        PlanStatus.incomplete.value,
                        PlanStatus.budget_exceeded.value,
                    }),
                    "plan_status": status,
                }
                if record.stop_reason in SUCCESS_REASONS and not complete:
                    # Do not claim a successful turn when persisted evidence
                    # does not support the final answer.  Keep the existing
                    # Plan status vocabulary and expose a stable stop reason.
                    failure_reason = (
                        status
                        if status in {
                            PlanStatus.invalid.value,
                            PlanStatus.blocked.value,
                            PlanStatus.budget_exceeded.value,
                            PlanStatus.incomplete.value,
                        }
                        else "incomplete"
                    )
                    if status not in {
                        PlanStatus.invalid.value,
                        PlanStatus.blocked.value,
                        PlanStatus.budget_exceeded.value,
                    }:
                        try:
                            coordinator.fail(
                                PlanStatus.incomplete,
                                "finalizer evidence verification failed",
                            )
                        except Exception:
                            # The persisted finalizer record is the recovery
                            # authority; a secondary Plan status write must
                            # not strand it at cursor 1.
                            pass
                    self.stop_reason = failure_reason
                    self.error = f"finalizer evidence verification failed: {failure_reason}"
                    record = self.state_store.advance_turn_finalizer(
                        self.context,
                        expected_cursor=record.cursor,
                        next_cursor=2,
                        fields={
                            "stop_reason": failure_reason,
                            "terminal_status": "failed",
                            "final_answer": None,
                            "error": self.error,
                            "plan_json": json.dumps(plan, ensure_ascii=False, sort_keys=True),
                            "verification_json": json.dumps(verification, ensure_ascii=False, sort_keys=True),
                        },
                    )
                    self._hit("plan_verified")
                    return record
            except Exception as error:
                # Fault injection deliberately models a crash immediately
                # after a durable cursor commit.  Let the caller observe it;
                # recovery must start from the persisted cursor rather than
                # attempting a second verifier write in this stack frame.
                if type(error).__name__ == "InjectedFault":
                    raise
                current_record = self.state_store.get_turn_finalizer(
                    self.context.run_id,
                    session_id=self.context.session_id,
                    actor_id=self.context.actor_id,
                    tenant_id=self.context.tenant_id,
                )
                if current_record is not None and current_record.cursor > record.cursor:
                    return current_record
                # A verifier/storage failure is uncertainty about evidence,
                # not a provider success.  Persist a stable manual-review
                # outcome and continue the remaining terminal steps.
                verification = {
                    "checked": True,
                    "ok": False,
                    "error": f"{type(error).__name__}: {error}",
                }
                self.stop_reason = "manual_review"
                self.error = verification["error"]
                record = self.state_store.advance_turn_finalizer(
                    self.context,
                    expected_cursor=record.cursor,
                    next_cursor=2,
                    fields={
                        "stop_reason": "manual_review",
                        "terminal_status": "failed",
                        "final_answer": None,
                        "plan_json": json.dumps(plan, ensure_ascii=False, sort_keys=True)
                        if plan is not None
                        else None,
                        "verification_json": json.dumps(verification, ensure_ascii=False, sort_keys=True),
                        "error": self.error,
                    },
                )
                self._hit("plan_verified")
                return record
        record = self.state_store.advance_turn_finalizer(
            self.context,
            expected_cursor=record.cursor,
            next_cursor=2,
            fields={
                "plan_json": json.dumps(plan, ensure_ascii=False, sort_keys=True)
                if plan is not None
                else None,
                "verification_json": json.dumps(verification, ensure_ascii=False, sort_keys=True),
            },
        )
        self._hit("plan_verified")
        return record

    def _enter_finalizing(self) -> None:
        """Move the durable loop to ``finalizing`` exactly once.

        R2.3 can leave a run in ``model``/``verifying`` when the aggregate
        response is returned.  This transition is the journal proof that no
        more model/tool work may be scheduled while the cursor is closing.
        """
        try:
            snapshot = self.state_store.get_run_journal_snapshot(
                self.context.run_id,
                session_id=self.context.session_id,
                actor_id=self.context.actor_id,
                tenant_id=self.context.tenant_id,
            )
        except Exception:
            return
        if snapshot.phase in {
            RunPhase.FINALIZING,
            RunPhase.TERMINAL,
            RunPhase.CANCELLED,
            RunPhase.FAILED,
        }:
            return
        if snapshot.phase is RunPhase.TOOLS:
            try:
                snapshot = self.state_store.complete_tool_batch(
                    self.context,
                    model_attempt=snapshot.model_attempt,
                    allow_cancelled=True,
                    budget_snapshot=self.budget,
                )
            except Exception:
                return
        try:
            self.state_store.compare_and_set_run_journal(
                self.context,
                expected_revision=snapshot.revision,
                expected_phase=snapshot.phase,
                phase=RunPhase.FINALIZING,
                expected_loop_cursor=snapshot.loop_cursor,
                loop_cursor=snapshot.loop_cursor,
                expected_model_attempt=snapshot.model_attempt,
                model_attempt=snapshot.model_attempt,
                expected_event_sequence=snapshot.event_sequence,
                event_sequence=snapshot.event_sequence + 1,
                expected_fencing_token=snapshot.fencing_token,
                stable_boundary="verification_committed",
                budget_snapshot=self.budget,
            )
        except Exception:
            # A concurrent recovery worker may have won the CAS.  The next
            # cursor operation reads the durable row and continues safely.
            return

    def _commit_message(self, record: TurnFinalizerRecord) -> TurnFinalizerRecord:
        if record.cursor >= FINALIZER_CURSOR["final_message_committed"]:
            return record
        message = self._message() if record.stop_reason == "completed" else None
        record = self.state_store.commit_final_message(
            self.context,
            expected_cursor=record.cursor,
            message=message,
        )
        self._hit("final_message_committed")
        return record

    def _settle(self, record: TurnFinalizerRecord) -> TurnFinalizerRecord:
        if record.cursor >= FINALIZER_CURSOR["usage_settled"]:
            return record
        self._hit("before_usage_settled")
        record = self.state_store.settle_turn_usage(
            self.context,
            expected_cursor=record.cursor,
        )
        self._hit("usage_settled")
        return record

    def _terminal(self, record: TurnFinalizerRecord) -> TurnFinalizerRecord:
        if record.cursor >= FINALIZER_CURSOR["terminal"]:
            return record
        record = self.state_store.mark_turn_terminal(
            self.context,
            expected_cursor=record.cursor,
        )
        self._hit("terminal")
        return record

    def _hooks(self, record: TurnFinalizerRecord) -> TurnFinalizerRecord:
        if record.cursor >= FINALIZER_CURSOR["hooks_done"]:
            return record
        outcomes: dict[str, Any] = {}
        for key, hook in self._hook_map.items():
            if not callable(hook):
                continue
            if not self.state_store.claim_turn_finalizer_hook(self.context, hook_key=key):
                outcomes[key] = "already_claimed"
                continue
            try:
                value = self._invoke_compatible(
                    hook,
                    self.context,
                    FinalizationResult.from_record(record),
                )
                self.state_store.complete_turn_finalizer_hook(
                    self.context,
                    hook_key=key,
                    success=True,
                    details={"result": value} if value is not None else {},
                )
                outcomes[key] = "completed"
            except Exception as error:  # hooks never reverse the main turn
                self.state_store.complete_turn_finalizer_hook(
                    self.context,
                    hook_key=key,
                    success=False,
                    error=f"{type(error).__name__}: {error}",
                    details={},
                )
                outcomes[key] = "failed"
        record = self.state_store.finish_turn_finalizer_hooks(
            self.context,
            expected_cursor=record.cursor,
            hooks=outcomes,
        )
        self._hit("hooks_done")
        return record

    def _cleanup(self, record: TurnFinalizerRecord) -> TurnFinalizerRecord:
        if record.cursor >= FINALIZER_CURSOR["cleanup_done"]:
            return record
        if self.cleanup is not None:
            # Claim cleanup in the durable hook table before invoking any
            # external callback.  A second/recovery worker observes the claim
            # and advances its cursor without repeating a side effect.
            claimed = self.state_store.claim_turn_finalizer_hook(
                self.context,
                hook_key=_CLEANUP_HOOK_KEY,
            )
            if claimed:
                outcome = "completed"
                details: dict[str, Any] = {}
                errors: list[Exception] = []

                def invoke_cleanup() -> None:
                    try:
                        return self._invoke_compatible(
                            self.cleanup,
                            self.context,
                            FinalizationResult.from_record(record),
                        )
                    except Exception as error:
                        errors.append(error)

                worker = threading.Thread(
                    target=invoke_cleanup,
                    name=f"turn-finalizer-cleanup-{self.context.run_id[:8]}",
                    daemon=True,
                )
                worker.start()
                worker.join(timeout=self.cleanup_timeout_seconds)
                if worker.is_alive():
                    outcome = "timeout"
                    details = {"timeout_seconds": self.cleanup_timeout_seconds}
                    self.state_store.record_audit_event(
                        actor_id=self.context.actor_id,
                        tenant_id=self.context.tenant_id,
                        action="turn_finalizer.cleanup",
                        resource=f"run:{self.context.run_id}",
                        decision="timeout",
                        details=details,
                    )
                elif errors:
                    error = errors[0]
                    outcome = "failed"
                    details = {"error": f"{type(error).__name__}: {error}"}
                    self.state_store.record_audit_event(
                        actor_id=self.context.actor_id,
                        tenant_id=self.context.tenant_id,
                        action="turn_finalizer.cleanup",
                        resource=f"run:{self.context.run_id}",
                        decision="failed",
                        details=details,
                    )
                self.state_store.complete_turn_finalizer_hook(
                    self.context,
                    hook_key=_CLEANUP_HOOK_KEY,
                    success=outcome == "completed",
                    error=None if outcome == "completed" else outcome,
                    details=details,
                )
        record = self.state_store.finish_turn_finalizer_cleanup(
            self.context,
            expected_cursor=record.cursor,
        )
        self._hit("cleanup_done")
        return record

    def _finalize_once(self) -> FinalizationResult:
        record = self._record()
        # The persisted row is authoritative when a new worker resumes it.
        self.stop_reason = record.stop_reason
        self.final_answer = record.final_answer
        self.plan = record.plan
        self.trace = list(record.trace)
        self.usage = list(record.usage)
        self.budget = dict(record.budget)
        for field in ("model_calls", "max_model_calls", "tool_calls", "max_tool_calls"):
            value = self.budget.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                setattr(self.context.budget, field, value)
        if record.cursor < FINALIZER_CURSOR["terminal"]:
            self.state_store.assert_run_writable(
                self.context,
                boundary="turn_finalizer.resume",
                allow_cancelled=True,
                require_lease=True,
            )
        if record.cursor < 1:
            record = self._close_unpaired(record)
        if record.cursor < 2:
            record = self._verify_plan(record)
        if record.cursor < 3:
            record = self._commit_message(record)
        if record.cursor < 4:
            record = self._settle(record)
        if record.cursor < 5:
            record = self._terminal(record)
        if record.cursor < 6:
            record = self._hooks(record)
        if record.cursor < 7:
            record = self._cleanup(record)
        return FinalizationResult.from_record(record)

    def finalize(self) -> FinalizationResult:
        """Run the cursor with bounded retries for a competing worker CAS."""
        for attempt in range(8):
            try:
                return self._finalize_once()
            except RuntimeError as error:
                text = str(error).lower()
                retryable = (
                    type(error).__name__ not in {"InjectedFault", "RunCancelled"}
                    and any(token in text for token in ("conflict", "race", "compare-and-set", "cas"))
                )
                if not retryable or attempt == 7:
                    raise
        raise RuntimeError("turn finalizer retry budget exhausted")

    __call__ = finalize


__all__ = ["FinalizationResult", "TurnFinalizer"]
