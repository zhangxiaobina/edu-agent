from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from edu_agent.engine.base import Engine, EngineResponse
from edu_agent.engine.resilient import is_provider_context_overflow
from edu_agent.runtime.context import CurrentUserInputTooLarge
from edu_agent.runtime.cancellation import CancellationToken
from edu_agent.runtime.config import (
    AppConfig,
    MemoryConfig,
    PlanningConfig,
    RuntimeConfig,
    SecurityConfig,
    StorageConfig,
)
from edu_agent.service import EduAgentService
from edu_agent.runtime.models import RunContext
from edu_agent.runtime.transactions import SimulatedProcessCrash
from edu_agent.state import StateStore


class OverflowThenSuccess(Engine):
    name = "overflow-fixture"

    def __init__(self, failures: int = 1):
        self.failures = failures
        self.calls = 0

    def chat(self, messages, tools):
        self.calls += 1
        if self.calls <= self.failures:
            error = RuntimeError("provider error: context_length_exceeded")
            error.code = "context_length_exceeded"
            raise error
        return EngineResponse(content="recovered")


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 8, 24, tzinfo=UTC)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class CrashAfterRecoveryCommit(CancellationToken):
    def checkpoint(self, boundary=None):
        if boundary == "context_overflow.recovery.after_compaction":
            raise SimulatedProcessCrash(boundary)
        return super().checkpoint(boundary)


def _config(tmp_path):
    return AppConfig(
        runtime=RuntimeConfig(
            context_token_budget=20_000,
            output_token_reserve=128,
            compression_trigger_ratio=1.0,
            compression_release_ratio=0.8,
            compression_keep_recent=2,
            compression_min_reclaim_tokens=0,
            compression_cooldown_turns=1,
            tool_result_inline_chars=160,
            tool_result_preview_chars=80,
        ),
        planning=PlanningConfig(enabled=False),
        memory=MemoryConfig(enabled=False),
        security=SecurityConfig(require_write_approval=False, default_role="admin"),
        storage=StorageConfig(
            state_path=str(tmp_path / "state.db"),
            artifact_path=str(tmp_path / "artifacts"),
        ),
    )


def _service(tmp_path, engine):
    store = StateStore(tmp_path / "state.db")
    store.ensure_session(
        "session-r43",
        actor_id="teacher-r43",
        tenant_id="school-r43",
        role="admin",
        course_ids={7},
    )
    store.append_messages(
        "session-r43",
        [
            {"role": "user", "content": "必须只使用课程 7 " + "x" * 700},
            {"role": "assistant", "content": "旧答复 " + "y" * 700},
            {"role": "user", "content": "旧问题 " + "z" * 700},
            {"role": "assistant", "content": "旧答复 2 " + "q" * 700},
        ],
    )
    service = EduAgentService(
        engine,
        config=_config(tmp_path),
        state_store=store,
    )
    return service, store


def test_provider_overflow_recovers_once_on_same_turn(tmp_path):
    engine = OverflowThenSuccess()
    service, store = _service(tmp_path, engine)
    try:
        result = service.chat(
            "继续完成课程 7 的任务",
            actor_id="teacher-r43",
            tenant_id="school-r43",
            role="admin",
            course_ids={7},
            session_id="session-r43",
            run_id="run-r43",
        )
        assert result.final_answer == "recovered"
        assert engine.calls == 2
        checkpoint = store.latest_context_checkpoint(
            "session-r43",
            actor_id="teacher-r43",
            tenant_id="school-r43",
        )
        journal = store.get_run_journal_snapshot(
            "run-r43",
            session_id="session-r43",
            actor_id="teacher-r43",
            tenant_id="school-r43",
        )
        assert checkpoint is not None
        assert journal.context_checkpoint_id == checkpoint["id"]
        assert journal.budget_snapshot["model_calls"] == 2
        assert journal.model_attempt == 2
        events = store.connect().execute(
            "SELECT event FROM provider_events WHERE run_id=? ORDER BY id",
            ("run-r43",),
        ).fetchall()
        names = [row["event"] for row in events]
        assert "context_overflow_recovery_started" in names
        assert "context_overflow_recovery_recounted" in names
        assert "context_overflow_recovery_compacted" in names
        assert "context_overflow_recovery_retry_started" in names
    finally:
        service.close()


