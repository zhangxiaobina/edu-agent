"""Reproducible R5.4 ten-minute candidate demo.

The default path is an entirely local, fixed-seed run. The fault scenario
enables one explicit process-crash fixture after a transactional write commits
but before its tool result is journaled. Neither path contacts a model endpoint
or a private teaching platform.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

import httpx

from edu_agent.data import db, generate
from edu_agent.engine import (
    ApiMode,
    CredentialRef,
    EngineResponse,
    GatewayEngine,
    ProviderCapabilities,
    ProviderGateway,
    ProviderSpec,
    ResilientEngine,
)
from edu_agent.engine.streaming import (
    ProviderStreamEvent,
    ProviderStreamEventType,
    aggregate_provider_stream,
)
from edu_agent.observability import (
    RedactionPolicy,
    RunEventBus,
    RunEventType,
    RunEventWriterRejected,
    RunStreamWriterRegistry,
    SubscriptionClosed,
    TraceRepository,
)
from edu_agent.planning.models import CompletionCondition, PlanSpec, PlanStepSpec
from edu_agent.runtime import CancellationToken
from edu_agent.runtime.config import (
    AppConfig,
    DelegationConfig,
    MemoryConfig,
    ModelConfig,
    PlanningConfig,
    RuntimeConfig,
    SecurityConfig,
    StorageConfig,
)
from edu_agent.runtime.manager import RuntimeManager
from edu_agent.runtime.transactions import (
    ProcessCrashFaultInjector,
    SimulatedProcessCrash,
)
from edu_agent.service import EduAgentService
from edu_agent.state import StateStore
from edu_agent.teaching import SyntheticProvider, TeachingQueryKind
from edu_agent.tools import registry


SEED = 314
ACTOR_ID = "teacher-r54"
TENANT_ID = "school-r54"
SESSION_ID = "session-r54"
RUN_ID = "run-r54"
REPLAY_SCOPE = "r54:seed314:create-exam"
FAULT_POINT = "after_write_operation_commit_before_result"
EXAM_NAME = f"R54 candidate exam seed {SEED}"
TASK = "读取课程考试与名单，分析成绩后创建考试，并复盘写入证据"
SCHEMA_VERSION = "edu-agent.r54-candidate-demo.v1"


class DemoClock:
    """Deterministic lease clock advanced explicitly by the fault scenario."""

    def __init__(self) -> None:
        self.value = datetime(2026, 8, 25, 8, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


@dataclass
class FixtureScript:
    """Deterministic model fixture deriving its stage from durable messages."""

    fail_primary_once: bool = False
    primary_failed: bool = False
    calls: list[dict[str, Any]] = field(default_factory=list)

    def next_stage(
        self,
        adapter_name: str,
        api_mode: ApiMode,
        tool_names: set[str],
        messages: list[dict[str, Any]],
    ) -> str:
        tool_results = {
            str(message.get("tool_call_id"))
            for message in messages
            if isinstance(message, dict)
            and message.get("role") == "tool"
            and message.get("tool_call_id")
        }
        read_results = {"r54-roster", "r54-exams"}
        if tool_results & read_results and not read_results <= tool_results:
            raise RuntimeError("R5.4 fixture observed a partial durable read batch")
        if not read_results <= tool_results:
            stage, kind = 0, "read"
        elif "r54-write" not in tool_results:
            stage, kind = 1, "write"
        else:
            stage, kind = 2, "final"
        required = {
            "read": {"get_class_roster", "list_exams"},
            "write": {"create_exam"},
            "final": set(),
        }[kind]
        if not required <= tool_names:
            raise RuntimeError(
                f"R5.4 fixture stage {kind} is missing tools: "
                f"{sorted(required - tool_names)}"
            )
        if (
            self.fail_primary_once
            and kind == "read"
            and adapter_name == "primary"
            and not self.primary_failed
        ):
            self.primary_failed = True
            raise httpx.ReadError(
                "R5.4 fixture transport failure before any visible stream delta"
            )
        self.calls.append(
            {
                "adapter": adapter_name,
                "api_mode": api_mode.value,
                "stage": stage,
                "kind": kind,
            }
        )
        return kind


class FixtureAdapter:
    """Local adapter emitting the same normalized stream events as live adapters."""

    def __init__(self, script: FixtureScript, *, name: str, api_mode: ApiMode):
        self.script = script
        self.name = name
        self.api_mode = api_mode
        self.capabilities = ProviderCapabilities(
            tool_calling=True,
            usage=True,
            streaming=True,
            context_window_tokens=16_384,
            max_output_tokens=512,
            tokenizer="r54-fixture-v1",
        )

    def chat(
        self,
        route,
        messages,
        tools,
        *,
        cancellation_token=None,
        max_output_tokens=None,
    ) -> EngineResponse:
        return aggregate_provider_stream(
            self.stream_events(
                route,
                messages,
                tools,
                attempt=1,
                cancellation_token=cancellation_token,
                max_output_tokens=max_output_tokens,
            )
        )

    def stream_events(
        self,
        route,
        messages,
        tools,
        *,
        attempt: int = 1,
        cancellation_token=None,
        max_output_tokens=None,
    ) -> Iterator[ProviderStreamEvent]:
        tool_names = {
            item.get("function", {}).get("name")
            for item in tools
            if isinstance(item, dict)
        }
        tool_names.discard(None)
        kind = self.script.next_stage(
            self.name,
            self.api_mode,
            tool_names,
            messages,
        )
        if kind == "read":
            yield self._text(route, attempt, "fixture-text-read")
            yield from self._tool_call(
                route,
                attempt,
                index=0,
                call_id="r54-roster",
                name="get_class_roster",
                arguments={"class_id": 3, "page": "1", "page_size": "2"},
            )
            yield from self._tool_call(
                route,
                attempt,
                index=1,
                call_id="r54-exams",
                name="list_exams",
                arguments={"course_id": 1, "page": "1", "page_size": "2"},
            )
            yield self._usage(route, attempt, total=19)
            yield self._completed(route, attempt, finish_reason="tool_calls")
            return
        if kind == "write":
            yield self._text(route, attempt, "fixture-text-plan")
            yield from self._tool_call(
                route,
                attempt,
                index=0,
                call_id="r54-write",
                name="create_exam",
                arguments={
                    "exam_name": EXAM_NAME,
                    "class_id": 3,
                    "course_id": 1,
                    "duration": 45,
                },
            )
            yield self._usage(route, attempt, total=17)
            yield self._completed(route, attempt, finish_reason="tool_calls")
            return
        yield self._text(route, attempt, "fixture-text-final")
        yield self._usage(route, attempt, total=13)
        yield self._completed(route, attempt, finish_reason="stop")

    @staticmethod
    def _text(route, attempt: int, event_id: str) -> ProviderStreamEvent:
        return ProviderStreamEvent(
            ProviderStreamEventType.TEXT_DELTA,
            route=route,
            attempt=attempt,
            provider_event_id=event_id,
            provider_event_type="fixture.text.delta",
            delta="候选演示文本增量；",
        )

    @staticmethod
    def _tool_call(
        route,
        attempt: int,
        *,
        index: int,
        call_id: str,
        name: str,
        arguments: dict[str, Any],
    ) -> Iterator[ProviderStreamEvent]:
        raw = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        yield ProviderStreamEvent(
            ProviderStreamEventType.TOOL_CALL_ID_DELTA,
            route=route,
            attempt=attempt,
            provider_event_id=f"{call_id}-id",
            provider_event_type="fixture.tool_call.id.delta",
            delta=call_id,
            tool_call_index=index,
        )
        yield ProviderStreamEvent(
            ProviderStreamEventType.TOOL_CALL_NAME_DELTA,
            route=route,
            attempt=attempt,
            provider_event_id=f"{call_id}-name",
            provider_event_type="fixture.tool_call.name.delta",
            delta=name,
            tool_call_index=index,
        )
        yield ProviderStreamEvent(
            ProviderStreamEventType.TOOL_CALL_ARGUMENTS_DELTA,
            route=route,
            attempt=attempt,
            provider_event_id=f"{call_id}-args",
            provider_event_type="fixture.tool_call.arguments.delta",
            delta=raw,
            tool_call_index=index,
        )

    def _usage(self, route, attempt: int, *, total: int) -> ProviderStreamEvent:
        return ProviderStreamEvent(
            ProviderStreamEventType.USAGE,
            route=route,
            attempt=attempt,
            provider_event_id=f"usage-{self.name}-{attempt}-{len(self.script.calls)}",
            provider_event_type="fixture.usage",
            usage={
                "prompt_tokens": total - 3,
                "completion_tokens": 3,
                "total_tokens": total,
            },
        )

    def _completed(
        self,
        route,
        attempt: int,
        *,
        finish_reason: str,
    ) -> ProviderStreamEvent:
        return ProviderStreamEvent(
            ProviderStreamEventType.COMPLETED,
            route=route,
            attempt=attempt,
            provider_event_id=f"completed-{self.name}-{attempt}-{len(self.script.calls)}",
            provider_event_type="fixture.completed",
            finish_reason=finish_reason,
            model=route.model,
        )


class ConcurrentSyntheticProvider(SyntheticProvider):
    """Synthetic provider with a two-party proof barrier for the read batch."""

    _TARGETS = frozenset(
        {TeachingQueryKind.CLASS_ROSTER, TeachingQueryKind.EXAMS}
    )

    def __init__(self, connection_factory):
        super().__init__(connection_factory)
        self._demo_barrier = threading.Barrier(2)
        self._demo_lock = threading.Lock()
        self._arrivals: set[TeachingQueryKind] = set()
        self._worker_ids: set[int] = set()
        self._active = 0
        self._max_active = 0
        self._released = 0

    def execute(self, query, *, connection=None):
        if query.kind not in self._TARGETS:
            return super().execute(query, connection=connection)
        with self._demo_lock:
            self._arrivals.add(query.kind)
            self._worker_ids.add(threading.get_ident())
            self._active += 1
            self._max_active = max(self._max_active, self._active)
        try:
            try:
                self._demo_barrier.wait(timeout=5)
            except threading.BrokenBarrierError as error:
                raise RuntimeError(
                    "R5.4 parallel-read proof barrier did not receive both calls"
                ) from error
            with self._demo_lock:
                self._released += 1
            return super().execute(query, connection=connection)
        finally:
            with self._demo_lock:
                self._active -= 1

    def concurrency_proof(self) -> dict[str, Any]:
        with self._demo_lock:
            return {
                "arrived_queries": sorted(item.value for item in self._arrivals),
                "distinct_worker_count": len(self._worker_ids),
                "max_simultaneous_calls": self._max_active,
                "barrier_releases": self._released,
                "proved": (
                    self._arrivals == self._TARGETS
                    and len(self._worker_ids) == 2
                    and self._max_active == 2
                    and self._released == 2
                ),
            }


class FixturePlanGenerator:
    """Fixed PlanSpec fixture; Plan/Evidence persistence remains production code."""

    def generate(
        self,
        task: str,
        *,
        context,
        available_tools: set[str],
        max_steps: int,
    ) -> PlanSpec:
        required = {"get_class_roster", "list_exams", "create_exam"}
        if max_steps < 2 or not required <= available_tools:
            raise ValueError("R5.4 fixture plan requirements are unavailable")
        return PlanSpec(
            goal=task,
            steps=[
                PlanStepSpec(
                    id="inspect",
                    goal="并发读取班级名单与课程考试",
                    allowed_tools=["get_class_roster", "list_exams"],
                    expected_tools=["get_class_roster", "list_exams"],
                    completion_conditions=[
                        CompletionCondition(
                            kind="tool_success",
                            tool="get_class_roster",
                        ),
                        CompletionCondition(kind="tool_success", tool="list_exams"),
                    ],
                ),
                PlanStepSpec(
                    id="publish",
                    goal="在审批后创建一次稳定考试写入",
                    depends_on=["inspect"],
                    allowed_tools=["create_exam"],
                    expected_tools=["create_exam"],
                    completion_conditions=[
                        CompletionCondition(kind="tool_success", tool="create_exam"),
                    ],
                ),
            ],
        )


def _route_engine(script: FixtureScript) -> ResilientEngine:
    primary_adapter = FixtureAdapter(
        script,
        name="primary",
        api_mode=ApiMode.CHAT_COMPLETIONS,
    )
    fallback_adapter = FixtureAdapter(
        script,
        name="fallback",
        api_mode=ApiMode.RESPONSES,
    )
    gateway = ProviderGateway(
        adapters={
            ApiMode.CHAT_COMPLETIONS: primary_adapter,
            ApiMode.RESPONSES: fallback_adapter,
        }
    )
    primary = GatewayEngine(
        gateway,
        ProviderSpec(
            model="r54-fixture-primary",
            endpoint="https://fixture.invalid/v1/",
            api_mode=ApiMode.CHAT_COMPLETIONS,
            provider="fixture",
            deployment="offline-primary",
            credential=CredentialRef("R54_FIXTURE_KEY"),
            capabilities=primary_adapter.capabilities,
        ),
        name="fixture-primary",
    )
    fallback = GatewayEngine(
        gateway,
        ProviderSpec(
            model="r54-fixture-fallback",
            endpoint="https://fixture-fallback.invalid/v1/",
            api_mode=ApiMode.RESPONSES,
            provider="fixture",
            deployment="offline-fallback",
            credential=CredentialRef("R54_FIXTURE_FALLBACK_KEY"),
            capabilities=fallback_adapter.capabilities,
        ),
        name="fixture-fallback",
    )
    return ResilientEngine(
        primary,
        fallback=fallback,
        max_retries=0,
        sleeper=lambda _: None,
        event_sink=None,
        route_max_concurrency=2,
    )


def _config(work_dir: Path) -> AppConfig:
    return AppConfig(
        model=ModelConfig(
            provider="fixture",
            model="r54-fixture-primary",
            endpoint="https://fixture.invalid/v1/",
            api_mode=ApiMode.CHAT_COMPLETIONS,
            vendor="fixture",
            deployment="offline-primary",
            context_window_tokens=16_384,
            max_output_tokens=512,
            tokenizer="r54-fixture-v1",
        ),
        runtime=RuntimeConfig(
            max_model_calls=8,
            max_tool_calls=12,
            max_input_tokens=8_192,
            max_output_tokens=4_096,
            max_total_tokens=12_288,
            max_wall_time_seconds=120,
            tool_batch_max_workers=2,
            tool_call_timeout_seconds=10,
            context_token_budget=16_384,
            output_token_reserve=512,
            compression_trigger_ratio=0.2,
            compression_release_ratio=0.1,
            compression_min_reclaim_tokens=1,
            compression_cooldown_turns=0,
            compression_keep_recent=4,
            compression_summary_max_chars=1_600,
            recent_message_limit=20,
            session_lease_seconds=0.2,
            session_heartbeat_seconds=0.05,
            run_stall_seconds=0.4,
        ),
        planning=PlanningConfig(
            enabled=True,
            max_steps=3,
            max_step_retries=1,
            max_iterations=8,
        ),
        memory=MemoryConfig(enabled=False),
        security=SecurityConfig(
            require_write_approval=True,
            default_role="teacher",
        ),
        delegation=DelegationConfig(enabled=False),
        storage=StorageConfig(
            state_path=str(work_dir / "r54-state.db"),
            artifact_path=str(work_dir / "r54-artifacts"),
        ),
    )


def _prepare_work_dir(work_dir: Path) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    for name in ("r54-state.db", "r54-teaching.db"):
        for suffix in ("", "-wal", "-shm"):
            (work_dir / f"{name}{suffix}").unlink(missing_ok=True)
    artifact_dir = work_dir / "r54-artifacts"
    if artifact_dir.exists():
        shutil.rmtree(artifact_dir)


def _seed_history(state: StateStore) -> None:
    state.ensure_session(
        SESSION_ID,
        actor_id=ACTOR_ID,
        tenant_id=TENANT_ID,
        role="teacher",
        course_ids={1, 2, 3},
        title="R5.4 fixed synthetic history",
    )
    messages: list[dict[str, str]] = []
    for index in range(8):
        if index < 6:
            user_body = (f"synthetic historical topic {index} " * 55).strip()
            assistant_body = (f"fixture evidence summary {index} " * 55).strip()
        else:
            user_body = f"recent synthetic topic {index}"
            assistant_body = f"recent fixture answer {index}"
        messages.extend(
            [
                {"role": "user", "content": user_body},
                {"role": "assistant", "content": assistant_body},
            ]
        )
    state.append_messages(SESSION_ID, messages)


def _open_stream(
    service: EduAgentService,
    writer_registry: RunStreamWriterRegistry,
    bus: RunEventBus,
    *,
    attempt: int,
):
    token = CancellationToken()
    writer = writer_registry.open(
        run_id=RUN_ID,
        attempt=attempt,
        writer_id=f"r54-attempt-{attempt}",
        cancellation_token=token,
        sequence_reserver=lambda **fields: service.reserve_stream_event_sequence(
            actor_id=ACTOR_ID,
            tenant_id=TENANT_ID,
            **fields,
        ),
    )
    subscription = bus.subscribe(run_id=RUN_ID, attempt=attempt, buffer_size=512)
    return token, writer, subscription


def _drain_stream(subscription) -> list[dict[str, Any]]:
    events = []
    while True:
        try:
            events.append(subscription.get_nowait().to_dict())
        except (TimeoutError, SubscriptionClosed):
            break
    subscription.cancel()
    return events


def _stream_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(events, key=lambda item: int(item["sequence"]))
    sequences = [int(item["sequence"]) for item in ordered]
    event_types = [str(item["event_type"]) for item in ordered]
    return {
        "attempts": sorted({int(item["attempt"]) for item in ordered}),
        "event_types": sorted(set(event_types)),
        "text_delta_count": event_types.count(RunEventType.TEXT_DELTA.value),
        "tool_call_delta_count": event_types.count(
            RunEventType.TOOL_CALL_DELTA.value
        ),
        "terminal_count": sum(
            item in {RunEventType.COMPLETED.value, RunEventType.ERROR.value}
            for item in event_types
        ),
        "sequence_monotonic": (
            bool(sequences)
            and sequences == sorted(sequences)
            and len(sequences) == len(set(sequences))
        ),
    }


def _teaching_evidence(data_path: Path) -> dict[str, Any]:
    connection = db.connect(data_path)
    try:
        exam_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM exams WHERE exam_name=?",
                (EXAM_NAME,),
            ).fetchone()[0]
        )
        operations = connection.execute(
            """
            SELECT idempotency_key, tool_name, status, tool_call_id
            FROM tool_operations ORDER BY created_at, id
            """
        ).fetchall()
        approval_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM tool_approvals WHERE decision='approved'"
            ).fetchone()[0]
        )
        return {
            "exam_count": exam_count,
            "operation_count": len(operations),
            "approval_count": approval_count,
            "idempotency_keys": sorted(
                {str(item["idempotency_key"]) for item in operations}
            ),
            "operations": [
                {
                    "tool": item["tool_name"],
                    "tool_call_id": item["tool_call_id"],
                    "status": item["status"],
                }
                for item in operations
            ],
        }
    finally:
        connection.close()


def _decision_summary(decision) -> dict[str, Any] | None:
    if decision is None:
        return None
    return {
        "action": decision.action.value,
        "reason": decision.reason,
        "next_step": decision.next_step,
        "phase": decision.phase,
        "stable_boundary": decision.stable_boundary,
        "tool": decision.tool_name,
        "tool_call_id": decision.tool_call_id,
        "operation_status": decision.operation_status,
    }


def _event_snapshot(
    review: dict[str, Any],
    stream: dict[str, Any],
) -> dict[str, Any]:
    concurrent_tools = []
    for segment in review["tools"]["segments"]:
        if segment["concurrent"]:
            concurrent_tools.append(
                sorted({str(call["tool"]) for call in segment["calls"]})
            )
    recovery_actions = list(
        dict.fromkeys(str(item["action"]) for item in review["recovery"])
    )
    return {
        "route_winners": sorted(
            f"{item['role']}:{item['api_mode']}"
            for item in review["route"]["winners"]
        ),
        "retry_reasons": sorted(
            {
                str(item["retry_decision_reason"])
                for item in review["retry_fallback"]["failed_attempts"]
            }
        ),
        "fallback_decisions": sorted(
            str(item["decision"])
            for item in review["retry_fallback"]["fallbacks"]
        ),
        "stream": {
            "has_text_delta": stream["text_delta_count"] > 0,
            "terminal_count": stream["terminal_count"],
            "sequence_monotonic": stream["sequence_monotonic"],
        },
        "concurrent_tools": sorted(concurrent_tools),
        "normalization": sorted(
            f"{item['tool']}:{item['pointer']}:{item['rule']}"
            for item in review["argument_normalization"]
        ),
        "plan_steps": [
            {
                "step": item["step_id"],
                "status": item["status"],
                "accepted_tools": sorted({
                    str(evidence["tool"])
                    for evidence in item["evidence"]
                    if evidence["status"] == "accepted" and evidence["tool"]
                }),
            }
            for item in review["plan_evidence"]["steps"]
        ],
        "approvals": sorted(
            f"{item['tool']}:{item['decision']}" for item in review["approval"]
        ),
        "writes": sorted(
            f"{item['tool']}:{item['status']}" for item in review["writes"]
        ),
        "context": {
            "checkpoint_count": review["context"]["checkpoint_count"],
            "reclaimed": any(
                int(item["reclaimed_tokens"]) > 0
                for item in review["context"]["compactions"]
            ),
        },
        "recovery_actions": recovery_actions,
        "budget": {
            "status": review["budget"]["status"],
            "finalized": review["budget"].get("finalized"),
            "root_outstanding": review["budget"]
            .get("root", {})
            .get("outstanding_operations"),
            "child_settlement": review["budget"].get("child_settlement"),
        },
    }


def _assert_demo(
    *,
    scenario: str,
    trace: dict[str, Any],
    stream: dict[str, Any],
    concurrency: dict[str, Any],
    teaching: dict[str, Any],
    run_status: dict[str, Any] | None,
    fixture_calls: list[dict[str, Any]],
    fixture_instance_count: int,
    crash_observed: bool,
    stale_writer_rejected: bool,
) -> dict[str, bool]:
    review = trace["review"]
    resolved_modes = {
        (item.get("role"), item.get("api_mode"))
        for item in review["route"]["resolved"]
    }
    read_group = next(
        (
            segment
            for segment in review["tools"]["segments"]
            if segment["concurrent"]
            and {
                str(call["tool"])
                for call in segment["calls"]
                if call["status"] == "ok"
            }
            == {"get_class_roster", "list_exams"}
        ),
        None,
    )
    repairs = {
        (item["tool"], item["pointer"], item["rule"])
        for item in review["argument_normalization"]
    }
    expected_repairs = {
        ("get_class_roster", "/page", "string_to_integer_v1"),
        ("get_class_roster", "/page_size", "string_to_integer_v1"),
        ("list_exams", "/page", "string_to_integer_v1"),
        ("list_exams", "/page_size", "string_to_integer_v1"),
    }
    plan_steps = review["plan_evidence"]["steps"]
    write_calls = [
        call
        for segment in review["tools"]["segments"]
        for call in segment["calls"]
        if call["tool"] == "create_exam"
    ]
    recovery_actions = {
        str(item["action"]) for item in review["recovery"]
    }
    assertions = {
        "route_and_api_modes_recorded": {
            ("primary", ApiMode.CHAT_COMPLETIONS.value),
            ("fallback", ApiMode.RESPONSES.value),
        }
        <= resolved_modes,
        "real_text_delta_observed": stream["text_delta_count"] > 0,
        "run_event_sequence_monotonic": stream["sequence_monotonic"],
        "single_stream_terminal": stream["terminal_count"] == 1,
        "two_reads_in_one_concurrent_segment": read_group is not None,
        "provider_barrier_proved_overlap": concurrency["proved"] is True,
        "arguments_normalized_without_values": repairs == expected_repairs
        and all(
            item["original_value_exported"] is False
            for item in review["argument_normalization"]
        ),
        "plan_and_evidence_completed": (
            review["plan_evidence"]["status"] == "completed"
            and [item["step_id"] for item in plan_steps] == ["inspect", "publish"]
            and all(item["status"] == "completed" for item in plan_steps)
            and all(
                any(evidence["status"] == "accepted" for evidence in item["evidence"])
                for item in plan_steps
            )
        ),
        "approved_idempotent_write": (
            teaching["operation_count"] == 1
            and teaching["approval_count"] == 1
            and len(teaching["idempotency_keys"]) == 1
            and teaching["operations"] == [
                {
                    "tool": "create_exam",
                    "tool_call_id": "r54-write",
                    "status": "committed",
                }
            ]
            and any(
                item["tool"] == "create_exam"
                and item["decision"] == "approved"
                for item in review["approval"]
            )
            and len(review["writes"]) == 1
            and review["writes"][0]["status"] == "committed"
        ),
        "single_synthetic_side_effect": teaching["exam_count"] == 1,
        "context_compaction_recorded": (
            review["context"]["checkpoint_count"] >= 1
            and any(
                int(item["reclaimed_tokens"]) > 0
                for item in review["context"]["compactions"]
            )
        ),
        "budget_root_settled": (
            review["budget"]["status"] == "recorded"
            and review["budget"]["finalized"] is True
            and review["budget"]["root"]["outstanding_operations"] == 0
            and review["budget"]["child_settlement"] == "not_exercised"
        ),
        "model_fixture_rebuilt_from_durable_messages": (
            fixture_instance_count == (2 if scenario == "fault" else 1)
            and [
                (item["stage"], item["kind"])
                for item in fixture_calls
            ]
            == [(0, "read"), (1, "write"), (2, "final")]
        ),
        "run_completed": bool(run_status and run_status["status"] == "completed"),
    }
    if scenario == "fault":
        assertions.update(
            {
                "explicit_crash_observed": crash_observed,
                "old_stream_writer_fenced": stale_writer_rejected,
                "retry_skipped_then_fallback": (
                    review["retry_fallback"]["max_retries"] == 0
                    and any(
                        item["retry_decision_reason"] == "retry_limit_exhausted"
                        and item["retry_scheduled"] is False
                        for item in review["retry_fallback"]["failed_attempts"]
                    )
                    and any(
                        item["decision"] == "activated"
                        for item in review["retry_fallback"]["fallbacks"]
                    )
                ),
                "operation_reused_without_duplicate_write": (
                    any(call["idempotent_replay"] for call in write_calls)
                    and {"reuse-operation", "terminal-replay"} <= recovery_actions
                    and teaching["exam_count"] == 1
                    and teaching["operation_count"] == 1
                    and teaching["approval_count"] == 1
                ),
            }
        )
    else:
        assertions.update(
            {
                "fault_injection_default_off": not crash_observed,
                "normal_route_has_no_fallback": (
                    not review["retry_fallback"]["failed_attempts"]
                    and not review["retry_fallback"]["fallbacks"]
                ),
                "first_write_not_replayed": (
                    len(write_calls) == 1
                    and write_calls[0]["idempotent_replay"] is False
                ),
            }
        )
    return assertions


def _run(scenario: str, work_dir: Path) -> dict[str, Any]:
    if scenario not in {"normal", "fault"}:
        raise ValueError("scenario must be normal or fault")
    _prepare_work_dir(work_dir)
    data_path = work_dir / "r54-teaching.db"
    state_path = work_dir / "r54-state.db"
    generate.build(seed=SEED, out_path=data_path)

    previous_db = os.environ.get("EDU_AGENT_DB")
    original_provider = registry.teaching_data_provider()
    os.environ["EDU_AGENT_DB"] = str(data_path)
    teaching_provider = ConcurrentSyntheticProvider(lambda: db.connect(data_path))
    registry.configure_teaching_data_provider(teaching_provider)

    clock = DemoClock()
    _seed_history(StateStore(state_path, clock=clock))
    model_calls: list[dict[str, Any]] = []
    fixture_instance_count = 0
    bus = RunEventBus(max_buffer_size=512)
    writer_registry = RunStreamWriterRegistry(bus)
    subscriptions = []
    tokens: list[CancellationToken] = []
    service: EduAgentService | None = None
    recovered_service: EduAgentService | None = None
    crash_observed = False
    stale_writer_rejected = False
    recovery_decision = None
    terminal_decision = None
    approvals: list[dict[str, Any]] = []
    stream_events: list[dict[str, Any]] = []
    started = time.perf_counter()

    def approval_handler(request) -> bool:
        approvals.append(
            {
                "tool": request.tool_name,
                "payload_hash": request.payload_hash,
                "scope": request.scope,
            }
        )
        return True

    def make_service(
        *,
        owner: str,
        crash: bool,
        fail_primary_once: bool,
    ) -> EduAgentService:
        nonlocal fixture_instance_count
        fixture_instance_count += 1
        fixture_script = FixtureScript(
            fail_primary_once=fail_primary_once,
            calls=model_calls,
        )
        runtime_state = StateStore(state_path, clock=clock)
        return EduAgentService(
            _route_engine(fixture_script),
            config=_config(work_dir),
            state_store=runtime_state,
            tools_provider=registry,
            approval_handler=approval_handler,
            runtime_manager=RuntimeManager(
                runtime_state,
                owner_id=owner,
                lease_seconds=0.2,
                heartbeat_seconds=0.05,
            ),
            plan_generator=FixturePlanGenerator(),
            loop_fault_injector=(
                ProcessCrashFaultInjector(FAULT_POINT) if crash else None
            ),
        )

    try:
        service = make_service(
            owner="r54-worker-before" if scenario == "fault" else "r54-worker",
            crash=scenario == "fault",
            fail_primary_once=scenario == "fault",
        )
        token, writer, subscription = _open_stream(
            service,
            writer_registry,
            bus,
            attempt=0,
        )
        tokens.append(token)
        subscriptions.append(subscription)
        try:
            result = service.chat(
                TASK,
                actor_id=ACTOR_ID,
                tenant_id=TENANT_ID,
                role="teacher",
                course_ids={1, 2, 3},
                session_id=SESSION_ID,
                run_id=RUN_ID,
                replay_scope=REPLAY_SCOPE,
                cancellation_token=token,
                stream_writer=writer,
            )
            writer.complete({"stop_reason": result.stop_reason or "completed"})
        except SimulatedProcessCrash as error:
            if scenario != "fault" or str(error) != FAULT_POINT:
                raise
            crash_observed = True
            service.close()
            service = None
            clock.advance(1.0)

            recovered_service = make_service(
                owner="r54-worker-after",
                crash=False,
                fail_primary_once=False,
            )
            recovery_decision = recovered_service.get_recovery_decision(
                RUN_ID,
                actor_id=ACTOR_ID,
                tenant_id=TENANT_ID,
            )
            recovered_token, recovered_writer, recovered_subscription = _open_stream(
                recovered_service,
                writer_registry,
                bus,
                attempt=1,
            )
            tokens.append(recovered_token)
            subscriptions.append(recovered_subscription)
            try:
                writer.publish(
                    RunEventType.TEXT_DELTA,
                    {"delta": "stale writer must not publish"},
                )
            except RunEventWriterRejected:
                stale_writer_rejected = True
            result = recovered_service.resume_run(
                RUN_ID,
                actor_id=ACTOR_ID,
                tenant_id=TENANT_ID,
                cancellation_token=recovered_token,
                stream_writer=recovered_writer,
            )
            recovered_writer.complete(
                {"stop_reason": result.stop_reason or "completed"}
            )
            replayed_result = recovered_service.resume_run(
                RUN_ID,
                actor_id=ACTOR_ID,
                tenant_id=TENANT_ID,
            )
            if replayed_result.final_answer != result.final_answer:
                raise AssertionError("terminal replay changed the recovered result")
            terminal_decision = recovered_service.get_recovery_decision(
                RUN_ID,
                actor_id=ACTOR_ID,
                tenant_id=TENANT_ID,
            )
        finally:
            if service is not None:
                service.close()
                service = None
            if recovered_service is not None:
                recovered_service.close()
                recovered_service = None

        for subscription in subscriptions:
            stream_events.extend(_drain_stream(subscription))
        stream = _stream_summary(stream_events)
        teaching = _teaching_evidence(data_path)
        final_state = StateStore(state_path, read_only=True)
        trace = TraceRepository(final_state).inspect_run(
            RUN_ID,
            actor_id=ACTOR_ID,
            tenant_id=TENANT_ID,
        )
        run_status = final_state.get_run_status(
            RUN_ID,
            actor_id=ACTOR_ID,
            tenant_id=TENANT_ID,
        )
        concurrency = teaching_provider.concurrency_proof()
        assertions = _assert_demo(
            scenario=scenario,
            trace=trace,
            stream=stream,
            concurrency=concurrency,
            teaching=teaching,
            run_status=run_status,
            fixture_calls=model_calls,
            fixture_instance_count=fixture_instance_count,
            crash_observed=crash_observed,
            stale_writer_rejected=stale_writer_rejected,
        )
        failed = [name for name, passed in assertions.items() if not passed]
        if failed:
            raise AssertionError(f"R5.4 demo smoke failed: {failed}")

        review = trace["review"]
        report = {
            "schema_version": SCHEMA_VERSION,
            "scenario": scenario,
            "seed": SEED,
            "fixture": {
                "teaching_provider": "SyntheticProvider",
                "model_provider": "deterministic local ProviderAdapter",
                "network_access": False,
                "private_platform_access": False,
                "stage_source": "durable_tool_messages",
                "instance_count": fixture_instance_count,
                "model_call_sequence": model_calls,
            },
            "run": {
                "run_id": RUN_ID,
                "session_id": SESSION_ID,
                "status": run_status["status"] if run_status else None,
                "final_answer_characters": len(result.final_answer or ""),
            },
            "fault_injection": {
                "enabled": scenario == "fault",
                "default_off": True,
                "point": FAULT_POINT if scenario == "fault" else None,
                "crash_observed": crash_observed,
                "old_writer_rejected": stale_writer_rejected,
                "recovery_decision": _decision_summary(recovery_decision),
                "terminal_decision": _decision_summary(terminal_decision),
            },
            "approval": {
                "handler_call_count": len(approvals),
                "tools": [item["tool"] for item in approvals],
                "payload_hashes": [item["payload_hash"] for item in approvals],
            },
            "teaching_state": teaching,
            "concurrency_proof": concurrency,
            "stream": stream,
            "event_snapshot": _event_snapshot(review, stream),
            "trace_review": review,
            "assertions": assertions,
            "timing": {
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "note": "single observation on this machine; not an SLA",
            },
            "evidence_classes": {
                "implemented": [
                    "Provider route and API-mode selection",
                    "RunEvent v2 text delta transport",
                    "parallel read execution with a provider capability gate",
                    "Plan/Evidence completion gate",
                    "approval-bound idempotent transactional write",
                    "process-reopen recovery and stream-writer fencing",
                    "owner-scoped and redacted Trace review",
                ],
                "fixture_offline_verified": [
                    "seed-314 SyntheticProvider teaching data",
                    "deterministic local model stream and explicit failure switch",
                ],
                "real_model_verified": [
                    "separate fixed R5.2 DashScope evidence; not rerun by this demo"
                ],
                "not_verified": [
                    "private teaching platform",
                    "live model endpoint in this demo",
                    "Docker/Jobe runtime on this machine",
                    "cross-host SQLite consensus",
                ],
            },
        }
        return RedactionPolicy().redact(report)
    finally:
        for subscription in subscriptions:
            try:
                subscription.cancel()
            except Exception:
                pass
        writer_registry.close()
        bus.close()
        for token in tokens:
            token.close()
        if service is not None:
            service.close()
        if recovered_service is not None:
            recovered_service.close()
        registry.configure_teaching_data_provider(original_provider)
        if previous_db is None:
            os.environ.pop("EDU_AGENT_DB", None)
        else:
            os.environ["EDU_AGENT_DB"] = previous_db


def main() -> int:
    parser = argparse.ArgumentParser(
        description="R5.4 reproducible ten-minute candidate demo"
    )
    parser.add_argument(
        "--scenario",
        choices=("normal", "fault"),
        default="normal",
        help="fault is the explicit test-only crash switch; default is normal",
    )
    parser.add_argument(
        "--work-dir",
        default=str(Path(tempfile.gettempdir()) / "edu-agent-r54-candidate"),
        help="owned directory for disposable state and synthetic teaching databases",
    )
    parser.add_argument(
        "--report",
        default=None,
        help="redacted JSON report path (default: artifacts/r54-demo-<scenario>.json)",
    )
    args = parser.parse_args()
    report_path = Path(args.report or f"artifacts/r54-demo-{args.scenario}.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = _run(args.scenario, Path(args.work_dir).resolve())
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "scenario": report["scenario"],
                "run_id": report["run"]["run_id"],
                "status": report["run"]["status"],
                "report": str(report_path),
                "state": str(Path(args.work_dir).resolve() / "r54-state.db"),
                "assertions": report["assertions"],
                "elapsed_ms": report["timing"]["elapsed_ms"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
