"""LangGraph 多工具 Agent：预算化模型调用、安全工具执行和结构化错误回灌。"""
from __future__ import annotations

import json
import operator
from copy import deepcopy
from typing import Annotated, TypedDict

from langgraph.graph import END, StateGraph

from ..engine.base import Engine
from ..engine.streaming import consume_provider_stream
from ..planning.models import PlanStatus, StepStatus
from ..planning.planner import ModelPlanGenerator, PlanGenerationError, should_create_plan
from ..planning.runtime import PlanCoordinator, PlanningOptions
from ..planning.verifier import EvidenceVerifier
from ..runtime.models import BudgetExceeded, RunContext
from ..runtime.cancellation import CancellationRequested
from ..runtime.tool_executor import ExecutionPolicy, PolicyToolExecutor, ToolOutcome
from ..state import RunPhase
from ..state.store import RunCancelled
from ..tools import registry
from .loop_journal import AgentLoopJournal
from .prompts import SYSTEM_PROMPT


class AgentState(TypedDict, total=False):
    messages: Annotated[list, operator.add]
    stop_reason: str
    usage: Annotated[list[dict], operator.add]
    active_step_id: str | None
    model_attempt: int


def _select_tools(provider, context: RunContext, policy: ExecutionPolicy) -> list[dict]:
    try:
        return provider.openai_tools(
            role=context.role,
            allow_local_code_execution=policy.allow_local_code_execution,
        )
    except TypeError:
        tools = provider.openai_tools()
        selected = []
        for tool in tools:
            name = tool["function"]["name"]
            spec = registry.get_spec(name)
            if spec is not None and context.role not in spec.allowed_roles:
                continue
            if name == "run_code" and not policy.allow_local_code_execution:
                continue
            selected.append(tool)
    return selected


def _scope_tools(tools: list[dict], step) -> list[dict]:
    if step is None:
        return []
    selected = []
    for tool in tools:
        if tool["function"]["name"] not in step.allowed_tools:
            continue
        scoped = deepcopy(tool)
        description = scoped["function"].get("description", "")
        scoped["function"]["description"] = (
            f"当前计划步骤 {step.id}：{step.goal}\n"
            f"只用于完成此步骤；完成证据："
            f"{', '.join(condition.kind for condition in step.completion_conditions)}。\n"
            f"{description}"
        )
        selected.append(scoped)
    return selected


def _plan_stop_message(coordinator: PlanCoordinator, status: str, reason: str) -> str:
    result = coordinator.result() or {}
    missing = result.get("missing_evidence", [])
    return (
        f"执行未完成（{status}）：{reason}。"
        f"缺失证据：{json.dumps(missing, ensure_ascii=False)}"
    )


