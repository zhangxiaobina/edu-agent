from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from edu_agent.delegation import (
    DelegationLimitExceeded,
    DelegationPolicy,
    DelegationRuntime,
    PartialSuccessPolicy,
    SubtaskStatus,
    TeachingDelegationService,
    TeachingSubtask,
    TeachingTaskKind,
)
from edu_agent.knowledge import KnowledgeToolProvider, SQLiteKnowledgeProvider, build_synthetic_corpus
from edu_agent.runtime.artifacts import ArtifactStore
from edu_agent.runtime.models import RunContext
from edu_agent.state import StateStore
from edu_agent.tools import registry


class MutableClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


def _context(*, run_id: str = "parent", course_ids: set[int] | None = None, tenant_id: str = "default"):
    return RunContext.create(
        session_id="parent-session",
        run_id=run_id,
        actor_id="teacher-1",
        tenant_id=tenant_id,
        role="teacher",
        course_ids=course_ids or {1},
    )


def _task(key: str, *, kind: TeachingTaskKind = TeachingTaskKind.class_analysis, course_id: int = 1):
    arguments = {"course_id": course_id, "class_id": 1}
    if kind == TeachingTaskKind.chapter_retrieval:
        arguments = {"course_id": course_id, "query": "递归终止条件"}
    return TeachingSubtask(
        task_key=key,
        kind=kind,
        task=f"task {key}",
        arguments=arguments,
        course_ids={course_id},
    )


def _runner(execution, *, delay: float = 0.0, fail_key: str | None = None, entered=None, release=None):
    if entered is not None:
        entered.set()
    if release is not None:
        release.wait(2)
    if delay:
        time.sleep(delay)
    execution.checkpoint("test.before_tool")
    execution.execute_tool(
        "list_exams",
        {"class_id": 1, "course_id": int(execution.task.arguments["course_id"]), "page_size": 1},
    )
    if fail_key == execution.task.task_key:
        raise RuntimeError("controlled failure")
    return {"summary": f"completed:{execution.task.task_key}"}


def _runtime(tmp_path, *, policy=None, child_runner=None, provider=None):
    state = StateStore(tmp_path / "state.db")
    artifacts = ArtifactStore(tmp_path / "artifacts", state)
    runtime = DelegationRuntime(
        state,
        provider or registry,
        artifact_store=artifacts,
        policy=policy,
        child_runner=child_runner,
    )
    return state, artifacts, runtime


def test_parallel_results_match_serial_and_show_speedup(tmp_path):
    tasks = [_task(f"class-{index}") for index in range(3)]
    policy = DelegationPolicy(max_concurrency=3, child_timeout_seconds=2, worker_lease_seconds=3)
    _, _, parallel = _runtime(
        tmp_path / "parallel",
        policy=policy,
        child_runner=lambda execution: _runner(execution, delay=0.08),
    )
    _, _, serial = _runtime(
        tmp_path / "serial",
        policy=DelegationPolicy(max_concurrency=1, child_timeout_seconds=2, worker_lease_seconds=3),
        child_runner=lambda execution: _runner(execution, delay=0.08),
    )
    try:
        parallel_result = parallel.delegate(_context(), tasks)
        serial_result = serial.delegate(_context(), tasks)
    finally:
        parallel.close()
        serial.close()
    assert parallel_result.status == serial_result.status == "completed"
    assert [item.summary for item in parallel_result.results] == [item.summary for item in serial_result.results]
    assert parallel_result.elapsed_ms < serial_result.elapsed_ms * 0.8
    assert parallel_result.root_usage["tool_calls"] == serial_result.root_usage["tool_calls"] == 3


