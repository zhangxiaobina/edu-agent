from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from edu_agent.agent.turn_finalizer import TurnFinalizer
from edu_agent.engine.base import Engine, EngineResponse, ToolCall
from edu_agent.engine.mock import MockEngine
from edu_agent.engine.resilient import ResilientEngine
from edu_agent.runtime.config import AppConfig, RuntimeConfig, StorageConfig
from edu_agent.runtime.context import ContextBudgetExceeded
from edu_agent.runtime.manager import RuntimeManager
from edu_agent.runtime.models import RunContext
from edu_agent.runtime.transactions import InjectedFault, NamedFaultInjector
from edu_agent.state import (
    STATE_SCHEMA_VERSION,
    TURN_FINALIZER_SCHEMA,
    FencingTokenRejected,
    StateStore,
)
from edu_agent.service import EduAgentService
from edu_agent.tools.registry import ToolSpec


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 8, 22, tzinfo=UTC)

    def __call__(self):
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class ReadProvider:
    def __init__(self):
        self.spec = ToolSpec(
            schema={
                "name": "read_once",
                "description": "read",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
            handler=lambda **_arguments: {"ok": True},
            category="query",
        )

    def openai_tools(self, **_kwargs):
        return [{"type": "function", "function": self.spec.schema}]

    def get_spec(self, name):
        return self.spec if name == "read_once" else None

    def dispatch(self, name, arguments, conn=None):
        assert name == "read_once"
        return {"ok": True}


class UsageFailure(RuntimeError):
    def __init__(self, message, usage):
        super().__init__(message)
        self.usage = usage


class UsageThenFailureEngine(Engine):
    name = "usage-then-failure"

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return EngineResponse(
                tool_calls=[ToolCall("usage-call", "read_once", {})],
                usage={"input_tokens": 5, "output_tokens": 2},
            )
        raise UsageFailure(
            "provider failed after billing",
            {"input_tokens": 3, "output_tokens": 0},
        )


def _durable_run(tmp_path, *, lease_seconds: float = 5.0):
    clock = MutableClock()
    store = StateStore(tmp_path / "state.db", clock=clock)
    context = RunContext.create(
        session_id="session-1",
        actor_id="actor-1",
        tenant_id="tenant-1",
        role="teacher",
        run_id="run-1",
    )
    store.ensure_session(
        context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        role=context.role,
    )
    store.enqueue_run(context, request_text="hello")
    manager = RuntimeManager(
        store,
        owner_id="worker-1",
        lease_seconds=lease_seconds,
        heartbeat_seconds=min(lease_seconds / 4, 0.05),
    )
    scope = manager.session_scope(
        run_id=context.run_id,
        session_id=context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
    )
    claim = scope.__enter__()
    manager.bind_context(context, claim)
    store.create_run_journal(
        context,
        tool_manifest_hash="manifest",
        frozen_provider_route={"provider": "mock", "model": "mock"},
        budget_snapshot=context.budget.usage(),
    )
    return store, context, manager, scope, clock


def _result(store, context, **kwargs):
    options = {
        "final_answer": "answer",
        "trace": [{"tool": "lookup"}],
        "usage": [{"provider": "mock", "total_tokens": 3}],
        "budget": {"model_calls": 1, "tool_calls": 0},
    }
    options.update(kwargs)
    return TurnFinalizer(
        store,
        context,
        **options,
    )


def test_turn_finalizer_schema_has_a_numeric_migration_boundary(tmp_path):
    store = StateStore(tmp_path / "state.db")
    with store.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == STATE_SCHEMA_VERSION
        assert STATE_SCHEMA_VERSION == 14
        assert connection.execute(
            "SELECT COUNT(*) FROM state_schema_migrations WHERE version=?",
            (TURN_FINALIZER_SCHEMA,),
        ).fetchone()[0] == 1


def _recovery_context():
    return RunContext.create(
        session_id="session-1",
        actor_id="actor-1",
        tenant_id="tenant-1",
        role="teacher",
        run_id="run-1",
    )


def _claim_recovery(store, previous, clock, *, owner="worker-2", lease_seconds=5):
    clock.advance(lease_seconds + 1)
    context = _recovery_context()
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
        control_check=lambda boundary: store.assert_run_writable(
            context,
            boundary=boundary,
        ),
    )
    assert context.fencing_token > previous.fencing_token
    return context


