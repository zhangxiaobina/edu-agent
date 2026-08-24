from __future__ import annotations

import json
import sqlite3

import pytest

from edu_agent.runtime.artifacts import (
    ARTIFACT_REFERENCE_TYPE,
    ArtifactStore,
    ToolResultBudget,
)
from edu_agent.runtime.context_engine import CheckpointContextEngine
from edu_agent.runtime.models import RunContext
from edu_agent.state import (
    CHECKPOINT_MIGRATION,
    CHECKPOINT_SCHEMA_VERSION,
    ContextCheckpointConflict,
    ContextCheckpointValidationError,
    STATE_SCHEMA_VERSION,
    StateStore,
)


def _active_context(
    store: StateStore,
    *,
    session_id: str = "session-1",
    run_id: str = "run-1",
    actor_id: str = "teacher-1",
    tenant_id: str = "school-1",
) -> RunContext:
    context = RunContext.create(
        session_id=session_id,
        run_id=run_id,
        actor_id=actor_id,
        tenant_id=tenant_id,
        role="teacher",
        course_ids={1},
    )
    store.ensure_session(
        session_id,
        actor_id=actor_id,
        tenant_id=tenant_id,
        role="teacher",
        course_ids={1},
    )
    store.enqueue_run(context, request_text="checkpoint test")
    owner = f"owner:{run_id}"
    claim = store.acquire_session_lease(
        session_id=session_id,
        run_id=run_id,
        owner_id=owner,
        actor_id=actor_id,
        tenant_id=tenant_id,
        lease_seconds=60,
    )
    context.bind_runtime_control(
        lease_owner=owner,
        fencing_token=claim["fencing_token"],
        control_check=lambda boundary: store.assert_run_writable(
            context,
            boundary=boundary,
        ),
    )
    return context


def _tool_call(call_id: str = "call-1") -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "large_query", "arguments": "{}"},
            }
        ],
    }


def _engine(store: StateStore, artifacts: ArtifactStore | None = None):
    return CheckpointContextEngine(
        store,
        token_budget=256,
        trigger_ratio=0.5,
        keep_recent=2,
        summary_max_chars=4_000,
        artifact_store=artifacts,
        tool_result_inline_chars=160,
        tool_result_preview_chars=120,
    )


def test_artifact_first_compaction_uses_typed_ref_and_sensitive_safe_preview(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _active_context(store)
    tool_result = json.dumps(
        {
            "ok": True,
            "data": {
                "student_name": "Alice Example",
                "phone": "13800138000",
                "report_path": "/Users/private-user/report.json",
                "rows": ["x" * 2_000],
            },
            "error": None,
            "meta": {"receipt_id": "receipt-1"},
        },
        ensure_ascii=False,
    )
    store.append_messages(
        context.session_id,
        [
            {"role": "user", "content": "旧问题" + "甲" * 600},
            _tool_call(),
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "name": "large_query",
                "content": tool_result,
            },
            {"role": "assistant", "content": "旧回答" + "乙" * 600},
            {"role": "user", "content": "最近问题"},
            {"role": "assistant", "content": "最近回答"},
        ],
    )
    artifacts = ArtifactStore(tmp_path / "artifacts", store)
    engine = _engine(store, artifacts)

    result = engine.compact_if_needed(
        context.session_id,
        store.get_messages(context.session_id),
        context=context,
    )

    assert result.checkpoint_id and result.compacted_messages == 2
    active = store.get_messages(context.session_id)
    tool = next(message for message in active if message["role"] == "tool")
    payload = json.loads(tool["content"])
    reference = payload["data"]["artifact_ref"]
    assert reference["type"] == ARTIFACT_REFERENCE_TYPE
    assert reference["sha256"] == payload["data"]["sha256"]
    assert "artifact_path" not in payload["data"]
    assert reference["classification"] == "student_pii"
    assert "Alice Example" not in payload["data"]["preview"]
    assert "13800138000" not in payload["data"]["preview"]
    assert "/Users/private-user" not in payload["data"]["preview"]
    assert payload["meta"]["receipt_id"] == "receipt-1"

    artifact = store.get_artifact(
        reference["artifact_id"],
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
    )
    assert artifact is not None
    assert "Alice Example" in artifacts.read_text(reference["artifact_id"], context=context)
    checkpoint = store.latest_context_checkpoint(context.session_id, context=context)
    assert checkpoint["artifact_refs"][0]["artifact_id"] == reference["artifact_id"]
    assert checkpoint["schema_version"] == CHECKPOINT_SCHEMA_VERSION
    assert checkpoint["created_run_id"] == context.run_id
    assert checkpoint["source_sequences"] == [0, 3]
    assert checkpoint["estimated_tokens_before"] == result.estimated_tokens_before
    assert checkpoint["estimated_tokens_after"] == result.estimated_tokens_after
    assert checkpoint["estimated_tokens_after"] > 0


