from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class DelegationError(RuntimeError):
    pass


class DelegationLimitExceeded(DelegationError):
    pass


class DelegationBackpressure(DelegationError):
    pass


class DelegationTimedOut(DelegationError):
    pass


class PartialSuccessPolicy(str, Enum):
    fail_fast = "fail_fast"
    best_effort = "best_effort"
    required_quorum = "required_quorum"


class TeachingTaskKind(str, Enum):
    class_analysis = "class_analysis"
    chapter_retrieval = "chapter_retrieval"
    intervention_grade = "intervention_grade"
    intervention_weakness = "intervention_weakness"
    intervention_resources = "intervention_resources"


class SubtaskStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    timed_out = "timed_out"
    cancelled = "cancelled"


@dataclass(frozen=True)
class DelegationPolicy:
    max_depth: int = 1
    max_children_per_parent: int = 8
    max_concurrency: int = 3
    child_timeout_seconds: float = 30.0
    worker_lease_seconds: float = 45.0
    max_model_calls_per_child: int = 2
    max_tool_calls_per_child: int = 6
    max_tokens_per_child: int = 4_000
    max_cost_usd_per_child: float = 0.05
    max_root_model_calls: int = 16
    max_root_tool_calls: int = 48
    max_root_tokens: int = 32_000
    max_root_cost_usd: float = 0.40
    allowed_tool_categories: frozenset[str] = field(
        default_factory=lambda: frozenset({"query", "analysis", "knowledge"})
    )
    allowed_models: frozenset[str] = field(
        default_factory=lambda: frozenset({"deterministic-readonly-v1"})
    )
    default_model: str = "deterministic-readonly-v1"
    allowed_child_roles: frozenset[str] = field(
        default_factory=lambda: frozenset({"student", "teacher"})
    )
    allow_child_delegation: bool = False

    def __post_init__(self) -> None:
        positive = {
            "max_depth": self.max_depth,
            "max_children_per_parent": self.max_children_per_parent,
            "max_concurrency": self.max_concurrency,
            "child_timeout_seconds": self.child_timeout_seconds,
            "worker_lease_seconds": self.worker_lease_seconds,
            "max_model_calls_per_child": self.max_model_calls_per_child,
            "max_tool_calls_per_child": self.max_tool_calls_per_child,
            "max_tokens_per_child": self.max_tokens_per_child,
            "max_root_model_calls": self.max_root_model_calls,
            "max_root_tool_calls": self.max_root_tool_calls,
            "max_root_tokens": self.max_root_tokens,
        }
        invalid = [name for name, value in positive.items() if value <= 0]
        if invalid:
            raise ValueError(f"delegation 配额必须大于 0：{sorted(invalid)}")
        if self.worker_lease_seconds <= self.child_timeout_seconds:
            raise ValueError("delegation worker lease 必须长于 child timeout")
        if self.max_cost_usd_per_child < 0 or self.max_root_cost_usd < 0:
            raise ValueError("delegation 成本配额不能为负数")
        if self.default_model not in self.allowed_models:
            raise ValueError("delegation default_model 必须位于 allowed_models")
        if not self.allowed_tool_categories or not self.allowed_child_roles:
            raise ValueError("delegation 工具类别和 child role 不能为空")

    def child_budget(self) -> dict[str, int | float]:
        return {
            "max_model_calls": self.max_model_calls_per_child,
            "max_tool_calls": self.max_tool_calls_per_child,
            "max_tokens": self.max_tokens_per_child,
            "max_cost_usd": self.max_cost_usd_per_child,
        }

    def root_budget(self) -> dict[str, int | float]:
        return {
            "max_model_calls": self.max_root_model_calls,
            "max_tool_calls": self.max_root_tool_calls,
            "max_tokens": self.max_root_tokens,
            "max_cost_usd": self.max_root_cost_usd,
        }


