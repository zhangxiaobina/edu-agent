"""R3.2 canonical teaching-data provider contract tests.

The in-memory fake below proves only the public contract.  It is deliberately
not named or presented as a TeachingPlatformProvider and has no production
transport or storage behavior.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from edu_agent.data import db, generate
from edu_agent.teaching import (
    ExamStatus,
    PageRequest,
    SyntheticProvider,
    TeachingDataProvider,
    TeachingProviderErrorKind,
    TeachingQuery,
    TeachingQueryKind,
    TeachingResult,
    TeachingScope,
)
from edu_agent.tools import registry


@dataclass(frozen=True)
class _ContractIds:
    exam_id: int
    denied_exam_id: int
    class_id: int
    student_id: int
    course_id: int = 1
    denied_course_id: int = 2


@dataclass(frozen=True)
class _ProviderBundle:
    provider: TeachingDataProvider
    ids: _ContractIds


class _ContractFakeTeachingProvider(TeachingDataProvider):
    """Pure deterministic adapter used only to exercise the shared contract."""

    ids = _ContractIds(exam_id=101, denied_exam_id=201, class_id=11, student_id=7)

    def execute(self, query: TeachingQuery, *, connection=None) -> TeachingResult:
        filters = query.filters
        course_id = filters.get("course_id")
        if course_id is not None and not query.scope.allows_course(course_id):
            return TeachingResult.failure(
                TeachingProviderErrorKind.SCOPE_DENIED,
                f"当前身份无权访问课程 {course_id}",
                details={"course_id": course_id},
            )
        exam_id = filters.get("exam_id")
        if exam_id == self.ids.denied_exam_id and not query.scope.allows_course(2):
            return TeachingResult.failure(
                TeachingProviderErrorKind.SCOPE_DENIED,
                "当前身份无权访问课程 2",
                details={"course_id": 2},
            )
        handlers = {
            TeachingQueryKind.EXAMS: self._exams,
            TeachingQueryKind.SCORE_RECORDS: self._scores,
            TeachingQueryKind.CLASS_ROSTER: self._roster,
            TeachingQueryKind.QUESTIONS: self._questions,
            TeachingQueryKind.LEARNING_PROGRESS: self._progress,
            TeachingQueryKind.CLASS_ERRORS: self._errors,
            TeachingQueryKind.WEAK_POINTS: self._weak_points,
            TeachingQueryKind.SCORE_DISTRIBUTION: self._distribution,
            TeachingQueryKind.KNOWLEDGE_GRAPH: self._graph,
            TeachingQueryKind.STUDY_PATH: self._study_path,
        }
        return handlers[query.kind](query)

    def _exams(self, query: TeachingQuery) -> TeachingResult:
        rows = [
            {
                "id": 102,
                "exam_name": "函数测验",
                "exam_code": "EX0102",
                "class_id": self.ids.class_id,
                "class_name": "合成一班",
                "course_id": 1,
                "course_name": "Python",
                "start_time": "2026-02-02 08:00:00",
                "end_time": "2026-02-02 09:00:00",
                "duration": 60,
                "total_score": 100,
                "pass_score": 60,
                "question_count": 2,
                "status": 1,
                "submit_count": 1,
                "avg_score": 80.0,
                "pass_rate": 100.0,
                "status_text": "进行中",
            },
            {
                "id": self.ids.exam_id,
                "exam_name": "递归测验",
                "exam_code": "EX0101",
                "class_id": self.ids.class_id,
                "class_name": "合成一班",
                "course_id": 1,
                "course_name": "Python",
                "start_time": "2026-02-01 08:00:00",
                "end_time": "2026-02-01 09:00:00",
                "duration": 60,
                "total_score": 100,
                "pass_score": 60,
                "question_count": 2,
                "status": 2,
                "submit_count": 2,
                "avg_score": 65.0,
                "pass_rate": 50.0,
                "status_text": "已结束",
            },
        ]
        requested_status = query.filters.get("status")
        if requested_status is not None:
            rows = [row for row in rows if row["status"] == requested_status]
        search = query.filters.get("search")
        if search:
            rows = [
                row
                for row in rows
                if search in row["exam_name"] or search in row["exam_code"]
            ]
        page = query.page or PageRequest(1, 50)
        offset = (page.number - 1) * page.size
        return TeachingResult.success(
            {
                "total": len(rows),
                "page": page.number,
                "page_size": page.size,
                "exams": rows[offset : offset + page.size],
            }
        )

    def _scores(self, query: TeachingQuery) -> TeachingResult:
        page = query.page or PageRequest(1, 50)
        records = [
            {
                "student_id": self.ids.student_id,
                "student_name": "合成学生",
                "student_no": "S0007",
                "exam_id": self.ids.exam_id,
                "exam_name": "递归测验",
                "score": 50.0,
                "total_score": 100.0,
                "score_rate": 50.0,
                "correct_count": 1,
                "answer_count": 2,
                "passed": 0,
                "rank": 2,
                "status": 3,
                "submit_time": "2026-02-01 08:30:00",
                "duration": 30,
            }
        ]
        return TeachingResult.success(
            {
                "total": len(records),
                "page": page.number,
                "page_size": page.size,
                "records": records,
            }
        )

    def _roster(self, query: TeachingQuery) -> TeachingResult:
        class_id = query.filters["class_id"]
        if class_id != self.ids.class_id:
            return TeachingResult.failure(
                TeachingProviderErrorKind.NOT_FOUND,
                f"班级 {class_id} 不存在",
            )
        page = query.page or PageRequest(1, 100)
        return TeachingResult.success(
            {
                "class_id": class_id,
                "class_name": "合成一班",
                "total": 1,
                "page": page.number,
                "page_size": page.size,
                "students": [
                    {
                        "student_id": self.ids.student_id,
                        "student_username": "S0007",
                        "student_name": "合成学生",
                        "phone": None,
                        "email": None,
                        "join_time": "2026-01-01 00:00:00",
                        "status": 1,
                        "exam_count": 1,
                        "avg_score": 50.0,
                        "homework_count": 0,
                    }
                ],
            }
        )

    def _questions(self, query: TeachingQuery) -> TeachingResult:
        page = query.page or PageRequest(1, 20)
        questions = [] if query.filters.get("keyword") == "__no_match__" else [
            {
                "id": 301,
                "title": "递归终止条件",
                "question_type": "single",
                "difficulty": "medium",
                "content": "选择正确的终止条件",
                "options": ["n == 0", "n > 0"],
                "score": 5.0,
                "source": "manual",
                "status": 1,
                "language": None,
                "usage_count": 1,
                "course_id": 1,
                "knowledge_points": ["递归"],
            }
        ]
        return TeachingResult.success(
            {
                "total": len(questions),
                "page": page.number,
                "page_size": page.size,
                "questions": questions,
            }
        )

    def _progress(self, query: TeachingQuery) -> TeachingResult:
        return TeachingResult.success(
            {
                "student_id": query.filters["student_id"],
                "courses": [
                    {
                        "course_id": 1,
                        "course_name": "Python",
                        "total_courseware": 1,
                        "completed_courseware": 1,
                        "coursewares": [
                            {
                                "course_id": 1,
                                "course_name": "Python",
                                "courseware_id": 401,
                                "courseware_name": "递归",
                                "progress": 100.0,
                                "completed": 1,
                                "watched_time": 600,
                                "study_status": "completed",
                                "last_access_time": "2026-02-01 10:00:00",
                            }
                        ],
                        "overall_progress": 100.0,
                    }
                ],
            }
        )

    def _errors(self, query: TeachingQuery) -> TeachingResult:
        if query.filters.get("exam_id") is None and query.filters.get("class_id") is None:
            return TeachingResult.failure(
                TeachingProviderErrorKind.INVALID_QUERY,
                "需提供 exam_id 或 class_id 之一",
            )
        return TeachingResult.success(
            {
                "scope": {
                    "exam_id": query.filters.get("exam_id"),
                    "class_id": query.filters.get("class_id"),
                },
                "top": query.filters.get("top", 10),
                "error_questions": [
                    {
                        "question_id": 301,
                        "title": "递归终止条件",
                        "difficulty": "medium",
                        "full_score": 5.0,
                        "error_count": 2,
                        "total_count": 2,
                        "avg_score": 1.0,
                        "error_rate": 1.0,
                        "knowledge_point_name": "递归",
                        "knowledge_point_uid": "kp-recursion",
                    }
                ],
            }
        )

    def _weak_points(self, query: TeachingQuery) -> TeachingResult:
        student_id = query.filters.get("student_id")
        class_id = query.filters.get("class_id")
        if student_id is None and class_id is None:
            return TeachingResult.failure(
                TeachingProviderErrorKind.INVALID_QUERY,
                "需提供 student_id 或 class_id 之一",
            )
        threshold = query.filters.get("threshold", 0.6)
        return TeachingResult.success(
            {
                "scope": "student" if student_id is not None else "class",
                **(
                    {"student_id": student_id}
                    if student_id is not None
                    else {"class_id": class_id}
                ),
                "threshold": threshold,
                "weak_points": [
                    {
                        "node_uid": "kp-recursion",
                        "knowledge_point": "递归",
                        "type": "concept",
                        "course_id": 1,
                        "mastery_rate": 0.4,
                        "correct_count": 2,
                        "total_questions": 5,
                    }
                ],
            }
        )

    def _distribution(self, query: TeachingQuery) -> TeachingResult:
        exam_id = query.filters["exam_id"]
        if exam_id != self.ids.exam_id:
            return TeachingResult.failure(
                TeachingProviderErrorKind.NOT_FOUND,
                f"考试 {exam_id} 不存在",
            )
        return TeachingResult.success(
            {
                "exam_id": exam_id,
                "exam_name": "递归测验",
                "total_score": 100,
                "pass_score": 60,
                "total_students": 2,
                "average_score": 65.0,
                "median_score": 65.0,
                "max_score": 80.0,
                "min_score": 50.0,
                "std_dev": 15.0,
                "pass_count": 1,
                "pass_rate": 50.0,
                "distribution": [
                    {"grade": grade, "range_pct": span, "student_count": count,
                     "percentage": count * 50.0}
                    for grade, span, count in (
                        ("A", "90-100", 0), ("B", "80-90", 1), ("C", "70-80", 0),
                        ("D", "60-70", 0), ("F", "0-60", 1),
                    )
                ],
            }
        )

    def _graph(self, query: TeachingQuery) -> TeachingResult:
        operation = query.filters["operation"]
        node = {
            "node_uid": "kp-recursion",
            "name": "递归",
            "type": "concept",
            "difficulty": 3,
            "importance": 0.9,
            "course_id": 1,
        }
        if operation == "find":
            return TeachingResult.success(
                {"operation": "find", "course_id": 1, "count": 1, "nodes": [node]}
            )
        return TeachingResult.success(
            {"operation": operation, "node": node, "count": 0, "prerequisites": []}
        )

    def _study_path(self, query: TeachingQuery) -> TeachingResult:
        return TeachingResult.success(
            {
                "student_id": query.filters["student_id"],
                "course_id": 1,
                "target": {
                    "node_uid": "kp-recursion",
                    "name": "递归",
                    "type": "concept",
                    "mastery_rate": 0.4,
                },
                "weak_point_count": 1,
                "path": [
                    {
                        "node_uid": "kp-recursion",
                        "name": "递归",
                        "type": "concept",
                        "difficulty": 3,
                        "mastery_rate": 0.4,
                        "practice_questions": [
                            {
                                "id": 301,
                                "title": "递归终止条件",
                                "difficulty": "medium",
                                "question_type": "single",
                            }
                        ],
                    }
                ],
                "cypher_hint": "MATCH canonical learning path",
            }
        )


class _TrackingConnection(sqlite3.Connection):
    was_closed = False

    def close(self):
        self.was_closed = True
        return super().close()


class TeachingProviderContract:
    """Shared read-only behavioral contract for every teaching-data adapter."""

    @pytest.fixture
    def provider_bundle(self, tmp_path) -> _ProviderBundle:
        raise NotImplementedError

    def test_all_read_only_slices_return_canonical_results(self, provider_bundle):
        provider, ids = provider_bundle.provider, provider_bundle.ids
        queries = (
            TeachingQuery(
                TeachingQueryKind.EXAMS,
                {"course_id": ids.course_id},
                page=PageRequest(1, 5),
            ),
            TeachingQuery(
                TeachingQueryKind.SCORE_RECORDS,
                {"exam_id": ids.exam_id, "only_failed": False},
                page=PageRequest(1, 5),
            ),
            TeachingQuery(
                TeachingQueryKind.CLASS_ROSTER,
                {"class_id": ids.class_id},
                page=PageRequest(1, 5),
            ),
            TeachingQuery(
                TeachingQueryKind.QUESTIONS,
                {"course_id": ids.course_id, "status": 1},
                page=PageRequest(1, 5),
            ),
            TeachingQuery(
                TeachingQueryKind.LEARNING_PROGRESS,
                {"student_id": ids.student_id, "course_id": ids.course_id},
            ),
            TeachingQuery(
                TeachingQueryKind.CLASS_ERRORS,
                {"exam_id": ids.exam_id, "top": 3},
            ),
            TeachingQuery(
                TeachingQueryKind.WEAK_POINTS,
                {"student_id": ids.student_id, "course_id": ids.course_id},
            ),
            TeachingQuery(
                TeachingQueryKind.SCORE_DISTRIBUTION,
                {"exam_id": ids.exam_id},
            ),
            TeachingQuery(
                TeachingQueryKind.KNOWLEDGE_GRAPH,
                {"course_id": ids.course_id, "operation": "find"},
            ),
            TeachingQuery(
                TeachingQueryKind.STUDY_PATH,
                {"student_id": ids.student_id, "course_id": ids.course_id},
            ),
        )
        for query in queries:
            result = provider.execute(query)
            assert result.ok, (query.kind, result.error)
            assert provider.execute(query).to_tool_result() == result.to_tool_result()
            json.dumps(result.data, ensure_ascii=False)
            json.dumps(result.to_tool_result(), ensure_ascii=False)

    def test_pagination_empty_results_status_mapping_and_order(self, provider_bundle):
        provider, ids = provider_bundle.provider, provider_bundle.ids
        base = {"course_id": ids.course_id}
        first_query = TeachingQuery(
            TeachingQueryKind.EXAMS, base, page=PageRequest(number=1, size=1)
        )
        second_query = TeachingQuery(
            TeachingQueryKind.EXAMS, base, page=PageRequest(number=2, size=1)
        )
        first = provider.execute(first_query)
        second = provider.execute(second_query)
        assert first.ok and second.ok
        assert first.data["page"] == 1 and second.data["page"] == 2
        assert first.data["total"] >= 2
        assert first.data["exams"][0]["id"] != second.data["exams"][0]["id"]
        for page in (first, second):
            exam = page.data["exams"][0]
            assert exam["status_text"] == ExamStatus.label_for(exam["status"])
        assert provider.execute(first_query).to_tool_result() == first.to_tool_result()

        empty = provider.execute(
            TeachingQuery(
                TeachingQueryKind.QUESTIONS,
                {"course_id": ids.course_id, "keyword": "__no_match__", "status": 1},
                page=PageRequest(1, 2),
            )
        )
        assert empty.ok and empty.data["total"] == 0 and empty.data["questions"] == []

    def test_scope_rejection_and_error_classification(self, provider_bundle):
        provider, ids = provider_bundle.provider, provider_bundle.ids
        restricted = TeachingScope.restricted(
            {ids.course_id}, tenant_id="tenant-contract", role="teacher"
        )
        denied = provider.execute(
            TeachingQuery(
                TeachingQueryKind.EXAMS,
                {"course_id": ids.denied_course_id},
                scope=restricted,
            )
        )
        assert denied.error.kind is TeachingProviderErrorKind.SCOPE_DENIED

        indirect_denied = provider.execute(
            TeachingQuery(
                TeachingQueryKind.SCORE_RECORDS,
                {"exam_id": ids.denied_exam_id},
                scope=restricted,
            )
        )
        assert indirect_denied.error.kind is TeachingProviderErrorKind.SCOPE_DENIED

        invalid = provider.execute(
            TeachingQuery(TeachingQueryKind.CLASS_ERRORS, {}, scope=restricted)
        )
        assert invalid.error.kind is TeachingProviderErrorKind.INVALID_QUERY

        missing = provider.execute(
            TeachingQuery(
                TeachingQueryKind.CLASS_ROSTER,
                {"class_id": 999_999},
                scope=restricted,
            )
        )
        assert missing.error.kind is TeachingProviderErrorKind.NOT_FOUND


class TestSyntheticProviderContract(TeachingProviderContract):
    @pytest.fixture
    def provider_bundle(self, tmp_path) -> _ProviderBundle:
        path = generate.build(seed=42, out_path=tmp_path / "teaching.db")
        with db.connect(path) as connection:
            row = connection.execute(
                """SELECT e.id AS exam_id, e.class_id, er.student_id
                   FROM exams e JOIN exam_records er ON er.exam_id=e.id
                   WHERE e.course_id=1 ORDER BY e.id, er.student_id LIMIT 1"""
            ).fetchone()
            denied_exam_id = connection.execute(
                "SELECT id FROM exams WHERE course_id=2 ORDER BY id LIMIT 1"
            ).fetchone()["id"]
        ids = _ContractIds(
            exam_id=row["exam_id"],
            denied_exam_id=denied_exam_id,
            class_id=row["class_id"],
            student_id=row["student_id"],
        )
        return _ProviderBundle(SyntheticProvider(lambda: db.connect(path)), ids)


class TestContractFakeTeachingProvider(TeachingProviderContract):
    @pytest.fixture
    def provider_bundle(self, tmp_path) -> _ProviderBundle:
        provider = _ContractFakeTeachingProvider()
        return _ProviderBundle(provider, provider.ids)


def test_synthetic_provider_owns_one_connection_per_worker_call(tmp_path):
    path = generate.build(seed=42, out_path=tmp_path / "connections.db")
    opened: list[tuple[int, sqlite3.Connection]] = []
    lock = threading.Lock()

    def factory():
        connection = sqlite3.connect(path, timeout=10, factory=_TrackingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        with lock:
            opened.append((threading.get_ident(), connection))
        return connection

    provider = SyntheticProvider(factory)
    query = TeachingQuery(
        TeachingQueryKind.EXAMS, {"course_id": 1}, page=PageRequest(1, 1)
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: provider.execute(query), range(4)))
    assert all(result.ok for result in results)
    assert len(opened) == 4
    assert len({id(connection) for _, connection in opened}) == 4
    for _, connection in opened:
        assert connection.was_closed is True


def test_synthetic_provider_uses_but_does_not_close_controlled_connection(tmp_path):
    path = generate.build(seed=42, out_path=tmp_path / "controlled.db")
    factory_calls = 0

    def factory():
        nonlocal factory_calls
        factory_calls += 1
        return db.connect(path)

    provider = SyntheticProvider(factory)
    connection = db.connect(path)
    try:
        result = provider.execute(
            TeachingQuery(TeachingQueryKind.EXAMS, {"course_id": 1}),
            connection=connection,
        )
        assert result.ok and factory_calls == 0
        assert connection.execute("SELECT 1").fetchone()[0] == 1
    finally:
        connection.close()


def test_synthetic_provider_hides_sqlite_failure_details():
    def unavailable():
        raise sqlite3.OperationalError("no such table: private_students")

    result = SyntheticProvider(unavailable).execute(
        TeachingQuery(TeachingQueryKind.EXAMS, {})
    )
    assert result.error.kind is TeachingProviderErrorKind.UNAVAILABLE
    rendered = json.dumps(
        {
            "message": result.error.message,
            "details": dict(result.error.details),
        },
        ensure_ascii=False,
    )
    assert "private_students" not in rendered
    assert result.error.retryable is True


def test_registry_provider_swap_preserves_manifest_and_passes_context_scope():
    original = registry.teaching_data_provider()
    fake = _ContractFakeTeachingProvider()
    manifest_before = registry.build_tool_manifest(role="teacher")
    try:
        registry.configure_teaching_data_provider(fake)
        result = registry.dispatch("list_exams", {"course_id": 1, "page_size": 1})
        assert result["exams"][0]["id"] == 102

        context = SimpleNamespace(
            tenant_id="tenant-contract",
            actor_id="teacher-contract",
            role="teacher",
            course_ids=frozenset({1}),
        )
        denied = registry.dispatch_with_context(
            "list_exams", {"course_id": 2}, context
        )
        assert denied == {"error": "当前身份无权访问课程 2"}
        assert registry.build_tool_manifest(role="teacher").manifest_hash == (
            manifest_before.manifest_hash
        )
    finally:
        registry.configure_teaching_data_provider(original)