def test_artifact_only_compaction_is_reported_without_fake_checkpoint(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _active_context(store, session_id="artifact-only", run_id="artifact-only-run")
    store.append_messages(
        context.session_id,
        [
            _tool_call("artifact-only-call"),
            {
                "role": "tool",
                "tool_call_id": "artifact-only-call",
                "name": "large_query",
                "content": json.dumps(
                    {"ok": True, "data": {"rows": ["x" * 2_000]}, "meta": {}},
                ),
            },
        ],
    )
    artifacts = ArtifactStore(tmp_path / "artifacts", store)

    result = _engine(store, artifacts).compact_if_needed(
        context.session_id,
        store.get_messages(context.session_id),
        context=context,
        force=True,
        reason="provider_context_overflow",
    )

    assert result.decision == "artifact_only"
    assert result.compacted_messages == 0
    assert result.externalized_messages == 1
    assert result.checkpoint_id is None
    assert store.count("context_checkpoints") == 0
    assert store.has_context_overflow_artifact_externalization(
        context.run_id,
        session_id=context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
    )
    tool = next(message for message in store.get_messages(context.session_id) if message["role"] == "tool")
    assert json.loads(tool["content"])["data"]["artifact_ref"]["type"] == (
        ARTIFACT_REFERENCE_TYPE
    )


def test_checkpoint_preserves_constraints_plan_receipts_citations_and_unpaired_group(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _active_context(store)
    operation = {
        "id": "operation-1",
        "idempotency_key": "idem-1",
        "payload_hash": "a" * 64,
        "tool_name": "write_tool",
        "tenant_id": context.tenant_id,
        "actor_id": context.actor_id,
        "session_id": context.session_id,
        "run_id": context.run_id,
        "plan_step_id": "step-1",
        "tool_call_id": "write-call",
        "status": "committed",
        "updated_at": store.now_iso(),
    }
    store.upsert_tool_operation_ref(operation, context=context)
    plan = store.create_plan(
        run_id=context.run_id,
        session_id=context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        spec={
            "goal": "完成待办教学分析",
            "steps": [
                {
                    "id": "step-1",
                    "goal": "保留尚未完成步骤",
                    "depends_on": [],
                    "allowed_tools": ["write_tool"],
                    "expected_tools": ["write_tool"],
                    "completion_conditions": [
                        {"kind": "tool_success", "tool": "write_tool"}
                    ],
                }
            ],
        },
        max_iterations=4,
        context=context,
    )
    store.append_messages(
        context.session_id,
        [
            {"role": "user", "content": "普通旧问题" + "甲" * 600},
            {"role": "assistant", "content": "普通旧回答" + "乙" * 600},
            {"role": "user", "content": "必须始终使用课程 1，不能跨租户"},
            _tool_call("write-call"),
            {
                "role": "tool",
                "tool_call_id": "write-call",
                "name": "write_tool",
                "content": json.dumps(
                    {
                        "ok": True,
                        "data": {"citation_id": "citation:course-1:section-2"},
                        "error": None,
                        "meta": {
                            "operation_id": operation["id"],
                            "operation_status": "committed",
                            "approval_id": "approval-1",
                            "approval_status": "approved",
                        },
                    }
                ),
            },
            _tool_call("pending-call"),
            {"role": "user", "content": "最近问题"},
            {"role": "assistant", "content": "最近回答"},
        ],
    )
    result = _engine(store).compact_if_needed(
        context.session_id,
        store.get_messages(context.session_id),
        context=context,
    )

    checkpoint = store.latest_context_checkpoint(context.session_id, context=context)
    assert result.compacted_messages == 2
    reasons = {item.get("reason") for item in checkpoint["preserved_items"]}
    assert {
        "outside_persisted_history",
        "outside_compaction_source",
        "explicit_user_constraint",
        "operation_receipt",
        "unpaired_tool_group",
        "unfinished_plan",
    } <= reasons
    assert checkpoint["citation_refs"] == ["citation:course-1:section-2"]
    assert checkpoint["operation_refs"][0]["operation_id"] == operation["id"]
    assert any(item.get("plan_id") == plan["id"] for item in checkpoint["preserved_items"])
    assert "必须始终使用课程 1" in checkpoint["summary"]
    assert "citation:course-1:section-2" in checkpoint["summary"]
    assert "approval-1" in checkpoint["summary"]
    assert "approved" in checkpoint["summary"]


def test_checkpoint_hash_tampering_fails_closed_and_is_audited(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _active_context(store)
    store.append_messages(
        context.session_id,
        [
            {"role": "user", "content": "old" + "x" * 1_000},
            {"role": "assistant", "content": "done" + "y" * 1_000},
            {"role": "user", "content": "recent"},
            {"role": "assistant", "content": "recent done"},
        ],
    )
    result = _engine(store).compact_if_needed(
        context.session_id,
        store.get_messages(context.session_id),
        context=context,
    )
    checkpoint = store.latest_context_checkpoint(context.session_id, context=context)
    with store.connect() as connection:
        connection.execute(
            "UPDATE messages SET content='tampered' WHERE session_id=? AND sequence=?",
            (context.session_id, checkpoint["source_sequences"][0]),
        )

    with pytest.raises(ContextCheckpointValidationError) as captured:
        _engine(store).checkpoint_summary(context.session_id, context=context)
    assert captured.value.reason == "source_hash_mismatch"
    with store.connect() as connection:
        audit = connection.execute(
            "SELECT decision, details_json FROM audit_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert audit["decision"] == "denied"
    assert json.loads(audit["details_json"])["reason"] == "source_hash_mismatch"
    assert result.checkpoint_id == checkpoint["id"]


def test_checkpoint_summary_hash_tampering_fails_closed(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _active_context(store)
    store.append_messages(
        context.session_id,
        [
            {"role": "user", "content": "archive"},
            {"role": "assistant", "content": "keep"},
        ],
    )
    checkpoint = store.compact_messages(
        context.session_id,
        summary="trusted summary",
        message_count=1,
        source_sequences=[0],
        estimated_tokens_before=300,
        context=context,
    )
    with store.connect() as connection:
        connection.execute(
            "UPDATE context_checkpoints SET summary='tampered summary' WHERE id=?",
            (checkpoint["id"],),
        )
    with pytest.raises(ContextCheckpointValidationError) as captured:
        store.latest_context_checkpoint(context.session_id, context=context)
    assert captured.value.reason == "summary_hash_mismatch"


def test_checkpoint_incomplete_provenance_fails_closed(tmp_path):
    path = tmp_path / "state.db"
    store = StateStore(path)
    context = _active_context(store)
    store.append_messages(
        context.session_id,
        [{"role": "user", "content": "archive"}, {"role": "assistant", "content": "keep"}],
    )
    checkpoint = store.compact_messages(
        context.session_id,
        summary="trusted summary",
        message_count=1,
        source_sequences=[0],
        estimated_tokens_before=300,
        context=context,
    )
    with store.connect() as connection:
        connection.execute(
            "UPDATE context_checkpoints SET source_sha256=NULL WHERE id=?",
            (checkpoint["id"],),
        )

    reopened = StateStore(path)
    with pytest.raises(ContextCheckpointValidationError) as captured:
        reopened.latest_context_checkpoint(context.session_id, context=context)

    assert captured.value.reason == "provenance_incomplete"


def test_missing_artifact_and_cross_scope_reference_fail_closed(tmp_path):
    store = StateStore(tmp_path / "state.db")
    first = _active_context(store, session_id="first", run_id="run-first")
    second = _active_context(
        store,
        session_id="second",
        run_id="run-second",
        actor_id="teacher-2",
        tenant_id="school-2",
    )
    artifacts = ArtifactStore(tmp_path / "artifacts", store)
    foreign = artifacts.write_text("foreign", context=second, kind="tool-result")
    store.append_messages(
        first.session_id,
        [
            {"role": "user", "content": "old" + "x" * 800},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "recent"},
        ],
    )

    with pytest.raises(ContextCheckpointValidationError) as captured:
        store.compact_messages(
            first.session_id,
            summary="bad scope",
            message_count=1,
            source_sequences=[0],
            estimated_tokens_before=300,
            artifact_refs=[
                {
                    "type": ARTIFACT_REFERENCE_TYPE,
                    "artifact_id": foreign.id,
                    "sha256": foreign.sha256,
                }
            ],
            context=first,
        )
    assert captured.value.reason == "artifact_scope_mismatch"
    assert store.count("context_checkpoints") == 0
    assert len(store.get_messages(first.session_id)) == 3
    with pytest.raises(ContextCheckpointValidationError) as cross_scope:
        store.latest_context_checkpoint(first.session_id, context=second)
    assert cross_scope.value.reason == "scope_mismatch"


def test_cross_scope_compaction_fails_before_artifact_externalization(tmp_path):
    store = StateStore(tmp_path / "state.db")
    owner = _active_context(store, session_id="owner", run_id="run-owner")
    attacker = _active_context(
        store,
        session_id="attacker",
        run_id="run-attacker",
        actor_id="teacher-2",
        tenant_id="school-2",
    )
    large_result = json.dumps(
        {"ok": True, "data": {"rows": ["secret" * 500]}, "error": None, "meta": {}}
    )
    store.append_messages(
        owner.session_id,
        [
            {"role": "user", "content": "old" + "x" * 800},
            _tool_call(),
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "name": "large_query",
                "content": large_result,
            },
            {"role": "assistant", "content": "recent"},
        ],
    )
    artifacts = ArtifactStore(tmp_path / "artifacts", store)

    with pytest.raises(ContextCheckpointValidationError) as captured:
        _engine(store, artifacts).compact_if_needed(
            owner.session_id,
            store.get_messages(owner.session_id),
            context=attacker,
        )

    assert captured.value.reason == "scope_mismatch"
    assert store.count("artifacts") == 0
    assert json.loads(store.get_messages(owner.session_id)[2]["content"])["data"][
        "rows"
    ][0].startswith("secret")


def test_missing_artifact_after_checkpoint_is_not_silently_ignored(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _active_context(store)
    artifact = ArtifactStore(tmp_path / "artifacts", store).write_text(
        "recoverable body",
        context=context,
        kind="tool-result",
    )
    store.append_messages(
        context.session_id,
        [
            {"role": "user", "content": "old" + "x" * 800},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "recent"},
        ],
    )
    checkpoint = store.compact_messages(
        context.session_id,
        summary="artifact ref",
        message_count=1,
        source_sequences=[0],
        estimated_tokens_before=300,
        artifact_refs=[
            {
                "type": ARTIFACT_REFERENCE_TYPE,
                "artifact_id": artifact.id,
                "sha256": artifact.sha256,
            }
        ],
        context=context,
    )
    with store.connect() as connection:
        path = connection.execute(
            "SELECT path FROM artifacts WHERE id=?",
            (artifact.id,),
        ).fetchone()["path"]
    # Deliberate fault injection: the index remains while the referenced payload vanishes.
    from pathlib import Path

    Path(path).unlink()
    with pytest.raises(ContextCheckpointValidationError) as captured:
        store.get_context_checkpoint(
            checkpoint["id"],
            session_id=context.session_id,
            context=context,
        )
    assert captured.value.reason == "artifact_missing"


def test_compaction_rejects_partial_tool_group_atomically(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _active_context(store)
    store.append_messages(
        context.session_id,
        [
            _tool_call(),
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "name": "large_query",
                "content": "{}",
            },
            {"role": "assistant", "content": "done"},
        ],
    )
    with pytest.raises(ContextCheckpointConflict, match="partial tool group"):
        store.compact_messages(
            context.session_id,
            summary="partial",
            message_count=1,
            source_sequences=[0],
            estimated_tokens_before=300,
            context=context,
        )
    assert store.count("context_checkpoints") == 0
    assert len(store.get_messages(context.session_id)) == 3


def test_legacy_checkpoint_schema_migrates_idempotently_and_remains_readable(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA user_version = 12;
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
                title TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                sequence INTEGER NOT NULL, role TEXT NOT NULL, content TEXT,
                name TEXT, tool_call_id TEXT, tool_calls_json TEXT, run_id TEXT,
                fencing_token INTEGER, active INTEGER NOT NULL DEFAULT 1,
                compaction_id TEXT, created_at TEXT NOT NULL,
                UNIQUE(session_id, sequence)
            );
            CREATE TABLE context_checkpoints (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, summary TEXT NOT NULL,
                first_sequence INTEGER NOT NULL, last_sequence INTEGER NOT NULL,
                source_messages INTEGER NOT NULL,
                estimated_tokens_before INTEGER NOT NULL, created_at TEXT NOT NULL
            );
            INSERT INTO sessions VALUES ('legacy', 'actor', 'tenant', 'old', 't0', 't0');
            INSERT INTO messages(
                session_id, sequence, role, content, active, compaction_id, created_at
            ) VALUES ('legacy', 0, 'user', 'recover me', 0, 'checkpoint-1', 't0');
            INSERT INTO context_checkpoints VALUES (
                'checkpoint-1', 'legacy', 'legacy summary', 0, 0, 1, 10, 't1'
            );
            """
        )

    store = StateStore(path)
    reopened = StateStore(path)
    checkpoint = reopened.latest_context_checkpoint("legacy")
    assert checkpoint["schema_version"] == 1
    assert checkpoint["source_sequences"] == [0]
    assert checkpoint["source_hashes"][0]["sha256"]
    assert checkpoint["summary_sha256"]
    with reopened.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == STATE_SCHEMA_VERSION
        assert connection.execute(
            "SELECT COUNT(*) FROM state_schema_migrations WHERE version=?",
            (CHECKPOINT_MIGRATION,),
        ).fetchone()[0] == 1
    assert store.count("context_checkpoints") == 1


def test_repeated_checkpoint_of_same_source_is_idempotent(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _active_context(store)
    store.append_messages(
        context.session_id,
        [
            {"role": "user", "content": "archive once"},
            {"role": "assistant", "content": "keep"},
        ],
    )
    arguments = {
        "summary": "same source",
        "message_count": 1,
        "source_sequences": [0],
        "estimated_tokens_before": 300,
        "estimated_tokens_after": 100,
        "context": context,
    }
    first = store.compact_messages(context.session_id, **arguments)
    second = store.compact_messages(context.session_id, **arguments)
    assert first["id"] == second["id"]
    assert store.count("context_checkpoints") == 1
    assert store.restore_context_checkpoint_messages(
        first["id"],
        context=context,
    ) == [{"role": "user", "content": "archive once"}]


def test_archived_typed_artifact_reference_restores_original_tool_payload(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _active_context(store)
    artifacts = ArtifactStore(tmp_path / "artifacts", store)
    budget = ToolResultBudget(artifacts, inline_chars=80, preview_chars=40)
    original = json.dumps(
        {
            "ok": True,
            "data": {
                "citation_id": "citation:restore:1",
                "rows": ["payload" * 100],
            },
            "error": None,
            "meta": {},
        }
    )
    replacement = budget.externalize_message(
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "large_query",
            "content": original,
        },
        context=context,
        reason="test_restore",
    )
    reference = json.loads(replacement["content"])["data"]["artifact_ref"]
    store.append_messages(context.session_id, [_tool_call(), replacement])
    checkpoint = store.compact_messages(
        context.session_id,
        summary="citation:restore:1",
        message_count=2,
        source_sequences=[0, 1],
        estimated_tokens_before=300,
        artifact_refs=[reference],
        citation_refs=["citation:restore:1"],
        context=context,
    )

    restored = store.restore_context_checkpoint_messages(
        checkpoint["id"],
        context=context,
        artifact_store=artifacts,
    )
    assert restored[0]["tool_calls"][0]["id"] == "call-1"
    assert json.loads(restored[1]["content"])["data"]["citation_id"] == (
        "citation:restore:1"
    )
    assert store.get_context_checkpoint(
        checkpoint["id"],
        session_id=context.session_id,
        context=context,
    )["citation_refs"] == ["citation:restore:1"]
