from __future__ import annotations

import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from edu_agent.runtime.models import RunContext
from edu_agent.state import (
    RUN_JOURNAL_MIGRATION,
    RUN_JOURNAL_SCHEMA_VERSION,
    RunJournalConflict,
    RunJournalCorrupt,
    RunJournalCursorError,
    RunJournalFencingError,
    RunJournalIdentityError,
    RunJournalSchemaVersionError,
    RunJournalTransitionError,
    RunPhase,
    RunStableBoundary,
    StateSchemaVersionError,
    StateStore,
)


MANIFEST_HASH = hashlib.sha256(b"r2.2-tool-manifest").hexdigest()


class MutableClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


def _context(*, run_id: str = "run-1", session_id: str = "session-1") -> RunContext:
    return RunContext.create(
        run_id=run_id,
        session_id=session_id,
        actor_id="teacher-1",
        tenant_id="school-1",
        role="teacher",
    )


def _queued(store: StateStore, context: RunContext) -> None:
    store.ensure_session(
        context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        role=context.role,
    )
    store.enqueue_run(context, request_text="journal fixture")


def _claim(
    store: StateStore,
    context: RunContext,
    *,
    owner: str = "worker-1",
    lease_seconds: float = 30,
) -> dict:
    claim = store.acquire_session_lease(
        session_id=context.session_id,
        run_id=context.run_id,
        owner_id=owner,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        lease_seconds=lease_seconds,
    )
    context.bind_runtime_control(
        lease_owner=owner,
        fencing_token=int(claim["fencing_token"]),
        control_check=lambda _boundary: None,
    )
    return claim


def _create_prelease(store: StateStore, context: RunContext):
    return store.create_run_journal(
        context,
        tool_manifest_hash=MANIFEST_HASH,
        frozen_provider_route={
            "provider": "mock",
            "model": "deterministic",
            "api_mode": "chat_completions",
            "api_key": "must-not-persist",
        },
        budget_snapshot={"model_calls": 0, "tool_calls": 0},
        writer_id="request-acceptor",
        fencing_token=0,
    )


def _cas(
    store: StateStore,
    context: RunContext,
    snapshot,
    *,
    phase: RunPhase,
    boundary: RunStableBoundary,
    loop_cursor: int | None = None,
    model_attempt: int | None = None,
    event_sequence: int | None = None,
    **references,
):
    return store.compare_and_set_run_journal(
        context,
        expected_revision=snapshot.revision,
        expected_phase=snapshot.phase,
        phase=phase,
        expected_loop_cursor=snapshot.loop_cursor,
        loop_cursor=snapshot.loop_cursor if loop_cursor is None else loop_cursor,
        expected_model_attempt=snapshot.model_attempt,
        model_attempt=(
            snapshot.model_attempt if model_attempt is None else model_attempt
        ),
        expected_event_sequence=snapshot.event_sequence,
        event_sequence=(
            snapshot.event_sequence + 1
            if event_sequence is None
            else event_sequence
        ),
        expected_fencing_token=snapshot.fencing_token,
        stable_boundary=boundary,
        budget_snapshot={
            "model_calls": snapshot.model_attempt
            if model_attempt is None
            else model_attempt,
            "tool_calls": snapshot.loop_cursor,
        },
        **references,
    )


def test_new_database_has_minimal_journal_schema_and_strict_snapshot(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context()
    _queued(store, context)
    created = _create_prelease(store, context)

    assert created.schema_version == RUN_JOURNAL_SCHEMA_VERSION
    assert created.phase is RunPhase.ACCEPTED
    assert created.stable_boundary is RunStableBoundary.ACCEPTED
    assert created.loop_cursor == created.model_attempt == created.event_sequence == 0
    assert created.fencing_token == 0 and created.revision == 1
    assert created.frozen_provider_route["api_key"] == "[REDACTED]"
    assert "must-not-persist" not in str(created.to_dict())

    reopened = StateStore(tmp_path / "state.db")
    snapshot = reopened.get_run_journal_snapshot(
        context.run_id,
        session_id=context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
    )
    assert snapshot == created
    readonly = StateStore(tmp_path / "state.db", read_only=True)
    assert readonly.get_run_journal_snapshot(
        context.run_id,
        session_id=context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
    ) == created
    with reopened.connect() as connection:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(run_journals)")
        }
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        migration_count = connection.execute(
            "SELECT COUNT(*) FROM state_schema_migrations WHERE version=?",
            (RUN_JOURNAL_MIGRATION,),
        ).fetchone()[0]
    assert user_version == 12 and migration_count == 1
    assert not {
        "plan_json",
        "evidence_json",
        "operation_json",
        "artifact_json",
        "trace_json",
    } & columns
    assert store.count("run_journals") == 1

    with pytest.raises(RunJournalIdentityError):
        store.get_run_journal_snapshot(
            context.run_id,
            session_id=context.session_id,
            actor_id="other-actor",
            tenant_id=context.tenant_id,
        )


