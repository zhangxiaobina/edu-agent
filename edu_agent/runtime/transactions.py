from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from ..state.store import FencingTokenRejected, RunCancelled
from .cancellation import CancellationRequested
from .security import redact_sensitive


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def payload_hash(tool_name: str, arguments: dict) -> str:
    return hashlib.sha256(_json({"tool": tool_name, "arguments": arguments}).encode()).hexdigest()


def approval_scope(*, tenant_id: str, actor_id: str, tool_name: str, arguments: dict) -> str:
    course_id = arguments.get("course_id")
    return f"tenant:{tenant_id}:actor:{actor_id}:tool:{tool_name}:course:{course_id or '*'}"


def idempotency_key(
    *,
    tenant_id: str,
    actor_id: str,
    session_id: str,
    run_id: str,
    plan_step_id: str | None,
    tool_call_id: str | None,
    tool_name: str,
    arguments: dict,
    caller_key: str | None = None,
    replay_scope: str | None = None,
) -> str:
    if caller_key:
        source = {"tenant": tenant_id, "actor": actor_id, "caller_key": caller_key}
    elif replay_scope:
        source = {
            "tenant": tenant_id,
            "actor": actor_id,
            "replay_scope": replay_scope,
            "tool": tool_name,
            "arguments": arguments,
        }
    else:
        source = {
            "tenant": tenant_id,
            "actor": actor_id,
            "session": session_id,
            "run": run_id,
            "plan_step": plan_step_id or "",
            "tool_call": tool_call_id or "",
            "tool": tool_name,
            "arguments": arguments,
        }
    return hashlib.sha256(_json(source).encode()).hexdigest()


class OperationStatus(str, Enum):
    prepared = "prepared"
    approved = "approved"
    executing = "executing"
    committed = "committed"
    failed = "failed"
    compensating = "compensating"
    compensated = "compensated"
    manual_review = "manual_review"


class IdempotencyConflict(RuntimeError):
    pass


class OperationUnavailable(RuntimeError):
    pass


class InjectedFault(RuntimeError):
    pass


class FaultInjector:
    def hit(self, point: str, operation: dict | None = None) -> None:
        return None


class NamedFaultInjector(FaultInjector):
    def __init__(self, *points: str):
        self.points = set(points)

    def hit(self, point: str, operation: dict | None = None) -> None:
        if point in self.points:
            self.points.remove(point)
            raise InjectedFault(point)


@dataclass(frozen=True)
class OperationExecution:
    operation: dict
    result: dict
    replayed: bool


_OPERATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_operations (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_hash TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    plan_step_id TEXT,
    tool_call_id TEXT,
    status TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    result_json TEXT,
    snapshot_json TEXT,
    approval_scope TEXT NOT NULL,
    approved_by TEXT,
    approval_expires_at TEXT,
    last_error TEXT,
    compensation_attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    committed_at TEXT,
    compensated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tool_operations_owner
    ON tool_operations(tenant_id, actor_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_operations_call
    ON tool_operations(run_id, tool_call_id);

CREATE TABLE IF NOT EXISTS tool_approvals (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES tool_operations(id) ON DELETE CASCADE,
    payload_hash TEXT NOT NULL,
    scope TEXT NOT NULL,
    decision TEXT NOT NULL,
    approver_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tool_approvals_operation
    ON tool_approvals(operation_id, created_at DESC);

CREATE TABLE IF NOT EXISTS tool_outbox (
    event_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES tool_operations(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_until TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    published_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tool_outbox_pending
    ON tool_outbox(status, lease_until, created_at);

CREATE TABLE IF NOT EXISTS tool_consumer_events (
    consumer_name TEXT NOT NULL,
    event_id TEXT NOT NULL,
    consumed_at TEXT NOT NULL,
    PRIMARY KEY(consumer_name, event_id)
);
"""


def initialize_transaction_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(_OPERATION_SCHEMA)


def _decode_operation(row: sqlite3.Row | dict) -> dict:
    record = dict(row)
    for source, target in (
        ("arguments_json", "arguments"),
        ("result_json", "result"),
        ("snapshot_json", "snapshot"),
    ):
        value = record.pop(source, None)
        record[target] = json.loads(value) if value else None
    return record


class TransactionalToolRuntime:
    def __init__(self, *, state_store=None, fault_injector: FaultInjector | None = None):
        self.state_store = state_store
        self.faults = fault_injector or FaultInjector()

    def prepare(
        self,
        connection: sqlite3.Connection,
        *,
        key: str,
        digest: str,
        tool_name: str,
        arguments: dict,
        context,
        tool_call_id: str | None,
        plan_step_id: str | None,
        scope: str,
    ) -> dict:
        if self.state_store is not None:
            self.state_store.assert_run_writable(
                context,
                boundary="tool.operation.prepare",
            )
        initialize_transaction_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT * FROM tool_operations WHERE idempotency_key=?", (key,)
            ).fetchone()
            if row is not None:
                operation = _decode_operation(row)
                if operation["payload_hash"] != digest or operation["tool_name"] != tool_name:
                    raise IdempotencyConflict(
                        "相同 idempotency key 已绑定不同工具或规范化 payload"
                    )
                connection.commit()
                self._sync_ref(operation, context=context)
                return operation
            now = _now()
            operation_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO tool_operations(
                    id, idempotency_key, payload_hash, tool_name, tenant_id, actor_id,
                    session_id, run_id, plan_step_id, tool_call_id, status,
                    arguments_json, approval_scope, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    key,
                    digest,
                    tool_name,
                    context.tenant_id,
                    context.actor_id,
                    context.session_id,
                    context.run_id,
                    plan_step_id,
                    tool_call_id,
                    _json(redact_sensitive(arguments)),
                    scope,
                    now,
                    now,
                ),
            )
            connection.commit()
            operation = self._get_by_id(connection, operation_id)
        except BaseException:
            connection.rollback()
            raise
        self._sync_ref(operation, context=context)
        return operation

    def approve(
        self,
        connection: sqlite3.Connection,
        operation_id: str,
        *,
        digest: str,
        scope: str,
        approved: bool,
        approver_id: str,
        expires_at: str,
        context=None,
    ) -> dict:
        connection.execute("BEGIN IMMEDIATE")
        try:
            if self.state_store is not None and context is not None:
                self.state_store.assert_run_writable(
                    context,
                    boundary="tool.operation.approve",
                )
            operation = self._get_by_id(connection, operation_id)
            if operation["payload_hash"] != digest or operation["approval_scope"] != scope:
                raise IdempotencyConflict("审批 payload hash 或 scope 与 operation 不匹配")
            connection.execute(
                """
                INSERT INTO tool_approvals(
                    id, operation_id, payload_hash, scope, decision,
                    approver_id, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    operation_id,
                    digest,
                    scope,
                    "approved" if approved else "denied",
                    approver_id,
                    expires_at,
                    _now(),
                ),
            )
            if approved:
                connection.execute(
                    """
                    UPDATE tool_operations
                    SET status='approved', approved_by=?, approval_expires_at=?, updated_at=?
                    WHERE id=? AND status IN ('prepared', 'approved', 'failed')
                    """,
                    (approver_id, expires_at, _now(), operation_id),
                )
            connection.commit()
            operation = self._get_by_id(connection, operation_id)
        except BaseException:
            connection.rollback()
            raise
        self._sync_ref(operation, context=context)
        return operation

    def valid_approval(self, connection: sqlite3.Connection, operation: dict) -> bool:
        row = connection.execute(
            """
            SELECT 1 FROM tool_approvals
            WHERE operation_id=? AND payload_hash=? AND scope=?
                AND decision='approved' AND expires_at>?
            ORDER BY created_at DESC LIMIT 1
            """,
            (
                operation["id"],
                operation["payload_hash"],
                operation["approval_scope"],
                _now(),
            ),
        ).fetchone()
        return row is not None

    def execute(
        self,
        connection: sqlite3.Connection,
        operation: dict,
        handler: Callable[[], dict],
        context=None,
    ) -> OperationExecution:
        if context is not None:
            context.check_control("tool.operation.before_execute")
        current = self._get_by_id(connection, operation["id"])
        if current["status"] == OperationStatus.committed.value:
            return OperationExecution(current, current["result"], True)
        if current["status"] in {
            OperationStatus.executing.value,
            OperationStatus.compensating.value,
            OperationStatus.compensated.value,
            OperationStatus.manual_review.value,
        }:
            raise OperationUnavailable(f"operation 当前状态为 {current['status']}，不能重放写入")

        self.faults.hit("after_approval_before_business", current)
        connection.execute("BEGIN IMMEDIATE")
        try:
            if self.state_store is not None and context is not None:
                self.state_store.assert_run_writable(
                    context,
                    boundary="tool.operation.execute",
                )
            current = self._get_by_id(connection, operation["id"])
            if current["status"] == OperationStatus.committed.value:
                connection.commit()
                return OperationExecution(current, current["result"], True)
            connection.execute(
                "UPDATE tool_operations SET status='executing', last_error=NULL, updated_at=? WHERE id=?",
                (_now(), current["id"]),
            )
            snapshot = self._snapshot_before(connection, current["tool_name"], current["arguments"])
            connection.execute(
                "UPDATE tool_operations SET snapshot_json=? WHERE id=?",
                (_json(redact_sensitive(snapshot)), current["id"]),
            )
            fence_guard = (
                self.state_store.fenced_section(
                    context,
                    boundary="tool.operation.business_commit",
                )
                if self.state_store is not None and context is not None
                else nullcontext()
            )
            with fence_guard as fence_connection:
                result = handler()
                if context is not None:
                    context.check_control("tool.operation.after_handler")
                if not isinstance(result, dict):
                    raise TypeError("写工具必须返回 JSON object")
                if "error" in result:
                    raise RuntimeError(str(result["error"]))
                snapshot = self._snapshot_after(
                    connection,
                    current["tool_name"],
                    current["arguments"],
                    snapshot,
                    result,
                )
                self.faults.hit("after_business_write_before_operation_commit", current)
                now = _now()
                safe_result = redact_sensitive(result)
                connection.execute(
                    """
                    UPDATE tool_operations
                    SET status='committed', result_json=?, snapshot_json=?,
                        last_error=NULL, committed_at=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        _json(safe_result),
                        _json(redact_sensitive(snapshot)),
                        now,
                        now,
                        current["id"],
                    ),
                )
                event_id = uuid.uuid4().hex
                event = {
                    "event_id": event_id,
                    "operation_id": current["id"],
                    "type": "tool.operation.committed",
                    "tool_name": current["tool_name"],
                    "tenant_id": current["tenant_id"],
                    "actor_id": current["actor_id"],
                    "result": safe_result,
                }
                connection.execute(
                    """
                    INSERT INTO tool_outbox(
                        event_id, operation_id, event_type, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (event_id, current["id"], event["type"], _json(event), now),
                )
                connection.commit()
                committed = self._get_by_id(connection, current["id"])
                if self.state_store is not None:
                    self.state_store.upsert_tool_operation_ref(
                        committed,
                        context=context,
                        connection=fence_connection,
                    )
        except BaseException as error:
            connection.rollback()
            if not isinstance(
                error,
                (FencingTokenRejected, RunCancelled, CancellationRequested),
            ):
                self._mark_execution_failed(
                    connection,
                    current["id"],
                    error,
                    context=context,
                )
            raise
        committed = self._get_by_id(connection, current["id"])
        self.faults.hit("after_operation_commit_before_outbox_publish", committed)
        return OperationExecution(committed, committed["result"], False)

    def get_operation(self, connection: sqlite3.Connection, operation_id: str, *, context) -> dict:
        operation = self._get_by_id(connection, operation_id)
        self._authorize(operation, context)
        operation.pop("snapshot", None)
        return operation

    def get_compensation_snapshot(
        self, connection: sqlite3.Connection, operation_id: str, *, context
    ) -> dict | None:
        operation = self._get_by_id(connection, operation_id)
        self._authorize(operation, context)
        return operation["snapshot"]

    def compensate(
        self,
        connection: sqlite3.Connection,
        operation_id: str,
        *,
        context,
    ) -> dict:
        operation = self._get_by_id(connection, operation_id)
        self._authorize(operation, context)
        if operation["status"] == OperationStatus.compensated.value:
            return operation
        if operation["status"] not in {
            OperationStatus.committed.value,
            OperationStatus.compensating.value,
        }:
            raise OperationUnavailable(f"状态 {operation['status']} 不能补偿")
        if not self._supports_compensation(operation["tool_name"]):
            return self._manual_review(
                connection,
                operation_id,
                "工具没有安全补偿器",
                context=context,
            )

        if operation["status"] == OperationStatus.committed.value:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE tool_operations
                SET status='compensating', compensation_attempts=compensation_attempts+1,
                    updated_at=? WHERE id=?
                """,
                (_now(), operation_id),
            )
            connection.commit()
        else:
            connection.execute(
                "UPDATE tool_operations SET compensation_attempts=compensation_attempts+1 WHERE id=?",
                (operation_id,),
            )
            connection.commit()

        operation = self._get_by_id(connection, operation_id)
        self._sync_ref(operation, context=context)
        connection.execute("BEGIN IMMEDIATE")
        try:
            safe, reason = self._apply_compensation(connection, operation)
            if not safe:
                connection.rollback()
                return self._manual_review(
                    connection,
                    operation_id,
                    reason,
                    context=context,
                )
            self.faults.hit("during_compensation", operation)
            now = _now()
            connection.execute(
                """
                UPDATE tool_operations
                SET status='compensated', compensated_at=?, updated_at=?, last_error=NULL
                WHERE id=?
                """,
                (now, now, operation_id),
            )
            event_id = uuid.uuid4().hex
            event = {
                "event_id": event_id,
                "operation_id": operation_id,
                "type": "tool.operation.compensated",
                "tool_name": operation["tool_name"],
                "tenant_id": operation["tenant_id"],
                "actor_id": operation["actor_id"],
            }
            connection.execute(
                """
                INSERT INTO tool_outbox(
                    event_id, operation_id, event_type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (event_id, operation_id, event["type"], _json(event), now),
            )
            connection.commit()
        except BaseException as error:
            connection.rollback()
            connection.execute(
                "UPDATE tool_operations SET last_error=?, updated_at=? WHERE id=?",
                (f"{type(error).__name__}: {error}", _now(), operation_id),
            )
            connection.commit()
            raise
        compensated = self._get_by_id(connection, operation_id)
        self._sync_ref(compensated, context=context)
        if self.state_store is not None:
            self.state_store.reject_operation_evidence(
                operation_id,
                status=compensated["status"],
                context=context,
            )
        return compensated

    def _mark_execution_failed(
        self,
        connection: sqlite3.Connection,
        operation_id: str,
        error: BaseException,
        *,
        context=None,
    ) -> None:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE tool_operations SET status='failed', last_error=?, updated_at=?
            WHERE id=? AND status!='committed'
            """,
            (f"{type(error).__name__}: {error}", _now(), operation_id),
        )
        connection.commit()
        self._sync_ref(self._get_by_id(connection, operation_id), context=context)

    def _manual_review(
        self,
        connection: sqlite3.Connection,
        operation_id: str,
        reason: str,
        *,
        context=None,
    ) -> dict:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE tool_operations SET status='manual_review', last_error=?, updated_at=?
            WHERE id=?
            """,
            (reason, _now(), operation_id),
        )
        connection.commit()
        operation = self._get_by_id(connection, operation_id)
        self._sync_ref(operation, context=context)
        if self.state_store is not None:
            self.state_store.reject_operation_evidence(
                operation_id,
                status=operation["status"],
                context=context,
            )
        return operation

    @staticmethod
    def _get_by_id(connection: sqlite3.Connection, operation_id: str) -> dict:
        row = connection.execute(
            "SELECT * FROM tool_operations WHERE id=?", (operation_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"operation 不存在：{operation_id}")
        return _decode_operation(row)

    @staticmethod
    def _authorize(operation: dict, context) -> None:
        if operation["tenant_id"] != context.tenant_id or operation["actor_id"] != context.actor_id:
            raise PermissionError("operation 不属于当前 tenant/actor")

    def _sync_ref(self, operation: dict, *, context=None) -> None:
        if self.state_store is not None:
            self.state_store.upsert_tool_operation_ref(operation, context=context)

    @staticmethod
    def _supports_compensation(tool_name: str) -> bool:
        return tool_name in {"create_exam", "assign_homework", "batch_grade", "generate_questions"}

    @staticmethod
    def _snapshot_before(connection: sqlite3.Connection, tool_name: str, arguments: dict) -> dict:
        if tool_name == "batch_grade":
            rows = connection.execute(
                """
                SELECT id, score, correct_count, answer_count, status, passed, rank
                FROM exam_records WHERE exam_id=? ORDER BY id
                """,
                (arguments["exam_id"],),
            ).fetchall()
            return {"kind": "restore_exam_records", "before": [dict(row) for row in rows]}
        if tool_name == "create_exam":
            return {"kind": "delete_created_exam"}
        if tool_name == "assign_homework":
            return {"kind": "delete_created_homework"}
        if tool_name == "generate_questions":
            return {"kind": "delete_created_questions"}
        return {"kind": "manual_review"}

    @staticmethod
    def _snapshot_after(
        connection: sqlite3.Connection,
        tool_name: str,
        arguments: dict,
        snapshot: dict,
        result: dict,
    ) -> dict:
        updated = dict(snapshot)
        if tool_name == "create_exam":
            updated["exam_id"] = result.get("exam_id")
        elif tool_name == "assign_homework":
            updated["homework_id"] = result.get("homework_id")
        elif tool_name == "generate_questions":
            updated["question_ids"] = list(result.get("saved_question_ids") or [])
        elif tool_name == "batch_grade":
            rows = connection.execute(
                """
                SELECT id, score, correct_count, answer_count, status, passed, rank
                FROM exam_records WHERE exam_id=? ORDER BY id
                """,
                (arguments["exam_id"],),
            ).fetchall()
            updated["after"] = [dict(row) for row in rows]
        return updated

    def _apply_compensation(
        self, connection: sqlite3.Connection, operation: dict
    ) -> tuple[bool, str]:
        snapshot = operation.get("snapshot") or {}
        kind = snapshot.get("kind")
        if kind == "delete_created_exam":
            exam_id = snapshot.get("exam_id")
            dependent = sum(
                connection.execute(f"SELECT COUNT(*) FROM {table} WHERE exam_id=?", (exam_id,)).fetchone()[0]
                for table in ("exam_questions", "exam_records", "exam_answers")
            )
            if dependent:
                return False, "考试已有试题、作答或成绩记录，不能安全删除"
            connection.execute("DELETE FROM exams WHERE id=?", (exam_id,))
            return True, ""
        if kind == "delete_created_homework":
            homework_id = snapshot.get("homework_id")
            connection.execute("DELETE FROM homework_classes WHERE homework_id=?", (homework_id,))
            connection.execute("DELETE FROM homeworks WHERE id=?", (homework_id,))
            return True, ""
        if kind == "delete_created_questions":
            question_ids = snapshot.get("question_ids") or []
            for question_id in question_ids:
                for table in ("exam_questions", "exam_answers", "wrong_questions"):
                    if connection.execute(
                        f"SELECT 1 FROM {table} WHERE question_id=? LIMIT 1", (question_id,)
                    ).fetchone():
                        return False, f"题目 {question_id} 已被业务记录引用，不能安全删除"
            for question_id in question_ids:
                connection.execute("DELETE FROM kg_resource_link WHERE resource_type='question' AND resource_id=?", (question_id,))
                connection.execute("DELETE FROM question_bank_questions WHERE question_id=?", (question_id,))
                connection.execute("DELETE FROM questions WHERE id=?", (question_id,))
            return True, ""
        if kind == "restore_exam_records":
            after = {row["id"]: row for row in snapshot.get("after") or []}
            current_rows = connection.execute(
                """
                SELECT id, score, correct_count, answer_count, status, passed, rank
                FROM exam_records WHERE exam_id=? ORDER BY id
                """,
                (operation["arguments"]["exam_id"],),
            ).fetchall()
            current = {row["id"]: dict(row) for row in current_rows}
            if current != after:
                return False, "判分结果提交后又发生变化，不能覆盖后续写入"
            for row in snapshot.get("before") or []:
                connection.execute(
                    """
                    UPDATE exam_records
                    SET score=?, correct_count=?, answer_count=?, status=?, passed=?, rank=?
                    WHERE id=?
                    """,
                    (
                        row["score"],
                        row["correct_count"],
                        row["answer_count"],
                        row["status"],
                        row["passed"],
                        row["rank"],
                        row["id"],
                    ),
                )
            return True, ""
        return False, "缺少可验证的补偿快照"


class OutboxWorker:
    def __init__(
        self,
        connection_factory: Callable[[], sqlite3.Connection],
        *,
        worker_id: str | None = None,
        lease_seconds: int = 30,
        fault_injector: FaultInjector | None = None,
    ):
        self.connection_factory = connection_factory
        self.worker_id = worker_id or uuid.uuid4().hex
        self.lease_seconds = lease_seconds
        self.faults = fault_injector or FaultInjector()

    def run_once(self, publisher: Callable[[dict], None], *, limit: int = 10) -> list[dict]:
        events = self._claim(limit)
        results = []
        for event in events:
            stopped = threading.Event()
            heartbeat = threading.Thread(
                target=self._heartbeat_loop,
                args=(event["event_id"], stopped),
                name=f"edu-agent-outbox-{event['event_id'][:8]}",
                daemon=True,
            )
            heartbeat.start()
            try:
                try:
                    publisher(event)
                except Exception as error:
                    self._nack(event["event_id"], error)
                    results.append({"event_id": event["event_id"], "status": "pending"})
                    continue
                self.faults.hit("after_outbox_publish_before_ack", event)
                acknowledged = self._ack(event["event_id"])
                results.append(
                    {"event_id": event["event_id"], "status": "published" if acknowledged else "lost_lease"}
                )
            finally:
                stopped.set()
                heartbeat.join(timeout=1)
        return results

    def _claim(self, limit: int) -> list[dict]:
        connection = self.connection_factory()
        try:
            initialize_transaction_schema(connection)
            now = _now()
            lease_until = (datetime.now(UTC) + timedelta(seconds=self.lease_seconds)).isoformat()
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM tool_outbox
                WHERE status='pending'
                    OR (status='publishing' AND lease_until<?)
                ORDER BY created_at LIMIT ?
                """,
                (now, limit),
            ).fetchall()
            claimed = []
            for row in rows:
                connection.execute(
                    """
                    UPDATE tool_outbox
                    SET status='publishing', lease_owner=?, lease_until=?,
                        attempts=attempts+1, last_error=NULL
                    WHERE event_id=?
                    """,
                    (self.worker_id, lease_until, row["event_id"]),
                )
                record = dict(row)
                record.update(json.loads(record.pop("payload_json")))
                claimed.append(record)
            connection.commit()
            return claimed
        finally:
            connection.close()

    def heartbeat(self, event_id: str) -> bool:
        connection = self.connection_factory()
        try:
            lease_until = (datetime.now(UTC) + timedelta(seconds=self.lease_seconds)).isoformat()
            cursor = connection.execute(
                """
                UPDATE tool_outbox SET lease_until=?
                WHERE event_id=? AND status='publishing' AND lease_owner=?
                """,
                (lease_until, event_id, self.worker_id),
            )
            connection.commit()
            return cursor.rowcount == 1
        finally:
            connection.close()

    def _heartbeat_loop(self, event_id: str, stopped: threading.Event) -> None:
        interval = max(0.1, self.lease_seconds / 3)
        while not stopped.wait(interval):
            if not self.heartbeat(event_id):
                return

    def _ack(self, event_id: str) -> bool:
        connection = self.connection_factory()
        try:
            cursor = connection.execute(
                """
                UPDATE tool_outbox
                SET status='published', published_at=?, lease_owner=NULL, lease_until=NULL
                WHERE event_id=? AND status='publishing' AND lease_owner=?
                """,
                (_now(), event_id, self.worker_id),
            )
            connection.commit()
            return cursor.rowcount == 1
        finally:
            connection.close()

    def _nack(self, event_id: str, error: Exception) -> None:
        connection = self.connection_factory()
        try:
            connection.execute(
                """
                UPDATE tool_outbox
                SET status='pending', lease_owner=NULL, lease_until=NULL, last_error=?
                WHERE event_id=? AND lease_owner=?
                """,
                (f"{type(error).__name__}: {error}", event_id, self.worker_id),
            )
            connection.commit()
        finally:
            connection.close()


class IdempotentConsumer:
    @staticmethod
    def consume(
        connection: sqlite3.Connection,
        *,
        consumer_name: str,
        event: dict,
        handler: Callable[[dict], None],
    ) -> bool:
        initialize_transaction_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing = connection.execute(
                "SELECT 1 FROM tool_consumer_events WHERE consumer_name=? AND event_id=?",
                (consumer_name, event["event_id"]),
            ).fetchone()
            if existing:
                connection.commit()
                return False
            handler(event)
            connection.execute(
                """
                INSERT INTO tool_consumer_events(consumer_name, event_id, consumed_at)
                VALUES (?, ?, ?)
                """,
                (consumer_name, event["event_id"], _now()),
            )
            connection.commit()
            return True
        except BaseException:
            connection.rollback()
            raise