def test_second_provider_overflow_is_terminal_and_does_not_recompact(tmp_path):
    engine = OverflowThenSuccess(failures=2)
    service, store = _service(tmp_path, engine)
    try:
        with pytest.raises(RuntimeError, match="context_length_exceeded"):
            service.chat(
                "继续完成课程 7 的任务",
                actor_id="teacher-r43",
                tenant_id="school-r43",
                role="admin",
                course_ids={7},
                session_id="session-r43",
                run_id="run-r43",
            )
        assert engine.calls == 2
        assert store.count("context_checkpoints") == 1
        with store.connect() as connection:
            names = [
                row["event"]
                for row in connection.execute(
                    "SELECT event FROM provider_events WHERE run_id=? ORDER BY id",
                    ("run-r43",),
                ).fetchall()
            ]
        assert names.count("context_overflow_recovery_started") == 1
        assert names.count("context_overflow_recovery_exhausted") == 1
    finally:
        service.close()


def test_provider_overflow_can_recover_with_artifact_only_compression(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.ensure_session(
        "session-r43",
        actor_id="teacher-r43",
        tenant_id="school-r43",
        role="admin",
        course_ids={7},
    )
    store.append_messages(
        "session-r43",
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "large-call",
                        "type": "function",
                        "function": {"name": "large_query", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "large-call",
                "name": "large_query",
                "content": json.dumps(
                    {"ok": True, "data": {"rows": ["x" * 2_000]}, "meta": {}},
                ),
            },
        ],
    )
    engine = OverflowThenSuccess()
    service = EduAgentService(
        engine,
        config=_config(tmp_path),
        state_store=store,
    )
    try:
        result = service.chat(
            "继续完成课程 7 的任务",
            actor_id="teacher-r43",
            tenant_id="school-r43",
            role="admin",
            course_ids={7},
            session_id="session-r43",
            run_id="run-r43",
        )
        assert result.final_answer == "recovered"
        assert engine.calls == 2
        assert store.count("context_checkpoints") == 0
        assert store.count("artifacts") == 1
        compacted = next(
            event
            for event in store.connect().execute(
                "SELECT details_json FROM provider_events WHERE run_id=? AND event=?",
                ("run-r43", "context_overflow_recovery_compacted"),
            )
        )
        details = json.loads(compacted["details_json"])
        assert details["checkpoint_id"] is None
        assert details["compacted_messages"] == 0
        assert details["externalized_messages"] == 1
    finally:
        service.close()


def test_overflow_predicate_excludes_local_preflight_and_visible_stream_errors():
    assert not is_provider_context_overflow(CurrentUserInputTooLarge("too long"))
    output_cap = RuntimeError("provider output was capped")
    output_cap.code = "max_output_tokens"
    assert not is_provider_context_overflow(output_cap)
    visible = RuntimeError("context_length_exceeded")
    visible.code = "context_length_exceeded"
    visible.stream_visible = True
    assert not is_provider_context_overflow(visible)


@pytest.mark.parametrize("message", ["context_length_exceeded", "output cap"])
def test_non_provider_overflow_does_not_enter_recovery(tmp_path, message):
    class Failing(Engine):
        name = "terminal-fixture"

        def chat(self, messages, tools):
            raise ValueError(message)

    service, store = _service(tmp_path, Failing())
    try:
        with pytest.raises(ValueError):
            service.chat(
                "继续",
                actor_id="teacher-r43",
                tenant_id="school-r43",
                role="admin",
                course_ids={7},
                session_id="session-r43",
                run_id="run-r43",
            )
        assert not store.has_provider_event(
            "run-r43", "context_overflow_recovery_started", provider="runtime"
        )
    finally:
        service.close()


