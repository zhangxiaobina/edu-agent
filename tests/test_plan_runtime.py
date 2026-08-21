from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from edu_agent.agent import run_agent
from edu_agent.data import db, generate
from edu_agent.engine.mock import MockEngine, call, final
from edu_agent.planning import (
    EvidenceVerifier,
    ModelPlanGenerator,
    PlanCoordinator,
    PlanGenerationError,
    PlanningOptions,
)
from edu_agent.planning.planner import should_create_plan
from edu_agent.runtime.artifacts import ArtifactStore, ToolResultBudget
from edu_agent.runtime.config import load_config
from edu_agent.runtime.models import RunContext
from edu_agent.runtime.tool_executor import ExecutionPolicy, PolicyToolExecutor
from edu_agent.state import StateStore
from edu_agent.tools import registry
from edu_agent.tools.registry import ToolSpec


def _context(
    session_id: str = "session-1",
    *,
    actor_id: str = "teacher-1",
    tenant_id: str = "tenant-1",
    role: str = "teacher",
    course_ids: set[int] | None = None,
    max_model_calls: int = 20,
    max_tool_calls: int = 20,
) -> RunContext:
    return RunContext.create(
        session_id=session_id,
        actor_id=actor_id,
        tenant_id=tenant_id,
        role=role,
        course_ids=course_ids,
        max_model_calls=max_model_calls,
        max_tool_calls=max_tool_calls,
    )


def _step(
    step_id: str,
    tool: str,
    *,
    depends_on: list[str] | None = None,
    conditions: list[dict] | None = None,
) -> dict:
    return {
        "id": step_id,
        "goal": f"使用 {tool} 获取真实数据",
        "depends_on": depends_on or [],
        "allowed_tools": [tool],
        "expected_tools": [tool],
        "completion_conditions": conditions
        or [{"kind": "tool_success", "tool": tool}],
    }


def _spec(*steps: dict) -> dict:
    return {"goal": "完成多步教学任务", "steps": list(steps)}


def _planner(spec: dict) -> ModelPlanGenerator:
    return ModelPlanGenerator(MockEngine(lambda messages, tools, step: final(json.dumps(spec))))


def _coordinator(
    store: StateStore,
    context: RunContext,
    spec: dict,
    *,
    available_tools: set[str] | None = None,
    options: PlanningOptions | None = None,
) -> PlanCoordinator:
    store.ensure_session(
        context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        role=context.role,
        course_ids=context.course_ids,
    )
    coordinator = PlanCoordinator(
        store,
        context,
        options=options or PlanningOptions(),
    )
    coordinator.ensure_plan(
        "复杂教学任务",
        generator=_planner(spec),
        available_tools=available_tools or set(registry.tool_names()),
    )
    return coordinator


@pytest.mark.parametrize(
    ("spec", "code"),
    [
        (
            _spec(_step("duplicate", "list_exams"), _step("duplicate", "list_exams")),
            "INVALID_PLAN_SCHEMA",
        ),
        (
            _spec(_step("child", "list_exams", depends_on=["missing"])),
            "UNKNOWN_DEPENDENCY",
        ),
        (
            _spec(
                _step("root", "list_exams"),
                _step("a", "list_exams", depends_on=["root", "b"]),
                _step("b", "list_exams", depends_on=["a"]),
            ),
            "CYCLIC_DEPENDENCY",
        ),
        (
            _spec(
                _step("a", "list_exams", depends_on=["b"]),
                _step("b", "list_exams", depends_on=["a"]),
            ),
            "NO_ROOT",
        ),
        (_spec(_step("root", "missing_tool")), "UNKNOWN_TOOL"),
        (
            _spec(
                _step("root", "list_exams"),
                _step("a", "list_exams", depends_on=["b"]),
                _step("b", "list_exams", depends_on=["a"]),
            ),
            "UNREACHABLE_STEP",
        ),
    ],
)
def test_plan_schema_rejects_illegal_graphs(spec, code):
    context = _context()
    with pytest.raises(PlanGenerationError) as error:
        _planner(spec).generate(
            "复杂任务",
            context=context,
            available_tools=set(registry.tool_names()),
            max_steps=8,
        )
    assert error.value.code == code


