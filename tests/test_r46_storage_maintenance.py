from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from edu_agent.api import DemoTokenAuth, EduAgentApi, Principal
from edu_agent.delegation.persistence import DelegationState
from edu_agent.engine.base import Engine, EngineResponse
from edu_agent.runtime import AppConfig
from edu_agent.runtime.artifacts import ArtifactStore
from edu_agent.runtime.config import MemoryConfig, StorageConfig
from edu_agent.runtime.models import RunContext
from edu_agent.service import EduAgentService
from edu_agent.state import (
    BackupRefusedError,
    BackupValidationError,
    RetentionError,
    RetentionPolicy,
    StateMaintenance,
    StateIntegrityError,
    StateStorageError,
    StateStore,
    normalize_state_storage_error,
)
from edu_agent.runtime.transactions import initialize_transaction_schema


class MutableClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class CountingEngine(Engine):
    name = "counting"

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools):
        del messages, tools
        self.calls += 1
        return EngineResponse(content="done")


def _service(tmp_path: Path, *, clock: MutableClock | None = None):
    engine = CountingEngine()
    state = StateStore(tmp_path / "state.db", clock=clock)
    config = AppConfig(
        storage=StorageConfig(
            state_path=str(state.path),
            artifact_path=str(tmp_path / "artifacts"),
        ),
        memory=MemoryConfig(enabled=False),
    )
    service = EduAgentService(engine, config=config, state_store=state)
    return service, engine


def _terminal_session(
    tmp_path: Path,
    *,
    clock: MutableClock,
    actor_id: str = "teacher",
    session_id: str = "session-old",
):
    service, engine = _service(tmp_path, clock=clock)
    result = service.chat(
        "finish",
        actor_id=actor_id,
        role="teacher",
        session_id=session_id,
    )
    return service, engine, result


def _rewrite_age(store: StateStore, *, session_id: str, run_id: str, value: str) -> None:
    with store.connect() as connection:
        connection.execute("UPDATE sessions SET created_at=?, updated_at=? WHERE id=?", (value, value, session_id))
        connection.execute(
            "UPDATE runs SET queued_at=?, started_at=?, heartbeat_at=?, finished_at=? WHERE id=?",
            (value, value, value, value, run_id),
        )
        connection.execute("UPDATE messages SET created_at=? WHERE session_id=?", (value, session_id))
        connection.execute("UPDATE run_journals SET created_at=?, updated_at=? WHERE run_id=?", (value, value, run_id))
        connection.execute("UPDATE turn_finalizers SET created_at=?, updated_at=?, terminal_at=?, cleanup_at=? WHERE run_id=?", (value, value, value, value, run_id))
        connection.execute("UPDATE run_budget_ledgers SET created_at=?, updated_at=?, finalized_at=? WHERE root_run_id=?", (value, value, value, run_id))


def _artifact_for_run(
    service: EduAgentService,
    *,
    run_id: str,
    session_id: str,
    actor_id: str = "teacher",
) -> str:
    context = RunContext.create(
        session_id=session_id,
        actor_id=actor_id,
        tenant_id="default",
        role="teacher",
        run_id=run_id,
    )
    artifact = ArtifactStore(service.config.artifact_path, service.state_store).write_text(
        json.dumps({"large": "payload"}),
        context=context,
        kind="test",
    )
    return artifact.id


