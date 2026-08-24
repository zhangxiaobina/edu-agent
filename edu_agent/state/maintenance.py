from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sqlite3
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


BACKUP_FORMAT = "edu-agent-state-backup.v1"
MANIFEST_NAME = "manifest.json"
BACKUP_DATABASE_NAME = "state.db"
_TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "interrupted"})
_TERMINAL_JOURNAL_PHASES = frozenset({"terminal", "cancelled", "failed"})
_TERMINAL_OPERATION_STATUSES = frozenset({"committed", "failed", "compensated"})
_TERMINAL_DELEGATION_STATUSES = frozenset(
    {"completed", "failed", "timed_out", "cancelled"}
)


class StateMaintenanceError(RuntimeError):
    code = "STATE_MAINTENANCE_ERROR"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.error_code = self.code
        self.details = details or {}


class StateIntegrityError(StateMaintenanceError):
    code = "STATE_INTEGRITY_FAILED"


class BackupRefusedError(StateMaintenanceError):
    code = "STATE_BACKUP_REFUSED"


class BackupValidationError(StateMaintenanceError):
    code = "STATE_BACKUP_INVALID"


class RestoreRefusedError(StateMaintenanceError):
    code = "STATE_RESTORE_REFUSED"


class RetentionError(StateMaintenanceError):
    code = "STATE_RETENTION_FAILED"


@dataclass(frozen=True)
class IntegrityReport:
    schema_version: int
    integrity: str
    foreign_key_violations: int
    sessions: int
    runs: int
    journals: int
    operations: int
    artifacts: int
    checkpoints: int
    artifact_references: int
    operation_references: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BackupResult:
    target: str
    manifest_sha256: str
    schema_version: int
    database_sha256: str
    artifact_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RestoreResult:
    target_dir: str
    state_path: str
    artifact_root: str
    source_schema_version: int
    restored_schema_version: int
    manifest_sha256: str
    integrity: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RetentionPolicy:
    terminal_age_seconds: int = 30 * 24 * 60 * 60
    artifact_age_seconds: int = 30 * 24 * 60 * 60
    batch_size: int = 100

    def __post_init__(self) -> None:
        values = {
            "terminal_age_seconds": self.terminal_age_seconds,
            "artifact_age_seconds": self.artifact_age_seconds,
            "batch_size": self.batch_size,
        }
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values.values()):
            raise ValueError("retention policy values must be integers")
        if self.terminal_age_seconds <= 0 or self.artifact_age_seconds <= 0:
            raise ValueError("retention ages must be positive")
        if self.batch_size <= 0 or self.batch_size > 1000:
            raise ValueError("retention batch_size must be between 1 and 1000")


@dataclass
class GcReport:
    dry_run: bool
    cutoff: str
    artifact_cutoff: str
    scanned_sessions: int
    eligible_sessions: list[str] = field(default_factory=list)
    blocked_sessions: dict[str, list[str]] = field(default_factory=dict)
    eligible_artifacts: list[str] = field(default_factory=list)
    deleted_sessions: int = 0
    deleted_runs: int = 0
    deleted_artifacts: int = 0
    resumed_artifact_deletions: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _column_exists(connection: sqlite3.Connection, table: str, column: str) -> bool:
    return _table_exists(connection, table) and any(
        str(row[1]) == column for row in connection.execute(f"PRAGMA table_info({table})")
    )


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _manifest_hash(manifest: dict[str, Any]) -> str:
    content = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    return hashlib.sha256(_canonical_json(content)).hexdigest()


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _owned_stage(parent: Path, label: str) -> Path:
    stage = parent / f".{label}.edu-agent-{uuid.uuid4().hex}"
    stage.mkdir(mode=0o700)
    return stage


def _remove_owned_stage(stage: Path, *, parent: Path, label: str) -> None:
    resolved_parent = parent.resolve()
    resolved_stage = stage.resolve()
    if (
        resolved_stage.parent != resolved_parent
        or not stage.name.startswith(f".{label}.edu-agent-")
    ):
        raise RuntimeError("refusing to remove an unowned maintenance path")
    if stage.exists():
        shutil.rmtree(stage)


