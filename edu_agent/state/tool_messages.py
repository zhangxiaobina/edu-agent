from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping

from ..runtime.security import redact_sensitive
from .journal import (
    RunJournalCorrupt,
    RunJournalFencingError,
    RunJournalIdentityError,
    RunJournalNotFound,
    RunJournalSnapshot,
    RunJournalTransitionError,
    RunPhase,
    RunStableBoundary,
    _decode_snapshot,
)


AGENT_TOOL_MESSAGES_MIGRATION = "010_agent_tool_messages"


class ToolMessageError(RuntimeError):
    code = "TOOL_MESSAGE_ERROR"

    def __init__(self, message: str, **details: Any):
        super().__init__(message)
        self.details = details
        self.error_code = self.code

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "details": copy.deepcopy(self.details),
        }


class ToolMessageConflict(ToolMessageError):
    code = "TOOL_MESSAGE_CONFLICT"


class ToolMessagePairingError(ToolMessageError):
    code = "TOOL_MESSAGE_PAIRING_REJECTED"


class ToolMessageOperationError(ToolMessageError):
    code = "TOOL_MESSAGE_OPERATION_REJECTED"


class ToolMessageSchemaError(ToolMessageError):
    code = "TOOL_MESSAGE_SCHEMA_INVALID"


@dataclass(frozen=True)
class ToolMessageCommit:
    message: dict[str, Any]
    journal: RunJournalSnapshot
    replayed: bool


_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_tool_envelopes (
    run_id TEXT NOT NULL REFERENCES run_journals(run_id) ON DELETE CASCADE,
    model_attempt INTEGER NOT NULL CHECK(model_attempt > 0),
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    assistant_message_id INTEGER NOT NULL UNIQUE REFERENCES messages(id) ON DELETE CASCADE,
    payload_hash TEXT NOT NULL,
    call_ids_json TEXT NOT NULL,
    tool_manifest_hash TEXT NOT NULL,
    provider_route_json TEXT NOT NULL,
    journal_cursor INTEGER NOT NULL CHECK(journal_cursor >= 0),
    fencing_token INTEGER NOT NULL CHECK(fencing_token >= 0),
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id, model_attempt)
);