def test_online_backup_uses_committed_snapshot_and_restores_references(tmp_path):
    service, engine = _service(tmp_path / "live")
    result = service.chat("one", actor_id="teacher", role="teacher", session_id="s1")
    artifact_id = _artifact_for_run(service, run_id=result.run_id, session_id="s1")
    now = service.state_store.now_iso()
    with service.state_store.connect() as connection:
        connection.execute(
            """
            INSERT INTO tool_operation_refs(
                operation_id, idempotency_key, payload_hash, tool_name,
                tenant_id, actor_id, session_id, run_id, status, updated_at
            ) VALUES ('backup-operation', 'backup-key', 'backup-hash', 'write',
                      'default', 'teacher', 's1', ?, 'committed', ?)
            """,
            (result.run_id, now),
        )
        connection.execute(
            """
            INSERT INTO context_checkpoints(
                id, session_id, summary, first_sequence, last_sequence,
                source_messages, estimated_tokens_before, created_at,
                schema_version, actor_id, tenant_id, created_run_id,
                source_sequences_json, source_hashes_json, source_sha256,
                strategy_version, estimator_version, summary_sha256,
                estimated_tokens_after, preserved_items_json,
                artifact_refs_json, citation_refs_json, operation_refs_json
            ) VALUES (
                'backup-checkpoint', 's1', 'summary', 0, 0, 1, 1, ?, 2,
                'teacher', 'default', ?, '[0]', '[]', 'source-hash',
                'policy', 'estimator', 'summary-hash', 1, '[]', ?, '[]',
                '[{"operation_id":"backup-operation"}]'
            )
            """,
            (now, result.run_id, json.dumps([{"artifact_id": artifact_id}])),
        )
        connection.execute(
            """
            UPDATE run_journals
            SET operation_id='backup-operation', artifact_id=?,
                context_checkpoint_id='backup-checkpoint'
            WHERE run_id=?
            """,
            (artifact_id, result.run_id),
        )
    with service.state_store.connect() as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE sessions SET title='uncommitted' WHERE id='s1'")
        backup = StateMaintenance(
            service.state_store,
            service.config.artifact_path,
        ).backup(tmp_path / "backup")
        writer.rollback()

    manifest = json.loads((tmp_path / "backup" / "manifest.json").read_text())
    serialized = json.dumps(manifest, sort_keys=True)
    assert backup.artifact_count == 1
    assert "uncommitted" not in serialized
    assert "credential" not in serialized.lower()
    assert not any(item["file"].endswith(("-wal", "-shm")) for item in manifest["files"])
    restored = StateMaintenance.restore(tmp_path / "backup", tmp_path / "restored")
    restored_store = StateStore(restored.state_path, read_only=True)
    with restored_store.connect() as connection:
        assert connection.execute("SELECT title FROM sessions WHERE id='s1'").fetchone()[0] == "one"
        assert connection.execute("SELECT COUNT(*) FROM run_journals").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM tool_operation_refs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM context_checkpoints").fetchone()[0] == 1
        journal = connection.execute(
            """
            SELECT operation_id, artifact_id, context_checkpoint_id
            FROM run_journals WHERE run_id=?
            """,
            (result.run_id,),
        ).fetchone()
        assert tuple(journal) == (
            "backup-operation",
            artifact_id,
            "backup-checkpoint",
        )
        row = connection.execute("SELECT path FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        assert Path(row[0]).is_relative_to(tmp_path / "restored" / "artifacts")
    assert engine.calls == 1


def test_backup_preserves_pending_gc_and_artifact_ids_cannot_control_restore_paths(
    tmp_path,
):
    service, _ = _service(tmp_path / "live")
    result = service.chat("one", actor_id="teacher", role="teacher", session_id="s1")
    active_id = _artifact_for_run(service, run_id=result.run_id, session_id="s1")
    pending_id = _artifact_for_run(service, run_id=result.run_id, session_id="s1")
    unsafe_id = "../outside"
    with service.state_store.connect() as connection:
        pending_path = Path(
            connection.execute(
                "SELECT path FROM artifacts WHERE id=?", (pending_id,)
            ).fetchone()[0]
        )
        connection.execute(
            "UPDATE artifacts SET id=? WHERE id=?",
            (unsafe_id, active_id),
        )
        connection.execute(
            "UPDATE artifacts SET gc_pending_at=? WHERE id=?",
            (service.state_store.now_iso(), pending_id),
        )

    backup = tmp_path / "backup"
    StateMaintenance(service.state_store, service.config.artifact_path).backup(backup)
    assert pending_path.is_file()
    with service.state_store.connect() as connection:
        assert connection.execute(
            "SELECT gc_pending_at FROM artifacts WHERE id=?", (pending_id,)
        ).fetchone()[0]
    manifest = json.loads((backup / "manifest.json").read_text())
    artifact_entry = next(item for item in manifest["files"] if item["role"] == "artifact")
    assert artifact_entry["artifact_id"] == unsafe_id
    assert artifact_entry["file"] == "artifacts/00000000.blob"

    restored = StateMaintenance.restore(backup, tmp_path / "restored")
    restored_store = StateStore(restored.state_path)
    with restored_store.connect() as connection:
        active_path = Path(
            connection.execute(
                "SELECT path FROM artifacts WHERE id=?", (unsafe_id,)
            ).fetchone()[0]
        )
        restored_pending_path = Path(
            connection.execute(
                "SELECT path FROM artifacts WHERE id=?", (pending_id,)
            ).fetchone()[0]
        )
    assert active_path == tmp_path / "restored" / "artifacts" / "00000000.blob"
    assert restored_pending_path.is_relative_to(tmp_path / "restored" / "artifacts")
    assert not restored_pending_path.exists()
    assert not (tmp_path / "outside").exists()
    maintenance = StateMaintenance(restored_store, restored.artifact_root)
    assert maintenance._resume_pending_artifact_gc(limit=1) == 1
    assert pending_path.is_file()


def test_backup_rejects_cross_session_operation_reference(tmp_path):
    service, _ = _service(tmp_path / "live")
    first = service.chat("one", actor_id="teacher", role="teacher", session_id="s1")
    second = service.chat("two", actor_id="teacher", role="teacher", session_id="s2")
    now = service.state_store.now_iso()
    with service.state_store.connect() as connection:
        connection.execute(
            """
            INSERT INTO tool_operation_refs(
                operation_id, idempotency_key, payload_hash, tool_name,
                tenant_id, actor_id, session_id, run_id, status, updated_at
            ) VALUES ('op-s1', 'key-s1', 'hash-s1', 'write',
                      'default', 'teacher', 's1', ?, 'committed', ?)
            """,
            (first.run_id, now),
        )
        connection.execute(
            """
            INSERT INTO context_checkpoints(
                id, session_id, summary, first_sequence, last_sequence,
                source_messages, estimated_tokens_before, created_at,
                schema_version, actor_id, tenant_id, created_run_id,
                source_sequences_json, source_hashes_json, source_sha256,
                strategy_version, estimator_version, summary_sha256,
                estimated_tokens_after, preserved_items_json,
                artifact_refs_json, citation_refs_json, operation_refs_json
            ) VALUES (
                'cp-s2', 's2', 'summary', 0, 0, 1, 1, ?, 2,
                'teacher', 'default', ?, '[0]', '[]', 'source-hash',
                'policy', 'estimator', 'summary-hash', 1,
                '[]', '[]', '[]', '[{"operation_id":"op-s1"}]'
            )
            """,
            (now, second.run_id),
        )
    target = tmp_path / "backup"
    with pytest.raises(StateIntegrityError, match="reference validation"):
        StateMaintenance(service.state_store, service.config.artifact_path).backup(target)
    assert not target.exists()


def test_backup_refuses_unknown_target_and_restore_refuses_active_or_nonempty_target(tmp_path):
    service, _ = _service(tmp_path / "live")
    target = tmp_path / "backup"
    target.write_text("unknown user file")
    maintenance = StateMaintenance(service.state_store, service.config.artifact_path)
    with pytest.raises(BackupRefusedError, match="already exists"):
        maintenance.backup(target)
    target.unlink()
    maintenance.backup(target)
    active = tmp_path / "active"
    active.mkdir()
    StateStore(active / "state.db")
    with pytest.raises(Exception, match="not empty"):
        StateMaintenance.restore(target, active)


def test_corrupt_backup_and_manifest_are_rejected_before_restore(tmp_path):
    service, _ = _service(tmp_path / "live")
    maintenance = StateMaintenance(service.state_store, service.config.artifact_path)
    backup = tmp_path / "backup"
    maintenance.backup(backup)
    database = backup / "state.db"
    payload = bytearray(database.read_bytes())
    payload[-1] ^= 0xFF
    database.write_bytes(payload)
    with pytest.raises(BackupValidationError, match="checksum"):
        StateMaintenance.restore(backup, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_restore_runs_idempotent_migration_from_v14_snapshot(tmp_path):
    service, _ = _service(tmp_path / "live")
    maintenance = StateMaintenance(service.state_store, service.config.artifact_path)
    backup = tmp_path / "backup"
    maintenance.backup(backup)
    database = backup / "state.db"
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE retention_holds")
        connection.execute("DROP INDEX idx_artifacts_gc_pending")
        connection.execute("ALTER TABLE artifacts DROP COLUMN gc_pending_at")
        connection.execute("DELETE FROM state_schema_migrations WHERE version='015_storage_maintenance'")
        connection.execute("DELETE FROM state_schema_migrations WHERE version='016_run_replay_scope'")
        connection.execute("ALTER TABLE runs DROP COLUMN replay_scope")
        connection.execute("PRAGMA user_version=14")
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    digest = __import__("hashlib").sha256(database.read_bytes()).hexdigest()
    for item in manifest["files"]:
        if item["role"] == "state_database":
            item["sha256"] = digest
            item["size_bytes"] = database.stat().st_size
    manifest["schema_version"] = 14
    manifest["integrity"]["schema_version"] = 14
    manifest["migration_ids"] = [
        item
        for item in manifest["migration_ids"]
        if item not in {"015_storage_maintenance", "016_run_replay_scope"}
    ]
    from edu_agent.state.maintenance import _manifest_hash
    manifest["manifest_sha256"] = _manifest_hash(manifest)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))

    restored = StateMaintenance.restore(backup, tmp_path / "restored")
    assert restored.source_schema_version == 14
    assert restored.restored_schema_version == 16
    with StateStore(restored.state_path, read_only=True).connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM state_schema_migrations WHERE version='015_storage_maintenance'").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM state_schema_migrations WHERE version='016_run_replay_scope'").fetchone()[0] == 1