@pytest.mark.parametrize(
    "fault_point",
    [
        "tools_closed",
        "plan_verified",
        "final_message_committed",
        "usage_settled",
        "terminal",
        "hooks_done",
        "cleanup_done",
    ],
)
def test_finalizer_cursor_resumes_after_every_step(tmp_path, fault_point):
    store, context, _manager, scope, clock = _durable_run(tmp_path)
    try:
        with pytest.raises(InjectedFault):
            _result(
                store,
                context,
                fault_injector=NamedFaultInjector(f"after_finalizer_{fault_point}"),
            ).finalize()
    finally:
        scope.__exit__(None, None, None)

    pending = store.get_turn_finalizer(
        context.run_id,
        session_id=context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
    )
    recovery = (
        _recovery_context()
        if pending.terminal
        else _claim_recovery(store, context, clock)
    )
    resumed = _result(store, recovery).finalize()
    assert resumed.cursor == 7
    assert resumed.stop_reason == "completed"
    assert store.count("turn_finalizers") == 1
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM messages WHERE run_id=? AND idempotency_key=?",
            ("run-1", "final-assistant:run-1"),
        ).fetchone()[0] == 1
        run = connection.execute(
            "SELECT status, stop_reason, usage_json FROM runs WHERE id=?",
            ("run-1",),
        ).fetchone()
        assert (run["status"], run["stop_reason"]) == ("completed", "completed")
        assert json.loads(run["usage_json"])[0]["total_tokens"] == 3


def test_repeated_finalizer_is_a_noop_and_keeps_one_message(tmp_path):
    store, context, _manager, scope, _clock = _durable_run(tmp_path)
    try:
        first = _result(store, context).finalize()
    finally:
        scope.__exit__(None, None, None)
    second = _result(store, _recovery_context(), final_answer="different").finalize()
    assert first.final_message_id == second.final_message_id
    assert second.final_answer == "answer"
    assert store.count("messages") == 1


def test_pending_finalizer_requires_a_current_lease(tmp_path):
    store, context, _manager, scope, _clock = _durable_run(tmp_path)
    try:
        with pytest.raises(InjectedFault):
            _result(
                store,
                context,
                fault_injector=NamedFaultInjector("after_finalizer_tools_closed"),
            ).finalize()
    finally:
        scope.__exit__(None, None, None)

    with pytest.raises(FencingTokenRejected, match="lease identity is required"):
        _result(store, _recovery_context()).finalize()
    assert store.get_turn_finalizer(
        context.run_id,
        session_id=context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
    ).cursor == 1


def test_recovery_keeps_the_persisted_budget_snapshot(tmp_path):
    store, context, _manager, scope, clock = _durable_run(tmp_path)
    budget = {
        "model_calls": 3,
        "max_model_calls": 12,
        "tool_calls": 2,
        "max_tool_calls": 24,
    }
    try:
        with pytest.raises(InjectedFault):
            _result(
                store,
                context,
                budget=budget,
                fault_injector=NamedFaultInjector("after_finalizer_tools_closed"),
            ).finalize()
    finally:
        scope.__exit__(None, None, None)

    recovery = _claim_recovery(store, context, clock)
    assert recovery.budget.usage()["model_calls"] == 0
    result = _result(store, recovery).finalize()
    journal = store.get_run_journal_snapshot(
        context.run_id,
        session_id=context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
    )
    assert result.budget == budget
    assert journal.budget_snapshot == budget
    assert recovery.budget.usage() == budget


def test_cursor_zero_recovery_uses_the_first_persisted_candidate(tmp_path):
    store, context, _manager, scope, clock = _durable_run(tmp_path)
    try:
        store.ensure_turn_finalizer(
            context,
            stop_reason="completed",
            terminal_status="completed",
            final_answer="first answer",
            trace=[{"winner": "first"}],
            plan=None,
            usage=[{"total_tokens": 1}],
            budget={"model_calls": 1},
        )
    finally:
        scope.__exit__(None, None, None)
    recovery = _claim_recovery(store, context, clock)
    result = _result(
        store,
        recovery,
        final_answer="second answer",
        trace=[{"winner": "second"}],
        usage=[{"total_tokens": 99}],
    ).finalize()
    assert result.final_answer == "first answer"
    assert result.trace == [{"winner": "first"}]
    assert result.usage == [{"total_tokens": 1}]


