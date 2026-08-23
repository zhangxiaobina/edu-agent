from __future__ import annotations

import hashlib
import inspect
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..data import db
from ..state.store import FencingTokenRejected, RunCancelled
from .cancellation import CancellationRequested
from .models import BudgetExceeded, RunContext
from .security import redact_sensitive
from .tool_arguments import (
    RepairAudit,
    ToolArgumentError,
    normalize_tool_arguments,
    redact_classified_arguments,
    strict_parse_tool_arguments,
    summarize_raw_arguments,
    validate_tool_arguments,
)
from .transactions import (
    IdempotencyConflict,
    OperationUnavailable,
    TransactionalToolRuntime,
    approval_scope,
    idempotency_key,
    payload_hash,
)
from ..tools.manifest import (
    ToolEffect,
    ToolManifest,
    ToolManifestMismatch,
    manifest_entry_matches,
)
from ..teaching import TeachingProviderErrorKind, TeachingProviderRejected


@dataclass(frozen=True)
class ApprovalRequest:
    run_id: str
    session_id: str
    actor_id: str
    tool_call_id: str | None
    operation_id: str
    tool_name: str
    arguments: dict
    payload_hash: str
    scope: str
    expires_at: str
    risk_level: str
    reason: str


ApprovalHandler = Callable[[ApprovalRequest], bool]


@dataclass(frozen=True)
class ExecutionPolicy:
    require_write_approval: bool = True
    require_code_execution_approval: bool = True
    allow_local_code_execution: bool = False
    enforce_roles: bool = True
    approval_ttl_seconds: int = 900

    @classmethod
    def legacy_demo(cls) -> ExecutionPolicy:
        return cls(
            require_write_approval=False,
            require_code_execution_approval=False,
            allow_local_code_execution=True,
            enforce_roles=False,
        )


@dataclass
class ToolResult:
    """Stable result crossing every ToolProvider -> Agent boundary."""

    ok: bool
    data: Any = None
    error: dict | None = None
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "data": self.data,
            "error": self.error,
            "meta": self.meta,
        }


# Backward-compatible name used by the pre-R3 runtime and public callers.
ToolOutcome = ToolResult


def _validate_value(value: Any, schema: dict, path: str) -> list[str]:
    """Compatibility wrapper over the complete JSON Schema validator."""

    issues = validate_tool_arguments(value, schema)
    prefix = "" if path == "arguments" else path
    return [f"{prefix}{issue['message']}" for issue in issues]


