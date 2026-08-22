from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from .agent.graph import run_agent
from .agent.prompts import SYSTEM_PROMPT
from .agent.turn_finalizer import FinalizationResult, TurnFinalizer
from .code_execution import build_code_execution_provider
from .engine import Engine, get_engine
from .knowledge import KnowledgeToolProvider, SQLiteKnowledgeProvider
from .observability import RedactionPolicy, RunEventType, TraceRepository
from .planning.runtime import PlanningOptions
from .planning.runtime import PlanCoordinator
from .planning.verifier import EvidenceVerifier
from .runtime.config import AppConfig, load_config
from .runtime.context import ContextBudgetExceeded, ContextManager
from .runtime.artifacts import ArtifactStore, ToolResultBudget
from .runtime.context_engine import CheckpointContextEngine, ContextEngine
from .runtime.cancellation import CancellationRequested, CancellationToken
from .runtime.models import BudgetExceeded, RunContext
from .runtime.manager import RuntimeManager
from .runtime.tool_executor import ApprovalRequest, ExecutionPolicy, PolicyToolExecutor
from .scheduler import JobStore, Scheduler
from .state import MemoryManager, MemoryProvider, StateStore
from .state.store import FencingTokenRejected, RunCancelled, TurnFinalizerPending
from .tools import registry


@dataclass(frozen=True)
class ChatResult:
    session_id: str
    run_id: str
    final_answer: str | None
    trace: list[dict]
    budget: dict
    usage: list[dict]
    context: dict
    stop_reason: str | None
    plan: dict | None