def test_legacy_terminal_helpers_cannot_bypass_a_pending_finalizer(tmp_path):
    store, context, _manager, scope, _clock = _durable_run(tmp_path)
    try:
        with pytest.raises(InjectedFault):
            _result(
                store,
                context,
                fault_injector=NamedFaultInjector("after_finalizer_tools_closed"),
            ).finalize()
        with pytest.raises(RuntimeError, match="TurnFinalizer"):
            store.finish_run(
                context.run_id,
                status="failed",
                budget={},
                context=context,
            )
        with pytest.raises(RuntimeError, match="TurnFinalizer"):
            store.append_messages(
                context.session_id,
                [{"role": "assistant", "content": "bypass"}],
                context=context,
            )
    finally:
        scope.__exit__(None, None, None)


def test_verifier_exception_becomes_manual_review_and_still_reaches_terminal(tmp_path):
    store, context, _manager, scope, _clock = _durable_run(tmp_path)

    class BrokenCoordinator:
        plan = object()

        def result(self):
            raise RuntimeError("evidence store unavailable")

    class UnusedVerifier:
        pass

    try:
        result = _result(
            store,
            context,
            final_answer="should not publish",
            plan={"id": "persisted"},
            plan_coordinator=BrokenCoordinator(),
            evidence_verifier=UnusedVerifier(),
        ).finalize()
    finally:
        scope.__exit__(None, None, None)
    assert result.status == "failed"
    assert result.stop_reason == "manual_review"
    assert result.cursor == 7
    assert store.count("messages") == 0


def test_cancelled_finalizer_uses_cancelled_journal_branch(tmp_path):
    store, context, _manager, scope, _clock = _durable_run(tmp_path)
    try:
        assert store.cancel_run(context.run_id, actor_id=context.actor_id, tenant_id=context.tenant_id)
        result = _result(store, context, stop_reason="cancelled").finalize()
        journal = store.get_run_journal_snapshot(
            context.run_id,
            session_id=context.session_id,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
        )
    finally:
        scope.__exit__(None, None, None)
    assert result.stop_reason == "interrupted"
    assert journal.phase.value == "cancelled"


def test_two_workers_compete_for_one_terminal_and_one_message(tmp_path):
    store, context, _manager, scope, clock = _durable_run(tmp_path)
    try:
        with pytest.raises(InjectedFault):
            _result(
                store,
                context,
                fault_injector=NamedFaultInjector("after_finalizer_tools_closed"),
            ).finalize()
    finally:
        scope.__exit__(None, None, None)

    recovery = _claim_recovery(store, context, clock)

    def finish():
        return _result(store, recovery).finalize()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _item: finish(), range(2)))
    assert {item.cursor for item in results} == {7}
    assert {item.final_message_id for item in results}.__len__() == 1
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM messages WHERE run_id=? AND role='assistant'",
            ("run-1",),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT status FROM runs WHERE id=?", ("run-1",)
        ).fetchone()[0] == "completed"


def test_recovery_worker_fences_the_old_finalizer_owner(tmp_path):
    store, old_context, _manager, scope, clock = _durable_run(tmp_path)
    try:
        with pytest.raises(InjectedFault):
            _result(
                store,
                old_context,
                fault_injector=NamedFaultInjector("after_finalizer_tools_closed"),
            ).finalize()
    finally:
        scope.__exit__(None, None, None)

    assert not store.release_session_lease(
        session_id=old_context.session_id,
        run_id=old_context.run_id,
        owner_id=old_context.lease_owner,
        fencing_token=old_context.fencing_token,
    )
    recovery_context = _claim_recovery(store, old_context, clock)

    with pytest.raises(FencingTokenRejected):
        _result(store, old_context).finalize()
    recovered = _result(store, recovery_context).finalize()

    assert recovered.cursor == 7
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM messages WHERE run_id=? AND idempotency_key=?",
            (old_context.run_id, "final-assistant:run-1"),
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    ("reason", "status"),
    [
        ("cancelled", "interrupted"),
        ("budget_exceeded", "failed"),
        ("model_failed", "failed"),
        ("manual_review", "failed"),
    ],
)
def test_stop_reasons_are_stable_and_non_success_has_no_final_message(
    tmp_path, reason, status
):
    store, context, _manager, scope, _clock = _durable_run(tmp_path)
    try:
        result = _result(store, context, stop_reason=reason).finalize()
    finally:
        scope.__exit__(None, None, None)
    assert result.stop_reason == ("interrupted" if reason == "cancelled" else reason)
    assert result.status == status
    if result.stop_reason == "interrupted":
        assert result.final_answer is None
    assert store.count("messages") == 0
    run = store.get_run_status("run-1", actor_id="actor-1", tenant_id="tenant-1")
    assert (run["status"], run["stop_reason"]) == (status, result.stop_reason)


