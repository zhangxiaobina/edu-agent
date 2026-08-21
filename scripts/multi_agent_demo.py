"""阶段 5：真实 SQLite 教学消费者的并行委派演示。"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from edu_agent.delegation import (
    DelegationPolicy,
    DelegationRuntime,
    PartialSuccessPolicy,
    TeachingDelegationService,
    TeachingSubtask,
    TeachingTaskKind,
)
from edu_agent.knowledge import KnowledgeToolProvider, SQLiteKnowledgeProvider, build_synthetic_corpus
from edu_agent.runtime.artifacts import ArtifactStore
from edu_agent.runtime.models import RunContext
from edu_agent.state import StateStore
from edu_agent.tools import registry


def _context(run_id: str = "demo-parent") -> RunContext:
    return RunContext.create(
        session_id="demo-session",
        run_id=run_id,
        actor_id="teacher-demo",
        tenant_id="school-1",
        role="teacher",
        course_ids={1, 2},
    )


def _runner(execution):
    time.sleep(0.08)
    if execution.task.task_key.endswith(":failed"):
        raise RuntimeError("演示中的受控失败")
    if execution.task.kind == TeachingTaskKind.chapter_retrieval:
        outcome = execution.execute_tool(
            "retrieve_course_materials",
            {
                "query": execution.task.arguments["query"],
                "course_id": execution.task.arguments["course_id"],
                "limit": 2,
                "mode": "sparse",
            },
        )
    else:
        outcome = execution.execute_tool(
            "list_exams",
            {
                "class_id": execution.task.arguments["class_id"],
                "course_id": execution.task.arguments["course_id"],
                "page_size": 2,
            },
        )
    if not outcome.ok:
        raise RuntimeError(outcome.error)
    return {"summary": f"完成 {execution.task.task_key}"}


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="edu-agent-stage5-") as directory:
        root = Path(directory)
        knowledge = SQLiteKnowledgeProvider(build_synthetic_corpus(root / "knowledge.db"))
        provider = KnowledgeToolProvider(registry, knowledge)
        state = StateStore(root / "state.db")
        artifacts = ArtifactStore(root / "artifacts", state)
        policy = DelegationPolicy(max_concurrency=3, child_timeout_seconds=2, worker_lease_seconds=3)
        with DelegationRuntime(
            state,
            provider,
            artifact_store=artifacts,
            policy=policy,
            child_runner=_runner,
        ) as runtime:
            facade = TeachingDelegationService(runtime)
            result = facade.analyze_classes(
                _context(),
                [
                    {"course_id": 1, "class_id": 1},
                    {"course_id": 1, "class_id": 2},
                    {"course_id": 2, "class_id": 1},
                ],
                partial_policy=PartialSuccessPolicy.best_effort,
            )
            chapter = facade.retrieve_chapters(
                _context("chapter-parent"),
                [
                    {"course_id": 1, "query": "递归终止条件"},
                    {"course_id": 1, "query": "函数作用域"},
                ],
            )
            failure = runtime.delegate(
                _context("failure-parent"),
                [
                    TeachingSubtask(
                        task_key="demo:ok",
                        kind=TeachingTaskKind.class_analysis,
                        task="成功的只读分析",
                        arguments={"course_id": 1, "class_id": 1},
                        course_ids={1},
                    ),
                    TeachingSubtask(
                        task_key="demo:failed",
                        kind=TeachingTaskKind.class_analysis,
                        task="受控失败的只读分析",
                        arguments={"course_id": 1, "class_id": 2},
                        course_ids={1},
                    ),
                ],
                partial_policy=PartialSuccessPolicy.best_effort,
            )
            tree = runtime.tree(_context())
            print(json.dumps({"analysis": result.to_dict(), "chapter": chapter.to_dict(), "failure": failure.to_dict(), "run_tree": tree}, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
