"""Demonstrate R2 process-reopen recovery through public service APIs."""
from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from edu_agent.engine.base import Engine, EngineResponse, ToolCall
from edu_agent.observability.redaction import RedactionPolicy
from edu_agent.runtime.config import (
    AppConfig,
    MemoryConfig,
    PlanningConfig,
    RuntimeConfig,
    SecurityConfig,
    StorageConfig,
)
from edu_agent.runtime.manager import RuntimeManager
from edu_agent.runtime.recovery import RecoveryAction
from edu_agent.runtime.transactions import (
    ProcessCrashFaultInjector,
    SimulatedProcessCrash,
)
from edu_agent.service import EduAgentService
from edu_agent.state import StateStore
from edu_agent.tools.registry import ToolSpec


ACTOR_ID = "teacher-r2-demo"
TENANT_ID = "school-r2-demo"
SESSION_ID = "session-r2-demo"
RUN_ID = "run-r2-demo"
FAULT_POINT = "after_assistant_envelope_commit"


class DemoClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 22, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class DemoReadProvider:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.spec = ToolSpec(
            schema={
                "name": "read_recovery_marker",
                "description": "Return one deterministic recovery marker.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
            handler=lambda connection, **arguments: {},
            category="query",
        )

    def openai_tools(self, **kwargs):
        return [{"type": "function", "function": self.spec.schema}]

    def get_spec(self, name):
        return self.spec if name == "read_recovery_marker" else None

    def dispatch(self, name, arguments, conn=None):
        self.calls.append(name)
        return {"marker": "durable-read-result"}


class DemoEngine(Engine):
    name = "r2-recovery-demo-engine"

    def chat(self, messages, tools):
        if any(
            message.get("tool_call_id") == "read-recovery-call"
            for message in messages
        ):
            return EngineResponse(content="recovery completed")
        return EngineResponse(
            tool_calls=[
                ToolCall(
                    "read-recovery-call",
                    "read_recovery_marker",
                    {},
                )
            ]
        )


def _config(root: Path) -> AppConfig:
    return AppConfig(
        runtime=RuntimeConfig(
            max_model_calls=4,
            max_tool_calls=4,
            compression_enabled=False,
            session_lease_seconds=0.2,
            session_heartbeat_seconds=0.05,
            run_stall_seconds=0.4,
        ),
        planning=PlanningConfig(enabled=False),
        memory=MemoryConfig(enabled=False),
        security=SecurityConfig(default_role="teacher"),
        storage=StorageConfig(
            state_path=str(root / "state.db"),
            artifact_path=str(root / "artifacts"),
        ),
    )


def _service(
    root: Path,
    clock: DemoClock,
    provider: DemoReadProvider,
    *,
    owner_id: str,
    crash: bool = False,
) -> EduAgentService:
    store = StateStore(root / "state.db", clock=clock)
    return EduAgentService(
        DemoEngine(),
        config=_config(root),
        state_store=store,
        tools_provider=provider,
        runtime_manager=RuntimeManager(
            store,
            owner_id=owner_id,
            lease_seconds=0.2,
            heartbeat_seconds=0.05,
        ),
        loop_fault_injector=(
            ProcessCrashFaultInjector(FAULT_POINT) if crash else None
        ),
    )


def main() -> None:
    clock = DemoClock()
    read_calls: list[str] = []
    policy = RedactionPolicy()
    with tempfile.TemporaryDirectory(prefix="edu-agent-r2-recovery-") as directory:
        root = Path(directory)
        provider = DemoReadProvider(read_calls)
        crashed_service = _service(
            root,
            clock,
            provider,
            owner_id="worker-before-crash",
            crash=True,
        )
        crash_observed = False
        try:
            crashed_service.chat(
                "demonstrate durable recovery",
                actor_id=ACTOR_ID,
                tenant_id=TENANT_ID,
                role="teacher",
                session_id=SESSION_ID,
                run_id=RUN_ID,
            )
        except SimulatedProcessCrash as error:
            crash_observed = str(error) == FAULT_POINT
        finally:
            crashed_service.close()

        clock.advance(1)
        recovered_service = _service(
            root,
            clock,
            provider,
            owner_id="worker-after-reopen",
        )
        try:
            decision = recovered_service.get_recovery_decision(
                RUN_ID,
                actor_id=ACTOR_ID,
                tenant_id=TENANT_ID,
            )
            result = recovered_service.resume_run(
                RUN_ID,
                actor_id=ACTOR_ID,
                tenant_id=TENANT_ID,
            )
            terminal_decision = recovered_service.get_recovery_decision(
                RUN_ID,
                actor_id=ACTOR_ID,
                tenant_id=TENANT_ID,
            )
            run_status = recovered_service.get_run_status(
                RUN_ID,
                actor_id=ACTOR_ID,
                tenant_id=TENANT_ID,
            )
            trace_page = recovered_service.get_trace(
                actor_id=ACTOR_ID,
                tenant_id=TENANT_ID,
                limit=100,
            )
        finally:
            recovered_service.close()

        recovery_trace = [
            event
            for event in trace_page["events"]
            if event["attributes"].get("action") == "run.recovery_decision"
        ]
        invariants = {
            "process_reopened": crash_observed,
            "read_replayed_exactly_once": read_calls == ["read_recovery_marker"],
            "run_terminal": run_status is not None
            and run_status["status"] == "completed",
            "terminal_replay_selected": (
                terminal_decision.action is RecoveryAction.TERMINAL_REPLAY
            ),
            "budget_preserved": result.budget["max_model_calls"] == 4
            and result.budget["max_tool_calls"] == 4,
            "recovery_trace_recorded": bool(recovery_trace),
        }
        output = policy.redact(
            {
                "schema_version": "edu-agent.r2-recovery-demo.v1",
                "fault_window": FAULT_POINT,
                "decision": decision.to_safe_dict(),
                "terminal_decision": terminal_decision.to_safe_dict(),
                "result": {
                    "status": run_status["status"] if run_status else None,
                    "final_answer": result.final_answer,
                    "budget": result.budget,
                },
                "invariants": invariants,
                "trace": recovery_trace,
            }
        )
        print(json.dumps(output, ensure_ascii=True, indent=2, sort_keys=True))

        assert decision.action is RecoveryAction.REPLAY_READ
        assert all(invariants.values())


if __name__ == "__main__":
    main()