def test_cancel_wins_after_final_message_cursor(tmp_path):
    store, context, _manager, scope, clock = _durable_run(tmp_path)
    try:
        with pytest.raises(InjectedFault):
            _result(
                store,
                context,
                fault_injector=NamedFaultInjector(
                    "after_finalizer_final_message_committed"
                ),
            ).finalize()
    finally:
        scope.__exit__(None, None, None)
    assert store.cancel_run("run-1", actor_id="actor-1", tenant_id="tenant-1")
    result = _result(store, _claim_recovery(store, context, clock)).finalize()
    assert result.stop_reason == "interrupted"
    assert result.final_answer is None
    with store.connect() as connection:
        assert connection.execute(
            "SELECT active FROM messages WHERE id=?", (result.final_message_id,)
        ).fetchone()[0] == 0


def test_lease_stays_until_terminal_and_api_completion_is_guarded(tmp_path):
    store, context, _manager, scope, clock = _durable_run(tmp_path, lease_seconds=5)
    try:
        with pytest.raises(InjectedFault):
            _result(
                store,
                context,
                fault_injector=NamedFaultInjector("after_finalizer_tools_closed"),
            ).finalize()
        session = store.get_session_status(
            context.session_id,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
        )
        assert session["active_run_id"] == context.run_id
        assert not store.release_session_lease(
            session_id=context.session_id,
            run_id=context.run_id,
            owner_id=context.lease_owner,
            fencing_token=context.fencing_token,
        )
        request = store.begin_api_request(
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
            request_id="request-1",
            request_hash="hash",
            run_id=context.run_id,
            owner_id="api-owner",
            lease_seconds=10,
        )
        store.start_api_request(
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
            request_id="request-1",
            owner_id="api-owner",
            attempt=request["attempt"],
        )
        with pytest.raises(RuntimeError, match="before run terminal"):
            store.finish_api_request(
                actor_id=context.actor_id,
                tenant_id=context.tenant_id,
                request_id="request-1",
                status="completed",
                run_id=context.run_id,
                response={"ok": True},
                owner_id="api-owner",
                attempt=request["attempt"],
            )
        with pytest.raises(RuntimeError, match="before run terminal"):
            store.finish_api_request(
                actor_id=context.actor_id,
                tenant_id=context.tenant_id,
                request_id="request-1",
                status="completed",
                response={"ok": True},
                owner_id="api-owner",
                attempt=request["attempt"],
            )
    finally:
        scope.__exit__(None, None, None)
    recovery = _claim_recovery(store, context, clock)
    _result(store, recovery).finalize()
    with store.connect() as connection:
        lease = connection.execute(
            "SELECT lease_owner, fencing_token FROM session_leases WHERE session_id=?",
            (context.session_id,),
        ).fetchone()
    assert lease is not None
    assert store.release_session_lease(
        session_id=context.session_id,
        run_id=context.run_id,
        owner_id=lease["lease_owner"],
        fencing_token=lease["fencing_token"],
    )
    assert store.get_session_status(
        context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
    )["active_run_id"] is None


def test_api_request_cannot_change_its_bound_run(tmp_path):
    store, context, _manager, scope, _clock = _durable_run(tmp_path)
    try:
        request = store.begin_api_request(
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
            request_id="request-bound-run",
            request_hash="hash",
            run_id=context.run_id,
            owner_id="api-owner",
            lease_seconds=10,
        )
        assert store.start_api_request(
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
            request_id="request-bound-run",
            owner_id="api-owner",
            attempt=request["attempt"],
        )
        with pytest.raises(RuntimeError, match="another run"):
            store.finish_api_request(
                actor_id=context.actor_id,
                tenant_id=context.tenant_id,
                request_id="request-bound-run",
                status="failed",
                run_id="different-run",
                owner_id="api-owner",
                attempt=request["attempt"],
            )
    finally:
        scope.__exit__(None, None, None)