class PolicyToolExecutor:
    def __init__(
        self,
        provider,
        *,
        policy: ExecutionPolicy | None = None,
        approval_handler: ApprovalHandler | None = None,
        state_store=None,
        result_budget=None,
        transaction_runtime: TransactionalToolRuntime | None = None,
        manifest: ToolManifest | None = None,
        defer_result_commit: bool = False,
    ):
        self.provider = provider
        self.policy = policy or ExecutionPolicy()
        self.approval_handler = approval_handler
        self.state_store = state_store
        self.result_budget = result_budget
        self.transaction_runtime = transaction_runtime or TransactionalToolRuntime(
            state_store=state_store
        )
        self.manifest = manifest
        self.defer_result_commit = bool(defer_result_commit)

    def deferred_worker(self) -> PolicyToolExecutor:
        """Create an execution view whose candidate result needs coordinator acceptance."""

        return PolicyToolExecutor(
            self.provider,
            policy=self.policy,
            approval_handler=self.approval_handler,
            state_store=self.state_store,
            result_budget=None,
            transaction_runtime=self.transaction_runtime,
            manifest=self.manifest,
            defer_result_commit=True,
        )

    @staticmethod
    def _argument_pipeline_meta(
        context: RunContext,
        *,
        tool_call_id: str | None,
        repairs: tuple[RepairAudit, ...],
        consume_retry: bool,
    ) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        if repairs:
            meta["argument_repairs"] = [repair.to_dict() for repair in repairs]
        if consume_retry:
            consumed = context.consume_argument_retry_budget(tool_call_id)
            meta["argument_retry"] = {
                "consumed": int(consumed),
                "used_for_call": 1,
                "max_per_call": 1,
            }
        return meta

    def _record_argument_repairs(
        self,
        context: RunContext,
        *,
        name: str,
        tool_call_id: str | None,
        repairs: tuple[RepairAudit, ...],
    ) -> None:
        if self.state_store is None or not repairs:
            return
        results = {repair.result for repair in repairs}
        decision = (
            "applied"
            if results == {"applied"}
            else "rejected"
            if "applied" not in results
            else "partial"
        )
        self.state_store.record_audit_event(
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
            action="tool.argument_repair",
            resource=name,
            decision=decision,
            details={
                "run_id": context.run_id,
                "tool_call_id": tool_call_id,
                "repairs": [repair.to_dict() for repair in repairs],
            },
        )

    def execute_raw(
        self,
        name: str,
        raw_arguments: str | dict | None,
        context: RunContext,
        conn=None,
        *,
        allowed_tools: set[str] | None = None,
        tool_call_id: str | None = None,
        plan_step_id: str | None = None,
        caller_idempotency_key: str | None = None,
        manifest: ToolManifest | None = None,
        budget_reserved: bool = False,
    ) -> ToolOutcome:
        frozen_manifest = (
            manifest
            if manifest is not None
            else self.manifest
            if self.manifest is not None
            else context.tool_manifest
        )
        started = time.monotonic()
        raw_summary = {"_raw": summarize_raw_arguments(raw_arguments)}
        if frozen_manifest is not None:
            if hasattr(frozen_manifest, "matches_context") and not frozen_manifest.matches_context(context):
                return self._finish(
                    context,
                    name,
                    raw_summary,
                    started,
                    ToolOutcome(
                        False,
                        error={
                            "code": "TOOL_MANIFEST_MISMATCH",
                            "message": "工具 manifest 与当前 actor/tenant/role/course 作用域不一致",
                        },
                    ),
                    tool_call_id=tool_call_id,
                )
            try:
                spec = self._spec(name, manifest=frozen_manifest)
            except ToolManifestMismatch as error:
                return self._finish(
                    context,
                    name,
                    raw_summary,
                    started,
                    ToolOutcome(
                        False,
                        error={"code": "TOOL_MANIFEST_MISMATCH", "message": str(error)},
                    ),
                    tool_call_id=tool_call_id,
                )
            if spec is None:
                return self._finish(
                    context,
                    name,
                    raw_summary,
                    started,
                    ToolOutcome(
                        False,
                        error={
                            "code": "TOOL_NOT_IN_MANIFEST",
                            "message": f"工具 {name} 不在本 run 冻结的工具面内",
                        },
                    ),
                    tool_call_id=tool_call_id,
                )
        try:
            arguments = strict_parse_tool_arguments(raw_arguments)
        except ToolArgumentError as error:
            context.check_control("tool.before_call")
            if not budget_reserved:
                try:
                    context.budget.consume_tool_call()
                except BudgetExceeded as budget_error:
                    return self._finish(
                        context,
                        name,
                        raw_summary,
                        started,
                        ToolOutcome(
                            False,
                            error={"code": "BUDGET_EXCEEDED", "message": str(budget_error)},
                        ),
                        tool_call_id=tool_call_id,
                    )
            argument_meta = self._argument_pipeline_meta(
                context,
                tool_call_id=tool_call_id,
                repairs=(),
                consume_retry=True,
            )
            return self._finish(
                context,
                name,
                raw_summary,
                started,
                ToolOutcome(False, error=error.to_error()),
                tool_call_id=tool_call_id,
                argument_meta=argument_meta,
            )
        return self.execute(
            name,
            arguments,
            context,
            conn=conn,
            allowed_tools=allowed_tools,
            tool_call_id=tool_call_id,
            plan_step_id=plan_step_id,
            caller_idempotency_key=caller_idempotency_key,
            manifest=frozen_manifest,
            _parsed=True,
            _budget_reserved=budget_reserved,
        )

    def execute(
        self,
        name: str,
        arguments: dict,
        context: RunContext,
        conn=None,
        *,
        allowed_tools: set[str] | None = None,
        tool_call_id: str | None = None,
        plan_step_id: str | None = None,
        caller_idempotency_key: str | None = None,
        manifest: ToolManifest | None = None,
        _parsed: bool = False,
        _budget_reserved: bool = False,
    ) -> ToolOutcome:
        started = time.monotonic()
        unvalidated_summary = {"_raw": summarize_raw_arguments(arguments)}
        context.check_control("tool.before_call")
        if not _budget_reserved:
            try:
                context.budget.consume_tool_call()
            except BudgetExceeded as error:
                return self._finish(
                    context,
                    name,
                    unvalidated_summary,
                    started,
                    ToolOutcome(False, error={"code": "BUDGET_EXCEEDED", "message": str(error)}),
                    tool_call_id=tool_call_id,
                )
        if allowed_tools is not None and name not in allowed_tools:
            return self._finish(
                context,
                name,
                unvalidated_summary,
                started,
                ToolOutcome(
                    False,
                    error={
                        "code": "PLAN_SCOPE_DENIED",
                        "message": f"工具 {name} 不在当前计划步骤的允许范围内",
                    },
                ),
                tool_call_id=tool_call_id,
            )
        frozen_manifest = (
            manifest
            if manifest is not None
            else self.manifest
            if self.manifest is not None
            else context.tool_manifest
        )
        if frozen_manifest is not None and hasattr(frozen_manifest, "matches_context"):
            if not frozen_manifest.matches_context(context):
                return self._finish(
                    context,
                    name,
                    unvalidated_summary,
                    started,
                    ToolOutcome(
                        False,
                        error={
                            "code": "TOOL_MANIFEST_MISMATCH",
                            "message": "工具 manifest 与当前 actor/tenant/role/course 作用域不一致",
                        },
                    ),
                    tool_call_id=tool_call_id,
                )
        if frozen_manifest is not None and not frozen_manifest.contains(name):
            return self._finish(
                context,
                name,
                unvalidated_summary,
                started,
                ToolOutcome(
                    False,
                    error={
                        "code": "TOOL_NOT_IN_MANIFEST",
                        "message": f"工具 {name} 不在本 run 冻结的工具面内",
                    },
                ),
                tool_call_id=tool_call_id,
            )
        try:
            spec = self._spec(name, manifest=frozen_manifest)
        except ToolManifestMismatch as error:
            return self._finish(
                context,
                name,
                unvalidated_summary,
                started,
                ToolOutcome(
                    False,
                    error={"code": "TOOL_MANIFEST_MISMATCH", "message": str(error)},
                ),
                tool_call_id=tool_call_id,
            )
        if spec is None:
            return self._finish(
                context,
                name,
                unvalidated_summary,
                started,
                ToolOutcome(
                    False,
                    error={"code": "UNKNOWN_TOOL", "message": f"未知工具：{name}"},
                ),
                tool_call_id=tool_call_id,
            )
        if not _parsed:
            try:
                arguments = strict_parse_tool_arguments(arguments)
            except ToolArgumentError as error:
                argument_meta = self._argument_pipeline_meta(
                    context,
                    tool_call_id=tool_call_id,
                    repairs=(),
                    consume_retry=True,
                )
                return self._finish(
                    context,
                    name,
                    unvalidated_summary,
                    started,
                    ToolOutcome(False, error=error.to_error()),
                    tool_call_id=tool_call_id,
                    argument_meta=argument_meta,
                    data_classification=getattr(spec, "data_classification", {}),
                )
        parameter_schema = spec.schema.get("parameters", {})
        data_classification = getattr(spec, "data_classification", {})
        protected_pointers = tuple(
            f"/{key}" for key in getattr(spec, "mutation_parameters", ())
        )
        try:
            arguments, repairs = normalize_tool_arguments(
                arguments,
                parameter_schema,
                effect=spec.effect,
                data_classification=data_classification,
                protected_pointers=protected_pointers,
            )
        except ToolArgumentError as error:
            repairs = error.repair_audits
            argument_meta = self._argument_pipeline_meta(
                context,
                tool_call_id=tool_call_id,
                repairs=repairs,
                consume_retry=True,
            )
            self._record_argument_repairs(
                context,
                name=name,
                tool_call_id=tool_call_id,
                repairs=repairs,
            )
            return self._finish(
                context,
                name,
                {"_raw": summarize_raw_arguments(arguments)},
                started,
                ToolOutcome(False, error=error.to_error()),
                tool_call_id=tool_call_id,
                argument_meta=argument_meta,
                data_classification=data_classification,
            )
        validation_issues = validate_tool_arguments(arguments, parameter_schema)
        argument_meta = self._argument_pipeline_meta(
            context,
            tool_call_id=tool_call_id,
            repairs=repairs,
            consume_retry=bool(repairs or validation_issues),
        )
        self._record_argument_repairs(
            context,
            name=name,
            tool_call_id=tool_call_id,
            repairs=repairs,
        )
        if validation_issues:
            return self._finish(
                context,
                name,
                {"_raw": summarize_raw_arguments(arguments)},
                started,
                ToolOutcome(
                    False,
                    error={
                        "code": "INVALID_ARGUMENTS",
                        "message": "; ".join(issue["message"] for issue in validation_issues),
                        "details": {"issues": [dict(issue) for issue in validation_issues]},
                    },
                ),
                tool_call_id=tool_call_id,
                argument_meta=argument_meta,
                data_classification=data_classification,
            )
        if self.policy.enforce_roles and context.role not in spec.allowed_roles:
            return self._finish(
                context,
                name,
                arguments,
                started,
                ToolOutcome(
                    False,
                    error={
                        "code": "FORBIDDEN",
                        "message": f"角色 {context.role} 无权调用 {name}",
                    },
                ),
                tool_call_id=tool_call_id,
                argument_meta=argument_meta,
                data_classification=data_classification,
            )
        course_id = arguments.get("course_id")
        if (
            self.policy.enforce_roles
            and context.role not in {"admin", "system"}
            and context.course_ids
            and course_id is not None
            and int(course_id) not in context.course_ids
        ):
            return self._finish(
                context,
                name,
                arguments,
                started,
                ToolOutcome(
                    False,
                    error={
                        "code": "COURSE_SCOPE_DENIED",
                        "message": f"当前身份无权访问课程 {course_id}",
                    },
                ),
                tool_call_id=tool_call_id,
                argument_meta=argument_meta,
                data_classification=data_classification,
            )
        if spec.effect is ToolEffect.CODE_EXECUTION and not self.policy.allow_local_code_execution:
            return self._finish(
                context,
                name,
                arguments,
                started,
                ToolOutcome(
                    False,
                    error={
                        "code": "TOOL_UNAVAILABLE",
                        "message": "真实隔离代码执行后端未启用或不健康",
                    },
                ),
                tool_call_id=tool_call_id,
                argument_meta=argument_meta,
                data_classification=data_classification,
            )
        availability = getattr(self.provider, "tool_available", None)
        if callable(availability) and not self._tool_is_available(availability, name, context):
            return self._finish(
                context,
                name,
                arguments,
                started,
                ToolOutcome(
                    False,
                    error={"code": "TOOL_UNAVAILABLE", "message": f"工具 {name} 当前健康状态不可用"},
                ),
                tool_call_id=tool_call_id,
                argument_meta=argument_meta,
                data_classification=data_classification,
            )
        if spec.effect is ToolEffect.CODE_EXECUTION:
            approval_outcome = self._approve_code_execution(
                spec,
                arguments,
                context,
                started=started,
                tool_call_id=tool_call_id,
                argument_meta=argument_meta,
            )
            if approval_outcome is not None:
                return approval_outcome
        if spec.is_mutating(arguments):
            return self._execute_mutation(
                spec,
                name,
                arguments,
                context,
                conn,
                started=started,
                tool_call_id=tool_call_id,
                plan_step_id=plan_step_id,
                caller_idempotency_key=caller_idempotency_key,
                argument_meta=argument_meta,
            )
        try:
            contextual_dispatch = getattr(self.provider, "dispatch_with_context", None)
            if callable(contextual_dispatch):
                result = self._dispatch_with_optional_manifest(
                    contextual_dispatch,
                    name,
                    arguments,
                    context,
                    conn,
                    frozen_manifest,
                    contextual=True,
                )
            else:
                result = self._dispatch_with_optional_manifest(
                    self.provider.dispatch,
                    name,
                    arguments,
                    context,
                    conn,
                    frozen_manifest,
                )
            context.check_control("tool.after_call")
            if isinstance(result, dict) and isinstance(
                result.get("_teaching_provider_error"), dict
            ):
                outcome = self._teaching_provider_error(
                    result["_teaching_provider_error"]
                )
            elif isinstance(result, dict) and "error" in result:
                outcome = ToolOutcome(
                    False,
                    error={"code": "TOOL_ERROR", "message": str(result["error"])},
                )
            elif spec.effect is ToolEffect.CODE_EXECUTION and isinstance(result, dict) and not result.get("success"):
                code = str(result.get("status") or result.get("outcome") or "CODE_EXECUTION_FAILED")
                outcome = ToolOutcome(
                    False,
                    data=result,
                    error={"code": code.upper(), "message": str(result.get("status_description") or code)},
                )
            else:
                outcome = ToolOutcome(True, data=result)
        except (FencingTokenRejected, RunCancelled, CancellationRequested):
            raise
        except TeachingProviderRejected as error:
            outcome = self._teaching_provider_error(error.error)
        except TimeoutError as error:
            outcome = ToolOutcome(
                False,
                error={"code": "TOOL_TIMEOUT", "message": str(error) or "工具执行超时"},
            )
        except Exception as error:
            outcome = ToolOutcome(
                False,
                error={"code": "TOOL_EXCEPTION", "message": f"{type(error).__name__}: {error}"},
            )
        return self._finish(
            context,
            name,
            arguments,
            started,
            outcome,
            tool_call_id=tool_call_id,
            argument_meta=argument_meta,
            data_classification=data_classification,
        )

    @staticmethod
    def _code_approval_arguments(arguments: dict) -> dict:
        source = str(arguments.get("source_code", ""))
        expected = arguments.get("expected_output")
        return {
            "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "language": str(arguments.get("language", "python")),
            "stdin_sha256": hashlib.sha256(str(arguments.get("stdin") or "").encode("utf-8")).hexdigest(),
            "expected_output_sha256": (
                hashlib.sha256(str(expected).encode("utf-8")).hexdigest()
                if expected is not None else None
            ),
            "cpu_time_limit_seconds": int(arguments.get("cpu_time_limit_seconds", 2)),
            "wall_time_limit_seconds": int(
                arguments.get("wall_time_limit_seconds", arguments.get("timeout", 5))
            ),
            "memory_limit_mb": int(arguments.get("memory_limit_mb", 512)),
            "output_limit_bytes": int(arguments.get("output_limit_bytes", 65536)),
            "process_limit": int(arguments.get("process_limit", 16)),
            "file_size_limit_mb": int(arguments.get("file_size_limit_mb", 16)),
            "artifact_limit_bytes": int(arguments.get("artifact_limit_bytes", 262144)),
            "args": list(arguments.get("args") or ()),
            "network_policy": str(arguments.get("network_policy", "disabled")),
            "network_allowlist": list(arguments.get("network_allowlist") or ()),
        }

    def _approve_code_execution(
        self,
        spec,
        arguments: dict,
        context: RunContext,
        *,
        started: float,
        tool_call_id: str | None,
        argument_meta: dict[str, Any] | None = None,
    ) -> ToolOutcome | None:
        if not self.policy.require_code_execution_approval:
            return None
        approval_arguments = self._code_approval_arguments(arguments)
        digest = payload_hash("run_code", approval_arguments)
        scope = approval_scope(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            tool_name="run_code",
            arguments=approval_arguments,
        )
        expires_at = (
            datetime.now(UTC) + timedelta(seconds=self.policy.approval_ttl_seconds)
        ).isoformat()
        request = ApprovalRequest(
            run_id=context.run_id,
            session_id=context.session_id,
            actor_id=context.actor_id,
            tool_call_id=tool_call_id,
            operation_id=f"code:{digest[:24]}",
            tool_name="run_code",
            arguments=approval_arguments,
            payload_hash=digest,
            scope=scope,
            expires_at=expires_at,
            risk_level=spec.risk_level,
            reason="执行不受信代码；审批绑定 source hash、语言、资源限制和网络策略",
        )
        context.check_control("approval.before_wait")
        approved = bool(self.approval_handler and self.approval_handler(request))
        context.check_control("approval.after_wait")
        expired = datetime.now(UTC) >= datetime.fromisoformat(request.expires_at)
        approved = approved and not expired
        self._record_approval(context, request, approved)
        if approved:
            return None
        error_code = "APPROVAL_EXPIRED" if expired else "APPROVAL_REQUIRED"
        message = (
            "run_code 审批已过期"
            if expired
            else "run_code 需要有效审批后才能执行"
        )
        return self._finish(
            context,
            "run_code",
            arguments,
            started,
            ToolOutcome(
                False,
                error={
                    "code": error_code,
                    "message": message,
                },
            ),
            tool_call_id=tool_call_id,
            argument_meta=argument_meta,
            data_classification=getattr(spec, "data_classification", {}),
        )

    def _execute_mutation(
        self,
        spec,
        name: str,
        arguments: dict,
        context: RunContext,
        conn,
        *,
        started: float,
        tool_call_id: str | None,
        plan_step_id: str | None,
        caller_idempotency_key: str | None,
        argument_meta: dict[str, Any] | None = None,
    ) -> ToolOutcome:
        own_connection = conn is None
        connection = conn or db.connect()
        operation = None

        transactional_dispatch = getattr(self.provider, "dispatch_transactional", None)
        if not callable(transactional_dispatch):
            transactional_base = getattr(self.provider, "transactional_base", self.provider)
            transactional_dispatch = getattr(transactional_base, "dispatch_transactional", None)
        if not callable(transactional_dispatch):
            if own_connection:
                connection.close()
            return self._finish(
                context,
                name,
                arguments,
                started,
                ToolOutcome(
                    False,
                    error={
                        "code": "TRANSACTIONAL_ADAPTER_REQUIRED",
                        "message": "远程或插件写工具必须提供受控事务适配器",
                    },
                ),
                tool_call_id=tool_call_id,
                argument_meta=argument_meta,
                data_classification=getattr(spec, "data_classification", {}),
            )
        digest = payload_hash(name, arguments)
        scope = approval_scope(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            tool_name=name,
            arguments=arguments,
        )
        key = idempotency_key(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            session_id=context.session_id,
            run_id=context.run_id,
            plan_step_id=plan_step_id,
            tool_call_id=tool_call_id,
            tool_name=name,
            arguments=arguments,
            caller_key=caller_idempotency_key,
            replay_scope=context.replay_scope,
        )
        try:
            operation = self.transaction_runtime.prepare(
                connection,
                key=key,
                digest=digest,
                tool_name=name,
                arguments=arguments,
                context=context,
                tool_call_id=tool_call_id,
                plan_step_id=plan_step_id,
                scope=scope,
            )
            context.check_control("approval.before_wait")
            if operation["status"] != "committed" and not self.transaction_runtime.valid_approval(
                connection, operation
            ):
                expires_at = (
                    datetime.now(UTC) + timedelta(seconds=self.policy.approval_ttl_seconds)
                ).isoformat()
                request = ApprovalRequest(
                    run_id=context.run_id,
                    session_id=context.session_id,
                    actor_id=context.actor_id,
                    tool_call_id=tool_call_id,
                    operation_id=operation["id"],
                    tool_name=name,
                    arguments=redact_sensitive(arguments),
                    payload_hash=digest,
                    scope=scope,
                    expires_at=expires_at,
                    risk_level=spec.risk_level,
                    reason="该工具会修改教学业务数据",
                )
                approved = not self.policy.require_write_approval or bool(
                    self.approval_handler and self.approval_handler(request)
                )
                context.check_control("approval.after_wait")
                operation = self.transaction_runtime.approve(
                    connection,
                    operation["id"],
                    digest=digest,
                    scope=scope,
                    approved=approved,
                    approver_id=context.actor_id,
                    expires_at=expires_at,
                    context=context,
                )
                self._record_approval(context, request, approved)
                if not approved:
                    return self._finish(
                        context,
                        name,
                        arguments,
                        started,
                        ToolOutcome(
                            False,
                            error={
                                "code": "APPROVAL_REQUIRED",
                                "message": f"工具 {name} 需要教师确认后才能执行",
                            },
                        ),
                        tool_call_id=tool_call_id,
                        operation=operation,
                        argument_meta=argument_meta,
                        data_classification=getattr(spec, "data_classification", {}),
                    )
            execution = self.transaction_runtime.execute(
                connection,
                operation,
                lambda: self._dispatch_transactional(
                    transactional_dispatch,
                    name,
                    arguments,
                    context,
                    connection,
                    {
                        **operation,
                        "status": "executing",
                        # Persisted operation arguments are redacted.  The
                        # in-process command must bind the original validated
                        # payload to the already stored hash.
                        "arguments": arguments,
                    },
                ),
                context=context,
            )
            outcome = ToolOutcome(
                True,
                data=execution.result,
                meta={"idempotent_replay": execution.replayed},
            )
            operation = execution.operation
        except (FencingTokenRejected, RunCancelled, CancellationRequested):
            raise
        except IdempotencyConflict as error:
            outcome = ToolOutcome(
                False,
                error={"code": "IDEMPOTENCY_CONFLICT", "message": str(error)},
            )
        except TeachingProviderRejected as error:
            outcome = self._teaching_provider_error(error.error)
        except OperationUnavailable as error:
            outcome = ToolOutcome(
                False,
                error={"code": "OPERATION_UNAVAILABLE", "message": str(error)},
            )
        except TimeoutError as error:
            outcome = ToolOutcome(
                False,
                error={"code": "TOOL_TIMEOUT", "message": str(error) or "工具执行超时"},
            )
        except Exception as error:
            outcome = ToolOutcome(
                False,
                error={"code": "TOOL_EXCEPTION", "message": f"{type(error).__name__}: {error}"},
            )
        finally:
            if operation is not None:
                try:
                    operation = self.transaction_runtime.get_operation(
                        connection, operation["id"], context=context
                    )
                except Exception:
                    pass
            if own_connection:
                connection.close()
        return self._finish(
            context,
            name,
            arguments,
            started,
            outcome,
            tool_call_id=tool_call_id,
            operation=operation,
            argument_meta=argument_meta,
            data_classification=getattr(spec, "data_classification", {}),
        )

    @staticmethod
    def _teaching_provider_error(provider_error) -> ToolOutcome:
        if isinstance(provider_error, dict):
            raw_kind = provider_error.get("kind")
            try:
                kind = TeachingProviderErrorKind(raw_kind)
            except (TypeError, ValueError):
                kind = TeachingProviderErrorKind.INTERNAL
            message = str(provider_error.get("message") or "教学 Provider 执行失败")
            retryable = bool(provider_error.get("retryable", False))
            details = provider_error.get("details")
            details = dict(details) if isinstance(details, dict) else {}
        else:
            kind = provider_error.kind
            message = provider_error.message
            retryable = provider_error.retryable
            details = dict(provider_error.details)
        code = {
            TeachingProviderErrorKind.INVALID_QUERY: "INVALID_ARGUMENTS",
            TeachingProviderErrorKind.INVALID_COMMAND: "INVALID_ARGUMENTS",
            TeachingProviderErrorKind.NOT_FOUND: "NOT_FOUND",
            TeachingProviderErrorKind.BUSINESS_REJECTED: "BUSINESS_REJECTED",
            TeachingProviderErrorKind.SCOPE_DENIED: "COURSE_SCOPE_DENIED",
            TeachingProviderErrorKind.APPROVAL_REQUIRED: "APPROVAL_REQUIRED",
            TeachingProviderErrorKind.UNAVAILABLE: "TOOL_UNAVAILABLE",
            TeachingProviderErrorKind.UNSUPPORTED: "TOOL_UNAVAILABLE",
        }.get(kind, "TOOL_EXCEPTION")
        return ToolOutcome(
            False,
            error={
                "code": code,
                "message": message,
                "kind": kind.value,
                "retryable": retryable,
                "details": details,
            },
        )

    @staticmethod
    def _dispatch_transactional(
        dispatch,
        name: str,
        arguments: dict,
        context: RunContext,
        connection,
        operation: dict,
    ) -> dict:
        """Invoke the provider's write boundary with executor-issued identity."""

        try:
            parameters = inspect.signature(dispatch).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_kwargs = any(
            item.kind is inspect.Parameter.VAR_KEYWORD
            for item in parameters.values()
        )
        kwargs = {
            "conn": connection,
            "context": context,
            "operation": operation,
        }
        if not accepts_kwargs:
            kwargs = {
                key: value
                for key, value in kwargs.items()
                if key in parameters
            }
        return dispatch(name, arguments, **kwargs)

    def _spec(self, name: str, *, manifest: ToolManifest | None = None):
        if manifest is not None:
            entry = manifest.get(name)
            if entry is None:
                return None
            try:
                current = self.provider.get_spec(name) if hasattr(self.provider, "get_spec") else None
            except Exception as error:
                raise ToolManifestMismatch(
                    f"工具 {name} 当前 registry 元数据不可读取: {type(error).__name__}"
                ) from error
            if current is None:
                raise ToolManifestMismatch(
                    f"工具 {name} 已从 provider registry 消失，拒绝静默漂移"
                )
            if not manifest_entry_matches(entry, current):
                raise ToolManifestMismatch(
                    f"工具 {name} 的 registry 元数据/handler 已变化，拒绝执行"
                )
            function_map = getattr(self.provider, "TOOL_FUNCTIONS", None)
            if (
                isinstance(function_map, dict)
                and entry.handler is not None
                and function_map.get(name) is not entry.handler
            ):
                raise ToolManifestMismatch(
                    f"工具 {name} 的 registry handler 映射已变化，拒绝执行"
                )
        if hasattr(self.provider, "get_spec"):
            return self.provider.get_spec(name)
        from ..tools import registry

        return registry.get_spec(name)

    @staticmethod
    def _tool_is_available(checker, name: str, context: RunContext) -> bool:
        """Call old and new provider health contracts without broad retries."""

        try:
            parameters = inspect.signature(checker).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_context = "context" in parameters or any(
            item.kind is inspect.Parameter.VAR_KEYWORD
            for item in parameters.values()
        )
        return bool(checker(name, context=context)) if accepts_context else bool(checker(name))

    @staticmethod
    def _dispatch_with_optional_manifest(
        dispatch,
        name: str,
        arguments: dict,
        context: RunContext,
        conn,
        manifest: ToolManifest | None,
        *,
        contextual: bool = False,
    ):
        """Dispatch using the provider's declared signature, avoiding TypeError retries."""

        try:
            parameters = inspect.signature(dispatch).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_manifest = "manifest" in parameters or any(
            item.kind is inspect.Parameter.VAR_KEYWORD
            for item in parameters.values()
        )
        kwargs = {"conn": conn}
        if accepts_manifest:
            kwargs["manifest"] = manifest
        if contextual:
            return dispatch(name, arguments, context, **kwargs)
        return dispatch(name, arguments, **kwargs)

    def _finish(
        self,
        context: RunContext,
        name: str,
        arguments: dict,
        started: float,
        outcome: ToolOutcome,
        *,
        tool_call_id: str | None = None,
        operation: dict | None = None,
        argument_meta: dict[str, Any] | None = None,
        data_classification: Mapping[str, str] | None = None,
    ) -> ToolOutcome:
        if argument_meta:
            outcome.meta.update(argument_meta)
        outcome.meta.update(
            {
                "tool": name,
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
                "run_id": context.run_id,
                "tool_call_id": tool_call_id,
            }
        )
        if operation is not None:
            outcome.meta.update(
                {
                    "operation_id": operation["id"],
                    "operation_status": operation["status"],
                }
            )
        if self.defer_result_commit:
            return outcome
        return self._persist_outcome(
            context,
            name,
            arguments,
            outcome,
            tool_call_id=tool_call_id,
            operation=operation,
            data_classification=data_classification,
        )

    def finalize_deferred(
        self,
        name: str,
        arguments: dict,
        context: RunContext,
        outcome: ToolOutcome,
        *,
        tool_call_id: str | None = None,
    ) -> ToolOutcome:
        """Accept one worker candidate after its timeout/cancellation fence wins."""

        context.check_control("tool.deferred_result.accept")
        outcome.meta.setdefault("tool", name)
        outcome.meta.setdefault("run_id", context.run_id)
        outcome.meta.setdefault("tool_call_id", tool_call_id)
        outcome.meta.setdefault("duration_ms", 0.0)
        operation = None
        operation_id = outcome.meta.get("operation_id")
        operation_status = outcome.meta.get("operation_status")
        if isinstance(operation_id, str) and isinstance(operation_status, str):
            operation = {"id": operation_id, "status": operation_status}
        try:
            spec = self._spec(name, manifest=self.manifest or context.tool_manifest)
            classifications = getattr(spec, "data_classification", {}) if spec is not None else {}
        except ToolManifestMismatch:
            classifications = {}
        return self._persist_outcome(
            context,
            name,
            arguments,
            outcome,
            tool_call_id=tool_call_id,
            operation=operation,
            data_classification=classifications,
        )

    def _persist_outcome(
        self,
        context: RunContext,
        name: str,
        arguments: dict,
        outcome: ToolOutcome,
        *,
        tool_call_id: str | None = None,
        operation: dict | None = None,
        data_classification: Mapping[str, str] | None = None,
    ) -> ToolOutcome:
        if self.result_budget is not None:
            context.check_control("tool.before_result_spill")
            processed = self.result_budget.apply(
                redact_sensitive(outcome.to_dict()),
                context=context,
                tool_name=name,
            )
            outcome = ToolOutcome(
                processed["ok"],
                data=processed.get("data"),
                error=processed.get("error"),
                meta=processed.get("meta", {}),
            )
        if self.state_store is not None:
            context.check_control("tool.before_event_commit")
            classifications = data_classification
            if classifications is None:
                try:
                    current_spec = (
                        self.provider.get_spec(name)
                        if hasattr(self.provider, "get_spec")
                        else None
                    )
                    classifications = getattr(current_spec, "data_classification", {})
                except Exception:
                    classifications = {}
            persisted_arguments = redact_sensitive(
                redact_classified_arguments(arguments, classifications)
            )
            tool_event_id = self.state_store.record_tool_event(
                run_id=context.run_id,
                session_id=context.session_id,
                tool_call_id=tool_call_id,
                operation_id=operation["id"] if operation else None,
                operation_status=operation["status"] if operation else None,
                tool_name=name,
                arguments=persisted_arguments,
                outcome=outcome.to_dict(),
                duration_ms=outcome.meta["duration_ms"],
                context=context,
            )
            outcome.meta["tool_event_id"] = tool_event_id
        return outcome

    def enforce_turn_budget(self, messages: list[dict], context: RunContext) -> list[dict]:
        if self.result_budget is None:
            return messages
        return self.result_budget.enforce_turn(messages, context=context)

    def enforce_incremental_turn_budget(
        self,
        message: dict,
        prior_messages: list[dict],
        context: RunContext,
    ) -> dict:
        if self.result_budget is None:
            return message
        return self.result_budget.enforce_incremental(
            message,
            prior_messages=prior_messages,
            context=context,
        )

    def _record_approval(
        self,
        context: RunContext,
        request: ApprovalRequest,
        approved: bool,
    ) -> None:
        if self.state_store is None:
            return
        self.state_store.record_audit_event(
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
            action="tool.approval",
            resource=request.tool_name,
            decision="approved" if approved else "denied",
            details={
                "arguments": redact_sensitive(request.arguments),
                "run_id": request.run_id,
                "tool_call_id": request.tool_call_id,
                "operation_id": request.operation_id,
                "payload_hash": request.payload_hash,
                "scope": request.scope,
                "expires_at": request.expires_at,
                "approver_id": context.actor_id,
            },
        )


def parse_tool_arguments(raw_arguments: str | dict | None) -> tuple[dict | None, ToolOutcome | None]:
    try:
        return strict_parse_tool_arguments(raw_arguments), None
    except ToolArgumentError as error:
        return None, ToolOutcome(False, error=error.to_error())
