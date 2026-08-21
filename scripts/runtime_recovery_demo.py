"""阶段 4 离线演示：跨实例 lease、fencing、取消与僵尸恢复。"""
from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from edu_agent.engine.base import Engine, EngineResponse
from edu_agent.runtime.config import AppConfig, MemoryConfig, RuntimeConfig, StorageConfig
from edu_agent.runtime.manager import RuntimeManager
from edu_agent.runtime.models import RunContext
from edu_agent.service import EduAgentService
from edu_agent.state import FencingTokenRejected, RunCancelled, SessionLeaseUnavailable, StateStore


class DemoClock:
    def __init__(self):
        self.value = datetime(2026, 8, 17, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class NoopEngine(Engine):
    name = "runtime-demo"

    def chat(self, messages, tools):
        return EngineResponse(content="unused")


def _context(run_id: str) -> RunContext:
    return RunContext.create(
        session_id="shared-session",
        run_id=run_id,
        actor_id="teacher-demo",
        tenant_id="school-demo",
        role="teacher",
        course_ids={1},
    )


def _queue(store: StateStore, context: RunContext) -> None:
    store.ensure_session(
        context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        role=context.role,
        course_ids=context.course_ids,
    )
    store.enqueue_run(context, request_text=f"demo:{context.run_id}")


def main() -> None:
    state_path = Path(tempfile.gettempdir()) / "edu_agent_runtime_recovery_demo.db"
    state_path.unlink(missing_ok=True)
    clock = DemoClock()
    first_store = StateStore(state_path, clock=clock)
    second_store = StateStore(state_path, clock=clock)
    config = AppConfig(
        runtime=RuntimeConfig(
            session_lease_seconds=5,
            session_heartbeat_seconds=2,
            run_stall_seconds=10,
        ),
        memory=MemoryConfig(enabled=False),
        storage=StorageConfig(state_path=str(state_path)),
    )
    first_service = EduAgentService(
        NoopEngine(),
        config=config,
        state_store=first_store,
        runtime_manager=RuntimeManager(
            first_store,
            owner_id="service-a:random-instance",
            lease_seconds=5,
            heartbeat_seconds=2,
        ),
    )
    second_service = EduAgentService(
        NoopEngine(),
        config=config,
        state_store=second_store,
        runtime_manager=RuntimeManager(
            second_store,
            owner_id="service-b:random-instance",
            lease_seconds=5,
            heartbeat_seconds=2,
        ),
    )

    old = _context("run-old")
    _queue(first_store, old)
    busy_rejected = False
    old_fenced = False
    cancelled = False
    with first_service.runtime_manager.session_scope(
        run_id=old.run_id,
        session_id=old.session_id,
        actor_id=old.actor_id,
        tenant_id=old.tenant_id,
    ) as old_claim:
        first_service.runtime_manager.bind_context(old, old_claim)
        contender = _context("run-contender")
        _queue(second_store, contender)
        try:
            with second_service.runtime_manager.session_scope(
                run_id=contender.run_id,
                session_id=contender.session_id,
                actor_id=contender.actor_id,
                tenant_id=contender.tenant_id,
            ):
                pass
        except SessionLeaseUnavailable:
            busy_rejected = True

        clock.advance(6)
        replacement = _context("run-replacement")
        _queue(second_store, replacement)
        with second_service.runtime_manager.session_scope(
            run_id=replacement.run_id,
            session_id=replacement.session_id,
            actor_id=replacement.actor_id,
            tenant_id=replacement.tenant_id,
        ) as replacement_claim:
            second_service.runtime_manager.bind_context(replacement, replacement_claim)
            try:
                first_store.append_messages(
                    old.session_id,
                    [{"role": "assistant", "content": "stale result"}],
                    context=old,
                )
            except FencingTokenRejected:
                old_fenced = True
            second_store.append_messages(
                replacement.session_id,
                [{"role": "assistant", "content": "new owner result"}],
                context=replacement,
            )
            second_service.cancel_run(
                replacement.run_id,
                actor_id=replacement.actor_id,
                tenant_id=replacement.tenant_id,
            )
            try:
                replacement.check_control("demo.cancel")
            except RunCancelled:
                cancelled = True
                second_store.finish_run(
                    replacement.run_id,
                    status="interrupted",
                    budget=replacement.budget.usage(),
                    recovery_reason="demo_cancel",
                    context=replacement,
                )

    orphan = RunContext.create(
        session_id="orphan-session",
        run_id="run-orphan",
        actor_id="teacher-demo",
        tenant_id="school-demo",
        role="teacher",
    )
    _queue(first_store, orphan)
    with first_service.runtime_manager.session_scope(
        run_id=orphan.run_id,
        session_id=orphan.session_id,
        actor_id=orphan.actor_id,
        tenant_id=orphan.tenant_id,
    ) as orphan_claim:
        first_service.runtime_manager.bind_context(orphan, orphan_claim)
    clock.advance(11)
    recovery = second_store.recover_stalled_runs(stall_timeout_seconds=10)

    summary = {
        "same_session_busy_rejected": busy_rejected,
        "old_owner_fenced": old_fenced,
        "cancel_propagated": cancelled,
        "old_token": old.fencing_token,
        "replacement_token": replacement.fencing_token,
        "recovery": recovery,
        "messages": second_store.get_messages("shared-session"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    assert busy_rejected and old_fenced and cancelled
    assert replacement.fencing_token > old.fencing_token
    assert recovery[0]["status"] == "abandoned"
    assert [item["content"] for item in summary["messages"]] == ["new owner result"]


if __name__ == "__main__":
    main()
