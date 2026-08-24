from __future__ import annotations

import json
import re
from typing import Protocol

from pydantic import ValidationError

from ..engine.base import Engine
from ..runtime.cancellation import call_with_cancellation
from ..runtime.cancellation import accepts_keyword_argument
from ..runtime.context import ContextBudgetExceeded
from ..runtime.models import RunContext
from .models import PlanSpec, PlanValidationError, validate_plan_graph


class PlanGenerationError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class PlanGenerator(Protocol):
    def generate(
        self,
        task: str,
        *,
        context: RunContext,
        available_tools: set[str],
        max_steps: int,
    ) -> PlanSpec: ...


PLANNER_SYSTEM_PROMPT = """你是教学 Agent 的计划编译器。只输出一个 JSON object，不要 Markdown。
计划只包含必须由工具真实结果证明的步骤，不添加“总结回答”步骤。每步只能引用给定工具，
依赖必须形成有根、无环、全部可达的 DAG。完成条件只能使用 tool_success、artifact、citation。
不要扩大用户身份、课程范围或工具能力。"""


class ModelPlanGenerator:
    def __init__(
        self,
        engine: Engine,
        *,
        context_accounting=None,
        max_output_tokens: int | None = None,
    ):
        self.engine = engine
        self.context_accounting = context_accounting
        self.max_output_tokens = max_output_tokens

    def generate(
        self,
        task: str,
        *,
        context: RunContext,
        available_tools: set[str],
        max_steps: int,
    ) -> PlanSpec:
        context.budget.consume_model_call()
        planning_context = {
            "available_tools": sorted(available_tools),
            "max_steps": max_steps,
            "output_schema": PlanSpec.model_json_schema(),
        }
        planning_injection = (
            f"<planning_context>{json.dumps(planning_context, ensure_ascii=False)}"
            "</planning_context>"
        )
        messages = [
            {
                "role": "system",
                "content": f"{PLANNER_SYSTEM_PROMPT}\n\n{planning_injection}",
            },
            {"role": "user", "content": task},
        ]
        context_accounting = self.context_accounting or context.context_accounting
        max_output_tokens = self.max_output_tokens
        if max_output_tokens is None and context_accounting is not None:
            max_output_tokens = context_accounting.max_output_reserve_tokens
        accounting = None
        route_accounting = []
        if context_accounting is not None:
            for route in context_accounting.routes:
                route_accounting.append(
                    context_accounting.measure(
                        messages=messages,
                        tools=[],
                        phase="planner",
                        route=route,
                        current_user_turn=task,
                        current_user_wire_content=task,
                        base_system_prompt=PLANNER_SYSTEM_PROMPT,
                        memory_checkpoint_injection="",
                        plan_evidence_injection=planning_injection,
                    )
                )
            accounting = route_accounting[0]
            if accounting.decision != "send":
                raise ContextBudgetExceeded(
                    "planner request exceeds the reserved context budget",
                    breakdown=accounting,
                )
        kwargs = {}
        if max_output_tokens is not None and accepts_keyword_argument(
            self.engine.chat,
            "max_output_tokens",
        ):
            kwargs["max_output_tokens"] = max_output_tokens
        try:
            response = call_with_cancellation(
                self.engine.chat,
                messages,
                [],
                cancellation_token=context.cancellation_token,
                **kwargs,
            )
        except Exception as error:
            if accounting is not None:
                context_accounting.settle(
                    accounting,
                    getattr(error, "usage", None),
                    phase="planner",
                )
            raise
        if accounting is not None:
            selected_accounting = context_accounting.select_breakdown(
                route_accounting,
                response_model=response.model,
                usage=response.usage,
            )
            context_accounting.settle(
                selected_accounting,
                response.usage,
                phase="planner",
            )
        if response.tool_calls:
            raise PlanGenerationError(
                "PLANNER_TOOL_CALL",
                "计划模型必须返回 JSON，不能发起工具调用",
            )
        if not response.content:
            raise PlanGenerationError("EMPTY_PLAN", "计划模型返回了空内容")
        try:
            spec = PlanSpec.model_validate_json(response.content)
            return validate_plan_graph(
                spec,
                available_tools=available_tools,
                max_steps=max_steps,
            )
        except ValidationError as error:
            raise PlanGenerationError(
                "INVALID_PLAN_SCHEMA",
                "计划未通过严格 Schema 校验",
                details={"errors": error.errors(include_url=False)},
            ) from error
        except PlanValidationError as error:
            raise PlanGenerationError(
                error.code,
                str(error),
                details=error.details,
            ) from error


_IRRELEVANT_PATTERNS = (
    r"^(你好|您好|嗨|hello|hi)\b",
    r"(谢谢|辛苦了|再见)[！!。\s]*$",
    r"(订|买).*(机票|酒店)",
    r"list\s+和\s+tuple.*区别",
)
_ACTION_GROUPS = (
    ("不及格", "成绩", "分数", "考得怎么样"),
    ("错在哪", "错题", "错误分析"),
    ("薄弱", "掌握得最差", "诊断"),
    ("练习题", "推题", "找题"),
    ("学习路径", "前置", "先修"),
    ("成绩分布", "分布"),
    ("组卷", "卷子"),
    ("建一场", "创建考试", "建考试"),
    ("布置作业", "判分", "批改"),
)


def should_create_plan(task: str) -> bool:
    normalized = task.strip().lower()
    if not normalized or any(re.search(pattern, normalized) for pattern in _IRRELEVANT_PATTERNS):
        return False
    action_count = sum(any(token in normalized for token in group) for group in _ACTION_GROUPS)
    has_sequence = bool(re.search(r"(再|然后|之后|最后|并|同时|以及|、|，|,|；|;|？|\?)", normalized))
    repeated_graph_operation = "前置" in normalized and "学习路径" in normalized
    return (action_count >= 2 and has_sequence) or repeated_graph_operation