@pytest.mark.parametrize(
    "fault_point",
    [
        "after_015_schema_before_marker",
        "after_015_marker_before_user_version",
        "after_016_schema_before_marker",
        "after_016_marker_before_user_version",
    ],
)
def test_migration_interruption_rolls_back_and_restart_applies_once(tmp_path, fault_point):
    path = tmp_path / "state.db"
    StateStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE retention_holds")
        connection.execute("DROP INDEX idx_artifacts_gc_pending")
        connection.execute("ALTER TABLE artifacts DROP COLUMN gc_pending_at")
        connection.execute("DELETE FROM state_schema_migrations WHERE version='015_storage_maintenance'")
        connection.execute("DELETE FROM state_schema_migrations WHERE version='016_run_replay_scope'")
        connection.execute("ALTER TABLE runs DROP COLUMN replay_scope")
        connection.execute("PRAGMA user_version=14")

    def interrupt(point: str) -> None:
        if point == fault_point:
            raise RuntimeError("migration interrupted")

    with pytest.raises(RuntimeError, match="migration interrupted"):
        StateStore(path, migration_fault_injector=interrupt)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 14
        assert connection.execute("SELECT COUNT(*) FROM state_schema_migrations WHERE version='015_storage_maintenance'").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM state_schema_migrations WHERE version='016_run_replay_scope'").fetchone()[0] == 0
    recovered = StateStore(path)
    assert recovered.migration_ready()


