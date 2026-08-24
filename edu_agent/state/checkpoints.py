from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping


CHECKPOINT_MIGRATION = "013_context_checkpoint_provenance"
CHECKPOINT_SCHEMA_VERSION = 2
CHECKPOINT_STRATEGY_VERSION = "artifact-first-checkpoint@2026-08-24.v1"
CHECKPOINT_ESTIMATOR_VERSION = "legacy-json-chars-div4@r4.1.v1"

_JSON_FIELDS = {
    "source_sequences_json": "source_sequences",
    "source_hashes_json": "source_hashes",
    "preserved_items_json": "preserved_items",
    "artifact_refs_json": "artifact_refs",
    "citation_refs_json": "citation_refs",
    "operation_refs_json": "operation_refs",
}


class ContextCheckpointError(RuntimeError):
    code = "CONTEXT_CHECKPOINT_ERROR"

    def __init__(self, message: str, **details: Any):
        super().__init__(message)
        self.error_code = self.code
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.error_code,
            "message": str(self),
            "details": dict(self.details),
        }


class ContextCheckpointConflict(ContextCheckpointError):
    code = "CONTEXT_CHECKPOINT_CONFLICT"


class ContextCheckpointValidationError(ContextCheckpointError):
    code = "CONTEXT_CHECKPOINT_INVALID"

    def __init__(self, reason: str, message: str, **details: Any):
        super().__init__(message, reason=reason, **details)
        self.reason = reason
        self.error_code = f"CONTEXT_CHECKPOINT_{reason.upper()}"


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def message_from_row(row: Mapping[str, Any], *, include_sequence: bool = False) -> dict:
    message: dict[str, Any] = {
        "role": row["role"],
        "content": row["content"] or "",
    }
    if row["name"]:
        message["name"] = row["name"]
    if row["tool_call_id"]:
        message["tool_call_id"] = row["tool_call_id"]
    if row["tool_calls_json"]:
        try:
            message["tool_calls"] = json.loads(row["tool_calls_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ContextCheckpointValidationError(
                "source_message_invalid",
                "checkpoint source message contains invalid tool_calls JSON",
                sequence=int(row["sequence"]),
            ) from error
    if include_sequence:
        message["sequence"] = int(row["sequence"])
        message["run_id"] = row["run_id"]
        message["active"] = bool(row["active"])
        message["compaction_id"] = row["compaction_id"]
    return message


def message_hash(row: Mapping[str, Any]) -> str:
    payload = {
        "sequence": int(row["sequence"]),
        "message": message_from_row(row),
    }
    return sha256_text(canonical_json(payload))


def source_hashes(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"sequence": int(row["sequence"]), "sha256": message_hash(row)}
        for row in rows
    ]


