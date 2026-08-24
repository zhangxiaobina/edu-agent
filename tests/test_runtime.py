from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from edu_agent.agent import run_agent
from edu_agent.engine.base import Engine, EngineResponse, ToolCall
from edu_agent.engine.mock import MockEngine, final
from edu_agent.runtime.config import AppConfig, MemoryConfig, RuntimeConfig, SecurityConfig, StorageConfig
from edu_agent.runtime.artifacts import ArtifactStore, ToolResultBudget
from edu_agent.runtime.context import ContextBudgetExceeded, ContextManager
from edu_agent.runtime.context_engine import (
    CheckpointContextEngine,
    CompactionResult,
    ContextEngine,
)
from edu_agent.runtime.manager import RuntimeManager
from edu_agent.runtime.models import RunContext
from edu_agent.runtime.tool_executor import ExecutionPolicy, PolicyToolExecutor
from edu_agent.service import EduAgentService
from edu_agent.state import MemoryManager, StateStore
from edu_agent.tools import registry


def _context(role="teacher", max_tool_calls=8):
    return RunContext.create(
        session_id="session-1",
        actor_id="actor-1",
        role=role,
        tenant_id="school-1",
        max_tool_calls=max_tool_calls,
    )


def test_state_store_persists_short_and_long_term_memory(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.ensure_session("s1", actor_id="teacher-1", tenant_id="school-1")
    store.append_messages(
        "s1",
        [
            {"role": "user", "content": "以后报告使用表格"},
            {"role": "assistant", "content": "好的"},
        ],
    )
    assert [message["role"] for message in store.get_messages("s1")] == ["user", "assistant"]

    memory = MemoryManager(store)
    context = RunContext.create(
        session_id="s1",
        actor_id="teacher-1",
        role="teacher",
        tenant_id="school-1",
        course_ids={1},
    )
    memory.remember(context, "教师偏好使用 Markdown 表格", importance=0.9)
    snapshot = memory.snapshot(context, "这次报告怎么排版")
    assert snapshot.items == ["教师偏好使用 Markdown 表格"]

    other = RunContext.create(
        session_id="s2",
        actor_id="teacher-2",
        role="teacher",
        tenant_id="school-1",
    )
    assert memory.snapshot(other, "报告").items == []
    with pytest.raises(PermissionError):
        store.ensure_session("s1", actor_id="teacher-2", tenant_id="school-1")


def test_session_binds_role_and_course_scope(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.ensure_session(
        "s1",
        actor_id="teacher-1",
        tenant_id="school-1",
        role="teacher",
        course_ids={1, 2},
    )
    store.ensure_session(
        "s1",
        actor_id="teacher-1",
        tenant_id="school-1",
        role="teacher",
        course_ids={2, 1},
    )
    with pytest.raises(PermissionError, match="role"):
        store.ensure_session(
            "s1",
            actor_id="teacher-1",
            tenant_id="school-1",
            role="admin",
            course_ids={1, 2},
        )
    with pytest.raises(PermissionError, match="course scope"):
        store.ensure_session(
            "s1",
            actor_id="teacher-1",
            tenant_id="school-1",
            role="teacher",
            course_ids={1, 2, 3},
        )


def test_context_keeps_tool_call_and_results_atomic():
    history = [
        {"role": "user", "content": "查考试"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "list_exams", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "list_exams", "content": "{}"},
        {"role": "assistant", "content": "查到了"},
    ]
    snapshot = ContextManager(token_budget=256).prepare(
        system_prompt="system",
        history=history,
        user_message="继续",
    )
    roles = [message["role"] for message in snapshot.messages]
    if "tool" in roles:
        tool_index = roles.index("tool")
        assert roles[tool_index - 1] == "assistant"
        assert snapshot.messages[tool_index - 1].get("tool_calls")


def test_context_rejects_uncuttable_current_turn():
    with pytest.raises(ContextBudgetExceeded, match="缩短或拆分"):
        ContextManager(token_budget=256).prepare(
            system_prompt="system" * 100,
            history=[],
            user_message="当前输入" * 100,
        )


def test_tool_policy_hides_roles_and_requires_approval():
    student_names = {
        tool["function"]["name"]
        for tool in registry.openai_tools(role="student", allow_local_code_execution=False)
    }
    assert "batch_grade" not in student_names
    assert "query_student_scores" not in student_names
    assert "run_code" not in student_names

    denied = PolicyToolExecutor(registry, policy=ExecutionPolicy())
    outcome = denied.execute(
        "create_exam",
        {"exam_name": "测试", "class_id": 3, "course_id": 1},
        _context(),
    )
    assert outcome.error["code"] == "APPROVAL_REQUIRED"

    scoped = denied.execute(
        "list_exams",
        {"course_id": 2},
        RunContext.create(
            session_id="scope",
            actor_id="teacher-1",
            role="teacher",
            course_ids={1},
        ),
    )
    assert scoped.error["code"] == "COURSE_SCOPE_DENIED"


def test_invalid_tool_json_is_returned_to_model():
    def policy(messages, tools, step):
        if step == 0:
            return EngineResponse(
                tool_calls=[ToolCall(id="bad", name="list_exams", arguments="{bad json")]
            )
        tool_result = json.loads(messages[-1]["content"])
        assert tool_result["error"]["code"] == "INVALID_JSON"
        return final("已识别参数错误，没有执行工具。")

    result = run_agent("查考试", MockEngine(policy))
    assert result["final_answer"] == "已识别参数错误，没有执行工具。"


class InspectingEngine(Engine):
    name = "inspecting"

    def __init__(self):
        self.calls: list[list[dict]] = []

    def chat(self, messages: list[dict], tools: list[dict]) -> EngineResponse:
        self.calls.append(messages)
        return EngineResponse(content="收到")


def test_service_recalls_history_and_memory_and_persists_run(tmp_path):
    engine = InspectingEngine()
    config = AppConfig(
        runtime=RuntimeConfig(max_model_calls=4, max_tool_calls=4),
        memory=MemoryConfig(enabled=True, max_recalled_items=3),
        security=SecurityConfig(),
        storage=StorageConfig(state_path=str(tmp_path / "state.db")),
    )
    service = EduAgentService(engine, config=config)
    service.remember("教师偏好使用表格", actor_id="teacher-1", importance=0.9)

    first = service.chat("分析三班", actor_id="teacher-1", role="teacher")
    second = service.chat(
        "继续分析",
        actor_id="teacher-1",
        role="teacher",
        session_id=first.session_id,
    )

    assert first.final_answer == second.final_answer == "收到"
    second_messages = engine.calls[-1]
    assert any(message.get("content") == "分析三班" for message in second_messages)
    assert "教师偏好使用表格" in second_messages[-1]["content"]
    assert service.state_store.count("sessions") == 1
    assert service.state_store.count("messages") == 4
    assert service.state_store.count("runs") == 2


def test_service_accepts_pre_r42_context_engine_signature(tmp_path):
    class LegacyContextEngine(ContextEngine):
        def __init__(self):
            self.compacted = False

        def compact_if_needed(self, session_id, history):
            self.compacted = True
            return CompactionResult(None, 0, None, 0, 0)

        def checkpoint_summary(self, session_id):
            return "legacy checkpoint"

    legacy = LegacyContextEngine()
    engine = InspectingEngine()
    config = AppConfig(
        runtime=RuntimeConfig(max_model_calls=4, max_tool_calls=4),
        memory=MemoryConfig(enabled=False),
        security=SecurityConfig(),
        storage=StorageConfig(state_path=str(tmp_path / "state.db")),
    )
    service = EduAgentService(engine, config=config, context_engine=legacy)

    result = service.chat("继续", actor_id="teacher-1", role="teacher")

    assert result.final_answer == "收到"
    assert legacy.compacted is True
    assert "legacy checkpoint" in engine.calls[-1][-1]["content"]


def test_budget_stops_unbounded_model_loop():
    def endless_policy(messages, tools, step):
        return EngineResponse(tool_calls=[ToolCall(id=f"c{step}", name="list_exams", arguments={})])

    context = RunContext.create(
        session_id="budget",
        actor_id="teacher",
        role="system",
        max_model_calls=2,
        max_tool_calls=2,
    )
    result = run_agent("一直调用", MockEngine(endless_policy), run_context=context)
    assert result["stop_reason"] == "budget_exceeded"
    assert result["budget"]["model_calls"] == 2


def test_context_compaction_is_atomic_recoverable_and_injected_into_user_turn(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.ensure_session("s1", actor_id="teacher-1", tenant_id="school-1")
    messages = [
        {"role": "user", "content": "第一轮问题" + "甲" * 400},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "list_exams", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "name": "list_exams", "content": "{}"},
        {"role": "assistant", "content": "第一轮完成"},
        {"role": "user", "content": "第二轮问题" + "乙" * 400},
        {"role": "assistant", "content": "第二轮完成"},
    ]
    store.append_messages("s1", messages)
    engine = CheckpointContextEngine(
        store,
        token_budget=256,
        trigger_ratio=0.5,
        keep_recent=2,
        summary_max_chars=1200,
    )

    result = engine.compact_if_needed("s1", store.get_messages("s1"))
    assert result.compacted_messages == 4
    assert [message["content"] for message in store.get_messages("s1")] == [
        messages[4]["content"],
        messages[5]["content"],
    ]
    assert store.get_messages("s1", include_compacted=True) == messages
    checkpoint = store.latest_context_checkpoint("s1")
    assert checkpoint["source_messages"] == 4
    assert "list_exams" in checkpoint["summary"]

    snapshot = ContextManager(token_budget=2000).prepare(
        system_prompt="stable-system",
        history=store.get_messages("s1"),
        user_message="继续",
        context_checkpoint=checkpoint["summary"],
    )
    assert snapshot.messages[0] == {"role": "system", "content": "stable-system"}
    assert snapshot.messages[-1]["role"] == "user"
    assert "<context_checkpoint>" in snapshot.messages[-1]["content"]
    assert all(message.get("role") != "user" or message is snapshot.messages[-1]
               for message in snapshot.messages[-1:])


def test_tool_result_spills_to_scoped_artifact_and_redacts_secrets(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.ensure_session("s1", actor_id="teacher-1", tenant_id="school-1")
    context = RunContext.create(
        session_id="s1",
        actor_id="teacher-1",
        tenant_id="school-1",
        role="teacher",
    )
    budget = ToolResultBudget(
        ArtifactStore(tmp_path / "artifacts", store),
        inline_chars=200,
        preview_chars=80,
    )
    processed = budget.apply(
        {
            "ok": True,
            "data": {"rows": ["x" * 300], "api_token": "should-not-leak"},
            "error": None,
            "meta": {},
        },
        context=context,
        tool_name="large_query",
    )
    assert processed["meta"]["spilled"] is True
    assert "artifact_path" not in processed["data"]
    artifact_record = store.get_artifact(
        processed["data"]["artifact_id"],
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
    )
    artifact_path = artifact_record["path"]
    assert str(tmp_path / "artifacts" / "school-1" / "teacher-1" / "s1") in artifact_path
    assert "should-not-leak" not in open(artifact_path, encoding="utf-8").read()
    assert store.count("artifacts") == 1

    class SecretRegistry:
        spec = registry.ToolSpec(
            schema={
                "name": "secret_query",
                "description": "测试敏感参数脱敏",
                "parameters": {
                    "type": "object",
                    "properties": {"api_token": {"type": "string"}},
                    "required": ["api_token"],
                    "additionalProperties": False,
                },
            },
            handler=lambda conn, **kwargs: kwargs,
            category="query",
        )

        @staticmethod
        def get_spec(name):
            return SecretRegistry.spec

        @staticmethod
        def dispatch(name, arguments, conn=None):
            return {"echo": arguments, "blob": "z" * 500}

    executor = PolicyToolExecutor(
        SecretRegistry(),
        policy=ExecutionPolicy(require_write_approval=False),
        state_store=store,
        result_budget=budget,
    )
    outcome = executor.execute(
        "secret_query",
        {"api_token": "secret-value"},
        context,
    )
    assert outcome.meta["spilled"] is True
    with store.connect() as connection:
        event = connection.execute(
            "SELECT arguments_json, outcome_json FROM tool_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert "secret-value" not in event["arguments_json"]
    assert "secret-value" not in event["outcome_json"]
    outcome_artifact = store.get_artifact(
        outcome.data["artifact_id"],
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
    )
    assert "artifact_path" not in outcome.data
    assert "secret-value" not in open(outcome_artifact["path"], encoding="utf-8").read()

    assert budget.artifact_store.read_text(
        outcome.data["artifact_id"],
        context=context,
    )
    other = RunContext.create(
        session_id="other",
        actor_id="teacher-2",
        tenant_id="school-1",
        role="teacher",
    )
    with pytest.raises(PermissionError):
        budget.artifact_store.read_text(outcome.data["artifact_id"], context=other)
    with open(outcome_artifact["path"], "a", encoding="utf-8") as artifact:
        artifact.write("tampered")
    with pytest.raises(RuntimeError, match="完整性"):
        budget.artifact_store.read_text(outcome.data["artifact_id"], context=context)


def test_runtime_manager_serializes_same_session_but_not_different_sessions():
    manager = RuntimeManager()
    same_entered = threading.Event()
    release = threading.Event()
    order = []

    def first():
        with manager.session_scope(
            run_id="r1",
            session_id="same",
            actor_id="a1",
            tenant_id="t1",
        ):
            order.append("first-enter")
            same_entered.set()
            release.wait(1)
            order.append("first-exit")

    def second():
        same_entered.wait(1)
        with manager.session_scope(
            run_id="r2",
            session_id="same",
            actor_id="a1",
            tenant_id="t1",
        ):
            order.append("second-enter")

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    second_thread.start()
    assert same_entered.wait(1)
    time.sleep(0.02)
    assert order == ["first-enter"]
    assert manager.active_runs()[0]["run_id"] == "r1"
    release.set()
    first_thread.join(1)
    second_thread.join(1)
    assert order == ["first-enter", "first-exit", "second-enter"]
    assert manager.active_runs() == []

    both_entered = threading.Barrier(3)

    def independent(run_id):
        with manager.session_scope(
            run_id=run_id,
            session_id=run_id,
            actor_id="a1",
            tenant_id="t1",
        ):
            both_entered.wait(timeout=1)

    threads = [threading.Thread(target=independent, args=(run_id,)) for run_id in ("r3", "r4")]
    for thread in threads:
        thread.start()
    both_entered.wait(timeout=1)
    for thread in threads:
        thread.join(1)


def test_state_store_migrates_existing_schema_without_data_loss(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
            title TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL, role TEXT NOT NULL, content TEXT,
            name TEXT, tool_call_id TEXT, tool_calls_json TEXT,
            created_at TEXT NOT NULL, UNIQUE(session_id, sequence)
        );
        CREATE TABLE scheduled_jobs (
            id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
            name TEXT NOT NULL, prompt TEXT NOT NULL, interval_seconds INTEGER,
            next_run_at TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
            lease_owner TEXT, lease_until TEXT, last_status TEXT,
            last_result TEXT, last_error TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO sessions VALUES ('s1', 'a1', 't1', NULL, 'now', 'now');
        INSERT INTO messages(
            session_id, sequence, role, content, created_at
        ) VALUES ('s1', 0, 'user', 'legacy-message', 'now');
        """
    )
    connection.commit()
    connection.close()

    store = StateStore(path)
    assert store.get_messages("s1") == [{"role": "user", "content": "legacy-message"}]
    with store.connect() as migrated:
        message_columns = {
            row["name"] for row in migrated.execute("PRAGMA table_info(messages)")
        }
        job_columns = {
            row["name"] for row in migrated.execute("PRAGMA table_info(scheduled_jobs)")
        }
    assert {"active", "compaction_id"} <= message_columns
    with store.connect() as migrated:
        session_columns = {
            row["name"] for row in migrated.execute("PRAGMA table_info(sessions)")
        }
    assert {"role", "course_ids_json"} <= session_columns
    assert {"role", "status", "max_attempts", "idempotency_key"} <= job_columns