def test_gc_dry_run_is_audited_and_apply_is_bounded(tmp_path):
    now = datetime(2026, 8, 24, tzinfo=UTC)
    clock = MutableClock(now)
    service, _, result = _terminal_session(tmp_path / "live", clock=clock)
    old = (now - timedelta(days=60)).isoformat()
    _rewrite_age(service.state_store, session_id="session-old", run_id=result.run_id, value=old)
    policy = RetentionPolicy(terminal_age_seconds=30 * 86400, artifact_age_seconds=30 * 86400, batch_size=1)
    maintenance = StateMaintenance(service.state_store, service.config.artifact_path)

    dry_run = maintenance.gc(policy)
    assert dry_run.dry_run is True
    assert dry_run.eligible_sessions == ["session-old"]
    assert service.state_store.count("sessions") == 1
    applied = maintenance.gc(policy, dry_run=False)
    assert applied.deleted_sessions == 1
    assert applied.deleted_runs == 1
    assert service.state_store.count("sessions") == 0
    with service.state_store.connect() as connection:
        decisions = [row[0] for row in connection.execute("SELECT decision FROM audit_events WHERE action='state.gc' ORDER BY id")]
    assert decisions == ["dry_run", "applied"]


def test_gc_preserves_manual_review_pending_outbox_active_checkpoint_and_hold(tmp_path):
    now = datetime(2026, 8, 24, tzinfo=UTC)
    clock = MutableClock(now)
    policy = RetentionPolicy(terminal_age_seconds=86400, artifact_age_seconds=86400, batch_size=10)
    blockers = {
        "manual": "manual_review_operation",
        "outbox": "pending_outbox",
        "recent-operation": "operation_retained",
        "checkpoint": "active_checkpoint",
        "hold": "retention_hold",
    }
    root = tmp_path / "live"
    operation_path = tmp_path / "operations.db"
    operation_connection = sqlite3.connect(operation_path)
    operation_connection.row_factory = sqlite3.Row
    initialize_transaction_schema(operation_connection)
    operation_connection.commit()
    services = {}
    for index, session_id in enumerate(blockers):
        service, _, result = _terminal_session(root, clock=clock, actor_id="teacher", session_id=session_id)
        old = (now - timedelta(days=10)).isoformat()
        _rewrite_age(service.state_store, session_id=session_id, run_id=result.run_id, value=old)
        services[session_id] = (service, result)
        if session_id in {"manual", "outbox", "recent-operation"}:
            operation_id = f"op-{session_id}"
            status = "manual_review" if session_id == "manual" else "committed"
            operation_updated_at = now.isoformat() if session_id == "recent-operation" else old
            with service.state_store.connect() as connection:
                connection.execute("INSERT INTO tool_operation_refs VALUES (?, ?, ?, 'write', 'default', 'teacher', ?, ?, NULL, NULL, ?, ?)", (operation_id, f"key-{index}", f"hash-{index}", session_id, result.run_id, status, operation_updated_at))
            operation_connection.execute(
                """INSERT INTO tool_operations(id,idempotency_key,payload_hash,tool_name,tenant_id,actor_id,session_id,run_id,status,arguments_json,approval_scope,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'{}','scope',?,?)""",
                (operation_id, f"key-{index}", f"hash-{index}", "write", "default", "teacher", session_id, result.run_id, status, old, operation_updated_at),
            )
            if session_id == "outbox":
                operation_connection.execute("INSERT INTO tool_outbox(event_id,operation_id,event_type,payload_json,status,created_at) VALUES('event-outbox',?,'test','{}','pending',?)", (operation_id, old))
        elif session_id == "checkpoint":
            with service.state_store.connect() as connection:
                connection.execute("INSERT INTO context_checkpoints(id,session_id,summary,first_sequence,last_sequence,source_messages,estimated_tokens_before,created_at,schema_version,actor_id,tenant_id,created_run_id,source_sequences_json,source_hashes_json,source_sha256,strategy_version,estimator_version,summary_sha256,estimated_tokens_after,preserved_items_json,artifact_refs_json,citation_refs_json,operation_refs_json) VALUES('cp',?,'summary',0,0,1,1,?,2,'teacher','default',?,'[0]','[]','hash','policy','estimator','summaryhash',1,'[]','[]','[]','[]')", (session_id, old, result.run_id))
                connection.execute(
                    """
                    UPDATE messages SET active=0, compaction_id='cp'
                    WHERE id=(SELECT id FROM messages WHERE session_id=? ORDER BY id LIMIT 1)
                    """,
                    (session_id,),
                )
                connection.execute(
                    "UPDATE run_journals SET context_checkpoint_id='cp', phase='model' WHERE run_id=?",
                    (result.run_id,),
                )
        else:
            StateMaintenance(service.state_store, service.config.artifact_path).add_retention_hold(resource_type="session", resource_id=session_id, reason="audit", created_by="tester")
    operation_connection.commit()
    operation_connection.close()

    service = services["manual"][0]
    held_service, held_result = services["hold"]
    held_artifact = _artifact_for_run(
        held_service,
        run_id=held_result.run_id,
        session_id="hold",
    )
    with held_service.state_store.connect() as connection:
        held_artifact_path = Path(
            connection.execute(
                "SELECT path FROM artifacts WHERE id=?",
                (held_artifact,),
            ).fetchone()[0]
        )
        connection.execute(
            "UPDATE artifacts SET created_at=? WHERE id=?",
            (old, held_artifact),
        )
    report = StateMaintenance(service.state_store, service.config.artifact_path).gc(policy, operation_db_path=operation_path)
    for session_id, expected in blockers.items():
        assert expected in report.blocked_sessions[session_id]
    assert held_artifact not in report.eligible_artifacts
    assert held_artifact_path.is_file()


