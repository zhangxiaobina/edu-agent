"""R3.3 contract matrix for the 16 built-in tool boundaries."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from edu_agent.code_execution import (
    ExecutionResult,
    ProviderCapabilities,
    ProviderHealth,
)
from edu_agent.data import db, generate
from edu_agent.runtime.models import RunContext
from edu_agent.runtime.tool_executor import ExecutionPolicy, PolicyToolExecutor, ToolResult
from edu_agent.state import StateStore
from edu_agent.teaching import SyntheticProvider
from edu_agent.tools import registry


@dataclass(frozen=True)
class _ExpectedContract:
    capability: str
    effect: str
    boundary: str


EXPECTED_CONTRACTS = {
    "query_student_scores": _ExpectedContract("teaching.query", "read", "teaching_query"),
    "list_exams": _ExpectedContract("teaching.query", "read", "teaching_query"),
    "get_class_roster": _ExpectedContract("teaching.query", "read", "teaching_query"),
    "search_questions": _ExpectedContract("teaching.query", "read", "teaching_query"),
    "get_learning_progress": _ExpectedContract("teaching.query", "read", "teaching_query"),
    "query_knowledge_graph": _ExpectedContract(
        "teaching.knowledge", "read", "teaching_query"
    ),
    "recommend_study_path": _ExpectedContract(
        "teaching.knowledge", "read", "teaching_query"
    ),
    "analyze_class_errors": _ExpectedContract(
        "teaching.analysis", "read", "teaching_query"
    ),
    "diagnose_weak_points": _ExpectedContract(
        "teaching.analysis", "read", "teaching_query"
    ),
    "get_score_distribution": _ExpectedContract(
        "teaching.analysis", "read", "teaching_query"
    ),
    "create_exam": _ExpectedContract("teaching.write", "write", "teaching_command"),
    "generate_paper": _ExpectedContract("teaching.query", "read", "teaching_command"),
    "batch_grade": _ExpectedContract("teaching.write", "write", "teaching_command"),
    "assign_homework": _ExpectedContract(
        "teaching.write", "write", "teaching_command"
    ),
    "generate_questions": _ExpectedContract(
        "teaching.content", "conditional_write", "teaching_command"
    ),
    "run_code": _ExpectedContract("code_execution", "code_execution", "code_execution"),
}


class _RecordingTeachingProvider:
    def __init__(self, base):
        self.base = base
        self.queries = []
        self.commands = []

    def execute(self, query, *, connection=None):
        self.queries.append(query)
        return self.base.execute(query, connection=connection)

    def execute_command(self, command, *, connection=None):
        self.commands.append(command)
        return self.base.execute_command(command, connection=connection)


class _CodeProvider:
    name = "contract-code"

    def __init__(self):
        self.requests = []
        self._capabilities = ProviderCapabilities(
            provider=self.name,
            languages=frozenset({"python"}),
            trusted_isolation=True,
            supports_health_check=True,
            supports_wall_time=True,
            supports_cpu_time=True,
            supports_memory=True,
            supports_process_limit=True,
            supports_file_size_limit=True,
            supports_output_limit=True,
            supports_network_policy=True,
            supports_network_allowlist=False,
            supports_cancellation=True,
        )

    def capabilities(self):
        return self._capabilities

    def health_check(self, *, force=False):
        return ProviderHealth(
            healthy=True,
            checked_at=1.0,
            message="healthy",
            capabilities=self._capabilities,
            backend_languages=("python3",),
        )

    def execute(self, request, *, cancel_event=None):
        self.requests.append(request)
        return ExecutionResult(
            status="success",
            exit_status=0,
            stdout="2\n",
            provider=self.name,
            run_id="code-contract-run",
        )


@pytest.fixture(autouse=True)
def _restore_providers():
    teaching = registry.teaching_data_provider()
    code = registry.code_execution_provider()
    yield
    registry.configure_teaching_data_provider(teaching)
    registry.configure_code_execution(code)


def test_builtin_manifest_matches_the_16_tool_contract_matrix():
    assert set(EXPECTED_CONTRACTS) == set(registry.tool_names())
    assert len(EXPECTED_CONTRACTS) == 16
    for name, expected in EXPECTED_CONTRACTS.items():
        spec = registry.get_spec(name)
        assert spec.capabilities == frozenset({expected.capability})
        assert spec.effect.value == expected.effect
        assert spec.schema["name"] == name

    assert registry.READ_ONLY_TEACHING_TOOLS.isdisjoint(
        registry.TEACHING_COMMAND_TOOLS
    )
    assert (
        registry.READ_ONLY_TEACHING_TOOLS
        | registry.TEACHING_COMMAND_TOOLS
        | {"run_code"}
    ) == set(EXPECTED_CONTRACTS)


def test_all_16_tools_cross_one_executor_result_boundary(tmp_path):
    path = generate.build(seed=42, out_path=tmp_path / "matrix.db")
    connection = db.connect(path)
    row = connection.execute(
        """SELECT e.id AS exam_id, e.class_id, er.student_id
           FROM exams e
           JOIN exam_records er ON er.exam_id=e.id
           WHERE e.course_id=1
           ORDER BY e.id, er.student_id LIMIT 1"""
    ).fetchone()
    provider = _RecordingTeachingProvider(
        SyntheticProvider(lambda: db.connect(path))
    )
    code_provider = _CodeProvider()
    registry.configure_teaching_data_provider(provider)
    registry.configure_code_execution(code_provider)
    store = StateStore(tmp_path / "state.db")
    executor = PolicyToolExecutor(
        registry,
        policy=ExecutionPolicy(
            require_write_approval=True,
            require_code_execution_approval=True,
            allow_local_code_execution=True,
        ),
        approval_handler=lambda request: True,
        state_store=store,
    )
    context = RunContext.create(
        session_id="matrix-session",
        actor_id="matrix-teacher",
        tenant_id="matrix-school",
        role="admin",
        course_ids={1},
        max_tool_calls=24,
    )
    arguments = {
        "query_student_scores": {"exam_id": row["exam_id"]},
        "list_exams": {"course_id": 1},
        "get_class_roster": {"class_id": row["class_id"]},
        "search_questions": {"course_id": 1, "page_size": 1},
        "get_learning_progress": {"student_id": row["student_id"], "course_id": 1},
        "query_knowledge_graph": {"course_id": 1, "operation": "find"},
        "recommend_study_path": {"student_id": row["student_id"], "course_id": 1},
        "analyze_class_errors": {"exam_id": row["exam_id"], "top": 1},
        "diagnose_weak_points": {"student_id": row["student_id"], "course_id": 1},
        "get_score_distribution": {"exam_id": row["exam_id"]},
        "create_exam": {"exam_name": "契约矩阵", "class_id": 3, "course_id": 1},
        "generate_paper": {"question_bank_id": 1, "total_questions": 1},
        "batch_grade": {"exam_id": row["exam_id"], "regrade": True},
        "assign_homework": {
            "title": "契约矩阵作业",
            "course_id": 1,
            "class_ids": [3],
            "end_time": "2026-09-01T20:00:00+08:00",
        },
        "generate_questions": {"course_id": 1, "count": 1},
        "run_code": {"source_code": "print(1 + 1)", "expected_output": "2"},
    }

    outcomes = {}
    for name in EXPECTED_CONTRACTS:
        outcomes[name] = executor.execute(
            name,
            arguments[name],
            context,
            conn=connection,
            tool_call_id=f"call-{name}",
            caller_idempotency_key=f"matrix-{name}",
        )

    assert all(isinstance(outcome, ToolResult) for outcome in outcomes.values())
    assert all(outcome.ok for outcome in outcomes.values()), {
        name: outcome.error for name, outcome in outcomes.items() if not outcome.ok
    }
    assert {outcome.meta["tool"] for outcome in outcomes.values()} == set(
        EXPECTED_CONTRACTS
    )
    assert len(provider.queries) == len(registry.READ_ONLY_TEACHING_TOOLS)
    assert len(provider.commands) == len(registry.TEACHING_COMMAND_TOOLS)
    assert code_provider.requests and len(code_provider.requests) == 1
    assert all(
        command.kind.value in registry.TEACHING_COMMAND_TOOLS
        for command in provider.commands
    )
    assert connection.execute("SELECT COUNT(*) FROM tool_operations").fetchone()[0] == 3
    assert connection.execute("SELECT COUNT(*) FROM tool_outbox").fetchone()[0] == 3
    connection.close()
