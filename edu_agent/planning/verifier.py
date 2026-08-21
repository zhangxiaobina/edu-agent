from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ..runtime.models import RunContext
from .models import EvidenceStatus, PlanStatus, PlanStep, StepStatus


@dataclass(frozen=True)
class StepVerification:
    completed: bool
    blocked: bool
    missing_conditions: tuple[str, ...]
    failure_reason: str | None = None


def _find_values(value: Any, keys: set[str]) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and isinstance(item, (str, int)):
                found.append(str(item))
            found.extend(_find_values(item, keys))
    elif isinstance(value, list):
        for item in value:
            found.extend(_find_values(item, keys))
    return found


class EvidenceVerifier:
    def __init__(
        self,
        state_store,
        context: RunContext,
        *,
        max_step_retries: int,
        citation_verifier=None,
        citation_claim_verifier=None,
    ):
        self.state_store = state_store
        self.context = context
        self.max_step_retries = max_step_retries
        self.citation_verifier = citation_verifier
        self.citation_claim_verifier = citation_claim_verifier

    def verify_step(self, plan_id: str, step: PlanStep) -> StepVerification:
        events = self.state_store.get_tool_events(
            run_id=self.context.run_id,
            session_id=self.context.session_id,
            after_id=step.event_cursor,
        )
        new_failures: list[str] = []
        for event in events:
            try:
                outcome = json.loads(event["outcome_json"])
            except (TypeError, json.JSONDecodeError):
                outcome = {
                    "ok": False,
                    "error": {
                        "code": "MALFORMED_TOOL_EVENT",
                        "message": "tool event outcome 不是合法 JSON",
                    },
                }
            if not isinstance(outcome, dict):
                outcome = {
                    "ok": False,
                    "error": {
                        "code": "MALFORMED_TOOL_EVENT",
                        "message": "tool event outcome 必须是 JSON object",
                    },
                }
            if outcome.get("ok") is True:
                operation_id = event.get("operation_id")
                operation_status = event.get("operation_status")
                if operation_id:
                    operation = self.state_store.get_tool_operation_ref(
                        operation_id,
                        actor_id=self.context.actor_id,
                        tenant_id=self.context.tenant_id,
                    )
                    operation_status = operation["status"] if operation else operation_status
                if operation_id and operation_status != "committed":
                    reason = f"OPERATION_{str(operation_status).upper()}"
                    inserted = self.state_store.record_plan_evidence(
                        plan_id=plan_id,
                        step_id=step.id,
                        context=self.context,
                        kind="tool_event",
                        status=EvidenceStatus.rejected.value,
                        tool_name=event["tool_name"],
                        tool_event_id=event["id"],
                        operation_id=operation_id,
                        failure_reason=reason,
                        payload=outcome,
                    )
                    if inserted:
                        new_failures.append(reason)
                else:
                    self._record_tool_success(plan_id, step, event, outcome)
            else:
                error = outcome.get("error") or {}
                reason = str(error.get("code") or error.get("message") or "TOOL_FAILED")
                inserted = self.state_store.record_plan_evidence(
                    plan_id=plan_id,
                    step_id=step.id,
                    context=self.context,
                    kind="tool_event",
                    status=EvidenceStatus.rejected.value,
                    tool_name=event["tool_name"],
                    tool_event_id=event["id"],
                    failure_reason=reason,
                    payload=outcome,
                )
                if inserted:
                    new_failures.append(reason)

        evidence = self.state_store.get_step_evidence(
            plan_id,
            step.id,
            actor_id=self.context.actor_id,
            tenant_id=self.context.tenant_id,
        )
        missing = self._missing_conditions(step, evidence)
        if not missing:
            self.state_store.update_plan_step(
                plan_id,
                step.id,
                status=StepStatus.completed.value,
                failure_reason=None,
                context=self.context,
            )
            return StepVerification(True, False, ())
        if new_failures:
            return self._register_retry(plan_id, step, ",".join(sorted(set(new_failures))), missing)
        return StepVerification(False, False, tuple(missing))

    def reject_premature_answer(self, plan_id: str, step: PlanStep) -> StepVerification:
        evidence = self.state_store.get_step_evidence(
            plan_id,
            step.id,
            actor_id=self.context.actor_id,
            tenant_id=self.context.tenant_id,
        )
        missing = self._missing_conditions(step, evidence)
        if not missing:
            return StepVerification(True, False, ())
        self.state_store.record_plan_evidence(
            plan_id=plan_id,
            step_id=step.id,
            context=self.context,
            kind="missing",
            status=EvidenceStatus.rejected.value,
            failure_reason="MODEL_FINAL_WITHOUT_EVIDENCE",
            payload={"missing_conditions": missing},
        )
        return self._register_retry(
            plan_id,
            step,
            "MODEL_FINAL_WITHOUT_EVIDENCE",
            missing,
        )

    def _register_retry(
        self,
        plan_id: str,
        step: PlanStep,
        reason: str,
        missing: list[str],
    ) -> StepVerification:
        if "BUDGET_EXCEEDED" in reason:
            self.state_store.update_plan_step(
                plan_id,
                step.id,
                status=StepStatus.blocked.value,
                failure_reason=reason,
                context=self.context,
            )
            self.state_store.update_plan(
                plan_id,
                status=PlanStatus.budget_exceeded.value,
                failure_reason=reason,
                context=self.context,
            )
            return StepVerification(False, True, tuple(missing), reason)
        retry_count = self.state_store.increment_step_retry(
            plan_id,
            step.id,
            failure_reason=reason,
            context=self.context,
        )
        blocked = retry_count > self.max_step_retries
        if blocked:
            self.state_store.update_plan_step(
                plan_id,
                step.id,
                status=StepStatus.blocked.value,
                failure_reason=reason,
                context=self.context,
            )
            self.state_store.update_plan(
                plan_id,
                status=PlanStatus.blocked.value,
                failure_reason=f"步骤 {step.id} 超过重试上限：{reason}",
                context=self.context,
            )
        return StepVerification(False, blocked, tuple(missing), reason)

    def _record_tool_success(
        self,
        plan_id: str,
        step: PlanStep,
        event: dict,
        outcome: dict,
    ) -> None:
        allowed = event["tool_name"] in step.allowed_tools
        self.state_store.record_plan_evidence(
            plan_id=plan_id,
            step_id=step.id,
            context=self.context,
            kind="tool_event",
            status=(EvidenceStatus.accepted if allowed else EvidenceStatus.rejected).value,
            tool_name=event["tool_name"],
            tool_event_id=event["id"],
            operation_id=event.get("operation_id"),
            failure_reason=None if allowed else "PLAN_SCOPE_DENIED",
            payload=outcome,
        )
        if not allowed:
            return
        artifact_ids = _find_values(outcome, {"artifact_id"})
        for artifact_id in artifact_ids:
            artifact = self.state_store.verify_artifact(
                artifact_id,
                actor_id=self.context.actor_id,
                tenant_id=self.context.tenant_id,
                run_id=self.context.run_id,
                session_id=self.context.session_id,
            )
            accepted = artifact is not None
            self.state_store.record_plan_evidence(
                plan_id=plan_id,
                step_id=step.id,
                context=self.context,
                kind="artifact",
                status=(EvidenceStatus.accepted if accepted else EvidenceStatus.rejected).value,
                tool_name=event["tool_name"],
                tool_event_id=event["id"],
                artifact_id=artifact_id,
                failure_reason=None if accepted else "ARTIFACT_SCOPE_MISMATCH",
                payload={"artifact": artifact or {}},
            )
        citations = _find_values(outcome, {"citation", "citation_id", "chunk_id"})
        for citation in citations:
            accepted = bool(
                self.citation_verifier
                and self.citation_verifier(citation, self.context)
            )
            self.state_store.record_plan_evidence(
                plan_id=plan_id,
                step_id=step.id,
                context=self.context,
                kind="citation",
                status=(EvidenceStatus.accepted if accepted else EvidenceStatus.rejected).value,
                tool_name=event["tool_name"],
                tool_event_id=event["id"],
                citation=citation,
                failure_reason=None if accepted else "CITATION_SCOPE_MISMATCH",
                payload={"citation": citation},
            )

    @staticmethod
    def _missing_conditions(step: PlanStep, evidence: list[dict]) -> list[str]:
        accepted = [item for item in evidence if item["status"] == EvidenceStatus.accepted.value]
        missing = []
        for condition in step.completion_conditions:
            if condition.kind == "tool_success":
                matched = any(
                    item["kind"] == "tool_event" and item["tool_name"] == condition.tool
                    for item in accepted
                )
            elif condition.kind == "artifact":
                matched = any(
                    item["kind"] == "artifact"
                    and (condition.tool is None or item["tool_name"] == condition.tool)
                    for item in accepted
                )
            else:
                matched = any(
                    item["kind"] == "citation"
                    and (condition.tool is None or item["tool_name"] == condition.tool)
                    for item in accepted
                )
            if not matched:
                suffix = f":{condition.tool}" if condition.tool else ""
                missing.append(f"{condition.kind}{suffix}")
        return missing

    def plan_has_complete_evidence(self, plan_id: str, steps: list[PlanStep]) -> bool:
        """Re-check every completion condition immediately before finalization."""
        for step in steps:
            evidence = self.state_store.get_step_evidence(
                plan_id,
                step.id,
                actor_id=self.context.actor_id,
                tenant_id=self.context.tenant_id,
            )
            for item in evidence:
                if item["status"] != EvidenceStatus.accepted.value:
                    continue
                if item["kind"] == "artifact":
                    if not item["artifact_id"] or self.state_store.verify_artifact(
                        item["artifact_id"],
                        actor_id=self.context.actor_id,
                        tenant_id=self.context.tenant_id,
                        run_id=self.context.run_id,
                        session_id=self.context.session_id,
                    ) is None:
                        return False
                if item["kind"] == "citation":
                    if not self.citation_verifier or not self.citation_verifier(
                        item["citation"], self.context
                    ):
                        return False
            if self._missing_conditions(step, evidence):
                return False
        return bool(steps)

    def final_answer_citations_valid(
        self,
        plan_id: str,
        steps: list[PlanStep],
        answer: str,
    ) -> bool:
        requires_citation = any(
            condition.kind == "citation"
            for step in steps
            for condition in step.completion_conditions
        )
        if not requires_citation:
            return True
        evidence = self.state_store.get_plan_evidence(
            plan_id,
            actor_id=self.context.actor_id,
            tenant_id=self.context.tenant_id,
        )
        citations = {
            item["citation"]
            for item in evidence
            if item["kind"] == "citation"
            and item["status"] == EvidenceStatus.accepted.value
            and item["citation"]
        }
        for citation in sorted(citations):
            if citation not in answer:
                continue
            if not self.citation_verifier or not self.citation_verifier(
                citation, self.context
            ):
                continue
            if self.citation_claim_verifier is None:
                return True
            segment = next(
                (
                    part
                    for part in re.split(r"[\n。！？]", answer)
                    if citation in part
                ),
                "",
            )
            claim = segment.replace(citation, "").strip(" []()（）:：")
            if claim and self.citation_claim_verifier(
                citation, claim, self.context
            ):
                return True
        return False

    def missing_conditions(self, plan_id: str, step: PlanStep) -> tuple[str, ...]:
        evidence = self.state_store.get_step_evidence(
            plan_id,
            step.id,
            actor_id=self.context.actor_id,
            tenant_id=self.context.tenant_id,
        )
        return tuple(self._missing_conditions(step, evidence))
