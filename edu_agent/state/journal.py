from __future__ import annotations

import copy
import json
import sqlite3
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from ..observability.redaction import RedactionPolicy


RUN_JOURNAL_SCHEMA_VERSION = 1
RUN_JOURNAL_MIGRATION = "009_run_journal"

_UNSET = object()


class RunPhase(str, Enum):
    """Durable journal phases, including explicit terminal failure branches."""

    ACCEPTED = "accepted"
    PLANNING = "planning"
    MODEL = "model"
    TOOLS = "tools"
    VERIFYING = "verifying"
    FINALIZING = "finalizing"
    TERMINAL = "terminal"
    CANCELLED = "cancelled"
    FAILED = "failed"


JournalRunPhase = RunPhase


class RunStableBoundary(str, Enum):
    ACCEPTED = "accepted"
    PLAN_COMMITTED = "plan_committed"
    MODEL_ATTEMPT_STARTED = "model_attempt_started"
    ASSISTANT_ENVELOPE_COMMITTED = "assistant_envelope_committed"
    TOOL_RESULT_COMMITTED = "tool_result_committed"
    VERIFICATION_COMMITTED = "verification_committed"
    FINAL_MESSAGE_COMMITTED = "final_message_committed"
    TERMINAL = "terminal"
    CANCELLED = "cancelled"
    FAILED = "failed"


StableBoundary = RunStableBoundary


TERMINAL_RUN_PHASES = frozenset(
    {RunPhase.TERMINAL, RunPhase.CANCELLED, RunPhase.FAILED}
)

RUN_PHASE_TRANSITIONS: dict[RunPhase, frozenset[RunPhase]] = {
    RunPhase.ACCEPTED: frozenset(
        {RunPhase.PLANNING, RunPhase.FINALIZING, RunPhase.CANCELLED, RunPhase.FAILED}
    ),
    RunPhase.PLANNING: frozenset(
        {RunPhase.MODEL, RunPhase.FINALIZING, RunPhase.CANCELLED, RunPhase.FAILED}
    ),
    RunPhase.MODEL: frozenset(
        {RunPhase.TOOLS, RunPhase.FINALIZING, RunPhase.CANCELLED, RunPhase.FAILED}
    ),
    RunPhase.TOOLS: frozenset(
        {RunPhase.VERIFYING, RunPhase.FINALIZING, RunPhase.CANCELLED, RunPhase.FAILED}
    ),
    RunPhase.VERIFYING: frozenset(
        {
            RunPhase.MODEL,
            RunPhase.FINALIZING,
            RunPhase.CANCELLED,
            RunPhase.FAILED,
        }
    ),
    RunPhase.FINALIZING: frozenset(
        {RunPhase.TERMINAL, RunPhase.CANCELLED, RunPhase.FAILED}
    ),
    RunPhase.TERMINAL: frozenset(),
    RunPhase.CANCELLED: frozenset(),
    RunPhase.FAILED: frozenset(),
}


def is_legal_phase_transition(
    current: RunPhase | str,
    next_phase: RunPhase | str,
) -> bool:
    try:
        current_phase = RunPhase(current)
        target_phase = RunPhase(next_phase)
    except (TypeError, ValueError):
        return False
    return target_phase in RUN_PHASE_TRANSITIONS[current_phase]


def validate_phase_transition(
    current: RunPhase | str,
    next_phase: RunPhase | str,
) -> None:
    try:
        current_phase = RunPhase(current)
        target_phase = RunPhase(next_phase)
    except (TypeError, ValueError) as error:
        raise RunJournalTransitionError(
            "unknown run journal phase",
            current_phase=repr(current),
            next_phase=repr(next_phase),
        ) from error
    if target_phase not in RUN_PHASE_TRANSITIONS[current_phase]:
        raise RunJournalTransitionError(
            "illegal run journal phase transition",
            current_phase=current_phase.value,
            next_phase=target_phase.value,
        )

_BOUNDARY_ORDER = {
    RunStableBoundary.ACCEPTED: 0,
    RunStableBoundary.PLAN_COMMITTED: 1,
    RunStableBoundary.MODEL_ATTEMPT_STARTED: 2,
    RunStableBoundary.ASSISTANT_ENVELOPE_COMMITTED: 3,
    RunStableBoundary.TOOL_RESULT_COMMITTED: 4,
    RunStableBoundary.VERIFICATION_COMMITTED: 5,
    RunStableBoundary.FINAL_MESSAGE_COMMITTED: 6,
    RunStableBoundary.TERMINAL: 7,
}

_MAX_BOUNDARY_BY_PHASE = {
    RunPhase.ACCEPTED: _BOUNDARY_ORDER[RunStableBoundary.ACCEPTED],
    RunPhase.PLANNING: _BOUNDARY_ORDER[RunStableBoundary.PLAN_COMMITTED],
    RunPhase.MODEL: _BOUNDARY_ORDER[RunStableBoundary.MODEL_ATTEMPT_STARTED],
    RunPhase.TOOLS: _BOUNDARY_ORDER[RunStableBoundary.TOOL_RESULT_COMMITTED],
    RunPhase.VERIFYING: _BOUNDARY_ORDER[RunStableBoundary.VERIFICATION_COMMITTED],
    RunPhase.FINALIZING: _BOUNDARY_ORDER[RunStableBoundary.FINAL_MESSAGE_COMMITTED],
}