@dataclass(frozen=True)
class TeachingSubtask:
    task_key: str
    kind: TeachingTaskKind
    task: str
    arguments: dict[str, Any]
    course_ids: frozenset[int]
    requested_role: str | None = None
    model: str | None = None
    plan_step_id: str | None = None
    input_evidence_ids: tuple[int, ...] = ()
    input_citations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TeachingTaskKind):
            object.__setattr__(self, "kind", TeachingTaskKind(self.kind))
        object.__setattr__(self, "arguments", dict(self.arguments))
        object.__setattr__(self, "course_ids", frozenset(int(item) for item in self.course_ids))
        object.__setattr__(self, "input_evidence_ids", tuple(int(item) for item in self.input_evidence_ids))
        object.__setattr__(self, "input_citations", tuple(str(item) for item in self.input_citations))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["kind"] = self.kind.value
        payload["course_ids"] = sorted(self.course_ids)
        return payload


@dataclass(frozen=True)
class SubagentInput:
    system_prompt: str
    messages: tuple[dict[str, Any], ...]
    plan_projection: dict[str, Any]
    evidence: tuple[dict[str, Any], ...]
    citations: tuple[str, ...]


@dataclass(frozen=True)
class SubtaskUsage:
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int | None = None
    estimated_cost_usd: float | None = None
    duration_ms: float = 0.0

    def __post_init__(self) -> None:
        if self.total_tokens is None:
            object.__setattr__(self, "total_tokens", self.input_tokens + self.output_tokens)

    def to_dict(self) -> dict[str, int | float | None]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> SubtaskUsage:
        payload = payload or {}
        return cls(
            model_calls=int(payload.get("model_calls", 0)),
            tool_calls=int(payload.get("tool_calls", 0)),
            input_tokens=int(payload.get("input_tokens", 0)),
            output_tokens=int(payload.get("output_tokens", 0)),
            total_tokens=(
                int(payload["total_tokens"])
                if payload.get("total_tokens") is not None
                else None
            ),
            estimated_cost_usd=(
                float(payload["estimated_cost_usd"])
                if payload.get("estimated_cost_usd") is not None
                else None
            ),
            duration_ms=float(payload.get("duration_ms", 0.0)),
        )


@dataclass(frozen=True)
class SubtaskResult:
    run_id: str
    task_key: str
    status: SubtaskStatus
    summary: str
    evidence_ids: tuple[int, ...] = ()
    citations: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    usage: SubtaskUsage = field(default_factory=SubtaskUsage)
    warnings: tuple[str, ...] = ()
    failure_reason: str | None = None
    cancel_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_key": self.task_key,
            "status": self.status.value,
            "summary": self.summary,
            "evidence_ids": list(self.evidence_ids),
            "citations": list(self.citations),
            "artifacts": list(self.artifacts),
            "usage": self.usage.to_dict(),
            "warnings": list(self.warnings),
            "failure_reason": self.failure_reason,
            "cancel_reason": self.cancel_reason,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SubtaskResult:
        return cls(
            run_id=str(payload["run_id"]),
            task_key=str(payload["task_key"]),
            status=SubtaskStatus(payload["status"]),
            summary=str(payload.get("summary", "")),
            evidence_ids=tuple(int(item) for item in payload.get("evidence_ids", ())),
            citations=tuple(str(item) for item in payload.get("citations", ())),
            artifacts=tuple(str(item) for item in payload.get("artifacts", ())),
            usage=SubtaskUsage.from_dict(payload.get("usage")),
            warnings=tuple(str(item) for item in payload.get("warnings", ())),
            failure_reason=payload.get("failure_reason"),
            cancel_reason=payload.get("cancel_reason"),
        )


@dataclass(frozen=True)
class DelegationBatchResult:
    parent_run_id: str
    root_run_id: str
    policy: PartialSuccessPolicy
    status: str
    required_quorum: int | None
    results: tuple[SubtaskResult, ...]
    root_usage: dict[str, Any]
    elapsed_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent_run_id": self.parent_run_id,
            "root_run_id": self.root_run_id,
            "policy": self.policy.value,
            "status": self.status,
            "required_quorum": self.required_quorum,
            "results": [result.to_dict() for result in self.results],
            "root_usage": self.root_usage,
            "elapsed_ms": self.elapsed_ms,
        }
