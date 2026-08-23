"""Registry-backed SQLite implementation of the teaching-data contract."""

from __future__ import annotations

import json
import sqlite3
import statistics
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from ..data.kg import Edge, KnowledgeGraph
from .contracts import (
    ExamStatus,
    PageRequest,
    TeachingDataProvider,
    TeachingProviderErrorKind,
    TeachingQuery,
    TeachingQueryKind,
    TeachingResult,
    TeachingScope,
)


ConnectionFactory = Callable[[], sqlite3.Connection]


_NODE_FIELDS = ("node_uid", "name", "type", "difficulty", "importance", "course_id")


class SyntheticProvider(TeachingDataProvider):
    """Canonical provider for the reproducible public teaching dataset.

    The factory is invoked once per ordinary call.  A caller may explicitly
    supply its own controlled connection only when an existing transaction
    boundary needs the teaching read to join that transaction.
    """

    def __init__(self, connection_factory: ConnectionFactory):
        if not callable(connection_factory):
            raise TypeError("connection_factory 必须可调用")
        self._connection_factory = connection_factory
        self._handlers = {
            TeachingQueryKind.SCORE_RECORDS: self._score_records,
            TeachingQueryKind.EXAMS: self._exams,
            TeachingQueryKind.CLASS_ROSTER: self._class_roster,
            TeachingQueryKind.QUESTIONS: self._questions,
            TeachingQueryKind.LEARNING_PROGRESS: self._learning_progress,
            TeachingQueryKind.CLASS_ERRORS: self._class_errors,
            TeachingQueryKind.WEAK_POINTS: self._weak_points,
            TeachingQueryKind.SCORE_DISTRIBUTION: self._score_distribution,
            TeachingQueryKind.KNOWLEDGE_GRAPH: self._knowledge_graph,
            TeachingQueryKind.STUDY_PATH: self._study_path,
        }

    def execute(self, query: TeachingQuery, *, connection: object | None = None) -> TeachingResult:
        if not isinstance(query, TeachingQuery):
            return TeachingResult.failure(
                TeachingProviderErrorKind.INVALID_QUERY,
                "教学数据查询契约无效",
            )
        handler = self._handlers.get(query.kind)
        if handler is None:
            return TeachingResult.failure(
                TeachingProviderErrorKind.INVALID_QUERY,
                f"不支持的教学数据查询：{query.kind.value}",
            )
        if connection is not None and not isinstance(connection, sqlite3.Connection):
            return TeachingResult.failure(
                TeachingProviderErrorKind.INVALID_QUERY,
                "受控连接类型与 SyntheticProvider 不兼容",
            )
        try:
            with self._connection(connection) as active:
                return handler(active, query)
        except (KeyError, TypeError) as error:
            return TeachingResult.failure(
                TeachingProviderErrorKind.INVALID_QUERY,
                "教学数据查询参数无效",
                details={"cause": type(error).__name__},
            )
        except (TimeoutError, sqlite3.OperationalError) as error:
            return TeachingResult.failure(
                TeachingProviderErrorKind.UNAVAILABLE,
                "合成教学数据暂不可用",
                retryable=True,
                details={"cause": type(error).__name__},
            )
        except sqlite3.DatabaseError as error:
            return TeachingResult.failure(
                TeachingProviderErrorKind.INTERNAL,
                "合成教学数据读取失败",
                details={"cause": type(error).__name__},
            )
        except Exception as error:  # noqa: BLE001 - canonical boundary must not leak adapters
            return TeachingResult.failure(
                TeachingProviderErrorKind.INTERNAL,
                "教学数据 Provider 执行失败",
                details={"cause": type(error).__name__},
            )

    @contextmanager
    def _connection(self, controlled: object | None) -> Iterator[sqlite3.Connection]:
        if controlled is not None:
            yield controlled
            return
        connection = self._connection_factory()
        if not isinstance(connection, sqlite3.Connection):
            close = getattr(connection, "close", None)
            if callable(close):
                close()
            raise TypeError("connection_factory 未返回 sqlite3.Connection")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _page(query: TeachingQuery, *, default_size: int) -> tuple[int, int, int]:
        number = query.page.number if query.page is not None else 1
        size = query.page.size if query.page is not None else default_size
        return number, size, (number - 1) * size

    @staticmethod
    def _scope_clause(scope: TeachingScope, column: str) -> tuple[str | None, list[int]]:
        if not scope.enforce_course_scope:
            return None, []
        course_ids = sorted(scope.course_ids)
        if not course_ids:
            return "1=0", []
        return f"{column} IN ({','.join('?' for _ in course_ids)})", course_ids

    @staticmethod
    def _scope_denied(course_id: int) -> TeachingResult:
        return TeachingResult.failure(
            TeachingProviderErrorKind.SCOPE_DENIED,
            f"当前身份无权访问课程 {course_id}",
            details={"course_id": int(course_id)},
        )

    @classmethod
    def _require_course(cls, query: TeachingQuery, course_id: int | None) -> TeachingResult | None:
        if course_id is not None and not query.scope.allows_course(int(course_id)):
            return cls._scope_denied(int(course_id))
        return None

    @classmethod
    def _guard_entity_course(
        cls,
        connection: sqlite3.Connection,
        query: TeachingQuery,
        sql: str,
        parameters: tuple[Any, ...],
    ) -> TeachingResult | None:
        if not query.scope.enforce_course_scope:
            return None
        row = connection.execute(sql, parameters).fetchone()
        if row is not None and not query.scope.allows_course(int(row[0])):
            return cls._scope_denied(int(row[0]))
        return None

    def _score_records(self, connection: sqlite3.Connection, query: TeachingQuery) -> TeachingResult:
        filters = query.filters
        exam_id = filters.get("exam_id")
        student_id = filters.get("student_id")
        class_id = filters.get("class_id")
        only_failed = bool(filters.get("only_failed", False))
        if exam_id is not None:
            denied = self._guard_entity_course(
                connection,
                query,
                "SELECT course_id FROM exams WHERE id=?",
                (exam_id,),
            )
            if denied is not None:
                return denied
        where: list[str] = []
        parameters: list[Any] = []
        if exam_id is not None:
            where.append("er.exam_id=?")
            parameters.append(exam_id)
        if student_id is not None:
            where.append("er.student_id=?")
            parameters.append(student_id)
        if class_id is not None:
            where.append("e.class_id=?")
            parameters.append(class_id)
        if only_failed:
            where.append("er.passed=0")
        scope_clause, scope_parameters = self._scope_clause(query.scope, "e.course_id")
        if scope_clause:
            where.append(scope_clause)
            parameters.extend(scope_parameters)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        page, page_size, offset = self._page(query, default_size=50)
        rows = connection.execute(
            f"""SELECT er.student_id, s.name AS student_name, s.username AS student_no,
                       er.exam_id, e.exam_name, er.score, er.total_score,
                       ROUND(er.score * 100.0 / NULLIF(er.total_score,0), 1) AS score_rate,
                       er.correct_count, er.answer_count, er.passed, er.rank, er.status,
                       er.submit_time, er.duration
                FROM exam_records er
                JOIN students s ON s.id = er.student_id
                JOIN exams e ON e.id = er.exam_id
                {clause}
                ORDER BY er.exam_id, er.rank, er.student_id
                LIMIT ? OFFSET ?""",
            parameters + [page_size, offset],
        ).fetchall()
        total = connection.execute(
            f"SELECT COUNT(*) FROM exam_records er JOIN exams e ON e.id=er.exam_id{clause}",
            parameters,
        ).fetchone()[0]
        return TeachingResult.success(
            {
                "total": total,
                "page": page,
                "page_size": page_size,
                "records": [dict(row) for row in rows],
            }
        )

    def _exams(self, connection: sqlite3.Connection, query: TeachingQuery) -> TeachingResult:
        filters = query.filters
        class_id = filters.get("class_id")
        course_id = filters.get("course_id")
        status = filters.get("status")
        search = filters.get("search")
        denied = self._require_course(query, course_id)
        if denied is not None:
            return denied
        where: list[str] = []
        parameters: list[Any] = []
        if class_id is not None:
            where.append("e.class_id=?")
            parameters.append(class_id)
        if course_id is not None:
            where.append("e.course_id=?")
            parameters.append(course_id)
        if status is not None:
            where.append("e.status=?")
            parameters.append(status)
        if search:
            where.append("(e.exam_name LIKE ? OR e.exam_code LIKE ?)")
            parameters.extend([f"%{search}%", f"%{search}%"])
        scope_clause, scope_parameters = self._scope_clause(query.scope, "e.course_id")
        if scope_clause:
            where.append(scope_clause)
            parameters.extend(scope_parameters)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        page, page_size, offset = self._page(query, default_size=50)
        rows = connection.execute(
            f"""SELECT e.id, e.exam_name, e.exam_code, e.class_id, cl.name AS class_name,
                       e.course_id, co.name AS course_name, e.start_time, e.end_time,
                       e.duration, e.total_score, e.pass_score, e.question_count, e.status,
                       (SELECT COUNT(*) FROM exam_records r WHERE r.exam_id=e.id) AS submit_count,
                       (SELECT ROUND(AVG(r.score),1) FROM exam_records r WHERE r.exam_id=e.id)
                           AS avg_score,
                       (SELECT ROUND(SUM(r.passed)*100.0/COUNT(*),1)
                          FROM exam_records r WHERE r.exam_id=e.id) AS pass_rate
                FROM exams e
                JOIN classes cl ON cl.id = e.class_id
                JOIN courses co ON co.id = e.course_id
                {clause}
                ORDER BY e.start_time DESC, e.id
                LIMIT ? OFFSET ?""",
            parameters + [page_size, offset],
        ).fetchall()
        exams = [dict(row) for row in rows]
        for exam in exams:
            exam["status_text"] = ExamStatus.label_for(exam["status"])
        total = connection.execute(
            f"SELECT COUNT(*) FROM exams e{clause}", parameters
        ).fetchone()[0]
        return TeachingResult.success(
            {"total": total, "page": page, "page_size": page_size, "exams": exams}
        )

    def _class_roster(self, connection: sqlite3.Connection, query: TeachingQuery) -> TeachingResult:
        filters = query.filters
        class_id = filters["class_id"]
        search = filters.get("search")
        sort_by = filters.get("sort_by")
        sort_order = filters.get("sort_order", "asc")
        class_row = connection.execute(
            "SELECT id,name FROM classes WHERE id=?", (class_id,)
        ).fetchone()
        if class_row is None:
            return TeachingResult.failure(
                TeachingProviderErrorKind.NOT_FOUND,
                f"班级 {class_id} 不存在",
                details={"class_id": class_id},
            )
        where = ["cs.class_id=?"]
        parameters: list[Any] = [class_id]
        if search:
            where.append("(s.name LIKE ? OR s.username LIKE ?)")
            parameters.extend([f"%{search}%", f"%{search}%"])
        sort_column = {
            "student_name": "s.name",
            "student_username": "s.username",
            "join_time": "cs.join_time",
            "avg_score": "avg_score",
        }.get(sort_by, "s.username")
        order = "DESC" if str(sort_order).lower() == "desc" else "ASC"
        page, page_size, offset = self._page(query, default_size=100)
        rows = connection.execute(
            f"""SELECT s.id AS student_id, s.username AS student_username,
                       s.name AS student_name, s.phone, s.email, cs.join_time, cs.status,
                       (SELECT COUNT(*) FROM exam_records r JOIN exams e ON e.id=r.exam_id
                            WHERE r.student_id=s.id AND e.class_id=cs.class_id) AS exam_count,
                       (SELECT ROUND(AVG(r.score),1) FROM exam_records r
                            JOIN exams e ON e.id=r.exam_id
                            WHERE r.student_id=s.id AND e.class_id=cs.class_id) AS avg_score,
                       (SELECT COUNT(*) FROM homework_classes hc
                            WHERE hc.class_id=cs.class_id) AS homework_count
                FROM class_students cs
                JOIN students s ON s.id = cs.student_id
                WHERE {' AND '.join(where)}
                ORDER BY {sort_column} {order}, s.id {order}
                LIMIT ? OFFSET ?""",
            parameters + [page_size, offset],
        ).fetchall()
        total = connection.execute(
            "SELECT COUNT(*) FROM class_students cs JOIN students s ON s.id=cs.student_id "
            f"WHERE {' AND '.join(where)}",
            parameters,
        ).fetchone()[0]
        return TeachingResult.success(
            {
                "class_id": class_id,
                "class_name": class_row["name"],
                "total": total,
                "page": page,
                "page_size": page_size,
                "students": [dict(row) for row in rows],
            }
        )

    def _questions(self, connection: sqlite3.Connection, query: TeachingQuery) -> TeachingResult:
        filters = query.filters
        bank_id = filters.get("question_bank_id")
        course_id = filters.get("course_id")
        question_type = filters.get("question_type")
        difficulty = filters.get("difficulty")
        knowledge_point = filters.get("knowledge_point")
        keyword = filters.get("keyword")
        status = filters.get("status", 1)
        denied = self._require_course(query, course_id)
        if denied is not None:
            return denied
        if bank_id is not None:
            denied = self._guard_entity_course(
                connection,
                query,
                "SELECT course_id FROM question_banks WHERE id=?",
                (bank_id,),
            )
            if denied is not None:
                return denied
        join = ""
        join_parameters: list[Any] = []
        if bank_id is not None:
            join = (
                " JOIN question_bank_questions qbq"
                " ON qbq.question_id=q.id AND qbq.question_bank_id=?"
            )
            join_parameters.append(bank_id)
        where = ["q.status=?"]
        where_parameters: list[Any] = [status]
        if course_id is not None:
            where.append("q.course_id=?")
            where_parameters.append(course_id)
        if question_type:
            where.append("q.question_type=?")
            where_parameters.append(question_type)
        if difficulty:
            where.append("q.difficulty=?")
            where_parameters.append(difficulty)
        if keyword:
            where.append("(q.title LIKE ? OR q.content LIKE ?)")
            where_parameters.extend([f"%{keyword}%", f"%{keyword}%"])
        if knowledge_point:
            resolved = self._resolve_knowledge_point(
                connection, str(knowledge_point), course_id=course_id
            )
            if resolved is None:
                return TeachingResult.success(
                    {
                        "total": 0,
                        "questions": [],
                        "note": f"未找到知识点：{knowledge_point}",
                    }
                )
            uid, resolved_course_id = resolved
            denied = self._require_course(query, resolved_course_id)
            if denied is not None:
                return denied
            where.append(
                "q.id IN (SELECT resource_id FROM kg_resource_link "
                "WHERE resource_type='question' AND node_uid=?)"
            )
            where_parameters.append(uid)
        scope_clause, scope_parameters = self._scope_clause(query.scope, "q.course_id")
        if scope_clause:
            where.append(scope_clause)
            where_parameters.extend(scope_parameters)
        clause = " WHERE " + " AND ".join(where)
        parameters = join_parameters + where_parameters
        page, page_size, offset = self._page(query, default_size=20)
        rows = connection.execute(
            f"""SELECT q.id, q.title, q.question_type, q.difficulty, q.content, q.options,
                       q.score, q.source, q.status, q.language, q.usage_count, q.course_id
                FROM questions q{join}{clause}
                ORDER BY q.id LIMIT ? OFFSET ?""",
            parameters + [page_size, offset],
        ).fetchall()
        questions = [dict(row) for row in rows]
        for question in questions:
            if question.get("options"):
                question["options"] = json.loads(question["options"])
            question["knowledge_points"] = self._question_knowledge_points(
                connection, question["id"]
            )
        total = connection.execute(
            f"SELECT COUNT(*) FROM questions q{join}{clause}", parameters
        ).fetchone()[0]
        return TeachingResult.success(
            {
                "total": total,
                "page": page,
                "page_size": page_size,
                "questions": questions,
            }
        )

    def _learning_progress(
        self, connection: sqlite3.Connection, query: TeachingQuery
    ) -> TeachingResult:
        filters = query.filters
        student_id = filters["student_id"]
        course_id = filters.get("course_id")
        denied = self._require_course(query, course_id)
        if denied is not None:
            return denied
        where = ["lp.student_id=?"]
        parameters: list[Any] = [student_id]
        if course_id is not None:
            where.append("lp.course_id=?")
            parameters.append(course_id)
        scope_clause, scope_parameters = self._scope_clause(query.scope, "lp.course_id")
        if scope_clause:
            where.append(scope_clause)
            parameters.extend(scope_parameters)
        rows = connection.execute(
            f"""SELECT lp.course_id, co.name AS course_name, lp.courseware_id,
                       cw.name AS courseware_name, lp.progress, lp.completed,
                       lp.watched_time, lp.study_status, lp.last_access_time
                FROM learning_progress lp
                JOIN courseware cw ON cw.id = lp.courseware_id
                JOIN courses co ON co.id = lp.course_id
                WHERE {' AND '.join(where)}
                ORDER BY lp.course_id, cw.sort_order, lp.courseware_id""",
            parameters,
        ).fetchall()
        courses: dict[int, dict] = {}
        for row in rows:
            item = dict(row)
            course = courses.setdefault(
                item["course_id"],
                {
                    "course_id": item["course_id"],
                    "course_name": item["course_name"],
                    "total_courseware": 0,
                    "completed_courseware": 0,
                    "_progress_sum": 0,
                    "coursewares": [],
                },
            )
            course["total_courseware"] += 1
            course["completed_courseware"] += item["completed"]
            course["_progress_sum"] += item["progress"]
            course["coursewares"].append(item)
        summaries = []
        for course in courses.values():
            total_progress = course.pop("_progress_sum")
            count = course["total_courseware"]
            course["overall_progress"] = round(total_progress / count, 1) if count else 0
            summaries.append(course)
        return TeachingResult.success({"student_id": student_id, "courses": summaries})

    def _class_errors(self, connection: sqlite3.Connection, query: TeachingQuery) -> TeachingResult:
        filters = query.filters
        exam_id = filters.get("exam_id")
        class_id = filters.get("class_id")
        top = filters.get("top", 10)
        if exam_id is None and class_id is None:
            return TeachingResult.failure(
                TeachingProviderErrorKind.INVALID_QUERY,
                "需提供 exam_id 或 class_id 之一",
            )
        if exam_id is not None:
            denied = self._guard_entity_course(
                connection,
                query,
                "SELECT course_id FROM exams WHERE id=?",
                (exam_id,),
            )
            if denied is not None:
                return denied
        needs_exam_join = class_id is not None or query.scope.enforce_course_scope
        join = " JOIN exams e ON e.id = ea.exam_id" if needs_exam_join else ""
        where: list[str] = []
        parameters: list[Any] = []
        if exam_id is not None:
            where.append("ea.exam_id=?")
            parameters.append(exam_id)
        if class_id is not None:
            where.append("e.class_id=?")
            parameters.append(class_id)
        scope_clause, scope_parameters = self._scope_clause(query.scope, "e.course_id")
        if scope_clause:
            where.append(scope_clause)
            parameters.extend(scope_parameters)
        clause = " WHERE " + " AND ".join(where)
        rows = connection.execute(
            f"""SELECT q.id AS question_id, q.title, q.difficulty, q.score AS full_score,
                       SUM(CASE WHEN ea.is_correct=0 THEN 1 ELSE 0 END) AS error_count,
                       COUNT(*) AS total_count, ROUND(AVG(ea.earned_score),2) AS avg_score
                FROM exam_answers ea
                JOIN questions q ON q.id = ea.question_id{join}
                {clause}
                GROUP BY q.id
                ORDER BY error_count DESC, q.id
                LIMIT ?""",
            parameters + [top],
        ).fetchall()
        items = [dict(row) for row in rows]
        for item in items:
            item["error_rate"] = (
                round(item["error_count"] / item["total_count"], 3)
                if item["total_count"]
                else 0
            )
            knowledge = connection.execute(
                """SELECT kn.node_uid, kn.name FROM kg_resource_link krl
                   JOIN kg_nodes kn ON kn.node_uid=krl.node_uid
                   WHERE krl.resource_type='question' AND krl.resource_id=?
                   ORDER BY kn.node_uid LIMIT 1""",
                (item["question_id"],),
            ).fetchone()
            item["knowledge_point_name"] = knowledge["name"] if knowledge else None
            item["knowledge_point_uid"] = knowledge["node_uid"] if knowledge else None
        return TeachingResult.success(
            {
                "scope": {"exam_id": exam_id, "class_id": class_id},
                "top": top,
                "error_questions": items,
            }
        )

    def _weak_points(self, connection: sqlite3.Connection, query: TeachingQuery) -> TeachingResult:
        filters = query.filters
        student_id = filters.get("student_id")
        class_id = filters.get("class_id")
        course_id = filters.get("course_id")
        threshold = filters.get("threshold", 0.6)
        top = filters.get("top", 10)
        if student_id is None and class_id is None:
            return TeachingResult.failure(
                TeachingProviderErrorKind.INVALID_QUERY,
                "需提供 student_id 或 class_id 之一",
            )
        denied = self._require_course(query, course_id)
        if denied is not None:
            return denied
        if student_id is not None:
            where = ["sks.student_id=?", "sks.mastery_rate < ?"]
            parameters: list[Any] = [student_id, threshold]
            if course_id is not None:
                where.append("sks.course_id=?")
                parameters.append(course_id)
            scope_clause, scope_parameters = self._scope_clause(query.scope, "sks.course_id")
            if scope_clause:
                where.append(scope_clause)
                parameters.extend(scope_parameters)
            rows = connection.execute(
                f"""SELECT sks.node_uid, kn.name AS knowledge_point, kn.type, kn.course_id,
                           sks.mastery_rate, sks.correct_count, sks.total_questions
                    FROM student_knowledge_stats sks
                    JOIN kg_nodes kn ON kn.node_uid = sks.node_uid
                    WHERE {' AND '.join(where)}
                    ORDER BY sks.mastery_rate ASC, sks.total_questions DESC, sks.node_uid
                    LIMIT ?""",
                parameters + [top],
            ).fetchall()
            return TeachingResult.success(
                {
                    "scope": "student",
                    "student_id": student_id,
                    "threshold": threshold,
                    "weak_points": [dict(row) for row in rows],
                }
            )
        where = ["cs.class_id=?"]
        parameters = [class_id]
        if course_id is not None:
            where.append("sks.course_id=?")
            parameters.append(course_id)
        scope_clause, scope_parameters = self._scope_clause(query.scope, "sks.course_id")
        if scope_clause:
            where.append(scope_clause)
            parameters.extend(scope_parameters)
        rows = connection.execute(
            f"""SELECT sks.node_uid, kn.name AS knowledge_point, kn.type, kn.course_id,
                       ROUND(AVG(sks.mastery_rate),3) AS avg_mastery,
                       COUNT(DISTINCT sks.student_id) AS student_count
                FROM student_knowledge_stats sks
                JOIN class_students cs ON cs.student_id = sks.student_id
                JOIN kg_nodes kn ON kn.node_uid = sks.node_uid
                WHERE {' AND '.join(where)}
                GROUP BY sks.node_uid
                HAVING avg_mastery < ?
                ORDER BY avg_mastery ASC, sks.node_uid
                LIMIT ?""",
            parameters + [threshold, top],
        ).fetchall()
        return TeachingResult.success(
            {
                "scope": "class",
                "class_id": class_id,
                "threshold": threshold,
                "weak_points": [dict(row) for row in rows],
            }
        )

    def _score_distribution(
        self, connection: sqlite3.Connection, query: TeachingQuery
    ) -> TeachingResult:
        exam_id = query.filters["exam_id"]
        exam = connection.execute(
            "SELECT id, exam_name, total_score, pass_score, course_id FROM exams WHERE id=?",
            (exam_id,),
        ).fetchone()
        if exam is None:
            return TeachingResult.failure(
                TeachingProviderErrorKind.NOT_FOUND,
                f"考试 {exam_id} 不存在",
                details={"exam_id": exam_id},
            )
        denied = self._require_course(query, exam["course_id"])
        if denied is not None:
            return denied
        rows = connection.execute(
            "SELECT score, passed FROM exam_records "
            "WHERE exam_id=? AND score IS NOT NULL ORDER BY student_id",
            (exam_id,),
        ).fetchall()
        scores = [row["score"] for row in rows]
        if not scores:
            return TeachingResult.success(
                {"exam_id": exam_id, "total_students": 0, "distribution": []}
            )
        total_score = exam["total_score"] or 100
        buckets = [
            ("A", 90, 100),
            ("B", 80, 90),
            ("C", 70, 80),
            ("D", 60, 70),
            ("F", 0, 60),
        ]
        distribution = []
        for label, low, high in buckets:
            count = sum(
                1
                for score in scores
                if score * 100.0 / total_score >= low
                and (
                    score * 100.0 / total_score < high
                    or high == 100
                    and score * 100.0 / total_score <= 100
                )
            )
            distribution.append(
                {
                    "grade": label,
                    "range_pct": f"{low}-{high}",
                    "student_count": count,
                    "percentage": round(count * 100.0 / len(scores), 1),
                }
            )
        pass_count = sum(row["passed"] for row in rows)
        return TeachingResult.success(
            {
                "exam_id": exam_id,
                "exam_name": exam["exam_name"],
                "total_score": total_score,
                "pass_score": exam["pass_score"],
                "total_students": len(scores),
                "average_score": round(statistics.mean(scores), 1),
                "median_score": round(statistics.median(scores), 1),
                "max_score": max(scores),
                "min_score": min(scores),
                "std_dev": round(statistics.pstdev(scores), 2) if len(scores) > 1 else 0.0,
                "pass_count": pass_count,
                "pass_rate": round(pass_count * 100.0 / len(scores), 1),
                "distribution": distribution,
            }
        )

    def _knowledge_graph(
        self, connection: sqlite3.Connection, query: TeachingQuery
    ) -> TeachingResult:
        filters = query.filters
        course_id = filters["course_id"]
        operation = filters["operation"]
        node_ref = filters.get("node")
        target_ref = filters.get("target")
        node_type = filters.get("node_type")
        name = filters.get("name")
        denied = self._require_course(query, course_id)
        if denied is not None:
            return denied
        graph = self._load_graph(connection, course_id)
        if not graph.nodes:
            return TeachingResult.failure(
                TeachingProviderErrorKind.NOT_FOUND,
                f"课程 {course_id} 无知识图谱数据",
                details={"course_id": course_id},
            )
        if operation == "find":
            found = sorted(
                graph.find_nodes(name=name, node_type=node_type),
                key=lambda item: item["node_uid"],
            )
            return TeachingResult.success(
                {
                    "operation": "find",
                    "course_id": course_id,
                    "count": len(found),
                    "nodes": [self._clean_node(item) for item in found],
                }
            )
        if node_ref is None:
            return TeachingResult.failure(
                TeachingProviderErrorKind.INVALID_QUERY,
                f"operation={operation} 需提供 node",
            )
        source = graph.resolve(str(node_ref))
        if source is None:
            return TeachingResult.failure(
                TeachingProviderErrorKind.NOT_FOUND,
                f"未找到节点：{node_ref}",
            )
        if operation == "neighbors":
            neighbors = sorted(
                graph.neighbors(source["node_uid"], direction="both"),
                key=self._neighbor_order,
            )
            return TeachingResult.success(
                {
                    "operation": "neighbors",
                    "node": self._clean_node(source),
                    "count": len(neighbors),
                    "neighbors": [self._clean_node(item) for item in neighbors],
                }
            )
        if operation == "prerequisites":
            prerequisites = graph.prerequisites(source["node_uid"])
            return TeachingResult.success(
                {
                    "operation": "prerequisites",
                    "node": self._clean_node(source),
                    "count": len(prerequisites),
                    "prerequisites": [self._clean_node(item) for item in prerequisites],
                }
            )
        if operation in {"related", "similar"}:
            relation = "RELATED_TO" if operation == "related" else "SIMILAR_TO"
            neighbors = sorted(
                graph.neighbors(
                    source["node_uid"], rel_types={relation}, direction="both"
                ),
                key=self._neighbor_order,
            )
            return TeachingResult.success(
                {
                    "operation": operation,
                    "node": self._clean_node(source),
                    "count": len(neighbors),
                    "nodes": [self._clean_node(item) for item in neighbors],
                }
            )
        if operation == "path":
            if not target_ref:
                return TeachingResult.failure(
                    TeachingProviderErrorKind.INVALID_QUERY,
                    "operation=path 需提供 target",
                )
            target = graph.resolve(str(target_ref))
            if target is None:
                return TeachingResult.failure(
                    TeachingProviderErrorKind.NOT_FOUND,
                    f"未找到终点节点：{target_ref}",
                )
            shortest = graph.shortest_path([source["node_uid"]], target["node_uid"])
            if shortest is None:
                return TeachingResult.success(
                    {
                        "operation": "path",
                        "from": self._clean_node(source),
                        "to": self._clean_node(target),
                        "path": [],
                        "note": "两节点间无（有向）学习路径",
                    }
                )
            path, cost = shortest
            return TeachingResult.success(
                {
                    "operation": "path",
                    "from": self._clean_node(source),
                    "to": self._clean_node(target),
                    "cost": cost,
                    "length": len(path),
                    "path": [self._clean_node(item) for item in path],
                    "cypher_hint": graph.to_cypher_hint(target["node_uid"]),
                }
            )
        return TeachingResult.failure(
            TeachingProviderErrorKind.INVALID_QUERY,
            f"未知 operation：{operation}",
        )

    def _study_path(self, connection: sqlite3.Connection, query: TeachingQuery) -> TeachingResult:
        filters = query.filters
        student_id = filters["student_id"]
        course_id = filters["course_id"]
        target_ref = filters.get("target")
        threshold = filters.get("threshold", 0.6)
        max_points = filters.get("max_points", 6)
        questions_per_point = filters.get("questions_per_point", 3)
        denied = self._require_course(query, course_id)
        if denied is not None:
            return denied
        graph = self._load_graph(connection, course_id)
        if not graph.nodes:
            return TeachingResult.failure(
                TeachingProviderErrorKind.NOT_FOUND,
                f"课程 {course_id} 无知识图谱数据",
                details={"course_id": course_id},
            )
        mastery = {
            row["node_uid"]: row["mastery_rate"]
            for row in connection.execute(
                "SELECT node_uid, mastery_rate FROM student_knowledge_stats "
                "WHERE student_id=? AND course_id=? ORDER BY node_uid",
                (student_id, course_id),
            )
        }
        weak_uids = {
            uid
            for uid, rate in mastery.items()
            if rate < threshold
            and graph.nodes.get(uid, {}).get("type") in {"concept", "skill"}
        }
        if target_ref:
            target = graph.resolve(str(target_ref))
            if target is None:
                return TeachingResult.failure(
                    TeachingProviderErrorKind.NOT_FOUND,
                    f"未找到目标知识点：{target_ref}",
                )
        else:
            candidates = [graph.nodes[uid] for uid in weak_uids] or [
                node for node in graph.nodes.values() if node["type"] in {"concept", "skill"}
            ]
            if not candidates:
                return TeachingResult.success(
                    {
                        "student_id": student_id,
                        "course_id": course_id,
                        "note": "无可推荐的知识点",
                        "path": [],
                    }
                )
            candidates.sort(
                key=lambda item: (
                    item["type"] != "skill",
                    -(item.get("importance") or 0),
                    item["node_uid"],
                )
            )
            target = candidates[0]
        target_uid = target["node_uid"]
        prerequisites = graph.prerequisites(target_uid)
        weak_prerequisites = [
            item for item in prerequisites if item["node_uid"] in weak_uids
        ]
        weak_prerequisites.sort(
            key=lambda item: (
                len(graph.prerequisites(item["node_uid"])),
                item["node_uid"],
            )
        )
        ordered = list(weak_prerequisites)
        if mastery.get(target_uid, 1.0) < threshold or not ordered:
            ordered.append(target)
        if len(ordered) > max_points:
            ordered = ordered[-max_points:]
        path = []
        for node in ordered:
            practice = self._questions(
                connection,
                TeachingQuery(
                    kind=TeachingQueryKind.QUESTIONS,
                    filters={
                        "course_id": course_id,
                        "knowledge_point": node["node_uid"],
                        "status": 1,
                    },
                    scope=query.scope,
                    page=PageRequest(number=1, size=questions_per_point),
                ),
            )
            questions = list((practice.data or {}).get("questions", ())) if practice.ok else []
            path.append(
                {
                    "node_uid": node["node_uid"],
                    "name": node["name"],
                    "type": node["type"],
                    "difficulty": node.get("difficulty"),
                    "mastery_rate": mastery.get(node["node_uid"]),
                    "practice_questions": [
                        {
                            "id": item["id"],
                            "title": item["title"],
                            "difficulty": item["difficulty"],
                            "question_type": item["question_type"],
                        }
                        for item in questions
                    ],
                }
            )
        return TeachingResult.success(
            {
                "student_id": student_id,
                "course_id": course_id,
                "target": {
                    "node_uid": target_uid,
                    "name": target["name"],
                    "type": target["type"],
                    "mastery_rate": mastery.get(target_uid),
                },
                "weak_point_count": len(weak_uids),
                "path": path,
                "cypher_hint": graph.to_cypher_hint(target_uid),
            }
        )

    @staticmethod
    def _resolve_knowledge_point(
        connection: sqlite3.Connection, ref: str, *, course_id: int | None
    ) -> tuple[str, int] | None:
        row = connection.execute(
            "SELECT node_uid, course_id FROM kg_nodes WHERE node_uid=?", (ref,)
        ).fetchone()
        if row is not None:
            return row["node_uid"], row["course_id"]
        if course_id is not None:
            row = connection.execute(
                "SELECT node_uid, course_id FROM kg_nodes "
                "WHERE name=? AND course_id=? ORDER BY node_uid LIMIT 1",
                (ref, course_id),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT node_uid, course_id FROM kg_nodes WHERE name=? "
                "ORDER BY course_id, node_uid LIMIT 1",
                (ref,),
            ).fetchone()
        if row is not None:
            return row["node_uid"], row["course_id"]
        if course_id is not None:
            row = connection.execute(
                "SELECT node_uid, course_id FROM kg_nodes "
                "WHERE name LIKE ? AND course_id=? ORDER BY node_uid LIMIT 1",
                (f"%{ref}%", course_id),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT node_uid, course_id FROM kg_nodes WHERE name LIKE ? "
                "ORDER BY course_id, node_uid LIMIT 1",
                (f"%{ref}%",),
            ).fetchone()
        return (row["node_uid"], row["course_id"]) if row is not None else None

    @staticmethod
    def _question_knowledge_points(
        connection: sqlite3.Connection, question_id: int
    ) -> list[str]:
        rows = connection.execute(
            """SELECT kn.name FROM kg_resource_link krl
               JOIN kg_nodes kn ON kn.node_uid=krl.node_uid
               WHERE krl.resource_type='question' AND krl.resource_id=?
               ORDER BY kn.name, kn.node_uid""",
            (question_id,),
        ).fetchall()
        return [row["name"] for row in rows]

    @staticmethod
    def _load_graph(connection: sqlite3.Connection, course_id: int) -> KnowledgeGraph:
        graph = KnowledgeGraph()
        for row in connection.execute(
            "SELECT * FROM kg_nodes WHERE course_id=? ORDER BY node_uid", (course_id,)
        ):
            graph.nodes[row["node_uid"]] = dict(row)
        for row in connection.execute(
            "SELECT * FROM kg_edges WHERE course_id=? "
            "ORDER BY start_uid, end_uid, rel_type, id",
            (course_id,),
        ):
            graph.add_edge(
                Edge(
                    row["rel_type"],
                    row["start_uid"],
                    row["end_uid"],
                    row["weight"],
                    row["source"],
                )
            )
        return graph

    @staticmethod
    def _clean_node(node: dict) -> dict:
        result = {key: node[key] for key in _NODE_FIELDS if key in node}
        for extra in ("_rel_type", "_weight", "_direction"):
            if extra in node:
                result[extra.lstrip("_")] = node[extra]
        return result

    @staticmethod
    def _neighbor_order(node: dict) -> tuple[str, str, str]:
        return (
            str(node.get("_rel_type", "")),
            str(node.get("node_uid", "")),
            str(node.get("_direction", "")),
        )
