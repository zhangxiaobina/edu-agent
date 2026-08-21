from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

from ..runtime.security import redact_sensitive, redact_sensitive_text
from ..state.store import FencingTokenRejected, RunCancelled
from .models import DelegationBackpressure, DelegationLimitExceeded, SubtaskStatus


_TERMINAL = {
    SubtaskStatus.completed.value,
    SubtaskStatus.failed.value,
    SubtaskStatus.timed_out.value,
    SubtaskStatus.cancelled.value,
}

_ROLE_RANK = {"student": 0, "teacher": 1, "admin": 2, "system": 3}


def _role_rank(role: str) -> int:
    try:
        return _ROLE_RANK[role]
    except KeyError as error:
        raise PermissionError(f"未知 delegation role：{role}") from error


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _redacted_json(value: Any) -> str:
    return _json(redact_sensitive(value))


def _decode_run(row) -> dict[str, Any]:
    record = dict(row)
    for source, target in (
        ("course_ids_json", "course_ids"),
        ("allowed_tools_json", "allowed_tools"),
        ("allowed_categories_json", "allowed_categories"),
        ("budget_json", "budget"),
        ("usage_json", "usage"),
        ("task_json", "task_spec"),
        ("input_json", "input"),
        ("result_json", "result"),
    ):
        value = record.pop(source)
        record[target] = json.loads(value) if value else None
    record["can_delegate"] = bool(record["can_delegate"])
    return record


