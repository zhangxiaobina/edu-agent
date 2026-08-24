from __future__ import annotations

import contextlib
import inspect
import json
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from .agent.graph import _select_tool_manifest, run_agent
from .agent.loop_journal import frozen_route_shape, tool_manifest_hash
from .agent.prompts import SYSTEM_PROMPT
from .agent.turn_finalizer import FinalizationResult, TurnFinalizer
from .code_execution import build_code_execution_provider
from .engine import Engine, get_engine, is_provider_context_overflow
from .knowledge import KnowledgeToolProvider, SQLiteKnowledgeProvider
from .observability import RedactionPolicy, RunEventType, TraceRepository
from .planning.runtime import PlanningOptions
from .planning.runtime import PlanCoordinator
from .planning.verifier import EvidenceVerifier
from .runtime.config import AppConfig, load_config
from .runtime.budget import RunBudgetLedger, runtime_budget_limits
from .runtime.context import (
    ContextAccountant,
    ContextAccountingSession,
    ContextBudgetExceeded,
    ContextManager,
    ContextRouteLimits,
)
from .runtime.artifacts import ArtifactStore, ToolResultBudget
from .runtime.context_engine import CheckpointContextEngine, ContextEngine
from .runtime.cancellation import CancellationRequested, CancellationToken
from .runtime.models import BudgetExceeded, RunContext
from .runtime.manager import RuntimeManager
from .runtime.lifecycle import (
    LifecycleAdmission,
    LifecycleController,
    LifecycleStartupError,
    LifecycleState,
    ShutdownReport,
)
from .runtime.recovery import (
    RecoveryAction,
    RecoveryDecision,
    RecoveryManualReviewRequired,
    RunRecoveryPlanner,
)
from .runtime.tool_executor import ApprovalRequest, ExecutionPolicy, PolicyToolExecutor
from .scheduler import JobStore, Scheduler
from .state import MemoryManager, MemoryProvider, StateStorageError, StateStore
from .state import RunJournalIdentityError
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
        context_accountant: ContextAccountant | None = None,
        plan_generator=None,
        loop_fault_injector=None,
        finalizer_fault_injector=None,
        post_process_hooks=None,
        finalizer_cleanup=None,
        lifecycle_controller: LifecycleController | None = None,
    ):
        self.config = config or AppConfig()
        self.lifecycle = lifecycle_controller or LifecycleController(
            poll_interval_seconds=self.config.lifecycle.poll_interval_seconds,
        )
        self.engine = engine
        self.state_store = state_store or StateStore(self.config.state_path)
        self.lifecycle.set_audit_sink(self._record_lifecycle_transition)
        self.code_execution_provider = self._build_code_execution_provider()
        self._required_provider_healthy = True
        if self.code_execution_provider is not None:
            try:
                self._required_provider_healthy = bool(
                    self.code_execution_provider.health_check(force=True).healthy
                )
            except Exception:
                self._required_provider_healthy = False
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
        self.recovery_planner = RunRecoveryPlanner(
            self.state_store,
            self.tools_provider,
        )
        stalled_recovery = self.state_store.recover_stalled_runs(
            stall_timeout_seconds=self.config.runtime.run_stall_seconds,
        )
        shutdown_recovery = self.state_store.list_shutdown_recoverable_runs()
        known_recovery_ids = {item["run_id"] for item in stalled_recovery}
        self.recovery_report = self._startup_recovery_report(
            stalled_recovery
            + [
                item
                for item in shutdown_recovery
                if item["run_id"] not in known_recovery_ids
            ]
        )
        self.memory = memory_provider or MemoryManager(
            self.state_store,
            max_items=self.config.memory.max_recalled_items,
            max_item_chars=self.config.memory.max_item_chars,
        )
        self.context_accountant = context_accountant or ContextAccountant()
        self.budget_pricing = self.config.pricing.catalog()
        self.context_manager = ContextManager(
            token_budget=self.config.runtime.context_token_budget,
            recent_message_limit=self.config.runtime.recent_message_limit,
            accountant=self.context_accountant,
            output_reserve_tokens=self.config.runtime.output_token_reserve,
        )
        self._validate_context_route_limits()
        self.artifact_store = ArtifactStore(self.config.artifact_path, self.state_store)
        self.result_budget = ToolResultBudget(
            self.artifact_store,
            inline_chars=self.config.runtime.tool_result_inline_chars,
            preview_chars=self.config.runtime.tool_result_preview_chars,
            turn_budget_chars=self.config.runtime.tool_turn_budget_chars,
        )
        self.context_engine = context_engine or (
            CheckpointContextEngine(
                self.state_store,
                token_budget=self.config.runtime.context_token_budget,
                trigger_ratio=self.config.runtime.compression_trigger_ratio,
                release_ratio=self.config.runtime.compression_release_ratio,
                min_reclaim_tokens=self.config.runtime.compression_min_reclaim_tokens,
                cooldown_turns=self.config.runtime.compression_cooldown_turns,
                cooldown_seconds=self.config.runtime.compression_cooldown_seconds,
                keep_recent=self.config.runtime.compression_keep_recent,
                summary_max_chars=self.config.runtime.compression_summary_max_chars,
                result_budget=self.result_budget,
            )
            if self.config.runtime.compression_enabled
            else None
        )
        self.plan_generator = plan_generator
        self.loop_fault_injector = loop_fault_injector
        self.finalizer_fault_injector = finalizer_fault_injector
        self.post_process_hooks = post_process_hooks
        self.finalizer_cleanup = finalizer_cleanup
        self.trace_repository = TraceRepository(
            self.state_store,
            redaction=RedactionPolicy(),
        )
        self._delegation_runtime = None
        self._teaching_delegation = None
        self._shutdown_lock = threading.Lock()
        self._shutdown_report: ShutdownReport | None = None
        self._shutdown_hooks_lock = threading.Lock()
        self._shutdown_hooks: dict[str, Callable[[], None]] = {}
        self._resources_closed = False
        if hasattr(self.engine, "event_sink") and self.engine.event_sink is None:
            self.engine.event_sink = lambda event: self.state_store.record_provider_event(**event)
        self._refresh_lifecycle_health()
        try:
            self.lifecycle.complete_startup()
        except LifecycleStartupError:
            # The process remains observable in ``starting`` and can become
            # ready when a required local provider recovers.
            pass

    def _record_lifecycle_transition(self, event: Mapping[str, object]) -> None:
        runtime_manager = getattr(self, "runtime_manager", None)
        owner_id = getattr(runtime_manager, "owner_id", "initializing")
        self.state_store.record_audit_event(
            actor_id="system",
            tenant_id="system",
            action="process.lifecycle_transition",
            resource=f"process:{owner_id}",
            decision=str(event.get("to_state") or "unknown"),
            details={
                "sequence": event.get("sequence"),
                "from_state": event.get("from_state"),
                "reason": event.get("reason"),
            },
        )

    def _refresh_lifecycle_health(self) -> dict:
        provider_ready = self._required_provider_healthy
        if self.code_execution_provider is not None:
            try:
                provider_ready = bool(
                    self.code_execution_provider.health_check(force=False).healthy
                )
            except Exception:
                provider_ready = False
            self._required_provider_healthy = provider_ready
        tools_readiness = getattr(self.tools_provider, "readiness_check", None)
        if callable(tools_readiness):
            try:
                provider_ready = provider_ready and bool(tools_readiness())
            except Exception:
                provider_ready = False
        self.lifecycle.set_health(
            migration=self.state_store.migration_ready(),
            state_db_writable=self.state_store.writable_ready(),
            required_providers=provider_ready,
        )
        if self.lifecycle.state is LifecycleState.STARTING:
            try:
                self.lifecycle.complete_startup()
            except LifecycleStartupError:
                pass
        return self.lifecycle.health_snapshot()

    def health_snapshot(self) -> dict:
        return self._refresh_lifecycle_health()

    def liveness_snapshot(self) -> dict:
        """Return process-local liveness without touching external resources."""

        return self.lifecycle.health_snapshot()

    def _context_route_limits(
        self,
        routes=None,
    ) -> tuple[ContextRouteLimits, ...]:
        provider_routes = self._begin_turn_routes() if routes is None else tuple(routes)
        if not provider_routes:
            name = str(getattr(self.engine, "name", type(self.engine).__name__))
            model = str(getattr(self.engine, "model", name))
            return (ContextRouteLimits(provider=name, model=model),)
        resolver = getattr(self.engine, "capabilities_for_route", None)
        result = []
        for route in provider_routes:
            capabilities = resolver(route) if callable(resolver) else route.capabilities
            result.append(
                ContextRouteLimits(
                    provider=route.provider,
                    model=route.model,
                    context_window_tokens=capabilities.context_window_tokens,
                    max_output_tokens=capabilities.max_output_tokens,
                    tokenizer=capabilities.tokenizer,
                    route_identity=tuple(route.identity),
                )
            )
        return tuple(result)

    def _begin_turn_routes(self):
        freezer = getattr(self.engine, "begin_turn_routes", None)
        return tuple(freezer()) if callable(freezer) else ()

    def _validate_context_route_limits(self) -> None:
        context_budget = self.config.runtime.context_token_budget
        output_reserve = self.config.runtime.output_token_reserve
        for route in self._context_route_limits():
            if (
                route.context_window_tokens is not None
                and context_budget > route.context_window_tokens
            ):
                raise ValueError(
                    "runtime.context_token_budget cannot exceed effective Provider "
                    f"context capability for {route.provider}:{route.model}"
                )
            if (
                route.max_output_tokens is not None
                and output_reserve > route.max_output_tokens
            ):
                raise ValueError(
                    "runtime.output_token_reserve cannot exceed effective Provider "
                    f"output capability for {route.provider}:{route.model}"
                )

    def _context_accounting_session(
        self,
        context: RunContext,
        *,
        routes,
        tool_manifest_hash: str,
    ) -> ContextAccountingSession:
        def event_sink(event, route, sequence, details):
            self.state_store.record_provider_event(
                run_id=context.run_id,
                provider=route.provider,
                event=event,
                attempt=sequence,
                details=details,
            )

        session = ContextAccountingSession(
            self.context_accountant,
            routes=self._context_route_limits(routes),
            configured_context_limit_tokens=self.config.runtime.context_token_budget,
            max_output_reserve_tokens=self.config.runtime.output_token_reserve,
            event_sink=event_sink,
            tool_manifest_hash=tool_manifest_hash,
        )
        context.bind_context_accounting(session)
        return session

    def _build_code_execution_provider(self):
        return build_code_execution_provider(self.config.code_execution)

    def _startup_recovery_report(self, recovered: list[dict]) -> list[dict]:
        report: list[dict] = []
        for item in recovered:
            with self.state_store.connect() as connection:
                run = connection.execute(
                    "SELECT actor_id, tenant_id FROM runs WHERE id=?",
                    (item["run_id"],),
                ).fetchone()
            if run is None or not run["actor_id"] or not run["tenant_id"]:
                report.append(dict(item))
                continue
            decision = self.get_recovery_decision(
                item["run_id"],
                actor_id=run["actor_id"],
                tenant_id=run["tenant_id"],
            )
            self._record_recovery_decision(decision, source="startup")
            report.append({**item, "decision": decision.to_safe_dict()})
        return report

    def _record_recovery_decision(
        self,
        decision: RecoveryDecision,
        *,
        source: str,
    ) -> None:
        with self.state_store.connect() as connection:
            run = connection.execute(
                "SELECT actor_id, tenant_id FROM runs WHERE id=?",
                (decision.run_id,),
            ).fetchone()
        if run is None:
            return
        self.state_store.record_audit_event(
            actor_id=run["actor_id"],
            tenant_id=run["tenant_id"],
            action="run.recovery_decision",
            resource=f"run:{decision.run_id}",
            decision=decision.action.value,
            details={"source": source, **decision.to_safe_dict()},
        )

    def get_recovery_decision(
        self,
        run_id: str,
        *,
        actor_id: str,
        tenant_id: str = "default",
    ) -> RecoveryDecision:
        return self.recovery_planner.decide(
            run_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
        )

    def reserve_stream_event_sequence(
        self,
        *,
        actor_id: str,
        tenant_id: str,
        **fields,
    ) -> int:
        return self.state_store.reserve_run_event_sequence(
            actor_id=actor_id,
            tenant_id=tenant_id,
            **fields,
        )

    def _stream_sequence(
        self,
        run_id: str,
        *,
        actor_id: str,
        tenant_id: str,
    ) -> int:
        return self.state_store.get_run_event_sequence(
            run_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
        )

    def bind_terminal_replay_stream(
        self,
        stream_writer,
        run_id: str,
        *,
        actor_id: str,
        tenant_id: str,
    ) -> None:
        run = self.state_store.get_run_status(
            run_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
        )
        if run is None or run["status"] not in {"completed", "failed", "interrupted"}:
            raise RuntimeError("terminal replay stream requires a terminal run")
        stream_writer.bind(
            session_id=run["session_id"],
            fencing_token=0,
            sequence_start=self._stream_sequence(
                run_id,
                actor_id=actor_id,
                tenant_id=tenant_id,
            ),
            terminal_replay=True,
        )

    def _execution_policy(self) -> ExecutionPolicy:
        return ExecutionPolicy(
            require_write_approval=self.config.security.require_write_approval,
            allow_local_code_execution=(
                self.code_execution_provider is not None
                and self.config.code_execution.enabled
            ),
            enforce_roles=True,
            approval_ttl_seconds=self.config.transaction.approval_ttl_seconds,
        )

    def _model_supports_tool_calling(self) -> bool:
        """Read the frozen model route capability without assuming a Gateway."""

        resolver = getattr(self.engine, "effective_capabilities", None)
        if callable(resolver):
            capabilities = resolver()
            value = getattr(capabilities, "tool_calling", None)
            if value is not None:
                return bool(value)
        routes = self._begin_turn_routes()
        if routes:
            value = getattr(getattr(routes[0], "capabilities", None), "tool_calling", None)
            if value is not None:
                return bool(value)
        # Legacy/mock engines have no route declaration; preserve their old
        # tool-calling behavior and treat capability as known-by-contract.
        return True

    def _assert_recovery_runtime_identity(
        self,
        context: RunContext,
        decision: RecoveryDecision,
    ) -> None:
        if decision.tool_manifest_hash is None:
            return
        manifest = _select_tool_manifest(
            self.tools_provider,
            context,
            self._execution_policy(),
            model_tool_calling=self._model_supports_tool_calling(),
        )
        context.bind_tool_manifest(manifest)
        current_hashes = {manifest.manifest_hash}
        # Compatibility for R2 journals that hashed only the OpenAI schema
        # list.  New runs always persist the richer manifest hash.
        current_hashes.add(tool_manifest_hash(manifest.to_openai_tools()))
        if decision.tool_manifest_hash not in current_hashes:
            raise RunJournalIdentityError(
                "run tool manifest changed before recovery",
                run_id=context.run_id,
            )
        if decision.tool_manifest_hash != manifest.manifest_hash:
            # Keep the legacy journal identity for this resumed run while
            # making the compatibility decision explicit and auditable.  The
            # executor still receives the richer frozen manifest and checks
            # every live entry before dispatch.
            context._tool_manifest_hash_override = decision.tool_manifest_hash
            self.state_store.record_audit_event(
                actor_id=context.actor_id,
                tenant_id=context.tenant_id,
                action="tool_manifest.compatibility",
                resource=f"run:{context.run_id}",
                decision="legacy_schema_hash_accepted",
                details={
                    "legacy_hash": decision.tool_manifest_hash,
                    "canonical_manifest_hash": manifest.manifest_hash,
                },
            )
        route = RedactionPolicy().redact(frozen_route_shape(self.engine))
        if json.dumps(route, sort_keys=True, separators=(",", ":"), default=str) != json.dumps(
            decision.frozen_provider_route,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ):
            raise RunJournalIdentityError(
                "run provider route changed before recovery",
                run_id=context.run_id,
            )
        budget = decision.budget_snapshot or {}
        if context.budget.ledger is not None:
            persisted = context.budget.usage()
            journal_root = budget.get("root_run_id")
            if journal_root is not None and journal_root != persisted["root_run_id"]:
                raise RunJournalIdentityError(
                    "run budget root changed before recovery",
                    run_id=context.run_id,
                )
            return
        for field in (
            "model_calls",
            "max_model_calls",
            "tool_calls",
            "max_tool_calls",
        ):
            setattr(context.budget, field, int(budget[field]))

    def _bind_root_budget(
        self,
        context: RunContext,
        *,
        legacy_snapshot: Mapping | None = None,
    ) -> RunBudgetLedger:
        with self.state_store.connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM run_budget_ledgers WHERE root_run_id=?",
                (context.run_id,),
            ).fetchone() is not None
        if not exists and legacy_snapshot and legacy_snapshot.get("root_run_id"):
            raise RunJournalIdentityError(
                "run budget ledger is missing for a ledger-backed journal",
                run_id=context.run_id,
            )
        ledger = RunBudgetLedger(
            self.state_store,
            root_run_id=context.run_id,
            session_id=context.session_id,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
            limits=(
                None
                if exists
                else runtime_budget_limits(
                    self.config.runtime,
                    self.config.delegation,
                )
            ),
            pricing=self.budget_pricing,
        )
        if not exists and legacy_snapshot:
            model_calls = int(legacy_snapshot.get("model_calls", 0))
            tool_calls = int(legacy_snapshot.get("tool_calls", 0))
            if model_calls or tool_calls:
                operation_id = f"migration:{context.run_id}:legacy-budget"
                ledger.reserve(
                    operation_id,
                    owner_run_id=context.run_id,
                    kind="legacy_budget_import",
                    amount={"model_calls": model_calls, "tool_calls": tool_calls},
                    cost_known=False,
                    metadata={"component": "migration", "estimated": True},
                )
                ledger.commit(
                    operation_id,
                    actual={"model_calls": model_calls, "tool_calls": tool_calls},
                    usage_source="estimated",
                    cost_known=False,
                )
        context.budget.bind_ledger(ledger, owner_run_id=context.run_id)
        return ledger

    @staticmethod
    def _record_compression_budget(
        context: RunContext,
        *,
        operation_id: str,
        status: str,
    ) -> None:
        ledger = context.budget.ledger
        if ledger is None:
            return
        ledger.record_free_operation(
            operation_id,
            owner_run_id=context.run_id,
            kind="deterministic_compression",
            metadata={"component": "compression", "status": status},
        )

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
                pricing=self.budget_pricing,
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
        lifecycle_admission: LifecycleAdmission | None = None,
    ) -> ChatResult:
        owned_admission = lifecycle_admission is None
        admission = lifecycle_admission or self.lifecycle.admit("chat")
        self.lifecycle.assert_admission(admission)
        effective_run_id = run_id or uuid.uuid4().hex
        effective_token = cancellation_token or CancellationToken()

        def cancel_for_shutdown() -> None:
            effective_token.cancel(
                "process shutdown deadline exceeded",
                source="process_shutdown",
            )
            try:
                self.state_store.request_process_shutdown(
                    effective_run_id,
                    actor_id=actor_id,
                    tenant_id=tenant_id,
                )
            except Exception:
                pass

        unregister_cancel = admission.add_cancel_callback(cancel_for_shutdown)
        try:
            effective_token.checkpoint("service.chat.before_enqueue")
            return self._chat_accepted(
                message,
                actor_id=actor_id,
                role=role,
                tenant_id=tenant_id,
                course_ids=course_ids,
                session_id=session_id,
                run_id=effective_run_id,
                db_conn=db_conn,
                replay_scope=replay_scope,
                cancellation_token=effective_token,
                stream_writer=stream_writer,
            )
        finally:
            if effective_token.cancelled:
                cancel_for_shutdown()
            unregister_cancel()
            if cancellation_token is None:
                effective_token.close()
            if owned_admission:
                admission.close()

    def _chat_accepted(
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
        self._bind_root_budget(context)
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
                    sequence_start=self._stream_sequence(
                        context.run_id,
                        actor_id=actor_id,
                        tenant_id=tenant_id,
                    ),
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
            "breakdown": None,
            "accounting": [],
        }

    def _failure_context_payload(self, context: RunContext) -> dict:
        payload = self._default_turn_context()
        accounting = context.context_accounting
        if accounting is None:
            return payload
        records = accounting.records()
        payload["accounting"] = records
        for record in reversed(records):
            breakdown = record.get("breakdown")
            if isinstance(breakdown, dict):
                payload["breakdown"] = breakdown
                payload["estimated_tokens"] = int(
                    breakdown.get("estimated_input_tokens", 0)
                )
                payload["omitted_messages"] = int(
                    breakdown.get("omitted_messages", 0)
                )
                break
        return payload

    @staticmethod
    def _call_context_engine(method, *args, context: RunContext, **kwargs):
        """Pass run scope when supported while retaining the pre-R4.2 plugin API."""

        parameters = inspect.signature(method).parameters.values()
        accepts_context = any(
            parameter.name == "context"
            or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        accepts_var_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters
        )
        accepted_names = {
            parameter.name
            for parameter in parameters
            if parameter.kind
            in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
        }
        filtered = {
            key: value
            for key, value in kwargs.items()
            if accepts_var_kwargs or key in accepted_names
        }
        if accepts_context:
            filtered["context"] = context
        return method(*args, **filtered)

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
        if is_provider_context_overflow(error):
            return "context_overflow"
        if isinstance(error, BudgetExceeded):
            return error.stop_reason
        if isinstance(error, ContextBudgetExceeded):
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
        if isinstance(error, BudgetExceeded) and context.budget.ledger is not None:
            context.budget.ledger.set_stop_reason(error.stop_reason)
        existing_finalizer = self.state_store.get_turn_finalizer(
            context.run_id,
            session_id=context.session_id,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
        )
        if existing_finalizer is not None:
            return
        if context_payload is None:
            context_payload = self._failure_context_payload(context)
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
            context_payload = self._failure_context_payload(context)
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
        except StateStorageError:
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
        routes = self._begin_turn_routes()
        route_limits = self._context_route_limits(routes)
        for index, route in enumerate(routes):
            details = route.to_event()
            effective = route_limits[index]
            details["capabilities"] = {
                **details.get("capabilities", {}),
                "context_window_tokens": effective.context_window_tokens,
                "max_output_tokens": effective.max_output_tokens,
                "tokenizer": effective.tokenizer,
            }
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
            self._call_context_engine(
                self.context_engine.compact_if_needed,
                session_id,
                history,
                context=context,
            )
            if self.context_engine is not None and not resume
            else None
        )
        if self.context_engine is not None and not resume:
            self._record_compression_budget(
                context,
                operation_id=f"compression:{context.run_id}:initial:1",
                status=str(getattr(compaction, "decision", "completed")),
            )
        if compaction and compaction.compacted_messages:
            context.emit_run_event(
                RunEventType.CONTEXT_COMPACTED.value,
                {
                    "checkpoint_id": compaction.checkpoint_id,
                    "compacted_messages": compaction.compacted_messages,
                    "estimated_tokens_before": compaction.estimated_tokens_before,
                    "estimated_tokens_after": compaction.estimated_tokens_after,
                },
            )
        if compaction and (
            compaction.compacted_messages
            or getattr(compaction, "externalized_messages", 0)
        ):
            history = self.state_store.get_messages(
                session_id,
                limit=None,
            )
        checkpoint_summary = (
            self._call_context_engine(
                self.context_engine.checkpoint_summary,
                session_id,
                context=context,
            )
            if self.context_engine is not None
            else None
        )
        memory_snapshot = (
            self.memory.snapshot(context, message)
            if self.config.memory.enabled
            else None
        )
        policy = self._execution_policy()
        manifest = _select_tool_manifest(
            self.tools_provider,
            context,
            policy,
            model_tool_calling=self._model_supports_tool_calling(),
        )
        context.bind_tool_manifest(manifest)
        frozen_tools = manifest.to_openai_tools()
        accounting = self._context_accounting_session(
            context,
            routes=routes,
            tool_manifest_hash=manifest.manifest_hash,
        )
        snapshot = self.context_manager.prepare(
            system_prompt=self.system_prompt,
            history=history,
            user_message=message,
            memory_items=memory_snapshot.items if memory_snapshot else [],
            context_checkpoint=checkpoint_summary,
            tools=frozen_tools,
            accounting=accounting,
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
        executor = PolicyToolExecutor(
            self.tools_provider,
            policy=policy,
            approval_handler=self.approval_handler,
            state_store=self.state_store,
            result_budget=self.result_budget,
            manifest=manifest,
        )
        context_payload = {
            "estimated_tokens": snapshot.estimated_tokens,
            "omitted_messages": snapshot.omitted_messages,
            "memory_ids": memory_snapshot.ids if memory_snapshot else [],
            "checkpoint_id": compaction.checkpoint_id if compaction else None,
            "compacted_messages": compaction.compacted_messages if compaction else 0,
            "breakdown": (
                snapshot.breakdown.to_trace() if snapshot.breakdown is not None else None
            ),
            "accounting": accounting.records(),
        }

        frozen_route_json = json.dumps(
            frozen_route_shape(self.engine),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        recovery_attempted = False

        def _recovery_event_exists(event: str) -> bool:
            checker = getattr(self.state_store, "has_provider_event", None)
            if not callable(checker):
                return False
            try:
                return bool(
                    checker(
                        context.run_id,
                        event,
                        provider="runtime",
                        actor_id=context.actor_id,
                        tenant_id=context.tenant_id,
                    )
                )
            except TypeError:
                # Keep lightweight test stores and pre-R4.3 adapters usable;
                # the durable StateStore path above remains scope checked.
                return bool(checker(context.run_id, event, provider="runtime"))

        def _recovery_committed() -> bool:
            # ``compacted`` is retained as a durable alias for databases that
            # were written before the explicit committed event was introduced.
            if _recovery_event_exists(
                "context_overflow_recovery_committed"
            ) or _recovery_event_exists("context_overflow_recovery_compacted"):
                return True
            # ``compact_messages`` and the checkpoint policy marker commit in
            # one SQLite transaction.  This closes the narrow crash window
            # between that commit and recording the provider Trace event.
            try:
                checkpoint = self.state_store.latest_context_checkpoint(
                    session_id,
                    context=context,
                )
            except (AttributeError, TypeError):
                return False
            if not checkpoint or checkpoint.get("created_run_id") != context.run_id:
                checkpoint_committed = False
            else:
                checkpoint_committed = any(
                    isinstance(item, dict)
                    and item.get("type") == "compaction_policy"
                    and item.get("reason") == "provider_context_overflow"
                    for item in checkpoint.get("preserved_items", [])
                )
            if checkpoint_committed:
                return True
            artifact_checker = getattr(
                self.state_store,
                "has_context_overflow_artifact_externalization",
                None,
            )
            return bool(
                _recovery_event_exists("context_overflow_recovery_started")
                and callable(artifact_checker)
                and artifact_checker(
                    context.run_id,
                    session_id=session_id,
                    actor_id=context.actor_id,
                    tenant_id=context.tenant_id,
                )
            )

        recovery_attempted = _recovery_committed()
        force_recovery_model_attempt = recovery_attempted and not _recovery_event_exists(
            "context_overflow_recovery_retry_started"
        )

        def _without_persisted_protocol(all_history: list[dict]):
            """Remove this run's durable protocol before rebuilding a snapshot."""

            persisted = self.state_store.get_run_messages(context.run_id)
            if not persisted:
                return all_history, []
            protocol = [
                item
                for item in persisted
                if item.get("role") in {"assistant", "tool"}
            ]
            if len(all_history) >= len(persisted) and all_history[-len(persisted) :] == persisted:
                return all_history[: -len(persisted)], protocol
            count = len(persisted)
            start = next(
                (
                    index
                    for index in range(len(all_history) - count, -1, -1)
                    if all_history[index : index + count] == persisted
                ),
                None,
            )
            if start is None:
                raise RuntimeError("context recovery found an unordered run protocol")
            return [*all_history[:start], *all_history[start + count :]], protocol

        def _rebuild_after_compaction():
            refreshed_history = self.state_store.get_messages(session_id, limit=None)
            compact_history, protocol = _without_persisted_protocol(refreshed_history)
            refreshed_checkpoint = (
                self._call_context_engine(
                    self.context_engine.checkpoint_summary,
                    session_id,
                    context=context,
                )
                if self.context_engine is not None
                else None
            )
            refreshed_snapshot = self.context_manager.prepare(
                system_prompt=self.system_prompt,
                history=compact_history,
                user_message=message,
                memory_items=memory_snapshot.items if memory_snapshot else [],
                context_checkpoint=refreshed_checkpoint,
                tools=frozen_tools,
                accounting=accounting,
            )
            return refreshed_snapshot, [*refreshed_snapshot.messages, *protocol]

        def _run_agent_once():
            return run_agent(
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
                    if compaction is not None and compaction.checkpoint_id
                    else (
                        self.state_store.latest_context_checkpoint(
                            session_id,
                            context=context,
                        )
                        or {}
                    ).get("id")
                ),
                force_new_model_attempt=force_recovery_model_attempt,
                loop_fault_injector=self.loop_fault_injector,
                tool_manifest=manifest,
                tool_batch_max_workers=self.config.runtime.tool_batch_max_workers,
                tool_call_timeout_seconds=self.config.runtime.tool_call_timeout_seconds,
                context_accounting=accounting,
            )

        try:
            while True:
                try:
                    result = _run_agent_once()
                    break
                except Exception as provider_error:
                    marker_exists = _recovery_committed()
                    started_exists = _recovery_event_exists(
                        "context_overflow_recovery_started"
                    )
                    eligible = (
                        not recovery_attempted
                        and not marker_exists
                        and is_provider_context_overflow(provider_error)
                    )
                    if not eligible:
                        if (
                            recovery_attempted
                            and is_provider_context_overflow(provider_error)
                        ):
                            self.state_store.record_provider_event(
                                run_id=context.run_id,
                                provider="runtime",
                                event="context_overflow_recovery_exhausted",
                                attempt=context.budget.model_calls,
                                error_class=type(provider_error).__name__,
                                details={
                                    "failure_kind": "context_overflow",
                                    "recovery_attempts": 1,
                                    "fallback": False,
                                },
                            )
                        raise
                    recovery_attempted = True
                    force_recovery_model_attempt = True
                    context.check_control("context_overflow.recovery.before")
                    current_route_json = json.dumps(
                        frozen_route_shape(self.engine),
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    )
                    if current_route_json != frozen_route_json:
                        raise RuntimeError("provider route changed before context overflow recovery")
                    if not started_exists:
                        self.state_store.record_provider_event(
                            run_id=context.run_id,
                            provider="runtime",
                            event="context_overflow_recovery_started",
                            attempt=context.budget.model_calls,
                            error_class=type(provider_error).__name__,
                            details={
                                "failure_kind": "context_overflow",
                                "route": frozen_route_shape(self.engine),
                                "recovery_attempt": 1,
                            },
                        )
                    else:
                        self.state_store.record_provider_event(
                            run_id=context.run_id,
                            provider="runtime",
                            event="context_overflow_recovery_resumed",
                            attempt=context.budget.model_calls,
                            error_class=type(provider_error).__name__,
                            details={
                                "failure_kind": "context_overflow",
                                "route": frozen_route_shape(self.engine),
                                "recovery_attempt": 1,
                            },
                        )
                    recount = accounting.measure(
                        messages=agent_messages,
                        tools=frozen_tools,
                        phase="context_overflow_recount",
                        event="context_overflow_recount",
                    )
                    self.state_store.record_provider_event(
                        run_id=context.run_id,
                        provider="runtime",
                        event="context_overflow_recovery_recounted",
                        attempt=context.budget.model_calls,
                        details={
                            "estimated_input_tokens": recount.estimated_input_tokens,
                            "total_reserved_tokens": recount.total_reserved_tokens,
                            "decision": recount.decision,
                        },
                    )
                    compaction = (
                        self._call_context_engine(
                            self.context_engine.compact_if_needed,
                            session_id,
                            self.state_store.get_messages(session_id, limit=None),
                            context=context,
                            force=True,
                            reason="provider_context_overflow",
                        )
                        if self.context_engine is not None
                        else None
                    )
                    if self.context_engine is not None:
                        self._record_compression_budget(
                            context,
                            operation_id=f"compression:{context.run_id}:overflow:1",
                            status=str(getattr(compaction, "decision", "completed")),
                        )
                    if compaction is None or not (
                        compaction.compacted_messages
                        or getattr(compaction, "externalized_messages", 0)
                    ):
                        self.state_store.record_provider_event(
                            run_id=context.run_id,
                            provider="runtime",
                            event="context_overflow_recovery_unavailable",
                            attempt=context.budget.model_calls,
                            details={
                                "decision": getattr(compaction, "decision", "no_engine"),
                                "compacted_messages": getattr(
                                    compaction, "compacted_messages", 0
                                ),
                                "externalized_messages": getattr(
                                    compaction, "externalized_messages", 0
                                ),
                            },
                        )
                        raise
                    # The checkpoint or Artifact replacement is the durable
                    # recovery boundary.  Once this event is committed, a
                    # later owner must not compact again before the retry.
                    self.state_store.record_provider_event(
                        run_id=context.run_id,
                        provider="runtime",
                        event="context_overflow_recovery_committed",
                        attempt=context.budget.model_calls,
                        details={
                            "checkpoint_id": compaction.checkpoint_id,
                            "compacted_messages": compaction.compacted_messages,
                            "externalized_messages": getattr(
                                compaction, "externalized_messages", 0
                            ),
                            "same_route": True,
                            "recovery_attempt": 1,
                        },
                    )
                    context.check_control("context_overflow.recovery.after_compaction")
                    context.emit_run_event(
                        RunEventType.CONTEXT_COMPACTED.value,
                        {
                            "checkpoint_id": compaction.checkpoint_id,
                            "compacted_messages": compaction.compacted_messages,
                            "externalized_messages": getattr(
                                compaction, "externalized_messages", 0
                            ),
                            "estimated_tokens_before": compaction.estimated_tokens_before,
                            "estimated_tokens_after": compaction.estimated_tokens_after,
                            "reclaimed_tokens": getattr(compaction, "reclaimed_tokens", 0),
                            "reason": "provider_context_overflow",
                            "recovery_attempt": 1,
                        },
                    )
                    snapshot, agent_messages = _rebuild_after_compaction()
                    self.state_store.start_run(
                        run_id=context.run_id,
                        session_id=session_id,
                        model=getattr(self.engine, "name", type(self.engine).__name__),
                        context_tokens=snapshot.estimated_tokens,
                        omitted_messages=snapshot.omitted_messages,
                        context=context,
                    )
                    context_payload.update(
                        {
                            "estimated_tokens": snapshot.estimated_tokens,
                            "omitted_messages": snapshot.omitted_messages,
                            "checkpoint_id": compaction.checkpoint_id,
                            "compacted_messages": compaction.compacted_messages,
                            "externalized_messages": getattr(
                                compaction, "externalized_messages", 0
                            ),
                            "breakdown": (
                                snapshot.breakdown.to_trace()
                                if snapshot.breakdown is not None
                                else None
                            ),
                            "recovery": {
                                "attempted": True,
                                "failure_kind": "context_overflow",
                                "recount_estimated_tokens": recount.estimated_input_tokens,
                                "checkpoint_id": compaction.checkpoint_id,
                            },
                        }
                    )
                    self.state_store.record_provider_event(
                        run_id=context.run_id,
                        provider="runtime",
                        event="context_overflow_recovery_compacted",
                        attempt=context.budget.model_calls,
                        details={
                            "checkpoint_id": compaction.checkpoint_id,
                            "compacted_messages": compaction.compacted_messages,
                            "externalized_messages": getattr(
                                compaction, "externalized_messages", 0
                            ),
                            "estimated_tokens_before": compaction.estimated_tokens_before,
                            "estimated_tokens_after": compaction.estimated_tokens_after,
                            "same_route": True,
                        },
                    )
                    # The loop has exactly one eligible transition.  A second
                    # overflow reaches the outer failure path unchanged.
                    continue
            if recovery_attempted:
                self.state_store.record_provider_event(
                    run_id=context.run_id,
                    provider="runtime",
                    event="context_overflow_recovery_succeeded",
                    attempt=context.budget.model_calls,
                    details={"recovery_attempts": 1, "same_route": True},
                )
            context_payload["accounting"] = accounting.records()
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
            context_payload["accounting"] = accounting.records()
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
        except StateStorageError:
            raise
        except Exception as error:
            context_payload["accounting"] = accounting.records()
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
        lifecycle_admission: LifecycleAdmission | None = None,
    ) -> ChatResult:
        owned_admission = lifecycle_admission is None
        admission = lifecycle_admission or self.lifecycle.admit("run.resume")
        self.lifecycle.assert_admission(admission)
        effective_token = cancellation_token or CancellationToken()

        def cancel_for_shutdown() -> None:
            effective_token.cancel(
                "process shutdown deadline exceeded",
                source="process_shutdown",
            )
            try:
                self.state_store.request_process_shutdown(
                    run_id,
                    actor_id=actor_id,
                    tenant_id=tenant_id,
                )
            except Exception:
                pass

        unregister_cancel = admission.add_cancel_callback(cancel_for_shutdown)
        try:
            effective_token.checkpoint("service.resume.before_prepare")
            return self._resume_run_accepted(
                run_id,
                actor_id=actor_id,
                tenant_id=tenant_id,
                db_conn=db_conn,
                cancellation_token=effective_token,
                stream_writer=stream_writer,
            )
        finally:
            if effective_token.cancelled:
                cancel_for_shutdown()
            unregister_cancel()
            if cancellation_token is None:
                effective_token.close()
            if owned_admission:
                admission.close()

    def _resume_run_accepted(
        self,
        run_id: str,
        *,
        actor_id: str,
        tenant_id: str = "default",
        db_conn=None,
        cancellation_token: CancellationToken | None = None,
        stream_writer=None,
    ) -> ChatResult:
        decision = self.get_recovery_decision(
            run_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
        )
        self._record_recovery_decision(decision, source="resume")
        if decision.action is RecoveryAction.MANUAL_REVIEW:
            raise RecoveryManualReviewRequired(decision)
        if decision.action is RecoveryAction.TERMINAL_REPLAY:
            if stream_writer is not None:
                self.bind_terminal_replay_stream(
                    stream_writer,
                    run_id,
                    actor_id=actor_id,
                    tenant_id=tenant_id,
                )
                stream_writer.publish(
                    RunEventType.RUN_PHASE,
                    {"phase": "accepted", "status": "replaying"},
                )
            return self.recover_chat_result(
                run_id,
                actor_id=actor_id,
                tenant_id=tenant_id,
            )
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
        self._bind_root_budget(context, legacy_snapshot=decision.budget_snapshot)
        self._assert_recovery_runtime_identity(context, decision)
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
                    sequence_start=self._stream_sequence(
                        run_id,
                        actor_id=actor_id,
                        tenant_id=tenant_id,
                    ),
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
                self._bind_root_budget(context, legacy_snapshot=finalizer.budget)
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
        lifecycle_admission: LifecycleAdmission | None = None,
    ) -> ChatResult:
        owned_admission = lifecycle_admission is None
        admission = lifecycle_admission or self.lifecycle.admit("api.run.resume")
        self.lifecycle.assert_admission(admission)
        effective_token = cancellation_token or CancellationToken()

        def cancel_for_shutdown() -> None:
            effective_token.cancel(
                "process shutdown deadline exceeded",
                source="process_shutdown",
            )
            try:
                self.state_store.request_process_shutdown(
                    run_id,
                    actor_id=actor_id,
                    tenant_id=tenant_id,
                )
            except Exception:
                pass

        unregister_cancel = admission.add_cancel_callback(cancel_for_shutdown)
        try:
            effective_token.checkpoint("service.api_resume.before_prepare")
            return self._resume_api_run_accepted(
                run_id,
                actor_id=actor_id,
                tenant_id=tenant_id,
                cancellation_token=effective_token,
                stream_writer=stream_writer,
                lifecycle_admission=admission,
            )
        finally:
            if effective_token.cancelled:
                cancel_for_shutdown()
            unregister_cancel()
            if cancellation_token is None:
                effective_token.close()
            if owned_admission:
                admission.close()

    def _resume_api_run_accepted(
        self,
        run_id: str,
        *,
        actor_id: str,
        tenant_id: str = "default",
        cancellation_token: CancellationToken | None = None,
        stream_writer=None,
        lifecycle_admission: LifecycleAdmission | None = None,
    ) -> ChatResult:
        """Resume a claimed queued/abandoned request using persistent run state."""
        if lifecycle_admission is not None:
            self.lifecycle.assert_admission(lifecycle_admission)
        record = self.state_store.get_run_status(run_id, actor_id=actor_id, tenant_id=tenant_id)
        if record is None:
            raise KeyError(f"run 不存在：{run_id}")
        decision = self.get_recovery_decision(
            run_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
        )
        if record["status"] != "abandoned":
            self._record_recovery_decision(decision, source="api-resume")
        if decision.action is RecoveryAction.MANUAL_REVIEW:
            raise RecoveryManualReviewRequired(decision)
        if record["status"] == "abandoned":
            return self.resume_run(
                run_id,
                actor_id=actor_id,
                tenant_id=tenant_id,
                cancellation_token=cancellation_token,
                stream_writer=stream_writer,
                lifecycle_admission=lifecycle_admission,
            )
        if record["status"] != "queued":
            if record["status"] in {"completed", "failed", "interrupted"}:
                if stream_writer is not None:
                    self.bind_terminal_replay_stream(
                        stream_writer,
                        run_id,
                        actor_id=actor_id,
                        tenant_id=tenant_id,
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
                    lifecycle_admission=lifecycle_admission,
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
        self._bind_root_budget(context, legacy_snapshot=decision.budget_snapshot)
        self._assert_recovery_runtime_identity(context, decision)
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
                    sequence_start=self._stream_sequence(
                        run_id,
                        actor_id=actor_id,
                        tenant_id=tenant_id,
                    ),
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
        lifecycle_admission: LifecycleAdmission | None = None,
    ) -> str:
        owned_admission = lifecycle_admission is None
        admission = lifecycle_admission or self.lifecycle.admit("schedule.create")
        self.lifecycle.assert_admission(admission)
        try:
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
        finally:
            if owned_admission:
                admission.close()

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
        def run_job(
            job: dict,
            *,
            cancellation_token: CancellationToken | None = None,
            lifecycle_admission: LifecycleAdmission | None = None,
        ) -> str:
            result = self.chat(
                job["prompt"],
                actor_id=job["actor_id"],
                role=job["role"],
                tenant_id=job["tenant_id"],
                replay_scope=f"scheduled-job:{job['id']}:{job['execution_key']}",
                cancellation_token=cancellation_token,
                lifecycle_admission=lifecycle_admission,
            )
            return result.final_answer or ""

        return Scheduler(
            self.state_store,
            run_job,
            worker_id=worker_id,
            lease_seconds=self.config.scheduler.lease_seconds,
            lifecycle=self.lifecycle,
        )

    def register_shutdown_hook(self, name: str, callback: Callable[[], None]) -> None:
        if not isinstance(name, str) or not name.strip() or not callable(callback):
            raise ValueError("shutdown hook requires a stable name and callable")
        with self._shutdown_hooks_lock:
            current = self._shutdown_hooks.get(name)
            if current is not None and current is not callback:
                raise ValueError(f"shutdown hook already registered: {name}")
            self._shutdown_hooks[name] = callback

    @staticmethod
    def _bounded_call(call: Callable[[], object], timeout: float) -> tuple[bool, Exception | None]:
        completed = threading.Event()
        errors: list[Exception] = []

        def invoke() -> None:
            try:
                call()
            except Exception as error:
                errors.append(error)
            finally:
                completed.set()

        worker = threading.Thread(
            target=invoke,
            name="edu-agent-shutdown-hook",
            daemon=True,
        )
        worker.start()
        if not completed.wait(max(0.001, float(timeout))):
            return False, None
        return not errors, errors[0] if errors else None

    def _interrupt_blocking_resources(self, timeout: float) -> None:
        close = getattr(self.tools_provider, "close", None)
        if callable(close):
            self._bounded_call(close, timeout)

    def _close_resources(self) -> None:
        with self._shutdown_hooks_lock:
            if self._resources_closed:
                return
            self._resources_closed = True
        if self._delegation_runtime is not None:
            self._delegation_runtime.close(wait=False)
        close = getattr(self.tools_provider, "close", None)
        if callable(close):
            close()

    def shutdown(
        self,
        *,
        deadline_seconds: float | None = None,
        reason: str = "explicit_shutdown",
    ) -> ShutdownReport:
        configured = self.config.lifecycle
        timeout = float(
            configured.shutdown_deadline_seconds
            if deadline_seconds is None
            else deadline_seconds
        )
        if timeout <= 0:
            raise ValueError("shutdown deadline must be positive")
        with self._shutdown_lock:
            if self._shutdown_report is not None:
                return self._shutdown_report
            started = self.lifecycle.now()
            hard_deadline = started + timeout
            flush_reserve = min(configured.final_flush_seconds, timeout * 0.25)
            cancel_reserve = min(configured.cancellation_grace_seconds, timeout * 0.5)
            graceful_deadline = max(started, hard_deadline - flush_reserve - cancel_reserve)
            self.lifecycle.begin_draining(reason)

            normal_drained = self.lifecycle.wait_for_idle(
                graceful_deadline,
                extra_active=self.runtime_manager.active_count,
            )
            cancellation_requested = False
            recoverable: list[dict] = []
            if not normal_drained:
                cancellation_requested = True
                self.lifecycle.cancel_active()
                self.runtime_manager.cancel_all()
                self._interrupt_blocking_resources(
                    min(cancel_reserve, max(0.001, hard_deadline - self.lifecycle.now()))
                )
                cancel_deadline = max(self.lifecycle.now(), hard_deadline - flush_reserve)
                completed_after_cancel = self.lifecycle.wait_for_idle(
                    cancel_deadline,
                    extra_active=self.runtime_manager.active_count,
                )
                if not completed_after_cancel:
                    recoverable = self.state_store.mark_owner_runs_recoverable(
                        owner_id=self.runtime_manager.owner_id,
                    )

            marked_ids = {item["run_id"] for item in recoverable}
            recoverable.extend(
                item
                for item in self.state_store.mark_owner_runs_recoverable(
                    owner_id=self.runtime_manager.owner_id,
                )
                if item["run_id"] not in marked_ids
            )

            flush_timeout = max(0.001, hard_deadline - self.lifecycle.now())
            flush_succeeded, flush_error = self._bounded_call(
                self.state_store.flush,
                flush_timeout,
            )
            flush_timed_out = not flush_succeeded and flush_error is None

            with self._shutdown_hooks_lock:
                hooks = tuple(self._shutdown_hooks.items())
            hooks = (("service.resources", self._close_resources),) + hooks
            close_failures: list[str] = []
            for name, hook in hooks:
                remaining = max(0.001, hard_deadline - self.lifecycle.now())
                succeeded, _ = self._bounded_call(hook, remaining)
                if not succeeded:
                    close_failures.append(name)

            self.lifecycle.mark_stopped("shutdown_complete")
            active_remaining = max(
                self.lifecycle.active_count(),
                self.runtime_manager.active_count(),
            )
            self._shutdown_report = ShutdownReport(
                state=self.lifecycle.state.value,
                normal_drained=normal_drained and not recoverable,
                cancellation_requested=cancellation_requested,
                recoverable_runs=len(recoverable),
                active_remaining=active_remaining,
                flush_succeeded=flush_succeeded,
                flush_timed_out=flush_timed_out,
                resource_close_failures=tuple(close_failures),
                elapsed_seconds=max(0.0, self.lifecycle.now() - started),
            )
            return self._shutdown_report

    def close(self) -> ShutdownReport | None:
        self._close_resources()
        return self._shutdown_report