def _json_value(value: str | None, *, field_name: str) -> Any:
    try:
        return json.loads(value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise StateIntegrityError(
            "state reference validation failed",
            details={"invalid_json_field": field_name},
        ) from error


def _reservation_is_clear(value: str | None) -> bool:
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(decoded, dict) and all(
        not isinstance(amount, bool)
        and isinstance(amount, (int, float))
        and amount == 0
        for amount in decoded.values()
    )


def _find_named_ids(value: Any, field_name: str) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == field_name and isinstance(item, str) and item:
                found.add(item)
            found.update(_find_named_ids(item, field_name))
    elif isinstance(value, list):
        for item in value:
            found.update(_find_named_ids(item, field_name))
    return found


def _artifact_references(
    connection: sqlite3.Connection,
) -> dict[str, list[tuple[str, str | None]]]:
    references: dict[str, list[tuple[str, str | None]]] = {}

    def add(artifact_id: str | None, source: str, session_id: str | None) -> None:
        if artifact_id:
            references.setdefault(str(artifact_id), []).append((source, session_id))

    if _table_exists(connection, "evidence"):
        for row in connection.execute(
            "SELECT artifact_id, session_id FROM evidence WHERE artifact_id IS NOT NULL"
        ):
            add(row["artifact_id"], "evidence", row["session_id"])
    if _table_exists(connection, "run_journals"):
        for row in connection.execute(
            "SELECT artifact_id, session_id FROM run_journals WHERE artifact_id IS NOT NULL"
        ):
            add(row["artifact_id"], "run_journal", row["session_id"])
    if _table_exists(connection, "delegation_runs"):
        for row in connection.execute(
            """
            SELECT result_artifact_id, session_id FROM delegation_runs
            WHERE result_artifact_id IS NOT NULL
            """
        ):
            add(row["result_artifact_id"], "delegation_run", row["session_id"])
    if _table_exists(connection, "context_checkpoints"):
        for row in connection.execute(
            "SELECT id, session_id, artifact_refs_json FROM context_checkpoints"
        ):
            value = _json_value(
                row["artifact_refs_json"],
                field_name=f"context_checkpoints:{row['id']}:artifact_refs_json",
            )
            for artifact_id in _find_named_ids(value, "artifact_id"):
                add(artifact_id, "context_checkpoint", row["session_id"])
    if _table_exists(connection, "messages"):
        for row in connection.execute(
            "SELECT id, session_id, content FROM messages WHERE content IS NOT NULL"
        ):
            try:
                value = json.loads(row["content"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            for artifact_id in _find_named_ids(value, "artifact_id"):
                add(artifact_id, "message", row["session_id"])
    return references


def _operation_references(
    connection: sqlite3.Connection,
) -> dict[str, list[tuple[str, str | None]]]:
    references: dict[str, list[tuple[str, str | None]]] = {}

    def add(operation_id: str | None, source: str, session_id: str | None) -> None:
        if operation_id:
            references.setdefault(str(operation_id), []).append((source, session_id))

    for table, column in (
        ("run_journals", "operation_id"),
        ("agent_tool_calls", "operation_id"),
        ("evidence", "operation_id"),
        ("tool_events", "operation_id"),
    ):
        if not _table_exists(connection, table):
            continue
        for row in connection.execute(
            f"SELECT {column}, session_id FROM {table} WHERE {column} IS NOT NULL"
        ):
            add(row[column], table, row["session_id"])
    if _table_exists(connection, "context_checkpoints"):
        for row in connection.execute(
            "SELECT id, session_id, operation_refs_json FROM context_checkpoints"
        ):
            value = _json_value(
                row["operation_refs_json"],
                field_name=f"context_checkpoints:{row['id']}:operation_refs_json",
            )
            for operation_id in _find_named_ids(value, "operation_id"):
                add(operation_id, "context_checkpoint", row["session_id"])
    return references


def _record_count(connection: sqlite3.Connection, table: str) -> int:
    if not _table_exists(connection, table):
        return 0
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def validate_state_integrity(
    connection: sqlite3.Connection,
    *,
    artifact_root: Path | None = None,
    artifact_paths: dict[str, Path] | None = None,
    require_current_schema: bool = True,
) -> IntegrityReport:
    from .store import STATE_SCHEMA_VERSION, STORAGE_MAINTENANCE_MIGRATION

    connection.row_factory = sqlite3.Row
    integrity_rows = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
    if integrity_rows != ["ok"]:
        raise StateIntegrityError("state database integrity check failed")
    schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if schema_version > STATE_SCHEMA_VERSION:
        raise StateIntegrityError(
            "state database requires newer code",
            details={"schema_version": schema_version, "supported": STATE_SCHEMA_VERSION},
        )
    if require_current_schema and schema_version != STATE_SCHEMA_VERSION:
        raise StateIntegrityError(
            "state database migration is incomplete",
            details={"schema_version": schema_version, "expected": STATE_SCHEMA_VERSION},
        )
    if require_current_schema:
        marker = connection.execute(
            "SELECT 1 FROM state_schema_migrations WHERE version=?",
            (STORAGE_MAINTENANCE_MIGRATION,),
        ).fetchone()
        if marker is None:
            raise StateIntegrityError("state database migration marker is missing")
    foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
    if foreign_keys:
        raise StateIntegrityError(
            "state database foreign key validation failed",
            details={"violations": len(foreign_keys)},
        )

    findings: list[str] = []
    if _table_exists(connection, "runs"):
        findings.extend(
            f"run_scope:{row['id']}"
            for row in connection.execute(
                """
                SELECT r.id FROM runs r
                LEFT JOIN sessions s ON s.id=r.session_id
                WHERE s.id IS NULL OR (
                    r.actor_id IS NOT NULL AND r.actor_id!=s.actor_id
                ) OR (
                    r.tenant_id IS NOT NULL AND r.tenant_id!=s.tenant_id
                )
                """
            )
        )
    if _table_exists(connection, "run_journals"):
        findings.extend(
            f"journal_scope:{row['run_id']}"
            for row in connection.execute(
                """
                SELECT j.run_id FROM run_journals j
                LEFT JOIN runs r ON r.id=j.run_id
                WHERE r.id IS NULL OR j.session_id!=r.session_id
                    OR j.actor_id!=r.actor_id OR j.tenant_id!=r.tenant_id
                """
            )
        )
    if _table_exists(connection, "tool_operation_refs"):
        findings.extend(
            f"operation_scope:{row['operation_id']}"
            for row in connection.execute(
                """
                SELECT o.operation_id FROM tool_operation_refs o
                LEFT JOIN runs r ON r.id=o.run_id
                WHERE r.id IS NULL OR o.session_id!=r.session_id
                    OR o.actor_id!=r.actor_id OR o.tenant_id!=r.tenant_id
                """
            )
        )
    if _table_exists(connection, "context_checkpoints"):
        findings.extend(
            f"checkpoint_scope:{row['id']}"
            for row in connection.execute(
                """
                SELECT c.id FROM context_checkpoints c
                LEFT JOIN sessions s ON s.id=c.session_id
                LEFT JOIN runs r ON r.id=c.created_run_id
                WHERE s.id IS NULL
                    OR (c.actor_id IS NOT NULL AND c.actor_id!=s.actor_id)
                    OR (c.tenant_id IS NOT NULL AND c.tenant_id!=s.tenant_id)
                    OR (c.created_run_id IS NOT NULL AND (
                        r.id IS NULL OR r.session_id!=c.session_id
                    ))
                    OR (c.parent_checkpoint_id IS NOT NULL AND NOT EXISTS (
                        SELECT 1 FROM context_checkpoints p
                        WHERE p.id=c.parent_checkpoint_id AND p.session_id=c.session_id
                    ))
                """
            )
        )

    artifact_references = _artifact_references(connection)
    operation_references = _operation_references(connection)
    artifact_rows = {
        str(row["id"]): dict(row)
        for row in connection.execute("SELECT * FROM artifacts")
    } if _table_exists(connection, "artifacts") else {}
    operation_rows = {
        str(row["operation_id"]): dict(row)
        for row in connection.execute("SELECT * FROM tool_operation_refs")
    } if _table_exists(connection, "tool_operation_refs") else {}
    findings.extend(
        f"artifact_missing:{artifact_id}"
        for artifact_id in artifact_references
        if artifact_id not in artifact_rows
    )
    findings.extend(
        f"operation_missing:{operation_id}"
        for operation_id in operation_references
        if operation_id not in operation_rows
    )
    for operation_id, references in operation_references.items():
        operation = operation_rows.get(operation_id)
        if operation is None:
            continue
        for source, reference_session in references:
            if (
                reference_session is not None
                and reference_session != operation["session_id"]
            ):
                findings.append(f"operation_reference_scope:{operation_id}:{source}")

    resolved_root = artifact_root.expanduser().resolve() if artifact_root is not None else None
    for artifact_id, record in artifact_rows.items():
        if record.get("gc_pending_at"):
            if artifact_references.get(artifact_id):
                findings.append(f"artifact_pending_referenced:{artifact_id}")
            continue
        session = connection.execute(
            "SELECT actor_id, tenant_id FROM sessions WHERE id=?",
            (record["session_id"],),
        ).fetchone()
        run_exists = connection.execute(
            "SELECT 1 FROM runs WHERE id=? AND session_id=?",
            (record["run_id"], record["session_id"]),
        ).fetchone()
        delegation_exists = (
            connection.execute(
                "SELECT 1 FROM delegation_runs WHERE id=? AND session_id=?",
                (record["run_id"], record["session_id"]),
            ).fetchone()
            if _table_exists(connection, "delegation_runs")
            else None
        )
        if (
            session is None
            or (run_exists is None and delegation_exists is None)
            or record["actor_id"] != session["actor_id"]
            or record["tenant_id"] != session["tenant_id"]
        ):
            findings.append(f"artifact_scope:{artifact_id}")
            continue
        for _, reference_session in artifact_references.get(artifact_id, []):
            if reference_session is not None and reference_session != record["session_id"]:
                findings.append(f"artifact_reference_scope:{artifact_id}")
        path = artifact_paths.get(artifact_id) if artifact_paths is not None else None
        if path is None and resolved_root is not None:
            path = Path(record["path"]).expanduser()
        if path is not None:
            if path.is_symlink():
                findings.append(f"artifact_file_symlink:{artifact_id}")
                continue
            try:
                resolved_path = path.resolve(strict=True)
            except (FileNotFoundError, OSError):
                findings.append(f"artifact_file_missing:{artifact_id}")
                continue
            if resolved_root is not None and not _is_relative_to(resolved_path, resolved_root):
                findings.append(f"artifact_path_scope:{artifact_id}")
                continue
            try:
                digest, size = _sha256_file(resolved_path)
            except OSError:
                findings.append(f"artifact_file_unreadable:{artifact_id}")
                continue
            if digest != record["sha256"] or size != int(record["size_bytes"]):
                findings.append(f"artifact_hash:{artifact_id}")

    if findings:
        raise StateIntegrityError(
            "state reference validation failed",
            details={"findings": findings[:100], "finding_count": len(findings)},
        )
    return IntegrityReport(
        schema_version=schema_version,
        integrity="ok",
        foreign_key_violations=0,
        sessions=_record_count(connection, "sessions"),
        runs=_record_count(connection, "runs"),
        journals=_record_count(connection, "run_journals"),
        operations=_record_count(connection, "tool_operation_refs"),
        artifacts=len([row for row in artifact_rows.values() if not row.get("gc_pending_at")]),
        checkpoints=_record_count(connection, "context_checkpoints"),
        artifact_references=sum(len(items) for items in artifact_references.values()),
        operation_references=len(operation_references),
    )


def _read_manifest(bundle: Path) -> dict[str, Any]:
    manifest_path = bundle / MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise BackupValidationError("backup manifest is missing or unsafe")
    try:
        raw = manifest_path.read_bytes()
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("manifest too large")
        manifest = json.loads(raw)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise BackupValidationError("backup manifest is invalid") from error
    if not isinstance(manifest, dict) or manifest.get("format") != BACKUP_FORMAT:
        raise BackupValidationError("backup format is unsupported")
    required_keys = {
        "format",
        "created_at",
        "schema_version",
        "migration_ids",
        "environment",
        "integrity",
        "files",
        "manifest_sha256",
    }
    if set(manifest) != required_keys:
        raise BackupValidationError("backup manifest fields are invalid")
    created_at = manifest.get("created_at")
    try:
        parsed_created_at = datetime.fromisoformat(created_at)
    except (TypeError, ValueError) as error:
        raise BackupValidationError("backup creation time is invalid") from error
    if parsed_created_at.tzinfo is None:
        raise BackupValidationError("backup creation time must include a timezone")
    migration_ids = manifest.get("migration_ids")
    if (
        not isinstance(migration_ids, list)
        or any(not isinstance(value, str) or not value for value in migration_ids)
        or migration_ids != sorted(set(migration_ids))
    ):
        raise BackupValidationError("backup migration inventory is invalid")
    environment = manifest.get("environment")
    expected_environment = {
        "python",
        "sqlite",
        "platform",
        "machine",
        "journal_mode",
        "page_size",
    }
    if not isinstance(environment, dict) or set(environment) != expected_environment:
        raise BackupValidationError("backup environment metadata is invalid")
    if any(
        not isinstance(environment[key], str) or not environment[key]
        for key in expected_environment - {"page_size"}
    ) or (
        isinstance(environment["page_size"], bool)
        or not isinstance(environment["page_size"], int)
        or environment["page_size"] <= 0
    ):
        raise BackupValidationError("backup environment metadata is invalid")
    if not isinstance(manifest.get("integrity"), dict):
        raise BackupValidationError("backup integrity metadata is invalid")
    expected = manifest.get("manifest_sha256")
    if not isinstance(expected, str) or expected != _manifest_hash(manifest):
        raise BackupValidationError("backup manifest checksum does not match")
    return manifest


def verify_backup_bundle(bundle_path: str | Path) -> tuple[dict[str, Any], IntegrityReport]:
    from .store import STATE_SCHEMA_VERSION

    requested_bundle = Path(bundle_path).expanduser()
    if requested_bundle.is_symlink():
        raise BackupValidationError("backup bundle directory is missing or unsafe")
    bundle = requested_bundle.resolve()
    if not bundle.is_dir():
        raise BackupValidationError("backup bundle directory is missing or unsafe")
    manifest = _read_manifest(bundle)
    schema_version = manifest.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise BackupValidationError("backup schema version is invalid")
    if schema_version < 0:
        raise BackupValidationError("backup schema version is invalid")
    if schema_version > STATE_SCHEMA_VERSION:
        raise RestoreRefusedError(
            "backup requires newer code; schema downgrade is refused",
            details={"schema_version": schema_version, "supported": STATE_SCHEMA_VERSION},
        )
    for migration_id in manifest["migration_ids"]:
        prefix = migration_id.split("_", 1)[0]
        if prefix.isdigit() and int(prefix) > STATE_SCHEMA_VERSION:
            raise RestoreRefusedError(
                "backup requires newer code; schema downgrade is refused",
                details={"migration_id": migration_id, "supported": STATE_SCHEMA_VERSION},
            )
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise BackupValidationError("backup file inventory is invalid")
    seen_files: set[str] = set()
    artifact_paths: dict[str, Path] = {}
    database_path: Path | None = None
    for item in files:
        if not isinstance(item, dict):
            raise BackupValidationError("backup file inventory is invalid")
        relative_text = item.get("file")
        expected_hash = item.get("sha256")
        expected_size = item.get("size_bytes")
        if (
            not isinstance(relative_text, str)
            or not isinstance(expected_hash, str)
            or len(expected_hash) != 64
            or any(character not in "0123456789abcdef" for character in expected_hash)
            or isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size < 0
            or relative_text in seen_files
        ):
            raise BackupValidationError("backup file inventory is invalid")
        relative = Path(relative_text)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative_text != relative.as_posix()
            or relative == Path(".")
        ):
            raise BackupValidationError("backup file path escapes the bundle")
        path = bundle / relative
        if path.is_symlink() or not path.is_file() or not _is_relative_to(path.resolve(), bundle):
            raise BackupValidationError("backup file is missing or unsafe")
        try:
            digest, size = _sha256_file(path)
        except OSError as error:
            raise BackupValidationError("backup file cannot be read") from error
        if digest != expected_hash or size != expected_size:
            raise BackupValidationError("backup file checksum does not match")
        seen_files.add(relative_text)
        if item.get("role") == "artifact":
            if set(item) != {"role", "artifact_id", "file", "sha256", "size_bytes"}:
                raise BackupValidationError("backup artifact inventory is invalid")
            artifact_id = item.get("artifact_id")
            if not isinstance(artifact_id, str) or not artifact_id or artifact_id in artifact_paths:
                raise BackupValidationError("backup artifact inventory is invalid")
            if len(relative.parts) != 2 or relative.parts[0] != "artifacts":
                raise BackupValidationError("backup artifact path is invalid")
            artifact_paths[artifact_id] = path
        elif item.get("role") == "state_database":
            if set(item) != {"role", "file", "sha256", "size_bytes"}:
                raise BackupValidationError("backup database inventory is invalid")
            if relative_text != BACKUP_DATABASE_NAME:
                raise BackupValidationError("backup database path is invalid")
            if database_path is not None:
                raise BackupValidationError(
                    "backup must contain exactly one state database"
                )
            database_path = path
        else:
            raise BackupValidationError("backup file role is unsupported")
    if database_path is None:
        raise BackupValidationError("backup must contain exactly one state database")
    uri = f"file:{database_path.resolve()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            connection.row_factory = sqlite3.Row
            database_schema = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if database_schema != schema_version:
                raise BackupValidationError("backup manifest schema does not match database")
            report = validate_state_integrity(
                connection,
                artifact_paths=artifact_paths,
                require_current_schema=schema_version == STATE_SCHEMA_VERSION,
            )
            migration_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT version FROM state_schema_migrations ORDER BY version"
                )
            ]
            if migration_ids != manifest["migration_ids"]:
                raise BackupValidationError(
                    "backup migration inventory does not match database"
                )
            if report.to_dict() != manifest["integrity"]:
                raise BackupValidationError(
                    "backup integrity metadata does not match database"
                )
            pending_clause = (
                " WHERE gc_pending_at IS NULL"
                if _column_exists(connection, "artifacts", "gc_pending_at")
                else ""
            )
            active_artifacts = {
                str(row[0])
                for row in connection.execute(
                    f"SELECT id FROM artifacts{pending_clause}"
                )
            } if _table_exists(connection, "artifacts") else set()
            if active_artifacts != set(artifact_paths):
                raise BackupValidationError("backup artifact inventory does not match database")
    except StateIntegrityError as error:
        raise BackupValidationError("backup database integrity validation failed") from error
    except sqlite3.Error as error:
        raise BackupValidationError("backup database cannot be opened") from error
    return manifest, report