def build_agent(
    engine: Engine,
    db_conn=None,
    max_tool_calls: int = 8,
    tools_provider=None,
    *,
    run_context: RunContext | None = None,
    tool_executor: PolicyToolExecutor | None = None,
    tool_schemas: list[dict] | None = None,
    plan_coordinator: PlanCoordinator | None = None,
    evidence_verifier: EvidenceVerifier | None = None,
    loop_journal: AgentLoopJournal | None = None,
    entry_point: str = "agent",
):
    """编译 Agent 图；生产控制由 RunContext 和 ToolExecutor 承担。"""
    provider = tools_provider if tools_provider is not None else registry
    context = run_context or RunContext.create(
        session_id="legacy",
        actor_id="legacy",
        role="system",
        max_model_calls=30,
        max_tool_calls=max_tool_calls,
    )
    executor = tool_executor or PolicyToolExecutor(
        provider,
        policy=ExecutionPolicy.legacy_demo(),
    )
    tools = tool_schemas or _select_tools(provider, context, executor.policy)

    def agent_node(state: AgentState):
        context.check_control("model.before_call")
        context.emit_run_event("run.phase", {"phase": "model"})
        active_step = None
        current_tools = tools
        if plan_coordinator is not None:
            if not plan_coordinator.consume_iteration():
                reason = "计划迭代预算已耗尽"
                return {
                    "messages": [
                        {
                            "role": "assistant",
                            "content": _plan_stop_message(
                                plan_coordinator,
                                PlanStatus.budget_exceeded.value,
                                reason,
                            ),
                        }
                    ],
                    "stop_reason": PlanStatus.budget_exceeded.value,
                }
            active_step = plan_coordinator.active_or_ready_step()
            current_tools = _scope_tools(tools, active_step)
        try:
            context.budget.consume_model_call()
        except BudgetExceeded as error:
            if plan_coordinator is not None:
                plan_coordinator.fail(PlanStatus.budget_exceeded, str(error))
                content = _plan_stop_message(
                    plan_coordinator,
                    PlanStatus.budget_exceeded.value,
                    str(error),
                )
            else:
                content = f"执行已停止：{error}"
            return {
                "messages": [{"role": "assistant", "content": content}],
                "stop_reason": "budget_exceeded",
            }
        model_attempt = (
            loop_journal.start_model_attempt() if loop_journal is not None else None
        )
        response = consume_provider_stream(
            engine,
            state["messages"],
            current_tools,
            cancellation_token=context.cancellation_token,
            event_sink=context.emit_provider_event if context.streams_events else None,
        )
        context.check_control("model.after_call")
        if loop_journal is not None:
            loop_journal.model_returned()
        usage = [response.usage] if response.usage else []
        assistant_message = response.to_assistant_message()
        if assistant_message.get("tool_calls") and loop_journal is not None:
            assistant_message = loop_journal.append_envelope(
                assistant_message,
                model_attempt=model_attempt,
            )
        return {
            "messages": [assistant_message],
            "usage": usage,
            "active_step_id": active_step.id if active_step else None,
            "model_attempt": model_attempt,
        }

    def tools_node(state: AgentState):
        context.emit_run_event("run.phase", {"phase": "tools"})
        last = next(
            (
                message
                for message in reversed(state["messages"])
                if message.get("role") == "assistant" and message.get("tool_calls")
            ),
            None,
        )
        if last is None:
            raise RuntimeError("tools phase 缺少 assistant tool-call envelope")
        output = []
        turn_results = []
        model_attempt = state.get("model_attempt")
        cancellation: RunCancelled | CancellationRequested | None = None
        allowed_tools = None
        if plan_coordinator is not None:
            step = next(
                (
                    item
                    for item in plan_coordinator.steps()
                    if item.id == state.get("active_step_id")
                ),
                None,
            )
            allowed_tools = set(step.allowed_tools) if step else set()
        for tool_call in last.get("tool_calls", []):
            function = tool_call["function"]
            name = function["name"]
            call_id = tool_call["id"]
            if loop_journal is not None:
                existing = loop_journal.call_record(call_id)
                if existing is not None and existing.get("status") == "completed":
                    already_present = any(
                        message.get("role") == "tool"
                        and message.get("tool_call_id") == call_id
                        for message in state["messages"]
                    )
                    if not already_present:
                        output.append(existing["result_message"])
                    turn_results.append(existing["result_message"])
                    continue
            operation_record = (
                executor.state_store.get_tool_operation_for_call(
                    run_id=context.run_id,
                    tool_call_id=call_id,
                    actor_id=context.actor_id,
                    tenant_id=context.tenant_id,
                )
                if executor.state_store is not None
                else None
            )
            try:
                if cancellation is None:
                    context.check_control("tools.before_batch" if not output else "tool.between_calls")
                    context.emit_run_event(
                        "tool.started",
                        {"tool_call_id": call_id, "tool_name": name},
                    )
                    if operation_record is not None and operation_record["status"] in {
                        "executing",
                        "manual_review",
                    }:
                        outcome = ToolOutcome(
                            False,
                            error={
                                "code": "OPERATION_UNAVAILABLE",
                                "message": (
                                    "写操作提交状态不确定，已禁止重新执行；"
                                    "需要恢复流程复用回执或人工确认"
                                ),
                            },
                            meta={
                                "operation_id": operation_record["operation_id"],
                                "operation_status": operation_record["status"],
                                "run_id": context.run_id,
                                "tool_call_id": call_id,
                            },
                        )
                    else:
                        outcome = executor.execute_raw(
                            name,
                            function.get("arguments"),
                            context,
                            conn=db_conn,
                            allowed_tools=allowed_tools,
                            tool_call_id=call_id,
                            plan_step_id=state.get("active_step_id"),
                        )
                    context.emit_run_event(
                        "tool.completed",
                        {
                            "tool_call_id": call_id,
                            "tool_name": name,
                            "ok": outcome.ok,
                            "error": outcome.error,
                        },
                    )
                else:
                    outcome = ToolOutcome(
                        False,
                        error={"code": "CANCELLED", "message": str(cancellation)},
                        meta={"run_id": context.run_id, "tool_call_id": call_id},
                    )
            except (RunCancelled, CancellationRequested) as error:
                cancellation = error
                outcome = ToolOutcome(
                    False,
                    error={"code": "CANCELLED", "message": str(error)},
                    meta={"run_id": context.run_id, "tool_call_id": call_id},
                )
            if operation_record is None and executor.state_store is not None:
                operation_record = executor.state_store.get_tool_operation_for_call(
                    run_id=context.run_id,
                    tool_call_id=call_id,
                    actor_id=context.actor_id,
                    tenant_id=context.tenant_id,
                )
            if operation_record is not None:
                outcome.meta.setdefault("operation_id", operation_record["operation_id"])
                outcome.meta.setdefault("operation_status", operation_record["status"])
            result_message = {
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": json.dumps(
                    outcome.to_dict(),
                    ensure_ascii=False,
                    default=str,
                ),
            }
            if loop_journal is not None:
                result_message = executor.enforce_incremental_turn_budget(
                    result_message,
                    turn_results,
                    context,
                )
                result_message = loop_journal.append_result(
                    result_message,
                    model_attempt=model_attempt,
                    operation_id=outcome.meta.get("operation_id"),
                    tool_event_id=outcome.meta.get("tool_event_id"),
                    allow_cancelled=cancellation is not None,
                )
            output.append(result_message)
            turn_results.append(result_message)
        if cancellation is not None:
            raise cancellation
        if loop_journal is not None:
            loop_journal.complete_tool_batch(model_attempt=model_attempt)
        else:
            output = executor.enforce_turn_budget(output, context)
        return {"messages": output}

    def verify_node(state: AgentState):
        context.check_control("plan.step_boundary")
        context.emit_run_event("run.phase", {"phase": "verifying"})
        if plan_coordinator is None or evidence_verifier is None:
            return {}
        step = next(
            (
                item
                for item in plan_coordinator.steps()
                if item.id == state.get("active_step_id")
            ),
            None,
        )
        last = state["messages"][-1]
        if step is not None and step.status == StepStatus.in_progress:
            if last.get("role") == "assistant" and not last.get("tool_calls"):
                verification = evidence_verifier.reject_premature_answer(
                    plan_coordinator.plan.id,
                    step,
                )
            else:
                verification = evidence_verifier.verify_step(
                    plan_coordinator.plan.id,
                    step,
                )
            if verification.blocked:
                reason = verification.failure_reason or "步骤无法取得所需证据"
                plan_status = (plan_coordinator.result() or {}).get(
                    "status",
                    PlanStatus.blocked.value,
                )
                return {
                    "messages": [
                        {
                            "role": "assistant",
                            "content": _plan_stop_message(
                                plan_coordinator,
                                plan_status,
                                reason,
                            ),
                        }
                    ],
                    "stop_reason": plan_status,
                    "active_step_id": step.id,
                }
            if verification.completed:
                return {"active_step_id": None}
            return {"active_step_id": step.id}

        if plan_coordinator.all_steps_completed():
            if last.get("role") == "assistant" and not last.get("tool_calls"):
                steps = plan_coordinator.steps()
                if evidence_verifier.plan_has_complete_evidence(
                    plan_coordinator.plan.id,
                    steps,
                ) and evidence_verifier.final_answer_citations_valid(
                    plan_coordinator.plan.id,
                    steps,
                    str(last.get("content") or ""),
                ):
                    plan_coordinator.complete()
                    return {"stop_reason": PlanStatus.completed.value}
                plan_coordinator.fail(
                    PlanStatus.incomplete,
                    "最终完成验证发现步骤缺失真实证据",
                )
                missing = []
                for candidate in steps:
                    conditions = evidence_verifier.missing_conditions(
                        plan_coordinator.plan.id,
                        candidate,
                    )
                    if conditions:
                        missing.append(
                            {
                                "step_id": candidate.id,
                                "goal": candidate.goal,
                                "status": candidate.status.value,
                                "missing_conditions": list(conditions),
                            }
                        )
                return {
                    "messages": [
                        {
                            "role": "assistant",
                            "content": (
                                _plan_stop_message(
                                    plan_coordinator,
                                    PlanStatus.incomplete.value,
                                    "最终完成验证发现步骤缺失真实证据",
                                )
                                + f"最终复核缺失条件：{json.dumps(missing, ensure_ascii=False)}"
                            ),
                        }
                    ],
                    "stop_reason": PlanStatus.incomplete.value,
                }
            return {"active_step_id": None}
        if plan_coordinator.blocked():
            reason = "计划存在 blocked 步骤"
            plan_coordinator.fail(PlanStatus.blocked, reason)
            return {
                "messages": [
                    {
                        "role": "assistant",
                        "content": _plan_stop_message(
                            plan_coordinator,
                            PlanStatus.blocked.value,
                            reason,
                        ),
                    }
                ],
                "stop_reason": PlanStatus.blocked.value,
            }
        return {"active_step_id": None}

    def should_continue(state: AgentState):
        if state.get("stop_reason"):
            return "end"
        if state["messages"][-1].get("tool_calls"):
            return "tools"
        if plan_coordinator is not None:
            return "verify"
        return "end"

    def after_tools(state: AgentState):
        return "verify" if plan_coordinator is not None else "agent"

    def after_verify(state: AgentState):
        return "end" if state.get("stop_reason") else "agent"

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_node("verify", verify_node)
    if entry_point not in {"agent", "tools", "verify"}:
        raise ValueError(f"unknown agent entry point: {entry_point}")
    graph.set_entry_point(entry_point)
    graph.add_conditional_edges(
        "agent",
        should_continue,
        {"tools": "tools", "verify": "verify", "end": END},
    )
    graph.add_conditional_edges("tools", after_tools, {"verify": "verify", "agent": "agent"})
    graph.add_conditional_edges("verify", after_verify, {"agent": "agent", "end": END})
    return graph.compile()


