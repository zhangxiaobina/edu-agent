"""Durable persistence primitives for the single turn finalizer.

The finalizer is deliberately persisted separately from ``runs`` and the
run journal.  ``runs`` remains the compatibility-facing status row while this
table records the idempotent sub-step cursor needed to resume a crashed
finalizer without relying on process memory.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping

from ..runtime.security import redact_sensitive, redact_sensitive_text


TURN_FINALIZER_SCHEMA = "011_turn_finalizer"
FINALIZER_CURSOR = {
    "open": 0,
    "tools_closed": 1,
    "plan_verified": 2,
    "final_message_committed": 3,
    "usage_settled": 4,
    "terminal": 5,
    "hooks_done": 6,
    "cleanup_done": 7,
}
FINALIZER_TERMINAL_CURSORS = frozenset({5, 6, 7})


_SCHEMA = """
CREATE TABLE IF NOT EXISTS turn_finalizers (
    run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    actor_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    cursor INTEGER NOT NULL CHECK(cursor >= 0 AND cursor <= 7),
    revision INTEGER NOT NULL CHECK(revision > 0),
    status TEXT NOT NULL CHECK(status IN ('in_progress', 'terminal')),
    terminal_status TEXT NOT NULL,
    stop_reason TEXT NOT NULL,
    final_answer TEXT,
    trace_json TEXT NOT NULL,
    plan_json TEXT,
    verification_json TEXT,
    usage_json TEXT NOT NULL,
    budget_json TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    final_message_id INTEGER UNIQUE REFERENCES messages(id) ON DELETE RESTRICT,
    final_message_hash TEXT,
    owner_id TEXT,
    fencing_token INTEGER,
    terminal_at TEXT,
    hooks_json TEXT NOT NULL,
    cleanup_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, final_message_hash)
);

CREATE INDEX IF NOT EXISTS idx_turn_finalizers_scope
    ON turn_finalizers(tenant_id, actor_id, session_id, run_id);