def test_journal_json_accepts_shared_children_and_rejects_cycles(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context()
    _queued(store, context)
    shared = {"model": "deterministic"}

    created = store.create_run_journal(
        context,
        tool_manifest_hash=MANIFEST_HASH,
        frozen_provider_route={"primary": shared, "fallback": shared},
        budget_snapshot={},
        writer_id="request-acceptor",
        fencing_token=0,
    )
    assert created.frozen_provider_route == {
        "primary": shared,
        "fallback": shared,
    }

    cyclic_context = _context(run_id="run-cyclic", session_id="session-cyclic")
    _queued(store, cyclic_context)
    cyclic_budget: dict[str, object] = {}
    cyclic_budget["self"] = cyclic_budget
    with pytest.raises(RunJournalCorrupt, match="circular references") as rejected:
        store.create_run_journal(
            cyclic_context,
            tool_manifest_hash=MANIFEST_HASH,
            frozen_provider_route={"provider": "mock"},
            budget_snapshot=cyclic_budget,
            writer_id="request-acceptor",
            fencing_token=0,
        )
    assert rejected.value.code == "RUN_JOURNAL_CORRUPT"


def test_explicit_scope_initialization_and_truth_source_references(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context()
    _queued(store, context)
    snapshot = store.create_run_journal(
        run_id=context.run_id,
        session_id=context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        tool_manifest_hash=MANIFEST_HASH,
        provider_route={"provider": "mock"},
        budget_snapshot={},
        writer_id="request-acceptor",
        fencing_token=0,
    )
    _claim(store, context)

    other = _context(run_id="run-2", session_id="session-2")
    _queued(store, other)
    _claim(store, other, owner="worker-2")
    other_plan = store.create_plan(
        run_id=other.run_id,
        session_id=other.session_id,
        actor_id=other.actor_id,
        tenant_id=other.tenant_id,
        spec={
            "goal": "other run",
            "steps": [
                {
                    "id": "step-1",
                    "goal": "query",
                    "depends_on": [],
                    "allowed_tools": ["list_exams"],
                    "expected_tools": ["list_exams"],
                    "completion_conditions": ["tool_event"],
                }
            ],
        },
        max_iterations=2,
        context=other,
    )

    with pytest.raises(RunJournalIdentityError, match="outside run scope"):
        _cas(
            store,
            context,
            snapshot,
            phase=RunPhase.PLANNING,
            boundary=RunStableBoundary.PLAN_COMMITTED,
            plan_id=other_plan["id"],
        )

    own_plan = store.create_plan(
        run_id=context.run_id,
        session_id=context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        spec={
            "goal": "own run",
            "steps": [
                {
                    "id": "step-1",
                    "goal": "query",
                    "depends_on": [],
                    "allowed_tools": ["list_exams"],
                    "expected_tools": ["list_exams"],
                    "completion_conditions": ["tool_event"],
                }
            ],
        },
        max_iterations=2,
        context=context,
    )
    persisted = _cas(
        store,
        context,
        snapshot,
        phase=RunPhase.PLANNING,
        boundary=RunStableBoundary.PLAN_COMMITTED,
        plan_id=own_plan["id"],
    )
    assert persisted.plan_id == own_plan["id"]
    assert "goal" not in persisted.to_dict()["references"]


def test_legal_phase_chain_cas_is_monotonic_and_terminal_is_irreversible(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context()
    _queued(store, context)
    snapshot = _create_prelease(store, context)
    _claim(store, context)

    transitions = (
        (RunPhase.PLANNING, RunStableBoundary.PLAN_COMMITTED, 0),
        (RunPhase.MODEL, RunStableBoundary.MODEL_ATTEMPT_STARTED, 1),
        (RunPhase.TOOLS, RunStableBoundary.ASSISTANT_ENVELOPE_COMMITTED, 1),
        (RunPhase.VERIFYING, RunStableBoundary.TOOL_RESULT_COMMITTED, 1),
        (RunPhase.FINALIZING, RunStableBoundary.VERIFICATION_COMMITTED, 1),
        (RunPhase.TERMINAL, RunStableBoundary.TERMINAL, 1),
    )
    for phase, boundary, model_attempt in transitions:
        snapshot = _cas(
            store,
            context,
            snapshot,
            phase=phase,
            boundary=boundary,
            model_attempt=model_attempt,
        )

    assert snapshot.phase is RunPhase.TERMINAL
    assert snapshot.event_sequence == len(transitions)
    assert snapshot.revision == len(transitions) + 1
    with pytest.raises(RunJournalTransitionError) as rejected:
        _cas(
            store,
            context,
            snapshot,
            phase=RunPhase.MODEL,
            boundary=RunStableBoundary.MODEL_ATTEMPT_STARTED,
            model_attempt=2,
        )
    assert rejected.value.code == "RUN_JOURNAL_PHASE_REJECTED"


def test_verification_can_start_next_loop_without_regressing_cursor(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context()
    _queued(store, context)
    snapshot = _create_prelease(store, context)
    _claim(store, context)
    for phase, boundary in (
        (RunPhase.PLANNING, RunStableBoundary.PLAN_COMMITTED),
        (RunPhase.MODEL, RunStableBoundary.MODEL_ATTEMPT_STARTED),
        (RunPhase.TOOLS, RunStableBoundary.ASSISTANT_ENVELOPE_COMMITTED),
        (RunPhase.VERIFYING, RunStableBoundary.VERIFICATION_COMMITTED),
    ):
        snapshot = _cas(
            store,
            context,
            snapshot,
            phase=phase,
            boundary=boundary,
            model_attempt=1,
        )

    resumed = _cas(
        store,
        context,
        snapshot,
        phase=RunPhase.MODEL,
        boundary=RunStableBoundary.MODEL_ATTEMPT_STARTED,
        loop_cursor=1,
        model_attempt=2,
    )
    assert resumed.phase is RunPhase.MODEL
    assert resumed.loop_cursor == 1 and resumed.model_attempt == 2


def test_jump_duplicate_and_cursor_regression_are_structured_failures(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context()
    _queued(store, context)
    snapshot = _create_prelease(store, context)
    _claim(store, context)

    with pytest.raises(RunJournalTransitionError) as jump:
        _cas(
            store,
            context,
            snapshot,
            phase=RunPhase.TOOLS,
            boundary=RunStableBoundary.ASSISTANT_ENVELOPE_COMMITTED,
        )
    assert jump.value.to_dict()["code"] == "RUN_JOURNAL_PHASE_REJECTED"

    planning = _cas(
        store,
        context,
        snapshot,
        phase=RunPhase.PLANNING,
        boundary=RunStableBoundary.PLAN_COMMITTED,
    )
    with pytest.raises(RunJournalConflict) as duplicate:
        store.compare_and_set_run_journal(
            context,
            expected_revision=planning.revision,
            expected_phase=planning.phase,
            phase=planning.phase,
            expected_loop_cursor=planning.loop_cursor,
            loop_cursor=planning.loop_cursor,
            expected_model_attempt=planning.model_attempt,
            model_attempt=planning.model_attempt,
            expected_event_sequence=planning.event_sequence,
            event_sequence=planning.event_sequence,
            expected_fencing_token=planning.fencing_token,
            stable_boundary=planning.stable_boundary,
            budget_snapshot=planning.budget_snapshot,
        )
    assert duplicate.value.code == "RUN_JOURNAL_CAS_CONFLICT"

    model = _cas(
        store,
        context,
        planning,
        phase=RunPhase.MODEL,
        boundary=RunStableBoundary.MODEL_ATTEMPT_STARTED,
        loop_cursor=2,
        model_attempt=2,
    )
    with pytest.raises(RunJournalCursorError) as regression:
        _cas(
            store,
            context,
            model,
            phase=RunPhase.MODEL,
            boundary=RunStableBoundary.MODEL_ATTEMPT_STARTED,
            loop_cursor=1,
            model_attempt=2,
        )
    assert regression.value.code == "RUN_JOURNAL_CURSOR_REJECTED"


@pytest.mark.parametrize(
    ("phase", "boundary", "cancel_first"),
    [
        (RunPhase.CANCELLED, RunStableBoundary.CANCELLED, True),
        (RunPhase.FAILED, RunStableBoundary.FAILED, False),
    ],
)
def test_cancelled_and_failed_are_explicit_irreversible_branches(
    tmp_path,
    phase,
    boundary,
    cancel_first,
):
    store = StateStore(tmp_path / f"{phase.value}.db")
    context = _context(run_id=f"run-{phase.value}")
    _queued(store, context)
    snapshot = _create_prelease(store, context)
    _claim(store, context)
    if cancel_first:
        assert store.cancel_run(
            context.run_id,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
        )
    terminal = _cas(
        store,
        context,
        snapshot,
        phase=phase,
        boundary=boundary,
    )
    assert terminal.phase is phase
    with pytest.raises((RunJournalTransitionError, RunJournalFencingError)):
        _cas(
            store,
            context,
            terminal,
            phase=RunPhase.PLANNING,
            boundary=RunStableBoundary.PLAN_COMMITTED,
        )


def test_two_concurrent_cas_writers_have_exactly_one_winner(tmp_path):
    path = tmp_path / "state.db"
    first = StateStore(path)
    context = _context()
    _queued(first, context)
    snapshot = _create_prelease(first, context)
    claim = _claim(first, context, owner="shared-writer")

    second = StateStore(path)
    second_context = _context()
    second_context.bind_runtime_control(
        lease_owner="shared-writer",
        fencing_token=int(claim["fencing_token"]),
        control_check=lambda _boundary: None,
    )
    barrier = threading.Barrier(2)

    def update(store, run_context):
        barrier.wait(timeout=2)
        return _cas(
            store,
            run_context,
            snapshot,
            phase=RunPhase.PLANNING,
            boundary=RunStableBoundary.PLAN_COMMITTED,
        )

    successes = []
    failures = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(update, first, context),
            pool.submit(update, second, second_context),
        ]
        for future in futures:
            try:
                successes.append(future.result(timeout=3))
            except RunJournalConflict as error:
                failures.append(error)

    assert len(successes) == len(failures) == 1
    assert failures[0].code == "RUN_JOURNAL_CAS_CONFLICT"
    persisted = first.get_run_journal_snapshot(
        context.run_id,
        session_id=context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
    )
    assert persisted.revision == 2 and persisted.phase is RunPhase.PLANNING


def test_expired_worker_fence_is_rejected_and_new_owner_takes_over(tmp_path):
    clock = MutableClock(datetime(2026, 8, 22, tzinfo=UTC))
    path = tmp_path / "state.db"
    first = StateStore(path, clock=clock)
    stale_context = _context()
    _queued(first, stale_context)
    first_claim = _claim(first, stale_context, owner="worker-old", lease_seconds=5)
    snapshot = first.create_run_journal(
        stale_context,
        tool_manifest_hash=MANIFEST_HASH,
        frozen_provider_route={"provider": "mock"},
        budget_snapshot={},
    )
    assert snapshot.fencing_token == first_claim["fencing_token"]

    clock.advance(6)
    second = StateStore(path, clock=clock)
    current_context = _context()
    second_claim = _claim(second, current_context, owner="worker-new", lease_seconds=5)
    assert second_claim["fencing_token"] > first_claim["fencing_token"]

    with pytest.raises(RunJournalFencingError) as stale:
        _cas(
            first,
            stale_context,
            snapshot,
            phase=RunPhase.PLANNING,
            boundary=RunStableBoundary.PLAN_COMMITTED,
        )
    assert stale.value.code == "RUN_JOURNAL_FENCE_REJECTED"

    taken_over = _cas(
        second,
        current_context,
        snapshot,
        phase=RunPhase.PLANNING,
        boundary=RunStableBoundary.PLAN_COMMITTED,
    )
    assert taken_over.writer_id == "worker-new"
    assert taken_over.fencing_token == second_claim["fencing_token"]


def test_old_database_migration_is_idempotent_and_recovers_missing_marker(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
                title TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                sequence INTEGER NOT NULL, role TEXT NOT NULL, content TEXT,
                name TEXT, tool_call_id TEXT, tool_calls_json TEXT,
                created_at TEXT NOT NULL, UNIQUE(session_id, sequence)
            );
            INSERT INTO sessions VALUES ('legacy', 'actor', 'tenant', 'old', 't0', 't0');
            INSERT INTO messages(session_id, sequence, role, content, created_at)
            VALUES ('legacy', 0, 'user', 'preserve-me', 't0');
            """
        )

    migrated = StateStore(path)
    assert migrated.get_messages("legacy") == [{"role": "user", "content": "preserve-me"}]
    interrupted_context = _context(run_id="interrupted-run", session_id="interrupted-session")
    _queued(migrated, interrupted_context)
    interrupted_snapshot = _create_prelease(migrated, interrupted_context)
    with migrated.connect() as connection:
        connection.execute(
            "DELETE FROM state_schema_migrations WHERE version=?",
            (RUN_JOURNAL_MIGRATION,),
        )
        connection.execute("PRAGMA user_version = 8")

    reopened = StateStore(path)
    StateStore(path)
    with reopened.connect() as connection:
        marker_count = connection.execute(
            "SELECT COUNT(*) FROM state_schema_migrations WHERE version=?",
            (RUN_JOURNAL_MIGRATION,),
        ).fetchone()[0]
        recovery_marker_count = connection.execute(
            "SELECT COUNT(*) FROM state_schema_migrations WHERE version=?",
            ("012_r2_recovery",),
        ).fetchone()[0]
        table_count = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='run_journals'"
        ).fetchone()[0]
        run_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(runs)")
        }
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert marker_count == table_count == 1
    assert recovery_marker_count == 1
    assert "stream_event_sequence" in run_columns
    assert user_version == 12
    assert reopened.get_messages("legacy") == [{"role": "user", "content": "preserve-me"}]
    assert reopened.get_run_journal_snapshot(
        interrupted_context.run_id,
        session_id=interrupted_context.session_id,
        actor_id=interrupted_context.actor_id,
        tenant_id=interrupted_context.tenant_id,
    ) == interrupted_snapshot


def test_newer_database_schema_is_never_downgraded(tmp_path):
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 13")

    with pytest.raises(StateSchemaVersionError, match="newer than supported"):
        StateStore(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 13
    with pytest.raises(StateSchemaVersionError, match="newer than supported"):
        StateStore(path, read_only=True)

    marker_path = tmp_path / "future-marker.db"
    with sqlite3.connect(marker_path) as connection:
        connection.executescript(
            """
            CREATE TABLE state_schema_migrations(
                version TEXT PRIMARY KEY, applied_at TEXT NOT NULL
            );
            INSERT INTO state_schema_migrations VALUES ('013_future', 't0');
            """
        )
    with pytest.raises(StateSchemaVersionError, match="013_future"):
        StateStore(marker_path)


@pytest.mark.parametrize(
    ("column", "value", "error_type"),
    [
        ("phase", "mystery", RunJournalCorrupt),
        ("stable_boundary", "mystery", RunJournalCorrupt),
        ("provider_route_json", "{broken", RunJournalCorrupt),
        ("schema_version", 99, RunJournalSchemaVersionError),
    ],
)
def test_corrupt_or_unknown_journal_state_never_silently_defaults(
    tmp_path,
    column,
    value,
    error_type,
):
    store = StateStore(tmp_path / f"{column}.db")
    context = _context(run_id=f"run-{column}")
    _queued(store, context)
    _create_prelease(store, context)
    with store.connect() as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            f"UPDATE run_journals SET {column}=? WHERE run_id=?",
            (value, context.run_id),
        )

    with pytest.raises(error_type):
        store.get_run_journal_snapshot(
            context.run_id,
            session_id=context.session_id,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
        )