def test_api_request_cannot_finish_for_a_missing_bound_run(tmp_path):
    store = StateStore(tmp_path / "state.db")
    request = store.begin_api_request(
        actor_id="actor-1",
        tenant_id="tenant-1",
        request_id="request-missing-run",
        request_hash="hash",
        run_id="missing-run",
        owner_id="api-owner",
        lease_seconds=10,
    )
    assert store.start_api_request(
        actor_id="actor-1",
        tenant_id="tenant-1",
        request_id="request-missing-run",
        owner_id="api-owner",
        attempt=request["attempt"],
    )
    with pytest.raises(RuntimeError, match="missing run"):
        store.finish_api_request(
            actor_id="actor-1",
            tenant_id="tenant-1",
            request_id="request-missing-run",
            status="failed",
            run_id="missing-run",
            error={"code": "SETUP_FAILED"},
            owner_id="api-owner",
            attempt=request["attempt"],
        )


def test_completed_without_a_final_message_fails_closed(tmp_path):
    store, context, _manager, scope, _clock = _durable_run(tmp_path)
    try:
        result = TurnFinalizer(
            store,
            context,
            stop_reason="completed",
            final_answer=None,
        ).finalize()
    finally:
        scope.__exit__(None, None, None)
    assert (result.status, result.stop_reason) == ("failed", "model_failed")
    assert result.final_message_id is None


def test_failed_post_hook_is_audited_without_reversing_success(tmp_path):
    store, context, _manager, scope, _clock = _durable_run(tmp_path)
    try:
        def failing_hook(_context, _result):
            raise RuntimeError("collector down")

        result = _result(
            store,
            context,
            hooks={"telemetry": failing_hook},
        ).finalize()
    finally:
        scope.__exit__(None, None, None)
    assert result.status == "completed"
    assert result.stop_reason == "completed"
    with store.connect() as connection:
        hook = connection.execute(
            "SELECT status, error FROM turn_finalizer_hooks WHERE run_id=?",
            (context.run_id,),
        ).fetchone()
        audit = connection.execute(
            "SELECT decision, details_json FROM audit_events WHERE action='turn_finalizer.post_hook'"
        ).fetchone()
    assert hook["status"] == "failed"
    assert "collector down" in hook["error"]
    assert audit["decision"] == "failed"
    assert "collector down" in audit["details_json"]


def test_cleanup_timeout_is_bounded_and_audited(tmp_path):
    store, context, _manager, scope, _clock = _durable_run(tmp_path)
    release = threading.Event()
    cleanup_threads = []
    try:
        def blocked_cleanup(_context, _result):
            cleanup_threads.append(threading.current_thread())
            release.wait(2)

        started = time.monotonic()
        result = TurnFinalizer(
            store,
            context,
            final_answer="answer",
            cleanup=blocked_cleanup,
            cleanup_timeout_seconds=0.02,
        ).finalize()
        elapsed = time.monotonic() - started
    finally:
        release.set()
        scope.__exit__(None, None, None)
    assert result.cursor == 7
    assert elapsed < 0.5
    assert cleanup_threads and cleanup_threads[0].daemon is True
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action='turn_finalizer.cleanup' AND decision='timeout'"
        ).fetchone()[0] == 1


def test_competing_workers_claim_cleanup_once(tmp_path):
    store, context, _manager, scope, _clock = _durable_run(tmp_path)
    calls = []
    lock = threading.Lock()

    def cleanup(_context, _result):
        with lock:
            calls.append("cleanup")

    try:
        with pytest.raises(InjectedFault):
            _result(
                store,
                context,
                cleanup=cleanup,
                fault_injector=NamedFaultInjector("after_finalizer_hooks_done"),
            ).finalize()
    finally:
        scope.__exit__(None, None, None)

    def finish():
        return _result(store, _recovery_context(), cleanup=cleanup).finalize()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _item: finish(), range(2)))
    assert calls == ["cleanup"]