def test_depth_fanout_budget_and_global_concurrency_are_enforced(tmp_path):
    policy = DelegationPolicy(
        max_depth=1,
        max_children_per_parent=2,
        max_concurrency=1,
        max_root_tool_calls=2,
        max_tool_calls_per_child=1,
        child_timeout_seconds=2,
        worker_lease_seconds=3,
    )
    state, _, runtime = _runtime(tmp_path, policy=policy, child_runner=lambda execution: _runner(execution))
    try:
        with pytest.raises(DelegationLimitExceeded):
            runtime.delegate(_context(), [_task("one"), _task("two"), _task("three")])
        result = runtime.delegate(_context(), [_task("one"), _task("two")])
        assert result.status == "completed"
        tree = runtime.tree(_context())
        assert len(tree["nodes"]) == 2
        assert tree["reserved"]["tool_calls"] == 2

        child = tree["nodes"][0]
        child_context = RunContext.create(
            session_id=child["session_id"],
            run_id=child["id"],
            actor_id="teacher-1",
            tenant_id="default",
            role="teacher",
            course_ids={1},
        )
        with pytest.raises(DelegationLimitExceeded):
            runtime.delegate(child_context, [_task("nested")])
    finally:
        runtime.close()
    assert runtime.state.get_run(
        _context().run_id, actor_id="teacher-1", tenant_id="default"
    ) is None


def test_allowed_child_delegation_keeps_lineage_and_root_scope(tmp_path):
    policy = DelegationPolicy(
        max_depth=2,
        allow_child_delegation=True,
        child_timeout_seconds=2,
        worker_lease_seconds=3,
    )
    _, _, runtime = _runtime(tmp_path, policy=policy, child_runner=lambda execution: _runner(execution))
    parent = _context()
    try:
        first = runtime.delegate(parent, [_task("level-one")])
        child_record = runtime.tree(parent)["nodes"][0]
        child_context = RunContext.create(
            session_id=child_record["session_id"],
            run_id=child_record["id"],
            actor_id="teacher-1",
            tenant_id="default",
            role="teacher",
            course_ids={1},
        )
        nested = runtime.delegate(child_context, [_task("level-two")])
        nodes = runtime.tree(parent)["nodes"]
        assert first.results[0].status == nested.results[0].status == SubtaskStatus.completed
        assert [(node["depth"], node["parent_run_id"]) for node in nodes] == [
            (1, parent.run_id),
            (2, child_record["id"]),
        ]
    finally:
        runtime.close()


def test_nested_child_cannot_expand_parent_tool_surface(tmp_path):
    knowledge = SQLiteKnowledgeProvider(build_synthetic_corpus(tmp_path / "knowledge.db"))
    provider = KnowledgeToolProvider(registry, knowledge)
    policy = DelegationPolicy(
        max_depth=2,
        allow_child_delegation=True,
        child_timeout_seconds=2,
        worker_lease_seconds=3,
    )
    _, _, runtime = _runtime(
        tmp_path / "runtime",
        policy=policy,
        provider=provider,
        child_runner=lambda execution: runtime._run_teaching_task(execution),
    )
    parent = _context(tenant_id="school-1")
    try:
        runtime.child_runner = runtime._run_teaching_task
        runtime.delegate(parent, [_task("level-one")])
        child_record = runtime.tree(parent)["nodes"][0]
        child_context = RunContext.create(
            session_id=child_record["session_id"],
            run_id=child_record["id"],
            actor_id="teacher-1",
            tenant_id="school-1",
            role="teacher",
            course_ids={1},
        )
        with pytest.raises(PermissionError):
            runtime.delegate(child_context, [_task("chapter", kind=TeachingTaskKind.chapter_retrieval)])
    finally:
        runtime.close()


