from __future__ import annotations

import json
import sys

import pytest

from edu_agent.observability import (
    RedactionPolicy,
    TraceRepository,
    contains_sensitive_data,
)
from edu_agent.runtime import (
    BudgetAmounts,
    BudgetLimits,
    RunBudgetLedger,
    RunContext,
)
from edu_agent.state import StateStore
from scripts import r54_candidate_demo
from scripts.trace_inspector import main as trace_inspector_main


NORMAL_SNAPSHOT = {
    "route_winners": ["primary:chat_completions"],
    "retry_reasons": [],
    "fallback_decisions": [],
    "stream": {
        "has_text_delta": True,
        "terminal_count": 1,
        "sequence_monotonic": True,
    },
    "concurrent_tools": [["get_class_roster", "list_exams"]],
    "normalization": [
        "get_class_roster:/page:string_to_integer_v1",
        "get_class_roster:/page_size:string_to_integer_v1",
        "list_exams:/page:string_to_integer_v1",
        "list_exams:/page_size:string_to_integer_v1",
    ],
    "plan_steps": [
        {
            "step": "inspect",
            "status": "completed",
            "accepted_tools": ["get_class_roster", "list_exams"],
        },
        {
            "step": "publish",
            "status": "completed",
            "accepted_tools": ["create_exam"],
        },
    ],
    "approvals": ["create_exam:approved"],
    "writes": ["create_exam:committed"],
    "context": {"checkpoint_count": 1, "reclaimed": True},
    "recovery_actions": [],
    "budget": {
        "status": "recorded",
        "finalized": True,
        "root_outstanding": 0,
        "child_settlement": "not_exercised",
    },
}

FAULT_SNAPSHOT = {
    **NORMAL_SNAPSHOT,
    "route_winners": ["fallback:responses", "primary:chat_completions"],
    "retry_reasons": ["retry_limit_exhausted"],
    "fallback_decisions": ["activated"],
    "recovery_actions": ["reuse-operation", "terminal-replay"],
}


def test_normal_and_fault_demo_smoke_and_stable_event_snapshots(tmp_path):
    normal_dir = tmp_path / "normal"
    fault_dir = tmp_path / "fault"

    normal = r54_candidate_demo._run("normal", normal_dir)
    fault = r54_candidate_demo._run("fault", fault_dir)

    assert all(value is True for value in normal["assertions"].values())
    assert all(value is True for value in fault["assertions"].values())
    assert normal["event_snapshot"] == NORMAL_SNAPSHOT
    assert fault["event_snapshot"] == FAULT_SNAPSHOT
    assert (
        normal["teaching_state"]["idempotency_keys"]
        == fault["teaching_state"]["idempotency_keys"]
    )
    assert normal["teaching_state"]["exam_count"] == 1
    assert fault["teaching_state"]["exam_count"] == 1
    assert normal["timing"]["note"] == fault["timing"]["note"]
    assert "not an SLA" in normal["timing"]["note"]
    assert normal["fixture"]["instance_count"] == 1
    assert fault["fixture"]["instance_count"] == 2
    assert normal["fixture"]["stage_source"] == "durable_tool_messages"
    assert fault["fixture"]["stage_source"] == "durable_tool_messages"
    assert [item["kind"] for item in normal["fixture"]["model_call_sequence"]] == [
        "read",
        "write",
        "final",
    ]
    assert [item["kind"] for item in fault["fixture"]["model_call_sequence"]] == [
        "read",
        "write",
        "final",
    ]
    for report in (normal, fault):
        evidence = report["evidence_classes"]
        assert "real_model_verified" not in evidence
        assert "development/dirty provenance" in evidence[
            "real_model_development_evidence"
        ][0]
        assert (
            "real-model provenance for the current candidate commit"
            in evidence["not_verified"]
        )

    replay = r54_candidate_demo._run("normal", normal_dir)
    assert replay["event_snapshot"] == NORMAL_SNAPSHOT
    assert replay["teaching_state"]["exam_count"] == 1
    assert (
        replay["teaching_state"]["idempotency_keys"]
        == normal["teaching_state"]["idempotency_keys"]
    )