class StateMaintenance:
    def __init__(self, state_store, artifact_root: str | Path | None = None):
        self.state_store = state_store
        default_root = state_store.path.parent / "artifacts"
        self.artifact_root = Path(artifact_root or default_root).expanduser().resolve()
        if self.artifact_root == Path(self.artifact_root.anchor):
            raise ValueError("artifact root must not be a filesystem root")

    def verify(self) -> IntegrityReport:
        with self.state_store.connect() as connection:
            return validate_state_integrity(
                connection,
                artifact_root=self.artifact_root,
            )

    def _resume_pending_artifact_gc(self, *, limit: int = 1000) -> int:
        if limit <= 0 or limit > 1000:
            raise ValueError("pending artifact GC limit must be between 1 and 1000")
        with self.state_store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM artifacts WHERE gc_pending_at IS NOT NULL
                ORDER BY gc_pending_at, id LIMIT ?
                """,
                (limit,),
            ).fetchall()
            row_ids = [str(row["id"]) for row in rows]
            held_ids: set[str] = set()
            if row_ids:
                placeholders = ",".join("?" for _ in row_ids)
                held_ids = {
                    str(row[0])
                    for row in connection.execute(
                        f"""
                        SELECT resource_id FROM retention_holds
                        WHERE resource_type='artifact'
                            AND resource_id IN ({placeholders})
                            AND (expires_at IS NULL OR expires_at>?)
                        """,
                        (*row_ids, self.state_store.now_iso()),
                    )
                }
            rows = [row for row in rows if str(row["id"]) not in held_ids]
            references = _artifact_references(connection)
            if any(references.get(str(row["id"])) for row in rows):
                raise RetentionError("pending artifact became referenced")
            completed: list[str] = []
            for row in rows:
                path = Path(row["path"]).expanduser()
                if path.is_symlink():
                    raise RetentionError("pending artifact path is a symbolic link")
                try:
                    resolved = path.resolve(strict=True)
                except FileNotFoundError:
                    completed.append(str(row["id"]))
                    continue
                except OSError as error:
                    raise RetentionError("pending artifact path cannot be resolved") from error
                if not _is_relative_to(resolved, self.artifact_root):
                    raise RetentionError("pending artifact path escapes the managed root")
                try:
                    digest, size = _sha256_file(resolved)
                except OSError as error:
                    raise RetentionError("pending artifact file cannot be read") from error
                if digest != row["sha256"] or size != int(row["size_bytes"]):
                    raise RetentionError("pending artifact checksum does not match")
                try:
                    resolved.unlink()
                except OSError as error:
                    raise RetentionError("pending artifact file cannot be removed") from error
                completed.append(str(row["id"]))
            if completed:
                placeholders = ",".join("?" for _ in completed)
                connection.execute(
                    f"DELETE FROM artifacts WHERE gc_pending_at IS NOT NULL "
                    f"AND id IN ({placeholders})",
                    completed,
                )
        return len(completed)

    def backup(self, target_path: str | Path) -> BackupResult:
        from .store import normalize_state_storage_error

        requested_target = Path(target_path).expanduser()
        if _lexists(requested_target):
            raise BackupRefusedError("backup target already exists; overwrite is refused")
        target = requested_target.resolve()
        parent = target.parent
        if not parent.is_dir() or parent.is_symlink():
            raise BackupRefusedError("backup target parent must be an existing directory")
        if _lexists(target):
            raise BackupRefusedError("backup target already exists; overwrite is refused")
        if _is_relative_to(target, self.artifact_root):
            raise BackupRefusedError("backup target cannot be inside the artifact root")
        if target == self.state_store.path.expanduser().resolve():
            raise BackupRefusedError("backup target cannot be the live state database")
        stage = _owned_stage(parent, target.name)
        try:
            database_path = stage / BACKUP_DATABASE_NAME
            source = self.state_store.connect()
            destination = sqlite3.connect(database_path)
            try:
                source.backup(destination, pages=256, sleep=0.01)
                destination.execute("PRAGMA journal_mode=DELETE")
                destination.commit()
            finally:
                destination.close()
                source.close()
            database_path.chmod(0o600)
            with sqlite3.connect(database_path) as snapshot:
                snapshot.row_factory = sqlite3.Row
                report = validate_state_integrity(
                    snapshot,
                    artifact_root=self.artifact_root,
                )
                migration_ids = [
                    str(row[0])
                    for row in snapshot.execute(
                        "SELECT version FROM state_schema_migrations ORDER BY version"
                    )
                ]
                artifact_rows = snapshot.execute(
                    """
                    SELECT id, path, sha256, size_bytes FROM artifacts
                    WHERE gc_pending_at IS NULL ORDER BY id
                    """
                ).fetchall()
                journal_mode = str(snapshot.execute("PRAGMA journal_mode").fetchone()[0])
                page_size = int(snapshot.execute("PRAGMA page_size").fetchone()[0])
            files: list[dict[str, Any]] = []
            database_hash, database_size = _sha256_file(database_path)
            files.append(
                {
                    "role": "state_database",
                    "file": BACKUP_DATABASE_NAME,
                    "sha256": database_hash,
                    "size_bytes": database_size,
                }
            )
            artifact_directory = stage / "artifacts"
            if artifact_rows:
                artifact_directory.mkdir(mode=0o700)
            for index, row in enumerate(artifact_rows):
                indexed_path = Path(row["path"]).expanduser()
                if indexed_path.is_symlink():
                    raise StateIntegrityError("artifact path is a symbolic link")
                source_path = indexed_path.resolve(strict=True)
                if not _is_relative_to(source_path, self.artifact_root):
                    raise StateIntegrityError("artifact path escapes the managed root")
                relative = Path("artifacts") / f"{index:08d}.blob"
                destination_path = stage / relative
                shutil.copyfile(source_path, destination_path)
                destination_path.chmod(0o600)
                digest, size = _sha256_file(destination_path)
                if digest != row["sha256"] or size != int(row["size_bytes"]):
                    raise StateIntegrityError("artifact changed during backup")
                files.append(
                    {
                        "role": "artifact",
                        "artifact_id": str(row["id"]),
                        "file": relative.as_posix(),
                        "sha256": digest,
                        "size_bytes": size,
                    }
                )
            created_at = datetime.now(UTC).isoformat()
            manifest: dict[str, Any] = {
                "format": BACKUP_FORMAT,
                "created_at": created_at,
                "schema_version": report.schema_version,
                "migration_ids": migration_ids,
                "environment": {
                    "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                    "sqlite": sqlite3.sqlite_version,
                    "platform": platform.system().lower(),
                    "machine": platform.machine().lower(),
                    "journal_mode": journal_mode.lower(),
                    "page_size": page_size,
                },
                "integrity": report.to_dict(),
                "files": files,
            }
            manifest["manifest_sha256"] = _manifest_hash(manifest)
            manifest_path = stage / MANIFEST_NAME
            manifest_path.write_bytes(json.dumps(manifest, indent=2, sort_keys=True).encode("ascii"))
            manifest_path.chmod(0o600)
            verified_manifest, _ = verify_backup_bundle(stage)
            stage.rename(target)
            return BackupResult(
                target=str(target),
                manifest_sha256=str(verified_manifest["manifest_sha256"]),
                schema_version=report.schema_version,
                database_sha256=database_hash,
                artifact_count=len(artifact_rows),
            )
        except sqlite3.Error as error:
            if stage.exists():
                _remove_owned_stage(stage, parent=parent, label=target.name)
            raise normalize_state_storage_error(error) from error
        except BaseException:
            if stage.exists():
                _remove_owned_stage(stage, parent=parent, label=target.name)
            raise

    @staticmethod
    def restore(bundle_path: str | Path, target_dir: str | Path) -> RestoreResult:
        from .store import StateStore

        requested_bundle = Path(bundle_path).expanduser()
        if requested_bundle.is_symlink():
            raise BackupValidationError("backup bundle directory is missing or unsafe")
        bundle = requested_bundle.resolve()
        requested_target = Path(target_dir).expanduser()
        if requested_target.is_symlink():
            raise RestoreRefusedError("restore target cannot be a symbolic link")
        target = requested_target.resolve()
        manifest, _ = verify_backup_bundle(bundle)
        if target == bundle or _is_relative_to(target, bundle):
            raise RestoreRefusedError("restore target cannot be inside the backup bundle")
        parent = target.parent
        if not parent.is_dir() or parent.is_symlink():
            raise RestoreRefusedError("restore target parent must be an existing directory")
        target_existed = _lexists(target)
        if target_existed:
            if target.is_symlink() or not target.is_dir():
                raise RestoreRefusedError("restore target must be a new path or empty directory")
            if any(target.iterdir()):
                raise RestoreRefusedError("restore target is not empty; overwrite is refused")
        stage = _owned_stage(parent, target.name)
        published = False
        try:
            source_database = bundle / BACKUP_DATABASE_NAME
            staged_database = stage / BACKUP_DATABASE_NAME
            shutil.copyfile(source_database, staged_database)
            database_entry = next(
                entry for entry in manifest["files"] if entry["role"] == "state_database"
            )
            staged_hash, staged_size = _sha256_file(staged_database)
            if (
                staged_hash != database_entry["sha256"]
                or staged_size != database_entry["size_bytes"]
            ):
                raise BackupValidationError(
                    "backup database changed while restore was running"
                )
            staged_database.chmod(0o600)
            staged_artifacts = stage / "artifacts"
            staged_artifacts.mkdir(mode=0o700)
            final_artifacts = target / "artifacts"
            artifact_paths: dict[str, Path] = {}
            final_paths: dict[str, str] = {}
            for entry in manifest["files"]:
                if entry.get("role") != "artifact":
                    continue
                artifact_id = str(entry["artifact_id"])
                relative = Path(entry["file"])
                destination_path = stage / relative
                shutil.copyfile(bundle / entry["file"], destination_path)
                destination_path.chmod(0o600)
                artifact_paths[artifact_id] = destination_path
                final_paths[artifact_id] = str(target / relative)
            restored = StateStore(staged_database)
            with restored.connect() as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                for artifact_id, final_path in final_paths.items():
                    connection.execute(
                        "UPDATE artifacts SET path=? WHERE id=? AND gc_pending_at IS NULL",
                        (final_path, artifact_id),
                    )
                pending_artifacts = connection.execute(
                    "SELECT id FROM artifacts WHERE gc_pending_at IS NOT NULL ORDER BY id"
                ).fetchall()
                for index, row in enumerate(pending_artifacts):
                    connection.execute(
                        "UPDATE artifacts SET path=? WHERE id=? AND gc_pending_at IS NOT NULL",
                        (
                            str(final_artifacts / ".gc-pending" / f"{index:08d}.blob"),
                            row["id"],
                        ),
                    )
            restored.record_audit_event(
                actor_id="system",
                tenant_id="system",
                action="state.restore",
                resource="state_db",
                decision="verified",
                details={
                    "backup_manifest_sha256": manifest["manifest_sha256"],
                    "source_schema_version": manifest["schema_version"],
                },
            )
            with restored.connect() as connection:
                validate_state_integrity(
                    connection,
                    artifact_paths=artifact_paths,
                )
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            shutil.copyfile(bundle / MANIFEST_NAME, stage / "restore-manifest.json")
            (stage / "restore-manifest.json").chmod(0o600)
            if target_existed:
                if any(target.iterdir()):
                    raise RestoreRefusedError("restore target changed while restore was running")
                target.rmdir()
            stage.rename(target)
            published = True
            final_store = StateStore(target / BACKUP_DATABASE_NAME, read_only=True)
            final_report = StateMaintenance(final_store, target / "artifacts").verify()
            return RestoreResult(
                target_dir=str(target),
                state_path=str(target / BACKUP_DATABASE_NAME),
                artifact_root=str(target / "artifacts"),
                source_schema_version=int(manifest["schema_version"]),
                restored_schema_version=final_report.schema_version,
                manifest_sha256=str(manifest["manifest_sha256"]),
                integrity=final_report.to_dict(),
            )
        except BaseException:
            if published and target.exists() and not stage.exists():
                target.rename(stage)
            if stage.exists():
                _remove_owned_stage(stage, parent=parent, label=target.name)
            if target_existed and not target.exists():
                target.mkdir(mode=0o700)
            raise

    def add_retention_hold(
        self,
        *,
        resource_type: str,
        resource_id: str,
        reason: str,
        created_by: str,
        expires_at: datetime | None = None,
    ) -> str:
        if resource_type not in {"session", "run", "artifact"}:
            raise ValueError("retention hold resource_type is invalid")
        if not all(isinstance(value, str) and value.strip() for value in (
            resource_id,
            reason,
            created_by,
        )):
            raise ValueError("retention hold identity, reason, and creator are required")
        if expires_at is not None:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            expires_at = expires_at.astimezone(UTC)
            if expires_at <= self.state_store.now():
                raise ValueError("retention hold expiry must be in the future")
        hold_id = uuid.uuid4().hex
        now = self.state_store.now_iso()
        with self.state_store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if resource_type == "session":
                resource_exists = connection.execute(
                    "SELECT 1 FROM sessions WHERE id=?",
                    (resource_id,),
                ).fetchone()
            elif resource_type == "run":
                resource_exists = connection.execute(
                    "SELECT 1 FROM runs WHERE id=?",
                    (resource_id,),
                ).fetchone()
                if resource_exists is None and _table_exists(connection, "delegation_runs"):
                    resource_exists = connection.execute(
                        "SELECT 1 FROM delegation_runs WHERE id=?",
                        (resource_id,),
                    ).fetchone()
            else:
                resource_exists = connection.execute(
                    "SELECT 1 FROM artifacts WHERE id=? AND gc_pending_at IS NULL",
                    (resource_id,),
                ).fetchone()
            if resource_exists is None:
                raise RetentionError(
                    "retention hold resource is missing or already pending GC"
                )
            connection.execute(
                """
                INSERT INTO retention_holds(
                    id, resource_type, resource_id, reason, created_by,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    hold_id,
                    resource_type,
                    resource_id,
                    reason,
                    created_by,
                    now,
                    expires_at.isoformat() if expires_at is not None else None,
                ),
            )
            connection.execute(
                """
                INSERT INTO audit_events(
                    actor_id, tenant_id, action, resource,
                    decision, details_json, created_at
                ) VALUES ('system', 'system', 'retention.hold', ?, 'created', ?, ?)
                """,
                (
                    f"{resource_type}:{resource_id}",
                    json.dumps(
                        {"hold_id": hold_id, "reason": reason, "created_by": created_by},
                        sort_keys=True,
                    ),
                    now,
                ),
            )
        return hold_id

    def release_retention_hold(self, hold_id: str, *, released_by: str) -> bool:
        if not hold_id or not released_by:
            raise ValueError("hold_id and released_by are required")
        now = self.state_store.now_iso()
        with self.state_store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT resource_type, resource_id FROM retention_holds WHERE id=?",
                (hold_id,),
            ).fetchone()
            if row is None:
                return False
            connection.execute("DELETE FROM retention_holds WHERE id=?", (hold_id,))
            connection.execute(
                """
                INSERT INTO audit_events(
                    actor_id, tenant_id, action, resource,
                    decision, details_json, created_at
                ) VALUES ('system', 'system', 'retention.hold', ?, 'released', ?, ?)
                """,
                (
                    f"{row['resource_type']}:{row['resource_id']}",
                    json.dumps(
                        {"hold_id": hold_id, "released_by": released_by},
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            return True

    @staticmethod
    def _has_active_hold(
        connection: sqlite3.Connection,
        resource_type: str,
        resource_ids: list[str],
        now: str,
    ) -> bool:
        if not resource_ids:
            return False
        placeholders = ",".join("?" for _ in resource_ids)
        return connection.execute(
            f"""
            SELECT 1 FROM retention_holds
            WHERE resource_type=? AND resource_id IN ({placeholders})
                AND (expires_at IS NULL OR expires_at>?)
            LIMIT 1
            """,
            (resource_type, *resource_ids, now),
        ).fetchone() is not None

    @staticmethod
    def _operation_blockers(
        state_connection: sqlite3.Connection,
        operation_connection: sqlite3.Connection | None,
        run_ids: list[str],
        cutoff: str,
    ) -> list[str]:
        if not run_ids:
            return []
        placeholders = ",".join("?" for _ in run_ids)
        references = state_connection.execute(
            f"""
            SELECT operation_id, status, updated_at FROM tool_operation_refs
            WHERE run_id IN ({placeholders}) ORDER BY operation_id
            """,
            run_ids,
        ).fetchall()
        if not references:
            return []
        statuses = {str(row["status"]) for row in references}
        blockers: list[str] = []
        if "manual_review" in statuses:
            blockers.append("manual_review_operation")
        if statuses - _TERMINAL_OPERATION_STATUSES:
            blockers.append("pending_operation")
        if any(
            row["updated_at"] is None or str(row["updated_at"]) > cutoff
            for row in references
        ):
            blockers.append("operation_retained")
        if operation_connection is None:
            blockers.append("operation_outbox_unverified")
            return blockers
        if not _table_exists(operation_connection, "tool_operations") or not _table_exists(
            operation_connection, "tool_outbox"
        ):
            blockers.append("operation_schema_missing")
            return blockers
        for reference in references:
            operation = operation_connection.execute(
                "SELECT status, updated_at FROM tool_operations WHERE id=?",
                (reference["operation_id"],),
            ).fetchone()
            if operation is None:
                blockers.append("operation_record_missing")
                continue
            actual_status = str(operation["status"])
            if actual_status == "manual_review":
                blockers.append("manual_review_operation")
            if actual_status not in _TERMINAL_OPERATION_STATUSES:
                blockers.append("pending_operation")
            if actual_status != str(reference["status"]):
                blockers.append("operation_state_mismatch")
            if operation["updated_at"] is None or str(operation["updated_at"]) > cutoff:
                blockers.append("operation_retained")
            outbox_rows = operation_connection.execute(
                """
                SELECT status, published_at FROM tool_outbox
                WHERE operation_id=?
                """,
                (reference["operation_id"],),
            ).fetchall()
            if any(row["status"] != "published" for row in outbox_rows):
                blockers.append("pending_outbox")
            if any(
                row["status"] == "published"
                and (
                    row["published_at"] is None
                    or str(row["published_at"]) > cutoff
                )
                for row in outbox_rows
            ):
                blockers.append("operation_retained")
        return sorted(set(blockers))

    def _session_blockers(
        self,
        connection: sqlite3.Connection,
        operation_connection: sqlite3.Connection | None,
        *,
        session_id: str,
        cutoff: str,
        artifact_cutoff: str,
        now: str,
        artifact_references: dict[str, list[tuple[str, str | None]]],
    ) -> list[str]:
        blockers: list[str] = []
        runs = connection.execute(
            "SELECT id, status, finished_at, recovery_recommendation FROM runs "
            "WHERE session_id=? ORDER BY id",
            (session_id,),
        ).fetchall()
        run_ids = [str(row["id"]) for row in runs]
        if not run_ids:
            retained_payload = connection.execute(
                """
                SELECT 1 FROM messages WHERE session_id=?
                UNION ALL
                SELECT 1 FROM context_checkpoints WHERE session_id=?
                UNION ALL
                SELECT 1 FROM artifacts WHERE session_id=?
                LIMIT 1
                """,
                (session_id, session_id, session_id),
            ).fetchone()
            if retained_payload is not None:
                blockers.append("run_truth_missing")
        if any(
            row["status"] not in _TERMINAL_RUN_STATUSES
            or row["finished_at"] is None
            or str(row["finished_at"]) > cutoff
            for row in runs
        ):
            blockers.append("run_not_terminal_or_retained")
        if any(row["recovery_recommendation"] for row in runs):
            blockers.append("recovery_required")
        delegated_rows: list[sqlite3.Row] = []
        if _table_exists(connection, "delegation_runs"):
            root_clause = ""
            parameters: list[str] = [session_id]
            if run_ids:
                root_marks = ",".join("?" for _ in run_ids)
                root_clause = f" OR root_run_id IN ({root_marks})"
                parameters.extend(run_ids)
            delegated_rows = connection.execute(
                f"""
                SELECT id, parent_run_id, root_run_id, session_id, status,
                       worker_owner, worker_lease_expires_at, finished_at
                FROM delegation_runs
                WHERE session_id=?{root_clause}
                ORDER BY id
                """,
                parameters,
            ).fetchall()
            delegated_ids = {str(row["id"]) for row in delegated_rows}
            allowed_parents = set(run_ids) | delegated_ids
            if any(
                row["session_id"] != session_id
                or str(row["root_run_id"]) not in run_ids
                or str(row["parent_run_id"]) not in allowed_parents
                for row in delegated_rows
            ):
                blockers.append("delegation_scope_mismatch")
            if any(
                row["status"] not in _TERMINAL_DELEGATION_STATUSES
                or row["finished_at"] is None
                or str(row["finished_at"]) > cutoff
                or row["worker_owner"] is not None
                or row["worker_lease_expires_at"] is not None
                for row in delegated_rows
            ):
                blockers.append("delegation_not_terminal_or_retained")
        delegated_run_ids = [str(row["id"]) for row in delegated_rows]
        cohort_run_ids = [*run_ids, *delegated_run_ids]
        if run_ids and _table_exists(connection, "delegation_roots"):
            root_marks = ",".join("?" for _ in run_ids)
            delegation_roots = connection.execute(
                f"""
                SELECT root_run_id, session_id, reserved_json, updated_at
                FROM delegation_roots
                WHERE root_run_id IN ({root_marks}) OR session_id=?
                """,
                (*run_ids, session_id),
            ).fetchall()
            if any(
                row["session_id"] != session_id
                or str(row["root_run_id"]) not in run_ids
                for row in delegation_roots
            ):
                blockers.append("delegation_scope_mismatch")
            if any(
                not _reservation_is_clear(row["reserved_json"])
                or row["updated_at"] is None
                or str(row["updated_at"]) > cutoff
                for row in delegation_roots
            ):
                blockers.append("delegation_not_terminal_or_retained")
        if self._has_active_hold(
            connection, "session", [session_id], now
        ) or self._has_active_hold(connection, "run", cohort_run_ids, now):
            blockers.append("retention_hold")
        lease = connection.execute(
            """
            SELECT 1 FROM session_leases
            WHERE session_id=? AND (lease_owner IS NOT NULL OR active_run_id IS NOT NULL)
            """,
            (session_id,),
        ).fetchone()
        if lease is not None:
            blockers.append("active_session_lease")
        if run_ids:
            placeholders = ",".join("?" for _ in run_ids)
            finalizers = connection.execute(
                f"""
                SELECT run_id, status, cursor, terminal_at, cleanup_at, updated_at
                FROM turn_finalizers WHERE run_id IN ({placeholders})
                """,
                run_ids,
            ).fetchall()
            if len(finalizers) != len(run_ids):
                blockers.append("finalizer_truth_missing")
            if any(
                row["status"] != "terminal"
                or int(row["cursor"]) < 7
                or row["terminal_at"] is None
                or row["cleanup_at"] is None
                or row["updated_at"] is None
                or str(row["updated_at"]) > cutoff
                for row in finalizers
            ):
                blockers.append("pending_finalizer")
            journals = connection.execute(
                f"""
                SELECT run_id, phase, updated_at FROM run_journals
                WHERE run_id IN ({placeholders})
                """,
                run_ids,
            ).fetchall()
            if len(journals) != len(run_ids):
                blockers.append("journal_truth_missing")
            if any(
                row["phase"] not in _TERMINAL_JOURNAL_PHASES
                or row["updated_at"] is None
                or str(row["updated_at"]) > cutoff
                for row in journals
            ):
                blockers.append("recoverable_journal")
            cohort_marks = ",".join("?" for _ in cohort_run_ids)
            active_request = connection.execute(
                f"""
                SELECT 1 FROM api_requests
                WHERE run_id IN ({cohort_marks}) AND (
                    status NOT IN ('completed', 'failed')
                    OR retained_until IS NULL OR retained_until>?
                ) LIMIT 1
                """,
                (*cohort_run_ids, now),
            ).fetchone()
            if active_request is not None:
                blockers.append("api_replay_retained")
            if _table_exists(connection, "run_budget_ledgers"):
                ledgers = connection.execute(
                    f"""
                    SELECT root_run_id, reserved_json, finalized_at
                    FROM run_budget_ledgers
                    WHERE root_run_id IN ({placeholders})
                    """,
                    run_ids,
                ).fetchall()
                if len(ledgers) != len(run_ids):
                    blockers.append("budget_truth_missing")
                reserved_operation = connection.execute(
                    f"""
                    SELECT 1 FROM run_budget_operations
                    WHERE root_run_id IN ({placeholders}) AND status='reserved'
                    LIMIT 1
                    """,
                    run_ids,
                ).fetchone()
                if reserved_operation is not None or any(
                    row["finalized_at"] is None
                    or str(row["finalized_at"]) > cutoff
                    or not _reservation_is_clear(row["reserved_json"])
                    for row in ledgers
                ):
                    blockers.append("budget_not_finalized")
            else:
                blockers.append("budget_truth_missing")
        blockers.extend(
            self._operation_blockers(
                connection,
                operation_connection,
                cohort_run_ids,
                cutoff,
            )
        )
        active_checkpoint = connection.execute(
            """
            SELECT 1 FROM context_checkpoints c
            JOIN run_journals j ON j.context_checkpoint_id=c.id
            WHERE c.session_id=?
                AND j.phase NOT IN ('terminal', 'cancelled', 'failed')
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        if active_checkpoint is not None:
            blockers.append("active_checkpoint")
        memory_reference = connection.execute(
            "SELECT 1 FROM memories WHERE source_session_id=? LIMIT 1",
            (session_id,),
        ).fetchone()
        if memory_reference is not None:
            blockers.append("memory_reference")
        artifacts = connection.execute(
            """
            SELECT id, created_at FROM artifacts
            WHERE session_id=? AND gc_pending_at IS NULL ORDER BY id
            """,
            (session_id,),
        ).fetchall()
        artifact_ids = [str(row["id"]) for row in artifacts]
        if any(str(row["created_at"]) > artifact_cutoff for row in artifacts):
            blockers.append("artifact_retained")
        if self._has_active_hold(connection, "artifact", artifact_ids, now):
            blockers.append("artifact_retention_hold")
        for artifact_id in artifact_ids:
            if any(
                reference_session is not None and reference_session != session_id
                for _, reference_session in artifact_references.get(artifact_id, [])
            ):
                blockers.append("artifact_cross_scope_reference")
        return sorted(set(blockers))

    def _verify_gc_artifact(self, row: sqlite3.Row) -> None:
        path = Path(row["path"]).expanduser()
        if path.is_symlink():
            raise RetentionError("artifact selected for GC is a symbolic link")
        try:
            resolved = path.resolve(strict=True)
        except (FileNotFoundError, OSError) as error:
            raise RetentionError("artifact selected for GC is missing") from error
        if not _is_relative_to(resolved, self.artifact_root):
            raise RetentionError("artifact selected for GC escapes the managed root")
        try:
            digest, size = _sha256_file(resolved)
        except OSError as error:
            raise RetentionError("artifact selected for GC cannot be read") from error
        if digest != row["sha256"] or size != int(row["size_bytes"]):
            raise RetentionError("artifact selected for GC failed checksum validation")

    @staticmethod
    def _delete_session_roots(
        connection: sqlite3.Connection,
        session_ids: list[str],
        run_ids: list[str],
        delegated_run_ids: list[str],
    ) -> None:
        if not session_ids:
            return
        session_marks = ",".join("?" for _ in session_ids)
        cohort_run_ids = [*run_ids, *delegated_run_ids]
        if cohort_run_ids:
            cohort_marks = ",".join("?" for _ in cohort_run_ids)
            connection.execute(
                f"DELETE FROM trace_event_index WHERE run_id IN ({cohort_marks}) "
                f"OR root_run_id IN ({cohort_marks}) OR session_id IN ({session_marks})",
                (*cohort_run_ids, *cohort_run_ids, *session_ids),
            )
            connection.execute(
                f"DELETE FROM api_requests WHERE run_id IN ({cohort_marks})",
                cohort_run_ids,
            )
            connection.execute(
                f"DELETE FROM agent_tool_calls WHERE run_id IN ({cohort_marks})",
                cohort_run_ids,
            )
            connection.execute(
                f"DELETE FROM agent_tool_envelopes WHERE run_id IN ({cohort_marks})",
                cohort_run_ids,
            )
        else:
            connection.execute(
                f"DELETE FROM trace_event_index WHERE session_id IN ({session_marks})",
                session_ids,
            )
        if run_ids:
            run_marks = ",".join("?" for _ in run_ids)
            connection.execute(
                f"DELETE FROM turn_finalizer_hooks WHERE run_id IN ({run_marks})",
                run_ids,
            )
            connection.execute(
                f"DELETE FROM turn_finalizers WHERE run_id IN ({run_marks})",
                run_ids,
            )
            connection.execute(
                f"DELETE FROM run_journals WHERE run_id IN ({run_marks})",
                run_ids,
            )
            connection.execute(
                f"DELETE FROM run_budget_ledgers WHERE root_run_id IN ({run_marks})",
                run_ids,
            )
            if _table_exists(connection, "delegation_roots"):
                connection.execute(
                    f"DELETE FROM delegation_roots WHERE root_run_id IN ({run_marks})",
                    run_ids,
                )
        if cohort_run_ids:
            cohort_marks = ",".join("?" for _ in cohort_run_ids)
            connection.execute(
                f"DELETE FROM tool_events WHERE run_id IN ({cohort_marks}) "
                f"OR session_id IN ({session_marks})",
                (*cohort_run_ids, *session_ids),
            )
            connection.execute(
                f"DELETE FROM provider_events WHERE run_id IN ({cohort_marks})",
                cohort_run_ids,
            )
            connection.execute(
                f"DELETE FROM plans WHERE run_id IN ({cohort_marks}) "
                f"OR session_id IN ({session_marks})",
                (*cohort_run_ids, *session_ids),
            )
            connection.execute(
                f"DELETE FROM tool_operation_refs WHERE run_id IN ({cohort_marks}) "
                f"OR session_id IN ({session_marks})",
                (*cohort_run_ids, *session_ids),
            )
        connection.execute(
            f"DELETE FROM messages WHERE session_id IN ({session_marks})",
            session_ids,
        )
        connection.execute(
            f"DELETE FROM context_checkpoints WHERE session_id IN ({session_marks})",
            session_ids,
        )
        connection.execute(
            f"DELETE FROM session_leases WHERE session_id IN ({session_marks})",
            session_ids,
        )
        connection.execute(
            f"DELETE FROM runs WHERE session_id IN ({session_marks})",
            session_ids,
        )
        connection.execute(
            f"DELETE FROM sessions WHERE id IN ({session_marks})",
            session_ids,
        )

    def gc(
        self,
        policy: RetentionPolicy,
        *,
        dry_run: bool = True,
        operation_db_path: str | Path | None = None,
    ) -> GcReport:
        resumed = 0 if dry_run else self._resume_pending_artifact_gc(limit=policy.batch_size)
        now_value = self.state_store.now()
        now = now_value.isoformat()
        cutoff = (now_value - timedelta(seconds=policy.terminal_age_seconds)).isoformat()
        artifact_cutoff = (
            now_value - timedelta(seconds=policy.artifact_age_seconds)
        ).isoformat()
        operation_connection: sqlite3.Connection | None = None
        operation_uses_state = False
        if operation_db_path is not None:
            operation_path = Path(operation_db_path).expanduser().resolve()
            if not operation_path.is_file():
                raise RetentionError("operation database does not exist")
            operation_uses_state = operation_path == self.state_store.path.expanduser().resolve()
            if not operation_uses_state:
                try:
                    operation_connection = sqlite3.connect(operation_path)
                    operation_connection.row_factory = sqlite3.Row
                    operation_connection.execute(
                        "BEGIN" if dry_run else "BEGIN IMMEDIATE"
                    )
                except sqlite3.Error as error:
                    if operation_connection is not None:
                        operation_connection.close()
                    raise RetentionError("operation database cannot be locked for GC") from error
        connection = self.state_store.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            effective_operation_connection = (
                connection if operation_uses_state else operation_connection
            )
            artifact_references = _artifact_references(connection)
            scan_limit = min(10_000, policy.batch_size * 10)
            session_rows = connection.execute(
                """
                SELECT id FROM sessions WHERE updated_at<=?
                ORDER BY updated_at, id LIMIT ?
                """,
                (cutoff, scan_limit),
            ).fetchall()
            eligible_sessions: list[str] = []
            blocked_sessions: dict[str, list[str]] = {}
            for row in session_rows:
                session_id = str(row["id"])
                blockers = self._session_blockers(
                    connection,
                    effective_operation_connection,
                    session_id=session_id,
                    cutoff=cutoff,
                    artifact_cutoff=artifact_cutoff,
                    now=now,
                    artifact_references=artifact_references,
                )
                if blockers:
                    blocked_sessions[session_id] = blockers
                elif len(eligible_sessions) < policy.batch_size:
                    eligible_sessions.append(session_id)
            session_artifact_ids: set[str] = set()
            if eligible_sessions:
                marks = ",".join("?" for _ in eligible_sessions)
                session_artifact_ids = {
                    str(row[0])
                    for row in connection.execute(
                        f"SELECT id FROM artifacts WHERE session_id IN ({marks}) "
                        "AND gc_pending_at IS NULL",
                        eligible_sessions,
                    )
                }
            artifact_ids = sorted(session_artifact_ids)
            if artifact_ids:
                marks = ",".join("?" for _ in artifact_ids)
                artifact_rows = connection.execute(
                    f"SELECT * FROM artifacts WHERE id IN ({marks}) ORDER BY id",
                    artifact_ids,
                ).fetchall()
                for artifact_row in artifact_rows:
                    self._verify_gc_artifact(artifact_row)
            run_ids: list[str] = []
            delegated_run_ids: list[str] = []
            if eligible_sessions:
                marks = ",".join("?" for _ in eligible_sessions)
                run_ids = [
                    str(row[0])
                    for row in connection.execute(
                        f"SELECT id FROM runs WHERE session_id IN ({marks}) ORDER BY id",
                        eligible_sessions,
                    )
                ]
                if _table_exists(connection, "delegation_runs"):
                    delegated_run_ids = [
                        str(row[0])
                        for row in connection.execute(
                            f"SELECT id FROM delegation_runs "
                            f"WHERE session_id IN ({marks}) ORDER BY id",
                            eligible_sessions,
                        )
                    ]
            report = GcReport(
                dry_run=dry_run,
                cutoff=cutoff,
                artifact_cutoff=artifact_cutoff,
                scanned_sessions=len(session_rows),
                eligible_sessions=eligible_sessions,
                blocked_sessions=blocked_sessions,
                eligible_artifacts=artifact_ids,
                resumed_artifact_deletions=resumed,
            )
            if not dry_run:
                self._delete_session_roots(
                    connection,
                    eligible_sessions,
                    run_ids,
                    delegated_run_ids,
                )
                remaining_references = _artifact_references(connection)
                if any(remaining_references.get(artifact_id) for artifact_id in artifact_ids):
                    raise RetentionError("artifact remained referenced after parent cleanup")
                if artifact_ids:
                    marks = ",".join("?" for _ in artifact_ids)
                    connection.execute(
                        f"UPDATE artifacts SET gc_pending_at=? "
                        f"WHERE id IN ({marks}) AND gc_pending_at IS NULL",
                        (now, *artifact_ids),
                    )
                hold_resources = [
                    *eligible_sessions,
                    *run_ids,
                    *delegated_run_ids,
                    *artifact_ids,
                ]
                if hold_resources:
                    marks = ",".join("?" for _ in hold_resources)
                    connection.execute(
                        f"DELETE FROM retention_holds WHERE resource_id IN ({marks}) "
                        "AND expires_at IS NOT NULL AND expires_at<=?",
                        (*hold_resources, now),
                    )
            audit_details = {
                "dry_run": dry_run,
                "terminal_age_seconds": policy.terminal_age_seconds,
                "artifact_age_seconds": policy.artifact_age_seconds,
                "batch_size": policy.batch_size,
                "eligible_sessions": eligible_sessions,
                "eligible_artifacts": artifact_ids,
                "blocked_reason_counts": {
                    reason: sum(reason in reasons for reasons in blocked_sessions.values())
                    for reason in sorted(
                        {reason for reasons in blocked_sessions.values() for reason in reasons}
                    )
                },
            }
            connection.execute(
                """
                INSERT INTO audit_events(
                    actor_id, tenant_id, action, resource,
                    decision, details_json, created_at
                ) VALUES ('system', 'system', 'state.gc', 'state_db', ?, ?, ?)
                """,
                (
                    "dry_run" if dry_run else "applied",
                    json.dumps(audit_details, sort_keys=True, separators=(",", ":")),
                    now,
                ),
            )
            connection.commit()
            if not dry_run:
                deleted_artifacts = self._resume_pending_artifact_gc(
                    limit=min(1000, max(policy.batch_size, len(artifact_ids)))
                )
                report.deleted_sessions = len(eligible_sessions)
                report.deleted_runs = len(run_ids) + len(delegated_run_ids)
                report.deleted_artifacts = deleted_artifacts
            return report
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
            if operation_connection is not None:
                operation_connection.rollback()
                operation_connection.close()


__all__ = [
    "BACKUP_FORMAT",
    "BACKUP_DATABASE_NAME",
    "MANIFEST_NAME",
    "BackupRefusedError",
    "BackupResult",
    "BackupValidationError",
    "GcReport",
    "IntegrityReport",
    "RestoreRefusedError",
    "RestoreResult",
    "RetentionError",
    "RetentionPolicy",
    "StateIntegrityError",
    "StateMaintenance",
    "StateMaintenanceError",
    "validate_state_integrity",
    "verify_backup_bundle",
]