def test_gc_fails_closed_when_terminal_truth_is_missing(tmp_path):
    now = datetime(2026, 8, 24, tzinfo=UTC)
    clock = MutableClock(now)
    service, _, result = _terminal_session(tmp_path / "live", clock=clock)
    old = (now - timedelta(days=60)).isoformat()
    _rewrite_age(
        service.state_store,
        session_id="session-old",
        run_id=result.run_id,
        value=old,
    )
    with service.state_store.connect() as connection:
        connection.execute("DELETE FROM turn_finalizers WHERE run_id=?", (result.run_id,))
        connection.execute("DELETE FROM run_journals WHERE run_id=?", (result.run_id,))
        connection.execute(
            "DELETE FROM run_budget_ledgers WHERE root_run_id=?",
            (result.run_id,),
        )

    report = StateMaintenance(
        service.state_store,
        service.config.artifact_path,
    ).gc(
        RetentionPolicy(
            terminal_age_seconds=30 * 86400,
            artifact_age_seconds=30 * 86400,
            batch_size=10,
        )
    )
    assert set(report.blocked_sessions["session-old"]) >= {
        "finalizer_truth_missing",
        "journal_truth_missing",
        "budget_truth_missing",
    }
    assert service.state_store.count("sessions") == 1


def test_gc_never_deletes_unknown_artifact_and_resumes_two_phase_cleanup(tmp_path, monkeypatch):
    now = datetime(2026, 8, 24, tzinfo=UTC)
    clock = MutableClock(now)
    service, _, result = _terminal_session(tmp_path / "live", clock=clock)
    artifact_id = _artifact_for_run(service, run_id=result.run_id, session_id="session-old")
    old = (now - timedelta(days=60)).isoformat()
    _rewrite_age(service.state_store, session_id="session-old", run_id=result.run_id, value=old)
    unknown = Path(service.config.artifact_path) / "unknown-user-file.txt"
    unknown.write_text("keep")
    with service.state_store.connect() as connection:
        connection.execute("UPDATE artifacts SET created_at=? WHERE id=?", (old, artifact_id))
    maintenance = StateMaintenance(service.state_store, service.config.artifact_path)
    policy = RetentionPolicy(terminal_age_seconds=86400, artifact_age_seconds=86400, batch_size=10)
    original = maintenance._resume_pending_artifact_gc
    calls = 0

    def interrupt(*, limit=1000):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("write interrupted after metadata commit")
        return original(limit=limit)

    monkeypatch.setattr(maintenance, "_resume_pending_artifact_gc", interrupt)
    with pytest.raises(RuntimeError, match="write interrupted"):
        maintenance.gc(policy, dry_run=False)
    assert unknown.read_text() == "keep"
    with service.state_store.connect() as connection:
        assert connection.execute("SELECT gc_pending_at FROM artifacts WHERE id=?", (artifact_id,)).fetchone()[0]
    resumed = StateMaintenance(service.state_store, service.config.artifact_path).gc(policy, dry_run=False)
    assert resumed.resumed_artifact_deletions == 1
    assert unknown.read_text() == "keep"