CREATE TABLE IF NOT EXISTS agent_tool_calls (
    run_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    model_attempt INTEGER NOT NULL,
    call_index INTEGER NOT NULL CHECK(call_index >= 0),
    envelope_message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    call_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'completed')),
    result_message_id INTEGER UNIQUE REFERENCES messages(id) ON DELETE RESTRICT,
    result_hash TEXT,
    operation_id TEXT REFERENCES tool_operation_refs(operation_id),
    result_cursor INTEGER CHECK(result_cursor IS NULL OR result_cursor >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(
        (status='pending' AND result_message_id IS NULL
            AND result_hash IS NULL AND result_cursor IS NULL)
        OR
        (status='completed' AND result_message_id IS NOT NULL
            AND result_hash IS NOT NULL AND result_cursor IS NOT NULL)
    ),
    PRIMARY KEY(run_id, tool_call_id),
    UNIQUE(run_id, model_attempt, call_index),
    FOREIGN KEY(run_id, model_attempt)
        REFERENCES agent_tool_envelopes(run_id, model_attempt) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_agent_tool_calls_global_call
    ON agent_tool_calls(tool_call_id, run_id);
CREATE INDEX IF NOT EXISTS idx_agent_tool_calls_pending
    ON agent_tool_calls(run_id, model_attempt, status, call_index);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_run_idempotency
    ON messages(run_id, idempotency_key)
    WHERE run_id IS NOT NULL AND idempotency_key IS NOT NULL;
"""


def initialize_agent_tool_message_schema(
    connection: sqlite3.Connection,
    *,
    now: str,
) -> None:
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(messages)")
    }
    additions = {
        "idempotency_key": "TEXT",
        "model_attempt": "INTEGER",
        "loop_cursor": "INTEGER",
    }
    for name, declaration in additions.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE messages ADD COLUMN {name} {declaration}")
    connection.executescript(_SCHEMA)
    required = {
        "agent_tool_envelopes": {
            "run_id",
            "model_attempt",
            "session_id",
            "assistant_message_id",
            "payload_hash",
            "call_ids_json",
            "tool_manifest_hash",
            "provider_route_json",
            "journal_cursor",
            "fencing_token",
            "created_at",
        },
        "agent_tool_calls": {
            "run_id",
            "tool_call_id",
            "session_id",
            "model_attempt",
            "call_index",
            "envelope_message_id",
            "tool_name",
            "arguments_json",
            "call_hash",
            "status",
            "result_message_id",
            "result_hash",
            "operation_id",
            "result_cursor",
            "created_at",
            "updated_at",
        },
    }
    for table, expected in required.items():
        actual = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        missing = sorted(expected - actual)
        if missing:
            raise ToolMessageSchemaError(
                "agent tool message schema is incomplete",
                table=table,
                missing_columns=missing,
            )
    connection.execute(
        """
        INSERT OR IGNORE INTO state_schema_migrations(version, applied_at)
        VALUES (?, ?)
        """,
        (AGENT_TOOL_MESSAGES_MIGRATION, now),
    )


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _validate_model_attempt(model_attempt: int) -> None:
    if isinstance(model_attempt, bool) or not isinstance(model_attempt, int) or model_attempt <= 0:
        raise ToolMessagePairingError("model_attempt must be a positive integer")


def _load_json(value: str, field: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RunJournalCorrupt(f"{field} contains invalid JSON", field=field) from error


def _message_from_row(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": row["role"],
        "content": row["content"] or "",
    }
    if row["name"]:
        message["name"] = row["name"]
    if row["tool_call_id"]:
        message["tool_call_id"] = row["tool_call_id"]
    if row["tool_calls_json"]:
        message["tool_calls"] = _load_json(row["tool_calls_json"], "tool_calls_json")
    return message


def _load_journal(connection: sqlite3.Connection, context) -> RunJournalSnapshot:
    row = connection.execute(
        "SELECT * FROM run_journals WHERE run_id=?",
        (context.run_id,),
    ).fetchone()
    if row is None:
        raise RunJournalNotFound("run journal does not exist", run_id=context.run_id)
    snapshot = _decode_snapshot(row)
    if (
        snapshot.session_id != context.session_id
        or snapshot.actor_id != context.actor_id
        or snapshot.tenant_id != context.tenant_id
    ):
        raise RunJournalIdentityError("journal scope does not match run context")
    writer = getattr(context, "lease_owner", None)
    token = getattr(context, "fencing_token", None)
    if not writer or token is None:
        raise RunJournalFencingError("tool message commit requires a bound run lease")
    if snapshot.fencing_token > int(token):
        raise RunJournalFencingError(
            "tool message writer has a stale fencing token",
            journal_fencing_token=snapshot.fencing_token,
            writer_fencing_token=int(token),
        )
    if snapshot.fencing_token == int(token) and snapshot.writer_id != writer:
        raise RunJournalFencingError(
            "tool message writer identity does not match journal",
            fencing_token=int(token),
        )
    return snapshot


def _next_message_sequence(connection: sqlite3.Connection, session_id: str) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(sequence), -1) AS sequence FROM messages WHERE session_id=?",
        (session_id,),
    ).fetchone()
    return int(row["sequence"]) + 1


def _validate_tool_calls(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    if message.get("role") != "assistant":
        raise ToolMessagePairingError("tool-call envelope must have assistant role")
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        raise ToolMessagePairingError("tool-call envelope must contain at least one call")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, call in enumerate(calls):
        if not isinstance(call, Mapping):
            raise ToolMessagePairingError("tool call must be an object", call_index=index)
        call_id = call.get("id")
        function = call.get("function")
        if not isinstance(call_id, str) or not call_id.strip():
            raise ToolMessagePairingError("tool call id must be non-empty", call_index=index)
        if call_id in seen:
            raise ToolMessagePairingError(
                "tool-call envelope contains a duplicate call id",
                tool_call_id=call_id,
            )
        if not isinstance(function, Mapping):
            raise ToolMessagePairingError("tool call function must be an object", call_id=call_id)
        name = function.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ToolMessagePairingError("tool name must be non-empty", call_id=call_id)
        arguments = function.get("arguments", "{}")
        if not isinstance(arguments, (str, Mapping)):
            raise ToolMessagePairingError(
                "tool arguments must be a JSON string or object",
                call_id=call_id,
            )
        seen.add(call_id)
        normalized.append(
            {
                "id": call_id,
                "type": str(call.get("type") or "function"),
                "function": {
                    "name": name,
                    "arguments": arguments,
                },
            }
        )
    return normalized


def _safe_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    safe = copy.deepcopy(calls)
    for call in safe:
        arguments = call["function"]["arguments"]
        if isinstance(arguments, str):
            try:
                decoded = json.loads(arguments)
            except (TypeError, ValueError, json.JSONDecodeError):
                call["function"]["arguments"] = redact_sensitive(arguments)
            else:
                call["function"]["arguments"] = _json(redact_sensitive(decoded))
        else:
            call["function"]["arguments"] = _json(redact_sensitive(dict(arguments)))
    return redact_sensitive(safe)


def _update_journal(
    connection: sqlite3.Connection,
    store,
    context,
    current: RunJournalSnapshot,
    *,
    phase: RunPhase,
    boundary: RunStableBoundary,
    loop_cursor: int,
    operation_id: str | None = None,
    last_tool_event_id: int | None = None,
) -> RunJournalSnapshot:
    writer = str(context.lease_owner)
    token = int(context.fencing_token)
    operation = operation_id or current.references.operation_id
    tool_event = (
        last_tool_event_id
        if last_tool_event_id is not None
        else current.references.last_tool_event_id
    )
    now = store.now_iso()
    cursor = connection.execute(
        """
        UPDATE run_journals
        SET phase=?, loop_cursor=?, event_sequence=event_sequence+1,
            budget_snapshot_json=?, stable_boundary=?, operation_id=?,
            last_tool_event_id=?, writer_id=?, fencing_token=?,
            revision=revision+1, updated_at=?
        WHERE run_id=? AND session_id=? AND actor_id=? AND tenant_id=?
            AND revision=? AND phase=? AND loop_cursor=?
            AND model_attempt=? AND event_sequence=? AND fencing_token=?
        """,
        (
            phase.value,
            loop_cursor,
            _json(context.budget.usage()),
            boundary.value,
            operation,
            tool_event,
            writer,
            token,
            now,
            context.run_id,
            context.session_id,
            context.actor_id,
            context.tenant_id,
            current.revision,
            current.phase.value,
            current.loop_cursor,
            current.model_attempt,
            current.event_sequence,
            current.fencing_token,
        ),
    )
    if cursor.rowcount != 1:
        raise ToolMessageConflict(
            "journal changed while committing a tool message",
            run_id=context.run_id,
            expected_revision=current.revision,
        )
    row = connection.execute(
        "SELECT * FROM run_journals WHERE run_id=?",
        (context.run_id,),
    ).fetchone()
    return _decode_snapshot(row)


def append_assistant_tool_envelope(
    store,
    context,
    message: Mapping[str, Any],
    *,
    model_attempt: int,
) -> ToolMessageCommit:
    _validate_model_attempt(model_attempt)
    calls = _validate_tool_calls(message)
    raw_payload = {
        "role": "assistant",
        "content": message.get("content") or "",
        "tool_calls": calls,
    }
    payload_hash = _digest(raw_payload)
    safe_payload = redact_sensitive(
        {
            "role": "assistant",
            "content": message.get("content") or "",
            "tool_calls": _safe_calls(calls),
        }
    )

    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        store._assert_fence(connection, context, boundary="assistant.envelope.commit")
        current = _load_journal(connection, context)
        existing = connection.execute(
            """
            SELECT e.payload_hash, m.*
            FROM agent_tool_envelopes e
            JOIN messages m ON m.id=e.assistant_message_id
            WHERE e.run_id=? AND e.model_attempt=?
            """,
            (context.run_id, model_attempt),
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] != payload_hash:
                raise ToolMessageConflict(
                    "model attempt already has a different assistant envelope",
                    run_id=context.run_id,
                    model_attempt=model_attempt,
                )
            return ToolMessageCommit(_message_from_row(existing), current, True)
        if current.phase is not RunPhase.MODEL or current.model_attempt != model_attempt:
            raise RunJournalTransitionError(
                "assistant envelope may only commit for the active model attempt",
                phase=current.phase.value,
                journal_attempt=current.model_attempt,
                model_attempt=model_attempt,
            )
        duplicates = connection.execute(
            f"""
            SELECT tool_call_id FROM agent_tool_calls
            WHERE run_id=? AND tool_call_id IN ({','.join('?' for _ in calls)})
            """,
            (context.run_id, *(call["id"] for call in calls)),
        ).fetchall()
        if duplicates:
            raise ToolMessagePairingError(
                "tool call id is already bound in this run",
                call_ids=[row["tool_call_id"] for row in duplicates],
            )

        next_cursor = current.loop_cursor + 1
        sequence = _next_message_sequence(connection, context.session_id)
        now = store.now_iso()
        key = f"assistant-envelope:{model_attempt}"
        inserted = connection.execute(
            """
            INSERT INTO messages(
                session_id, sequence, role, content, tool_calls_json,
                run_id, fencing_token, idempotency_key, model_attempt,
                loop_cursor, created_at
            ) VALUES (?, ?, 'assistant', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                context.session_id,
                sequence,
                safe_payload.get("content") or "",
                _json(safe_payload["tool_calls"]),
                context.run_id,
                int(context.fencing_token),
                key,
                model_attempt,
                next_cursor,
                now,
            ),
        )
        message_id = int(inserted.lastrowid)
        connection.execute(
            """
            INSERT INTO agent_tool_envelopes(
                run_id, model_attempt, session_id, assistant_message_id,
                payload_hash, call_ids_json, tool_manifest_hash,
                provider_route_json, journal_cursor, fencing_token, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                context.run_id,
                model_attempt,
                context.session_id,
                message_id,
                payload_hash,
                _json([call["id"] for call in calls]),
                current.tool_manifest_hash,
                _json(current.frozen_provider_route),
                next_cursor,
                int(context.fencing_token),
                now,
            ),
        )
        for index, (raw_call, safe_call) in enumerate(zip(calls, safe_payload["tool_calls"])):
            connection.execute(
                """
                INSERT INTO agent_tool_calls(
                    run_id, tool_call_id, session_id, model_attempt, call_index,
                    envelope_message_id, tool_name, arguments_json, call_hash,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    context.run_id,
                    raw_call["id"],
                    context.session_id,
                    model_attempt,
                    index,
                    message_id,
                    raw_call["function"]["name"],
                    str(safe_call["function"]["arguments"]),
                    _digest(raw_call),
                    now,
                    now,
                ),
            )
        updated = _update_journal(
            connection,
            store,
            context,
            current,
            phase=RunPhase.TOOLS,
            boundary=RunStableBoundary.ASSISTANT_ENVELOPE_COMMITTED,
            loop_cursor=next_cursor,
        )
        connection.execute(
            "UPDATE sessions SET updated_at=? WHERE id=?",
            (now, context.session_id),
        )
        persisted = connection.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        return ToolMessageCommit(_message_from_row(persisted), updated, False)