def test_trace_review_is_scoped_minimized_redacted_and_available_from_cli(
    tmp_path,
    monkeypatch,
    capsys,
):
    work_dir = tmp_path / "trace"
    r54_candidate_demo._run("fault", work_dir)
    state_path = work_dir / "r54-state.db"
    state = StateStore(state_path)

    with pytest.raises(PermissionError):
        TraceRepository(state).inspect_run(
            r54_candidate_demo.RUN_ID,
            actor_id="other-actor",
            tenant_id=r54_candidate_demo.TENANT_ID,
        )

    canary = "r54-export-canary-2d371f"
    state.record_audit_event(
        actor_id=r54_candidate_demo.ACTOR_ID,
        tenant_id=r54_candidate_demo.TENANT_ID,
        action="run.recovery_decision",
        resource=f"run:{r54_candidate_demo.RUN_ID}",
        decision="terminal-replay",
        details={
            "run_id": r54_candidate_demo.RUN_ID,
            "source": "test",
            "reason": canary,
            "next_step": "redaction-check",
        },
    )
    review = TraceRepository(
        state,
        redaction=RedactionPolicy((canary,)),
    ).inspect_run(
        r54_candidate_demo.RUN_ID,
        actor_id=r54_candidate_demo.ACTOR_ID,
        tenant_id=r54_candidate_demo.TENANT_ID,
    )["review"]
    serialized = json.dumps(review, ensure_ascii=False, sort_keys=True)
    assert canary not in serialized
    assert "[REDACTED]" in serialized
    assert "fixture.invalid" not in serialized
    assert r54_candidate_demo.EXAM_NAME not in serialized
    assert '"arguments"' not in serialized
    assert not contains_sensitive_data(review, secrets=(canary,))

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "trace_inspector.py",
            "--state",
            str(state_path),
            "--actor",
            r54_candidate_demo.ACTOR_ID,
            "--tenant",
            r54_candidate_demo.TENANT_ID,
            "--run",
            r54_candidate_demo.RUN_ID,
            "--format",
            "review",
        ],
    )
    assert trace_inspector_main() == 0
    cli_review = json.loads(capsys.readouterr().out)
    assert cli_review["schema_version"] == "edu-agent.trace-review.v1"
    assert cli_review["run_id"] == r54_candidate_demo.RUN_ID
    assert cli_review["tools"]["concurrent_segments"] == ["segment-000"]


def test_trace_review_associates_retries_with_their_model_call(tmp_path):
    store = StateStore(tmp_path / "retry-groups.db")
    context = RunContext.create(
        session_id="session-retry-groups",
        run_id="run-retry-groups",
        actor_id="teacher-retry-groups",
        tenant_id="school-retry-groups",
        role="teacher",
    )
    store.ensure_session(
        context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        role=context.role,
    )
    store.enqueue_run(context, request_text="retry grouping fixture")
    route = {
        "provider": "fixture-provider",
        "deployment": "fixture-deployment",
        "api_mode": "chat_completions",
        "model": "fixture-model",
    }

    def provider_event(event, attempt, details):
        store.record_provider_event(
            run_id=context.run_id,
            provider="fixture-provider",
            event=event,
            attempt=attempt,
            details=details,
        )

    provider_event(
        "route_selected",
        0,
        {
            "route_role": "primary",
            "route": route,
            "selection_reason": "configured_primary",
            "fallback_configured": True,
            "max_retries": 1,
        },
    )
    provider_event(
        "provider_attempt",
        1,
        {
            "route_role": "primary",
            "status": "failed",
            "failure_kind": "connection",
            "retryable": True,
            "fallback_allowed": False,
            "stream_visible": True,
            "breaker_state": "closed",
        },
    )
    provider_event(
        "route_selected",
        0,
        {
            "route_role": "primary",
            "route": route,
            "selection_reason": "configured_primary",
            "fallback_configured": True,
            "max_retries": 1,
        },
    )
    provider_event(
        "provider_attempt",
        1,
        {
            "route_role": "primary",
            "status": "failed",
            "failure_kind": "connection",
            "retryable": True,
            "fallback_allowed": True,
            "stream_visible": False,
            "breaker_state": "closed",
        },
    )
    provider_event(
        "retry_scheduled",
        1,
        {"delay_source": "backoff", "delay_seconds": 0.1},
    )
    provider_event(
        "route_selected",
        0,
        {
            "route_role": "primary",
            "route": route,
            "selection_reason": "configured_primary",
            "fallback_configured": True,
            "max_retries": 1,
        },
    )
    provider_event(
        "provider_attempt",
        2,
        {
            "route_role": "fallback",
            "status": "failed",
            "failure_kind": "connection",
            "retryable": True,
            "fallback_allowed": False,
            "stream_visible": False,
            "breaker_state": "closed",
        },
    )

    review = TraceRepository(store).inspect_run(
        context.run_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
    )["review"]
    failed = review["retry_fallback"]["failed_attempts"]

    assert review["retry_fallback"]["per_model_call"] == [
        {"model_call": 1, "max_retries": 1},
        {"model_call": 2, "max_retries": 1},
        {"model_call": 3, "max_retries": 1},
    ]
    assert [item["model_call"] for item in failed] == [1, 2, 3]
    assert failed[0]["retry_scheduled"] is False
    assert failed[0]["retry_decision_reason"] == "visible_stream_forbids_retry"
    assert failed[1]["retry_scheduled"] is True
    assert failed[1]["retry_decision_reason"] == "scheduled"
    assert failed[2]["max_retries"] == 0
    assert failed[2]["retry_scheduled"] is False
    assert failed[2]["retry_decision_reason"] == "retry_limit_exhausted"