class DelegationState:
    def __init__(self, state_store):
        self.state_store = state_store
        self._initialize()

    def _initialize(self) -> None:
        with self.state_store.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS delegation_roots (
                    root_run_id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    session_id TEXT,
                    role TEXT,
                    course_ids_json TEXT,
                    budget_json TEXT NOT NULL,
                    reserved_json TEXT NOT NULL,
                    usage_json TEXT NOT NULL,
                    cancel_requested_at TEXT,
                    cancel_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS delegation_runs (
                    id TEXT PRIMARY KEY,
                    parent_run_id TEXT NOT NULL,
                    root_run_id TEXT NOT NULL
                        REFERENCES delegation_roots(root_run_id) ON DELETE CASCADE,
                    session_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    course_ids_json TEXT NOT NULL,
                    depth INTEGER NOT NULL,
                    task_key TEXT NOT NULL,
                    task_kind TEXT NOT NULL,
                    task TEXT NOT NULL,
                    task_json TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    plan_step_id TEXT,
                    status TEXT NOT NULL,
                    model TEXT NOT NULL,
                    allowed_tools_json TEXT NOT NULL,
                    allowed_categories_json TEXT NOT NULL,
                    can_delegate INTEGER NOT NULL DEFAULT 0,
                    budget_json TEXT NOT NULL,
                    usage_json TEXT NOT NULL,
                    result_json TEXT,
                    result_artifact_id TEXT,
                    failure_reason TEXT,
                    cancel_reason TEXT,
                    worker_owner TEXT,
                    worker_lease_expires_at TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    heartbeat_at TEXT,
                    finished_at TEXT,
                    UNIQUE(parent_run_id, task_key)
                );

                CREATE INDEX IF NOT EXISTS idx_delegation_runs_root
                    ON delegation_runs(root_run_id, created_at);

                CREATE INDEX IF NOT EXISTS idx_delegation_runs_active
                    ON delegation_runs(status, worker_lease_expires_at);
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(delegation_roots)").fetchall()
            }
            for name, definition in (
                ("session_id", "TEXT"),
                ("role", "TEXT"),
                ("course_ids_json", "TEXT"),
            ):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE delegation_roots ADD COLUMN {name} {definition}"
                    )
            connection.execute(
                """
                INSERT OR IGNORE INTO state_schema_migrations(version, applied_at)
                VALUES ('005_multi_agent_delegation', ?)
                """,
                (self.state_store.now_iso(),),
            )
            from ..state.trace_index import initialize_delegation_trace_index

            initialize_delegation_trace_index(connection)

    @staticmethod
    def _zero_usage() -> dict[str, int | float]:
        return {
            "model_calls": 0,
            "tool_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": 0.0,
        }

    @staticmethod
    def _zero_reservation() -> dict[str, int | float]:
        return {
            "model_calls": 0,
            "tool_calls": 0,
            "tokens": 0,
            "cost_usd": 0.0,
        }

    def create_batch(
        self,
        *,
        parent_context,
        entries: list[dict[str, Any]],
        root_budget: dict[str, int | float],
        child_budget: dict[str, int | float],
        max_depth: int,
        max_children_per_parent: int,
    ) -> list[dict[str, Any]]:
        if len({entry["task_spec"]["task_key"] for entry in entries}) != len(entries):
            raise DelegationLimitExceeded("同一批次 task_key 不能重复")
        now = self.state_store.now_iso()
        with self.state_store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            parent = connection.execute(
                "SELECT * FROM delegation_runs WHERE id=?",
                (parent_context.run_id,),
            ).fetchone()
            parent_is_delegated = parent is not None
            if parent is None:
                root_run_id = parent_context.run_id
                depth = 1
            else:
                root_run_id = parent["root_run_id"]
                depth = int(parent["depth"]) + 1
                if not bool(parent["can_delegate"]):
                    raise DelegationLimitExceeded("父 child policy 禁止继续委派")
                if (
                    parent["actor_id"] != parent_context.actor_id
                    or parent["tenant_id"] != parent_context.tenant_id
                    or parent["session_id"] != parent_context.session_id
                    or parent["role"] != parent_context.role
                    or set(json.loads(parent["course_ids_json"]))
                    != set(parent_context.course_ids)
                ):
                    raise PermissionError("委派父 run 不属于当前 actor/tenant")
            if depth > max_depth:
                raise DelegationLimitExceeded(
                    f"委派深度超过上限（{depth}/{max_depth}）"
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO delegation_roots(
                    root_run_id, actor_id, tenant_id, session_id, role,
                    course_ids_json, budget_json,
                    reserved_json, usage_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    root_run_id,
                    parent_context.actor_id,
                    parent_context.tenant_id,
                    parent_context.session_id,
                    parent_context.role,
                    _json(sorted(parent_context.course_ids)),
                    _json(root_budget),
                    _json(self._zero_reservation()),
                    _json(self._zero_usage()),
                    now,
                    now,
                ),
            )
            root = connection.execute(
                "SELECT * FROM delegation_roots WHERE root_run_id=?",
                (root_run_id,),
            ).fetchone()
            if (
                root["actor_id"] != parent_context.actor_id
                or root["tenant_id"] != parent_context.tenant_id
                or (
                    root["course_ids_json"]
                    and not set(parent_context.course_ids).issubset(
                        set(json.loads(root["course_ids_json"]))
                    )
                )
            ):
                raise PermissionError("delegation root scope 不属于当前 parent run")
            if root["role"] and _role_rank(root["role"]) < _role_rank(parent_context.role):
                raise PermissionError("delegation parent role 超出 root scope")
            if (
                not parent_is_delegated
                and root["session_id"]
                and root["session_id"] != parent_context.session_id
            ):
                raise PermissionError("delegation root session 不属于当前 parent run")
            if (
                not parent_is_delegated
                and root["role"]
                and root["role"] != parent_context.role
            ):
                raise PermissionError("delegation root role 不属于当前 parent run")
            if root["session_id"] is None or root["role"] is None or root["course_ids_json"] is None:
                connection.execute(
                    """
                    UPDATE delegation_roots
                    SET session_id=?, role=?, course_ids_json=?, updated_at=?
                    WHERE root_run_id=?
                    """,
                    (
                        parent_context.session_id,
                        parent_context.role,
                        _json(sorted(parent_context.course_ids)),
                        now,
                        root_run_id,
                    ),
                )
            if json.loads(root["budget_json"]) != root_budget:
                raise DelegationLimitExceeded("同一 root run 的 delegation budget 不可变更")
            if root["cancel_requested_at"]:
                raise RunCancelled(root["cancel_reason"] or "delegation root 已取消")

            existing_rows = connection.execute(
                "SELECT * FROM delegation_runs WHERE parent_run_id=?",
                (parent_context.run_id,),
            ).fetchall()
            existing_by_key = {row["task_key"]: row for row in existing_rows}
            new_entries = [
                entry
                for entry in entries
                if entry["task_spec"]["task_key"] not in existing_by_key
            ]
            if len(existing_rows) + len(new_entries) > max_children_per_parent:
                raise DelegationLimitExceeded(
                    "父 run child 数超过上限"
                    f"（{len(existing_rows) + len(new_entries)}/{max_children_per_parent}）"
                )
            for entry in entries:
                existing = existing_by_key.get(entry["task_spec"]["task_key"])
                if existing is None:
                    continue
                if (
                    existing["task_json"] != _redacted_json(entry["task_spec"])
                    or existing["input_json"] != _redacted_json(entry["input"])
                    or existing["allowed_tools_json"] != _json(entry["allowed_tools"])
                ):
                    raise DelegationLimitExceeded(
                        f"task_key {existing['task_key']} 已绑定不同任务"
                    )

            reserved = json.loads(root["reserved_json"])
            additions = {
                "model_calls": int(child_budget["max_model_calls"]) * len(new_entries),
                "tool_calls": int(child_budget["max_tool_calls"]) * len(new_entries),
                "tokens": int(child_budget["max_tokens"]) * len(new_entries),
                "cost_usd": float(child_budget["max_cost_usd"]) * len(new_entries),
            }
            proposed = {key: reserved[key] + additions[key] for key in reserved}
            limits = {
                "model_calls": int(root_budget["max_model_calls"]),
                "tool_calls": int(root_budget["max_tool_calls"]),
                "tokens": int(root_budget["max_tokens"]),
                "cost_usd": float(root_budget["max_cost_usd"]),
            }
            exceeded = [key for key, value in proposed.items() if value > limits[key]]
            if exceeded:
                raise DelegationLimitExceeded(
                    f"root delegation budget 预留失败：{sorted(exceeded)}"
                )

            for entry in new_entries:
                task_spec = entry["task_spec"]
                run_id = uuid.uuid4().hex
                connection.execute(
                    """
                    INSERT INTO delegation_runs(
                        id, parent_run_id, root_run_id, session_id, actor_id,
                        tenant_id, role, course_ids_json, depth, task_key,
                        task_kind, task, task_json, input_json, plan_step_id,
                        status, model, allowed_tools_json, allowed_categories_json,
                        can_delegate, budget_json, usage_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              'queued', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        parent_context.run_id,
                        root_run_id,
                        f"delegate-{run_id}",
                        parent_context.actor_id,
                        parent_context.tenant_id,
                        entry["role"],
                        _json(task_spec["course_ids"]),
                        depth,
                        task_spec["task_key"],
                        task_spec["kind"],
                        redact_sensitive_text(task_spec["task"]),
                        _redacted_json(task_spec),
                        _redacted_json(entry["input"]),
                        task_spec.get("plan_step_id"),
                        entry["model"],
                        _json(entry["allowed_tools"]),
                        _json(entry["allowed_categories"]),
                        int(entry["can_delegate"]),
                        _json(child_budget),
                        _json(self._zero_usage()),
                        now,
                    ),
                )
            if new_entries:
                connection.execute(
                    """
                    UPDATE delegation_roots
                    SET reserved_json=?, updated_at=? WHERE root_run_id=?
                    """,
                    (_json(proposed), now, root_run_id),
                )
            rows = connection.execute(
                """
                SELECT * FROM delegation_runs
                WHERE parent_run_id=? ORDER BY created_at, task_key
                """,
                (parent_context.run_id,),
            ).fetchall()
            requested = {entry["task_spec"]["task_key"] for entry in entries}
        return [_decode_run(row) for row in rows if row["task_key"] in requested]

    def claim(
        self,
        run_id: str,
        *,
        worker_owner: str,
        max_concurrency: int,
        lease_seconds: float,
    ) -> dict[str, Any] | None:
        now = self.state_store.now()
        now_iso = now.isoformat()
        expires = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.state_store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM delegation_runs WHERE id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"未知 delegated run：{run_id}")
            if row["status"] in _TERMINAL:
                return _decode_run(row)
            root = connection.execute(
                "SELECT * FROM delegation_roots WHERE root_run_id=?",
                (row["root_run_id"],),
            ).fetchone()
            if root["cancel_requested_at"] or row["status"] == "cancel_requested":
                connection.execute(
                    """
                    UPDATE delegation_runs
                    SET status='cancelled', cancel_reason=COALESCE(cancel_reason, ?),
                        finished_at=?, worker_owner=NULL, worker_lease_expires_at=NULL
                    WHERE id=?
                    """,
                    (root["cancel_reason"] or "PARENT_CANCELLED", now_iso, run_id),
                )
                return _decode_run(
                    connection.execute(
                        "SELECT * FROM delegation_runs WHERE id=?", (run_id,)
                    ).fetchone()
                )
            running = connection.execute(
                """
                SELECT COUNT(*) FROM delegation_runs
                WHERE status='running' AND worker_lease_expires_at>?
                """,
                (now_iso,),
            ).fetchone()[0]
            if running >= max_concurrency:
                return None
            cursor = connection.execute(
                """
                UPDATE delegation_runs
                SET status='running', worker_owner=?, worker_lease_expires_at=?,
                    started_at=COALESCE(started_at, ?), heartbeat_at=?
                WHERE id=? AND status='queued'
                """,
                (worker_owner, expires, now_iso, now_iso, run_id),
            )
            if cursor.rowcount != 1:
                raise DelegationBackpressure(f"delegated run {run_id} 当前不可领取")
            claimed = connection.execute(
                "SELECT * FROM delegation_runs WHERE id=?", (run_id,)
            ).fetchone()
        return _decode_run(claimed)

    def heartbeat(
        self,
        run_id: str,
        *,
        worker_owner: str,
        lease_seconds: float,
    ) -> bool:
        now = self.state_store.now()
        with self.state_store.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE delegation_runs
                SET heartbeat_at=?, worker_lease_expires_at=?
                WHERE id=? AND status='running' AND worker_owner=?
                    AND worker_lease_expires_at>?
                """,
                (
                    now.isoformat(),
                    (now + timedelta(seconds=lease_seconds)).isoformat(),
                    run_id,
                    worker_owner,
                    now.isoformat(),
                ),
            )
            return cursor.rowcount == 1

    def checkpoint(self, run_id: str, *, worker_owner: str, boundary: str) -> None:
        now = self.state_store.now_iso()
        with self.state_store.connect() as connection:
            row = connection.execute(
                """
                SELECT d.*, r.cancel_requested_at AS root_cancelled,
                       r.cancel_reason AS root_cancel_reason
                FROM delegation_runs d
                JOIN delegation_roots r ON r.root_run_id=d.root_run_id
                WHERE d.id=?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise FencingTokenRejected(f"{boundary}: delegated run 不存在")
            parent_run = connection.execute(
                "SELECT status FROM runs WHERE id=?",
                (row["root_run_id"],),
            ).fetchone()
        if row["root_cancelled"] or row["status"] == "cancel_requested":
            raise RunCancelled(
                row["cancel_reason"] or row["root_cancel_reason"] or "PARENT_CANCELLED"
            )
        if parent_run is not None and parent_run["status"] in {
            "cancel_requested",
            "interrupted",
            "failed",
            "abandoned",
        }:
            self.cancel_root(
                row["root_run_id"],
                actor_id=row["actor_id"],
                tenant_id=row["tenant_id"],
                reason=f"PARENT_RUN_{parent_run['status'].upper()}",
            )
            raise RunCancelled(f"父 run 状态为 {parent_run['status']}")
        if (
            row["status"] != "running"
            or row["worker_owner"] != worker_owner
            or not row["worker_lease_expires_at"]
            or row["worker_lease_expires_at"] <= now
        ):
            raise FencingTokenRejected(f"{boundary}: delegated worker lease 失效")

    def finish(
        self,
        run_id: str,
        *,
        worker_owner: str,
        status: SubtaskStatus,
        usage: dict[str, Any],
        result: dict[str, Any] | None,
        result_artifact_id: str | None = None,
        failure_reason: str | None = None,
        cancel_reason: str | None = None,
    ) -> dict[str, Any]:
        if status.value not in _TERMINAL:
            raise ValueError(f"非法 delegated run 终态：{status.value}")
        failure_reason = (
            redact_sensitive_text(failure_reason) if failure_reason is not None else None
        )
        cancel_reason = (
            redact_sensitive_text(cancel_reason) if cancel_reason is not None else None
        )
        now = self.state_store.now_iso()
        with self.state_store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM delegation_runs WHERE id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"未知 delegated run：{run_id}")
            if row["status"] in _TERMINAL:
                return _decode_run(row)
            if row["worker_owner"] != worker_owner:
                raise FencingTokenRejected("delegated run 终态提交 owner 不匹配")
            connection.execute(
                """
                UPDATE delegation_runs
                SET status=?, usage_json=?, result_json=?, result_artifact_id=?,
                    failure_reason=?, cancel_reason=?, finished_at=?,
                    worker_owner=NULL, worker_lease_expires_at=NULL
                WHERE id=? AND status IN ('running', 'cancel_requested')
                """,
                (
                    status.value,
                    _json(usage),
                    _redacted_json(result) if result is not None else None,
                    result_artifact_id,
                    failure_reason,
                    cancel_reason,
                    now,
                    run_id,
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise RuntimeError(f"delegated run {run_id} 不能结束")
            root = connection.execute(
                "SELECT usage_json FROM delegation_roots WHERE root_run_id=?",
                (row["root_run_id"],),
            ).fetchone()
            aggregate = json.loads(root["usage_json"])
            for key in aggregate:
                aggregate[key] += usage.get(key, 0)
            connection.execute(
                """
                UPDATE delegation_roots SET usage_json=?, updated_at=?
                WHERE root_run_id=?
                """,
                (_json(aggregate), now, row["root_run_id"]),
            )
            updated = connection.execute(
                "SELECT * FROM delegation_runs WHERE id=?", (run_id,)
            ).fetchone()
        return _decode_run(updated)

    def cancel_root(
        self,
        root_run_id: str,
        *,
        actor_id: str,
        tenant_id: str,
        reason: str,
    ) -> int:
        reason = redact_sensitive_text(reason)
        now = self.state_store.now_iso()
        with self.state_store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            root = connection.execute(
                "SELECT * FROM delegation_roots WHERE root_run_id=?",
                (root_run_id,),
            ).fetchone()
            if root is None:
                return 0
            if root["actor_id"] != actor_id or root["tenant_id"] != tenant_id:
                raise PermissionError("delegation root 不属于当前 actor/tenant")
            connection.execute(
                """
                UPDATE delegation_roots
                SET cancel_requested_at=COALESCE(cancel_requested_at, ?),
                    cancel_reason=COALESCE(cancel_reason, ?), updated_at=?
                WHERE root_run_id=?
                """,
                (now, reason, now, root_run_id),
            )
            queued = connection.execute(
                """
                UPDATE delegation_runs
                SET status='cancelled', cancel_reason=?, finished_at=?
                WHERE root_run_id=? AND status='queued'
                """,
                (reason, now, root_run_id),
            ).rowcount
            running = connection.execute(
                """
                UPDATE delegation_runs
                SET status='cancel_requested', cancel_reason=?
                WHERE root_run_id=? AND status='running'
                """,
                (reason, root_run_id),
            ).rowcount
        return queued + running

    def cancel_children(self, run_ids: set[str], *, reason: str) -> int:
        if not run_ids:
            return 0
        reason = redact_sensitive_text(reason)
        placeholders = ",".join("?" for _ in run_ids)
        now = self.state_store.now_iso()
        with self.state_store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            queued = connection.execute(
                f"""
                UPDATE delegation_runs SET status='cancelled', cancel_reason=?, finished_at=?
                WHERE id IN ({placeholders}) AND status='queued'
                """,
                (reason, now, *sorted(run_ids)),
            ).rowcount
            running = connection.execute(
                f"""
                UPDATE delegation_runs SET status='cancel_requested', cancel_reason=?
                WHERE id IN ({placeholders}) AND status='running'
                """,
                (reason, *sorted(run_ids)),
            ).rowcount
        return queued + running

    def reject_queued(self, run_id: str, reason: str) -> bool:
        reason = redact_sensitive_text(reason)
        now = self.state_store.now_iso()
        with self.state_store.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE delegation_runs
                SET status='failed', failure_reason=?, finished_at=?
                WHERE id=? AND status='queued'
                """,
                (reason, now, run_id),
            )
            return cursor.rowcount == 1

    def reject_completed_result(self, run_id: str, reason: str) -> bool:
        reason = redact_sensitive_text(reason)
        now = self.state_store.now_iso()
        with self.state_store.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE delegation_runs
                SET status='failed', failure_reason=?, finished_at=?
                WHERE id=? AND status='completed'
                """,
                (reason, now, run_id),
            )
            return cursor.rowcount == 1

    def fail_running_worker(self, run_id: str, *, worker_owner: str, reason: str) -> bool:
        reason = redact_sensitive_text(reason)
        now = self.state_store.now_iso()
        with self.state_store.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE delegation_runs
                SET status='failed', failure_reason=?, finished_at=?,
                    worker_owner=NULL, worker_lease_expires_at=NULL
                WHERE id=? AND status IN ('running', 'cancel_requested')
                    AND worker_owner=?
                """,
                (reason, now, run_id, worker_owner),
            )
            return cursor.rowcount == 1

    def recover_expired(self) -> list[dict[str, Any]]:
        now = self.state_store.now_iso()
        with self.state_store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM delegation_runs
                WHERE status IN ('running', 'cancel_requested')
                    AND worker_lease_expires_at<=?
                ORDER BY started_at
                """,
                (now,),
            ).fetchall()
            for row in rows:
                status = "cancelled" if row["status"] == "cancel_requested" else "failed"
                reason = (
                    row["cancel_reason"]
                    if status == "cancelled"
                    else "WORKER_LEASE_EXPIRED"
                )
                connection.execute(
                    """
                    UPDATE delegation_runs
                    SET status=?, failure_reason=?, cancel_reason=?, finished_at=?,
                        worker_owner=NULL, worker_lease_expires_at=NULL
                    WHERE id=?
                    """,
                    (
                        status,
                        reason if status == "failed" else None,
                        reason if status == "cancelled" else None,
                        now,
                        row["id"],
                    ),
                )
        return [
            {
                "run_id": row["id"],
                "status": "cancelled" if row["status"] == "cancel_requested" else "failed",
                "reason": row["cancel_reason"] or "WORKER_LEASE_EXPIRED",
            }
            for row in rows
        ]

    def get_run(
        self,
        run_id: str,
        *,
        actor_id: str,
        tenant_id: str,
    ) -> dict[str, Any] | None:
        with self.state_store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM delegation_runs WHERE id=?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        if row["actor_id"] != actor_id or row["tenant_id"] != tenant_id:
            raise PermissionError("delegated run 不属于当前 actor/tenant")
        return _decode_run(row)

    def tree(
        self,
        root_run_id: str,
        *,
        actor_id: str,
        tenant_id: str,
    ) -> dict[str, Any]:
        with self.state_store.connect() as connection:
            root = connection.execute(
                "SELECT * FROM delegation_roots WHERE root_run_id=?",
                (root_run_id,),
            ).fetchone()
            if root is None:
                return {
                    "root_run_id": root_run_id,
                    "nodes": [],
                    "usage": self._zero_usage(),
                }
            if root["actor_id"] != actor_id or root["tenant_id"] != tenant_id:
                raise PermissionError("delegation root 不属于当前 actor/tenant")
            rows = connection.execute(
                """
                SELECT * FROM delegation_runs
                WHERE root_run_id=? ORDER BY depth, created_at, task_key
                """,
                (root_run_id,),
            ).fetchall()
        return {
            "root_run_id": root_run_id,
            "actor_id": root["actor_id"],
            "tenant_id": root["tenant_id"],
            "session_id": root["session_id"],
            "role": root["role"],
            "course_ids": json.loads(root["course_ids_json"] or "[]"),
            "budget": json.loads(root["budget_json"]),
            "reserved": json.loads(root["reserved_json"]),
            "usage": json.loads(root["usage_json"]),
            "cancel_reason": root["cancel_reason"],
            "nodes": [_decode_run(row) for row in rows],
        }
