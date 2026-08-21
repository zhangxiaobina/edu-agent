from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from ..runtime.models import RunContext
from .models import (
    DelegationBatchResult,
    PartialSuccessPolicy,
    TeachingSubtask,
    TeachingTaskKind,
)
from .runtime import DelegationRuntime


def _stable_part(value: Any) -> str:
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    text = str(value).strip()
    digest = hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
    return f"{text[:48] or 'empty'}-{digest}"


def _scope_item(item: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise TypeError("委派消费者输入必须是 mapping")
    return dict(item)


class TeachingDelegationService:
    """面向教育场景的受限委派门面，不暴露通用 spawn API。"""

    def __init__(self, runtime: DelegationRuntime):
        self.runtime = runtime

    def analyze_classes(
        self,
        parent_context: RunContext,
        scopes: Iterable[Mapping[str, Any]],
        *,
        partial_policy: PartialSuccessPolicy = PartialSuccessPolicy.best_effort,
        required_quorum: int | None = None,
    ) -> DelegationBatchResult:
        """并行分析多个班级/考试，task key 可用于恢复时复用已完成 child。"""
        tasks = []
        for item in scopes:
            value = _scope_item(item)
            course_id = int(value["course_id"])
            class_id = int(value["class_id"])
            exam_id = value.get("exam_id")
            suffix = f"exam-{int(exam_id)}" if exam_id is not None else "latest-exam"
            tasks.append(
                TeachingSubtask(
                    task_key=f"class-analysis:course-{course_id}:class-{class_id}:{suffix}",
                    kind=TeachingTaskKind.class_analysis,
                    task=f"分析课程 {course_id} 班级 {class_id} 的学情与考试表现",
                    arguments={
                        "course_id": course_id,
                        "class_id": class_id,
                        **({"exam_id": int(exam_id)} if exam_id is not None else {}),
                        "top": int(value.get("top", 10)),
                        "page_size": int(value.get("page_size", 100)),
                    },
                    course_ids={course_id},
                    plan_step_id=value.get("plan_step_id"),
                )
            )
        return self.runtime.delegate(
            parent_context,
            tasks,
            partial_policy=partial_policy,
            required_quorum=required_quorum,
        )

    def retrieve_chapters(
        self,
        parent_context: RunContext,
        requests: Iterable[Mapping[str, Any]],
        *,
        partial_policy: PartialSuccessPolicy = PartialSuccessPolicy.best_effort,
        required_quorum: int | None = None,
    ) -> DelegationBatchResult:
        """并行检索多个课程章节，结果保留可复验 citation。"""
        tasks = []
        for item in requests:
            value = _scope_item(item)
            course_id = int(value["course_id"])
            query = str(value["query"]).strip()
            if not query:
                raise ValueError("章节检索 query 不能为空")
            tasks.append(
                TeachingSubtask(
                    task_key=(
                        f"chapter-retrieval:course-{course_id}:"
                        f"{_stable_part(query)}"
                    ),
                    kind=TeachingTaskKind.chapter_retrieval,
                    task=f"检索课程 {course_id} 中与“{query}”相关的章节证据",
                    arguments={
                        "course_id": course_id,
                        "query": query,
                        "limit": int(value.get("limit", 3)),
                        "mode": str(value.get("mode", "hybrid")),
                    },
                    course_ids={course_id},
                    plan_step_id=value.get("plan_step_id"),
                )
            )
        return self.runtime.delegate(
            parent_context,
            tasks,
            partial_policy=partial_policy,
            required_quorum=required_quorum,
        )

    def build_intervention(
        self,
        parent_context: RunContext,
        *,
        course_id: int,
        class_id: int,
        query: str,
        exam_id: int | None = None,
        partial_policy: PartialSuccessPolicy = PartialSuccessPolicy.required_quorum,
        required_quorum: int = 2,
        plan_step_id: str | None = None,
    ) -> DelegationBatchResult:
        """拆分干预方案的成绩、薄弱知识和资源检索三个只读子步骤。"""
        course_id = int(course_id)
        class_id = int(class_id)
        query = str(query).strip()
        if not query:
            raise ValueError("干预资源 query 不能为空")
        exam_part = f"exam-{int(exam_id)}" if exam_id is not None else "latest-exam"
        common = {
            "course_id": course_id,
            "class_id": class_id,
            **({"exam_id": int(exam_id)} if exam_id is not None else {}),
        }
        tasks = [
            TeachingSubtask(
                task_key=f"intervention:{course_id}:{class_id}:{exam_part}:grade",
                kind=TeachingTaskKind.intervention_grade,
                task="分析干预对象的成绩与通过情况",
                arguments={**common},
                course_ids={course_id},
                plan_step_id=plan_step_id,
            ),
            TeachingSubtask(
                task_key=f"intervention:{course_id}:{class_id}:{exam_part}:weakness",
                kind=TeachingTaskKind.intervention_weakness,
                task="诊断干预对象的薄弱知识点",
                arguments={**common},
                course_ids={course_id},
                plan_step_id=plan_step_id,
            ),
            TeachingSubtask(
                task_key=f"intervention:{course_id}:{class_id}:{exam_part}:resources",
                kind=TeachingTaskKind.intervention_resources,
                task=f"检索用于干预的课程资源：{query}",
                arguments={**common, "query": query},
                course_ids={course_id},
                plan_step_id=plan_step_id,
            ),
        ]
        return self.runtime.delegate(
            parent_context,
            tasks,
            partial_policy=partial_policy,
            required_quorum=required_quorum,
        )