def test_pending_artifact_gc_rechecks_references_before_unlink(tmp_path):
    now = datetime(2026, 8, 24, tzinfo=UTC)
    clock = MutableClock(now)
    service, _, result = _terminal_session(tmp_path / "live", clock=clock)
    artifact_id = _artifact_for_run(
        service,
        run_id=result.run_id,
        session_id="session-old",
    )
    with service.state_store.connect() as connection:
        artifact_path = Path(
            connection.execute(
                "SELECT path FROM artifacts WHERE id=?",
                (artifact_id,),
            ).fetchone()[0]
        )
        connection.execute(
            "UPDATE artifacts SET gc_pending_at=? WHERE id=?",
            (service.state_store.now_iso(), artifact_id),
        )
        connection.execute(
            "UPDATE run_journals SET artifact_id=? WHERE run_id=?",
            (artifact_id, result.run_id),
        )

    maintenance = StateMaintenance(service.state_store, service.config.artifact_path)
    with pytest.raises(RetentionError, match="became referenced"):
        maintenance._resume_pending_artifact_gc(limit=1)
    assert artifact_path.is_file()
    with service.state_store.connect() as connection:
        assert connection.execute(
            "SELECT gc_pending_at FROM artifacts WHERE id=?",
            (artifact_id,),
        ).fetchone()[0]


def test_pending_artifact_gc_preserves_late_exact_hold(tmp_path):
    now = datetime(2026, 8, 24, tzinfo=UTC)
    clock = MutableClock(now)
    service, _, result = _terminal_session(tmp_path / "live", clock=clock)
    artifact_id = _artifact_for_run(
        service,
        run_id=result.run_id,
        session_id="session-old",
    )
    maintenance = StateMaintenance(service.state_store, service.config.artifact_path)
    with service.state_store.connect() as connection:
        artifact_path = Path(
            connection.execute(
                "SELECT path FROM artifacts WHERE id=?",
                (artifact_id,),
            ).fetchone()[0]
        )
        connection.execute(
            "UPDATE artifacts SET gc_pending_at=? WHERE id=?",
            (service.state_store.now_iso(), artifact_id),
        )
        connection.execute(
            """
            INSERT INTO retention_holds(
                id, resource_type, resource_id, reason,
                created_by, created_at, expires_at
            ) VALUES ('late-hold', 'artifact', ?, 'audit', 'tester', ?, NULL)
            """,
            (artifact_id, service.state_store.now_iso()),
        )

    assert maintenance._resume_pending_artifact_gc(limit=1) == 0
    assert artifact_path.is_file()
    with pytest.raises(RetentionError, match="already pending GC"):
        maintenance.add_retention_hold(
            resource_type="artifact",
            resource_id=artifact_id,
            reason="second hold",
            created_by="tester",
        )