def test_child_scope_role_and_tool_surface_cannot_expand(tmp_path):
    _, _, runtime = _runtime(tmp_path)
    try:
        with pytest.raises(PermissionError):
            runtime.delegate(_context(course_ids={1}), [_task("outside", course_id=2)])
        with pytest.raises(PermissionError):
            runtime.delegate(
                _context(),
                [TeachingSubtask(
                    task_key="admin",
                    kind=TeachingTaskKind.class_analysis,
                    task="admin",
                    arguments={"course_id": 1, "class_id": 1},
                    course_ids={1},
                    requested_role="admin",
                )],
            )
        with pytest.raises(DelegationLimitExceeded):
            runtime.delegate(
                _context(),
                [_task("chapter", kind=TeachingTaskKind.chapter_retrieval)],
            )
        record = runtime.state.create_batch(
            parent_context=_context(),
            entries=[runtime._prepare_entry(_context(), _task("input"))],
            root_budget=runtime.policy.root_budget(),
            child_budget=runtime.policy.child_budget(),
            max_depth=runtime.policy.max_depth,
            max_children_per_parent=runtime.policy.max_children_per_parent,
        )[0]
        assert "parent-session" not in record["input"]["messages"][-1]["content"]
        assert record["input"]["plan_projection"]["allowed_tools"] == record["allowed_tools"]
    finally:
        runtime.close()