def _validate_tool_result(message: Mapping[str, Any]) -> tuple[str, str, str]:
    if message.get("role") != "tool":
        raise ToolMessagePairingError("tool result must have tool role")
    call_id = message.get("tool_call_id")
    name = message.get("name")
    content = message.get("content")
    if not isinstance(call_id, str) or not call_id.strip():
        raise ToolMessagePairingError("tool result must reference a call id")
    if not isinstance(name, str) or not name.strip():
        raise ToolMessagePairingError("tool result must name the tool")
    if not isinstance(content, str):
        raise ToolMessagePairingError("tool result content must be a string")
    return call_id, name, content


def _validate_operation(
    connection: sqlite3.Connection,
    context,
    call: sqlite3.Row,
    *,
    operation_id: str | None,
    content: str,
) -> bool:
    call_reference = connection.execute(
        """
        SELECT * FROM tool_operation_refs
        WHERE run_id=? AND tool_call_id=?
        ORDER BY updated_at DESC LIMIT 1
        """,
        (context.run_id, call["tool_call_id"]),
    ).fetchone()
    if call_reference is not None and operation_id is None:
        raise ToolMessageOperationError(
            "a write-tool result must reference its existing ToolOperation",
            tool_call_id=call["tool_call_id"],
            operation_id=call_reference["operation_id"],
        )
    if operation_id is None:
        return False
    reference = connection.execute(
        "SELECT * FROM tool_operation_refs WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    if reference is None:
        raise ToolMessageOperationError(
            "tool result references an unknown ToolOperation",
            tool_call_id=call["tool_call_id"],
            operation_id=operation_id,
        )
    if (
        reference["actor_id"] != context.actor_id
        or reference["tenant_id"] != context.tenant_id
        or reference["tool_name"] != call["tool_name"]
    ):
        raise ToolMessageOperationError(
            "ToolOperation is outside the result owner or tool scope",
            operation_id=operation_id,
        )
    try:
        decoded = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ToolMessageOperationError(
            "write-tool result must contain structured JSON",
            operation_id=operation_id,
        ) from error
    meta = decoded.get("meta") if isinstance(decoded, dict) else None
    if not isinstance(meta, dict) or meta.get("operation_id") != operation_id:
        raise ToolMessageOperationError(
            "write-tool result payload must carry the same operation id",
            operation_id=operation_id,
        )
    same_run_call = (
        reference["run_id"] == context.run_id
        and reference["session_id"] == context.session_id
        and reference["tool_call_id"] == call["tool_call_id"]
    )
    if not same_run_call and meta.get("idempotent_replay") is not True:
        raise ToolMessageOperationError(
            "ToolOperation belongs to another call without an idempotent replay receipt",
            operation_id=operation_id,
            operation_run_id=reference["run_id"],
            result_run_id=context.run_id,
        )
    return same_run_call


def append_tool_result(
    store,
    context,
    message: Mapping[str, Any],
    *,
    model_attempt: int,
    operation_id: str | None = None,
    tool_event_id: int | None = None,
    allow_cancelled: bool = False,
) -> ToolMessageCommit:
    _validate_model_attempt(model_attempt)
    call_id, name, content = _validate_tool_result(message)
    raw_payload = {
        "role": "tool",
        "tool_call_id": call_id,
        "name": name,
        "content": content,
        "operation_id": operation_id,
    }
    result_hash = _digest(raw_payload)
    try:
        decoded_content = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        safe_content = redact_sensitive(content)
    else:
        safe_content = _json(redact_sensitive(decoded_content))

    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        store._assert_fence(
            connection,
            context,
            boundary="tool.result.commit",
            allow_cancelled=allow_cancelled,
        )
        current = _load_journal(connection, context)
        call = connection.execute(
            "SELECT * FROM agent_tool_calls WHERE run_id=? AND tool_call_id=?",
            (context.run_id, call_id),
        ).fetchone()
        if call is None:
            other = connection.execute(
                "SELECT run_id FROM agent_tool_calls WHERE tool_call_id=? LIMIT 1",
                (call_id,),
            ).fetchone()
            if other is not None:
                raise ToolMessagePairingError(
                    "tool result cannot pair across runs",
                    tool_call_id=call_id,
                    envelope_run_id=other["run_id"],
                    result_run_id=context.run_id,
                )
            raise ToolMessagePairingError(
                "orphan tool result has no assistant call",
                tool_call_id=call_id,
            )
        if call["session_id"] != context.session_id:
            raise ToolMessagePairingError(
                "tool result session does not match its envelope",
                tool_call_id=call_id,
            )
        if int(call["model_attempt"]) != model_attempt:
            raise ToolMessagePairingError(
                "tool result model attempt does not match its envelope",
                tool_call_id=call_id,
                envelope_attempt=int(call["model_attempt"]),
                result_attempt=model_attempt,
            )
        if call["status"] == "completed":
            if call["result_hash"] != result_hash or call["operation_id"] != operation_id:
                raise ToolMessageConflict(
                    "tool call already has a different result",
                    tool_call_id=call_id,
                )
            persisted = connection.execute(
                "SELECT * FROM messages WHERE id=?",
                (call["result_message_id"],),
            ).fetchone()
            return ToolMessageCommit(_message_from_row(persisted), current, True)
        if current.phase is not RunPhase.TOOLS or current.model_attempt != model_attempt:
            raise RunJournalTransitionError(
                "tool result may only commit in the matching tools phase",
                phase=current.phase.value,
                journal_attempt=current.model_attempt,
                model_attempt=model_attempt,
            )
        if call["tool_name"] != name:
            raise ToolMessagePairingError(
                "tool result name does not match its call",
                tool_call_id=call_id,
                expected_tool=call["tool_name"],
                actual_tool=name,
            )
        earlier = connection.execute(
            """
            SELECT tool_call_id FROM agent_tool_calls
            WHERE run_id=? AND model_attempt=? AND call_index<? AND status='pending'
            ORDER BY call_index LIMIT 1
            """,
            (context.run_id, model_attempt, int(call["call_index"])),
        ).fetchone()
        if earlier is not None:
            raise ToolMessagePairingError(
                "tool results must commit in assistant call order",
                pending_tool_call_id=earlier["tool_call_id"],
                tool_call_id=call_id,
            )
        journal_operation = _validate_operation(
            connection,
            context,
            call,
            operation_id=operation_id,
            content=content,
        )
        if tool_event_id is not None:
            event = connection.execute(
                """
                SELECT id FROM tool_events
                WHERE id=? AND run_id=? AND session_id=? AND tool_call_id=?
                """,
                (tool_event_id, context.run_id, context.session_id, call_id),
            ).fetchone()
            if event is None:
                raise ToolMessagePairingError(
                    "tool result references an event outside its call",
                    tool_event_id=tool_event_id,
                    tool_call_id=call_id,
                )

        next_cursor = current.loop_cursor + 1
        sequence = _next_message_sequence(connection, context.session_id)
        now = store.now_iso()
        inserted = connection.execute(
            """
            INSERT INTO messages(
                session_id, sequence, role, content, name, tool_call_id,
                run_id, fencing_token, idempotency_key, model_attempt,
                loop_cursor, created_at
            ) VALUES (?, ?, 'tool', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                context.session_id,
                sequence,
                safe_content,
                name,
                call_id,
                context.run_id,
                int(context.fencing_token),
                f"tool-result:{call_id}",
                model_attempt,
                next_cursor,
                now,
            ),
        )
        message_id = int(inserted.lastrowid)
        updated_call = connection.execute(
            """
            UPDATE agent_tool_calls
            SET status='completed', result_message_id=?, result_hash=?,
                operation_id=?, result_cursor=?, updated_at=?
            WHERE run_id=? AND tool_call_id=? AND status='pending'
            """,
            (
                message_id,
                result_hash,
                operation_id,
                next_cursor,
                now,
                context.run_id,
                call_id,
            ),
        )
        if updated_call.rowcount != 1:
            raise ToolMessageConflict(
                "tool result lost a concurrent commit race",
                tool_call_id=call_id,
            )
        updated = _update_journal(
            connection,
            store,
            context,
            current,
            phase=RunPhase.TOOLS,
            boundary=RunStableBoundary.TOOL_RESULT_COMMITTED,
            loop_cursor=next_cursor,
            operation_id=operation_id if journal_operation else None,
            last_tool_event_id=tool_event_id,
        )
        connection.execute(
            "UPDATE sessions SET updated_at=? WHERE id=?",
            (now, context.session_id),
        )
        persisted = connection.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        return ToolMessageCommit(_message_from_row(persisted), updated, False)


def complete_tool_batch(store, context, *, model_attempt: int) -> RunJournalSnapshot:
    _validate_model_attempt(model_attempt)
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        store._assert_fence(connection, context, boundary="tool.batch.complete")
        current = _load_journal(connection, context)
        envelope = connection.execute(
            """
            SELECT 1 FROM agent_tool_envelopes
            WHERE run_id=? AND model_attempt=?
            """,
            (context.run_id, model_attempt),
        ).fetchone()
        if envelope is None:
            raise ToolMessagePairingError(
                "tool batch has no assistant envelope",
                model_attempt=model_attempt,
            )
        pending = connection.execute(
            """
            SELECT tool_call_id FROM agent_tool_calls
            WHERE run_id=? AND model_attempt=? AND status='pending'
            ORDER BY call_index
            """,
            (context.run_id, model_attempt),
        ).fetchall()
        if pending:
            raise ToolMessagePairingError(
                "tool batch cannot complete with unpaired calls",
                pending_call_ids=[row["tool_call_id"] for row in pending],
            )
        if current.phase is RunPhase.VERIFYING and current.model_attempt == model_attempt:
            return current
        if current.phase is not RunPhase.TOOLS or current.model_attempt != model_attempt:
            raise RunJournalTransitionError(
                "tool batch may only complete from its tools phase",
                phase=current.phase.value,
                journal_attempt=current.model_attempt,
                model_attempt=model_attempt,
            )
        return _update_journal(
            connection,
            store,
            context,
            current,
            phase=RunPhase.VERIFYING,
            boundary=RunStableBoundary.TOOL_RESULT_COMMITTED,
            loop_cursor=current.loop_cursor + 1,
        )


def get_tool_call_record(
    store,
    *,
    run_id: str,
    tool_call_id: str,
    session_id: str,
    actor_id: str,
    tenant_id: str,
) -> dict[str, Any] | None:
    with store.connect() as connection:
        run = connection.execute(
            "SELECT session_id, actor_id, tenant_id FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if run is None:
            return None
        if (run["session_id"], run["actor_id"], run["tenant_id"]) != (
            session_id,
            actor_id,
            tenant_id,
        ):
            raise PermissionError("tool call does not belong to the requested run scope")
        row = connection.execute(
            "SELECT * FROM agent_tool_calls WHERE run_id=? AND tool_call_id=?",
            (run_id, tool_call_id),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        if row["result_message_id"] is not None:
            message = connection.execute(
                "SELECT * FROM messages WHERE id=?",
                (row["result_message_id"],),
            ).fetchone()
            result["result_message"] = _message_from_row(message)
        else:
            result["result_message"] = None
        return result


def list_tool_call_records(
    store,
    *,
    run_id: str,
    session_id: str,
    actor_id: str,
    tenant_id: str,
) -> list[dict[str, Any]]:
    with store.connect() as connection:
        run = connection.execute(
            "SELECT session_id, actor_id, tenant_id FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if run is None:
            return []
        if (run["session_id"], run["actor_id"], run["tenant_id"]) != (
            session_id,
            actor_id,
            tenant_id,
        ):
            raise PermissionError("tool calls do not belong to the requested run scope")
        rows = connection.execute(
            """
            SELECT * FROM agent_tool_calls
            WHERE run_id=? ORDER BY model_attempt, call_index
            """,
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]


__all__ = [
    "AGENT_TOOL_MESSAGES_MIGRATION",
    "ToolMessageCommit",
    "ToolMessageConflict",
    "ToolMessageError",
    "ToolMessageOperationError",
    "ToolMessagePairingError",
    "ToolMessageSchemaError",
    "append_assistant_tool_envelope",
    "append_tool_result",
    "complete_tool_batch",
    "get_tool_call_record",
    "initialize_agent_tool_message_schema",
    "list_tool_call_records",
]