def run_agent(
    task: str,
    engine: Engine,
    system_prompt: str = SYSTEM_PROMPT,
    db_conn=None,
    recursion_limit: int = 30,
    tools_provider=None,
    *,
    initial_messages: list[dict] | None = None,
    run_context: RunContext | None = None,
    tool_executor: PolicyToolExecutor | None = None,
    tool_schemas: list[dict] | None = None,
    planning: PlanningOptions | None = None,
    plan_generator=None,
    state_store=None,
    force_plan: bool | None = None,
    context_checkpoint_id: str | None = None,
    loop_fault_injector=None,
) -> dict:
    """运行一次 Agent turn，返回回答、轨迹、消息、预算和模型 usage。"""
    context = run_context or RunContext.create(
        session_id="legacy",
        actor_id="legacy",
        role="system",
        max_model_calls=recursion_limit,
        max_tool_calls=max(8, recursion_limit),
    )
    provider = tools_provider if tools_provider is not None else registry
    executor = tool_executor or PolicyToolExecutor(
        provider,
        policy=ExecutionPolicy.legacy_demo(),
        state_store=state_store,
    )
    tools = tool_schemas or _select_tools(provider, context, executor.policy)
    loop_journal = AgentLoopJournal(
        state_store or executor.state_store,
        context,
        tools=tools,
        engine=engine,
        context_checkpoint_id=context_checkpoint_id,
        fault_injector=loop_fault_injector,
    )
    coordinator = None
    verifier = None
    use_plan = bool(
        planning
        and planning.enabled
        and (force_plan if force_plan is not None else should_create_plan(task))
    )
    if use_plan:
        persistent_store = state_store or executor.state_store
        if persistent_store is None:
            raise ValueError("启用 PlanGraph 时必须提供 StateStore")
        coordinator = PlanCoordinator(
            persistent_store,
            context,
            options=planning,
        )
        generator = plan_generator or ModelPlanGenerator(engine)
        try:
            coordinator.ensure_plan(
                task,
                generator=generator,
                available_tools={tool["function"]["name"] for tool in tools},
            )
        except (PlanGenerationError, BudgetExceeded) as error:
            code = getattr(error, "code", "BUDGET_EXCEEDED")
            reason = f"{code}: {error}"
            coordinator.create_invalid_plan(task, reason=reason)
            if isinstance(error, BudgetExceeded):
                coordinator.fail(PlanStatus.budget_exceeded, reason)
                status = PlanStatus.budget_exceeded.value
            else:
                status = PlanStatus.invalid.value
            loop_journal.enter_planning(plan_id=coordinator.plan.id)
            messages = initial_messages or [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]
            failure = _plan_stop_message(coordinator, status, reason)
            result_messages = [*messages, {"role": "assistant", "content": failure}]
            return {
                "final_answer": failure,
                "trace": [],
                "messages": result_messages,
                "usage": [],
                "budget": context.budget.usage(),
                "stop_reason": status,
                "plan": coordinator.result(),
            }
        context.emit_run_event(
            "plan.updated",
            {
                "plan_id": coordinator.plan.id,
                "status": coordinator.plan.status.value,
                "plan": coordinator.result(),
            },
        )
        verifier = EvidenceVerifier(
            persistent_store,
            context,
            max_step_retries=planning.max_step_retries,
            citation_verifier=getattr(provider, "verify_citation", None),
            citation_claim_verifier=getattr(provider, "verify_claim", None),
        )

    loop_journal.enter_planning(
        plan_id=coordinator.plan.id if coordinator is not None else None
    )
    context.emit_run_event("run.phase", {"phase": "model"})

    entry_point = "agent"
    if loop_journal.active:
        journal_snapshot = loop_journal.read()
        if journal_snapshot.phase is RunPhase.TOOLS:
            entry_point = "tools"
        elif journal_snapshot.phase is RunPhase.VERIFYING:
            entry_point = "verify"

    app = build_agent(
        engine,
        db_conn=db_conn,
        tools_provider=provider,
        run_context=context,
        tool_executor=executor,
        tool_schemas=tools,
        plan_coordinator=coordinator,
        evidence_verifier=verifier,
        loop_journal=loop_journal if loop_journal.active else None,
        entry_point=entry_point,
    )
    messages = initial_messages or [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]
    if loop_journal.active and entry_point in {"tools", "verify"}:
        persisted_protocol = [
            message
            for message in loop_journal.state_store.get_run_messages(context.run_id)
            if message.get("role") == "tool"
            or (message.get("role") == "assistant" and message.get("tool_calls"))
        ]
        existing_calls = {
            call["id"]
            for message in messages
            for call in message.get("tool_calls", [])
        }
        existing_results = {
            message.get("tool_call_id")
            for message in messages
            if message.get("role") == "tool"
        }
        for persisted in persisted_protocol:
            if persisted.get("role") == "assistant":
                call_ids = {call["id"] for call in persisted.get("tool_calls", [])}
                if call_ids and call_ids <= existing_calls:
                    continue
                existing_calls.update(call_ids)
            else:
                call_id = persisted.get("tool_call_id")
                if call_id in existing_results:
                    continue
                existing_results.add(call_id)
            messages.append(persisted)
    initial_state = {"messages": messages, "usage": []}
    if loop_journal.active and entry_point in {"tools", "verify"}:
        initial_state["model_attempt"] = loop_journal.read().model_attempt
        if coordinator is not None:
            active_step = next(
                (
                    step
                    for step in coordinator.steps()
                    if step.status == StepStatus.in_progress
                ),
                None,
            )
            initial_state["active_step_id"] = active_step.id if active_step else None
    state = app.invoke(
        initial_state,
        {"recursion_limit": recursion_limit},
    )
    if coordinator is not None:
        plan_result = coordinator.result()
        context.emit_run_event(
            "plan.updated",
            {
                "plan_id": coordinator.plan.id,
                "status": (plan_result or {}).get("status", "running"),
                "plan": plan_result,
            },
        )
    result_messages = state["messages"]
    trace = []
    for message in result_messages:
        if message.get("role") == "assistant" and message.get("tool_calls"):
            for tool_call in message["tool_calls"]:
                trace.append(
                    {
                        "tool": tool_call["function"]["name"],
                        "arguments": tool_call["function"]["arguments"],
                    }
                )
    final = next(
        (
            message["content"]
            for message in reversed(result_messages)
            if message.get("role") == "assistant" and not message.get("tool_calls")
        ),
        None,
    )
    return {
        "final_answer": final,
        "trace": trace,
        "messages": result_messages,
        "usage": state.get("usage", []),
        "budget": context.budget.usage(),
        "stop_reason": state.get("stop_reason"),
        "plan": coordinator.result() if coordinator else None,
    }
