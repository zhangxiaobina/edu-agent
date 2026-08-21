from __future__ import annotations

from dataclasses import dataclass

from ..runtime.models import RunContext
from .models import Plan, PlanSpec, PlanStatus, PlanStep, StepStatus
from .planner import PlanGenerator


@dataclass(frozen=True)
class PlanningOptions:
    enabled: bool = True
    max_steps: int = 8
    max_step_retries: int = 2
    max_iterations: int = 12


class PlanCoordinator:
    def __init__(
        self,
        state_store,
        context: RunContext,
        *,
        options: PlanningOptions,
    ):
        self.state_store = state_store
        self.context = context
        self.options = options
        self.plan = self._load_plan()

    def _load_plan(self) -> Plan | None:
        record = self.state_store.get_plan_for_run(
            self.context.run_id,
            session_id=self.context.session_id,
            actor_id=self.context.actor_id,
            tenant_id=self.context.tenant_id,
        )
        return Plan.model_validate(record) if record else None

    def ensure_plan(
        self,
        task: str,
        *,
        generator: PlanGenerator,
        available_tools: set[str],
    ) -> Plan:
        if self.plan is not None:
            return self.plan
        spec = generator.generate(
            task,
            context=self.context,
            available_tools=available_tools,
            max_steps=self.options.max_steps,
        )
        record = self.state_store.create_plan(
            run_id=self.context.run_id,
            session_id=self.context.session_id,
            actor_id=self.context.actor_id,
            tenant_id=self.context.tenant_id,
            spec=spec.model_dump(mode="json"),
            max_iterations=self.options.max_iterations,
            context=self.context,
        )
        self.plan = Plan.model_validate(record)
        return self.plan

    def create_invalid_plan(self, task: str, *, reason: str) -> Plan:
        record = self.state_store.create_invalid_plan(
            run_id=self.context.run_id,
            session_id=self.context.session_id,
            actor_id=self.context.actor_id,
            tenant_id=self.context.tenant_id,
            goal=task,
            failure_reason=reason,
            max_iterations=self.options.max_iterations,
            context=self.context,
        )
        self.plan = Plan.model_validate(record)
        return self.plan

    def steps(self) -> list[PlanStep]:
        if self.plan is None:
            return []
        return [
            PlanStep.model_validate(step)
            for step in self.state_store.get_plan_steps(
                self.plan.id,
                session_id=self.context.session_id,
                actor_id=self.context.actor_id,
                tenant_id=self.context.tenant_id,
            )
        ]

    def active_or_ready_step(self) -> PlanStep | None:
        steps = self.steps()
        active = next((step for step in steps if step.status == StepStatus.in_progress), None)
        if active is not None:
            return active
        completed = {step.id for step in steps if step.status == StepStatus.completed}
        ready = next(
            (
                step
                for step in steps
                if step.status == StepStatus.pending and set(step.depends_on).issubset(completed)
            ),
            None,
        )
        if ready is None:
            return None
        cursor = self.state_store.latest_tool_event_id(
            run_id=self.context.run_id,
            session_id=self.context.session_id,
        )
        self.state_store.update_plan_step(
            self.plan.id,
            ready.id,
            status=StepStatus.in_progress.value,
            event_cursor=cursor,
            failure_reason=None,
            context=self.context,
        )
        self.state_store.update_plan(
            self.plan.id,
            status=PlanStatus.running.value,
            context=self.context,
        )
        return next(step for step in self.steps() if step.id == ready.id)

    def consume_iteration(self) -> bool:
        if self.plan is None:
            return True
        updated = self.state_store.consume_plan_iteration(
            self.plan.id,
            max_iterations=self.options.max_iterations,
            context=self.context,
        )
        self.plan = Plan.model_validate(updated)
        return self.plan.status != PlanStatus.budget_exceeded

    def all_steps_completed(self) -> bool:
        steps = self.steps()
        return bool(steps) and all(step.status == StepStatus.completed for step in steps)

    def blocked(self) -> bool:
        return any(step.status == StepStatus.blocked for step in self.steps())

    def complete(self) -> None:
        if self.plan is None:
            return
        self.state_store.update_plan(
            self.plan.id,
            status=PlanStatus.completed.value,
            context=self.context,
        )
        self.plan = self._load_plan()

    def fail(self, status: PlanStatus, reason: str) -> None:
        if self.plan is None:
            return
        self.state_store.update_plan(
            self.plan.id,
            status=status.value,
            failure_reason=reason,
            context=self.context,
        )
        self.plan = self._load_plan()

    def result(self) -> dict | None:
        if self.plan is None:
            return None
        self.plan = self._load_plan()
        steps = self.steps()
        evidence = self.state_store.get_plan_evidence(
            self.plan.id,
            actor_id=self.context.actor_id,
            tenant_id=self.context.tenant_id,
        )
        missing = []
        for step in steps:
            if step.status != StepStatus.completed:
                missing.append(
                    {
                        "step_id": step.id,
                        "goal": step.goal,
                        "status": step.status.value,
                        "failure_reason": step.failure_reason,
                    }
                )
        return {
            **self.plan.model_dump(mode="json"),
            "steps": [step.model_dump(mode="json") for step in steps],
            "evidence": evidence,
            "missing_evidence": missing,
        }


def plan_spec_for_calls(goal: str, calls: list[tuple[str, str, list[str]]]) -> PlanSpec:
    """供离线 Demo/评测构造严格计划；生产路径仍由 ModelPlanGenerator 生成。"""
    steps = []
    for index, (step_id, tool, dependencies) in enumerate(calls):
        steps.append(
            {
                "id": step_id,
                "goal": f"调用 {tool} 获取第 {index + 1} 步真实数据",
                "depends_on": dependencies,
                "allowed_tools": [tool],
                "expected_tools": [tool],
                "completion_conditions": [{"kind": "tool_success", "tool": tool}],
            }
        )
    return PlanSpec.model_validate({"goal": goal, "steps": steps})
