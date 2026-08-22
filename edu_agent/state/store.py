from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from .journal import (
    _UNSET as _JOURNAL_UNSET,
    RunJournalFencingError,
    RunJournalIdentityError,
    RunJournalSnapshot,
    compare_and_set_run_journal,
    get_run_journal_snapshot,
    initialize_run_journal,
    initialize_run_journal_schema,
)
from .tool_messages import (
    ToolMessagePairingError,
    append_assistant_tool_envelope,
    append_tool_result,
    complete_tool_batch,
    get_tool_call_record,
    initialize_agent_tool_message_schema,
    list_tool_call_records,
)


_UNSET = object()
STATE_SCHEMA_VERSION = 10


class SessionLeaseUnavailable(RuntimeError):
    pass


class FencingTokenRejected(RuntimeError):
    pass


class RunCancelled(RuntimeError):
    pass


class StateSchemaVersionError(RuntimeError):
    """The database was written by a newer state schema than this code knows."""

    pass


class _StateConnection(sqlite3.Connection):
    """让 ``with state_store.connect()`` 同时提交并释放文件描述符。"""

    def __exit__(self, exc_type, exc_value, traceback):
        result = super().__exit__(exc_type, exc_value, traceback)
        self.close()
        return result


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


class StateStore:
    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        read_only: bool = False,
    ):
        self.path = Path(path).expanduser()
        self._clock = clock or (lambda: datetime.now(UTC))
        self.read_only = read_only
        if read_only:
            if not self.path.is_file():
                raise FileNotFoundError(self.path)
            with self.connect() as connection:
                self._assert_supported_schema_version(connection)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()

    def now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def now_iso(self) -> str:
        return self.now().isoformat()

    def connect(self) -> sqlite3.Connection:
        target = f"file:{self.path.resolve()}?mode=ro" if self.read_only else self.path
        connection = sqlite3.connect(
            target,
            timeout=10,
            factory=_StateConnection,
            uri=self.read_only,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        if not self.read_only:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def _initialize(self) -> None:
        with self.connect() as connection:
            self._assert_supported_schema_version(connection)
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    role TEXT,
                    course_ids_json TEXT,
                    title TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT,
                    name TEXT,
                    tool_call_id TEXT,
                    tool_calls_json TEXT,
                    run_id TEXT,
                    fencing_token INTEGER,
                    active INTEGER NOT NULL DEFAULT 1,
                    compaction_id TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, sequence)
                );

                CREATE INDEX IF NOT EXISTS idx_messages_session_sequence
                    ON messages(session_id, sequence);

                CREATE TABLE IF NOT EXISTS context_checkpoints (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    summary TEXT NOT NULL,
                    first_sequence INTEGER NOT NULL,
                    last_sequence INTEGER NOT NULL,
                    source_messages INTEGER NOT NULL,
                    estimated_tokens_before INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_context_checkpoints_session
                    ON context_checkpoints(session_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    actor_id TEXT,
                    tenant_id TEXT,
                    role TEXT,
                    owner_id TEXT,
                    fencing_token INTEGER,
                    request_text TEXT,
                    model TEXT,
                    context_tokens INTEGER NOT NULL DEFAULT 0,
                    omitted_messages INTEGER NOT NULL DEFAULT 0,
                    budget_json TEXT,
                    error TEXT,
                    queued_at TEXT,
                    started_at TEXT NOT NULL,
                    heartbeat_at TEXT,
                    cancel_requested_at TEXT,
                    recovery_reason TEXT,
                    recovery_recommendation TEXT,
                    finished_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_runs_session_started
                    ON runs(session_id, started_at DESC);

                CREATE TABLE IF NOT EXISTS session_leases (
                    session_id TEXT PRIMARY KEY
                        REFERENCES sessions(id) ON DELETE CASCADE,
                    lease_owner TEXT,
                    fencing_token INTEGER NOT NULL,
                    active_run_id TEXT,
                    acquired_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_session_leases_active
                    ON session_leases(active_run_id, expires_at);

                CREATE TABLE IF NOT EXISTS tool_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    tool_call_id TEXT,
                    operation_id TEXT,
                    operation_status TEXT,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    outcome_json TEXT NOT NULL,
                    duration_ms REAL NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_tool_events_run
                    ON tool_events(run_id, id);

                CREATE TABLE IF NOT EXISTS tool_operation_refs (
                    operation_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    plan_step_id TEXT,
                    tool_call_id TEXT,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_tool_operation_refs_owner
                    ON tool_operation_refs(tenant_id, actor_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS plans (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    status TEXT NOT NULL,
                    max_iterations INTEGER NOT NULL,
                    iterations_used INTEGER NOT NULL DEFAULT 0,
                    failure_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_plans_owner_run
                    ON plans(tenant_id, actor_id, session_id, run_id);

                CREATE TABLE IF NOT EXISTS plan_steps (
                    plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
                    step_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    goal TEXT NOT NULL,
                    depends_on_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    allowed_tools_json TEXT NOT NULL,
                    expected_tools_json TEXT NOT NULL,
                    completion_conditions_json TEXT NOT NULL,
                    failure_reason TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    event_cursor INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(plan_id, step_id)
                );

                CREATE INDEX IF NOT EXISTS idx_plan_steps_ready
                    ON plan_steps(plan_id, status, position);

                CREATE TABLE IF NOT EXISTS evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
                    step_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    tool_name TEXT,
                    tool_event_id INTEGER,
                    operation_id TEXT,
                    artifact_id TEXT,
                    citation TEXT,
                    failure_reason TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_evidence_step
                    ON evidence(plan_id, step_id, id);

                CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_binding_unique
                    ON evidence(
                        plan_id, step_id, kind,
                        IFNULL(tool_event_id, -1),
                        IFNULL(artifact_id, ''),
                        IFNULL(citation, '')
                    );

                CREATE TABLE IF NOT EXISTS state_schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_artifacts_run
                    ON artifacts(run_id, created_at);

                CREATE TABLE IF NOT EXISTS provider_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    provider TEXT NOT NULL,
                    event TEXT NOT NULL,
                    error_class TEXT,
                    attempt INTEGER NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    scope_id TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    importance REAL NOT NULL DEFAULT 0.5,
                    source_session_id TEXT,
                    source TEXT NOT NULL DEFAULT 'explicit',
                    expires_at TEXT,
                    conflict_key TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_accessed_at TEXT,
                    UNIQUE(actor_id, tenant_id, scope, scope_id, kind, content)
                );

                CREATE INDEX IF NOT EXISTS idx_memories_owner
                    ON memories(tenant_id, actor_id, active, importance DESC);

                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_audit_actor_created
                    ON audit_events(tenant_id, actor_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS scheduled_jobs (
                    id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'teacher',
                    name TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    interval_seconds INTEGER,
                    next_run_at TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    lease_owner TEXT,
                    lease_until TEXT,
                    last_status TEXT,
                    last_result TEXT,
                    last_error TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    retry_backoff_seconds INTEGER NOT NULL DEFAULT 60,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    idempotency_key TEXT,
                    execution_key TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS api_requests (
                    actor_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    run_id TEXT,
                    owner_id TEXT,
                    lease_expires_at TEXT,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    response_json TEXT,
                    response_hash TEXT,
                    response_status INTEGER,
                    response_content_type TEXT,
                    response_headers_json TEXT,
                    error_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    claimed_at TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    failed_at TEXT,
                    retained_until TEXT,
                    PRIMARY KEY(actor_id, tenant_id, request_id)
                );

                CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_due
                    ON scheduled_jobs(enabled, next_run_at, lease_until);

                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO state_schema_migrations(version, applied_at)
                VALUES ('001_plan_graph_evidence', ?)
                """,
                (_now(),),
            )
            tool_event_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(tool_events)")
            }
            for column in ("tool_call_id", "operation_id", "operation_status"):
                if column not in tool_event_columns:
                    connection.execute(f"ALTER TABLE tool_events ADD COLUMN {column} TEXT")
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tool_events_call
                ON tool_events(run_id, tool_call_id)
                """
            )
            evidence_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(evidence)")
            }
            if "operation_id" not in evidence_columns:
                connection.execute("ALTER TABLE evidence ADD COLUMN operation_id TEXT")
            run_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(runs)")
            }
            run_migrations = {
                "actor_id": "TEXT",
                "tenant_id": "TEXT",
                "role": "TEXT",
                "owner_id": "TEXT",
                "fencing_token": "INTEGER",
                "request_text": "TEXT",
                "queued_at": "TEXT",
                "heartbeat_at": "TEXT",
                "cancel_requested_at": "TEXT",
                "recovery_reason": "TEXT",
                "recovery_recommendation": "TEXT",
            }
            for column, declaration in run_migrations.items():
                if column not in run_columns:
                    connection.execute(f"ALTER TABLE runs ADD COLUMN {column} {declaration}")
            connection.execute(
                """
                INSERT OR IGNORE INTO state_schema_migrations(version, applied_at)
                VALUES ('004_distributed_runtime_control', ?)
                """,
                (_now(),),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO state_schema_migrations(version, applied_at)
                VALUES ('003_transactional_tool_runtime', ?)
                """,
                (_now(),),
            )
            message_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(messages)")
            }
            if "active" not in message_columns:
                connection.execute(
                    "ALTER TABLE messages ADD COLUMN active INTEGER NOT NULL DEFAULT 1"
                )
            if "compaction_id" not in message_columns:
                connection.execute("ALTER TABLE messages ADD COLUMN compaction_id TEXT")
            if "run_id" not in message_columns:
                connection.execute("ALTER TABLE messages ADD COLUMN run_id TEXT")
            if "fencing_token" not in message_columns:
                connection.execute("ALTER TABLE messages ADD COLUMN fencing_token INTEGER")
            session_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(sessions)")
            }
            if "role" not in session_columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN role TEXT")
            if "course_ids_json" not in session_columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN course_ids_json TEXT")
            memory_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(memories)")
            }
            memory_migrations = {
                "source": "TEXT NOT NULL DEFAULT 'explicit'",
                "expires_at": "TEXT",
                "conflict_key": "TEXT",
            }
            for column, declaration in memory_migrations.items():
                if column not in memory_columns:
                    connection.execute(
                        f"ALTER TABLE memories ADD COLUMN {column} {declaration}"
                    )
            connection.execute(
                """
                INSERT OR IGNORE INTO state_schema_migrations(version, applied_at)
                VALUES ('002_course_rag_memory', ?)
                """,
                (_now(),),
            )
            job_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(scheduled_jobs)").fetchall()
            }
            if "role" not in job_columns:
                connection.execute(
                    "ALTER TABLE scheduled_jobs ADD COLUMN role TEXT NOT NULL DEFAULT 'teacher'"
                )
            job_migrations = {
                "status": "TEXT NOT NULL DEFAULT 'pending'",
                "max_attempts": "INTEGER NOT NULL DEFAULT 3",
                "attempt_count": "INTEGER NOT NULL DEFAULT 0",
                "retry_backoff_seconds": "INTEGER NOT NULL DEFAULT 60",
                "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
                "idempotency_key": "TEXT",
                "execution_key": "TEXT",
            }
            for column, declaration in job_migrations.items():
                if column not in job_columns:
                    connection.execute(
                        f"ALTER TABLE scheduled_jobs ADD COLUMN {column} {declaration}"
                    )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_jobs_idempotency
                ON scheduled_jobs(actor_id, tenant_id, idempotency_key)
                WHERE idempotency_key IS NOT NULL
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO state_schema_migrations(version, applied_at)
                VALUES ('007_observability_api', ?)
                """,
                (_now(),),
            )
            api_request_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(api_requests)").fetchall()
            }
            api_request_migrations = {
                "owner_id": "TEXT",
                "lease_expires_at": "TEXT",
                "attempt": "INTEGER NOT NULL DEFAULT 0",
                "response_hash": "TEXT",
                "response_status": "INTEGER",
                "response_content_type": "TEXT",
                "response_headers_json": "TEXT",
                "claimed_at": "TEXT",
                "started_at": "TEXT",
                "completed_at": "TEXT",
                "failed_at": "TEXT",
                "retained_until": "TEXT",
            }
            for column, declaration in api_request_migrations.items():
                if column not in api_request_columns:
                    connection.execute(
                        f"ALTER TABLE api_requests ADD COLUMN {column} {declaration}"
                    )
            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_api_requests_lease
                    ON api_requests(actor_id, tenant_id, status, lease_expires_at);
                CREATE INDEX IF NOT EXISTS idx_api_requests_retention
                    ON api_requests(actor_id, tenant_id, retained_until);
                CREATE TRIGGER IF NOT EXISTS api_requests_status_insert
                BEFORE INSERT ON api_requests
                WHEN NEW.status NOT IN (
                    'claimed', 'in_progress', 'completed', 'failed', 'stale', 'uncertain'
                ) BEGIN
                    SELECT RAISE(ABORT, 'invalid api request status');
                END;
                CREATE TRIGGER IF NOT EXISTS api_requests_status_transition
                BEFORE UPDATE OF status ON api_requests
                WHEN NOT (
                    OLD.status = NEW.status OR
                    (OLD.status = 'claimed' AND NEW.status IN (
                        'in_progress', 'completed', 'failed', 'stale', 'uncertain'
                    )) OR
                    (OLD.status = 'in_progress' AND NEW.status IN (
                        'completed', 'failed', 'stale', 'uncertain'
                    )) OR
                    (OLD.status = 'stale' AND NEW.status IN (
                        'claimed', 'in_progress', 'completed', 'failed', 'uncertain'
                    ))
                ) BEGIN
                    SELECT RAISE(ABORT, 'invalid api request status transition');
                END;
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO state_schema_migrations(version, applied_at)
                VALUES ('008_data_boundaries_api_trace', ?)
                """,
                (_now(),),
            )
            self._initialize_fts(connection)
            from .trace_index import initialize_trace_index

            initialize_trace_index(connection)
            initialize_run_journal_schema(connection, now=self.now_iso())
            initialize_agent_tool_message_schema(connection, now=self.now_iso())
            connection.execute(f"PRAGMA user_version = {STATE_SCHEMA_VERSION}")

    @staticmethod
    def _assert_supported_schema_version(connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > STATE_SCHEMA_VERSION:
            raise StateSchemaVersionError(
                f"state database schema version {version} is newer than supported "
                f"version {STATE_SCHEMA_VERSION}"
            )
        if not _table_exists(connection, "state_schema_migrations"):
            return
        rows = connection.execute(
            "SELECT version FROM state_schema_migrations"
        ).fetchall()
        for row in rows:
            value = str(row["version"])
            match = re.match(r"(\d+)", value)
            if match and int(match.group(1)) > STATE_SCHEMA_VERSION:
                raise StateSchemaVersionError(
                    f"state migration {value!r} is newer than supported version "
                    f"{STATE_SCHEMA_VERSION}"
                )

    @staticmethod
    def _initialize_fts(connection: sqlite3.Connection) -> None:
        try:
            connection.executescript(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    content,
                    content='memories',
                    content_rowid='id',
                    tokenize='unicode61'
                );

                CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                    INSERT INTO memory_fts(rowid, content) VALUES (new.id, new.content);
                END;

                CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                    INSERT INTO memory_fts(memory_fts, rowid, content)
                    VALUES ('delete', old.id, old.content);
                END;

                CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                    INSERT INTO memory_fts(memory_fts, rowid, content)
                    VALUES ('delete', old.id, old.content);
                    INSERT INTO memory_fts(rowid, content) VALUES (new.id, new.content);
                END;
                """
            )
        except sqlite3.OperationalError:
            pass

    def ensure_session(
        self,
        session_id: str,
        *,
        actor_id: str,
        tenant_id: str,
        role: str | None = None,
        course_ids: set[int] | frozenset[int] | None = None,
        title: str | None = None,
    ) -> None:
        from ..runtime.security import redact_sensitive_text

        now = _now()
        title = redact_sensitive_text(title) if title is not None else None
        normalized_courses = sorted(course_ids or ())
        courses_json = json.dumps(normalized_courses)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions(
                    id, actor_id, tenant_id, role, course_ids_json,
                    title, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    title=COALESCE(sessions.title, excluded.title),
                    role=COALESCE(sessions.role, excluded.role),
                    course_ids_json=COALESCE(sessions.course_ids_json, excluded.course_ids_json)
                """,
                (
                    session_id,
                    actor_id,
                    tenant_id,
                    role,
                    courses_json if role is not None else None,
                    title,
                    now,
                    now,
                ),
            )
            owner = connection.execute(
                """
                SELECT actor_id, tenant_id, role, course_ids_json
                FROM sessions WHERE id=?
                """,
                (session_id,),
            ).fetchone()
            if owner["actor_id"] != actor_id or owner["tenant_id"] != tenant_id:
                raise PermissionError("session 不属于当前 actor/tenant")
            if role is not None and owner["role"] != role:
                raise PermissionError("session role 在创建后不可变更")
            stored_courses = sorted(json.loads(owner["course_ids_json"] or "[]"))
            if role is not None and stored_courses != normalized_courses:
                raise PermissionError("session course scope 在创建后不可变更")

    def new_session_id(self) -> str:
        return uuid.uuid4().hex

    @staticmethod
    def _context_fence(context) -> tuple[str, str, int] | None:
        owner_id = getattr(context, "lease_owner", None)
        fencing_token = getattr(context, "fencing_token", None)
        if owner_id is None and fencing_token is None:
            return None
        if not owner_id or fencing_token is None:
            raise FencingTokenRejected("run lease identity 不完整")
        return context.run_id, owner_id, int(fencing_token)

    def _assert_fence(
        self,
        connection: sqlite3.Connection,
        context,
        *,
        boundary: str,
        allow_cancelled: bool = False,
    ) -> None:
        fence = self._context_fence(context)
        if fence is None:
            return
        run_id, owner_id, fencing_token = fence
        row = connection.execute(
            """
            SELECT l.lease_owner, l.fencing_token, l.active_run_id, l.expires_at,
                   r.status, r.actor_id, r.tenant_id
            FROM session_leases l
            JOIN runs r ON r.id=? AND r.session_id=l.session_id
            WHERE l.session_id=?
            """,
            (run_id, context.session_id),
        ).fetchone()
        if row is None:
            raise FencingTokenRejected(f"{boundary}: session lease 不存在")
        if (
            row["lease_owner"] != owner_id
            or int(row["fencing_token"]) != fencing_token
            or row["active_run_id"] != run_id
            or row["expires_at"] <= self.now_iso()
        ):
            raise FencingTokenRejected(f"{boundary}: lease 已过期或 fencing token 失效")
        if row["actor_id"] != context.actor_id or row["tenant_id"] != context.tenant_id:
            raise PermissionError(f"{boundary}: run 不属于当前 actor/tenant")
        if row["status"] == "cancel_requested" and not allow_cancelled:
            raise RunCancelled(f"{boundary}: run 已请求取消")
        if row["status"] not in {"running", "cancel_requested"}:
            raise FencingTokenRejected(f"{boundary}: run 状态 {row['status']} 不可写")

    def assert_run_writable(self, context, *, boundary: str) -> None:
        with self.connect() as connection:
            self._assert_fence(connection, context, boundary=boundary)

    @contextmanager
    def fenced_section(self, context, *, boundary: str):
        """Hold the state DB write reservation across an external commit."""
        if self._context_fence(context) is None:
            yield None
            return
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_fence(connection, context, boundary=boundary)
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def enqueue_run(self, context, *, request_text: str) -> None:
        from ..runtime.security import redact_sensitive_text

        now = self.now_iso()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO runs(
                    id, session_id, status, actor_id, tenant_id, role,
                    request_text, queued_at, started_at
                ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?)
                """,
                (
                    context.run_id,
                    context.session_id,
                    context.actor_id,
                    context.tenant_id,
                    context.role,
                    redact_sensitive_text(request_text),
                    now,
                    now,
                ),
            )

    # RunJournal is deliberately exposed through StateStore so callers cannot
    # accidentally create a second persistence/connection boundary.
    def create_run_journal(
        self,
        context=None,
        *,
        run_id: str | None = None,
        session_id: str | None = None,
        actor_id: str | None = None,
        tenant_id: str | None = None,
        tool_manifest_hash: str,
        frozen_provider_route: dict | None = None,
        provider_route: dict | None = None,
        budget_snapshot: dict,
        context_checkpoint_id: str | None = None,
        context_checkpoint: str | None = None,
        writer_id: str | None = None,
        fencing_token: int | None = None,
    ) -> RunJournalSnapshot:
        context = self._journal_context(
            context,
            run_id=run_id,
            session_id=session_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
            writer_id=writer_id,
            fencing_token=fencing_token,
        )
        if frozen_provider_route is None:
            frozen_provider_route = provider_route
        elif provider_route is not None:
            if frozen_provider_route != provider_route:
                raise ValueError("provider_route and frozen_provider_route disagree")
        if frozen_provider_route is None:
            raise ValueError("frozen_provider_route is required")
        if context_checkpoint is not None:
            if context_checkpoint_id is not None and context_checkpoint_id != context_checkpoint:
                raise ValueError("context checkpoint aliases disagree")
            context_checkpoint_id = context_checkpoint
        return initialize_run_journal(
            self,
            context,
            tool_manifest_hash=tool_manifest_hash,
            frozen_provider_route=frozen_provider_route,
            budget_snapshot=budget_snapshot,
            context_checkpoint_id=context_checkpoint_id,
            writer_id=writer_id,
            fencing_token=fencing_token,
        )

    initialize_run_journal = create_run_journal

    @staticmethod
    def _journal_context(
        context,
        *,
        run_id: str | None,
        session_id: str | None,
        actor_id: str | None,
        tenant_id: str | None,
        writer_id: str | None,
        fencing_token: int | None,
    ):
        if context is not None:
            if run_id is not None and run_id != context.run_id:
                raise RunJournalIdentityError("run_id does not match context")
            if session_id is not None and session_id != context.session_id:
                raise RunJournalIdentityError("session_id does not match context")
            if actor_id is not None and actor_id != context.actor_id:
                raise RunJournalIdentityError("actor_id does not match context")
            if tenant_id is not None and tenant_id != context.tenant_id:
                raise RunJournalIdentityError("tenant_id does not match context")
            context_writer = getattr(context, "lease_owner", None)
            context_token = getattr(context, "fencing_token", None)
            if context_writer is not None or context_token is not None:
                if writer_id is not None and writer_id != context_writer:
                    raise RunJournalFencingError("writer_id does not match context")
                if fencing_token is not None and fencing_token != context_token:
                    raise RunJournalFencingError("fencing_token does not match context")
            return context
        missing = [
            name
            for name, value in (
                ("run_id", run_id),
                ("session_id", session_id),
                ("actor_id", actor_id),
                ("tenant_id", tenant_id),
            )
            if not isinstance(value, str) or not value.strip()
        ]
        if missing:
            raise RunJournalIdentityError(
                "journal scope is incomplete: " + ", ".join(missing)
            )
        if writer_id is None or fencing_token is None:
            raise RunJournalFencingError(
                "journal writer_id and fencing_token are required"
            )
        return SimpleNamespace(
            run_id=run_id,
            session_id=session_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
            lease_owner=writer_id if fencing_token > 0 else None,
            fencing_token=fencing_token if fencing_token > 0 else None,
        )

    def get_run_journal_snapshot(
        self,
        run_id: str,
        *,
        session_id: str,
        actor_id: str,
        tenant_id: str,
    ) -> RunJournalSnapshot:
        return get_run_journal_snapshot(
            self,
            run_id=run_id,
            session_id=session_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
        )

    read_run_journal_snapshot = get_run_journal_snapshot
    get_run_journal = get_run_journal_snapshot

    def compare_and_set_run_journal(
        self,
        context=None,
        *,
        run_id: str | None = None,
        session_id: str | None = None,
        actor_id: str | None = None,
        tenant_id: str | None = None,
        expected_revision: int,
        expected_phase,
        phase,
        expected_loop_cursor: int,
        loop_cursor: int,
        expected_model_attempt: int,
        model_attempt: int,
        expected_event_sequence: int,
        event_sequence: int,
        expected_fencing_token: int,
        stable_boundary,
        budget_snapshot: dict,
        writer_id: str | None = None,
        fencing_token: int | None = None,
        context_checkpoint_id=_JOURNAL_UNSET,
        plan_id=_JOURNAL_UNSET,
        evidence_id=_JOURNAL_UNSET,
        operation_id=_JOURNAL_UNSET,
        artifact_id=_JOURNAL_UNSET,
        last_tool_event_id=_JOURNAL_UNSET,
    ) -> RunJournalSnapshot:
        context = self._journal_context(
            context,
            run_id=run_id,
            session_id=session_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
            writer_id=writer_id,
            fencing_token=fencing_token,
        )
        return compare_and_set_run_journal(
            self,
            context,
            expected_revision=expected_revision,
            expected_phase=expected_phase,
            phase=phase,
            expected_loop_cursor=expected_loop_cursor,
            loop_cursor=loop_cursor,
            expected_model_attempt=expected_model_attempt,
            model_attempt=model_attempt,
            expected_event_sequence=expected_event_sequence,
            event_sequence=event_sequence,
            expected_fencing_token=expected_fencing_token,
            stable_boundary=stable_boundary,
            budget_snapshot=budget_snapshot,
            context_checkpoint_id=context_checkpoint_id,
            plan_id=plan_id,
            evidence_id=evidence_id,
            operation_id=operation_id,
            artifact_id=artifact_id,
            last_tool_event_id=last_tool_event_id,
        )

    cas_run_journal = compare_and_set_run_journal
    compare_and_set_journal = compare_and_set_run_journal

    def prepare_run_resume(
        self,
        run_id: str,
        *,
        actor_id: str,
        tenant_id: str,
    ) -> dict:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"run 不存在：{run_id}")
            if row["actor_id"] != actor_id or row["tenant_id"] != tenant_id:
                raise PermissionError("run 不属于当前 actor/tenant")
            if row["status"] != "abandoned":
                raise RuntimeError(f"只有 abandoned run 可以恢复，当前为 {row['status']}")
            uncertain = connection.execute(
                """
                SELECT operation_id FROM tool_operation_refs
                WHERE run_id=? AND status IN ('executing', 'manual_review')
                """,
                (run_id,),
            ).fetchall()
            if uncertain:
                raise RuntimeError("run 存在不确定写操作，必须 manual_review")
            connection.execute(
                """
                UPDATE runs
                SET status='queued', owner_id=NULL, recovery_reason='operator_resume',
                    recovery_recommendation='resume_from_persistent_plan',
                    finished_at=NULL
                WHERE id=? AND status='abandoned'
                """,
                (run_id,),
            )
            return dict(connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())

    def acquire_session_lease(
        self,
        *,
        session_id: str,
        run_id: str,
        owner_id: str,
        actor_id: str,
        tenant_id: str,
        lease_seconds: float,
    ) -> dict:
        now = self.now()
        now_iso = now.isoformat()
        expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT actor_id, tenant_id, status FROM runs WHERE id=? AND session_id=?",
                (run_id, session_id),
            ).fetchone()
            if run is None:
                raise KeyError(f"run 不存在：{run_id}")
            if run["actor_id"] != actor_id or run["tenant_id"] != tenant_id:
                raise PermissionError("run 不属于当前 actor/tenant")
            lease = connection.execute(
                "SELECT * FROM session_leases WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if lease is not None and lease["active_run_id"] == run_id:
                if (
                    lease["lease_owner"] == owner_id
                    and lease["expires_at"] > now_iso
                ):
                    return dict(lease)
            if (
                lease is not None
                and lease["active_run_id"] is not None
                and lease["expires_at"] > now_iso
            ):
                raise SessionLeaseUnavailable(
                    f"session {session_id} 正由 {lease['lease_owner']} 执行"
                )
            if lease is not None and lease["active_run_id"] not in {None, run_id}:
                previous_run_id = lease["active_run_id"]
                previous = connection.execute(
                    "SELECT status FROM runs WHERE id=?",
                    (previous_run_id,),
                ).fetchone()
                if previous and previous["status"] in {"running", "cancel_requested"}:
                    terminal = (
                        "interrupted"
                        if previous["status"] == "cancel_requested"
                        else "abandoned"
                    )
                    uncertain = connection.execute(
                        """
                        SELECT 1 FROM tool_operation_refs
                        WHERE run_id=? AND status='executing' LIMIT 1
                        """,
                        (previous_run_id,),
                    ).fetchone()
                    recommendation = (
                        "manual_review" if uncertain else "resume_from_persistent_plan"
                    )
                    if uncertain:
                        connection.execute(
                            """
                            UPDATE tool_operation_refs
                            SET status='manual_review', updated_at=?
                            WHERE run_id=? AND status='executing'
                            """,
                            (now_iso, previous_run_id),
                        )
                    connection.execute(
                        """
                        UPDATE runs
                        SET status=?, finished_at=?, recovery_reason='session_lease_reclaimed',
                            recovery_recommendation=?
                        WHERE id=?
                        """,
                        (terminal, now_iso, recommendation, previous_run_id),
                    )
            fencing_token = int(lease["fencing_token"]) + 1 if lease else 1
            connection.execute(
                """
                INSERT INTO session_leases(
                    session_id, lease_owner, fencing_token, active_run_id,
                    acquired_at, heartbeat_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    lease_owner=excluded.lease_owner,
                    fencing_token=excluded.fencing_token,
                    active_run_id=excluded.active_run_id,
                    acquired_at=excluded.acquired_at,
                    heartbeat_at=excluded.heartbeat_at,
                    expires_at=excluded.expires_at
                """,
                (
                    session_id,
                    owner_id,
                    fencing_token,
                    run_id,
                    now_iso,
                    now_iso,
                    expires_at,
                ),
            )
            connection.execute(
                """
                UPDATE runs
                SET status='running', owner_id=?, fencing_token=?, started_at=?,
                    heartbeat_at=?, finished_at=NULL, recovery_reason=NULL,
                    recovery_recommendation=NULL
                WHERE id=? AND status IN ('queued', 'running')
                """,
                (owner_id, fencing_token, now_iso, now_iso, run_id),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise RuntimeError(f"run 状态不可领取：{run_id}")
            row = connection.execute(
                "SELECT * FROM session_leases WHERE session_id=?",
                (session_id,),
            ).fetchone()
        return dict(row)

    def heartbeat_session_lease(
        self,
        *,
        session_id: str,
        run_id: str,
        owner_id: str,
        fencing_token: int,
        lease_seconds: float,
    ) -> bool:
        now = self.now()
        now_iso = now.isoformat()
        expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE session_leases
                SET heartbeat_at=?, expires_at=?
                WHERE session_id=? AND active_run_id=? AND lease_owner=?
                    AND fencing_token=? AND expires_at>?
                """,
                (
                    now_iso,
                    expires_at,
                    session_id,
                    run_id,
                    owner_id,
                    fencing_token,
                    now_iso,
                ),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                """
                UPDATE runs SET heartbeat_at=?
                WHERE id=? AND owner_id=? AND fencing_token=?
                    AND status IN ('running', 'cancel_requested')
                """,
                (now_iso, run_id, owner_id, fencing_token),
            )
            return connection.execute("SELECT changes()").fetchone()[0] == 1

    def release_session_lease(
        self,
        *,
        session_id: str,
        run_id: str,
        owner_id: str,
        fencing_token: int,
    ) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE session_leases
                SET lease_owner=NULL, active_run_id=NULL, expires_at=?, heartbeat_at=?
                WHERE session_id=? AND active_run_id=? AND lease_owner=?
                    AND fencing_token=?
                """,
                (
                    self.now_iso(),
                    self.now_iso(),
                    session_id,
                    run_id,
                    owner_id,
                    fencing_token,
                ),
            )
            return cursor.rowcount == 1

    def append_messages(
        self,
        session_id: str,
        messages: list[dict],
        *,
        context=None,
    ) -> None:
        from ..runtime.security import redact_sensitive

        if not messages:
            return
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if context is not None:
                self._assert_fence(connection, context, boundary="messages.commit")
                has_journal = connection.execute(
                    "SELECT 1 FROM run_journals WHERE run_id=?",
                    (context.run_id,),
                ).fetchone()
                if has_journal is not None and any(
                    message.get("role") == "tool" or message.get("tool_calls")
                    for message in messages
                ):
                    raise ToolMessagePairingError(
                        "tool protocol messages must use the atomic paired append API",
                        run_id=context.run_id,
                    )
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), -1) AS sequence FROM messages WHERE session_id=?",
                (session_id,),
            ).fetchone()
            sequence = row["sequence"] + 1
            now = _now()
            for message in messages:
                message = redact_sensitive(message)
                idempotency_key = message.get("idempotency_key")
                if idempotency_key is not None:
                    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
                        raise ValueError("message idempotency_key must be a non-empty string")
                    if context is None:
                        raise ValueError("message idempotency_key requires a run context")
                    existing = connection.execute(
                        "SELECT * FROM messages WHERE run_id=? AND idempotency_key=?",
                        (getattr(context, "run_id", None), idempotency_key),
                    ).fetchone()
                    if existing is not None:
                        existing_calls = (
                            json.loads(existing["tool_calls_json"])
                            if existing["tool_calls_json"]
                            else None
                        )
                        if (
                            existing["role"] != message.get("role", "")
                            or (existing["content"] or "") != (message.get("content") or "")
                            or existing["name"] != message.get("name")
                            or existing["tool_call_id"] != message.get("tool_call_id")
                            or existing_calls != message.get("tool_calls")
                        ):
                            raise ValueError("message idempotency_key 已绑定不同消息")
                        continue
                try:
                    connection.execute(
                        """
                        INSERT INTO messages(
                            session_id, sequence, role, content, name, tool_call_id,
                            tool_calls_json, run_id, fencing_token, idempotency_key,
                            model_attempt, loop_cursor, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            session_id,
                            sequence,
                            message.get("role", ""),
                            message.get("content"),
                            message.get("name"),
                            message.get("tool_call_id"),
                            json.dumps(message.get("tool_calls"), ensure_ascii=False)
                            if message.get("tool_calls") is not None
                            else None,
                            getattr(context, "run_id", None),
                            getattr(context, "fencing_token", None),
                            idempotency_key,
                            message.get("model_attempt"),
                            message.get("loop_cursor"),
                            now,
                        ),
                    )
                except sqlite3.IntegrityError:
                    if idempotency_key is None:
                        raise
                    existing = connection.execute(
                        "SELECT * FROM messages WHERE run_id=? AND idempotency_key=?",
                        (getattr(context, "run_id", None), idempotency_key),
                    ).fetchone()
                    if existing is None:
                        raise
                    existing_calls = (
                        json.loads(existing["tool_calls_json"])
                        if existing["tool_calls_json"]
                        else None
                    )
                    if (
                        existing["role"] != message.get("role", "")
                        or (existing["content"] or "") != (message.get("content") or "")
                        or existing["name"] != message.get("name")
                        or existing["tool_call_id"] != message.get("tool_call_id")
                        or existing_calls != message.get("tool_calls")
                    ):
                        raise ValueError("message idempotency_key 已绑定不同消息")
                    continue
                sequence += 1
            connection.execute(
                "UPDATE sessions SET updated_at=? WHERE id=?",
                (now, session_id),
            )

    def append_assistant_tool_envelope(self, context, message: dict, *, model_attempt: int):
        return append_assistant_tool_envelope(
            self,
            context,
            message,
            model_attempt=model_attempt,
        )

    def append_tool_result(
        self,
        context,
        message: dict,
        *,
        model_attempt: int,
        operation_id: str | None = None,
        tool_event_id: int | None = None,
        allow_cancelled: bool = False,
    ):
        return append_tool_result(
            self,
            context,
            message,
            model_attempt=model_attempt,
            operation_id=operation_id,
            tool_event_id=tool_event_id,
            allow_cancelled=allow_cancelled,
        )

    def complete_tool_batch(self, context, *, model_attempt: int):
        return complete_tool_batch(self, context, model_attempt=model_attempt)

    def get_tool_call_record(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        session_id: str,
        actor_id: str,
        tenant_id: str,
    ):
        return get_tool_call_record(
            self,
            run_id=run_id,
            tool_call_id=tool_call_id,
            session_id=session_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
        )

    def list_tool_call_records(
        self,
        *,
        run_id: str,
        session_id: str,
        actor_id: str,
        tenant_id: str,
    ):
        return list_tool_call_records(
            self,
            run_id=run_id,
            session_id=session_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
        )

    def get_messages(
        self,
        session_id: str,
        limit: int | None = 200,
        *,
        include_compacted: bool = False,
    ) -> list[dict]:
        active_clause = "" if include_compacted else "AND active=1"
        with self.connect() as connection:
            if limit is None:
                rows = connection.execute(
                    f"""
                    SELECT role, content, name, tool_call_id, tool_calls_json
                    FROM messages
                    WHERE session_id=? {active_clause}
                    ORDER BY sequence ASC
                    """,
                    (session_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    f"""
                    SELECT role, content, name, tool_call_id, tool_calls_json
                    FROM (
                        SELECT * FROM messages
                        WHERE session_id=? {active_clause}
                        ORDER BY sequence DESC
                        LIMIT ?
                    ) ORDER BY sequence ASC
                    """,
                    (session_id, limit),
                ).fetchall()
        messages = []
        for row in rows:
            message = {"role": row["role"], "content": row["content"] or ""}
            if row["name"]:
                message["name"] = row["name"]
            if row["tool_call_id"]:
                message["tool_call_id"] = row["tool_call_id"]
            if row["tool_calls_json"]:
                message["tool_calls"] = json.loads(row["tool_calls_json"])
            messages.append(message)
        return messages

    def get_run_messages(self, run_id: str) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT role, content, name, tool_call_id, tool_calls_json
                FROM messages WHERE run_id=? ORDER BY sequence ASC
                """,
                (run_id,),
            ).fetchall()
        messages = []
        for row in rows:
            message = {"role": row["role"], "content": row["content"] or ""}
            if row["name"]:
                message["name"] = row["name"]
            if row["tool_call_id"]:
                message["tool_call_id"] = row["tool_call_id"]
            if row["tool_calls_json"]:
                message["tool_calls"] = json.loads(row["tool_calls_json"])
            messages.append(message)
        return messages

    def latest_context_checkpoint(self, session_id: str) -> dict | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM context_checkpoints
                WHERE session_id=? ORDER BY created_at DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def compact_messages(
        self,
        session_id: str,
        *,
        summary: str,
        message_count: int,
        estimated_tokens_before: int,
        active_message_count: int | None = None,
        context=None,
    ) -> dict:
        from ..runtime.security import redact_sensitive_text

        if message_count <= 0:
            raise ValueError("message_count 必须大于 0")
        summary = redact_sensitive_text(summary)
        checkpoint_id = uuid.uuid4().hex
        now = _now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if context is not None:
                self._assert_fence(connection, context, boundary="context.compact")
            if active_message_count is not None:
                actual_count = connection.execute(
                    "SELECT COUNT(*) FROM messages WHERE session_id=? AND active=1",
                    (session_id,),
                ).fetchone()[0]
                if actual_count != active_message_count:
                    raise RuntimeError("待压缩消息已被并发修改")
            rows = connection.execute(
                """
                SELECT sequence FROM messages
                WHERE session_id=? AND active=1
                ORDER BY sequence ASC LIMIT ?
                """,
                (session_id, message_count),
            ).fetchall()
            if len(rows) != message_count:
                raise RuntimeError("待压缩消息已被并发修改")
            first_sequence = int(rows[0]["sequence"])
            last_sequence = int(rows[-1]["sequence"])
            connection.execute(
                """
                UPDATE messages SET active=0, compaction_id=?
                WHERE session_id=? AND active=1
                    AND sequence BETWEEN ? AND ?
                """,
                (checkpoint_id, session_id, first_sequence, last_sequence),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != message_count:
                raise RuntimeError("上下文压缩提交发生并发冲突")
            connection.execute(
                """
                INSERT INTO context_checkpoints(
                    id, session_id, summary, first_sequence, last_sequence,
                    source_messages, estimated_tokens_before, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_id,
                    session_id,
                    summary,
                    first_sequence,
                    last_sequence,
                    message_count,
                    estimated_tokens_before,
                    now,
                ),
            )
        return {
            "id": checkpoint_id,
            "summary": summary,
            "source_messages": message_count,
            "first_sequence": first_sequence,
            "last_sequence": last_sequence,
        }

    def record_artifact(
        self,
        *,
        artifact_id: str,
        run_id: str,
        session_id: str,
        actor_id: str,
        tenant_id: str,
        kind: str,
        path: str,
        sha256: str,
        size_bytes: int,
        metadata: dict,
        context=None,
    ) -> None:
        from ..runtime.security import redact_sensitive

        metadata = redact_sensitive(metadata)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if context is not None:
                self._assert_fence(connection, context, boundary="artifact.commit")
            connection.execute(
                """
                INSERT INTO artifacts(
                    id, run_id, session_id, actor_id, tenant_id, kind,
                    path, sha256, size_bytes, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    run_id,
                    session_id,
                    actor_id,
                    tenant_id,
                    kind,
                    path,
                    sha256,
                    size_bytes,
                    json.dumps(metadata, ensure_ascii=False, default=str),
                    _now(),
                ),
            )

    def get_artifact(
        self,
        artifact_id: str,
        *,
        actor_id: str,
        tenant_id: str,
    ) -> dict | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM artifacts
                WHERE id=? AND actor_id=? AND tenant_id=?
                """,
                (artifact_id, actor_id, tenant_id),
            ).fetchone()
        return dict(row) if row else None

    def verify_artifact(
        self,
        artifact_id: str,
        *,
        actor_id: str,
        tenant_id: str,
        run_id: str,
        session_id: str,
    ) -> dict | None:
        """Return an artifact only when ownership and on-disk integrity match."""
        record = self.get_artifact(
            artifact_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
        )
        if record is None:
            return None
        if record["run_id"] != run_id or record["session_id"] != session_id:
            return None
        try:
            path = Path(record["path"]).resolve()
            payload = path.read_bytes()
        except (OSError, ValueError):
            return None
        if len(payload) != int(record["size_bytes"]):
            return None
        if hashlib.sha256(payload).hexdigest() != record["sha256"]:
            return None
        return record

    def record_provider_event(
        self,
        *,
        provider: str,
        event: str,
        attempt: int,
        run_id: str | None = None,
        error_class: str | None = None,
        details: dict | None = None,
    ) -> None:
        from ..runtime.security import redact_sensitive

        details = redact_sensitive(details or {})
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO provider_events(
                    run_id, provider, event, error_class, attempt,
                    details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    provider,
                    event,
                    error_class,
                    attempt,
                    json.dumps(details or {}, ensure_ascii=False, default=str),
                    _now(),
                ),
            )

    def start_run(
        self,
        *,
        run_id: str,
        session_id: str,
        model: str,
        context_tokens: int,
        omitted_messages: int,
        context=None,
    ) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if context is not None:
                self._assert_fence(connection, context, boundary="run.start")
            cursor = connection.execute(
                """
                UPDATE runs
                SET model=?, context_tokens=?, omitted_messages=?
                WHERE id=? AND session_id=?
                """,
                (
                    model,
                    context_tokens,
                    omitted_messages,
                    run_id,
                    session_id,
                ),
            )
            if cursor.rowcount == 0:
                now = self.now_iso()
                connection.execute(
                    """
                    INSERT INTO runs(
                        id, session_id, status, model, context_tokens,
                        omitted_messages, queued_at, started_at, heartbeat_at
                    ) VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        session_id,
                        model,
                        context_tokens,
                        omitted_messages,
                        now,
                        now,
                        now,
                    ),
                )

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        budget: dict,
        error: str | None = None,
        recovery_reason: str | None = None,
        context=None,
    ) -> None:
        from ..runtime.security import redact_sensitive_text

        if status not in {"interrupted", "completed", "failed", "abandoned"}:
            raise ValueError(f"非法终态：{status}")
        error = redact_sensitive_text(error) if error is not None else None
        recovery_reason = (
            redact_sensitive_text(recovery_reason) if recovery_reason is not None else None
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if context is not None:
                self._assert_fence(
                    connection,
                    context,
                    boundary="run.finish",
                    allow_cancelled=status == "interrupted",
                )
            cursor = connection.execute(
                """
                UPDATE runs
                SET status=?, budget_json=?, error=?, recovery_reason=?, finished_at=?
                WHERE id=? AND status IN ('running', 'cancel_requested', 'queued')
                """,
                (
                    status,
                    json.dumps(budget),
                    error,
                    recovery_reason,
                    self.now_iso(),
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"run {run_id} 不能从当前状态结束")

    def cancel_run(self, run_id: str, *, actor_id: str, tenant_id: str) -> bool:
        now = self.now_iso()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                return False
            if run["actor_id"] != actor_id or run["tenant_id"] != tenant_id:
                raise PermissionError("run 不属于当前 actor/tenant")
            if run["status"] in {"interrupted", "completed", "failed", "abandoned"}:
                return False
            cursor = connection.execute(
                """
                UPDATE runs
                SET status='cancel_requested', cancel_requested_at=?,
                    recovery_reason='actor_requested_cancel'
                WHERE id=? AND status IN ('queued', 'running')
                """,
                (now, run_id),
            )
            return cursor.rowcount == 1

    def get_run_status(self, run_id: str, *, actor_id: str, tenant_id: str) -> dict | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                return None
            if row["actor_id"] != actor_id or row["tenant_id"] != tenant_id:
                raise PermissionError("run 不属于当前 actor/tenant")
            record = dict(row)
            lease = connection.execute(
                "SELECT * FROM session_leases WHERE active_run_id=?",
                (run_id,),
            ).fetchone()
            record["cancel_requested"] = record["status"] == "cancel_requested"
            record["current_owner"] = lease["lease_owner"] if lease else None
            record["lease_remaining_seconds"] = self._lease_remaining(lease)
            record["recovery_recommendation"] = (
                record["recovery_recommendation"]
                or self._run_recovery_recommendation(connection, record)
            )
            return record

    def get_session_status(
        self,
        session_id: str,
        *,
        actor_id: str,
        tenant_id: str,
    ) -> dict | None:
        with self.connect() as connection:
            session = connection.execute(
                "SELECT * FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            if session is None:
                return None
            if session["actor_id"] != actor_id or session["tenant_id"] != tenant_id:
                raise PermissionError("session 不属于当前 actor/tenant")
            lease = connection.execute(
                "SELECT * FROM session_leases WHERE session_id=?", (session_id,)
            ).fetchone()
            run = None
            if lease is not None and lease["active_run_id"]:
                run = connection.execute(
                    "SELECT * FROM runs WHERE id=?", (lease["active_run_id"],)
                ).fetchone()
            return {
                "session_id": session_id,
                "current_owner": lease["lease_owner"] if lease else None,
                "fencing_token": int(lease["fencing_token"]) if lease else None,
                "active_run_id": lease["active_run_id"] if lease else None,
                "lease_remaining_seconds": self._lease_remaining(lease),
                "last_heartbeat": lease["heartbeat_at"] if lease else None,
                "cancel_requested": bool(run and run["status"] == "cancel_requested"),
                "run_status": run["status"] if run else None,
                "recovery_recommendation": (
                    self._run_recovery_recommendation(connection, dict(run))
                    if run
                    else "start_new_run"
                ),
            }

    def _lease_remaining(self, lease) -> float:
        if lease is None or lease["lease_owner"] is None:
            return 0.0
        expires = datetime.fromisoformat(lease["expires_at"])
        return max(0.0, (expires - self.now()).total_seconds())

    @staticmethod
    def _run_recovery_recommendation(connection, run: dict) -> str:
        if run["status"] == "queued":
            return "retry_after_session_lease"
        if run["status"] == "cancel_requested":
            return "wait_for_cooperative_interrupt"
        if run["status"] in {"completed", "failed", "interrupted"}:
            return "none"
        uncertain = connection.execute(
            """
            SELECT 1 FROM tool_operation_refs
            WHERE run_id=? AND status IN ('executing', 'manual_review') LIMIT 1
            """,
            (run["id"],),
        ).fetchone()
        if uncertain:
            return "manual_review"
        return "resume_from_persistent_plan"

    def recover_stalled_runs(self, *, stall_timeout_seconds: float) -> list[dict]:
        if stall_timeout_seconds <= 0:
            raise ValueError("stall timeout 必须大于 0")
        now = self.now()
        cutoff = (now - timedelta(seconds=stall_timeout_seconds)).isoformat()
        recovered: list[dict] = []
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM runs
                WHERE status IN ('running', 'cancel_requested')
                    AND COALESCE(heartbeat_at, started_at)<?
                ORDER BY started_at
                """,
                (cutoff,),
            ).fetchall()
            for row in rows:
                terminal = "interrupted" if row["status"] == "cancel_requested" else "abandoned"
                reason = (
                    "cancel_requested_before_worker_stall"
                    if terminal == "interrupted"
                    else "worker_heartbeat_stalled"
                )
                uncertain = connection.execute(
                    """
                    SELECT operation_id FROM tool_operation_refs
                    WHERE run_id=? AND status='executing'
                    """,
                    (row["id"],),
                ).fetchall()
                if uncertain:
                    connection.execute(
                        """
                        UPDATE tool_operation_refs
                        SET status='manual_review', updated_at=?
                        WHERE run_id=? AND status='executing'
                        """,
                        (now.isoformat(), row["id"]),
                    )
                    recommendation = "manual_review"
                else:
                    recommendation = "resume_from_persistent_plan"
                connection.execute(
                    """
                    UPDATE runs
                    SET status=?, finished_at=?, recovery_reason=?,
                        recovery_recommendation=?
                    WHERE id=? AND status IN ('running', 'cancel_requested')
                    """,
                    (terminal, now.isoformat(), reason, recommendation, row["id"]),
                )
                connection.execute(
                    """
                    UPDATE session_leases
                    SET lease_owner=NULL, active_run_id=NULL,
                        expires_at=?, heartbeat_at=?
                    WHERE active_run_id=?
                    """,
                    (now.isoformat(), now.isoformat(), row["id"]),
                )
                recovered.append(
                    {
                        "run_id": row["id"],
                        "status": terminal,
                        "recovery_reason": reason,
                        "recovery_recommendation": recommendation,
                        "manual_review_operations": [
                            item["operation_id"] for item in uncertain
                        ],
                    }
                )
        return recovered

    def record_tool_event(
        self,
        *,
        run_id: str,
        session_id: str,
        tool_call_id: str | None = None,
        operation_id: str | None = None,
        operation_status: str | None = None,
        tool_name: str,
        arguments: dict,
        outcome: dict,
        duration_ms: float,
        context=None,
    ) -> int:
        from ..runtime.security import redact_sensitive

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if context is not None:
                self._assert_fence(connection, context, boundary="tool.event.commit")
            cursor = connection.execute(
                """
                INSERT INTO tool_events(
                    run_id, session_id, tool_call_id, operation_id, operation_status,
                    tool_name, arguments_json, outcome_json, duration_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    session_id,
                    tool_call_id,
                    operation_id,
                    operation_status,
                    tool_name,
                    json.dumps(redact_sensitive(arguments), ensure_ascii=False, default=str),
                    json.dumps(redact_sensitive(outcome), ensure_ascii=False, default=str),
                    duration_ms,
                    _now(),
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("tool event insert did not return an id")
            return int(cursor.lastrowid)

    def upsert_tool_operation_ref(
        self,
        operation: dict,
        *,
        context=None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        own_connection = connection is None
        active_connection = connection or self.connect()
        try:
            if own_connection:
                active_connection.execute("BEGIN IMMEDIATE")
            if context is not None:
                self._assert_fence(
                    active_connection,
                    context,
                    boundary="tool.operation.commit",
                )
            active_connection.execute(
                """
                INSERT INTO tool_operation_refs(
                    operation_id, idempotency_key, payload_hash, tool_name,
                    tenant_id, actor_id, session_id, run_id, plan_step_id,
                    tool_call_id, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(operation_id) DO UPDATE SET
                    status=excluded.status,
                    run_id=excluded.run_id,
                    plan_step_id=excluded.plan_step_id,
                    tool_call_id=excluded.tool_call_id,
                    updated_at=excluded.updated_at
                """,
                (
                    operation["id"],
                    operation["idempotency_key"],
                    operation["payload_hash"],
                    operation["tool_name"],
                    operation["tenant_id"],
                    operation["actor_id"],
                    operation["session_id"],
                    operation["run_id"],
                    operation.get("plan_step_id"),
                    operation.get("tool_call_id"),
                    operation["status"],
                    operation["updated_at"],
                ),
            )
            if own_connection:
                active_connection.commit()
        except BaseException:
            if own_connection:
                active_connection.rollback()
            raise
        finally:
            if own_connection:
                active_connection.close()

    def get_tool_operation_ref(
        self,
        operation_id: str,
        *,
        actor_id: str,
        tenant_id: str,
    ) -> dict | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM tool_operation_refs WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        if row["actor_id"] != actor_id or row["tenant_id"] != tenant_id:
            raise PermissionError("operation 不属于当前 tenant/actor")
        return dict(row)

    def get_tool_operation_for_call(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        actor_id: str,
        tenant_id: str,
    ) -> dict | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM tool_operation_refs
                WHERE run_id=? AND tool_call_id=?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (run_id, tool_call_id),
            ).fetchone()
        if row is None:
            return None
        if row["actor_id"] != actor_id or row["tenant_id"] != tenant_id:
            raise PermissionError("operation 不属于当前 tenant/actor")
        return dict(row)

    def reject_operation_evidence(
        self,
        operation_id: str,
        *,
        status: str,
        context=None,
    ) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if context is not None:
                self._assert_fence(connection, context, boundary="evidence.reject")
            connection.execute(
                """
                UPDATE evidence
                SET status='rejected', failure_reason=?
                WHERE operation_id=? AND status='accepted'
                """,
                (f"OPERATION_{status.upper()}", operation_id),
            )

    def latest_tool_event_id(self, *, run_id: str, session_id: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(MAX(id), 0) AS event_id
                FROM tool_events WHERE run_id=? AND session_id=?
                """,
                (run_id, session_id),
            ).fetchone()
        return int(row["event_id"])

    def get_tool_events(
        self,
        *,
        run_id: str,
        session_id: str,
        after_id: int = 0,
    ) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM tool_events
                WHERE run_id=? AND session_id=? AND id>?
                ORDER BY id ASC
                """,
                (run_id, session_id, after_id),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _decode_plan(row) -> dict:
        return dict(row)

    @staticmethod
    def _decode_plan_step(row) -> dict:
        record = dict(row)
        record["id"] = record.pop("step_id")
        record["depends_on"] = json.loads(record.pop("depends_on_json"))
        record["allowed_tools"] = json.loads(record.pop("allowed_tools_json"))
        record["expected_tools"] = json.loads(record.pop("expected_tools_json"))
        record["completion_conditions"] = json.loads(
            record.pop("completion_conditions_json")
        )
        return record

    def create_plan(
        self,
        *,
        run_id: str,
        session_id: str,
        actor_id: str,
        tenant_id: str,
        spec: dict,
        max_iterations: int,
        context=None,
    ) -> dict:
        from ..runtime.security import redact_sensitive

        spec = redact_sensitive(spec)
        plan_id = uuid.uuid4().hex
        now = _now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if context is not None:
                self._assert_fence(connection, context, boundary="plan.create")
            existing = connection.execute(
                "SELECT * FROM plans WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["session_id"] != session_id
                    or existing["actor_id"] != actor_id
                    or existing["tenant_id"] != tenant_id
                ):
                    raise PermissionError("plan 不属于当前 run/session/actor/tenant")
                return self._decode_plan(existing)
            connection.execute(
                """
                INSERT INTO plans(
                    id, run_id, session_id, actor_id, tenant_id, goal,
                    status, max_iterations, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    plan_id,
                    run_id,
                    session_id,
                    actor_id,
                    tenant_id,
                    spec["goal"],
                    max_iterations,
                    now,
                    now,
                ),
            )
            for position, step in enumerate(spec["steps"]):
                connection.execute(
                    """
                    INSERT INTO plan_steps(
                        plan_id, step_id, position, goal, depends_on_json,
                        status, allowed_tools_json, expected_tools_json,
                        completion_conditions_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
                    """,
                    (
                        plan_id,
                        step["id"],
                        position,
                        step["goal"],
                        json.dumps(step["depends_on"], ensure_ascii=False),
                        json.dumps(step["allowed_tools"], ensure_ascii=False),
                        json.dumps(step["expected_tools"], ensure_ascii=False),
                        json.dumps(step["completion_conditions"], ensure_ascii=False),
                        now,
                        now,
                    ),
                )
            row = connection.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        return self._decode_plan(row)

    def create_invalid_plan(
        self,
        *,
        run_id: str,
        session_id: str,
        actor_id: str,
        tenant_id: str,
        goal: str,
        failure_reason: str,
        max_iterations: int,
        context=None,
    ) -> dict:
        from ..runtime.security import redact_sensitive_text

        goal = redact_sensitive_text(goal)
        failure_reason = redact_sensitive_text(failure_reason)
        plan_id = uuid.uuid4().hex
        now = _now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if context is not None:
                self._assert_fence(connection, context, boundary="plan.create_invalid")
            connection.execute(
                """
                INSERT INTO plans(
                    id, run_id, session_id, actor_id, tenant_id, goal, status,
                    max_iterations, failure_reason, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'invalid', ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    run_id,
                    session_id,
                    actor_id,
                    tenant_id,
                    goal,
                    max_iterations,
                    failure_reason,
                    now,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        return self._decode_plan(row)

    def get_plan_for_run(
        self,
        run_id: str,
        *,
        session_id: str,
        actor_id: str,
        tenant_id: str,
    ) -> dict | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM plans WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        if (
            row["session_id"] != session_id
            or row["actor_id"] != actor_id
            or row["tenant_id"] != tenant_id
        ):
            raise PermissionError("plan 不属于当前 run/session/actor/tenant")
        return self._decode_plan(row)

    def get_plan_steps(
        self,
        plan_id: str,
        *,
        session_id: str,
        actor_id: str,
        tenant_id: str,
    ) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT s.* FROM plan_steps s
                JOIN plans p ON p.id=s.plan_id
                WHERE s.plan_id=? AND p.session_id=? AND p.actor_id=? AND p.tenant_id=?
                ORDER BY s.position ASC
                """,
                (plan_id, session_id, actor_id, tenant_id),
            ).fetchall()
        return [self._decode_plan_step(row) for row in rows]

    def update_plan(
        self,
        plan_id: str,
        *,
        status: str | None = None,
        failure_reason: str | None | object = _UNSET,
        context=None,
    ) -> None:
        from ..runtime.security import redact_sensitive_text

        assignments = ["updated_at=?"]
        values: list = [_now()]
        if status is not None:
            assignments.append("status=?")
            values.append(status)
        if failure_reason is not _UNSET:
            assignments.append("failure_reason=?")
            values.append(
                redact_sensitive_text(failure_reason)
                if isinstance(failure_reason, str)
                else failure_reason
            )
        values.append(plan_id)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if context is not None:
                self._assert_fence(connection, context, boundary="plan.update")
            connection.execute(
                f"UPDATE plans SET {', '.join(assignments)} WHERE id=?",
                values,
            )

    def update_plan_step(
        self,
        plan_id: str,
        step_id: str,
        *,
        status: str | None = None,
        event_cursor: int | None = None,
        failure_reason: str | None | object = _UNSET,
        context=None,
    ) -> None:
        from ..runtime.security import redact_sensitive_text

        assignments = ["updated_at=?"]
        values: list = [_now()]
        if status is not None:
            assignments.append("status=?")
            values.append(status)
        if event_cursor is not None:
            assignments.append("event_cursor=?")
            values.append(event_cursor)
        if failure_reason is not _UNSET:
            assignments.append("failure_reason=?")
            values.append(
                redact_sensitive_text(failure_reason)
                if isinstance(failure_reason, str)
                else failure_reason
            )
        values.extend([plan_id, step_id])
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if context is not None:
                self._assert_fence(connection, context, boundary="plan.step.update")
            connection.execute(
                f"""
                UPDATE plan_steps SET {', '.join(assignments)}
                WHERE plan_id=? AND step_id=?
                """,
                values,
            )

    def increment_step_retry(
        self,
        plan_id: str,
        step_id: str,
        *,
        failure_reason: str,
        context=None,
    ) -> int:
        from ..runtime.security import redact_sensitive_text

        failure_reason = redact_sensitive_text(failure_reason)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if context is not None:
                self._assert_fence(connection, context, boundary="plan.step.retry")
            connection.execute(
                """
                UPDATE plan_steps
                SET retry_count=retry_count+1, failure_reason=?, updated_at=?
                WHERE plan_id=? AND step_id=?
                """,
                (failure_reason, _now(), plan_id, step_id),
            )
            row = connection.execute(
                "SELECT retry_count FROM plan_steps WHERE plan_id=? AND step_id=?",
                (plan_id, step_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"未知计划步骤：{plan_id}/{step_id}")
        return int(row["retry_count"])

    def consume_plan_iteration(
        self,
        plan_id: str,
        *,
        max_iterations: int,
        context=None,
    ) -> dict:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if context is not None:
                self._assert_fence(connection, context, boundary="plan.iteration")
            row = connection.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
            if row is None:
                raise KeyError(f"未知计划：{plan_id}")
            if row["iterations_used"] >= max_iterations:
                connection.execute(
                    """
                    UPDATE plans SET status='budget_exceeded',
                        failure_reason='PLAN_ITERATION_BUDGET_EXCEEDED', updated_at=?
                    WHERE id=?
                    """,
                    (_now(), plan_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE plans SET iterations_used=iterations_used+1, updated_at=?
                    WHERE id=?
                    """,
                    (_now(), plan_id),
                )
            updated = connection.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        return self._decode_plan(updated)

    def record_plan_evidence(
        self,
        *,
        plan_id: str,
        step_id: str,
        context,
        kind: str,
        status: str,
        payload: dict,
        tool_name: str | None = None,
        tool_event_id: int | None = None,
        operation_id: str | None = None,
        artifact_id: str | None = None,
        citation: str | None = None,
        failure_reason: str | None = None,
    ) -> bool:
        from ..runtime.security import redact_sensitive, redact_sensitive_text

        payload = redact_sensitive(payload)
        citation = redact_sensitive_text(citation) if citation is not None else None
        failure_reason = (
            redact_sensitive_text(failure_reason) if failure_reason is not None else None
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_fence(connection, context, boundary="evidence.commit")
            owner = connection.execute(
                """
                SELECT p.run_id, p.session_id, p.actor_id, p.tenant_id
                FROM plans p
                JOIN plan_steps s ON s.plan_id=p.id
                WHERE p.id=? AND s.step_id=?
                """,
                (plan_id, step_id),
            ).fetchone()
            if owner is None:
                raise KeyError(f"未知计划步骤：{plan_id}/{step_id}")
            expected_owner = (
                context.run_id,
                context.session_id,
                context.actor_id,
                context.tenant_id,
            )
            actual_owner = (
                owner["run_id"],
                owner["session_id"],
                owner["actor_id"],
                owner["tenant_id"],
            )
            if actual_owner != expected_owner:
                raise PermissionError("evidence 不属于当前 run/session/actor/tenant")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO evidence(
                    plan_id, step_id, run_id, session_id, actor_id, tenant_id,
                    kind, status, tool_name, tool_event_id, operation_id,
                    artifact_id, citation, failure_reason, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    step_id,
                    context.run_id,
                    context.session_id,
                    context.actor_id,
                    context.tenant_id,
                    kind,
                    status,
                    tool_name,
                    tool_event_id,
                    operation_id,
                    artifact_id,
                    citation,
                    failure_reason,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    _now(),
                ),
            )
            return cursor.rowcount > 0

    @staticmethod
    def _decode_evidence(row) -> dict:
        record = dict(row)
        record["payload"] = json.loads(record.pop("payload_json"))
        return record

    def get_step_evidence(
        self,
        plan_id: str,
        step_id: str,
        *,
        actor_id: str,
        tenant_id: str,
    ) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM evidence
                WHERE plan_id=? AND step_id=? AND actor_id=? AND tenant_id=?
                ORDER BY id ASC
                """,
                (plan_id, step_id, actor_id, tenant_id),
            ).fetchall()
        return [self._decode_evidence(row) for row in rows]

    def get_plan_evidence(
        self,
        plan_id: str,
        *,
        actor_id: str,
        tenant_id: str,
    ) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM evidence
                WHERE plan_id=? AND actor_id=? AND tenant_id=?
                ORDER BY id ASC
                """,
                (plan_id, actor_id, tenant_id),
            ).fetchall()
        return [self._decode_evidence(row) for row in rows]

    def add_memory(
        self,
        *,
        actor_id: str,
        tenant_id: str,
        content: str,
        scope: str = "user",
        scope_id: str = "",
        kind: str = "fact",
        importance: float = 0.5,
        source_session_id: str | None = None,
        source: str = "explicit",
        expires_at: str | None = None,
        conflict_key: str | None = None,
    ) -> int:
        from ..runtime.security import redact_sensitive_text

        content = content.strip()
        if not content:
            raise ValueError("memory content 不能为空")
        content = redact_sensitive_text(content)
        conflict_key = (
            redact_sensitive_text(conflict_key) if conflict_key is not None else None
        )
        importance = min(1.0, max(0.0, float(importance)))
        now = _now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if conflict_key:
                conflicting = connection.execute(
                    """
                    SELECT id FROM memories
                    WHERE actor_id=? AND tenant_id=? AND scope=? AND scope_id=?
                        AND kind=? AND conflict_key=? AND active=1 AND content<>?
                    """,
                    (
                        actor_id,
                        tenant_id,
                        scope,
                        scope_id,
                        kind,
                        conflict_key,
                        content,
                    ),
                ).fetchall()
                if conflicting:
                    connection.execute(
                        """
                        UPDATE memories SET active=0, updated_at=?
                        WHERE actor_id=? AND tenant_id=? AND scope=? AND scope_id=?
                            AND kind=? AND conflict_key=? AND active=1 AND content<>?
                        """,
                        (
                            now,
                            actor_id,
                            tenant_id,
                            scope,
                            scope_id,
                            kind,
                            conflict_key,
                            content,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO audit_events(
                            actor_id, tenant_id, action, resource,
                            decision, details_json, created_at
                        ) VALUES (?, ?, 'memory.conflict', ?, 'superseded', ?, ?)
                        """,
                        (
                            actor_id,
                            tenant_id,
                            conflict_key,
                            json.dumps(
                                {"superseded_ids": [row["id"] for row in conflicting]},
                                ensure_ascii=False,
                            ),
                            now,
                        ),
                    )
            connection.execute(
                """
                INSERT INTO memories(
                    actor_id, tenant_id, scope, scope_id, kind, content,
                    importance, source_session_id, source, expires_at, conflict_key,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(actor_id, tenant_id, scope, scope_id, kind, content)
                DO UPDATE SET
                    importance=MAX(memories.importance, excluded.importance),
                    source=excluded.source,
                    expires_at=excluded.expires_at,
                    conflict_key=excluded.conflict_key,
                    active=1,
                    updated_at=excluded.updated_at
                """,
                (
                    actor_id,
                    tenant_id,
                    scope,
                    scope_id,
                    kind,
                    content,
                    importance,
                    source_session_id,
                    source,
                    expires_at,
                    conflict_key,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT id FROM memories
                WHERE actor_id=? AND tenant_id=? AND scope=? AND scope_id=?
                    AND kind=? AND content=?
                """,
                (actor_id, tenant_id, scope, scope_id, kind, content),
            ).fetchone()
            return int(row["id"])

    def update_memory(
        self,
        memory_id: int,
        *,
        actor_id: str,
        tenant_id: str,
        content: str,
        importance: float | None = None,
        expires_at: str | None | object = _UNSET,
    ) -> bool:
        from ..runtime.security import redact_sensitive_text

        content = content.strip()
        if not content:
            raise ValueError("memory content 不能为空")
        content = redact_sensitive_text(content)
        importance = (
            None if importance is None else min(1.0, max(0.0, float(importance)))
        )
        with self.connect() as connection:
            expiry_clause = "" if expires_at is _UNSET else ", expires_at=?"
            params = [content, importance]
            if expires_at is not _UNSET:
                params.append(expires_at)
            params.extend((_now(), memory_id, actor_id, tenant_id))
            cursor = connection.execute(
                f"""
                UPDATE memories SET content=?, importance=COALESCE(?, importance)
                    {expiry_clause}, updated_at=?
                WHERE id=? AND actor_id=? AND tenant_id=?
                """,
                params,
            )
            return cursor.rowcount == 1

    def deactivate_memory(
        self, memory_id: int, *, actor_id: str, tenant_id: str
    ) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE memories SET active=0, updated_at=?
                WHERE id=? AND actor_id=? AND tenant_id=?
                """,
                (_now(), memory_id, actor_id, tenant_id),
            )
            return cursor.rowcount == 1

    def search_memories(
        self,
        *,
        actor_id: str,
        tenant_id: str,
        query: str,
        limit: int = 6,
        scope_ids: set[str] | None = None,
    ) -> list[dict]:
        scope_ids = scope_ids or set()
        params: list = [tenant_id, actor_id]
        scope_clause = ""
        if scope_ids:
            placeholders = ",".join("?" for _ in scope_ids)
            scope_clause = f" AND (m.scope_id='' OR m.scope_id IN ({placeholders}))"
            params.extend(sorted(scope_ids))
        params.append(limit)
        query = query.strip()
        with self.connect() as connection:
            rows = self._search_fts(
                connection,
                actor_id=actor_id,
                tenant_id=tenant_id,
                query=query,
                limit=limit,
                scope_ids=scope_ids,
            )
            if not rows:
                rows = connection.execute(
                    f"""
                    SELECT m.* FROM memories m
                    WHERE m.tenant_id=? AND m.actor_id=? AND m.active=1
                        AND (m.expires_at IS NULL OR m.expires_at>?)
                        {scope_clause}
                    ORDER BY m.importance DESC, m.updated_at DESC
                    LIMIT ?
                    """,
                    [tenant_id, actor_id, _now(), *params[2:]],
                ).fetchall()
            ids = [row["id"] for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                connection.execute(
                    f"UPDATE memories SET last_accessed_at=? WHERE id IN ({placeholders})",
                    [_now(), *ids],
                )
        return [dict(row) for row in rows]

    @staticmethod
    def _search_fts(
        connection: sqlite3.Connection,
        *,
        actor_id: str,
        tenant_id: str,
        query: str,
        limit: int,
        scope_ids: set[str],
    ):
        if not query:
            return []
        scope_clause = ""
        params: list = [
            f'"{query.replace(chr(34), chr(34) * 2)}"',
            tenant_id,
            actor_id,
            _now(),
        ]
        if scope_ids:
            placeholders = ",".join("?" for _ in scope_ids)
            scope_clause = f" AND (m.scope_id='' OR m.scope_id IN ({placeholders}))"
            params.extend(sorted(scope_ids))
        params.append(limit)
        try:
            return connection.execute(
                f"""
                SELECT m.* FROM memory_fts f
                JOIN memories m ON m.id=f.rowid
                WHERE memory_fts MATCH ? AND m.tenant_id=? AND m.actor_id=?
                    AND m.active=1 AND (m.expires_at IS NULL OR m.expires_at>?)
                    {scope_clause}
                ORDER BY bm25(memory_fts), m.importance DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        except sqlite3.OperationalError:
            return []

    def record_audit_event(
        self,
        *,
        actor_id: str,
        tenant_id: str,
        action: str,
        resource: str,
        decision: str,
        details: dict,
    ) -> None:
        from ..runtime.security import redact_sensitive

        details = redact_sensitive(details)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO audit_events(
                    actor_id, tenant_id, action, resource,
                    decision, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    actor_id,
                    tenant_id,
                    action,
                    resource,
                    decision,
                    json.dumps(details, ensure_ascii=False, default=str),
                    _now(),
                ),
            )

    def begin_api_request(
        self,
        *,
        actor_id: str,
        tenant_id: str,
        request_id: str,
        request_hash: str,
        run_id: str | None = None,
        owner_id: str | None = None,
        lease_seconds: float = 300.0,
        retention_seconds: int = 7 * 24 * 60 * 60,
    ) -> dict:
        """Atomically bind request scope, payload, owner lease and a stable run id."""
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", request_id) is None:
            raise ValueError("request id must be 1..128 safe ASCII characters")
        if lease_seconds <= 0 or retention_seconds <= 0:
            raise ValueError("API request lease and retention must be positive")
        now_value = self.now()
        now = now_value.isoformat()
        lease_expires_at = (now_value + timedelta(seconds=lease_seconds)).isoformat()
        retained_until = (now_value + timedelta(seconds=retention_seconds)).isoformat()
        proposed_run_id = run_id or uuid.uuid4().hex
        proposed_owner_id = owner_id or f"legacy:{uuid.uuid4().hex}"
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM api_requests
                WHERE actor_id=? AND tenant_id=? AND request_id=?
                """,
                (actor_id, tenant_id, request_id),
            ).fetchone()
            if row is not None:
                if row["request_hash"] != request_hash:
                    raise ValueError("request id is already bound to a different payload")
                if row["status"] in {"completed", "failed", "uncertain"}:
                    return self._decode_api_request(row)
                run = (
                    connection.execute("SELECT * FROM runs WHERE id=?", (row["run_id"],)).fetchone()
                    if row["run_id"]
                    else None
                )
                request_lease_active = bool(
                    row["lease_expires_at"] and row["lease_expires_at"] > now
                )
                if request_lease_active:
                    record = self._decode_api_request(row)
                    record["status"] = "in_progress"
                    record["recovery_action"] = "wait"
                    return record
                if run is not None and run["status"] in {"running", "cancel_requested"}:
                    session_lease = connection.execute(
                        "SELECT * FROM session_leases WHERE active_run_id=?", (run["id"],)
                    ).fetchone()
                    if session_lease and session_lease["expires_at"] > now:
                        record = self._decode_api_request(row)
                        record["status"] = "in_progress"
                        record["recovery_action"] = "wait"
                        return record
                connection.execute(
                    """
                    UPDATE api_requests SET status='stale', updated_at=?
                    WHERE actor_id=? AND tenant_id=? AND request_id=?
                        AND status IN ('claimed', 'in_progress')
                    """,
                    (now, actor_id, tenant_id, request_id),
                )
                self._record_api_request_audit(
                    connection,
                    actor_id=actor_id,
                    tenant_id=tenant_id,
                    request_id=request_id,
                    decision="stale",
                    details={"previous_owner": row["owner_id"]},
                )
                uncertain = bool(
                    row["run_id"]
                    and connection.execute(
                        """
                        SELECT 1 FROM tool_operation_refs
                        WHERE run_id=? AND status IN ('executing', 'manual_review') LIMIT 1
                        """,
                        (row["run_id"],),
                    ).fetchone()
                )
                if uncertain:
                    connection.execute(
                        """
                        UPDATE api_requests
                        SET status='uncertain', owner_id=NULL, lease_expires_at=NULL,
                            error_json=?, updated_at=?, retained_until=?
                        WHERE actor_id=? AND tenant_id=? AND request_id=?
                        """,
                        (
                            json.dumps({"code": "MANUAL_REVIEW_REQUIRED"}),
                            now,
                            retained_until,
                            actor_id,
                            tenant_id,
                            request_id,
                        ),
                    )
                    self._record_api_request_audit(
                        connection,
                        actor_id=actor_id,
                        tenant_id=tenant_id,
                        request_id=request_id,
                        decision="uncertain",
                        details={"run_id": row["run_id"], "reason": "uncertain_tool_operation"},
                    )
                    current = connection.execute(
                        """SELECT * FROM api_requests
                           WHERE actor_id=? AND tenant_id=? AND request_id=?""",
                        (actor_id, tenant_id, request_id),
                    ).fetchone()
                    return self._decode_api_request(current)
                recovery_action = "execute"
                if run is not None:
                    if run["status"] == "completed":
                        recovery_action = "recover_completed"
                    elif run["status"] in {"queued", "abandoned"}:
                        recovery_action = "resume"
                    elif run["status"] in {"failed", "interrupted"}:
                        connection.execute(
                            """
                            UPDATE api_requests
                            SET status='failed', owner_id=NULL, lease_expires_at=NULL,
                                failed_at=?, updated_at=?, retained_until=?, error_json=?
                            WHERE actor_id=? AND tenant_id=? AND request_id=?
                            """,
                            (
                                now,
                                now,
                                retained_until,
                                json.dumps({"code": "RUN_TERMINATED", "status": run["status"]}),
                                actor_id,
                                tenant_id,
                                request_id,
                            ),
                        )
                        current = connection.execute(
                            """SELECT * FROM api_requests
                               WHERE actor_id=? AND tenant_id=? AND request_id=?""",
                            (actor_id, tenant_id, request_id),
                        ).fetchone()
                        return self._decode_api_request(current)
                    elif run["status"] in {"running", "cancel_requested"}:
                        terminal = "interrupted" if run["status"] == "cancel_requested" else "abandoned"
                        connection.execute(
                            """
                            UPDATE runs
                            SET status=?, finished_at=?, recovery_reason='api_request_lease_expired',
                                recovery_recommendation='resume_from_persistent_plan'
                            WHERE id=? AND status IN ('running', 'cancel_requested')
                            """,
                            (terminal, now, run["id"]),
                        )
                        connection.execute(
                            """
                            UPDATE session_leases
                            SET lease_owner=NULL, active_run_id=NULL, heartbeat_at=?, expires_at=?
                            WHERE active_run_id=? AND expires_at<=?
                            """,
                            (now, now, run["id"], now),
                        )
                        recovery_action = "resume"
                connection.execute(
                    """
                    UPDATE api_requests
                    SET status='claimed', owner_id=?, lease_expires_at=?,
                        attempt=attempt+1, claimed_at=?, updated_at=?, retained_until=?
                    WHERE actor_id=? AND tenant_id=? AND request_id=?
                    """,
                    (
                        proposed_owner_id,
                        lease_expires_at,
                        now,
                        now,
                        retained_until,
                        actor_id,
                        tenant_id,
                        request_id,
                    ),
                )
                self._record_api_request_audit(
                    connection,
                    actor_id=actor_id,
                    tenant_id=tenant_id,
                    request_id=request_id,
                    decision="reclaimed",
                    details={"run_id": row["run_id"], "recovery_action": recovery_action},
                )
                current = connection.execute(
                    """SELECT * FROM api_requests
                       WHERE actor_id=? AND tenant_id=? AND request_id=?""",
                    (actor_id, tenant_id, request_id),
                ).fetchone()
                record = self._decode_api_request(current)
                record["recovery_action"] = recovery_action
                return record
            connection.execute(
                """
                INSERT INTO api_requests(
                    actor_id, tenant_id, request_id, request_hash,
                    status, run_id, owner_id, lease_expires_at, attempt,
                    created_at, updated_at, claimed_at, retained_until
                ) VALUES (?, ?, ?, ?, 'claimed', ?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    actor_id,
                    tenant_id,
                    request_id,
                    request_hash,
                    proposed_run_id,
                    proposed_owner_id,
                    lease_expires_at,
                    now,
                    now,
                    now,
                    retained_until,
                ),
            )
            self._record_api_request_audit(
                connection,
                actor_id=actor_id,
                tenant_id=tenant_id,
                request_id=request_id,
                decision="claimed",
                details={"run_id": proposed_run_id, "attempt": 1},
            )
            current = connection.execute(
                """SELECT * FROM api_requests
                   WHERE actor_id=? AND tenant_id=? AND request_id=?""",
                (actor_id, tenant_id, request_id),
            ).fetchone()
            record = self._decode_api_request(current)
            record["recovery_action"] = "execute"
            return record

    @staticmethod
    def _decode_api_request(row) -> dict:
        record = dict(row)
        record["response"] = json.loads(record.pop("response_json") or "null")
        record["error"] = json.loads(record.pop("error_json") or "null")
        record["response_headers"] = json.loads(
            record.pop("response_headers_json", None) or "{}"
        )
        return record

    @staticmethod
    def _record_api_request_audit(
        connection: sqlite3.Connection,
        *,
        actor_id: str,
        tenant_id: str,
        request_id: str,
        decision: str,
        details: dict,
    ) -> None:
        from ..runtime.security import redact_sensitive

        connection.execute(
            """
            INSERT INTO audit_events(
                actor_id, tenant_id, action, resource, decision, details_json, created_at
            ) VALUES (?, ?, 'api_request.transition', ?, ?, ?, ?)
            """,
            (
                actor_id,
                tenant_id,
                f"api_request:{request_id}",
                decision,
                json.dumps(redact_sensitive(details), ensure_ascii=False, sort_keys=True),
                _now(),
            ),
        )

    def start_api_request(
        self,
        *,
        actor_id: str,
        tenant_id: str,
        request_id: str,
        owner_id: str,
        attempt: int,
    ) -> bool:
        now = self.now_iso()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE api_requests
                SET status='in_progress', started_at=COALESCE(started_at, ?), updated_at=?
                WHERE actor_id=? AND tenant_id=? AND request_id=?
                    AND status='claimed' AND owner_id=? AND attempt=?
                    AND lease_expires_at>?
                """,
                (now, now, actor_id, tenant_id, request_id, owner_id, attempt, now),
            )
            if cursor.rowcount == 1:
                self._record_api_request_audit(
                    connection,
                    actor_id=actor_id,
                    tenant_id=tenant_id,
                    request_id=request_id,
                    decision="in_progress",
                    details={"attempt": attempt},
                )
            return cursor.rowcount == 1

    def renew_api_request(
        self,
        *,
        actor_id: str,
        tenant_id: str,
        request_id: str,
        owner_id: str,
        attempt: int,
        lease_seconds: float,
    ) -> bool:
        now = self.now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE api_requests SET lease_expires_at=?, updated_at=?
                WHERE actor_id=? AND tenant_id=? AND request_id=?
                    AND status='in_progress' AND owner_id=? AND attempt=?
                """,
                (
                    (now + timedelta(seconds=lease_seconds)).isoformat(),
                    now.isoformat(),
                    actor_id,
                    tenant_id,
                    request_id,
                    owner_id,
                    attempt,
                ),
            )
            return cursor.rowcount == 1

    def finish_api_request(
        self,
        *,
        actor_id: str,
        tenant_id: str,
        request_id: str,
        status: str,
        run_id: str | None = None,
        response: dict | None = None,
        error: dict | None = None,
        owner_id: str | None = None,
        attempt: int | None = None,
        response_status: int = 200,
        response_content_type: str = "application/json; charset=utf-8",
        response_headers: dict[str, str] | None = None,
        retention_seconds: int = 7 * 24 * 60 * 60,
    ) -> dict:
        from ..runtime.security import redact_sensitive

        if status not in {"completed", "failed"}:
            raise ValueError("api request terminal status must be completed or failed")
        if retention_seconds <= 0:
            raise ValueError("API request retention must be positive")
        response = redact_sensitive(response) if response is not None else None
        error = redact_sensitive(error) if error is not None else None
        response_headers = redact_sensitive(response_headers or {})
        canonical_response = (
            json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
            if response is not None
            else None
        )
        now_value = self.now()
        now = now_value.isoformat()
        retained_until = (now_value + timedelta(seconds=retention_seconds)).isoformat()
        owner_clause = ""
        parameters: list = []
        if owner_id is not None or attempt is not None:
            if owner_id is None or attempt is None:
                raise ValueError("owner_id and attempt must be supplied together")
            owner_clause = " AND owner_id=? AND attempt=?"
            parameters.extend((owner_id, attempt))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"""
                UPDATE api_requests
                SET status=?, run_id=COALESCE(?, run_id), response_json=?, response_hash=?,
                    response_status=?, response_content_type=?, response_headers_json=?,
                    error_json=?, owner_id=NULL, lease_expires_at=NULL, updated_at=?,
                    completed_at=?, failed_at=?, retained_until=?
                WHERE actor_id=? AND tenant_id=? AND request_id=?
                    AND status IN ('claimed', 'in_progress'){owner_clause}
                """,
                tuple(
                    [
                    status,
                    run_id,
                    canonical_response,
                    hashlib.sha256(canonical_response.encode()).hexdigest()
                    if canonical_response is not None
                    else None,
                    response_status,
                    response_content_type,
                    json.dumps(response_headers, ensure_ascii=False, sort_keys=True),
                    json.dumps(error, ensure_ascii=False, sort_keys=True, default=str)
                    if error is not None
                    else None,
                    now,
                    now if status == "completed" else None,
                    now if status == "failed" else None,
                    retained_until,
                    actor_id,
                    tenant_id,
                    request_id,
                    ]
                    + parameters
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("api request owner lease is stale or request is not active")
            self._record_api_request_audit(
                connection,
                actor_id=actor_id,
                tenant_id=tenant_id,
                request_id=request_id,
                decision=status,
                details={"run_id": run_id, "response_hash": (
                    hashlib.sha256(canonical_response.encode()).hexdigest()
                    if canonical_response is not None else None
                )},
            )
            row = connection.execute(
                """SELECT * FROM api_requests
                   WHERE actor_id=? AND tenant_id=? AND request_id=?""",
                (actor_id, tenant_id, request_id),
            ).fetchone()
            return self._decode_api_request(row)

    def get_api_request(
        self, *, actor_id: str, tenant_id: str, request_id: str
    ) -> dict | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM api_requests
                   WHERE actor_id=? AND tenant_id=? AND request_id=?""",
                (actor_id, tenant_id, request_id),
            ).fetchone()
        return self._decode_api_request(row) if row is not None else None

    def expire_api_request_leases(
        self,
        *,
        actor_id: str,
        tenant_id: str,
        owner_id: str,
        limit: int = 200,
    ) -> int:
        if limit <= 0 or limit > 10_000:
            raise ValueError("limit must be between 1 and 10000")
        now = self.now_iso()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT request_id FROM api_requests
                WHERE actor_id=? AND tenant_id=? AND owner_id=?
                    AND status IN ('claimed', 'in_progress') AND lease_expires_at<=?
                ORDER BY lease_expires_at LIMIT ?
                """,
                (actor_id, tenant_id, owner_id, now, limit),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE api_requests SET status='stale', updated_at=?
                    WHERE actor_id=? AND tenant_id=? AND request_id=? AND owner_id=?
                    """,
                    (now, actor_id, tenant_id, row["request_id"], owner_id),
                )
                self._record_api_request_audit(
                    connection,
                    actor_id=actor_id,
                    tenant_id=tenant_id,
                    request_id=row["request_id"],
                    decision="stale",
                    details={"owner_id": owner_id},
                )
            return len(rows)

    def gc_api_requests(
        self,
        *,
        actor_id: str,
        tenant_id: str,
        limit: int = 200,
    ) -> int:
        """Delete expired terminal envelopes without touching recoverable runs/operations."""
        if limit <= 0 or limit > 10_000:
            raise ValueError("limit must be between 1 and 10000")
        now = self.now_iso()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT request_id FROM api_requests a
                WHERE a.actor_id=? AND a.tenant_id=?
                    AND a.status IN ('completed', 'failed')
                    AND a.retained_until IS NOT NULL AND a.retained_until<=?
                    AND NOT EXISTS (
                        SELECT 1 FROM tool_operation_refs t
                        WHERE t.run_id=a.run_id AND t.status IN ('executing', 'manual_review')
                    )
                    AND (
                        a.run_id IS NULL OR NOT EXISTS (
                            SELECT 1 FROM runs missing WHERE missing.id=a.run_id
                        ) OR EXISTS (
                            SELECT 1 FROM runs r WHERE r.id=a.run_id
                            AND r.status IN ('completed', 'failed', 'interrupted')
                        )
                    )
                ORDER BY a.retained_until LIMIT ?
                """,
                (actor_id, tenant_id, now, limit),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """DELETE FROM api_requests
                       WHERE actor_id=? AND tenant_id=? AND request_id=?""",
                    (actor_id, tenant_id, row["request_id"]),
                )
                self._record_api_request_audit(
                    connection,
                    actor_id=actor_id,
                    tenant_id=tenant_id,
                    request_id=row["request_id"],
                    decision="garbage_collected",
                    details={},
                )
            return len(rows)

    def get_scheduled_job(
        self,
        job_id: str,
        *,
        actor_id: str,
        tenant_id: str,
    ) -> dict | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM scheduled_jobs WHERE id=?", (job_id,)
            ).fetchone()
        if row is None:
            return None
        if row["actor_id"] != actor_id or row["tenant_id"] != tenant_id:
            raise PermissionError("scheduled job does not belong to actor/tenant")
        return dict(row)

    def count(self, table: str) -> int:
        allowed = {
            "sessions",
            "messages",
            "runs",
            "session_leases",
            "tool_events",
            "tool_operation_refs",
            "artifacts",
            "provider_events",
            "context_checkpoints",
            "memories",
            "audit_events",
            "scheduled_jobs",
            "plans",
            "plan_steps",
            "evidence",
            "state_schema_migrations",
            "api_requests",
            "run_journals",
            "agent_tool_envelopes",
            "agent_tool_calls",
        }
        if table not in allowed:
            raise ValueError(f"不允许统计表：{table}")
        with self.connect() as connection:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