class RunJournalError(RuntimeError):
    code = "RUN_JOURNAL_ERROR"

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


class RunJournalNotFound(RunJournalError):
    code = "RUN_JOURNAL_NOT_FOUND"


class RunJournalConflict(RunJournalError):
    code = "RUN_JOURNAL_CAS_CONFLICT"


class RunJournalIdentityError(RunJournalError):
    code = "RUN_JOURNAL_IDENTITY_MISMATCH"


class RunJournalFencingError(RunJournalError):
    code = "RUN_JOURNAL_FENCE_REJECTED"


class RunJournalTransitionError(RunJournalError):
    code = "RUN_JOURNAL_PHASE_REJECTED"


class RunJournalCursorError(RunJournalError):
    code = "RUN_JOURNAL_CURSOR_REJECTED"


class RunJournalCorrupt(RunJournalError):
    code = "RUN_JOURNAL_CORRUPT"


class RunJournalSchemaVersionError(RunJournalError):
    code = "RUN_JOURNAL_SCHEMA_VERSION_UNSUPPORTED"


@dataclass(frozen=True)
class RunJournalReferences:
    context_checkpoint_id: str | None = None
    plan_id: str | None = None
    evidence_id: int | None = None
    operation_id: str | None = None
    artifact_id: str | None = None
    last_tool_event_id: int | None = None

    def to_dict(self) -> dict[str, str | int | None]:
        return {
            "context_checkpoint_id": self.context_checkpoint_id,
            "plan_id": self.plan_id,
            "evidence_id": self.evidence_id,
            "operation_id": self.operation_id,
            "artifact_id": self.artifact_id,
            "last_tool_event_id": self.last_tool_event_id,
        }


@dataclass(frozen=True)
class RunJournalSnapshot:
    schema_version: int
    run_id: str
    session_id: str
    actor_id: str
    tenant_id: str
    phase: RunPhase
    loop_cursor: int
    model_attempt: int
    event_sequence: int
    tool_manifest_hash: str
    frozen_provider_route: dict[str, Any]
    budget_snapshot: dict[str, Any]
    stable_boundary: RunStableBoundary
    references: RunJournalReferences
    writer_id: str
    fencing_token: int
    revision: int
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "actor_id": self.actor_id,
            "tenant_id": self.tenant_id,
            "phase": self.phase.value,
            "loop_cursor": self.loop_cursor,
            "model_attempt": self.model_attempt,
            "event_sequence": self.event_sequence,
            "tool_manifest_hash": self.tool_manifest_hash,
            "frozen_provider_route": copy.deepcopy(self.frozen_provider_route),
            "budget_snapshot": copy.deepcopy(self.budget_snapshot),
            "stable_boundary": self.stable_boundary.value,
            "references": self.references.to_dict(),
            "context_checkpoint_id": self.references.context_checkpoint_id,
            "plan_id": self.references.plan_id,
            "evidence_id": self.references.evidence_id,
            "operation_id": self.references.operation_id,
            "artifact_id": self.references.artifact_id,
            "last_tool_event_id": self.references.last_tool_event_id,
            "writer_id": self.writer_id,
            "fencing_token": self.fencing_token,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def __getitem__(self, key: str) -> Any:
        """Allow callers that use the StateStore dict convention to inspect a snapshot."""
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    @property
    def provider_route(self) -> dict[str, Any]:
        return copy.deepcopy(self.frozen_provider_route)

    @property
    def context_checkpoint_id(self) -> str | None:
        return self.references.context_checkpoint_id

    @property
    def plan_id(self) -> str | None:
        return self.references.plan_id

    @property
    def operation_id(self) -> str | None:
        return self.references.operation_id

    @property
    def artifact_id(self) -> str | None:
        return self.references.artifact_id

    @property
    def cursor(self) -> int:
        return self.loop_cursor

    @property
    def attempt(self) -> int:
        return self.model_attempt

    @property
    def sequence(self) -> int:
        return self.event_sequence

    @property
    def context_checkpoint(self) -> str | None:
        return self.references.context_checkpoint_id


class RunJournal:
    """Thin typed facade over ``StateStore``'s single journal persistence boundary."""

    def __init__(self, state_store):
        self.state_store = state_store

    def create(self, context=None, **kwargs) -> RunJournalSnapshot:
        return self.state_store.create_run_journal(context, **kwargs)

    initialize = create

    def snapshot(self, run_id: str, **scope) -> RunJournalSnapshot:
        return self.state_store.get_run_journal_snapshot(run_id, **scope)

    read = snapshot

    def compare_and_set(self, context=None, **kwargs) -> RunJournalSnapshot:
        return self.state_store.compare_and_set_run_journal(context, **kwargs)

    cas = compare_and_set


