from __future__ import annotations

import uuid
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from typing import Callable

from .runtime.security import redact_sensitive_text
from .state.store import StateStore


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat()


class JobStore:
    def __init__(self, state_store: StateStore):
        self.state_store = state_store

    def create(
        self,
        *,
        actor_id: str,
        tenant_id: str,
        role: str,
        name: str,
        prompt: str,
        next_run_at: datetime,
        interval_seconds: int | None = None,
        max_attempts: int = 3,
        retry_backoff_seconds: int = 60,
        idempotency_key: str | None = None,
    ) -> str:
        if not name.strip() or not prompt.strip():
            raise ValueError("任务名称和 Prompt 不能为空")
        if interval_seconds is not None and interval_seconds <= 0:
            raise ValueError("interval_seconds 必须大于 0")
        if max_attempts <= 0 or retry_backoff_seconds <= 0:
            raise ValueError("max_attempts 和 retry_backoff_seconds 必须大于 0")
        name = redact_sensitive_text(name.strip())
        prompt = redact_sensitive_text(prompt.strip())
        idempotency_key = idempotency_key.strip() if idempotency_key else None
        job_id = uuid.uuid4().hex
        now = _iso()
        with self.state_store.connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO scheduled_jobs(
                        id, actor_id, tenant_id, role, name, prompt, interval_seconds,
                        next_run_at, max_attempts, retry_backoff_seconds,
                        idempotency_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        actor_id,
                        tenant_id,
                        role,
                        name,
                        prompt,
                        interval_seconds,
                        _iso(next_run_at),
                        max_attempts,
                        retry_backoff_seconds,
                        idempotency_key,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                if idempotency_key is None or "UNIQUE constraint failed" not in str(error):
                    raise
                existing = connection.execute(
                    """
                    SELECT id, role, name, prompt, interval_seconds,
                        max_attempts, retry_backoff_seconds
                    FROM scheduled_jobs
                    WHERE actor_id=? AND tenant_id=? AND idempotency_key=?
                    """,
                    (actor_id, tenant_id, idempotency_key),
                ).fetchone()
                if existing is None:
                    raise
                expected = {
                    "role": role,
                    "name": name,
                    "prompt": prompt,
                    "interval_seconds": interval_seconds,
                    "max_attempts": max_attempts,
                    "retry_backoff_seconds": retry_backoff_seconds,
                }
                if any(existing[key] != value for key, value in expected.items()):
                    raise ValueError("idempotency_key 已用于不同的计划任务") from error
                return str(existing["id"])
        return job_id

    def claim_due(
        self,
        *,
        worker_id: str,
        now: datetime | None = None,
        limit: int = 10,
        lease_seconds: int = 300,
    ) -> list[dict]:
        current = now or datetime.now(UTC)
        now_text = _iso(current)
        lease_until = _iso(current + timedelta(seconds=lease_seconds))
        with self.state_store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM scheduled_jobs
                WHERE enabled=1 AND next_run_at<=?
                    AND cancel_requested=0
                    AND (lease_until IS NULL OR lease_until<?)
                ORDER BY next_run_at ASC
                LIMIT ?
                """,
                (now_text, now_text, limit),
            ).fetchall()
            claimed = []
            for row in rows:
                execution_key = row["execution_key"] or uuid.uuid4().hex
                connection.execute(
                    """
                    UPDATE scheduled_jobs
                    SET lease_owner=?, lease_until=?, status='running',
                        attempt_count=attempt_count+1, execution_key=?, updated_at=?
                    WHERE id=? AND enabled=1
                        AND cancel_requested=0
                        AND (lease_until IS NULL OR lease_until<?)
                    """,
                    (worker_id, lease_until, execution_key, now_text, row["id"], now_text),
                )
                if connection.execute("SELECT changes()").fetchone()[0]:
                    claimed_row = dict(row)
                    claimed_row["attempt_count"] = int(row["attempt_count"]) + 1
                    claimed_row["status"] = "running"
                    claimed_row["execution_key"] = execution_key
                    claimed.append(claimed_row)
            return claimed

    def heartbeat(
        self,
        job_id: str,
        *,
        worker_id: str,
        now: datetime | None = None,
        lease_seconds: int = 300,
    ) -> bool:
        current = now or datetime.now(UTC)
        with self.state_store.connect() as connection:
            connection.execute(
                """
                UPDATE scheduled_jobs SET lease_until=?, updated_at=?
                WHERE id=? AND lease_owner=? AND status='running'
                    AND cancel_requested=0
                """,
                (
                    _iso(current + timedelta(seconds=lease_seconds)),
                    _iso(current),
                    job_id,
                    worker_id,
                ),
            )
            return bool(connection.execute("SELECT changes()").fetchone()[0])

    def cancel(self, job_id: str, *, actor_id: str, tenant_id: str) -> bool:
        with self.state_store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE scheduled_jobs
                SET cancel_requested=1,
                    enabled=CASE WHEN status='running' THEN enabled ELSE 0 END,
                    status=CASE WHEN status='running' THEN status ELSE 'cancelled' END,
                    updated_at=?
                WHERE id=? AND actor_id=? AND tenant_id=?
                    AND status NOT IN ('success', 'cancelled', 'dead_letter')
                """,
                (_iso(), job_id, actor_id, tenant_id),
            )
            return bool(connection.execute("SELECT changes()").fetchone()[0])

    def is_cancel_requested(self, job_id: str, *, worker_id: str) -> bool:
        with self.state_store.connect() as connection:
            row = connection.execute(
                """
                SELECT cancel_requested FROM scheduled_jobs
                WHERE id=? AND lease_owner=?
                """,
                (job_id, worker_id),
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def complete(
        self,
        job: dict,
        *,
        worker_id: str,
        success: bool,
        result: str | None = None,
        error: str | None = None,
        now: datetime | None = None,
    ) -> None:
        result = redact_sensitive_text(result) if result is not None else None
        error = redact_sensitive_text(error) if error is not None else None
        current = now or datetime.now(UTC)
        with self.state_store.connect() as connection:
            current_row = connection.execute(
                """
                SELECT cancel_requested FROM scheduled_jobs
                WHERE id=? AND lease_owner=?
                """,
                (job["id"], worker_id),
            ).fetchone()
            if current_row is None:
                return
            cancelled = bool(current_row["cancel_requested"])
            if cancelled:
                enabled = 0
                status = "cancelled"
                next_run = current
            elif success:
                interval = job["interval_seconds"]
                enabled = 1 if interval else 0
                status = "pending" if interval else "success"
                next_run = current + timedelta(seconds=interval) if interval else current
            elif job["attempt_count"] < job["max_attempts"]:
                enabled = 1
                status = "retry_wait"
                delay = job["retry_backoff_seconds"] * 2 ** (job["attempt_count"] - 1)
                next_run = current + timedelta(seconds=delay)
            else:
                enabled = 0
                status = "dead_letter"
                next_run = current
            connection.execute(
                """
                UPDATE scheduled_jobs
                    SET enabled=?, next_run_at=?, lease_owner=NULL, lease_until=NULL,
                    status=?, last_status=?, last_result=?, last_error=?,
                    attempt_count=CASE WHEN ? AND interval_seconds IS NOT NULL
                        THEN 0 ELSE attempt_count END,
                    execution_key=CASE WHEN ? OR ? THEN NULL ELSE execution_key END,
                    updated_at=?
                WHERE id=? AND lease_owner=?
                """,
                (
                    enabled,
                    _iso(next_run),
                    status,
                    "success" if success else "cancelled" if cancelled else "failed",
                    result,
                    error,
                    success,
                    success,
                    cancelled,
                    _iso(current),
                    job["id"],
                    worker_id,
                ),
            )


class Scheduler:
    def __init__(
        self,
        state_store: StateStore,
        runner: Callable[[dict], str],
        *,
        worker_id: str | None = None,
        lease_seconds: int = 300,
    ):
        self.jobs = JobStore(state_store)
        self.runner = runner
        self.worker_id = worker_id or uuid.uuid4().hex
        self.lease_seconds = lease_seconds

    def tick(self, *, now: datetime | None = None, limit: int = 10) -> list[dict]:
        jobs = self.jobs.claim_due(
            worker_id=self.worker_id,
            now=now,
            limit=limit,
            lease_seconds=self.lease_seconds,
        )
        results = []
        for job in jobs:
            if self.jobs.is_cancel_requested(job["id"], worker_id=self.worker_id):
                self.jobs.complete(job, worker_id=self.worker_id, success=False, now=now)
                results.append({"job_id": job["id"], "status": "cancelled"})
                continue
            try:
                result = self._run_with_heartbeat(job)
                self.jobs.complete(
                    job,
                    worker_id=self.worker_id,
                    success=True,
                    result=result,
                    now=now,
                )
                with self.jobs.state_store.connect() as connection:
                    state = connection.execute(
                        "SELECT status FROM scheduled_jobs WHERE id=?",
                        (job["id"],),
                    ).fetchone()["status"]
                results.append({"job_id": job["id"], "status": state, "result": result})
            except Exception as error:
                self.jobs.complete(
                    job,
                    worker_id=self.worker_id,
                    success=False,
                    error=f"{type(error).__name__}: {error}",
                    now=now,
                )
                with self.jobs.state_store.connect() as connection:
                    state = connection.execute(
                        "SELECT status FROM scheduled_jobs WHERE id=?",
                        (job["id"],),
                    ).fetchone()["status"]
                results.append({"job_id": job["id"], "status": state, "error": str(error)})
        return results

    def _run_with_heartbeat(self, job: dict) -> str:
        stopped = threading.Event()
        interval = max(0.1, self.lease_seconds / 3)

        def renew() -> None:
            while not stopped.wait(interval):
                if not self.jobs.heartbeat(
                    job["id"],
                    worker_id=self.worker_id,
                    lease_seconds=self.lease_seconds,
                ):
                    return

        thread = threading.Thread(
            target=renew,
            name=f"edu-agent-job-heartbeat-{job['id'][:8]}",
            daemon=True,
        )
        thread.start()
        try:
            return self.runner(job)
        finally:
            stopped.set()
            thread.join(timeout=1)