def test_plan_rejects_oversized_graph():
    context = _context()
    spec = _spec(*[_step(f"s{index}", "list_exams") for index in range(3)])
    with pytest.raises(PlanGenerationError) as error:
        _planner(spec).generate(
            "复杂任务",
            context=context,
            available_tools=set(registry.tool_names()),
            max_steps=2,
        )
    assert error.value.code == "PLAN_TOO_LARGE"


def test_invalid_planner_json_stops_without_executing_tools(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context()
    store.ensure_session(
        context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        role=context.role,
    )
    bad_planner = ModelPlanGenerator(MockEngine(lambda messages, tools, step: final("not json")))
    result = run_agent(
        "先查考试，再分析成绩",
        MockEngine(lambda messages, tools, step: pytest.fail("非法计划不得执行")),
        run_context=context,
        planning=PlanningOptions(),
        plan_generator=bad_planner,
        state_store=store,
        force_plan=True,
    )
    assert result["stop_reason"] == "invalid"
    assert result["trace"] == []
    assert result["plan"]["status"] == "invalid"
    assert "INVALID_PLAN_SCHEMA" in result["final_answer"]


def test_complexity_gate_only_selects_real_multi_step_tasks():
    assert should_create_plan("三班 Python 考试有多少人不及格？再给成绩分布") is True
    assert should_create_plan("递归的前置有哪些？再给函数定义到递归的学习路径") is True
    assert should_create_plan("诊断三班最薄弱的知识点，并给最弱点找 3 道练习题") is True
    assert should_create_plan("学生这门课哪里薄弱？给他推一条学习路径") is True
    assert should_create_plan("你好呀") is False
    assert should_create_plan("列出三班 Python 考试") is False


def test_planning_limits_load_from_toml(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[planning]
enabled = false
max_steps = 5
max_step_retries = 1
max_iterations = 7
""".strip(),
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config.planning.enabled is False
    assert config.planning.max_steps == 5
    assert config.planning.max_step_retries == 1
    assert config.planning.max_iterations == 7


def test_legal_dag_advances_and_recovers_without_repeating_completed_step(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context()
    coordinator = _coordinator(
        store,
        context,
        _spec(
            _step("find_exam", "list_exams"),
            _step("find_scores", "query_student_scores", depends_on=["find_exam"]),
        ),
    )
    first = coordinator.active_or_ready_step()
    assert first.id == "find_exam"
    store.record_tool_event(
        run_id=context.run_id,
        session_id=context.session_id,
        tool_name="list_exams",
        arguments={"class_id": 3, "course_id": 1},
        outcome={"ok": True, "data": {"exams": []}, "error": None, "meta": {}},
        duration_ms=1,
    )
    verification = EvidenceVerifier(store, context, max_step_retries=2).verify_step(
        coordinator.plan.id,
        first,
    )
    assert verification.completed is True

    recovered = PlanCoordinator(store, context, options=PlanningOptions())
    second = recovered.active_or_ready_step()
    assert second.id == "find_scores"
    assert next(step for step in recovered.steps() if step.id == "find_exam").status == "completed"


def test_premature_final_answer_is_blocked_until_tool_evidence_exists(tmp_path):
    state = StateStore(tmp_path / "state.db")
    context = _context()
    state.ensure_session(
        context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        role=context.role,
    )
    database_path = tmp_path / "edu.db"
    generate.build(seed=42, out_path=database_path)
    connection = db.connect(database_path)

    def policy(messages, tools, step):
        if step == 0:
            return final("已经查完了")
        if step == 1:
            return call(step, "list_exams", class_id=3, course_id=1)
        return final("已基于真实考试列表完成回答")

    executor = PolicyToolExecutor(
        registry,
        policy=ExecutionPolicy.legacy_demo(),
        state_store=state,
    )
    try:
        result = run_agent(
            "先查三班 Python 考试，再基于结果回答",
            MockEngine(policy),
            db_conn=connection,
            run_context=context,
            tool_executor=executor,
            planning=PlanningOptions(max_iterations=6),
            plan_generator=_planner(_spec(_step("find_exam", "list_exams"))),
            state_store=state,
            force_plan=True,
        )
    finally:
        connection.close()

    assert result["stop_reason"] == "completed"
    assert [item["tool"] for item in result["trace"]] == ["list_exams"]
    assert result["plan"]["status"] == "completed"
    assert any(
        item["kind"] == "missing" and item["status"] == "rejected"
        for item in result["plan"]["evidence"]
    )
    assert any(
        item["kind"] == "tool_event" and item["status"] == "accepted"
        for item in result["plan"]["evidence"]
    )
    user_messages = [message for message in result["messages"] if message["role"] == "user"]
    assert user_messages == [{"role": "user", "content": "先查三班 Python 考试，再基于结果回答"}]


@pytest.mark.parametrize(
    ("raw_arguments", "expected_code"),
    [
        ("{bad json", "INVALID_JSON"),
        ({"class_id": "three", "course_id": 1}, "INVALID_ARGUMENTS"),
    ],
)
def test_bad_tool_arguments_create_rejected_evidence(tmp_path, raw_arguments, expected_code):
    store = StateStore(tmp_path / f"{expected_code}.db")
    context = _context()
    coordinator = _coordinator(store, context, _spec(_step("find", "list_exams")))
    step = coordinator.active_or_ready_step()
    executor = PolicyToolExecutor(
        registry,
        policy=ExecutionPolicy.legacy_demo(),
        state_store=store,
    )
    outcome = executor.execute_raw("list_exams", raw_arguments, context)
    assert outcome.error["code"] == expected_code
    verification = EvidenceVerifier(store, context, max_step_retries=2).verify_step(
        coordinator.plan.id,
        step,
    )
    assert verification.failure_reason == expected_code
    evidence = store.get_step_evidence(
        coordinator.plan.id,
        step.id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
    )
    assert evidence[-1]["status"] == "rejected"


def test_approval_denial_and_course_scope_are_rejected_as_evidence(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context(course_ids={1})
    coordinator = _coordinator(store, context, _spec(_step("create", "create_exam")))
    step = coordinator.active_or_ready_step()
    executor = PolicyToolExecutor(
        registry,
        policy=ExecutionPolicy(require_write_approval=True),
        approval_handler=lambda request: False,
        state_store=store,
    )
    denied = executor.execute(
        "create_exam",
        {"exam_name": "期中考试", "class_id": 3, "course_id": 1},
        context,
    )
    assert denied.error["code"] == "APPROVAL_REQUIRED"
    verification = EvidenceVerifier(store, context, max_step_retries=2).verify_step(
        coordinator.plan.id,
        step,
    )
    assert verification.failure_reason == "APPROVAL_REQUIRED"

    second_context = _context(session_id="session-2", course_ids={1})
    second = _coordinator(
        store,
        second_context,
        _spec(_step("find", "list_exams")),
    )
    second_step = second.active_or_ready_step()
    scoped_executor = PolicyToolExecutor(
        registry,
        policy=ExecutionPolicy(require_write_approval=False),
        state_store=store,
    )
    scoped = scoped_executor.execute(
        "list_exams",
        {"class_id": 3, "course_id": 2},
        second_context,
    )
    assert scoped.error["code"] == "COURSE_SCOPE_DENIED"
    scoped_verification = EvidenceVerifier(
        store,
        second_context,
        max_step_retries=2,
    ).verify_step(second.plan.id, second_step)
    assert scoped_verification.failure_reason == "COURSE_SCOPE_DENIED"


class _LargeProvider:
    def __init__(self):
        self.spec = ToolSpec(
            schema={
                "name": "large_tool",
                "description": "返回大结果",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
            handler=lambda conn=None: {"payload": "x" * 2000},
            category="query",
        )

    def get_spec(self, name):
        return self.spec if name == "large_tool" else None

    def dispatch(self, name, arguments, conn=None):
        return {"payload": "x" * 2000}


class _CitationProvider(_LargeProvider):
    def __init__(self):
        super().__init__()
        object.__setattr__(
            self.spec,
            "schema",
            {
                "name": "citation_tool",
                "description": "返回引用",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        )

    def get_spec(self, name):
        return self.spec if name == "citation_tool" else None

    def dispatch(self, name, arguments, conn=None):
        return {"answer": "真实资料", "citation_id": "course-1:chunk-7"}


def test_artifact_spill_is_bound_as_accepted_evidence(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context()
    coordinator = _coordinator(
        store,
        context,
        _spec(
            _step(
                "large",
                "large_tool",
                conditions=[
                    {"kind": "tool_success", "tool": "large_tool"},
                    {"kind": "artifact", "tool": "large_tool"},
                ],
            )
        ),
        available_tools={"large_tool"},
    )
    step = coordinator.active_or_ready_step()
    artifacts = ArtifactStore(tmp_path / "artifacts", store)
    executor = PolicyToolExecutor(
        _LargeProvider(),
        policy=ExecutionPolicy.legacy_demo(),
        state_store=store,
        result_budget=ToolResultBudget(
            artifacts,
            inline_chars=100,
            preview_chars=20,
            turn_budget_chars=200,
        ),
    )
    outcome = executor.execute("large_tool", {}, context)
    assert outcome.meta["spilled"] is True
    verification = EvidenceVerifier(store, context, max_step_retries=2).verify_step(
        coordinator.plan.id,
        step,
    )
    assert verification.completed is True
    evidence = coordinator.result()["evidence"]
    assert any(item["kind"] == "artifact" and item["status"] == "accepted" for item in evidence)


def test_tampered_artifact_is_rejected_as_evidence(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context()
    coordinator = _coordinator(
        store,
        context,
        _spec(
            _step(
                "large",
                "large_tool",
                conditions=[
                    {"kind": "tool_success", "tool": "large_tool"},
                    {"kind": "artifact", "tool": "large_tool"},
                ],
            )
        ),
        available_tools={"large_tool"},
    )
    step = coordinator.active_or_ready_step()
    artifacts = ArtifactStore(tmp_path / "artifacts", store)
    executor = PolicyToolExecutor(
        _LargeProvider(),
        policy=ExecutionPolicy.legacy_demo(),
        state_store=store,
        result_budget=ToolResultBudget(artifacts, inline_chars=100, preview_chars=20),
    )
    outcome = executor.execute("large_tool", {}, context)
    artifact_id = outcome.data["artifact_id"]
    artifact = store.get_artifact(
        artifact_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
    )
    Path(artifact["path"]).write_text("tampered", encoding="utf-8")
    verification = EvidenceVerifier(store, context, max_step_retries=2).verify_step(
        coordinator.plan.id,
        step,
    )
    assert verification.completed is False
    assert any(
        item["kind"] == "artifact" and item["status"] == "rejected"
        for item in coordinator.result()["evidence"]
    )


def test_malformed_tool_event_is_rejected_without_crashing(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context()
    coordinator = _coordinator(store, context, _spec(_step("find", "list_exams")))
    step = coordinator.active_or_ready_step()
    store.record_tool_event(
        run_id=context.run_id,
        session_id=context.session_id,
        tool_name="list_exams",
        arguments={},
        outcome={"ok": False, "error": {"code": "PLACEHOLDER"}},
        duration_ms=1,
    )
    with store.connect() as connection:
        connection.execute(
            "UPDATE tool_events SET outcome_json=? WHERE run_id=?",
            ("{broken", context.run_id),
        )
    verification = EvidenceVerifier(store, context, max_step_retries=2).verify_step(
        coordinator.plan.id,
        step,
    )
    assert verification.failure_reason == "MALFORMED_TOOL_EVENT"
    assert any(
        item["failure_reason"] == "MALFORMED_TOOL_EVENT"
        for item in coordinator.result()["evidence"]
    )


def test_final_evidence_gate_rechecks_persisted_completed_step(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context()
    coordinator = _coordinator(store, context, _spec(_step("find", "list_exams")))
    step = coordinator.active_or_ready_step()
    store.update_plan_step(coordinator.plan.id, step.id, status="completed")
    verifier = EvidenceVerifier(store, context, max_step_retries=2)
    assert verifier.plan_has_complete_evidence(coordinator.plan.id, coordinator.steps()) is False
    assert verifier.missing_conditions(coordinator.plan.id, step) == ("tool_success:list_exams",)


def test_citation_is_bound_as_accepted_evidence(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context()
    coordinator = _coordinator(
        store,
        context,
        _spec(
            _step(
                "cite",
                "citation_tool",
                conditions=[
                    {"kind": "tool_success", "tool": "citation_tool"},
                    {"kind": "citation", "tool": "citation_tool"},
                ],
            )
        ),
        available_tools={"citation_tool"},
    )
    step = coordinator.active_or_ready_step()
    executor = PolicyToolExecutor(
        _CitationProvider(),
        policy=ExecutionPolicy.legacy_demo(),
        state_store=store,
    )
    outcome = executor.execute("citation_tool", {}, context)
    assert outcome.ok is True
    verification = EvidenceVerifier(
        store,
        context,
        max_step_retries=2,
        citation_verifier=lambda citation, current: citation == "course-1:chunk-7",
    ).verify_step(
        coordinator.plan.id,
        step,
    )
    assert verification.completed is True
    assert any(item["kind"] == "citation" for item in coordinator.result()["evidence"])


def test_tool_exception_creates_rejected_evidence(tmp_path):
    class FailingProvider(_LargeProvider):
        def dispatch(self, name, arguments, conn=None):
            raise RuntimeError("backend unavailable")

    store = StateStore(tmp_path / "state.db")
    context = _context()
    coordinator = _coordinator(
        store,
        context,
        _spec(_step("fail", "large_tool")),
        available_tools={"large_tool"},
    )
    step = coordinator.active_or_ready_step()
    outcome = PolicyToolExecutor(
        FailingProvider(),
        policy=ExecutionPolicy.legacy_demo(),
        state_store=store,
    ).execute("large_tool", {}, context)
    assert outcome.error["code"] == "TOOL_EXCEPTION"
    verification = EvidenceVerifier(store, context, max_step_retries=2).verify_step(
        coordinator.plan.id,
        step,
    )
    assert verification.failure_reason == "TOOL_EXCEPTION"


def test_irrelevance_and_simple_tool_task_do_not_create_plan_or_extra_model_call(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context()
    store.ensure_session(
        context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        role=context.role,
    )
    chitchat_engine = MockEngine(lambda messages, tools, step: final("你好"))
    chitchat = run_agent(
        "你好呀",
        chitchat_engine,
        run_context=context,
        planning=PlanningOptions(),
        state_store=store,
        plan_generator=lambda *args, **kwargs: pytest.fail("不应生成计划"),
    )
    assert chitchat["trace"] == []
    assert chitchat["budget"]["model_calls"] == 1
    assert store.count("plans") == 0

    simple_context = _context(session_id="simple")
    store.ensure_session(
        simple_context.session_id,
        actor_id=simple_context.actor_id,
        tenant_id=simple_context.tenant_id,
        role=simple_context.role,
    )

    def simple_policy(messages, tools, step):
        return call(step, "list_exams", class_id=3, course_id=1) if step == 0 else final("完成")

    simple = run_agent(
        "列出三班 Python 考试",
        MockEngine(simple_policy),
        run_context=simple_context,
        planning=PlanningOptions(),
        state_store=store,
        plan_generator=lambda *args, **kwargs: pytest.fail("不应生成计划"),
    )
    assert [item["tool"] for item in simple["trace"]] == ["list_exams"]
    assert simple["budget"]["model_calls"] == 2
    assert store.count("plans") == 0


def test_student_plan_cannot_elevate_role_tool_surface(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context(role="student")
    store.ensure_session(
        context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        role=context.role,
    )
    result = run_agent(
        "先查成绩，再分析薄弱点",
        MockEngine(lambda messages, tools, step: final("不应执行")),
        run_context=context,
        tool_executor=PolicyToolExecutor(
            registry,
            policy=ExecutionPolicy(require_write_approval=False),
            state_store=store,
        ),
        planning=PlanningOptions(),
        plan_generator=_planner(_spec(_step("scores", "query_student_scores"))),
        state_store=store,
        force_plan=True,
    )
    assert result["stop_reason"] == "invalid"
    assert result["trace"] == []
    assert "UNKNOWN_TOOL" in result["final_answer"]


def test_concurrent_sessions_keep_plans_and_evidence_isolated(tmp_path):
    store = StateStore(tmp_path / "state.db")
    contexts = [
        _context(session_id="session-a", actor_id="actor-a"),
        _context(session_id="session-b", actor_id="actor-b"),
    ]

    def create(context):
        return _coordinator(
            store,
            context,
            _spec(_step("find", "list_exams")),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        coordinators = list(pool.map(create, contexts))

    assert coordinators[0].plan.id != coordinators[1].plan.id
    assert all(len(coordinator.steps()) == 1 for coordinator in coordinators)
    with pytest.raises(PermissionError):
        store.get_plan_for_run(
            contexts[0].run_id,
            session_id=contexts[1].session_id,
            actor_id=contexts[1].actor_id,
            tenant_id=contexts[1].tenant_id,
        )
    with pytest.raises(PermissionError):
        store.record_plan_evidence(
            plan_id=coordinators[0].plan.id,
            step_id="find",
            context=contexts[1],
            kind="missing",
            status="rejected",
            failure_reason="CROSS_OWNER",
            payload={},
        )


def test_plan_budget_exhaustion_stops_deterministically(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context(max_model_calls=20)
    store.ensure_session(
        context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        role=context.role,
    )
    engine = MockEngine(lambda messages, tools, step: final("没有调用工具但声称完成"))
    result = run_agent(
        "先查考试，再给出结论",
        engine,
        run_context=context,
        planning=PlanningOptions(max_step_retries=20, max_iterations=2),
        plan_generator=_planner(_spec(_step("find", "list_exams"))),
        state_store=store,
        force_plan=True,
    )
    assert result["stop_reason"] == "budget_exceeded"
    assert result["budget"]["model_calls"] == 3
    assert result["plan"]["iterations_used"] == 2
    assert result["plan"]["missing_evidence"]


def test_tool_budget_exhaustion_is_not_misreported_as_blocked(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context(max_tool_calls=0)
    store.ensure_session(
        context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        role=context.role,
    )

    def policy(messages, tools, step):
        return call(step, "list_exams", class_id=3, course_id=1)

    result = run_agent(
        "先查考试，再给出结论",
        MockEngine(policy),
        run_context=context,
        planning=PlanningOptions(max_iterations=5),
        plan_generator=_planner(_spec(_step("find", "list_exams"))),
        state_store=store,
        force_plan=True,
    )
    assert result["stop_reason"] == "budget_exceeded"
    assert result["plan"]["status"] == "budget_exceeded"
    assert "BUDGET_EXCEEDED" in result["final_answer"]


def test_old_database_is_upgraded_in_place_with_plan_migration(tmp_path):
    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
            title TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            sequence INTEGER NOT NULL, role TEXT NOT NULL, content TEXT,
            name TEXT, tool_call_id TEXT, tool_calls_json TEXT, created_at TEXT NOT NULL,
            UNIQUE(session_id, sequence)
        );
        INSERT INTO sessions VALUES ('legacy', 'actor', 'tenant', 'old', 't0', 't0');
        INSERT INTO messages(
            session_id, sequence, role, content, created_at
        ) VALUES ('legacy', 0, 'user', '保留我', 't0');
        """
    )
    connection.commit()
    connection.close()

    store = StateStore(path)
    assert store.get_messages("legacy") == [{"role": "user", "content": "保留我"}]
    assert store.count("plans") == 0
    assert store.count("plan_steps") == 0
    assert store.count("evidence") == 0
    with store.connect() as migrated:
        migrations = {
            row["version"]
            for row in migrated.execute("SELECT version FROM state_schema_migrations")
        }
    assert {
        "001_plan_graph_evidence",
        "002_course_rag_memory",
        "003_transactional_tool_runtime",
        "004_distributed_runtime_control",
    } <= migrations
    with store.connect() as migrated:
        tool_event_columns = {
            row["name"] for row in migrated.execute("PRAGMA table_info(tool_events)")
        }
        evidence_columns = {
            row["name"] for row in migrated.execute("PRAGMA table_info(evidence)")
        }
        assert {"tool_call_id", "operation_id", "operation_status"} <= tool_event_columns
        assert "operation_id" in evidence_columns