def test_gc_covers_terminal_delegation_tree_and_child_retention(tmp_path):
    now = datetime(2026, 8, 24, tzinfo=UTC)
    clock = MutableClock(now)
    service, _, result = _terminal_session(tmp_path / "live", clock=clock)
    old = (now - timedelta(days=60)).isoformat()
    _rewrite_age(
        service.state_store,
        session_id="session-old",
        run_id=result.run_id,
        value=old,
    )
    DelegationState(service.state_store)
    child_run_id = "delegated-child"
    with service.state_store.connect() as connection:
        connection.execute(
            """
            INSERT INTO delegation_roots(
                root_run_id, actor_id, tenant_id, session_id, role,
                course_ids_json, budget_json, reserved_json, usage_json,
                created_at, updated_at
            ) VALUES (?, 'teacher', 'default', 'session-old', 'teacher',
                      '[]', '{}', '{}', '{}', ?, ?)
            """,
            (result.run_id, old, old),
        )
        connection.execute(
            """
            INSERT INTO delegation_runs(
                id, parent_run_id, root_run_id, session_id, actor_id,
                tenant_id, role, course_ids_json, depth, task_key, task_kind,
                task, task_json, input_json, status, model,
                allowed_tools_json, allowed_categories_json, can_delegate,
                budget_json, usage_json, created_at, finished_at
            ) VALUES (?, ?, ?, 'session-old', 'teacher', 'default', 'teacher',
                      '[]', 1, 'child', 'class_analysis', 'task', '{}', '{}',
                      'completed', 'mock', '[]', '[]', 0, '{}', '{}', ?, ?)
            """,
            (child_run_id, result.run_id, result.run_id, old, old),
        )
        connection.execute(
            """
            INSERT INTO tool_events(
                run_id, session_id, tool_name, arguments_json,
                outcome_json, duration_ms, created_at
            ) VALUES (?, 'session-old', 'list_exams', '{}',
                      '{"ok":true}', 1.0, ?)
            """,
            (child_run_id, old),
        )
        connection.execute(
            """
            INSERT INTO provider_events(
                run_id, provider, event, attempt, details_json, created_at
            ) VALUES (?, 'mock', 'success', 1, '{}', ?)
            """,
            (child_run_id, old),
        )

    policy = RetentionPolicy(
        terminal_age_seconds=30 * 86400,
        artifact_age_seconds=30 * 86400,
        batch_size=10,
    )
    maintenance = StateMaintenance(service.state_store, service.config.artifact_path)
    hold_id = maintenance.add_retention_hold(
        resource_type="run",
        resource_id=child_run_id,
        reason="child audit",
        created_by="tester",
    )
    held = maintenance.gc(policy)
    assert "retention_hold" in held.blocked_sessions["session-old"]
    assert maintenance.release_retention_hold(hold_id, released_by="tester")

    with service.state_store.connect() as connection:
        connection.execute(
            "UPDATE delegation_runs SET finished_at=? WHERE id=?",
            (now.isoformat(), child_run_id),
        )
    retained = maintenance.gc(policy)
    assert "delegation_not_terminal_or_retained" in retained.blocked_sessions[
        "session-old"
    ]

    with service.state_store.connect() as connection:
        connection.execute(
            "UPDATE delegation_runs SET finished_at=? WHERE id=?",
            (old, child_run_id),
        )
    applied = maintenance.gc(policy, dry_run=False)
    assert applied.deleted_sessions == 1
    assert applied.deleted_runs == 2
    with service.state_store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM delegation_runs WHERE id=?",
            (child_run_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM tool_events WHERE run_id=?",
            (child_run_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM provider_events WHERE run_id=?",
            (child_run_id,),
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("database or disk is full", "STATE_STORAGE_FULL"),
        ("attempt to write a readonly database", "STATE_STORAGE_READ_ONLY"),
    ],
)
def test_storage_write_faults_are_stable_and_do_not_call_model_or_finalize(
    tmp_path,
    monkeypatch,
    message,
    code,
):
    service, engine = _service(tmp_path / "live")
    classified = normalize_state_storage_error(sqlite3.OperationalError(message))

    def fail(*args, **kwargs):
        del args, kwargs
        raise classified

    monkeypatch.setattr(service.state_store, "ensure_session", fail)
    with pytest.raises(StateStorageError) as caught:
        service.chat("hello", actor_id="teacher", role="teacher")
    assert caught.value.error_code == code
    assert engine.calls == 0
    assert service.state_store.count("runs") == 0


