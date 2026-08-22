from __future__ import annotations

import json
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..data import db
from ..state.store import FencingTokenRejected, RunCancelled
from .cancellation import CancellationRequested
from .models import BudgetExceeded, RunContext
from .security import redact_sensitive
from .transactions import (
    IdempotencyConflict,
    OperationUnavailable,
    TransactionalToolRuntime,
    approval_scope,
    idempotency_key,
    payload_hash,
)


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
class ToolOutcome:
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


def _validate_value(value: Any, schema: dict, path: str) -> list[str]:
    errors: list[str] = []
    expected = schema.get("type")
    type_map = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    if expected in type_map:
        expected_type = type_map[expected]
        if not isinstance(value, expected_type) or expected == "integer" and isinstance(value, bool):
            return [f"{path} 应为 {expected}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} 必须是 {schema['enum']} 之一")
    if isinstance(value, str):
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path} 长度不能超过 {schema['maxLength']}")
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path} 长度不能小于 {schema['minLength']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path} 不能小于 {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path} 不能大于 {schema['maximum']}")
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path} 元素数不能超过 {schema['maxItems']}")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                errors.extend(_validate_value(item, item_schema, f"{path}[{index}]"))
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for required in schema.get("required", []):
            if required not in value:
                errors.append(f"{path}.{required} 为必填参数")
        if schema.get("additionalProperties") is False:
            for key in set(value) - set(properties):
                errors.append(f"{path}.{key} 是未知参数")
        for key, item in value.items():
            if key in properties:
                errors.extend(_validate_value(item, properties[key], f"{path}.{key}"))
    return errors


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
    ):
        self.provider = provider
        self.policy = policy or ExecutionPolicy()
        self.approval_handler = approval_handler
        self.state_store = state_store
        self.result_budget = result_budget
        self.transaction_runtime = transaction_runtime or TransactionalToolRuntime(
            state_store=state_store
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
    ) -> ToolOutcome:
        arguments, parse_error = parse_tool_arguments(raw_arguments)
        if parse_error is not None:
            started = time.monotonic()
            return self._finish(
                context,
                name,
                {"_raw": raw_arguments},
                started,
                parse_error,
                tool_call_id=tool_call_id,
            )
        return self.execute(
            name,
            arguments or {},
            context,
            conn=conn,
            allowed_tools=allowed_tools,
            tool_call_id=tool_call_id,
            plan_step_id=plan_step_id,
            caller_idempotency_key=caller_idempotency_key,
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
    ) -> ToolOutcome:
        started = time.monotonic()
        context.check_control("tool.before_call")
        try:
            context.budget.consume_tool_call()
        except BudgetExceeded as error:
            return self._finish(
                context,
                name,
                arguments,
                started,
                ToolOutcome(False, error={"code": "BUDGET_EXCEEDED", "message": str(error)}),
                tool_call_id=tool_call_id,
            )
        if allowed_tools is not None and name not in allowed_tools:
            return self._finish(
                context,
                name,
                arguments,
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
        spec = self._spec(name)
        if spec is None:
            return self._finish(
                context,
                name,
                arguments,
                started,
                ToolOutcome(
                    False,
                    error={"code": "UNKNOWN_TOOL", "message": f"未知工具：{name}"},
                ),
                tool_call_id=tool_call_id,
            )
        validation_errors = _validate_value(arguments, spec.schema.get("parameters", {}), "arguments")
        if validation_errors:
            return self._finish(
                context,
                name,
                arguments,
                started,
                ToolOutcome(
                    False,
                    error={"code": "INVALID_ARGUMENTS", "message": "; ".join(validation_errors)},
                ),
                tool_call_id=tool_call_id,
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
            )
        if name == "run_code" and not self.policy.allow_local_code_execution:
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
            )
        if name == "run_code":
            approval_outcome = self._approve_code_execution(
                spec, arguments, context, started=started, tool_call_id=tool_call_id,
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
            )
        try:
            contextual_dispatch = getattr(self.provider, "dispatch_with_context", None)
            if callable(contextual_dispatch):
                result = contextual_dispatch(name, arguments, context, conn=conn)
            else:
                result = self.provider.dispatch(name, arguments, conn=conn)
            context.check_control("tool.after_call")
            if isinstance(result, dict) and "error" in result:
                outcome = ToolOutcome(
                    False,
                    error={"code": "TOOL_ERROR", "message": str(result["error"])},
                )
            elif name == "run_code" and isinstance(result, dict) and not result.get("success"):
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
    ) -> ToolOutcome:
        own_connection = conn is None
        connection = conn or db.connect()
        operation = None
        from ..tools import registry as local_registry

        transactional_base = getattr(self.provider, "transactional_base", self.provider)
        if transactional_base is not local_registry:
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
                    )
            execution = self.transaction_runtime.execute(
                connection,
                operation,
                lambda: spec.handler(connection, **arguments),
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
        )

    def _spec(self, name: str):
        if hasattr(self.provider, "get_spec"):
            return self.provider.get_spec(name)
        from ..tools import registry

        return registry.get_spec(name)

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
    ) -> ToolOutcome:
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
            tool_event_id = self.state_store.record_tool_event(
                run_id=context.run_id,
                session_id=context.session_id,
                tool_call_id=tool_call_id,
                operation_id=operation["id"] if operation else None,
                operation_status=operation["status"] if operation else None,
                tool_name=name,
                arguments=redact_sensitive(arguments),
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
    if isinstance(raw_arguments, dict):
        return raw_arguments, None
    try:
        parsed = json.loads(raw_arguments or "{}")
    except json.JSONDecodeError as error:
        return None, ToolOutcome(
            False,
            error={
                "code": "INVALID_JSON",
                "message": f"工具参数不是合法 JSON：{error.msg}",
            },
        )
    if not isinstance(parsed, dict):
        return None, ToolOutcome(
            False,
            error={"code": "INVALID_ARGUMENTS", "message": "工具参数必须是 JSON object"},
        )
    return parsed, None
