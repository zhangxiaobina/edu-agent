"""Registry-backed SQLite implementation of the teaching-data contract."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from ..data.kg import Edge, KnowledgeGraph
from .contracts import (
    ExamStatus,
    PageRequest,
    TeachingCommand,
    TeachingCommandKind,
    TeachingCommandResult,
    TeachingDataProvider,
    TeachingProviderErrorKind,
    TeachingQuery,
    TeachingQueryKind,
    TeachingResult,
    TeachingScope,
)


ConnectionFactory = Callable[[], sqlite3.Connection]


_NODE_FIELDS = ("node_uid", "name", "type", "difficulty", "importance", "course_id")
_QUESTION_TYPE_CYCLE = ("single", "judge", "fill")
_DIFFICULTY_CYCLE = ("easy", "medium", "hard")
_QUESTION_OPTIONS = ("选项A", "选项B", "选项C", "选项D")


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
        self._command_handlers = {
            TeachingCommandKind.CREATE_EXAM: self._create_exam,
            TeachingCommandKind.GENERATE_PAPER: self._generate_paper,
            TeachingCommandKind.BATCH_GRADE: self._batch_grade,
            TeachingCommandKind.ASSIGN_HOMEWORK: self._assign_homework,
            TeachingCommandKind.GENERATE_QUESTIONS: self._generate_questions,
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

    def execute_command(
        self,
        command: TeachingCommand,
        *,
        connection: object | None = None,
    ) -> TeachingCommandResult:
        if not isinstance(command, TeachingCommand):
            return TeachingCommandResult.failure(
                TeachingProviderErrorKind.INVALID_COMMAND,
                "教学 command 契约无效",
            )
        handler = self._command_handlers.get(command.kind)
        if handler is None:
            return TeachingCommandResult.failure(
                TeachingProviderErrorKind.UNSUPPORTED,
                f"不支持的教学 command：{command.kind.value}",
            )
        if connection is not None and not isinstance(connection, sqlite3.Connection):
            return TeachingCommandResult.failure(
                TeachingProviderErrorKind.INVALID_COMMAND,
                "受控连接类型与 SyntheticProvider 不兼容",
            )
        if command.mutating:
            if command.operation is None:
                return TeachingCommandResult.failure(
                    TeachingProviderErrorKind.APPROVAL_REQUIRED,
                    "教学写入必须经过 ToolOperation 与审批执行器",
                )
            if command.operation.status not in {"approved", "executing"}:
                return TeachingCommandResult.failure(
                    TeachingProviderErrorKind.APPROVAL_REQUIRED,
                    f"ToolOperation 状态 {command.operation.status} 不允许写入",
                )
            if connection is None or not connection.in_transaction:
                return TeachingCommandResult.failure(
                    TeachingProviderErrorKind.INVALID_COMMAND,
                    "教学写入必须加入执行器控制的同库事务",
                )
            authority_error = self._validate_mutation_authority(connection, command)
            if authority_error is not None:
                return authority_error
        try:
            with self._connection(connection) as active:
                return handler(active, command)
        except (KeyError, TypeError, ValueError) as error:
            return TeachingCommandResult.failure(
                TeachingProviderErrorKind.INVALID_COMMAND,
                "教学 command 参数无效",
                details={"cause": type(error).__name__},
            )
        except sqlite3.IntegrityError as error:
            return TeachingCommandResult.failure(
                TeachingProviderErrorKind.BUSINESS_REJECTED,
                "教学业务约束拒绝了该操作",
                details={"cause": type(error).__name__},
            )
        except (TimeoutError, sqlite3.OperationalError) as error:
            return TeachingCommandResult.failure(
                TeachingProviderErrorKind.UNAVAILABLE,
                "合成教学服务暂不可用",
                retryable=True,
                details={"cause": type(error).__name__},
            )
        except sqlite3.DatabaseError as error:
            return TeachingCommandResult.failure(
                TeachingProviderErrorKind.INTERNAL,
                "合成教学 command 执行失败",
                details={"cause": type(error).__name__},
            )
        except Exception as error:  # noqa: BLE001 - do not leak adapter details
            return TeachingCommandResult.failure(
                TeachingProviderErrorKind.INTERNAL,
                "教学 Provider command 执行失败",
                details={"cause": type(error).__name__},
            )

    @staticmethod
    def _validate_mutation_authority(
        connection: sqlite3.Connection,
        command: TeachingCommand,
    ) -> TeachingCommandResult | None:
        operation = command.operation
        if operation is None:
            return TeachingCommandResult.failure(
                TeachingProviderErrorKind.APPROVAL_REQUIRED,
                "教学写入缺少 ToolOperation",
            )
        try:
            row = connection.execute(
                """SELECT idempotency_key, payload_hash, tool_name, status,
                          approval_scope
                   FROM tool_operations WHERE id=?""",
                (operation.operation_id,),
            ).fetchone()
        except sqlite3.DatabaseError:
            row = None
        if row is None:
            return TeachingCommandResult.failure(
                TeachingProviderErrorKind.APPROVAL_REQUIRED,
                "教学写入没有可验证的 ToolOperation",
            )
        expected = {
            "idempotency_key": operation.idempotency_key,
            "payload_hash": operation.payload_hash,
            "tool_name": command.kind.value,
            "status": "executing",
            "approval_scope": operation.approval_scope,
        }
        if any(row[key] != value for key, value in expected.items()):
            return TeachingCommandResult.failure(
                TeachingProviderErrorKind.APPROVAL_REQUIRED,
                "ToolOperation 与 canonical command 绑定不匹配",
            )
        operation_arguments = dict(operation.arguments)
        digest_payload = json.dumps(
            {"tool": command.kind.value, "arguments": operation_arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        if hashlib.sha256(digest_payload.encode()).hexdigest() != operation.payload_hash:
            return TeachingCommandResult.failure(
                TeachingProviderErrorKind.APPROVAL_REQUIRED,
                "ToolOperation payload hash 无法验证",
            )
        if any(
            key not in command.payload or command.payload[key] != value
            for key, value in operation_arguments.items()
        ):
            return TeachingCommandResult.failure(
                TeachingProviderErrorKind.APPROVAL_REQUIRED,
                "canonical command payload 与审批参数不一致",
            )
        approval = connection.execute(
            """SELECT 1 FROM tool_approvals
               WHERE operation_id=? AND payload_hash=? AND scope=?
                   AND decision='approved' AND expires_at>?
               ORDER BY created_at DESC LIMIT 1""",
            (
                operation.operation_id,
                operation.payload_hash,
                operation.approval_scope,
                datetime.now(UTC).isoformat(),
            ),
        ).fetchone()
        if approval is None:
            return TeachingCommandResult.failure(
                TeachingProviderErrorKind.APPROVAL_REQUIRED,
                "ToolOperation 缺少未过期的有效审批",
            )
        return None

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

    @staticmethod
    def _command_scope_denied(course_id: int) -> TeachingCommandResult:
        return TeachingCommandResult.failure(
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
    def _require_command_course(
        cls,
        command: TeachingCommand,
        course_id: int | None,
    ) -> TeachingCommandResult | None:
        if course_id is not None and not command.scope.allows_course(int(course_id)):
            return cls._command_scope_denied(int(course_id))
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

    def _create_exam(
        self,
        connection: sqlite3.Connection,
        command: TeachingCommand,
    ) -> TeachingCommandResult:
        payload = command.payload
        class_id = payload["class_id"]
        course_id = payload["course_id"]
        denied = self._require_command_course(command, course_id)
        if denied is not None:
            return denied
        if connection.execute(
            "SELECT 1 FROM classes WHERE id=?", (class_id,)
        ).fetchone() is None:
            return TeachingCommandResult.failure(
                TeachingProviderErrorKind.NOT_FOUND,
                f"班级 {class_id} 不存在",
                details={"class_id": class_id},
            )
        if connection.execute(
            "SELECT 1 FROM courses WHERE id=?", (course_id,)
        ).fetchone() is None:
            return TeachingCommandResult.failure(
                TeachingProviderErrorKind.NOT_FOUND,
                f"课程 {course_id} 不存在",
                details={"course_id": course_id},
            )
        exam_id = self._next_id(connection, "exams")
        exam_code = f"EX{exam_id:04d}"
        connection.execute(
            """INSERT INTO exams(
                   id, exam_name, exam_code, description, class_id, course_id,
                   question_bank_id, creator_id, start_time, end_time, duration,
                   total_score, pass_score, question_count, status
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (
                exam_id,
                payload["exam_name"],
                exam_code,
                payload.get("description"),
                class_id,
                course_id,
                payload.get("question_bank_id"),
                self._course_teacher(connection, course_id),
                None,
                None,
                payload.get("duration", 90),
                payload.get("total_score", 100),
                payload.get("pass_score", 60),
                payload.get("question_count", 0),
            ),
        )
        return TeachingCommandResult.success(
            command,
            {
                "created": True,
                "exam_id": exam_id,
                "exam_code": exam_code,
                "exam_name": payload["exam_name"],
                "class_id": class_id,
                "course_id": course_id,
                "status": 0,
                "status_text": "未开始(草稿)",
            },
        )

    def _generate_paper(
        self,
        connection: sqlite3.Connection,
        command: TeachingCommand,
    ) -> TeachingCommandResult:
        payload = command.payload
        bank_id = payload["question_bank_id"]
        bank = connection.execute(
            "SELECT id, name, course_id FROM question_banks WHERE id=?",
            (bank_id,),
        ).fetchone()
        if bank is None:
            return TeachingCommandResult.failure(
                TeachingProviderErrorKind.NOT_FOUND,
                f"题库 {bank_id} 不存在",
                details={"question_bank_id": bank_id},
            )
        denied = self._require_command_course(command, bank["course_id"])
        if denied is not None:
            return denied
        pool = [
            dict(row)
            for row in connection.execute(
                """SELECT q.id, q.title, q.question_type, q.difficulty, q.score
                   FROM questions q
                   JOIN question_bank_questions qbq ON qbq.question_id=q.id
                   WHERE qbq.question_bank_id=? AND q.status=1
                   ORDER BY q.id""",
                (bank_id,),
            ).fetchall()
        ]
        knowledge_points = payload.get("knowledge_points")
        if knowledge_points:
            resolved = {
                item[0]
                for reference in knowledge_points
                if (
                    item := self._resolve_knowledge_point(
                        connection,
                        reference,
                        course_id=bank["course_id"],
                    )
                )
            }
            if resolved:
                placeholders = ",".join("?" for _ in resolved)
                allowed = {
                    row["resource_id"]
                    for row in connection.execute(
                        "SELECT resource_id FROM kg_resource_link "
                        "WHERE resource_type='question' "
                        f"AND node_uid IN ({placeholders})",
                        sorted(resolved),
                    )
                }
                pool = [question for question in pool if question["id"] in allowed]

        selected: list[dict[str, Any]] = []
        chosen_ids: set[int] = set()

        def take(items: list[dict[str, Any]], count: int) -> None:
            for question in items:
                if len(selected) >= 200 or question["id"] in chosen_ids:
                    continue
                selected.append(question)
                chosen_ids.add(question["id"])
                count -= 1
                if count <= 0:
                    break

        difficulty_distribution = payload.get("difficulty_distribution")
        question_counts = payload.get("question_counts")
        if difficulty_distribution:
            for difficulty, count in difficulty_distribution.items():
                take(
                    [item for item in pool if item["difficulty"] == difficulty],
                    count,
                )
        elif question_counts:
            for question_type, count in question_counts.items():
                take(
                    [item for item in pool if item["question_type"] == question_type],
                    count,
                )
        else:
            take(pool, payload.get("total_questions", 10))

        type_distribution: dict[str, int] = {}
        selected_difficulties: dict[str, int] = {}
        for question in selected:
            question_type = question["question_type"]
            difficulty = question["difficulty"]
            type_distribution[question_type] = type_distribution.get(question_type, 0) + 1
            selected_difficulties[difficulty] = selected_difficulties.get(difficulty, 0) + 1
        quality, suggestions = self._paper_quality(
            selected,
            selected_difficulties,
            type_distribution,
        )
        return TeachingCommandResult.success(
            command,
            {
                "preview_id": f"PV-{bank_id}-{len(selected)}",
                "paper_name": payload.get("paper_name") or f"{bank['name']}自动组卷",
                "question_bank_id": bank_id,
                "course_id": bank["course_id"],
                "total_questions": len(selected),
                "total_score": round(sum(item["score"] for item in selected), 1),
                "difficulty_distribution": selected_difficulties,
                "type_distribution": type_distribution,
                "quality_score": quality,
                "suggestions": suggestions,
                "questions": selected,
                "note": "预览阶段；真实平台需 confirm 才落库为正式试卷",
            },
        )

    def _batch_grade(
        self,
        connection: sqlite3.Connection,
        command: TeachingCommand,
    ) -> TeachingCommandResult:
        payload = command.payload
        exam_id = payload["exam_id"]
        exam = connection.execute(
            "SELECT course_id, pass_score FROM exams WHERE id=?", (exam_id,)
        ).fetchone()
        if exam is None:
            return TeachingCommandResult.failure(
                TeachingProviderErrorKind.NOT_FOUND,
                f"考试 {exam_id} 不存在",
                details={"exam_id": exam_id},
            )
        denied = self._require_command_course(command, exam["course_id"])
        if denied is not None:
            return denied
        regrade = bool(payload.get("regrade", False))
        status_filter = "" if regrade else " AND status < 3"
        records = connection.execute(
            f"SELECT id, student_id FROM exam_records "
            f"WHERE exam_id=?{status_filter} ORDER BY id",
            (exam_id,),
        ).fetchall()
        graded = 0
        failed = 0
        for record in records:
            aggregate = connection.execute(
                """SELECT COALESCE(SUM(earned_score), 0) AS score,
                          COALESCE(SUM(is_correct), 0) AS correct_count,
                          COUNT(*) AS answer_count
                   FROM exam_answers WHERE record_id=?""",
                (record["id"],),
            ).fetchone()
            if aggregate["answer_count"] == 0:
                failed += 1
                continue
            passed = 1 if aggregate["score"] >= exam["pass_score"] else 0
            connection.execute(
                """UPDATE exam_records
                   SET score=?, correct_count=?, answer_count=?, status=3, passed=?
                   WHERE id=?""",
                (
                    round(aggregate["score"], 1),
                    aggregate["correct_count"],
                    aggregate["answer_count"],
                    passed,
                    record["id"],
                ),
            )
            graded += 1
        ranked = connection.execute(
            "SELECT id FROM exam_records WHERE exam_id=? ORDER BY score DESC, id",
            (exam_id,),
        ).fetchall()
        for rank, record in enumerate(ranked, start=1):
            connection.execute(
                "UPDATE exam_records SET rank=? WHERE id=?",
                (rank, record["id"]),
            )
        return TeachingCommandResult.success(
            command,
            {
                "exam_id": exam_id,
                "total_records": len(records),
                "graded_count": graded,
                "failed_count": failed,
                "regrade": regrade,
            },
        )

    def _assign_homework(
        self,
        connection: sqlite3.Connection,
        command: TeachingCommand,
    ) -> TeachingCommandResult:
        payload = command.payload
        course_id = payload["course_id"]
        denied = self._require_command_course(command, course_id)
        if denied is not None:
            return denied
        if connection.execute(
            "SELECT 1 FROM courses WHERE id=?", (course_id,)
        ).fetchone() is None:
            return TeachingCommandResult.failure(
                TeachingProviderErrorKind.NOT_FOUND,
                f"课程 {course_id} 不存在",
                details={"course_id": course_id},
            )
        homework_id = self._next_id(connection, "homeworks")
        connection.execute(
            """INSERT INTO homeworks(
                   id, title, homework_type, description, course_id, creator_id,
                   start_time, end_time, total_score, max_submissions, status
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PUBLISHED')""",
            (
                homework_id,
                payload["title"],
                payload.get("homework_type", "open"),
                payload.get("description"),
                course_id,
                self._course_teacher(connection, course_id),
                payload.get("start_time"),
                payload["end_time"],
                payload.get("total_score", 100),
                payload.get("max_submissions", 1),
            ),
        )
        linked: list[int] = []
        class_ids = payload["class_ids"]
        if isinstance(class_ids, int):
            class_ids = [class_ids]
        for class_id in class_ids:
            if connection.execute(
                "SELECT 1 FROM classes WHERE id=?", (class_id,)
            ).fetchone():
                connection.execute(
                    "INSERT INTO homework_classes(homework_id, class_id) VALUES (?, ?)",
                    (homework_id, class_id),
                )
                linked.append(class_id)
        return TeachingCommandResult.success(
            command,
            {
                "created": True,
                "homework_id": homework_id,
                "title": payload["title"],
                "course_id": course_id,
                "class_ids": linked,
                "end_time": payload["end_time"],
                "status": "PUBLISHED",
            },
        )

    def _generate_questions(
        self,
        connection: sqlite3.Connection,
        command: TeachingCommand,
    ) -> TeachingCommandResult:
        payload = command.payload
        course_id = payload["course_id"]
        denied = self._require_command_course(command, course_id)
        if denied is not None:
            return denied
        course = connection.execute(
            "SELECT id, name FROM courses WHERE id=?", (course_id,)
        ).fetchone()
        if course is None:
            return TeachingCommandResult.failure(
                TeachingProviderErrorKind.NOT_FOUND,
                f"课程 {course_id} 不存在",
                details={"course_id": course_id},
            )
        bank_id = payload.get("save_to_bank")
        if bank_id:
            bank = connection.execute(
                "SELECT course_id FROM question_banks WHERE id=?", (bank_id,)
            ).fetchone()
            if bank is None:
                return TeachingCommandResult.failure(
                    TeachingProviderErrorKind.NOT_FOUND,
                    f"题库 {bank_id} 不存在",
                    details={"question_bank_id": bank_id},
                )
            if int(bank["course_id"]) != int(course_id):
                return TeachingCommandResult.failure(
                    TeachingProviderErrorKind.BUSINESS_REJECTED,
                    "目标题库与出题课程不一致",
                    details={"question_bank_id": bank_id, "course_id": course_id},
                )

        knowledge_point_uid = None
        knowledge_point_name = payload.get("knowledge_point")
        if knowledge_point_name:
            resolved = self._resolve_knowledge_point(
                connection,
                knowledge_point_name,
                course_id=course_id,
            )
            if resolved is not None:
                knowledge_point_uid = resolved[0]
                knowledge_point_name = connection.execute(
                    "SELECT name FROM kg_nodes WHERE node_uid=?",
                    (knowledge_point_uid,),
                ).fetchone()["name"]
        if not knowledge_point_name:
            row = connection.execute(
                "SELECT node_uid, name FROM kg_nodes "
                "WHERE course_id=? AND type='concept' ORDER BY node_uid LIMIT 1",
                (course_id,),
            ).fetchone()
            if row is not None:
                knowledge_point_uid = row["node_uid"]
                knowledge_point_name = row["name"]
            else:
                knowledge_point_name = course["name"]

        pairs = self._expand_question_pairs(
            payload.get("count", 5),
            payload.get("question_types"),
            payload.get("difficulty_distribution"),
        )
        generated = []
        saved_ids = []
        for index, (question_type, difficulty) in enumerate(pairs, start=1):
            options, answer = self._generate_question_body(question_type, index)
            question = {
                "title": f"【AI·{knowledge_point_name}】生成题{index}",
                "content": (
                    f"围绕知识点「{knowledge_point_name}」生成的"
                    f"{difficulty}难度{question_type}题（合成）。"
                ),
                "question_type": question_type,
                "difficulty": difficulty,
                "options": options,
                "correct_answer": answer,
                "source": "ai",
            }
            generated.append(question)
            if bank_id:
                question_id = self._save_question(
                    connection,
                    question,
                    course_id,
                    knowledge_point_uid,
                    bank_id,
                )
                question["id"] = question_id
                saved_ids.append(question_id)
        return TeachingCommandResult.success(
            command,
            {
                "course_id": course_id,
                "knowledge_point": knowledge_point_name,
                "generation_type": (
                    "knowledge_graph" if knowledge_point_uid else "manual"
                ),
                "status": "completed",
                "created_questions": len(generated),
                "saved_to_bank": bank_id,
                "saved_question_ids": saved_ids,
                "questions": generated,
                "note": (
                    "模板化合成生成；接入工具调用模型(vLLM/API)"
                    "后可替换为真实 AI 出题。"
                ),
            },
        )

    @staticmethod
    def _next_id(connection: sqlite3.Connection, table: str) -> int:
        return connection.execute(
            f"SELECT COALESCE(MAX(id), 0) + 1 FROM {table}"
        ).fetchone()[0]

    @staticmethod
    def _course_teacher(connection: sqlite3.Connection, course_id: int) -> int | None:
        row = connection.execute(
            "SELECT teacher_id FROM courses WHERE id=?", (course_id,)
        ).fetchone()
        return row["teacher_id"] if row is not None else None

    @staticmethod
    def _paper_quality(
        selected: list[dict[str, Any]],
        difficulty_distribution: dict[str, int],
        type_distribution: dict[str, int],
    ) -> tuple[float, list[str]]:
        suggestions = []
        if not selected:
            return 0.0, ["未选中任何题目，请放宽过滤条件"]
        count = len(selected)
        ideal = {"easy": 0.3, "medium": 0.5, "hard": 0.2}
        deviation = sum(
            abs(difficulty_distribution.get(level, 0) / count - ratio)
            for level, ratio in ideal.items()
        )
        balance = max(0.0, 1 - deviation)
        diversity = min(1.0, len(type_distribution) / 3)
        quality = round(0.6 * balance + 0.4 * diversity, 2)
        if difficulty_distribution.get("hard", 0) == 0:
            suggestions.append("缺少难题，建议加入 hard 题以拉开区分度")
        if len(type_distribution) < 2:
            suggestions.append("题型单一，建议混合多种题型")
        if not suggestions:
            suggestions.append("难度与题型分布较均衡")
        return quality, suggestions

    @staticmethod
    def _expand_question_pairs(
        count: int,
        question_types: dict[str, int] | None,
        difficulty_distribution: dict[str, int] | None,
    ) -> list[tuple[str, str]]:
        types = []
        if question_types:
            for question_type, amount in question_types.items():
                types += [question_type] * int(amount)
        difficulties = []
        if difficulty_distribution:
            for difficulty, amount in difficulty_distribution.items():
                difficulties += [difficulty] * int(amount)
        size = max(count or 0, len(types), len(difficulties)) or 5
        return [
            (
                types[index]
                if index < len(types)
                else _QUESTION_TYPE_CYCLE[index % len(_QUESTION_TYPE_CYCLE)],
                difficulties[index]
                if index < len(difficulties)
                else _DIFFICULTY_CYCLE[index % len(_DIFFICULTY_CYCLE)],
            )
            for index in range(size)
        ]

    @staticmethod
    def _generate_question_body(
        question_type: str,
        index: int,
    ) -> tuple[list[str] | None, str]:
        if question_type == "single":
            return list(_QUESTION_OPTIONS), "ABCD"[index % 4]
        if question_type == "multiple":
            return (
                list(_QUESTION_OPTIONS),
                ",".join(sorted({"A", "B", "C"})[: 2 + (index % 2)]),
            )
        if question_type == "judge":
            return ["正确", "错误"], "正确" if index % 2 == 0 else "错误"
        if question_type == "fill":
            return None, "参考答案"
        return None, "# 参考实现\npass"

    @classmethod
    def _save_question(
        cls,
        connection: sqlite3.Connection,
        question: dict[str, Any],
        course_id: int,
        knowledge_point_uid: str | None,
        bank_id: int,
    ) -> int:
        question_id = cls._next_id(connection, "questions")
        score = {"easy": 4, "medium": 5, "hard": 8}.get(
            question["difficulty"], 5
        )
        connection.execute(
            """INSERT INTO questions(
                   id, title, content, question_type, difficulty, options,
                   correct_answer, explanation, score, source, status,
                   creator_id, language, usage_count, course_id
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, NULL, 0, ?)""",
            (
                question_id,
                question["title"],
                question["content"],
                question["question_type"],
                question["difficulty"],
                json.dumps(question["options"], ensure_ascii=False)
                if question["options"]
                else None,
                question["correct_answer"],
                "考查：",
                score,
                "ai",
                course_id,
            ),
        )
        connection.execute(
            "INSERT INTO question_bank_questions(question_bank_id, question_id) "
            "VALUES (?, ?)",
            (bank_id, question_id),
        )
        if knowledge_point_uid:
            connection.execute(
                """INSERT OR IGNORE INTO kg_resource_link(
                       course_id, node_uid, resource_type, resource_id,
                       link_type, weight
                   ) VALUES (?, ?, 'question', ?, 'tests', 1.0)""",
                (course_id, knowledge_point_uid, question_id),
            )
        return question_id

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