CREATE TABLE IF NOT EXISTS turn_finalizer_hooks (
    run_id TEXT NOT NULL REFERENCES turn_finalizers(run_id) ON DELETE CASCADE,
    hook_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('claimed', 'completed', 'failed')),
    error TEXT,
    details_json TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY(run_id, hook_key)
);
"""


@dataclass(frozen=True)
class TurnFinalizerRecord:
    run_id: str
    session_id: str
    actor_id: str
    tenant_id: str
    cursor: int
    revision: int
    status: str
    terminal_status: str
    stop_reason: str
    final_answer: str | None
    trace: list[dict[str, Any]]
    plan: dict[str, Any] | None
    verification: dict[str, Any] | None
    usage: list[dict[str, Any]]
    budget: dict[str, Any]
    context: dict[str, Any]
    error: str | None
    final_message_id: int | None
    final_message_hash: str | None
    owner_id: str | None
    fencing_token: int | None
    terminal_at: str | None
    hooks: dict[str, Any]
    cleanup_at: str | None
    created_at: str
    updated_at: str

    @property
    def terminal(self) -> bool:
        return self.cursor >= 5 or self.status == "terminal"

    @property
    def step(self) -> str:
        for name, value in FINALIZER_CURSOR.items():
            if value == self.cursor:
                return name
        return "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "actor_id": self.actor_id,
            "tenant_id": self.tenant_id,
            "cursor": self.cursor,
            "step": self.step,
            "revision": self.revision,
            "status": self.status,
            "terminal_status": self.terminal_status,
            "stop_reason": self.stop_reason,
            "final_answer": self.final_answer,
            "trace": self.trace,
            "plan": self.plan,
            "verification": self.verification,
            "usage": self.usage,
            "budget": self.budget,
            "context": self.context,
            "error": self.error,
            "final_message_id": self.final_message_id,
            "final_message_hash": self.final_message_hash,
            "owner_id": self.owner_id,
            "fencing_token": self.fencing_token,
            "terminal_at": self.terminal_at,
            "hooks": self.hooks,
            "cleanup_at": self.cleanup_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )


def _load_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        # Finalizer rows are written by this module and malformed data must
        # fail closed rather than silently changing the terminal response.
        raise RuntimeError("turn finalizer contains invalid JSON")


def _hash_message(message: Mapping[str, Any]) -> str:
    canonical = _json(
        {
            "role": message.get("role"),
            "content": message.get("content") or "",
            "name": message.get("name"),
            "tool_call_id": message.get("tool_call_id"),
            "tool_calls": message.get("tool_calls"),
        }
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def initialize_turn_finalizer_schema(connection: sqlite3.Connection, *, now: str) -> None:
    connection.executescript(_SCHEMA)
    # ``runs`` was created before R2.4 and old databases do not have a
    # provider usage column.  These additive columns are part of migration 11.
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(runs)")}
    if "usage_json" not in columns:
        connection.execute("ALTER TABLE runs ADD COLUMN usage_json TEXT")
    if "stop_reason" not in columns:
        connection.execute("ALTER TABLE runs ADD COLUMN stop_reason TEXT")
    finalizer_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(turn_finalizers)")
    }
    if "context_json" not in finalizer_columns:
        connection.execute(
            "ALTER TABLE turn_finalizers ADD COLUMN context_json TEXT NOT NULL DEFAULT '{}'"
        )


def _scope_check(row: sqlite3.Row, context) -> None:
    if row is None:
        raise KeyError(f"turn finalizer does not exist: {context.run_id}")
    expected = (
        context.run_id,
        context.session_id,
        context.actor_id,
        context.tenant_id,
    )
    actual = (row["run_id"], row["session_id"], row["actor_id"], row["tenant_id"])
    if actual != expected:
        raise PermissionError("turn finalizer scope does not match context")


def _run_scope_check(connection: sqlite3.Connection, context) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT r.id, r.session_id, r.actor_id, r.tenant_id,
               s.actor_id AS session_actor_id, s.tenant_id AS session_tenant_id
        FROM runs r JOIN sessions s ON s.id=r.session_id
        WHERE r.id=?
        """,
        (context.run_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"run does not exist: {context.run_id}")
    if (
        row["session_id"],
        row["actor_id"],
        row["tenant_id"],
        row["session_actor_id"],
        row["session_tenant_id"],
    ) != (
        context.session_id,
        context.actor_id,
        context.tenant_id,
        context.actor_id,
        context.tenant_id,
    ):
        raise PermissionError("run/session scope does not match finalizer context")
    return row


def _decode(row: sqlite3.Row | Mapping[str, Any]) -> TurnFinalizerRecord:
    record = dict(row)
    trace = _load_json(record.get("trace_json"), [])
    usage = _load_json(record.get("usage_json"), [])
    budget = _load_json(record.get("budget_json"), {})
    context = _load_json(record.get("context_json"), {})
    plan = _load_json(record.get("plan_json"), None)
    verification = _load_json(record.get("verification_json"), None)
    hooks = _load_json(record.get("hooks_json"), {})
    if not isinstance(trace, list) or not isinstance(usage, list):
        raise RuntimeError("turn finalizer trace/usage must be JSON arrays")
    if (
        not isinstance(budget, dict)
        or not isinstance(context, dict)
        or (plan is not None and not isinstance(plan, dict))
    ):
        raise RuntimeError("turn finalizer budget/plan must be JSON objects")
    if not isinstance(hooks, dict):
        raise RuntimeError("turn finalizer hooks must be a JSON object")
    return TurnFinalizerRecord(
        run_id=str(record["run_id"]),
        session_id=str(record["session_id"]),
        actor_id=str(record["actor_id"]),
        tenant_id=str(record["tenant_id"]),
        cursor=int(record["cursor"]),
        revision=int(record["revision"]),
        status=str(record["status"]),
        terminal_status=str(record["terminal_status"]),
        stop_reason=str(record["stop_reason"]),
        final_answer=record.get("final_answer"),
        trace=trace,
        plan=plan,
        verification=verification,
        usage=usage,
        budget=budget,
        context=context,
        error=record.get("error"),
        final_message_id=(
            int(record["final_message_id"]) if record.get("final_message_id") is not None else None
        ),
        final_message_hash=record.get("final_message_hash"),
        owner_id=record.get("owner_id"),
        fencing_token=(
            int(record["fencing_token"]) if record.get("fencing_token") is not None else None
        ),
        terminal_at=record.get("terminal_at"),
        hooks=hooks,
        cleanup_at=record.get("cleanup_at"),
        created_at=str(record["created_at"]),
        updated_at=str(record["updated_at"]),
    )