def _budget_review(tmp_path, *, settled: bool) -> dict:
    suffix = "settled" if settled else "outstanding"
    store = StateStore(tmp_path / f"{suffix}.db")
    context = RunContext.create(
        session_id=f"session-{suffix}",
        run_id=f"run-{suffix}",
        actor_id="teacher-budget",
        tenant_id="school-budget",
        role="teacher",
    )
    store.ensure_session(
        context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        role=context.role,
    )
    store.enqueue_run(context, request_text="budget settlement fixture")
    ledger = RunBudgetLedger(
        store,
        root_run_id=context.run_id,
        session_id=context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        limits=BudgetLimits(
            max_model_calls=4,
            max_tool_calls=4,
            max_input_tokens=1_000,
            max_output_tokens=1_000,
            max_total_tokens=2_000,
            max_cost_microusd=10_000,
            max_wall_time_ms=10_000,
        ),
    )
    ledger.reserve(
        "child-allocation",
        owner_run_id=context.run_id,
        kind="child_reservation",
        amount=BudgetAmounts(model_calls=2, total_tokens=200),
    )
    ledger.reserve(
        "child-model-call",
        owner_run_id="child-run",
        kind="model_attempt",
        amount=BudgetAmounts(model_calls=1, total_tokens=100),
        parent_operation_id="child-allocation",
    )
    if settled:
        ledger.commit(
            "child-model-call",
            actual=BudgetAmounts(model_calls=1, total_tokens=80),
            usage_source="reported",
            cost_known=True,
        )
        ledger.release("child-allocation", reason="child completed")
    return TraceRepository(store).inspect_run(
        context.run_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
    )["review"]["budget"]


def test_trace_review_explains_child_budget_settlement(tmp_path):
    settled = _budget_review(tmp_path, settled=True)
    outstanding = _budget_review(tmp_path, settled=False)

    assert settled["child_settlement"] == "settled"
    assert settled["children"] == [
        {
            "owner_run_id": "child-run",
            "relation": "child",
            "reserved": {
                "model_calls": 0,
                "tool_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cost_microusd": 0,
                "wall_time_ms": 0,
            },
            "used": {
                "model_calls": 1,
                "tool_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 80,
                "cost_microusd": 0,
                "wall_time_ms": 0,
            },
            "unknown_cost_operations": 0,
            "outstanding_operations": 0,
            "operations": [
                {"kind": "model_attempt", "status": "committed", "count": 1}
            ],
        }
    ]
    assert outstanding["child_settlement"] == "outstanding"
    assert outstanding["children"][0]["outstanding_operations"] == 1
    assert outstanding["children"][0]["reserved"]["model_calls"] == 1