def test_service_response_recovery_finishes_terminal_post_processing(tmp_path):
    hook_calls = []
    service = EduAgentService(
        MockEngine(lambda _messages, _tools, _step: EngineResponse(content="answer")),
        config=AppConfig(
            storage=StorageConfig(
                state_path=str(tmp_path / "service.db"),
                artifact_path=str(tmp_path / "artifacts"),
            )
        ),
        finalizer_fault_injector=NamedFaultInjector("after_finalizer_terminal"),
        post_process_hooks={"review": lambda _context, _result: hook_calls.append("review")},
    )
    try:
        with pytest.raises(InjectedFault):
            service.chat(
                "hello",
                actor_id="actor-1",
                tenant_id="tenant-1",
                role="teacher",
                run_id="terminal-hook-recovery",
            )
        run = service.get_run_status(
            "terminal-hook-recovery",
            actor_id="actor-1",
            tenant_id="tenant-1",
        )
        before = service.state_store.get_turn_finalizer(
            "terminal-hook-recovery",
            session_id=run["session_id"],
            actor_id="actor-1",
            tenant_id="tenant-1",
        )
        recovered = service.recover_chat_result(
            "terminal-hook-recovery",
            actor_id="actor-1",
            tenant_id="tenant-1",
        )
        replay = service.recover_chat_result(
            "terminal-hook-recovery",
            actor_id="actor-1",
            tenant_id="tenant-1",
        )
        after = service.state_store.get_turn_finalizer(
            "terminal-hook-recovery",
            session_id=run["session_id"],
            actor_id="actor-1",
            tenant_id="tenant-1",
        )
    finally:
        service.close()

    assert before.cursor == 5
    assert after.cursor == 7
    assert recovered == replay
    assert recovered.final_answer == "answer"
    assert hook_calls == ["review"]


def test_service_finalizes_failure_before_model_call(tmp_path):
    engine = MockEngine(
        lambda messages, tools, step: pytest.fail("context failure must precede model")
    )
    service = EduAgentService(
        engine,
        config=AppConfig(
            runtime=RuntimeConfig(context_token_budget=256),
            storage=StorageConfig(
                state_path=str(tmp_path / "service.db"),
                artifact_path=str(tmp_path / "artifacts"),
            ),
        ),
    )
    try:
        with pytest.raises(ContextBudgetExceeded):
            service.chat(
                "message that cannot fit" * 100,
                actor_id="actor-1",
                tenant_id="tenant-1",
                role="teacher",
                run_id="setup-failure",
            )
        run = service.get_run_status(
            "setup-failure",
            actor_id="actor-1",
            tenant_id="tenant-1",
        )
        finalizer = service.state_store.get_turn_finalizer(
            "setup-failure",
            session_id=run["session_id"],
            actor_id="actor-1",
            tenant_id="tenant-1",
        )
    finally:
        service.close()
    assert (run["status"], run["stop_reason"]) == ("failed", "budget_exceeded")
    assert finalizer.cursor == 7


def test_service_model_failure_has_stable_terminal_reason(tmp_path):
    def fail_model(_messages, _tools, _step):
        raise RuntimeError("provider exploded")

    service = EduAgentService(
        MockEngine(fail_model),
        config=AppConfig(
            storage=StorageConfig(
                state_path=str(tmp_path / "service.db"),
                artifact_path=str(tmp_path / "artifacts"),
            )
        ),
    )
    try:
        with pytest.raises(RuntimeError, match="provider exploded"):
            service.chat(
                "hello",
                actor_id="actor-1",
                tenant_id="tenant-1",
                role="teacher",
                run_id="model-failure",
            )
        run = service.get_run_status(
            "model-failure",
            actor_id="actor-1",
            tenant_id="tenant-1",
        )
    finally:
        service.close()
    assert (run["status"], run["stop_reason"]) == ("failed", "model_failed")


def test_service_failure_settles_selected_and_exception_usage(tmp_path):
    engine = ResilientEngine(
        UsageThenFailureEngine(),
        max_retries=0,
    )
    service = EduAgentService(
        engine,
        tools_provider=ReadProvider(),
        config=AppConfig(
            storage=StorageConfig(
                state_path=str(tmp_path / "service.db"),
                artifact_path=str(tmp_path / "artifacts"),
            )
        ),
    )
    try:
        with pytest.raises(UsageFailure, match="provider failed after billing"):
            service.chat(
                "hello",
                actor_id="actor-1",
                tenant_id="tenant-1",
                role="teacher",
                run_id="usage-failure",
            )
        run = service.get_run_status(
            "usage-failure",
            actor_id="actor-1",
            tenant_id="tenant-1",
        )
        finalizer = service.state_store.get_turn_finalizer(
            "usage-failure",
            session_id=run["session_id"],
            actor_id="actor-1",
            tenant_id="tenant-1",
        )
    finally:
        service.close()

    assert (run["status"], run["stop_reason"]) == ("failed", "model_failed")
    assert finalizer.usage == [
        {"input_tokens": 5, "output_tokens": 2, "runtime_attempts": 1},
        {"input_tokens": 3, "output_tokens": 0},
    ]
    assert json.loads(run["usage_json"]) == finalizer.usage