_RUN_JOURNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_journals (
    run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    schema_version INTEGER NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    actor_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    loop_cursor INTEGER NOT NULL CHECK (loop_cursor >= 0),
    model_attempt INTEGER NOT NULL CHECK (model_attempt >= 0),
    event_sequence INTEGER NOT NULL CHECK (event_sequence >= 0),
    tool_manifest_hash TEXT NOT NULL,
    provider_route_json TEXT NOT NULL,
    budget_snapshot_json TEXT NOT NULL,
    stable_boundary TEXT NOT NULL,
    context_checkpoint_id TEXT REFERENCES context_checkpoints(id),
    plan_id TEXT REFERENCES plans(id),
    evidence_id INTEGER REFERENCES evidence(id),
    operation_id TEXT REFERENCES tool_operation_refs(operation_id),
    artifact_id TEXT REFERENCES artifacts(id),
    last_tool_event_id INTEGER REFERENCES tool_events(id),
    writer_id TEXT NOT NULL,
    fencing_token INTEGER NOT NULL CHECK (fencing_token >= 0),
    revision INTEGER NOT NULL CHECK (revision > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_run_journals_scope
    ON run_journals(tenant_id, actor_id, session_id, run_id);
"""

_REQUIRED_COLUMNS = frozenset(
    {
        "run_id",
        "schema_version",
        "session_id",
        "actor_id",
        "tenant_id",
        "phase",
        "loop_cursor",
        "model_attempt",
        "event_sequence",
        "tool_manifest_hash",
        "provider_route_json",
        "budget_snapshot_json",
        "stable_boundary",
        "context_checkpoint_id",
        "plan_id",
        "evidence_id",
        "operation_id",
        "artifact_id",
        "last_tool_event_id",
        "writer_id",
        "fencing_token",
        "revision",
        "created_at",
        "updated_at",
    }
)


def initialize_run_journal_schema(connection: sqlite3.Connection, *, now: str) -> None:
    already_applied = connection.execute(
        "SELECT 1 FROM state_schema_migrations WHERE version=?",
        (RUN_JOURNAL_MIGRATION,),
    ).fetchone()
    connection.executescript(_RUN_JOURNAL_SCHEMA)
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(run_journals)")
    }
    missing = sorted(_REQUIRED_COLUMNS - columns)
    if missing:
        raise RunJournalCorrupt(
            "run_journals schema is incomplete",
            missing_columns=missing,
        )
    if already_applied is None:
        for row in connection.execute("SELECT * FROM run_journals"):
            _decode_snapshot(row)
    connection.execute(
        """
        INSERT OR IGNORE INTO state_schema_migrations(version, applied_at)
        VALUES (?, ?)
        """,
        (RUN_JOURNAL_MIGRATION, now),
    )


def _enum_value(enum_type, value: Any, field: str):
    try:
        return enum_type(value)
    except (TypeError, ValueError) as error:
        raise RunJournalCorrupt(
            f"unknown {field}",
            field=field,
            value=repr(value),
        ) from error


def _input_phase(value: RunPhase | str, field: str) -> RunPhase:
    try:
        return RunPhase(value)
    except (TypeError, ValueError) as error:
        raise RunJournalTransitionError(
            f"unknown {field}",
            field=field,
            value=repr(value),
        ) from error


def _input_boundary(value: RunStableBoundary | str) -> RunStableBoundary:
    try:
        return RunStableBoundary(value)
    except (TypeError, ValueError) as error:
        raise RunJournalTransitionError(
            "unknown stable boundary",
            stable_boundary=repr(value),
        ) from error


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RunJournalCursorError(
            f"{field} must be a non-negative integer",
            field=field,
            value=repr(value),
        )
    return value


def _positive_int(value: Any, field: str) -> int:
    value = _non_negative_int(value, field)
    if value == 0:
        raise RunJournalCursorError(
            f"{field} must be a positive integer",
            field=field,
            value=value,
        )
    return value


def _json_object(value: Mapping[str, Any], field: str) -> tuple[str, dict[str, Any]]:
    if not isinstance(value, Mapping):
        raise RunJournalCorrupt(f"{field} must be a JSON object", field=field)

    # Validate the caller-owned graph before redaction.  The shared redaction
    # helper is intentionally a simple recursive transformer and therefore
    # cannot safely inspect a cyclic object graph.  Track only the active
    # recursion path so a harmless shared child is accepted while a real cycle
    # fails with a journal-specific error instead of leaking RecursionError.
    active: set[int] = set()

    def validate_graph(item: Any) -> None:
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in active:
                raise RunJournalCorrupt(
                    f"{field} must not contain circular references",
                    field=field,
                )
            active.add(identity)
            try:
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise RunJournalCorrupt(
                            f"{field} JSON object keys must be strings",
                            field=field,
                        )
                    validate_graph(child)
            finally:
                active.remove(identity)
        elif isinstance(item, (list, tuple)):
            identity = id(item)
            if identity in active:
                raise RunJournalCorrupt(
                    f"{field} must not contain circular references",
                    field=field,
                )
            active.add(identity)
            try:
                for child in item:
                    validate_graph(child)
            finally:
                active.remove(identity)

    validate_graph(value)
    redacted = RedactionPolicy().redact(copy.deepcopy(dict(value)))
    try:
        encoded = json.dumps(
            redacted,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise RunJournalCorrupt(
            f"{field} must contain finite JSON values",
            field=field,
        ) from error
    return encoded, redacted


def _load_json_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, str):
        raise RunJournalCorrupt(f"{field} is not encoded JSON", field=field)

    def reject_constant(constant: str):
        raise ValueError(f"non-finite JSON constant: {constant}")

    try:
        decoded = json.loads(value, parse_constant=reject_constant)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RunJournalCorrupt(f"{field} contains invalid JSON", field=field) from error
    if not isinstance(decoded, dict):
        raise RunJournalCorrupt(f"{field} must decode to an object", field=field)
    return decoded


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RunJournalCorrupt(f"{field} must be non-empty", field=field)
    return value


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field)


def _optional_id(value: Any, field: str) -> int | None:
    if value is None:
        return None
    return _non_negative_int(value, field)


def _validate_terminal_boundary(
    phase: RunPhase,
    boundary: RunStableBoundary,
    *,
    error_type: type[RunJournalError],
) -> None:
    required = {
        RunPhase.TERMINAL: RunStableBoundary.TERMINAL,
        RunPhase.CANCELLED: RunStableBoundary.CANCELLED,
        RunPhase.FAILED: RunStableBoundary.FAILED,
    }
    expected = required.get(phase)
    if expected is not None and boundary is not expected:
        raise error_type(
            "terminal phase and stable boundary disagree",
            phase=phase.value,
            stable_boundary=boundary.value,
            required_boundary=expected.value,
        )
    if expected is None and boundary in {
        RunStableBoundary.TERMINAL,
        RunStableBoundary.CANCELLED,
        RunStableBoundary.FAILED,
    }:
        raise error_type(
            "non-terminal phase cannot claim a terminal stable boundary",
            phase=phase.value,
            stable_boundary=boundary.value,
        )
    if (
        expected is None
        and boundary in _BOUNDARY_ORDER
        and _BOUNDARY_ORDER[boundary] > _MAX_BOUNDARY_BY_PHASE[phase]
    ):
        raise error_type(
            "stable boundary is ahead of the journal phase",
            phase=phase.value,
            stable_boundary=boundary.value,
        )


def _decode_snapshot(row: sqlite3.Row | Mapping[str, Any]) -> RunJournalSnapshot:
    record = dict(row)
    schema_version = record.get("schema_version")
    if schema_version != RUN_JOURNAL_SCHEMA_VERSION:
        raise RunJournalSchemaVersionError(
            "run journal row schema version is unsupported",
            stored_version=schema_version,
            supported_version=RUN_JOURNAL_SCHEMA_VERSION,
        )
    phase = _enum_value(RunPhase, record.get("phase"), "phase")
    boundary = _enum_value(
        RunStableBoundary,
        record.get("stable_boundary"),
        "stable_boundary",
    )
    _validate_terminal_boundary(phase, boundary, error_type=RunJournalCorrupt)
    manifest_hash = _required_text(record.get("tool_manifest_hash"), "tool_manifest_hash")
    if not manifest_hash.strip() or any(ord(char) < 32 for char in manifest_hash):
        raise RunJournalCorrupt("tool_manifest_hash must be a non-empty hash", field="tool_manifest_hash")
    references = RunJournalReferences(
        context_checkpoint_id=_optional_text(
            record.get("context_checkpoint_id"), "context_checkpoint_id"
        ),
        plan_id=_optional_text(record.get("plan_id"), "plan_id"),
        evidence_id=_optional_id(record.get("evidence_id"), "evidence_id"),
        operation_id=_optional_text(record.get("operation_id"), "operation_id"),
        artifact_id=_optional_text(record.get("artifact_id"), "artifact_id"),
        last_tool_event_id=_optional_id(
            record.get("last_tool_event_id"), "last_tool_event_id"
        ),
    )
    return RunJournalSnapshot(
        schema_version=schema_version,
        run_id=_required_text(record.get("run_id"), "run_id"),
        session_id=_required_text(record.get("session_id"), "session_id"),
        actor_id=_required_text(record.get("actor_id"), "actor_id"),
        tenant_id=_required_text(record.get("tenant_id"), "tenant_id"),
        phase=phase,
        loop_cursor=_non_negative_int(record.get("loop_cursor"), "loop_cursor"),
        model_attempt=_non_negative_int(record.get("model_attempt"), "model_attempt"),
        event_sequence=_non_negative_int(
            record.get("event_sequence"), "event_sequence"
        ),
        tool_manifest_hash=manifest_hash,
        frozen_provider_route=_load_json_object(
            record.get("provider_route_json"), "provider_route_json"
        ),
        budget_snapshot=_load_json_object(
            record.get("budget_snapshot_json"), "budget_snapshot_json"
        ),
        stable_boundary=boundary,
        references=references,
        writer_id=_required_text(record.get("writer_id"), "writer_id"),
        fencing_token=_non_negative_int(
            record.get("fencing_token"), "fencing_token"
        ),
        revision=_positive_int(record.get("revision"), "revision"),
        created_at=_required_text(record.get("created_at"), "created_at"),
        updated_at=_required_text(record.get("updated_at"), "updated_at"),
    )


def _load_run_scope(connection: sqlite3.Connection, context) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT r.id AS run_id, r.session_id, r.actor_id, r.tenant_id, r.status,
               s.actor_id AS session_actor_id, s.tenant_id AS session_tenant_id
        FROM runs r
        JOIN sessions s ON s.id=r.session_id
        WHERE r.id=?
        """,
        (context.run_id,),
    ).fetchone()
    if row is None:
        raise RunJournalIdentityError("run or session does not exist", run_id=context.run_id)
    expected = {
        "session_id": context.session_id,
        "actor_id": context.actor_id,
        "tenant_id": context.tenant_id,
    }
    actual = {
        "session_id": row["session_id"],
        "actor_id": row["actor_id"],
        "tenant_id": row["tenant_id"],
    }
    if actual != expected or (
        row["session_actor_id"] != context.actor_id
        or row["session_tenant_id"] != context.tenant_id
    ):
        raise RunJournalIdentityError(
            "run/session/actor/tenant scope does not match",
            run_id=context.run_id,
        )
    return row


def _resolve_initial_writer(
    connection: sqlite3.Connection,
    store,
    context,
    *,
    writer_id: str | None,
    fencing_token: int | None,
) -> tuple[str, int]:
    context_writer = getattr(context, "lease_owner", None)
    context_token = getattr(context, "fencing_token", None)
    if context_writer is None and context_token is None:
        if fencing_token != 0 or not isinstance(writer_id, str) or not writer_id.strip():
            raise RunJournalFencingError(
                "pre-lease journal initialization requires an explicit writer and token 0"
            )
        return writer_id, 0
    if not context_writer or context_token is None:
        raise RunJournalFencingError("run lease identity is incomplete")
    if writer_id is not None and writer_id != context_writer:
        raise RunJournalFencingError("writer does not match the bound run lease")
    if fencing_token is not None and fencing_token != context_token:
        raise RunJournalFencingError("token does not match the bound run lease")
    _assert_current_writer(
        connection,
        store,
        context,
        target_phase=RunPhase.ACCEPTED,
    )
    return context_writer, int(context_token)


def _assert_current_writer(
    connection: sqlite3.Connection,
    store,
    context,
    *,
    target_phase: RunPhase,
) -> tuple[str, int]:
    writer_id = getattr(context, "lease_owner", None)
    fencing_token = getattr(context, "fencing_token", None)
    if not writer_id or fencing_token is None:
        raise RunJournalFencingError("journal CAS requires a bound run lease")
    row = connection.execute(
        """
        SELECT l.lease_owner, l.fencing_token, l.active_run_id, l.expires_at,
               r.status, r.actor_id, r.tenant_id, r.session_id
        FROM session_leases l
        JOIN runs r ON r.id=? AND r.session_id=l.session_id
        WHERE l.session_id=?
        """,
        (context.run_id, context.session_id),
    ).fetchone()
    if row is None:
        raise RunJournalFencingError("session lease does not exist")
    if (
        row["lease_owner"] != writer_id
        or int(row["fencing_token"]) != int(fencing_token)
        or row["active_run_id"] != context.run_id
        or row["expires_at"] <= store.now_iso()
    ):
        raise RunJournalFencingError(
            "lease expired or fencing token is stale",
            run_id=context.run_id,
            fencing_token=int(fencing_token),
        )
    if (
        row["session_id"] != context.session_id
        or row["actor_id"] != context.actor_id
        or row["tenant_id"] != context.tenant_id
    ):
        raise RunJournalIdentityError("lease scope does not match run context")
    if row["status"] == "cancel_requested" and target_phase is not RunPhase.CANCELLED:
        raise RunJournalFencingError(
            "cancel-requested run may only advance journal to cancelled",
            run_status=row["status"],
            target_phase=target_phase.value,
        )
    if row["status"] not in {"running", "cancel_requested"}:
        raise RunJournalFencingError(
            "run status is not writable",
            run_status=row["status"],
        )
    return writer_id, int(fencing_token)


def _validate_reference(
    connection: sqlite3.Connection,
    *,
    field: str,
    value: str | int | None,
    run_id: str,
    session_id: str,
    actor_id: str,
    tenant_id: str,
) -> None:
    if value is None:
        return
    queries = {
        "context_checkpoint_id": (
            "SELECT 1 FROM context_checkpoints WHERE id=? AND session_id=?",
            (value, session_id),
        ),
        "plan_id": (
            """SELECT 1 FROM plans WHERE id=? AND run_id=? AND session_id=?
               AND actor_id=? AND tenant_id=?""",
            (value, run_id, session_id, actor_id, tenant_id),
        ),
        "evidence_id": (
            """SELECT 1 FROM evidence WHERE id=? AND run_id=? AND session_id=?
               AND actor_id=? AND tenant_id=?""",
            (value, run_id, session_id, actor_id, tenant_id),
        ),
        "operation_id": (
            """SELECT 1 FROM tool_operation_refs WHERE operation_id=? AND run_id=?
               AND session_id=? AND actor_id=? AND tenant_id=?""",
            (value, run_id, session_id, actor_id, tenant_id),
        ),
        "artifact_id": (
            """SELECT 1 FROM artifacts WHERE id=? AND run_id=? AND session_id=?
               AND actor_id=? AND tenant_id=?""",
            (value, run_id, session_id, actor_id, tenant_id),
        ),
        "last_tool_event_id": (
            "SELECT 1 FROM tool_events WHERE id=? AND run_id=? AND session_id=?",
            (value, run_id, session_id),
        ),
    }
    query, parameters = queries[field]
    if connection.execute(query, parameters).fetchone() is None:
        raise RunJournalIdentityError(
            "journal reference is missing or outside run scope",
            field=field,
            reference=value,
        )


def _validate_references(
    connection: sqlite3.Connection,
    references: RunJournalReferences,
    *,
    run_id: str,
    session_id: str,
    actor_id: str,
    tenant_id: str,
) -> None:
    for field, value in references.to_dict().items():
        _validate_reference(
            connection,
            field=field,
            value=value,
            run_id=run_id,
            session_id=session_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
        )


def initialize_run_journal(
    store,
    context,
    *,
    tool_manifest_hash: str,
    frozen_provider_route: Mapping[str, Any],
    budget_snapshot: Mapping[str, Any],
    context_checkpoint_id: str | None = None,
    writer_id: str | None = None,
    fencing_token: int | None = None,
) -> RunJournalSnapshot:
    if not isinstance(tool_manifest_hash, str) or not tool_manifest_hash.strip():
        raise RunJournalCorrupt("tool_manifest_hash must be a non-empty hash")
    route_json, _ = _json_object(frozen_provider_route, "frozen_provider_route")
    budget_json, _ = _json_object(budget_snapshot, "budget_snapshot")
    now = store.now_iso()
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        run = _load_run_scope(connection, context)
        resolved_writer, resolved_token = _resolve_initial_writer(
            connection,
            store,
            context,
            writer_id=writer_id,
            fencing_token=fencing_token,
        )
        if resolved_token == 0 and run["status"] != "queued":
            raise RunJournalFencingError(
                "token 0 initialization is only valid while the run is queued",
                run_status=run["status"],
            )
        references = RunJournalReferences(
            context_checkpoint_id=context_checkpoint_id,
        )
        _validate_references(
            connection,
            references,
            run_id=context.run_id,
            session_id=context.session_id,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
        )
        try:
            connection.execute(
                """
                INSERT INTO run_journals(
                    run_id, schema_version, session_id, actor_id, tenant_id,
                    phase, loop_cursor, model_attempt, event_sequence,
                    tool_manifest_hash, provider_route_json, budget_snapshot_json,
                    stable_boundary, context_checkpoint_id, writer_id,
                    fencing_token, revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'accepted', 0, 0, 0, ?, ?, ?,
                          'accepted', ?, ?, ?, 1, ?, ?)
                """,
                (
                    context.run_id,
                    RUN_JOURNAL_SCHEMA_VERSION,
                    context.session_id,
                    context.actor_id,
                    context.tenant_id,
                    tool_manifest_hash,
                    route_json,
                    budget_json,
                    context_checkpoint_id,
                    resolved_writer,
                    resolved_token,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as error:
            existing = connection.execute(
                "SELECT 1 FROM run_journals WHERE run_id=?",
                (context.run_id,),
            ).fetchone()
            if existing is not None:
                raise RunJournalConflict(
                    "run journal already exists",
                    run_id=context.run_id,
                ) from error
            raise RunJournalIdentityError(
                "run journal references an invalid persistent identity",
                run_id=context.run_id,
            ) from error
        row = connection.execute(
            "SELECT * FROM run_journals WHERE run_id=?",
            (context.run_id,),
        ).fetchone()
    return _decode_snapshot(row)


def _merged_references(
    current: RunJournalReferences,
    *,
    context_checkpoint_id: str | None | object,
    plan_id: str | None | object,
    evidence_id: int | None | object,
    operation_id: str | None | object,
    artifact_id: str | None | object,
    last_tool_event_id: int | None | object,
) -> RunJournalReferences:
    values = current.to_dict()
    updates = {
        "context_checkpoint_id": context_checkpoint_id,
        "plan_id": plan_id,
        "evidence_id": evidence_id,
        "operation_id": operation_id,
        "artifact_id": artifact_id,
        "last_tool_event_id": last_tool_event_id,
    }
    for field, value in updates.items():
        if value is not _UNSET:
            values[field] = value
    for field in ("context_checkpoint_id", "plan_id", "operation_id", "artifact_id"):
        value = values[field]
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise RunJournalIdentityError(f"{field} must be a non-empty string or null")
    for field in ("evidence_id", "last_tool_event_id"):
        value = values[field]
        if value is not None:
            _non_negative_int(value, field)
    for field in (
        "context_checkpoint_id",
        "plan_id",
        "operation_id",
        "artifact_id",
    ):
        if getattr(current, field) is not None and values[field] is None:
            raise RunJournalIdentityError(f"{field} reference cannot be cleared")
    if current.plan_id is not None and values["plan_id"] != current.plan_id:
        raise RunJournalIdentityError("plan_id is frozen once recorded")
    if current.evidence_id is not None and (
        values["evidence_id"] is None or values["evidence_id"] < current.evidence_id
    ):
        raise RunJournalCursorError("evidence_id cannot move backwards")
    if current.last_tool_event_id is not None and (
        values["last_tool_event_id"] is None
        or values["last_tool_event_id"] < current.last_tool_event_id
    ):
        raise RunJournalCursorError("last_tool_event_id cannot move backwards")
    return RunJournalReferences(**values)


def compare_and_set_run_journal(
    store,
    context,
    *,
    expected_revision: int,
    expected_phase: RunPhase | str,
    phase: RunPhase | str,
    expected_loop_cursor: int,
    loop_cursor: int,
    expected_model_attempt: int,
    model_attempt: int,
    expected_event_sequence: int,
    event_sequence: int,
    expected_fencing_token: int,
    stable_boundary: RunStableBoundary | str,
    budget_snapshot: Mapping[str, Any],
    context_checkpoint_id: str | None | object = _UNSET,
    plan_id: str | None | object = _UNSET,
    evidence_id: int | None | object = _UNSET,
    operation_id: str | None | object = _UNSET,
    artifact_id: str | None | object = _UNSET,
    last_tool_event_id: int | None | object = _UNSET,
) -> RunJournalSnapshot:
    expected_revision = _positive_int(expected_revision, "expected_revision")
    expected_loop_cursor = _non_negative_int(
        expected_loop_cursor, "expected_loop_cursor"
    )
    expected_model_attempt = _non_negative_int(
        expected_model_attempt, "expected_model_attempt"
    )
    expected_event_sequence = _non_negative_int(
        expected_event_sequence, "expected_event_sequence"
    )
    expected_fencing_token = _non_negative_int(
        expected_fencing_token, "expected_fencing_token"
    )
    loop_cursor = _non_negative_int(loop_cursor, "loop_cursor")
    model_attempt = _non_negative_int(model_attempt, "model_attempt")
    event_sequence = _non_negative_int(event_sequence, "event_sequence")
    expected_phase_value = _input_phase(expected_phase, "expected_phase")
    next_phase = _input_phase(phase, "phase")
    next_boundary = _input_boundary(stable_boundary)
    _validate_terminal_boundary(
        next_phase,
        next_boundary,
        error_type=RunJournalTransitionError,
    )
    budget_json, _ = _json_object(budget_snapshot, "budget_snapshot")

    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _load_run_scope(connection, context)
        writer_id, fencing_token = _assert_current_writer(
            connection,
            store,
            context,
            target_phase=next_phase,
        )
        row = connection.execute(
            "SELECT * FROM run_journals WHERE run_id=?",
            (context.run_id,),
        ).fetchone()
        if row is None:
            raise RunJournalNotFound("run journal does not exist", run_id=context.run_id)
        current = _decode_snapshot(row)
        if (
            current.session_id != context.session_id
            or current.actor_id != context.actor_id
            or current.tenant_id != context.tenant_id
        ):
            raise RunJournalIdentityError("journal scope does not match run context")
        actual = {
            "revision": current.revision,
            "phase": current.phase.value,
            "loop_cursor": current.loop_cursor,
            "model_attempt": current.model_attempt,
            "event_sequence": current.event_sequence,
            "fencing_token": current.fencing_token,
        }
        expected = {
            "revision": expected_revision,
            "phase": expected_phase_value.value,
            "loop_cursor": expected_loop_cursor,
            "model_attempt": expected_model_attempt,
            "event_sequence": expected_event_sequence,
            "fencing_token": expected_fencing_token,
        }
        if actual != expected:
            raise RunJournalConflict(
                "journal compare-and-set expectation did not match",
                expected=expected,
                actual=actual,
            )
        if fencing_token < current.fencing_token:
            raise RunJournalFencingError(
                "journal fencing token cannot move backwards",
                current_fencing_token=current.fencing_token,
                writer_fencing_token=fencing_token,
            )
        if fencing_token == current.fencing_token and writer_id != current.writer_id:
            raise RunJournalFencingError(
                "same fencing token cannot be used by another writer",
                fencing_token=fencing_token,
            )
        if current.phase in TERMINAL_RUN_PHASES:
            raise RunJournalTransitionError(
                "terminal journal cannot re-enter execution",
                current_phase=current.phase.value,
                next_phase=next_phase.value,
            )
        if next_phase is not current.phase:
            validate_phase_transition(current.phase, next_phase)
        counters = {
            "loop_cursor": (current.loop_cursor, loop_cursor),
            "model_attempt": (current.model_attempt, model_attempt),
            "event_sequence": (current.event_sequence, event_sequence),
        }
        regressed = {
            name: {"current": old, "next": new}
            for name, (old, new) in counters.items()
            if new < old
        }
        if regressed:
            raise RunJournalCursorError(
                "journal cursors cannot move backwards",
                regressions=regressed,
            )
        if next_phase is current.phase and all(new == old for old, new in counters.values()):
            raise RunJournalConflict(
                "duplicate journal update made no phase or cursor progress",
                phase=current.phase.value,
                revision=current.revision,
            )
        boundary_regressed = (
            current.stable_boundary in _BOUNDARY_ORDER
            and next_boundary in _BOUNDARY_ORDER
            and _BOUNDARY_ORDER[next_boundary] < _BOUNDARY_ORDER[current.stable_boundary]
        )
        next_loop_started = (
            current.phase is RunPhase.VERIFYING
            and next_phase is RunPhase.MODEL
            and loop_cursor > current.loop_cursor
            and model_attempt > current.model_attempt
            and next_boundary is RunStableBoundary.MODEL_ATTEMPT_STARTED
        )
        if boundary_regressed and not next_loop_started:
            raise RunJournalCursorError(
                "stable boundary cannot move backwards",
                current_boundary=current.stable_boundary.value,
                next_boundary=next_boundary.value,
            )
        references = _merged_references(
            current.references,
            context_checkpoint_id=context_checkpoint_id,
            plan_id=plan_id,
            evidence_id=evidence_id,
            operation_id=operation_id,
            artifact_id=artifact_id,
            last_tool_event_id=last_tool_event_id,
        )
        _validate_references(
            connection,
            references,
            run_id=context.run_id,
            session_id=context.session_id,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
        )
        now = store.now_iso()
        cursor = connection.execute(
            """
            UPDATE run_journals
            SET phase=?, loop_cursor=?, model_attempt=?, event_sequence=?,
                budget_snapshot_json=?, stable_boundary=?,
                context_checkpoint_id=?, plan_id=?, evidence_id=?, operation_id=?,
                artifact_id=?, last_tool_event_id=?, writer_id=?, fencing_token=?,
                revision=revision+1, updated_at=?
            WHERE run_id=? AND session_id=? AND actor_id=? AND tenant_id=?
                AND revision=? AND phase=? AND loop_cursor=? AND model_attempt=?
                AND event_sequence=? AND fencing_token=?
            """,
            (
                next_phase.value,
                loop_cursor,
                model_attempt,
                event_sequence,
                budget_json,
                next_boundary.value,
                references.context_checkpoint_id,
                references.plan_id,
                references.evidence_id,
                references.operation_id,
                references.artifact_id,
                references.last_tool_event_id,
                writer_id,
                fencing_token,
                now,
                context.run_id,
                context.session_id,
                context.actor_id,
                context.tenant_id,
                expected_revision,
                expected_phase_value.value,
                expected_loop_cursor,
                expected_model_attempt,
                expected_event_sequence,
                expected_fencing_token,
            ),
        )
        if cursor.rowcount != 1:
            raise RunJournalConflict(
                "journal compare-and-set lost a concurrent race",
                run_id=context.run_id,
                expected_revision=expected_revision,
            )
        updated = connection.execute(
            "SELECT * FROM run_journals WHERE run_id=?",
            (context.run_id,),
        ).fetchone()
    return _decode_snapshot(updated)


def get_run_journal_snapshot(
    store,
    *,
    run_id: str,
    session_id: str,
    actor_id: str,
    tenant_id: str,
) -> RunJournalSnapshot:
    with store.connect() as connection:
        connection.execute("BEGIN")
        row = connection.execute(
            "SELECT * FROM run_journals WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise RunJournalNotFound("run journal does not exist", run_id=run_id)
        snapshot = _decode_snapshot(row)
        expected = (run_id, session_id, actor_id, tenant_id)
        actual = (
            snapshot.run_id,
            snapshot.session_id,
            snapshot.actor_id,
            snapshot.tenant_id,
        )
        if actual != expected:
            raise RunJournalIdentityError(
                "journal scope does not match requested scope",
                run_id=run_id,
            )
        run = connection.execute(
            """
            SELECT r.session_id, r.actor_id, r.tenant_id,
                   s.actor_id AS session_actor_id, s.tenant_id AS session_tenant_id
            FROM runs r JOIN sessions s ON s.id=r.session_id WHERE r.id=?
            """,
            (run_id,),
        ).fetchone()
        if run is None or (
            run["session_id"] != session_id
            or run["actor_id"] != actor_id
            or run["tenant_id"] != tenant_id
            or run["session_actor_id"] != actor_id
            or run["session_tenant_id"] != tenant_id
        ):
            raise RunJournalIdentityError("journal owner truth source does not match")
        _validate_references(
            connection,
            snapshot.references,
            run_id=run_id,
            session_id=session_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
        )
        return snapshot


__all__ = [
    "RUN_JOURNAL_MIGRATION",
    "RUN_JOURNAL_SCHEMA_VERSION",
    "RUN_PHASE_TRANSITIONS",
    "is_legal_phase_transition",
    "TERMINAL_RUN_PHASES",
    "RunJournalConflict",
    "RunJournalCorrupt",
    "RunJournalCursorError",
    "RunJournalError",
    "RunJournalFencingError",
    "RunJournalIdentityError",
    "RunJournalNotFound",
    "RunJournalReferences",
    "RunJournal",
    "RunJournalSchemaVersionError",
    "RunJournalSnapshot",
    "RunJournalTransitionError",
    "RunPhase",
    "JournalRunPhase",
    "RunStableBoundary",
    "StableBoundary",
    "compare_and_set_run_journal",
    "get_run_journal_snapshot",
    "initialize_run_journal",
    "initialize_run_journal_schema",
    "validate_phase_transition",
]
