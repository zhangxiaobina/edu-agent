from __future__ import annotations

import pytest

from edu_agent.runtime.context_engine import CheckpointContextEngine
from edu_agent.runtime.config import RuntimeConfig
from edu_agent.runtime.models import RunContext
from edu_agent.state import ContextCheckpointConflict, StateStore


def _context(store, session="policy-session", run="policy-run"):
    context = RunContext.create(
        session_id=session,
        run_id=run,
        actor_id="policy-actor",
        tenant_id="policy-tenant",
        role="teacher",
        course_ids={3},
    )
    store.ensure_session(
        session,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        role=context.role,
        course_ids={3},
    )
    return context


def _append(store, session, marker, size=800):
    store.append_messages(
        session,
        [
            {"role": "user", "content": f"旧问题 {marker} " + "x" * size},
            {"role": "assistant", "content": f"旧答复 {marker} " + "y" * size},
        ],
    )


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"trigger_ratio": True}, "trigger_ratio"),
        ({"release_ratio": True}, "release_ratio"),
    ],
)
def test_context_policy_rejects_boolean_ratios(tmp_path, kwargs, match):
    store = StateStore(tmp_path / "state.db")
    with pytest.raises(ValueError, match=match):
        CheckpointContextEngine(store, token_budget=1_000, **kwargs)

    config_name = f"compression_{next(iter(kwargs))}"
    with pytest.raises(ValueError, match=config_name):
        RuntimeConfig(**{config_name: next(iter(kwargs.values()))})


