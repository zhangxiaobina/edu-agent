from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PlanValidationError(ValueError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class PlanStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    incomplete = "incomplete"
    blocked = "blocked"
    budget_exceeded = "budget_exceeded"
    invalid = "invalid"


class StepStatus(str, Enum):
    pending = "pending"
    in_progress = "in_progress"
    completed = "completed"
    blocked = "blocked"


class EvidenceStatus(str, Enum):
    accepted = "accepted"
    rejected = "rejected"


class CompletionCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["tool_success", "artifact", "citation"]
    tool: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def require_tool_for_tool_success(self) -> CompletionCondition:
        if self.kind == "tool_success" and not self.tool:
            raise ValueError("tool_success 完成条件必须声明 tool")
        return self


class PlanStepSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    goal: str = Field(min_length=1, max_length=500)
    depends_on: list[str] = Field(default_factory=list, max_length=32)
    allowed_tools: list[str] = Field(min_length=1, max_length=32)
    expected_tools: list[str] = Field(min_length=1, max_length=32)
    completion_conditions: list[CompletionCondition] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_step_contract(self) -> PlanStepSpec:
        for name, values in (
            ("depends_on", self.depends_on),
            ("allowed_tools", self.allowed_tools),
            ("expected_tools", self.expected_tools),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{name} 不能包含重复项")
        if self.id in self.depends_on:
            raise ValueError("步骤不能依赖自身")
        if not set(self.expected_tools).issubset(self.allowed_tools):
            raise ValueError("expected_tools 必须是 allowed_tools 的子集")
        condition_tools = {
            condition.tool
            for condition in self.completion_conditions
            if condition.kind == "tool_success"
        }
        missing = set(self.expected_tools) - condition_tools
        if missing:
            raise ValueError(f"expected_tools 缺少 tool_success 完成条件：{sorted(missing)}")
        return self


class PlanSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    goal: str = Field(min_length=1, max_length=1000)
    steps: list[PlanStepSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def reject_duplicate_ids(self) -> PlanSpec:
        step_ids = [step.id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("步骤 id 不能重复")
        return self


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    run_id: str
    session_id: str
    actor_id: str
    tenant_id: str
    goal: str
    status: PlanStatus
    max_iterations: int
    iterations_used: int = 0
    failure_reason: str | None = None
    created_at: str
    updated_at: str


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str
    id: str
    position: int
    goal: str
    depends_on: list[str]
    status: StepStatus
    allowed_tools: list[str]
    expected_tools: list[str]
    completion_conditions: list[CompletionCondition]
    failure_reason: str | None = None
    retry_count: int = 0
    event_cursor: int = 0
    created_at: str
    updated_at: str


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    plan_id: str
    step_id: str
    run_id: str
    session_id: str
    actor_id: str
    tenant_id: str
    kind: Literal["tool_event", "artifact", "citation", "missing"]
    status: EvidenceStatus
    tool_name: str | None = None
    tool_event_id: int | None = None
    artifact_id: str | None = None
    citation: str | None = None
    failure_reason: str | None = None
    payload: dict = Field(default_factory=dict)
    created_at: str | None = None


def validate_plan_graph(
    spec: PlanSpec,
    *,
    available_tools: set[str],
    max_steps: int,
) -> PlanSpec:
    if len(spec.steps) > max_steps:
        raise PlanValidationError(
            "PLAN_TOO_LARGE",
            f"计划步骤数超过上限（{len(spec.steps)}/{max_steps}）",
        )
    step_by_id = {step.id: step for step in spec.steps}
    step_ids = set(step_by_id)
    for step in spec.steps:
        unknown_dependencies = set(step.depends_on) - step_ids
        if unknown_dependencies:
            raise PlanValidationError(
                "UNKNOWN_DEPENDENCY",
                f"步骤 {step.id} 引用了未知依赖：{sorted(unknown_dependencies)}",
            )
        referenced_tools = set(step.allowed_tools) | set(step.expected_tools)
        referenced_tools |= {
            condition.tool for condition in step.completion_conditions if condition.tool
        }
        unknown_tools = referenced_tools - available_tools
        if unknown_tools:
            raise PlanValidationError(
                "UNKNOWN_TOOL",
                f"步骤 {step.id} 引用了当前运行不可用的工具：{sorted(unknown_tools)}",
            )

    roots = [step.id for step in spec.steps if not step.depends_on]
    if not roots:
        raise PlanValidationError("NO_ROOT", "计划没有可执行的根步骤")

    children = {step_id: [] for step_id in step_ids}
    indegree = {step.id: len(step.depends_on) for step in spec.steps}
    for step in spec.steps:
        for dependency in step.depends_on:
            children[dependency].append(step.id)

    reachable: set[str] = set()
    stack = list(roots)
    while stack:
        step_id = stack.pop()
        if step_id in reachable:
            continue
        reachable.add(step_id)
        stack.extend(children[step_id])
    unreachable = step_ids - reachable
    if unreachable:
        raise PlanValidationError(
            "UNREACHABLE_STEP",
            f"存在从根步骤不可达的步骤：{sorted(unreachable)}",
        )

    queue = list(roots)
    visited = 0
    while queue:
        step_id = queue.pop(0)
        visited += 1
        for child in children[step_id]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if visited != len(spec.steps):
        cyclic = sorted(step_id for step_id, degree in indegree.items() if degree > 0)
        raise PlanValidationError(
            "CYCLIC_DEPENDENCY",
            f"计划包含循环依赖：{cyclic}",
        )
    return spec