def test_parent_cancel_propagates_to_running_children(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    policy = DelegationPolicy(max_concurrency=1, child_timeout_seconds=2, worker_lease_seconds=3)
    _, _, runtime = _runtime(
        tmp_path,
        policy=policy,
        child_runner=lambda execution: _runner(execution, entered=entered, release=release),
    )
    result_holder = []
    parent = _context()
    thread = threading.Thread(target=lambda: result_holder.append(runtime.delegate(parent, [_task("cancel")])))
    thread.start()
    assert entered.wait(2)
    assert runtime.cancel_root(parent, reason="operator_cancel") == 1
    release.set()
    thread.join(3)
    runtime.close()
    assert not thread.is_alive()
    assert result_holder[0].results[0].status == SubtaskStatus.cancelled
    assert runtime.tree(parent)["nodes"][0]["status"] == "cancelled"


def test_child_timeout_is_terminal_and_does_not_orphan_worker(tmp_path):
    policy = DelegationPolicy(max_concurrency=1, child_timeout_seconds=0.05, worker_lease_seconds=0.2)
    _, _, runtime = _runtime(
        tmp_path,
        policy=policy,
        child_runner=lambda execution: _runner(execution, delay=0.12),
    )
    result = runtime.delegate(_context(), [_task("slow")])
    runtime.close()
    assert result.results[0].status == SubtaskStatus.timed_out
    assert result.results[0].failure_reason == "CHILD_TIMEOUT"
    assert runtime.tree(_context())["nodes"][0]["status"] == "timed_out"


def test_partial_success_strategies(tmp_path):
    policy = DelegationPolicy(max_concurrency=2, child_timeout_seconds=2, worker_lease_seconds=3)
    _, _, runtime = _runtime(
        tmp_path,
        policy=policy,
        child_runner=lambda execution: _runner(
            execution,
            fail_key=execution.task.task_key if execution.task.task_key.startswith("bad") else None,
        ),
    )
    try:
        best = runtime.delegate(_context(run_id="best"), [_task("good"), _task("bad")])
        assert best.status == "partial"
        fast = runtime.delegate(
            _context(run_id="fast"),
            [_task("bad-fast"), _task("good-fast")],
            partial_policy=PartialSuccessPolicy.fail_fast,
        )
        assert fast.status == "failed"
        quorum = runtime.delegate(
            _context(run_id="quorum"),
            [_task("good-q"), _task("bad-q"), _task("bad-q2")],
            partial_policy=PartialSuccessPolicy.required_quorum,
            required_quorum=1,
        )
        assert quorum.status == "completed"
    finally:
        runtime.close()


def test_worker_lease_expiry_is_recovered_without_orphan(tmp_path):
    clock = MutableClock(datetime(2026, 8, 17, tzinfo=UTC))
    state = StateStore(tmp_path / "state.db", clock=clock)
    artifacts = ArtifactStore(tmp_path / "artifacts", state)
    policy = DelegationPolicy(child_timeout_seconds=1, worker_lease_seconds=2)
    runtime = DelegationRuntime(state, registry, artifact_store=artifacts, policy=policy)
    try:
        entry = runtime._prepare_entry(_context(), _task("crash"))
        record = runtime.state.create_batch(
            parent_context=_context(),
            entries=[entry],
            root_budget=policy.root_budget(),
            child_budget=policy.child_budget(),
            max_depth=policy.max_depth,
            max_children_per_parent=policy.max_children_per_parent,
        )[0]
        assert runtime.state.claim(
            record["id"], worker_owner="dead-worker", max_concurrency=1, lease_seconds=2
        )["status"] == "running"
        clock.advance(3)
        recovered = runtime.state.recover_expired()
        assert recovered[0]["status"] == "failed"
        assert runtime.state.get_run(record["id"], actor_id="teacher-1", tenant_id="default")["failure_reason"] == "WORKER_LEASE_EXPIRED"
    finally:
        runtime.close()


def test_artifact_and_citation_scope_are_verified(tmp_path):
    knowledge = SQLiteKnowledgeProvider(build_synthetic_corpus(tmp_path / "knowledge.db"))
    provider = KnowledgeToolProvider(registry, knowledge)
    state, artifacts, runtime = _runtime(tmp_path / "runtime", provider=provider)
    try:
        chapter_parent = _context(tenant_id="school-1")
        chapter = runtime.delegate(
            chapter_parent,
            [_task("chapter", kind=TeachingTaskKind.chapter_retrieval)],
        )
        assert chapter.results[0].status == SubtaskStatus.completed
        citation = chapter.results[0].citations[0]
        assert knowledge.verify_citation(citation, chapter_parent)
        other = _context(run_id="other", tenant_id="school-secret")
        assert not knowledge.verify_citation(citation, other)

        runtime.result_inline_chars = 256
        class_result = runtime.delegate(_context(run_id="artifact"), [_task("artifact")])
        artifact_id = class_result.results[0].artifacts[0]

        def fabricated(execution):
            _runner(execution)
            return {"summary": "bad citation", "citations": ["fabricated:chunk-1"]}

        runtime.child_runner = fabricated
        rejected = runtime.delegate(_context(run_id="fabricated"), [_task("fabricated")])
        assert rejected.results[0].status == SubtaskStatus.failed
        assert "PARENT_EVIDENCE_REJECTED" in rejected.results[0].warnings
        with pytest.raises(PermissionError):
            artifacts.read_text(artifact_id, context=other)
        assert state.verify_artifact(
            artifact_id,
            actor_id="teacher-1",
            tenant_id="default",
            run_id=class_result.results[0].run_id,
                session_id=runtime.state.get_run(
                class_result.results[0].run_id,
                actor_id="teacher-1",
                tenant_id="default",
            )["session_id"],
        )
    finally:
        runtime.close()


def test_recovery_reuses_completed_task_key_without_duplicate_child(tmp_path):
    calls = []
    state, _, runtime = _runtime(
        tmp_path,
        child_runner=lambda execution: (calls.append(execution.task.task_key) or _runner(execution)),
    )
    parent = _context()
    try:
        first = runtime.delegate(parent, [_task("stable")])
        second = runtime.delegate(parent, [_task("stable")])
        assert first.results[0].run_id == second.results[0].run_id
        assert calls == ["stable"]
        with state.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM delegation_runs").fetchone()[0] == 1
    finally:
        runtime.close()


def test_teaching_consumer_facade_uses_stable_keys(tmp_path):
    knowledge = SQLiteKnowledgeProvider(build_synthetic_corpus(tmp_path / "knowledge.db"))
    provider = KnowledgeToolProvider(registry, knowledge)
    _, _, runtime = _runtime(tmp_path / "runtime", provider=provider)
    try:
        facade = TeachingDelegationService(runtime)
        result = facade.build_intervention(
            _context(tenant_id="school-1"), course_id=1, class_id=1, exam_id=1, query="函数作用域"
        )
        assert result.status == "completed"
        assert [item.task_key for item in result.results] == [
            "intervention:1:1:exam-1:grade",
            "intervention:1:1:exam-1:weakness",
            "intervention:1:1:exam-1:resources",
        ]
    finally:
        runtime.close()