def test_cancel_racing_recovery_commit_finishes_once_without_retry(tmp_path):
    class CancelAtCommittedCheckpoint(CancellationToken):
        def checkpoint(self, boundary=None):
            if (
                boundary == "context_overflow.recovery.after_compaction"
                and not self.cancelled
            ):
                self.cancel("cancel at recovery checkpoint", source="test")
            return super().checkpoint(boundary)

    token = CancelAtCommittedCheckpoint()
    engine = OverflowThenSuccess()
    service, store = _service(tmp_path, engine)
    try:
        result = service.chat(
            "继续完成课程 7 的任务",
            actor_id="teacher-r43",
            tenant_id="school-r43",
            role="admin",
            course_ids={7},
            session_id="session-r43",
            run_id="run-r43",
            cancellation_token=token,
        )
        assert result.final_answer is None
        assert result.stop_reason == "interrupted"
        assert engine.calls == 1
        assert store.count("context_checkpoints") == 1
        finalizer = store.get_turn_finalizer(
            "run-r43",
            session_id="session-r43",
            actor_id="teacher-r43",
            tenant_id="school-r43",
        )
        assert finalizer.terminal is True
        assert store.has_provider_event(
            "run-r43", "context_overflow_recovery_committed", provider="runtime"
        )
        assert not store.has_provider_event(
            "run-r43", "context_overflow_recovery_retry_started", provider="runtime"
        )
    finally:
        service.close()


def test_started_marker_without_checkpoint_can_resume_recovery(tmp_path):
    engine = OverflowThenSuccess()
    service, store = _service(tmp_path, engine)
    store.enqueue_run(
        RunContext.create(
            session_id="session-r43",
            run_id="run-r43",
            actor_id="teacher-r43",
            tenant_id="school-r43",
            role="admin",
            course_ids={7},
        ),
        request_text="继续完成课程 7 的任务",
    )
    store.record_provider_event(
        run_id="run-r43",
        provider="runtime",
        event="context_overflow_recovery_started",
        attempt=1,
        details={"failure_kind": "context_overflow"},
    )
    # Remove the synthetic queued run.  The chat entrypoint recreates it with
    # the same durable identity, while the started marker survives as it would
    # after a process crash before checkpoint commit.
    with store.connect() as connection:
        connection.execute(
            "DELETE FROM trace_event_index WHERE event_id LIKE ?",
            ("runs:run-r43:%",),
        )
        connection.execute("DELETE FROM runs WHERE id=?", ("run-r43",))
    try:
        result = service.chat(
            "继续完成课程 7 的任务",
            actor_id="teacher-r43",
            tenant_id="school-r43",
            role="admin",
            course_ids={7},
            session_id="session-r43",
            run_id="run-r43",
        )
        assert result.final_answer == "recovered"
        assert engine.calls == 2
        assert store.count("context_checkpoints") == 1
        assert store.has_provider_event(
            "run-r43", "context_overflow_recovery_resumed", provider="runtime"
        )
    finally:
        service.close()