def test_http_storage_fault_returns_stable_503_without_budget_or_final(tmp_path, monkeypatch):
    service, engine = _service(tmp_path / "live")
    failure = StateStorageError("STATE_STORAGE_FULL", "state storage capacity is exhausted")

    def fail(*args, **kwargs):
        del args, kwargs
        raise failure

    monkeypatch.setattr(service.state_store, "ensure_session", fail)
    api = EduAgentApi(
        service,
        authenticator=DemoTokenAuth(
            {"token": Principal("teacher", "default", "teacher")}
        ),
    )
    response = api.dispatch(
        "POST",
        "/v1/chat",
        headers={"Authorization": "Bearer token", "X-Request-ID": "storage-fault"},
        body=json.dumps({"message": "hello"}).encode(),
    )
    assert response.status == 503
    assert response.body["error"]["code"] == "STATE_STORAGE_FULL"
    assert response.body["error"]["retryable"] is True
    assert engine.calls == 0
    assert service.state_store.count("runs") == 0
    assert service.state_store.count("turn_finalizers") == 0
    assert service.state_store.count("run_budget_ledgers") == 0


def test_interrupted_final_write_resumes_without_partial_final_or_budget_refresh(
    tmp_path,
    monkeypatch,
):
    now = datetime(2026, 8, 24, tzinfo=UTC)
    clock = MutableClock(now)
    service, engine = _service(tmp_path / "live", clock=clock)
    original = service.state_store.commit_final_message

    def interrupt(*args, **kwargs):
        del args, kwargs
        raise StateStorageError(
            "STATE_STORAGE_UNAVAILABLE",
            "state storage is unavailable",
        )

    monkeypatch.setattr(service.state_store, "commit_final_message", interrupt)
    with pytest.raises(StateStorageError):
        service.chat(
            "hello",
            actor_id="teacher",
            role="teacher",
            session_id="resume-session",
            run_id="resume-run",
        )
    monkeypatch.setattr(service.state_store, "commit_final_message", original)
    with service.state_store.connect() as connection:
        finalizer = connection.execute(
            "SELECT cursor, final_message_id FROM turn_finalizers WHERE run_id='resume-run'"
        ).fetchone()
        assert tuple(finalizer) == (2, None)
        assert connection.execute(
            "SELECT COUNT(*) FROM messages WHERE run_id='resume-run' AND role='assistant'"
        ).fetchone()[0] == 0
        before = json.loads(
            connection.execute(
                "SELECT used_json FROM run_budget_ledgers WHERE root_run_id='resume-run'"
            ).fetchone()[0]
        )
    assert before["model_calls"] == 1
    assert engine.calls == 1

    clock.value += timedelta(minutes=2)
    resumed_engine = CountingEngine()
    resumed_state = StateStore(service.state_store.path, clock=clock)
    resumed_service = EduAgentService(
        resumed_engine,
        config=service.config,
        state_store=resumed_state,
    )
    result = resumed_service.resume_run(
        "resume-run",
        actor_id="teacher",
        tenant_id="default",
    )
    assert result.final_answer == "done"
    assert resumed_engine.calls == 0
    with resumed_state.connect() as connection:
        after = json.loads(
            connection.execute(
                "SELECT used_json FROM run_budget_ledgers WHERE root_run_id='resume-run'"
            ).fetchone()[0]
        )
        assert after["model_calls"] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM messages WHERE run_id='resume-run' AND role='assistant'"
        ).fetchone()[0] == 1


def test_real_sqlite_disk_full_and_read_only_faults_are_classified(tmp_path):
    full_store = StateStore(tmp_path / "full.db")
    with pytest.raises(StateStorageError) as full:
        with full_store.connect() as connection:
            current_pages = int(connection.execute("PRAGMA page_count").fetchone()[0])
            connection.execute(f"PRAGMA max_page_count={current_pages}")
            connection.execute("CREATE TABLE fill_disk(payload BLOB)")
            connection.execute("INSERT INTO fill_disk VALUES (zeroblob(1048576))")
    assert full.value.error_code == "STATE_STORAGE_FULL"

    read_only = StateStore(tmp_path / "readonly.db")
    readonly_store = StateStore(read_only.path, read_only=True)
    with pytest.raises(StateStorageError) as readonly:
        readonly_store.ensure_session(
            "session",
            actor_id="teacher",
            tenant_id="default",
        )
    assert readonly.value.error_code == "STATE_STORAGE_READ_ONLY"

    corrupt_path = tmp_path / "corrupt.db"
    corrupt_path.write_bytes(b"this is not a sqlite database")
    with pytest.raises(StateStorageError) as corrupt:
        StateStore(corrupt_path, read_only=True)
    assert corrupt.value.error_code == "STATE_STORAGE_CORRUPT"
    assert corrupt.value.retryable is False