def test_trigger_release_hysteresis_prevents_threshold_jitter(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context(store)
    _append(store, context.session_id, "one")
    _append(store, context.session_id, "zero")
    engine = CheckpointContextEngine(
        store,
        token_budget=700,
        trigger_ratio=0.5,
        release_ratio=0.25,
        keep_recent=2,
        cooldown_turns=0,
    )
    first = engine.compact_if_needed(
        context.session_id,
        store.get_messages(context.session_id),
        context=None,
    )
    assert first.compacted_messages == 2
    _append(store, context.session_id, "two", size=1_200)
    held = engine.compact_if_needed(
        context.session_id,
        store.get_messages(context.session_id),
        context=None,
    )
    assert held.decision == "hysteresis_hold"
    assert store.count("context_checkpoints") == 1


def test_minimum_reclaim_and_no_compactable_content_are_explicit(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context(store, session="min-session", run="min-run")
    _append(store, context.session_id, "small", size=800)
    _append(store, context.session_id, "small-2", size=100)
    engine = CheckpointContextEngine(
        store,
        token_budget=256,
        trigger_ratio=0.5,
        keep_recent=2,
        min_reclaim_tokens=10_000,
    )
    result = engine.compact_if_needed(
        context.session_id,
        store.get_messages(context.session_id),
        context=None,
    )
    assert result.decision == "below_min_reclaim"
    assert result.compacted_messages == 0
    assert store.count("context_checkpoints") == 0


def test_mandatory_fidelity_item_larger_than_summary_cap_fails_closed(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context(store, session="large-key", run="large-key-run")
    store.append_messages(
        context.session_id,
        [
            {"role": "user", "content": "ordinary old question " + "x" * 800},
            {"role": "assistant", "content": "ordinary old answer " + "y" * 800},
            {
                "role": "user",
                "content": "必须保留这个不可截断约束 " + "关" * 1_000,
            },
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "recent"},
            {"role": "assistant", "content": "recent done"},
        ],
    )
    engine = CheckpointContextEngine(
        store,
        token_budget=256,
        trigger_ratio=0.5,
        keep_recent=2,
        summary_max_chars=256,
    )
    result = engine.compact_if_needed(
        context.session_id,
        store.get_messages(context.session_id),
        context=None,
    )
    assert result.decision == "mandatory_summary_too_large"
    assert store.count("context_checkpoints") == 0
    assert len(store.get_messages(context.session_id)) == 6


def test_release_rearms_after_new_turn_and_restart_does_not_duplicate(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context(store, session="rearm-session", run="rearm-run")
    _append(store, context.session_id, "first", size=5_000)
    _append(store, context.session_id, "first-old", size=5_000)
    engine = CheckpointContextEngine(
        store,
        token_budget=10_000,
        trigger_ratio=0.5,
        release_ratio=0.4,
        keep_recent=2,
        cooldown_turns=0,
    )
    first = engine.compact_if_needed(
        context.session_id, store.get_messages(context.session_id), context=None
    )
    assert first.decision == "compacted"
    assert first.estimated_tokens_after <= first.release_threshold

    # A fresh engine sees the durable observed-sequence marker and must not
    # create a second checkpoint for the same active history.
    restarted = CheckpointContextEngine(
        store,
        token_budget=10_000,
        trigger_ratio=0.5,
        release_ratio=0.4,
        keep_recent=2,
        cooldown_turns=0,
    )
    duplicate = restarted.compact_if_needed(
        context.session_id, store.get_messages(context.session_id), context=None
    )
    assert duplicate.decision in {"below_trigger", "hysteresis_hold"}
    assert store.count("context_checkpoints") == 1

    _append(store, context.session_id, "second", size=6_000)
    second = restarted.compact_if_needed(
        context.session_id, store.get_messages(context.session_id), context=None
    )
    assert second.decision == "compacted"
    assert store.count("context_checkpoints") == 2


def test_cooldown_counts_user_turns_not_retained_messages(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context(store, session="cooldown-session", run="cooldown-run")
    _append(store, context.session_id, "first", size=5_000)
    _append(store, context.session_id, "first-old", size=5_000)
    engine = CheckpointContextEngine(
        store,
        token_budget=10_000,
        trigger_ratio=0.5,
        release_ratio=0.4,
        keep_recent=2,
        cooldown_turns=2,
    )
    assert engine.compact_if_needed(
        context.session_id, store.get_messages(context.session_id), context=None
    ).decision == "compacted"

    # Assistant/tool chatter without a user turn does not consume cooldown.
    store.append_messages(
        context.session_id,
        [{"role": "assistant", "content": "chatter" + "z" * 20_000}],
    )
    held = engine.compact_if_needed(
        context.session_id, store.get_messages(context.session_id), context=None
    )
    assert held.decision == "cooldown"
    _append(store, context.session_id, "turn-one", size=700)
    assert engine.compact_if_needed(
        context.session_id, store.get_messages(context.session_id), context=None
    ).decision == "cooldown"
    _append(store, context.session_id, "turn-two", size=700)
    assert engine.compact_if_needed(
        context.session_id, store.get_messages(context.session_id), context=None
    ).decision == "compacted"


def test_all_protected_history_reports_no_compactable_exchange(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context(store, session="protected-session", run="protected-run")
    store.append_messages(
        context.session_id,
        [
            {"role": "user", "content": "必须只使用课程 3"},
            {"role": "assistant", "content": "已记录 APPROVAL_REQUIRED"},
            {"role": "user", "content": "不得跨租户，必须保留审批"},
            {"role": "assistant", "content": "已记录 APPROVAL_REQUIRED"},
            {"role": "user", "content": "最近问题必须仍限课程 3"},
            {"role": "assistant", "content": "最近回答 APPROVAL_REQUIRED"},
        ],
    )
    engine = CheckpointContextEngine(
        store,
        token_budget=256,
        trigger_ratio=0.5,
        keep_recent=2,
        cooldown_turns=0,
    )
    result = engine.compact_if_needed(
        context.session_id,
        store.get_messages(context.session_id),
        context=None,
        force=True,
    )
    assert result.decision == "no_compactable_exchange"
    assert store.count("context_checkpoints") == 0


def test_force_compaction_bypasses_recent_count_but_keeps_protections(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _context(store, session="force-session", run="force-run")
    store.enqueue_run(context, request_text="recover from provider overflow")
    _append(store, context.session_id, "large", size=5_000)
    engine = CheckpointContextEngine(
        store,
        token_budget=20_000,
        trigger_ratio=1.0,
        keep_recent=12,
        cooldown_turns=10,
    )

    regular = engine.compact_if_needed(
        context.session_id,
        store.get_messages(context.session_id),
        context=context,
    )
    assert regular.decision == "below_trigger"

    forced = engine.compact_if_needed(
        context.session_id,
        store.get_messages(context.session_id),
        context=context,
        force=True,
        reason="provider_context_overflow",
    )
    assert forced.decision == "compacted"
    assert forced.compacted_messages == 2
    checkpoint = store.latest_context_checkpoint(
        context.session_id,
        context=context,
    )
    assert checkpoint is not None
    assert any(
        item.get("type") == "compaction_policy"
        and item.get("reason") == "provider_context_overflow"
        for item in checkpoint["preserved_items"]
    )


def test_structured_prior_summary_is_merged_without_scope_leak(tmp_path):
    store = StateStore(tmp_path / "state.db")
    engine = CheckpointContextEngine(store, token_budget=1_000, summary_max_chars=4_000)
    scope = {
        "session_id": "summary-session",
        "actor_id": "summary-actor",
        "tenant_id": "summary-tenant",
        "role": "teacher",
        "course_ids": [3],
    }
    first = engine._summarize(
        [{"role": "user", "content": "old"}],
        scope=scope,
        constraints=["必须保留旧约束"],
        entities=["course_id=3"],
        approvals=[{"approval_id": "approval-old", "approval_status": "approved"}],
        artifact_refs=[{"artifact_id": "artifact-old"}],
        citation_refs=["citation:old"],
        operation_refs=[{"operation_id": "operation-old", "status": "committed"}],
        unfinished_plans=[{"plan_id": "plan-old", "status": "in_progress"}],
    )
    second = engine._summarize(
        [{"role": "user", "content": "new"}],
        scope=scope,
        constraints=["不得丢弃新约束"],
        entities=["class_id=9"],
        approvals=[{"approval_id": "approval-new", "approval_status": "required"}],
        prior_summary=first,
        artifact_refs=[{"artifact_id": "artifact-new"}],
        citation_refs=["citation:new"],
        operation_refs=[{"operation_id": "operation-new", "status": "pending"}],
        unfinished_plans=[{"plan_id": "plan-new", "status": "in_progress"}],
    )
    for value in (
        "必须保留旧约束",
        "不得丢弃新约束",
        "course_id=3",
        "class_id=9",
        "approval-old",
        "approval-new",
        "artifact-old",
        "artifact-new",
        "citation:old",
        "citation:new",
        "operation-old",
        "operation-new",
        "plan-new",
    ):
        assert value in second

    with pytest.raises(ContextCheckpointConflict, match="scope"):
        engine._summarize(
            [{"role": "user", "content": "foreign"}],
            scope={**scope, "tenant_id": "foreign-tenant"},
            prior_summary=first,
        )