def test_committed_recovery_resumes_once_without_recompaction(tmp_path):
    clock = MutableClock()
    store = StateStore(tmp_path / "state.db", clock=clock)
    store.ensure_session(
        "session-r43",
        actor_id="teacher-r43",
        tenant_id="school-r43",
        role="admin",
        course_ids={7},
    )
    store.append_messages(
        "session-r43",
        [
            {"role": "user", "content": "旧问题 " + "x" * 700},
            {"role": "assistant", "content": "旧答复 " + "y" * 700},
            {"role": "user", "content": "旧问题 2 " + "z" * 700},
            {"role": "assistant", "content": "旧答复 2 " + "q" * 700},
        ],
    )
    first_engine = OverflowThenSuccess()
    first = EduAgentService(
        first_engine,
        config=_config(tmp_path),
        state_store=store,
    )
    with pytest.raises(SimulatedProcessCrash, match="after_compaction"):
        first.chat(
            "继续完成课程 7 的任务",
            actor_id="teacher-r43",
            tenant_id="school-r43",
            role="admin",
            course_ids={7},
            session_id="session-r43",
            run_id="run-r43",
            cancellation_token=CrashAfterRecoveryCommit(),
        )
    first.close()
    assert first_engine.calls == 1
    assert store.count("context_checkpoints") == 1
    assert store.has_provider_event(
        "run-r43", "context_overflow_recovery_committed", provider="runtime"
    )
    assert not store.has_provider_event(
        "run-r43", "context_overflow_recovery_retry_started", provider="runtime"
    )

    clock.advance(100)
    recovered_engine = OverflowThenSuccess(failures=0)
    recovered = EduAgentService(
        recovered_engine,
        config=_config(tmp_path),
        state_store=StateStore(tmp_path / "state.db", clock=clock),
    )
    try:
        result = recovered.resume_run(
            "run-r43",
            actor_id="teacher-r43",
            tenant_id="school-r43",
        )
        assert result.final_answer == "recovered"
        assert recovered_engine.calls == 1
        assert recovered.state_store.count("context_checkpoints") == 1
        journal = recovered.state_store.get_run_journal_snapshot(
            "run-r43",
            session_id="session-r43",
            actor_id="teacher-r43",
            tenant_id="school-r43",
        )
        checkpoint = recovered.state_store.latest_context_checkpoint(
            "session-r43",
            actor_id="teacher-r43",
            tenant_id="school-r43",
        )
        assert journal.model_attempt == 2
        assert journal.budget_snapshot["model_calls"] == 2
        assert journal.context_checkpoint_id == checkpoint["id"]
        with recovered.state_store.connect() as connection:
            retry_markers = connection.execute(
                "SELECT COUNT(*) FROM provider_events WHERE run_id=? AND event=?",
                ("run-r43", "context_overflow_recovery_retry_started"),
            ).fetchone()[0]
        assert retry_markers == 1
    finally:
        recovered.close()


def test_artifact_only_recovery_survives_crash_before_committed_event(tmp_path):
    clock = MutableClock()
    store = StateStore(tmp_path / "state.db", clock=clock)
    store.ensure_session(
        "session-r43",
        actor_id="teacher-r43",
        tenant_id="school-r43",
        role="admin",
        course_ids={7},
    )
    store.append_messages(
        "session-r43",
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "crash-large-call",
                        "type": "function",
                        "function": {"name": "large_query", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "crash-large-call",
                "name": "large_query",
                "content": json.dumps(
                    {"ok": True, "data": {"rows": ["x" * 2_000]}, "meta": {}},
                ),
            },
        ],
    )
    record_provider_event = store.record_provider_event

    def crash_before_committed(**event):
        if event.get("event") == "context_overflow_recovery_committed":
            raise SimulatedProcessCrash("before recovery committed event")
        return record_provider_event(**event)

    store.record_provider_event = crash_before_committed
    first_engine = OverflowThenSuccess()
    first = EduAgentService(
        first_engine,
        config=_config(tmp_path),
        state_store=store,
    )
    with pytest.raises(SimulatedProcessCrash, match="committed event"):
        first.chat(
            "继续完成课程 7 的任务",
            actor_id="teacher-r43",
            tenant_id="school-r43",
            role="admin",
            course_ids={7},
            session_id="session-r43",
            run_id="run-r43",
        )
    first.close()
    assert first_engine.calls == 1
    assert store.count("artifacts") == 1
    assert store.count("context_checkpoints") == 0
    assert store.has_provider_event(
        "run-r43", "context_overflow_recovery_started", provider="runtime"
    )
    assert not store.has_provider_event(
        "run-r43", "context_overflow_recovery_committed", provider="runtime"
    )

    clock.advance(100)
    recovered_engine = OverflowThenSuccess(failures=0)
    recovered = EduAgentService(
        recovered_engine,
        config=_config(tmp_path),
        state_store=StateStore(tmp_path / "state.db", clock=clock),
    )
    try:
        result = recovered.resume_run(
            "run-r43",
            actor_id="teacher-r43",
            tenant_id="school-r43",
        )
        assert result.final_answer == "recovered"
        assert recovered_engine.calls == 1
        assert recovered.state_store.count("artifacts") == 1
        assert recovered.state_store.count("context_checkpoints") == 0
        journal = recovered.state_store.get_run_journal_snapshot(
            "run-r43",
            session_id="session-r43",
            actor_id="teacher-r43",
            tenant_id="school-r43",
        )
        assert journal.model_attempt == 2
        assert journal.budget_snapshot["model_calls"] == 2
    finally:
        recovered.close()