class EduAgentService:
    def __init__(
        self,
        engine: Engine,
        *,
        config: AppConfig | None = None,
        state_store: StateStore | None = None,
        tools_provider=None,
        approval_handler: Callable[[ApprovalRequest], bool] | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        runtime_manager: RuntimeManager | None = None,
        memory_provider: MemoryProvider | None = None,
        context_engine: ContextEngine | None = None,
        plan_generator=None,
        loop_fault_injector=None,
        finalizer_fault_injector=None,
        post_process_hooks=None,
        finalizer_cleanup=None,
    ):
        self.config = config or AppConfig()
        self.engine = engine
        self.state_store = state_store or StateStore(self.config.state_path)
        self.code_execution_provider = self._build_code_execution_provider()
        if self.code_execution_provider is not None:
            self.code_execution_provider.health_check(force=True)
        registry.configure_code_execution(self.code_execution_provider)
        self.tools_provider = tools_provider or registry
        if tools_provider is None and self.config.knowledge.enabled:
            knowledge = SQLiteKnowledgeProvider(
                self.config.knowledge.path,
                event_sink=lambda event: self.state_store.record_provider_event(**event),
            )
            if knowledge.available():
                self.tools_provider = KnowledgeToolProvider(
                    self.tools_provider,
                    knowledge,
                    max_results=self.config.knowledge.max_results,
                )
        self.approval_handler = approval_handler
        self.system_prompt = system_prompt
        self.runtime_manager = runtime_manager or RuntimeManager(
            self.state_store,
            lease_seconds=self.config.runtime.session_lease_seconds,
            heartbeat_seconds=self.config.runtime.session_heartbeat_seconds,
        )
        self.recovery_report = self.state_store.recover_stalled_runs(
            stall_timeout_seconds=self.config.runtime.run_stall_seconds,
        )
        self.memory = memory_provider or MemoryManager(
            self.state_store,
            max_items=self.config.memory.max_recalled_items,
            max_item_chars=self.config.memory.max_item_chars,
        )
        self.context_manager = ContextManager(
            token_budget=self.config.runtime.context_token_budget,
            recent_message_limit=self.config.runtime.recent_message_limit,
        )
        self.context_engine = context_engine or (
            CheckpointContextEngine(
                self.state_store,
                token_budget=self.config.runtime.context_token_budget,
                trigger_ratio=self.config.runtime.compression_trigger_ratio,
                keep_recent=self.config.runtime.compression_keep_recent,
                summary_max_chars=self.config.runtime.compression_summary_max_chars,
            )
            if self.config.runtime.compression_enabled
            else None
        )
        self.plan_generator = plan_generator
        self.loop_fault_injector = loop_fault_injector
        self.finalizer_fault_injector = finalizer_fault_injector
        self.post_process_hooks = post_process_hooks
        self.finalizer_cleanup = finalizer_cleanup
        self.artifact_store = ArtifactStore(self.config.artifact_path, self.state_store)
        self.trace_repository = TraceRepository(
            self.state_store,
            redaction=RedactionPolicy(),
        )
        self.result_budget = ToolResultBudget(
            self.artifact_store,
            inline_chars=self.config.runtime.tool_result_inline_chars,
            preview_chars=self.config.runtime.tool_result_preview_chars,
            turn_budget_chars=self.config.runtime.tool_turn_budget_chars,
        )
        self._delegation_runtime = None
        self._teaching_delegation = None
        if hasattr(self.engine, "event_sink") and self.engine.event_sink is None:
            self.engine.event_sink = lambda event: self.state_store.record_provider_event(**event)

    def _build_code_execution_provider(self):
        return build_code_execution_provider(self.config.code_execution)

    @property
    def teaching_delegation(self):
        """Return the concrete read-only teaching delegation facade."""
        if not self.config.delegation.enabled:
            raise RuntimeError("delegation 已在配置中关闭")
        if self._teaching_delegation is None:
            from .delegation import DelegationRuntime, TeachingDelegationService

            self._delegation_runtime = DelegationRuntime(
                self.state_store,
                self.tools_provider,
                artifact_store=self.artifact_store,
                policy=self.config.delegation.policy(),
            )
            self._teaching_delegation = TeachingDelegationService(self._delegation_runtime)
        return self._teaching_delegation

    @classmethod
    def from_config(
        cls,
        config_path: str | None = None,
        *,
        approval_handler: Callable[[ApprovalRequest], bool] | None = None,
    ) -> EduAgentService:
        config = load_config(config_path)
        return cls(
            get_engine(config.model),
            config=config,
            approval_handler=approval_handler,
        )

    def chat(
        self,
        message: str,
        *,
        actor_id: str,
        role: str | None = None,
        tenant_id: str = "default",
        course_ids: set[int] | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        db_conn=None,
        replay_scope: str | None = None,
        cancellation_token: CancellationToken | None = None,
        stream_writer=None,
    ) -> ChatResult:
        if not message.strip():
            raise ValueError("message 不能为空")
        session_id = session_id or self.state_store.new_session_id()
        context = RunContext.create(
            session_id=session_id,
            actor_id=actor_id,
            role=role or self.config.security.default_role,
            tenant_id=tenant_id,
            course_ids=course_ids,
            replay_scope=replay_scope,
            run_id=run_id or uuid.uuid4().hex,
            max_model_calls=self.config.runtime.max_model_calls,
            max_tool_calls=self.config.runtime.max_tool_calls,
            cancellation_token=cancellation_token,
        )
        if stream_writer is not None:
            context.bind_event_sinks(
                run_event_sink=stream_writer.publish,
                provider_event_sink=stream_writer.provider_event,
            )
        self.state_store.ensure_session(
            session_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
            role=context.role,
            course_ids=context.course_ids,
            title=message[:80],
        )
        self.state_store.enqueue_run(context, request_text=message)
        with self.runtime_manager.session_scope(
            run_id=context.run_id,
            session_id=session_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
            cancellation_token=context.cancellation_token,
        ) as claim:
            self.runtime_manager.bind_context(context, claim)
            if stream_writer is not None:
                stream_writer.bind(
                    session_id=session_id,
                    fencing_token=claim.fencing_token if claim is not None else 0,
                )
                stream_writer.publish(
                    RunEventType.RUN_PHASE,
                    {
                        "phase": "accepted",
                        "status": "accepted",
                        "run_id": context.run_id,
                    },
                )
            return self._chat_turn(message, context=context, db_conn=db_conn)

    @staticmethod
    def _default_turn_context() -> dict:
        return {
            "estimated_tokens": 0,
            "omitted_messages": 0,
            "memory_ids": [],
            "checkpoint_id": None,
            "compacted_messages": 0,
        }

    def _plan_finalizer_components(self, context: RunContext):
        """Load the persisted Plan/Evidence verifiers for the final re-check."""
        try:
            coordinator = PlanCoordinator(
                self.state_store,
                context,
                options=PlanningOptions(
                    enabled=self.config.planning.enabled,
                    max_steps=self.config.planning.max_steps,
                    max_step_retries=self.config.planning.max_step_retries,
                    max_iterations=self.config.planning.max_iterations,
                ),
            )
            if coordinator.plan is None:
                return None, None, None
            verifier = EvidenceVerifier(
                self.state_store,
                context,
                max_step_retries=self.config.planning.max_step_retries,
                citation_verifier=getattr(self.tools_provider, "verify_citation", None),
                citation_claim_verifier=getattr(self.tools_provider, "verify_claim", None),
            )
            return coordinator, verifier, None
        except Exception as error:
            # A missing plan is handled above.  Once a plan is persisted,
            # inability to reload it is an uncertain completion boundary and
            # must be recorded by the finalizer rather than treated as a
            # plain, unplanned answer.
            return None, None, f"{type(error).__name__}: {error}"

    def _finalize_turn(
        self,
        context: RunContext,
        *,
        result: dict | None = None,
        final_message: dict | None = None,
        stop_reason: str | None = None,
        error: str | None = None,
        context_payload: dict | None = None,
    ) -> FinalizationResult:
        result = result or {}
        coordinator, verifier, plan_verification_error = self._plan_finalizer_components(context)
        finalizer = TurnFinalizer(
            self.state_store,
            context,
            result=result,
            final_message=final_message,
            stop_reason=stop_reason,
            error=error,
            plan=result.get("plan"),
            plan_coordinator=coordinator,
            evidence_verifier=verifier,
            plan_verification_error=plan_verification_error,
            hooks=self.post_process_hooks,
            cleanup=self.finalizer_cleanup,
            fault_injector=self.finalizer_fault_injector,
            context_payload=context_payload,
        )
        return finalizer.finalize()

    def _chat_result_from_finalization(
        self,
        finalization: FinalizationResult,
        *,
        context_payload: dict | None = None,
    ) -> ChatResult:
        return ChatResult(
            session_id=finalization.session_id,
            run_id=finalization.run_id,
            final_answer=finalization.final_answer,
            trace=finalization.trace,
            budget=finalization.budget,
            usage=finalization.usage,
            context=(
                context_payload
                or finalization.context
                or self._default_turn_context()
            ),
            stop_reason=finalization.stop_reason,
            plan=finalization.plan,
        )

    @staticmethod
    def _failure_stop_reason(error: Exception) -> str:
        text = f"{type(error).__name__}: {error}".lower()
        if isinstance(error, (BudgetExceeded, ContextBudgetExceeded)):
            return "budget_exceeded"
        if "budget" in text or "quota" in text:
            return "budget_exceeded"
        if "manual_review" in text or "uncertain" in text:
            return "manual_review"
        return "model_failed"

    def _persisted_failure_usage(
        self,
        context: RunContext,
        error: Exception,
    ) -> list[dict]:
        usage = self.state_store.get_selected_provider_usage(
            context.run_id,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
        )
        error_usage = getattr(error, "usage", None)
        if isinstance(error_usage, Mapping) and error_usage:
            usage.append(dict(error_usage))
        return usage

    def _finalize_failure(
        self,
        context: RunContext,
        error: Exception,
        *,
        context_payload: dict | None = None,
    ) -> None:
        existing_finalizer = self.state_store.get_turn_finalizer(
            context.run_id,
            session_id=context.session_id,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
        )
        if existing_finalizer is not None:
            return
        error_text = f"{type(error).__name__}: {error}"
        self._finalize_turn(
            context,
            result={
                "final_answer": None,
                "trace": [],
                "usage": self._persisted_failure_usage(context, error),
                "budget": context.budget.usage(),
                "stop_reason": self._failure_stop_reason(error),
                "plan": None,
            },
            stop_reason=self._failure_stop_reason(error),
            error=error_text,
            context_payload=context_payload,
        )

    def _chat_turn(
        self,
        message: str,
        *,
        context: RunContext,
        db_conn=None,
        resume: bool = False,
    ) -> ChatResult:
        try:
            runtime_context = (
                self.engine.runtime_context(context.run_id)
                if hasattr(self.engine, "runtime_context")
                else contextlib.nullcontext()
            )
            with runtime_context:
                return self._chat_turn_impl(
                    message,
                    context=context,
                    db_conn=db_conn,
                    resume=resume,
                )
        except (RunCancelled, CancellationRequested) as error:
            finalization = self._finalize_turn(
                context,
                result={
                    "final_answer": None,
                    "trace": [],
                    "usage": self._persisted_failure_usage(context, error),
                    "budget": context.budget.usage(),
                    "stop_reason": "interrupted",
                    "plan": None,
                },
                stop_reason="interrupted",
                error=str(error),
            )
            return self._chat_result_from_finalization(finalization)
        except FencingTokenRejected:
            raise
        except Exception as error:
            self._finalize_failure(context, error)
            raise

    def _chat_turn_impl(
        self,
        message: str,
        *,
        context: RunContext,
        db_conn=None,
        resume: bool = False,
    ) -> ChatResult:
        session_id = context.session_id
        context.check_control("turn.start")
        context.emit_run_event(RunEventType.RUN_PHASE.value, {"phase": "planning"})
        routes = self.engine.begin_turn_routes()
        for index, route in enumerate(routes):
            details = route.to_event()
            details["route_role"] = "primary" if index == 0 else "fallback"
            details["selection_reason"] = (
                "configured_primary"
                if index == 0
                else "configured_fallback_candidate"
            )
            self.state_store.record_provider_event(
                run_id=context.run_id,
                provider=route.provider,
                event="route_resolved",
                attempt=0,
                details=details,
            )
        history = self.state_store.get_messages(
            session_id,
            limit=None,
        )
        run_messages = self.state_store.get_run_messages(context.run_id) if resume else []
        resumed_protocol_messages: list[dict] = []
        if resume:
            persisted_finalizer = self.state_store.get_turn_finalizer(
                context.run_id,
                session_id=context.session_id,
                actor_id=context.actor_id,
                tenant_id=context.tenant_id,
            )
            if persisted_finalizer is not None:
                finalization = self._finalize_turn(context)
                return self._chat_result_from_finalization(finalization)
        if resume and run_messages:
            recovered_answer = next(
                (
                    item.get("content")
                    for item in reversed(run_messages)
                    if item.get("role") == "assistant" and not item.get("tool_calls")
                ),
                None,
            )
            if recovered_answer is not None:
                finalization = self._finalize_turn(
                    context,
                    result={
                        "final_answer": recovered_answer,
                        "trace": [],
                        "usage": [],
                        "budget": context.budget.usage(),
                        "stop_reason": "completed",
                        "plan": None,
                    },
                    final_message={"role": "assistant", "content": recovered_answer},
                )
                return self._chat_result_from_finalization(finalization)
            if len(history) >= len(run_messages) and history[-len(run_messages) :] == run_messages:
                history = history[: -len(run_messages)]
            else:
                run_message_count = len(run_messages)
                matching_start = next(
                    (
                        index
                        for index in range(len(history) - run_message_count, -1, -1)
                        if history[index : index + run_message_count] == run_messages
                    ),
                    None,
                )
                if matching_start is None:
                    raise RuntimeError("resume run messages are not an ordered session segment")
                history = [
                    *history[:matching_start],
                    *history[matching_start + run_message_count :],
                ]
            resumed_protocol_messages = [
                item for item in run_messages if item.get("role") in {"assistant", "tool"}
            ]
        compaction = (
            self.context_engine.compact_if_needed(
                session_id,
                history,
                context=context,
            )
            if self.context_engine is not None and not resume
            else None
        )
        if compaction and compaction.compacted_messages:
            history = self.state_store.get_messages(
                session_id,
                limit=None,
            )
        checkpoint_summary = (
            self.context_engine.checkpoint_summary(session_id)
            if self.context_engine is not None
            else None
        )
        memory_snapshot = (
            self.memory.snapshot(context, message)
            if self.config.memory.enabled
            else None
        )
        snapshot = self.context_manager.prepare(
            system_prompt=self.system_prompt,
            history=history,
            user_message=message,
            memory_items=memory_snapshot.items if memory_snapshot else [],
            context_checkpoint=checkpoint_summary,
        )
        agent_messages = [*snapshot.messages, *resumed_protocol_messages]
        if not resume or not self.state_store.get_run_messages(context.run_id):
            self.state_store.append_messages(
                session_id,
                [{"role": "user", "content": message}],
                context=context,
            )
        self.state_store.start_run(
            run_id=context.run_id,
            session_id=session_id,
            model=getattr(self.engine, "name", type(self.engine).__name__),
            context_tokens=snapshot.estimated_tokens,
            omitted_messages=snapshot.omitted_messages,
            context=context,
        )
        policy = ExecutionPolicy(
            require_write_approval=self.config.security.require_write_approval,
            allow_local_code_execution=(
                self.code_execution_provider is not None
                and self.config.code_execution.enabled
            ),
            enforce_roles=True,
            approval_ttl_seconds=self.config.transaction.approval_ttl_seconds,
        )
        executor = PolicyToolExecutor(
            self.tools_provider,
            policy=policy,
            approval_handler=self.approval_handler,
            state_store=self.state_store,
            result_budget=self.result_budget,
        )
        context_payload = {
            "estimated_tokens": snapshot.estimated_tokens,
            "omitted_messages": snapshot.omitted_messages,
            "memory_ids": memory_snapshot.ids if memory_snapshot else [],
            "checkpoint_id": compaction.checkpoint_id if compaction else None,
            "compacted_messages": compaction.compacted_messages if compaction else 0,
        }
        try:
            result = run_agent(
                message,
                self.engine,
                db_conn=db_conn,
                recursion_limit=max(30, self.config.runtime.max_model_calls * 2 + 2),
                tools_provider=self.tools_provider,
                initial_messages=agent_messages,
                run_context=context,
                tool_executor=executor,
                planning=PlanningOptions(
                    enabled=self.config.planning.enabled,
                    max_steps=self.config.planning.max_steps,
                    max_step_retries=self.config.planning.max_step_retries,
                    max_iterations=self.config.planning.max_iterations,
                ),
                plan_generator=self.plan_generator,
                state_store=self.state_store,
                context_checkpoint_id=(
                    compaction.checkpoint_id
                    if compaction is not None
                    else (
                        self.state_store.latest_context_checkpoint(session_id) or {}
                    ).get("id")
                ),
                loop_fault_injector=self.loop_fault_injector,
            )
            context.check_control("messages.before_final_commit")
            context.emit_run_event(
                RunEventType.RUN_PHASE.value,
                {"phase": "finalizing"},
            )
            generated = result["messages"][len(agent_messages) :]
            final_message = next(
                (
                    item
                    for item in reversed(generated)
                    if item.get("role") == "assistant" and not item.get("tool_calls")
                ),
                None,
            )
            finalization = self._finalize_turn(
                context,
                result=result,
                final_message=final_message,
                context_payload=context_payload,
            )
            return self._chat_result_from_finalization(
                finalization,
                context_payload=context_payload,
            )
        except (RunCancelled, CancellationRequested) as error:
            finalization = self._finalize_turn(
                context,
                result={
                    "final_answer": None,
                    "trace": [],
                    "usage": self._persisted_failure_usage(context, error),
                    "budget": context.budget.usage(),
                    "stop_reason": "interrupted",
                    "plan": None,
                },
                stop_reason="interrupted",
                error=str(error),
                context_payload=context_payload,
            )
            return self._chat_result_from_finalization(
                finalization,
                context_payload=context_payload,
            )
        except FencingTokenRejected:
            raise
        except Exception as error:
            # A failure raised by a finalizer step is a deliberate recovery
            # boundary.  Do not immediately start a second finalizer from
            # this worker; the persisted cursor is for the next owner.
            existing_finalizer = self.state_store.get_turn_finalizer(
                context.run_id,
                session_id=context.session_id,
                actor_id=context.actor_id,
                tenant_id=context.tenant_id,
            )
            if existing_finalizer is not None:
                raise
            self._finalize_failure(
                context,
                error,
                context_payload=context_payload,
            )
            raise

    def cancel_run(
        self,
        run_id: str,
        *,
        actor_id: str,
        tenant_id: str = "default",
    ) -> bool:
        requested = self.state_store.cancel_run(
            run_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
        )
        self.runtime_manager.cancel_run(
            run_id,
            reason="run cancellation requested",
            source="explicit",
        )
        if self._delegation_runtime is not None:
            status = self.state_store.get_run_status(
                run_id,
                actor_id=actor_id,
                tenant_id=tenant_id,
            )
            if status is not None:
                context = RunContext.create(
                    session_id=status["session_id"],
                    run_id=run_id,
                    actor_id=actor_id,
                    tenant_id=tenant_id,
                    role=status.get("role") or self.config.security.default_role,
                )
                self._delegation_runtime.cancel_root(
                    context,
                    reason="PARENT_RUN_CANCELLED",
                )
        return requested

    def resume_run(
        self,
        run_id: str,
        *,
        actor_id: str,
        tenant_id: str = "default",
        db_conn=None,
        cancellation_token: CancellationToken | None = None,
        stream_writer=None,
    ) -> ChatResult:
        try:
            record = self.state_store.prepare_run_resume(
                run_id,
                actor_id=actor_id,
                tenant_id=tenant_id,
            )
        except TurnFinalizerPending:
            # A finalizer crash can leave the run in ``running`` until its
            # lease expires.  Requeue only that durable-cursor case.
            record = self.state_store.prepare_turn_finalizer_resume(
                run_id,
                actor_id=actor_id,
                tenant_id=tenant_id,
            )
        session = self.state_store.get_session_status(
            record["session_id"],
            actor_id=actor_id,
            tenant_id=tenant_id,
        )
        if session is None:
            raise RuntimeError("run 对应 session 不存在")
        with self.state_store.connect() as connection:
            session_record = connection.execute(
                "SELECT role, course_ids_json FROM sessions WHERE id=?",
                (record["session_id"],),
            ).fetchone()
        context = RunContext.create(
            session_id=record["session_id"],
            actor_id=actor_id,
            tenant_id=tenant_id,
            role=record["role"] or session_record["role"] or self.config.security.default_role,
            course_ids=set(json.loads(session_record["course_ids_json"] or "[]")),
            run_id=run_id,
            max_model_calls=self.config.runtime.max_model_calls,
            max_tool_calls=self.config.runtime.max_tool_calls,
            cancellation_token=cancellation_token,
        )
        if stream_writer is not None:
            context.bind_event_sinks(
                run_event_sink=stream_writer.publish,
                provider_event_sink=stream_writer.provider_event,
            )
        with self.runtime_manager.session_scope(
            run_id=run_id,
            session_id=context.session_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
            cancellation_token=context.cancellation_token,
        ) as claim:
            self.runtime_manager.bind_context(context, claim)
            if stream_writer is not None:
                stream_writer.bind(
                    session_id=context.session_id,
                    fencing_token=claim.fencing_token if claim is not None else 0,
                )
                stream_writer.publish(
                    RunEventType.RUN_PHASE,
                    {"phase": "accepted", "status": "resumed"},
                )
            pending_finalizer = self.state_store.get_turn_finalizer(
                run_id,
                session_id=context.session_id,
                actor_id=actor_id,
                tenant_id=tenant_id,
            )
            if pending_finalizer is not None:
                return self._chat_result_from_finalization(
                    self._finalize_turn(context),
                )
            return self._chat_turn(
                record["request_text"],
                context=context,
                db_conn=db_conn,
                resume=True,
            )

    def get_run_status(
        self,
        run_id: str,
        *,
        actor_id: str,
        tenant_id: str = "default",
    ) -> dict | None:
        return self.state_store.get_run_status(
            run_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
        )

    def get_session_status(
        self,
        session_id: str,
        *,
        actor_id: str,
        tenant_id: str = "default",
    ) -> dict | None:
        return self.state_store.get_session_status(
            session_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
        )

    def get_plan(
        self,
        run_id: str,
        *,
        actor_id: str,
        tenant_id: str = "default",
    ) -> dict | None:
        run = self.get_run_status(run_id, actor_id=actor_id, tenant_id=tenant_id)
        if run is None:
            return None
        plan = self.state_store.get_plan_for_run(
            run_id,
            session_id=run["session_id"],
            actor_id=actor_id,
            tenant_id=tenant_id,
        )
        if plan is None:
            return None
        return {
            **plan,
            "steps": self.state_store.get_plan_steps(
                plan["id"],
                session_id=run["session_id"],
                actor_id=actor_id,
                tenant_id=tenant_id,
            ),
            "evidence": self.state_store.get_plan_evidence(
                plan["id"], actor_id=actor_id, tenant_id=tenant_id
            ),
        }

    def get_artifact_metadata(
        self,
        artifact_id: str,
        *,
        actor_id: str,
        tenant_id: str = "default",
    ) -> dict | None:
        record = self.state_store.get_artifact(
            artifact_id, actor_id=actor_id, tenant_id=tenant_id
        )
        if record is None:
            return None
        return {
            key: value
            for key, value in record.items()
            if key != "path"
        }

    def read_artifact(
        self,
        artifact_id: str,
        *,
        actor_id: str,
        role: str,
        tenant_id: str = "default",
    ) -> str:
        if role not in {"teacher", "admin"}:
            raise PermissionError("artifact content requires teacher/admin role")
        record = self.state_store.get_artifact(
            artifact_id, actor_id=actor_id, tenant_id=tenant_id
        )
        if record is None:
            raise KeyError(f"artifact not found: {artifact_id}")
        context = RunContext.create(
            session_id=record["session_id"],
            actor_id=actor_id,
            tenant_id=tenant_id,
            role=role,
            run_id=record["run_id"],
        )
        return self.artifact_store.read_text(artifact_id, context=context)

    def read_artifact_chunk(
        self,
        artifact_id: str,
        *,
        actor_id: str,
        role: str,
        tenant_id: str = "default",
        offset: int = 0,
        limit: int = 64 * 1024,
    ) -> tuple[str, bool]:
        if role not in {"teacher", "admin"}:
            raise PermissionError("artifact content requires teacher/admin role")
        record = self.state_store.get_artifact(
            artifact_id, actor_id=actor_id, tenant_id=tenant_id
        )
        if record is None:
            raise KeyError(f"artifact not found: {artifact_id}")
        context = RunContext.create(
            session_id=record["session_id"], actor_id=actor_id, tenant_id=tenant_id,
            role=role, run_id=record["run_id"],
        )
        return self.artifact_store.read_text_chunk(
            artifact_id, context=context, offset=offset, limit=limit
        )

    def get_trace(self, *, actor_id: str, tenant_id: str = "default", **query) -> dict:
        return self.trace_repository.list_events(
            actor_id=actor_id, tenant_id=tenant_id, **query
        ).to_dict()

    def begin_api_request(
        self,
        *,
        actor_id: str,
        tenant_id: str,
        request_id: str,
        request_hash: str,
        run_id: str | None = None,
        owner_id: str | None = None,
        lease_seconds: float | None = None,
        retention_seconds: int | None = None,
    ) -> dict:
        return self.state_store.begin_api_request(
            actor_id=actor_id,
            tenant_id=tenant_id,
            request_id=request_id,
            request_hash=request_hash,
            run_id=run_id,
            owner_id=owner_id,
            lease_seconds=lease_seconds or self.config.api.request_lease_seconds,
            retention_seconds=retention_seconds or self.config.api.request_retention_seconds,
        )

    def start_api_request(self, **kwargs) -> bool:
        return self.state_store.start_api_request(**kwargs)

    def finish_api_request(self, **kwargs) -> dict:
        return self.state_store.finish_api_request(**kwargs)

    def renew_api_request(self, **kwargs) -> bool:
        return self.state_store.renew_api_request(**kwargs)

    def recover_chat_result(
        self,
        run_id: str,
        *,
        actor_id: str,
        tenant_id: str = "default",
    ) -> ChatResult:
        """Reconstruct a terminal response after a response-commit crash window."""
        record = self.state_store.get_run_status(run_id, actor_id=actor_id, tenant_id=tenant_id)
        if record is None:
            raise KeyError(f"run 不存在：{run_id}")
        if record["status"] not in {"completed", "failed", "interrupted"}:
            raise RuntimeError(f"run 尚未进入可恢复终态：{record['status']}")
        finalizer = self.state_store.get_turn_finalizer(
            run_id,
            session_id=record["session_id"],
            actor_id=actor_id,
            tenant_id=tenant_id,
        )
        if finalizer is not None and finalizer.terminal:
            if finalizer.cursor < 7:
                with self.state_store.connect() as connection:
                    session = connection.execute(
                        "SELECT role, course_ids_json FROM sessions WHERE id=?",
                        (record["session_id"],),
                    ).fetchone()
                if session is None:
                    raise RuntimeError("run session does not exist")
                context = RunContext.create(
                    session_id=record["session_id"],
                    run_id=run_id,
                    actor_id=actor_id,
                    tenant_id=tenant_id,
                    role=record["role"] or session["role"] or self.config.security.default_role,
                    course_ids=set(json.loads(session["course_ids_json"] or "[]")),
                    max_model_calls=self.config.runtime.max_model_calls,
                    max_tool_calls=self.config.runtime.max_tool_calls,
                )
                return self._chat_result_from_finalization(self._finalize_turn(context))
            return self._chat_result_from_finalization(
                finalization=FinalizationResult.from_record(finalizer)
            )
        messages = self.state_store.get_run_messages(run_id)
        answer = next(
            (
                item.get("content")
                for item in reversed(messages)
                if item.get("role") == "assistant" and not item.get("tool_calls")
            ),
            None,
        )
        budget = json.loads(record.get("budget_json") or "{}")
        stop_reason = record.get("stop_reason") or (
            "completed" if record["status"] == "completed" else record["status"]
        )
        return ChatResult(
            session_id=record["session_id"],
            run_id=run_id,
            final_answer=answer,
            trace=[],
            budget=budget,
            usage=json.loads(record.get("usage_json") or "[]"),
            context={
                "estimated_tokens": record.get("context_tokens", 0),
                "omitted_messages": record.get("omitted_messages", 0),
                "memory_ids": [],
                "checkpoint_id": None,
                "compacted_messages": 0,
            },
            stop_reason=stop_reason,
            plan=None,
        )

    def resume_api_run(
        self,
        run_id: str,
        *,
        actor_id: str,
        tenant_id: str = "default",
        cancellation_token: CancellationToken | None = None,
        stream_writer=None,
    ) -> ChatResult:
        """Resume a claimed queued/abandoned request using persistent run state."""
        record = self.state_store.get_run_status(run_id, actor_id=actor_id, tenant_id=tenant_id)
        if record is None:
            raise KeyError(f"run 不存在：{run_id}")
        if record["status"] == "abandoned":
            return self.resume_run(
                run_id,
                actor_id=actor_id,
                tenant_id=tenant_id,
                cancellation_token=cancellation_token,
                stream_writer=stream_writer,
            )
        if record["status"] != "queued":
            if record["status"] in {"completed", "failed", "interrupted"}:
                if stream_writer is not None:
                    stream_writer.bind(
                        session_id=record["session_id"],
                        fencing_token=0,
                    )
                    stream_writer.publish(
                        RunEventType.RUN_PHASE,
                        {"phase": "accepted", "status": "replaying"},
                    )
                return self.recover_chat_result(run_id, actor_id=actor_id, tenant_id=tenant_id)
            pending_finalizer = self.state_store.get_turn_finalizer(
                run_id,
                session_id=record["session_id"],
                actor_id=actor_id,
                tenant_id=tenant_id,
            )
            if pending_finalizer is not None and not pending_finalizer.terminal:
                return self.resume_run(
                    run_id,
                    actor_id=actor_id,
                    tenant_id=tenant_id,
                    cancellation_token=cancellation_token,
                    stream_writer=stream_writer,
                )
            raise RuntimeError(f"run 不能从当前状态恢复：{record['status']}")
        with self.state_store.connect() as connection:
            session = connection.execute(
                "SELECT role, course_ids_json FROM sessions WHERE id=?",
                (record["session_id"],),
            ).fetchone()
        if session is None:
            raise RuntimeError("run 对应 session 不存在")
        context = RunContext.create(
            session_id=record["session_id"],
            actor_id=actor_id,
            tenant_id=tenant_id,
            role=record["role"] or session["role"] or self.config.security.default_role,
            course_ids=set(json.loads(session["course_ids_json"] or "[]")),
            run_id=run_id,
            max_model_calls=self.config.runtime.max_model_calls,
            max_tool_calls=self.config.runtime.max_tool_calls,
            cancellation_token=cancellation_token,
        )
        if stream_writer is not None:
            context.bind_event_sinks(
                run_event_sink=stream_writer.publish,
                provider_event_sink=stream_writer.provider_event,
            )
        with self.runtime_manager.session_scope(
            run_id=run_id,
            session_id=context.session_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
            cancellation_token=context.cancellation_token,
        ) as claim:
            self.runtime_manager.bind_context(context, claim)
            if stream_writer is not None:
                stream_writer.bind(
                    session_id=context.session_id,
                    fencing_token=claim.fencing_token if claim is not None else 0,
                )
                stream_writer.publish(
                    RunEventType.RUN_PHASE,
                    {"phase": "accepted", "status": "resumed"},
                )
            return self._chat_turn(
                record.get("request_text") or "",
                context=context,
                resume=True,
            )

    def get_scheduled_job(
        self,
        job_id: str,
        *,
        actor_id: str,
        tenant_id: str = "default",
    ) -> dict | None:
        return self.state_store.get_scheduled_job(
            job_id, actor_id=actor_id, tenant_id=tenant_id
        )

    def inspect_trace(
        self,
        run_id: str,
        *,
        actor_id: str,
        tenant_id: str = "default",
    ) -> dict:
        return self.trace_repository.inspect_run(
            run_id, actor_id=actor_id, tenant_id=tenant_id
        )

    def remember(
        self,
        content: str,
        *,
        actor_id: str,
        tenant_id: str = "default",
        session_id: str | None = None,
        course_id: int | None = None,
        importance: float = 0.5,
        kind: str = "preference",
    ) -> int:
        context = RunContext.create(
            session_id=session_id or "memory",
            actor_id=actor_id,
            role=self.config.security.default_role,
            tenant_id=tenant_id,
            course_ids={course_id} if course_id is not None else None,
        )
        return self.memory.remember(
            context,
            content,
            kind=kind,
            importance=importance,
            scope="course" if course_id is not None else "user",
            scope_id=str(course_id) if course_id is not None else "",
        )

    def schedule(
        self,
        *,
        name: str,
        prompt: str,
        actor_id: str,
        role: str,
        next_run_at: datetime,
        tenant_id: str = "default",
        interval_seconds: int | None = None,
        max_attempts: int = 3,
        retry_backoff_seconds: int = 60,
        idempotency_key: str | None = None,
    ) -> str:
        if role not in {"teacher", "admin"}:
            raise PermissionError("只有 teacher/admin 可以创建计划任务")
        return JobStore(self.state_store).create(
            actor_id=actor_id,
            tenant_id=tenant_id,
            role=role,
            name=name,
            prompt=prompt,
            next_run_at=next_run_at,
            interval_seconds=interval_seconds,
            max_attempts=max_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
            idempotency_key=idempotency_key,
        )

    def cancel_scheduled_job(
        self,
        job_id: str,
        *,
        actor_id: str,
        tenant_id: str = "default",
    ) -> bool:
        return JobStore(self.state_store).cancel(
            job_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
        )

    def scheduler(self, *, worker_id: str | None = None) -> Scheduler:
        def run_job(job: dict) -> str:
            result = self.chat(
                job["prompt"],
                actor_id=job["actor_id"],
                role=job["role"],
                tenant_id=job["tenant_id"],
                replay_scope=f"scheduled-job:{job['id']}:{job['execution_key']}",
            )
            return result.final_answer or ""

        return Scheduler(
            self.state_store,
            run_job,
            worker_id=worker_id,
            lease_seconds=self.config.scheduler.lease_seconds,
        )

    def close(self) -> None:
        if self._delegation_runtime is not None:
            self._delegation_runtime.close()
        close = getattr(self.tools_provider, "close", None)
        if callable(close):
            close()