def source_digest(hashes: list[dict[str, Any]]) -> str:
    return sha256_text(canonical_json(hashes))


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def initialize_context_checkpoint_schema(
    connection: sqlite3.Connection,
    *,
    now: str,
) -> None:
    additions = {
        "schema_version": "INTEGER NOT NULL DEFAULT 1",
        "actor_id": "TEXT",
        "tenant_id": "TEXT",
        "created_run_id": "TEXT",
        "source_sequences_json": "TEXT",
        "source_hashes_json": "TEXT",
        "source_sha256": "TEXT",
        "strategy_version": "TEXT",
        "estimator_version": "TEXT",
        "summary_sha256": "TEXT",
        "estimated_tokens_after": "INTEGER",
        "preserved_items_json": "TEXT",
        "artifact_refs_json": "TEXT",
        "citation_refs_json": "TEXT",
        "operation_refs_json": "TEXT",
        "parent_checkpoint_id": "TEXT",
        "parent_summary_sha256": "TEXT",
    }
    existing = _columns(connection, "context_checkpoints")
    for name, declaration in additions.items():
        if name not in existing:
            connection.execute(
                f"ALTER TABLE context_checkpoints ADD COLUMN {name} {declaration}"
            )

    rows = connection.execute("SELECT * FROM context_checkpoints ORDER BY created_at, id").fetchall()
    for row in rows:
        if int(row["schema_version"] or 1) >= CHECKPOINT_SCHEMA_VERSION:
            continue
        session = connection.execute(
            "SELECT actor_id, tenant_id FROM sessions WHERE id=?",
            (row["session_id"],),
        ).fetchone()
        sources = connection.execute(
            """
            SELECT * FROM messages
            WHERE session_id=? AND (
                compaction_id=? OR sequence BETWEEN ? AND ?
            )
            ORDER BY sequence
            """,
            (
                row["session_id"],
                row["id"],
                int(row["first_sequence"]),
                int(row["last_sequence"]),
            ),
        ).fetchall()
        hashes = source_hashes(sources) if sources else []
        sequences = [int(item["sequence"]) for item in sources]
        connection.execute(
            """
            UPDATE context_checkpoints
            SET actor_id=COALESCE(actor_id, ?),
                tenant_id=COALESCE(tenant_id, ?),
                source_sequences_json=COALESCE(source_sequences_json, ?),
                source_hashes_json=COALESCE(source_hashes_json, ?),
                source_sha256=COALESCE(source_sha256, ?),
                strategy_version=COALESCE(strategy_version, 'legacy-prefix@v1'),
                estimator_version=COALESCE(
                    estimator_version, 'legacy-json-chars-div4@v1'
                ),
                summary_sha256=COALESCE(summary_sha256, ?),
                estimated_tokens_after=COALESCE(estimated_tokens_after, ?),
                preserved_items_json=COALESCE(preserved_items_json, '[]'),
                artifact_refs_json=COALESCE(artifact_refs_json, '[]'),
                citation_refs_json=COALESCE(citation_refs_json, '[]'),
                operation_refs_json=COALESCE(operation_refs_json, '[]')
            WHERE id=?
            """,
            (
                session["actor_id"] if session else None,
                session["tenant_id"] if session else None,
                canonical_json(sequences),
                canonical_json(hashes),
                source_digest(hashes) if hashes else None,
                sha256_text(row["summary"]),
                max(1, len(row["summary"]) // 4),
                row["id"],
            ),
        )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_context_checkpoints_source
        ON context_checkpoints(session_id, source_sha256)
        WHERE schema_version >= 2 AND source_sha256 IS NOT NULL
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO state_schema_migrations(version, applied_at)
        VALUES (?, ?)
        """,
        (CHECKPOINT_MIGRATION, now),
    )


def decode_checkpoint(row: Mapping[str, Any]) -> dict[str, Any]:
    record = dict(row)
    checkpoint_id = record.get("id")
    for source, target in _JSON_FIELDS.items():
        raw = record.get(source)
        if raw in (None, ""):
            value = []
        else:
            try:
                value = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise ContextCheckpointValidationError(
                    "provenance_json_invalid",
                    f"checkpoint {source} contains invalid JSON",
                    checkpoint_id=checkpoint_id,
                    field=source,
                ) from error
        if not isinstance(value, list):
            raise ContextCheckpointValidationError(
                "provenance_json_invalid",
                f"checkpoint {source} must contain a JSON array",
                checkpoint_id=checkpoint_id,
                field=source,
            )
        record[target] = value
    try:
        record["schema_version"] = int(record.get("schema_version") or 1)
    except (TypeError, ValueError) as error:
        raise ContextCheckpointValidationError(
            "schema_version_invalid",
            "checkpoint schema version is invalid",
            checkpoint_id=checkpoint_id,
        ) from error
    return record


def _fail(reason: str, message: str, record: Mapping[str, Any], **details: Any) -> None:
    raise ContextCheckpointValidationError(
        reason,
        message,
        checkpoint_id=record.get("id"),
        session_id=record.get("session_id"),
        **details,
    )


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_artifact(
    connection: sqlite3.Connection,
    record: Mapping[str, Any],
    reference: Any,
    *,
    actor_id: str,
    tenant_id: str,
) -> None:
    if int(record.get("schema_version") or 1) >= CHECKPOINT_SCHEMA_VERSION and (
        not isinstance(reference, dict)
        or reference.get("type") != "edu-agent.scoped-artifact.v1"
        or not _is_sha256(reference.get("sha256"))
    ):
        _fail("artifact_reference_invalid", "checkpoint Artifact reference is invalid", record)
    artifact_id = reference.get("artifact_id") if isinstance(reference, dict) else reference
    if not isinstance(artifact_id, str) or not artifact_id:
        _fail("artifact_reference_invalid", "checkpoint artifact reference is invalid", record)
    artifact = connection.execute(
        "SELECT * FROM artifacts WHERE id=?",
        (artifact_id,),
    ).fetchone()
    if artifact is None:
        _fail(
            "artifact_missing",
            "checkpoint artifact is missing",
            record,
            artifact_id=artifact_id,
        )
    if (
        artifact["actor_id"] != actor_id
        or artifact["tenant_id"] != tenant_id
        or artifact["session_id"] != record["session_id"]
    ):
        _fail(
            "artifact_scope_mismatch",
            "checkpoint artifact is outside checkpoint scope",
            record,
            artifact_id=artifact_id,
        )
    if isinstance(reference, dict) and reference.get("sha256") not in {
        None,
        artifact["sha256"],
    }:
        _fail(
            "artifact_hash_mismatch",
            "checkpoint artifact reference hash does not match its index",
            record,
            artifact_id=artifact_id,
        )
    try:
        payload = Path(artifact["path"]).read_bytes()
    except OSError as error:
        _fail(
            "artifact_missing",
            "checkpoint artifact payload is unavailable",
            record,
            artifact_id=artifact_id,
            error_class=type(error).__name__,
        )
    if len(payload) != int(artifact["size_bytes"]):
        _fail(
            "artifact_hash_mismatch",
            "checkpoint artifact size does not match its index",
            record,
            artifact_id=artifact_id,
        )
    if hashlib.sha256(payload).hexdigest() != artifact["sha256"]:
        _fail(
            "artifact_hash_mismatch",
            "checkpoint artifact payload hash does not match its index",
            record,
            artifact_id=artifact_id,
        )


def validate_checkpoint(
    connection: sqlite3.Connection,
    row: Mapping[str, Any],
    *,
    session_id: str,
    actor_id: str,
    tenant_id: str,
    _visited: set[str] | None = None,
) -> dict[str, Any]:
    record = decode_checkpoint(row)
    schema_version = record["schema_version"]
    if schema_version < 1 or schema_version > CHECKPOINT_SCHEMA_VERSION:
        _fail(
            "schema_version_unsupported",
            "checkpoint schema version is not supported",
            record,
            schema_version=schema_version,
        )
    if (
        record["session_id"] != session_id
        or record.get("actor_id") not in {None, actor_id}
        or record.get("tenant_id") not in {None, tenant_id}
    ):
        _fail("scope_mismatch", "checkpoint scope does not match the reader", record)
    session = connection.execute(
        "SELECT actor_id, tenant_id FROM sessions WHERE id=?",
        (session_id,),
    ).fetchone()
    if session is None or session["actor_id"] != actor_id or session["tenant_id"] != tenant_id:
        _fail("scope_mismatch", "checkpoint session owner does not match the reader", record)

    try:
        source_messages = int(record["source_messages"])
        first_sequence = int(record["first_sequence"])
        last_sequence = int(record["last_sequence"])
        estimated_before = int(record["estimated_tokens_before"])
    except (KeyError, TypeError, ValueError):
        _fail("provenance_invalid", "checkpoint numeric provenance is invalid", record)
    if source_messages <= 0 or first_sequence < 0 or last_sequence < first_sequence:
        _fail("provenance_invalid", "checkpoint source range is invalid", record)
    if estimated_before < 0:
        _fail("provenance_invalid", "checkpoint token estimate is invalid", record)

    sequences = record["source_sequences"]
    hashes = record["source_hashes"]
    if sequences and (
        any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in sequences)
        or sequences != sorted(set(sequences))
    ):
        _fail("source_manifest_invalid", "checkpoint source sequences are invalid", record)
    if sequences and (
        len(sequences) != source_messages
        or sequences[0] != first_sequence
        or sequences[-1] != last_sequence
    ):
        _fail("source_manifest_invalid", "checkpoint source range does not match its manifest", record)

    if schema_version >= CHECKPOINT_SCHEMA_VERSION:
        required_text = {
            "actor_id": record.get("actor_id"),
            "tenant_id": record.get("tenant_id"),
            "created_run_id": record.get("created_run_id"),
            "source_sha256": record.get("source_sha256"),
            "strategy_version": record.get("strategy_version"),
            "estimator_version": record.get("estimator_version"),
            "summary_sha256": record.get("summary_sha256"),
        }
        missing = sorted(
            key for key, value in required_text.items() if not isinstance(value, str) or not value
        )
        if missing or record.get("estimated_tokens_after") is None:
            _fail(
                "provenance_incomplete",
                "checkpoint provenance is incomplete",
                record,
                fields=missing,
            )
        if record["actor_id"] != actor_id or record["tenant_id"] != tenant_id:
            _fail("scope_mismatch", "checkpoint owner scope is incomplete", record)
        if not _is_sha256(record["source_sha256"]):
            _fail("source_manifest_invalid", "checkpoint aggregate source hash is invalid", record)
        if not _is_sha256(record["summary_sha256"]):
            _fail("summary_hash_mismatch", "checkpoint summary hash is invalid", record)
        try:
            estimated_after = int(record["estimated_tokens_after"])
        except (TypeError, ValueError):
            _fail("provenance_invalid", "checkpoint post-compaction estimate is invalid", record)
        if estimated_after < 0:
            _fail("provenance_invalid", "checkpoint post-compaction estimate is invalid", record)
        if not sequences or len(hashes) != len(sequences):
            _fail("source_manifest_invalid", "checkpoint source manifest is incomplete", record)
        if any(
            not isinstance(item, dict)
            or item.get("sequence") != sequence
            or not _is_sha256(item.get("sha256"))
            for sequence, item in zip(sequences, hashes, strict=True)
        ):
            _fail("source_manifest_invalid", "checkpoint source hash manifest is invalid", record)
        if source_digest(hashes) != record["source_sha256"]:
            _fail("source_hash_mismatch", "checkpoint aggregate source hash verification failed", record)
    elif source_messages and not sequences:
        _fail("source_missing", "legacy checkpoint source messages are missing", record)

    expected_summary = record.get("summary_sha256")
    if expected_summary and sha256_text(record["summary"]) != expected_summary:
        _fail("summary_hash_mismatch", "checkpoint summary hash verification failed", record)

    created_run_id = record.get("created_run_id")
    if created_run_id:
        created_run = connection.execute(
            "SELECT session_id, actor_id, tenant_id FROM runs WHERE id=?",
            (created_run_id,),
        ).fetchone()
        if created_run is None:
            _fail("created_run_missing", "checkpoint creation run is missing", record)
        if (
            created_run["session_id"] != session_id
            or created_run["actor_id"] != actor_id
            or created_run["tenant_id"] != tenant_id
        ):
            _fail(
                "created_run_scope_mismatch",
                "checkpoint creation run is outside checkpoint scope",
                record,
            )

    if sequences:
        placeholders = ",".join("?" for _ in sequences)
        sources = connection.execute(
            f"""
            SELECT * FROM messages
            WHERE session_id=? AND sequence IN ({placeholders})
            ORDER BY sequence
            """,
            (session_id, *sequences),
        ).fetchall()
        if len(sources) != len(sequences):
            _fail("source_missing", "checkpoint source messages are missing", record)
        actual_hashes = source_hashes(sources)
        if hashes and actual_hashes != hashes:
            _fail("source_hash_mismatch", "checkpoint source message hash verification failed", record)
        expected_source = record.get("source_sha256")
        if expected_source and source_digest(actual_hashes) != expected_source:
            _fail("source_hash_mismatch", "checkpoint aggregate source hash verification failed", record)
        if schema_version >= CHECKPOINT_SCHEMA_VERSION and any(
            source["compaction_id"] != record["id"] or bool(source["active"])
            for source in sources
        ):
            _fail("source_archive_invalid", "checkpoint source archive state is invalid", record)

    for reference in record["artifact_refs"]:
        _validate_artifact(
            connection,
            record,
            reference,
            actor_id=actor_id,
            tenant_id=tenant_id,
        )
    for reference in record["operation_refs"]:
        operation_id = (
            reference.get("operation_id") if isinstance(reference, dict) else reference
        )
        if schema_version >= CHECKPOINT_SCHEMA_VERSION and (
            not isinstance(reference, dict)
            or not isinstance(operation_id, str)
            or not operation_id
            or not _is_sha256(reference.get("payload_hash"))
        ):
            _fail("operation_reference_invalid", "checkpoint operation reference is invalid", record)
        operation = connection.execute(
            "SELECT * FROM tool_operation_refs WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if operation is None:
            _fail(
                "operation_missing",
                "checkpoint operation reference is missing",
                record,
                operation_id=operation_id,
            )
        if (
            operation["actor_id"] != actor_id
            or operation["tenant_id"] != tenant_id
            or operation["session_id"] != session_id
        ):
            _fail(
                "operation_scope_mismatch",
                "checkpoint operation is outside checkpoint scope",
                record,
                operation_id=operation_id,
            )
        if isinstance(reference, dict) and reference.get("payload_hash") not in {
            None,
            operation["payload_hash"],
        }:
            _fail(
                "operation_hash_mismatch",
                "checkpoint operation payload hash does not match its index",
                record,
                operation_id=operation_id,
            )

    parent_id = record.get("parent_checkpoint_id")
    if parent_id:
        if schema_version >= CHECKPOINT_SCHEMA_VERSION and not _is_sha256(
            record.get("parent_summary_sha256")
        ):
            _fail("parent_hash_mismatch", "checkpoint parent summary hash is missing", record)
        visited = set(_visited or ())
        if record["id"] in visited or parent_id in visited:
            _fail("parent_cycle", "checkpoint parent chain contains a cycle", record)
        visited.add(record["id"])
        parent = connection.execute(
            "SELECT * FROM context_checkpoints WHERE id=?",
            (parent_id,),
        ).fetchone()
        if parent is None:
            _fail("parent_missing", "checkpoint parent is missing", record)
        expected_parent_hash = record.get("parent_summary_sha256")
        if expected_parent_hash and sha256_text(parent["summary"]) != expected_parent_hash:
            _fail("parent_hash_mismatch", "checkpoint parent summary hash failed", record)
        validate_checkpoint(
            connection,
            parent,
            session_id=session_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
            _visited=visited,
        )
    elif record.get("parent_summary_sha256") is not None:
        _fail("parent_hash_mismatch", "checkpoint parent hash has no parent", record)
    return record


__all__ = [
    "CHECKPOINT_ESTIMATOR_VERSION",
    "CHECKPOINT_MIGRATION",
    "CHECKPOINT_SCHEMA_VERSION",
    "CHECKPOINT_STRATEGY_VERSION",
    "ContextCheckpointConflict",
    "ContextCheckpointError",
    "ContextCheckpointValidationError",
    "canonical_json",
    "decode_checkpoint",
    "initialize_context_checkpoint_schema",
    "message_from_row",
    "message_hash",
    "sha256_text",
    "source_digest",
    "source_hashes",
    "validate_checkpoint",
]