def get_turn_finalizer(store, *, run_id: str, session_id: str, actor_id: str, tenant_id: str):
    with store.connect() as connection:
        run = connection.execute(
            "SELECT session_id, actor_id, tenant_id FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if run is not None and (
            run["session_id"], run["actor_id"], run["tenant_id"]
        ) != (session_id, actor_id, tenant_id):
            raise PermissionError("run does not belong to actor/tenant")
        row = connection.execute(
            "SELECT * FROM turn_finalizers WHERE run_id=?",
            (run_id,),
        ).fetchone()
    if row is None:
        return None
    if (
        row["session_id"],
        row["actor_id"],
        row["tenant_id"],
    ) != (session_id, actor_id, tenant_id):
        raise PermissionError("turn finalizer does not belong to actor/tenant")
    return _decode(row)


def ensure_turn_finalizer(
    store,
    context,
    *,
    stop_reason: str,
    terminal_status: str,
    final_answer: str | None,
    trace: list[dict[str, Any]],
    plan: dict[str, Any] | None,
    usage: list[dict[str, Any]],
    budget: dict[str, Any],
    context_payload: dict[str, Any] | None = None,
    error: str | None = None,
):
    now = store.now_iso()
    safe_answer = redact_sensitive_text(final_answer) if final_answer is not None else None
    safe_trace = redact_sensitive(trace)
    safe_plan = redact_sensitive(plan) if plan is not None else None
    safe_usage = redact_sensitive(usage)
    safe_budget = redact_sensitive(budget)
    safe_context = redact_sensitive(dict(context_payload or {}))
    safe_error = redact_sensitive_text(error) if error is not None else None
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _run_scope_check(connection, context)
        store._assert_fence(
            connection,
            context,
            boundary="turn_finalizer.ensure",
            allow_cancelled=True,
            require_lease=True,
        )
        existing = connection.execute(
            "SELECT * FROM turn_finalizers WHERE run_id=?",
            (context.run_id,),
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO turn_finalizers(
                    run_id, session_id, actor_id, tenant_id, cursor, revision,
                    status, terminal_status, stop_reason, final_answer,
                    trace_json, plan_json, verification_json, usage_json,
                    budget_json, context_json, error, owner_id, fencing_token, hooks_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 0, 1, 'in_progress', ?, ?, ?, ?, ?, NULL,
                          ?, ?, ?, ?, ?, ?, '{}', ?, ?)
                """,
                (
                    context.run_id,
                    context.session_id,
                    context.actor_id,
                    context.tenant_id,
                    terminal_status,
                    stop_reason,
                    safe_answer,
                    _json(safe_trace),
                    _json(safe_plan) if safe_plan is not None else None,
                    _json(safe_usage),
                    _json(safe_budget),
                    _json(safe_context),
                    safe_error,
                    getattr(context, "lease_owner", None),
                    getattr(context, "fencing_token", None),
                    now,
                    now,
                ),
            )
            existing = connection.execute(
                "SELECT * FROM turn_finalizers WHERE run_id=?",
                (context.run_id,),
            ).fetchone()
        else:
            _scope_check(existing, context)
        return _decode(existing)


def _load_for_update(connection: sqlite3.Connection, context) -> sqlite3.Row:
    _run_scope_check(connection, context)
    row = connection.execute(
        "SELECT * FROM turn_finalizers WHERE run_id=?",
        (context.run_id,),
    ).fetchone()
    _scope_check(row, context)
    return row


def _cas_cursor(
    connection: sqlite3.Connection,
    store,
    context,
    row: sqlite3.Row,
    *,
    expected_cursor: int,
    next_cursor: int,
    fields: Mapping[str, Any] | None = None,
):
    if next_cursor != expected_cursor + 1:
        raise RuntimeError(
            f"turn finalizer cursor must advance one step: {expected_cursor}->{next_cursor}"
        )
    if int(row["cursor"]) >= next_cursor:
        return row
    if int(row["cursor"]) != expected_cursor:
        raise RuntimeError(
            f"turn finalizer cursor conflict: expected {expected_cursor}, actual {row['cursor']}"
        )
    fields = dict(fields or {})
    if getattr(context, "lease_owner", None) is not None:
        fields.setdefault("owner_id", context.lease_owner)
        fields.setdefault("fencing_token", context.fencing_token)
    assignments = ["cursor=?", "revision=revision+1", "updated_at=?"]
    values: list[Any] = [next_cursor, store.now_iso()]
    for key, value in fields.items():
        assignments.append(f"{key}=?")
        values.append(value)
    values.extend((context.run_id, expected_cursor, int(row["revision"])))
    updated = connection.execute(
        f"""
        UPDATE turn_finalizers SET {', '.join(assignments)}
        WHERE run_id=? AND cursor=? AND revision=?
        """,
        values,
    )
    if updated.rowcount != 1:
        current = connection.execute(
            "SELECT * FROM turn_finalizers WHERE run_id=?", (context.run_id,)
        ).fetchone()
        if current is not None and int(current["cursor"]) >= next_cursor:
            return current
        raise RuntimeError("turn finalizer compare-and-set lost a race")
    return connection.execute(
        "SELECT * FROM turn_finalizers WHERE run_id=?", (context.run_id,)
    ).fetchone()


def _mark_final_message_journal_boundary(
    connection: sqlite3.Connection,
    store,
    context,
    *,
    required: bool,
) -> None:
    from .journal import RunPhase, RunStableBoundary

    journal = connection.execute(
        "SELECT * FROM run_journals WHERE run_id=?",
        (context.run_id,),
    ).fetchone()
    if journal is None:
        return
    if journal["stable_boundary"] == RunStableBoundary.FINAL_MESSAGE_COMMITTED.value:
        return
    if journal["phase"] != RunPhase.FINALIZING.value:
        if not required:
            return
        raise RuntimeError(
            "final message cannot commit before the run journal enters finalizing"
        )
    stream_sequence = int(
        connection.execute(
            "SELECT stream_event_sequence FROM runs WHERE id=?",
            (context.run_id,),
        ).fetchone()["stream_event_sequence"]
        or 0
    )
    event_sequence = max(int(journal["event_sequence"]), stream_sequence) + 1
    updated = connection.execute(
        """
        UPDATE run_journals
        SET stable_boundary=?, event_sequence=?, writer_id=?, fencing_token=?,
            revision=revision+1, updated_at=?
        WHERE run_id=? AND revision=? AND phase=? AND fencing_token=?
        """,
        (
            RunStableBoundary.FINAL_MESSAGE_COMMITTED.value,
            event_sequence,
            getattr(context, "lease_owner", None) or journal["writer_id"],
            (
                getattr(context, "fencing_token", None)
                if getattr(context, "fencing_token", None) is not None
                else journal["fencing_token"]
            ),
            store.now_iso(),
            context.run_id,
            journal["revision"],
            RunPhase.FINALIZING.value,
            journal["fencing_token"],
        ),
    )
    if updated.rowcount != 1:
        raise RuntimeError("run journal final-message compare-and-set lost a race")


def advance_turn_finalizer(
    store,
    context,
    *,
    expected_cursor: int,
    next_cursor: int,
    fields: Mapping[str, Any] | None = None,
    allow_terminal: bool = False,
):
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _load_for_update(connection, context)
        if int(row["cursor"]) >= next_cursor:
            return _decode(row)
        if not allow_terminal:
            store._assert_fence(
                connection,
                context,
                boundary=f"turn_finalizer.cursor.{expected_cursor}",
                allow_cancelled=True,
                require_lease=True,
            )
        updated = _cas_cursor(
            connection,
            store,
            context,
            row,
            expected_cursor=expected_cursor,
            next_cursor=next_cursor,
            fields=fields,
        )
        return _decode(updated)


def commit_final_message(
    store,
    context,
    *,
    expected_cursor: int,
    message: Mapping[str, Any] | None,
):
    """Insert the final assistant exactly once and advance cursor atomically."""
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _load_for_update(connection, context)
        if int(row["cursor"]) >= 3:
            return _decode(row)
        if expected_cursor != 2:
            raise RuntimeError("final message can only commit after Plan/Evidence verification")
        store._assert_fence(
            connection,
            context,
            boundary="turn_finalizer.final_message",
            allow_cancelled=True,
            require_lease=True,
        )
        run = connection.execute(
            "SELECT status FROM runs WHERE id=?",
            (context.run_id,),
        ).fetchone()
        cancelled = run is not None and run["status"] == "cancel_requested"
        fields: dict[str, Any] = {}
        if cancelled:
            # Cancellation may race with a model response after the finalizer
            # row was created.  Make the decision in this same transaction so
            # a stale worker cannot leave a user-visible answer behind.
            message = None
            fields.update(
                {
                    "stop_reason": "interrupted",
                    "terminal_status": "interrupted",
                    "final_answer": None,
                }
            )
        if message is None:
            updated = _cas_cursor(
                connection,
                store,
                context,
                row,
                expected_cursor=expected_cursor,
                next_cursor=3,
                fields=fields,
            )
            _mark_final_message_journal_boundary(
                connection,
                store,
                context,
                required=False,
            )
            return _decode(updated)
        safe = redact_sensitive(dict(message))
        if safe.get("role") != "assistant" or safe.get("tool_calls"):
            raise ValueError("final assistant message must be a plain assistant message")
        content = safe.get("content") or ""
        message_hash = _hash_message(safe)
        key = f"final-assistant:{context.run_id}"
        existing = connection.execute(
            "SELECT * FROM messages WHERE run_id=? AND idempotency_key=?",
            (context.run_id, key),
        ).fetchone()
        if existing is not None:
            existing_message = {
                "role": existing["role"],
                "content": existing["content"] or "",
                "name": existing["name"],
                "tool_call_id": existing["tool_call_id"],
                "tool_calls": (
                    json.loads(existing["tool_calls_json"])
                    if existing["tool_calls_json"]
                    else None
                ),
            }
            if _hash_message(existing_message) != message_hash:
                raise RuntimeError("final assistant idempotency key is bound to another message")
            message_id = int(existing["id"])
        else:
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), -1) + 1 AS sequence FROM messages WHERE session_id=?",
                (context.session_id,),
            ).fetchone()["sequence"]
            inserted = connection.execute(
                """
                INSERT INTO messages(
                    session_id, sequence, role, content, name, tool_call_id,
                    tool_calls_json, run_id, fencing_token, idempotency_key,
                    model_attempt, loop_cursor, created_at
                ) VALUES (?, ?, 'assistant', ?, ?, NULL, NULL, ?, ?, ?, NULL, NULL, ?)
                """,
                (
                    context.session_id,
                    int(sequence),
                    content,
                    safe.get("name"),
                    context.run_id,
                    getattr(context, "fencing_token", None),
                    key,
                    store.now_iso(),
                ),
            )
            message_id = int(inserted.lastrowid)
            connection.execute(
                "UPDATE sessions SET updated_at=? WHERE id=?",
                (store.now_iso(), context.session_id),
            )
        fields.update(
            {
                "final_message_id": message_id,
                "final_message_hash": message_hash,
                "final_answer": redact_sensitive_text(str(content)),
            }
        )
        updated = _cas_cursor(
            connection,
            store,
            context,
            row,
            expected_cursor=expected_cursor,
            next_cursor=3,
            fields=fields,
        )
        _mark_final_message_journal_boundary(
            connection,
            store,
            context,
            required=True,
        )
        return _decode(updated)


def settle_usage(
    store,
    context,
    *,
    expected_cursor: int,
    budget: dict[str, Any] | None = None,
):
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _load_for_update(connection, context)
        if int(row["cursor"]) >= 4:
            return _decode(row)
        if expected_cursor != 3:
            raise RuntimeError("usage can only settle after final message commit")
        store._assert_fence(
            connection,
            context,
            boundary="turn_finalizer.usage",
            allow_cancelled=True,
            require_lease=True,
        )
        budget_json = (
            _json(redact_sensitive(budget))
            if budget is not None
            else row["budget_json"]
        )
        usage_json = row["usage_json"]
        if budget is not None:
            connection.execute(
                """
                UPDATE turn_finalizers SET budget_json=?, updated_at=?
                WHERE run_id=?
                """,
                (budget_json, store.now_iso(), context.run_id),
            )
        connection.execute(
            "UPDATE runs SET budget_json=?, usage_json=? WHERE id=?",
            (budget_json, usage_json, context.run_id),
        )
        refreshed = _load_for_update(connection, context)
        updated = _cas_cursor(
            connection,
            store,
            context,
            refreshed,
            expected_cursor=expected_cursor,
            next_cursor=4,
        )
        return _decode(updated)


def mark_terminal(store, context, *, expected_cursor: int):
    """Atomically mark journal, compatibility run row and finalizer terminal."""
    from .journal import RunPhase, RunStableBoundary, validate_phase_transition

    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _load_for_update(connection, context)
        if int(row["cursor"]) >= 5:
            return _decode(row)
        if expected_cursor != 4:
            raise RuntimeError("terminal can only be marked after usage settlement")
        store._assert_fence(
            connection,
            context,
            boundary="turn_finalizer.terminal",
            allow_cancelled=True,
            require_lease=True,
        )
        run = connection.execute("SELECT * FROM runs WHERE id=?", (context.run_id,)).fetchone()
        if run is None:
            raise KeyError(f"run does not exist: {context.run_id}")
        stop_reason = str(row["stop_reason"])
        terminal_status = str(row["terminal_status"])
        # Cancellation and uncertain writes win any race with a successful
        # response, even when the model returned just before the cancel flag.
        if run["status"] == "cancel_requested":
            stop_reason = "interrupted"
            terminal_status = "interrupted"
        uncertain = connection.execute(
            """
            SELECT 1 FROM tool_operation_refs
            WHERE run_id=? AND status IN ('executing', 'manual_review') LIMIT 1
            """,
            (context.run_id,),
        ).fetchone()
        if uncertain:
            stop_reason = "manual_review"
            terminal_status = "failed"
        if terminal_status not in {"completed", "failed", "interrupted"}:
            terminal_status = "failed"
        journal = connection.execute(
            "SELECT * FROM run_journals WHERE run_id=?", (context.run_id,)
        ).fetchone()
        if journal is not None:
            if journal["phase"] == RunPhase.CANCELLED.value:
                stop_reason = "interrupted"
                terminal_status = "interrupted"
            elif journal["phase"] == RunPhase.FAILED.value and terminal_status == "completed":
                stop_reason = "manual_review" if uncertain else "model_failed"
                terminal_status = "failed"
        now = store.now_iso()
        if journal is not None and journal["phase"] not in {
            RunPhase.TERMINAL.value,
            RunPhase.CANCELLED.value,
            RunPhase.FAILED.value,
        }:
            target_phase = (
                RunPhase.TERMINAL
                if terminal_status == "completed"
                else RunPhase.CANCELLED
                if terminal_status == "interrupted"
                else RunPhase.FAILED
            )
            target_boundary = (
                RunStableBoundary.TERMINAL
                if target_phase is RunPhase.TERMINAL
                else RunStableBoundary.CANCELLED
                if target_phase is RunPhase.CANCELLED
                else RunStableBoundary.FAILED
            )
            # The agent normally enters ``finalizing`` during Plan/Evidence
            # verification.  A successful setup-failure recovery can reach
            # this transaction directly from an earlier execution phase,
            # however.  Preserve the journal's legal transition graph by
            # recording the intermediate finalizing boundary in this same
            # transaction before committing a successful terminal branch.
            current_phase = RunPhase(journal["phase"])
            needs_finalizing = (
                current_phase is not RunPhase.FINALIZING
                and target_phase is RunPhase.TERMINAL
            )
            if needs_finalizing:
                validate_phase_transition(current_phase, RunPhase.FINALIZING)
                next_event_sequence = max(
                    int(journal["event_sequence"]),
                    int(run["stream_event_sequence"] or 0),
                ) + 1
                writer_id = getattr(context, "lease_owner", None) or journal["writer_id"]
                fencing_token = (
                    getattr(context, "fencing_token", None)
                    if getattr(context, "fencing_token", None) is not None
                    else journal["fencing_token"]
                )
                connection.execute(
                    """
                    UPDATE run_journals
                    SET phase=?, stable_boundary=?, event_sequence=?,
                        writer_id=?, fencing_token=?, budget_snapshot_json=?,
                        revision=revision+1, updated_at=?
                    WHERE run_id=? AND revision=? AND phase=? AND fencing_token=?
                    """,
                    (
                        RunPhase.FINALIZING.value,
                        RunStableBoundary.VERIFICATION_COMMITTED.value,
                        next_event_sequence,
                        writer_id,
                        fencing_token,
                        row["budget_json"],
                        now,
                        context.run_id,
                        journal["revision"],
                        journal["phase"],
                        journal["fencing_token"],
                    ),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise RuntimeError("run journal finalizing compare-and-set lost a race")
                journal = connection.execute(
                    "SELECT * FROM run_journals WHERE run_id=?", (context.run_id,)
                ).fetchone()
            if not needs_finalizing:
                validate_phase_transition(journal["phase"], target_phase)
            next_event_sequence = max(
                int(journal["event_sequence"]),
                int(run["stream_event_sequence"] or 0),
            ) + 1
            connection.execute(
                """
                UPDATE run_journals
                SET phase=?, stable_boundary=?, event_sequence=?,
                    writer_id=?, fencing_token=?, budget_snapshot_json=?,
                    revision=revision+1, updated_at=?
                WHERE run_id=? AND revision=? AND phase=? AND fencing_token=?
                """,
                (
                    target_phase.value,
                    target_boundary.value,
                    next_event_sequence,
                    getattr(context, "lease_owner", None) or journal["writer_id"],
                    getattr(context, "fencing_token", None)
                    if getattr(context, "fencing_token", None) is not None
                    else journal["fencing_token"],
                    row["budget_json"],
                    now,
                    context.run_id,
                    journal["revision"],
                    journal["phase"],
                    journal["fencing_token"],
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise RuntimeError("run journal terminal compare-and-set lost a race")
        if run["status"] in {"running", "cancel_requested", "queued", "abandoned"}:
            error = row["error"]
            if stop_reason not in {"completed", "interrupted"} and not error:
                error = f"stop_reason={stop_reason}"
            connection.execute(
                """
                UPDATE runs SET status=?, stop_reason=?, budget_json=?, usage_json=?, error=?,
                    recovery_reason=?, finished_at=?
                WHERE id=? AND status IN ('running', 'cancel_requested', 'queued', 'abandoned')
                """,
                (
                    terminal_status,
                    stop_reason,
                    row["budget_json"],
                    row["usage_json"],
                    error,
                    row["error"] or ("manual_review" if stop_reason == "manual_review" else None),
                    now,
                    context.run_id,
                ),
            )
        terminal_fields = {
            "status": "terminal",
            "terminal_status": terminal_status,
            "stop_reason": stop_reason,
            "terminal_at": now,
        }
        if terminal_status != "completed":
            # A crash after the message transaction can leave a committed
            # assistant row at cursor 3.  Hide it atomically whenever the
            # terminal decision is no longer a success, while retaining its
            # id for auditability.
            if row["final_message_id"] is not None:
                connection.execute(
                    "UPDATE messages SET active=0 WHERE id=? AND run_id=?",
                    (row["final_message_id"], context.run_id),
                )
            terminal_fields["final_answer"] = None
        updated = _cas_cursor(
            connection,
            store,
            context,
            row,
            expected_cursor=expected_cursor,
            next_cursor=5,
            fields=terminal_fields,
        )
        return _decode(updated)


def claim_hook(store, context, *, hook_key: str) -> bool:
    if not hook_key or len(hook_key) > 128:
        raise ValueError("hook_key must be a non-empty short string")
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _load_for_update(connection, context)
        if int(row["cursor"]) < 5:
            raise RuntimeError("post-processing cannot run before terminal")
        now = store.now_iso()
        inserted = connection.execute(
            """
            INSERT OR IGNORE INTO turn_finalizer_hooks(
                run_id, hook_key, status, details_json, claimed_at
            ) VALUES (?, ?, 'claimed', '{}', ?)
            """,
            (context.run_id, hook_key, now),
        )
        return inserted.rowcount == 1


def complete_hook(
    store,
    context,
    *,
    hook_key: str,
    success: bool,
    error: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> None:
    safe_error = redact_sensitive_text(error) if error else None
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _load_for_update(connection, context)
        if int(row["cursor"]) < 5:
            raise RuntimeError("post-processing cannot complete before terminal")
        now = store.now_iso()
        status = "completed" if success else "failed"
        connection.execute(
            """
            UPDATE turn_finalizer_hooks
            SET status=?, error=?, details_json=?, completed_at=?
            WHERE run_id=? AND hook_key=? AND status='claimed'
            """,
            (
                status,
                safe_error,
                _json(redact_sensitive(dict(details or {}))),
                now,
                context.run_id,
                hook_key,
            ),
        )
        if not success:
            safe_details = redact_sensitive(dict(details or {}))
            connection.execute(
                """
                INSERT INTO audit_events(
                    actor_id, tenant_id, action, resource, decision,
                    details_json, created_at
                ) VALUES (?, ?, 'turn_finalizer.post_hook', ?, 'failed', ?, ?)
                """,
                (
                    context.actor_id,
                    context.tenant_id,
                    f"run:{context.run_id}:{hook_key}",
                    _json({"error": safe_error, **safe_details}),
                    now,
                ),
            )


def finish_hooks(store, context, *, expected_cursor: int, hooks: Mapping[str, Any]):
    if expected_cursor != 5:
        raise RuntimeError("post-processing hooks can only run after terminal")
    return advance_turn_finalizer(
        store,
        context,
        expected_cursor=expected_cursor,
        next_cursor=6,
        fields={"hooks_json": _json(redact_sensitive(dict(hooks)))},
        allow_terminal=True,
    )


def finish_cleanup(store, context, *, expected_cursor: int):
    if expected_cursor != 6:
        raise RuntimeError("cleanup can only run after post-processing hooks")
    return advance_turn_finalizer(
        store,
        context,
        expected_cursor=expected_cursor,
        next_cursor=7,
        fields={"cleanup_at": store.now_iso()},
        allow_terminal=True,
    )


def message_hash(message: Mapping[str, Any]) -> str:
    return _hash_message(message)


__all__ = [
    "FINALIZER_CURSOR",
    "FINALIZER_TERMINAL_CURSORS",
    "TURN_FINALIZER_SCHEMA",
    "TurnFinalizerRecord",
    "advance_turn_finalizer",
    "claim_hook",
    "commit_final_message",
    "complete_hook",
    "ensure_turn_finalizer",
    "finish_cleanup",
    "finish_hooks",
    "get_turn_finalizer",
    "initialize_turn_finalizer_schema",
    "mark_terminal",
    "message_hash",
    "settle_usage",
]
