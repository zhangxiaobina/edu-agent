from __future__ import annotations

import json
import re
from typing import Protocol

from pydantic import ValidationError

from ..engine.base import Engine
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
    def __init__(self, engine: Engine):
        self.engine = engine

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
        response = self.engine.chat(
            [
                {
                    "role": "system",
                    "content": (
                        f"{PLANNER_SYSTEM_PROMPT}\n\n"
                        f"<planning_context>{json.dumps(planning_context, ensure_ascii=False)}"
                        "</planning_context>"
                    ),
                },
                {"role": "user", "content": task},
            ],
            [],
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
