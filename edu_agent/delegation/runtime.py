from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable

from ..data import db
from ..runtime.artifacts import ArtifactStore
from ..runtime.models import RunContext
from ..runtime.tool_executor import ExecutionPolicy, PolicyToolExecutor, ToolOutcome
from ..state.store import FencingTokenRejected, RunCancelled
from .models import (
    DelegationBatchResult,
    DelegationError,
    DelegationLimitExceeded,
    DelegationPolicy,
    DelegationTimedOut,
    PartialSuccessPolicy,
    SubagentInput,
    SubtaskResult,
    SubtaskStatus,
    SubtaskUsage,
    TeachingSubtask,
    TeachingTaskKind,
)
from .persistence import DelegationState


CHILD_SYSTEM_PROMPT = (
    "你是 EduAgent 的受限只读教学分析子 Agent。只完成明确传入的 task；"
    "只使用宿主提供的工具面，不读取父会话历史或长期记忆，不执行写操作，"
    "不把总结当作证据。返回结构化结果，证据与引用由父级运行时复验。"
)


_ROLE_RANK = {"student": 0, "teacher": 1, "admin": 2, "system": 3}

_TASK_TOOLS: dict[TeachingTaskKind, tuple[str, ...]] = {
    TeachingTaskKind.class_analysis: (
        "list_exams",
        "query_student_scores",
        "analyze_class_errors",
        "get_score_distribution",
    ),
    TeachingTaskKind.chapter_retrieval: ("retrieve_course_materials",),
    TeachingTaskKind.intervention_grade: (
        "list_exams",
        "query_student_scores",
        "get_score_distribution",
    ),
    TeachingTaskKind.intervention_weakness: (
        "diagnose_weak_points",
        "analyze_class_errors",
    ),
    TeachingTaskKind.intervention_resources: (
        "retrieve_course_materials",
        "search_questions",
    ),
}


def _find_values(value: Any, keys: set[str]) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and isinstance(item, (str, int)):
                found.add(str(item))
            elif key in keys and isinstance(item, list):
                found.update(str(entry) for entry in item if isinstance(entry, (str, int)))
            found.update(_find_values(item, keys))
    elif isinstance(value, list):
        for item in value:
            found.update(_find_values(item, keys))
    return found


def _result_summary(task: TeachingSubtask, outputs: list[dict[str, Any]]) -> str:
    return json.dumps(
        {
            "task": task.task,
            "kind": task.kind.value,
            "outputs": outputs,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


@dataclass
class ChildExecution:
    record: dict[str, Any]
    task: TeachingSubtask
    input: SubagentInput
    context: RunContext
    executor: PolicyToolExecutor
    connection: Any
    state: DelegationState
    worker_owner: str
    stop_event: threading.Event
    stop_reason: list[str]
    deadline: float

    def checkpoint(self, boundary: str) -> None:
        if time.monotonic() >= self.deadline or self.stop_reason[:1] == ["timeout"]:
            self.stop_reason[:] = ["timeout"]
            self.stop_event.set()
            raise DelegationTimedOut("CHILD_TIMEOUT")
        if self.stop_event.is_set():
            raise RunCancelled(self.stop_reason[0] if self.stop_reason else "CHILD_CANCELLED")
        self.state.checkpoint(
            self.record["id"],
            worker_owner=self.worker_owner,
            boundary=boundary,
        )

    def execute_tool(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        self.checkpoint(f"child.{name}.before")
        outcome = self.executor.execute(
            name,
            arguments,
            self.context,
            conn=self.connection,
            allowed_tools=set(self.record["allowed_tools"]),
            tool_call_id=f"{self.record['id']}:{self.context.budget.tool_calls + 1}",
            plan_step_id=self.record.get("plan_step_id"),
        )
        self.checkpoint(f"child.{name}.after")
        if not outcome.ok:
            error = outcome.error or {"code": "TOOL_FAILED", "message": name}
            raise DelegationError(f"{error.get('code')}: {error.get('message')}")
        return outcome


class ParentEvidenceVerifier:
    def __init__(self, state_store, provider, artifact_store: ArtifactStore):
        self.state_store = state_store
        self.provider = provider
        self.artifact_store = artifact_store

    def verify(
        self,
        result: SubtaskResult,
        *,
        child_record: dict[str, Any],
        parent_context: RunContext,
    ) -> tuple[bool, str | None]:
        if result.run_id != child_record["id"] or result.task_key != child_record["task_key"]:
            return False, "RESULT_LINEAGE_MISMATCH"
        with self.state_store.connect() as connection:
            for evidence_id in result.evidence_ids:
                row = connection.execute(
                    "SELECT * FROM tool_events WHERE id=?", (evidence_id,)
                ).fetchone()
                if row is None:
                    return False, f"EVIDENCE_NOT_FOUND:{evidence_id}"
                if (
                    row["run_id"] != child_record["id"]
                    or row["session_id"] != child_record["session_id"]
                    or row["tool_name"] not in child_record["allowed_tools"]
                ):
                    return False, f"EVIDENCE_SCOPE_DENIED:{evidence_id}"
                outcome = json.loads(row["outcome_json"])
                if not outcome.get("ok"):
                    return False, f"EVIDENCE_TOOL_FAILED:{evidence_id}"
        child_context = RunContext.create(
            session_id=child_record["session_id"],
            run_id=child_record["id"],
            actor_id=child_record["actor_id"],
            tenant_id=child_record["tenant_id"],
            role=child_record["role"],
            course_ids=set(child_record["course_ids"]),
        )
        citation_verifier = getattr(self.provider, "verify_citation", None)
        for citation in result.citations:
            if not callable(citation_verifier):
                return False, f"CITATION_VERIFIER_UNAVAILABLE:{citation}"
            if not citation_verifier(citation, child_context):
                return False, f"CITATION_CHILD_SCOPE_DENIED:{citation}"
            if not citation_verifier(citation, parent_context):
                return False, f"CITATION_PARENT_SCOPE_DENIED:{citation}"
        for artifact_id in result.artifacts:
            verified = self.state_store.verify_artifact(
                artifact_id,
                actor_id=child_record["actor_id"],
                tenant_id=child_record["tenant_id"],
                run_id=child_record["id"],
                session_id=child_record["session_id"],
            )
            if verified is None:
                return False, f"ARTIFACT_SCOPE_OR_INTEGRITY_FAILED:{artifact_id}"
        if result.status == SubtaskStatus.completed and not result.evidence_ids:
            return False, "COMPLETED_RESULT_HAS_NO_EVIDENCE"
        return True, None


class DelegationRuntime:
    def __init__(
        self,
        state_store,
        provider,
        *,
        artifact_store: ArtifactStore,
        policy: DelegationPolicy | None = None,
        connection_factory: Callable[[], Any] | None = None,
        child_runner: Callable[[ChildExecution], dict[str, Any]] | None = None,
        result_inline_chars: int = 4_000,
    ):
        self.state_store = state_store
        self.provider = provider
        self.artifact_store = artifact_store
        if self.artifact_store.state_store is None:
            self.artifact_store.state_store = state_store
        self.policy = policy or DelegationPolicy()
        self.connection_factory = connection_factory or db.connect
        self.child_runner = child_runner or self._run_teaching_task
        self.result_inline_chars = max(256, int(result_inline_chars))
        self.state = DelegationState(state_store)
        self.worker_id = f"delegation:{uuid.uuid4().hex}"
        self._pool = ThreadPoolExecutor(
            max_workers=self.policy.max_concurrency,
            thread_name_prefix="edu-agent-child",
        )
        self._active_lock = threading.Lock()
        self._active_stops: dict[str, tuple[threading.Event, list[str]]] = {}
        self.recovery_report = self.state.recover_expired()

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=False)

    def __enter__(self) -> DelegationRuntime:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def cancel_root(self, parent_context: RunContext, *, reason: str) -> int:
        parent = self.state.get_run(
            parent_context.run_id,
            actor_id=parent_context.actor_id,
            tenant_id=parent_context.tenant_id,
        )
        root_run_id = parent["root_run_id"] if parent else parent_context.run_id
        count = self.state.cancel_root(
            root_run_id,
            actor_id=parent_context.actor_id,
            tenant_id=parent_context.tenant_id,
            reason=reason,
        )
        with self._active_lock:
            targets = list(self._active_stops.values())
        for stop_event, stop_reason in targets:
            stop_reason[:] = [reason]
            stop_event.set()
        return count

    def delegate(
        self,
        parent_context: RunContext,
        tasks: list[TeachingSubtask],
        *,
        partial_policy: PartialSuccessPolicy = PartialSuccessPolicy.best_effort,
        required_quorum: int | None = None,
    ) -> DelegationBatchResult:
        if not tasks:
            raise ValueError("delegation tasks 不能为空")
        if partial_policy == PartialSuccessPolicy.required_quorum:
            if required_quorum is None or not 1 <= required_quorum <= len(tasks):
                raise ValueError("required_quorum 必须位于 1..task_count")
        elif required_quorum is not None:
            raise ValueError("只有 required_quorum 策略可以设置 quorum")
        started = time.monotonic()
        entries = [self._prepare_entry(parent_context, task) for task in tasks]
        records = self.state.create_batch(
            parent_context=parent_context,
            entries=entries,
            root_budget=self.policy.root_budget(),
            child_budget=self.policy.child_budget(),
            max_depth=self.policy.max_depth,
            max_children_per_parent=self.policy.max_children_per_parent,
        )
        by_key = {record["task_key"]: record for record in records}
        futures: dict[Future, str] = {}
        for task in tasks:
            record = by_key[task.task_key]
            if record["status"] in {
                SubtaskStatus.completed.value,
                SubtaskStatus.failed.value,
                SubtaskStatus.timed_out.value,
                SubtaskStatus.cancelled.value,
            }:
                continue
            future = self._pool.submit(self._execute_child, record, task)
            futures[future] = record["id"]

        pending = set(futures)
        fail_fast_triggered = False
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                try:
                    future.result()
                except Exception:
                    # The child wrapper persists a failed terminal state for
                    # normal errors. A construction/worker crash is handled
                    # here as a failed subtask so one broken worker cannot
                    # strand the parent batch.
                    run_id = futures[future]
                    self.state.reject_queued(run_id, "WORKER_CRASH")
                record = self.state.get_run(
                    futures[future],
                    actor_id=parent_context.actor_id,
                    tenant_id=parent_context.tenant_id,
                )
                if (
                    partial_policy == PartialSuccessPolicy.fail_fast
                    and record["status"] != SubtaskStatus.completed.value
                    and not fail_fast_triggered
                ):
                    fail_fast_triggered = True
                    outstanding = {futures[item] for item in pending}
                    self.state.cancel_children(outstanding, reason="FAIL_FAST_SIBLING_FAILED")
                    self._signal_children(outstanding, "FAIL_FAST_SIBLING_FAILED")
                    for item in list(pending):
                        item.cancel()
            if partial_policy == PartialSuccessPolicy.required_quorum:
                current = [
                    self.state.get_run(
                        by_key[task.task_key]["id"],
                        actor_id=parent_context.actor_id,
                        tenant_id=parent_context.tenant_id,
                    )
                    for task in tasks
                ]
                completed = sum(item["status"] == "completed" for item in current)
                possible = completed + sum(
                    item["status"] in {"queued", "running", "cancel_requested"}
                    for item in current
                )
                if possible < int(required_quorum or 0):
                    outstanding = {futures[item] for item in pending}
                    self.state.cancel_children(outstanding, reason="QUORUM_UNREACHABLE")
                    self._signal_children(outstanding, "QUORUM_UNREACHABLE")

        verifier = ParentEvidenceVerifier(
            self.state_store,
            self.provider,
            self.artifact_store,
        )
        results: list[SubtaskResult] = []
        for task in tasks:
            record = self.state.get_run(
                by_key[task.task_key]["id"],
                actor_id=parent_context.actor_id,
                tenant_id=parent_context.tenant_id,
            )
            result = self._record_result(record)
            valid, reason = verifier.verify(
                result,
                child_record=record,
                parent_context=parent_context,
            )
            if not valid and result.status == SubtaskStatus.completed:
                result = SubtaskResult(
                    run_id=result.run_id,
                    task_key=result.task_key,
                    status=SubtaskStatus.failed,
                    summary="",
                    evidence_ids=result.evidence_ids,
                    citations=result.citations,
                    artifacts=result.artifacts,
                    usage=result.usage,
                    warnings=(*result.warnings, "PARENT_EVIDENCE_REJECTED"),
                    failure_reason=reason,
                )
                self.state.reject_completed_result(record["id"], reason or "EVIDENCE_REJECTED")
            results.append(result)

        completed = sum(result.status == SubtaskStatus.completed for result in results)
        if partial_policy == PartialSuccessPolicy.fail_fast:
            status = "completed" if completed == len(results) else "failed"
        elif partial_policy == PartialSuccessPolicy.required_quorum:
            status = "completed" if completed >= int(required_quorum or 0) else "failed"
        elif completed == len(results):
            status = "completed"
        elif completed:
            status = "partial"
        else:
            status = "failed"
        parent_record = self.state.get_run(
            parent_context.run_id,
            actor_id=parent_context.actor_id,
            tenant_id=parent_context.tenant_id,
        )
        root_run_id = parent_record["root_run_id"] if parent_record else parent_context.run_id
        tree = self.state.tree(
            root_run_id,
            actor_id=parent_context.actor_id,
            tenant_id=parent_context.tenant_id,
        )
        return DelegationBatchResult(
            parent_run_id=parent_context.run_id,
            root_run_id=root_run_id,
            policy=partial_policy,
            status=status,
            required_quorum=required_quorum,
            results=tuple(results),
            root_usage=tree["usage"],
            elapsed_ms=round((time.monotonic() - started) * 1000, 2),
        )

    def _prepare_entry(self, parent_context: RunContext, task: TeachingSubtask) -> dict[str, Any]:
        role = self._child_role(parent_context.role, task.requested_role)
        if not task.course_ids:
            raise PermissionError("delegated task 必须声明非空 course scope")
        if not parent_context.course_ids or not task.course_ids.issubset(parent_context.course_ids):
            raise PermissionError("delegated task course scope 超出父级权限")
        model = task.model or self.policy.default_model
        if model not in self.policy.allowed_models:
            raise DelegationLimitExceeded(f"child model 不在策略允许范围：{model}")
        allowed_tools = self._tool_surface(task, role)
        parent_record = self.state.get_run(
            parent_context.run_id,
            actor_id=parent_context.actor_id,
            tenant_id=parent_context.tenant_id,
        )
        if parent_record is not None:
            if _ROLE_RANK[role] > _ROLE_RANK[parent_record["role"]]:
                raise PermissionError("nested child role 超出 parent child scope")
            parent_tools = set(parent_record["allowed_tools"])
            if not set(allowed_tools).issubset(parent_tools):
                raise PermissionError("nested child tool surface 超出 parent child scope")
            child_categories = {
                self.provider.get_spec(name).category
                for name in allowed_tools
                if self.provider.get_spec(name) is not None
            }
            if not child_categories.issubset(set(parent_record["allowed_categories"])):
                raise PermissionError("nested child tool categories 超出 parent child scope")
        evidence, citations = self._validated_inputs(parent_context, task)
        plan_projection = {
            "parent_run_id": parent_context.run_id,
            "plan_step_id": task.plan_step_id,
            "task_key": task.task_key,
            "task_kind": task.kind.value,
            "allowed_tools": allowed_tools,
            "iteration_budget": self.policy.max_model_calls_per_child,
        }
        child_input = SubagentInput(
            system_prompt=CHILD_SYSTEM_PROMPT,
            messages=(
                {"role": "system", "content": CHILD_SYSTEM_PROMPT},
                {"role": "user", "content": task.task},
            ),
            plan_projection=plan_projection,
            evidence=tuple(evidence),
            citations=tuple(citations),
        )
        return {
            "task_spec": task.to_dict(),
            "input": {
                "system_prompt": child_input.system_prompt,
                "messages": list(child_input.messages),
                "plan_projection": child_input.plan_projection,
                "evidence": list(child_input.evidence),
                "citations": list(child_input.citations),
            },
            "role": role,
            "model": model,
            "allowed_tools": allowed_tools,
            "allowed_categories": sorted(self.policy.allowed_tool_categories),
            "can_delegate": self.policy.allow_child_delegation,
        }

    def _child_role(self, parent_role: str, requested: str | None) -> str:
        if parent_role not in _ROLE_RANK:
            raise PermissionError(f"未知 parent role：{parent_role}")
        candidates = [
            role
            for role in self.policy.allowed_child_roles
            if role in _ROLE_RANK and _ROLE_RANK[role] <= _ROLE_RANK[parent_role]
        ]
        if requested is not None:
            if requested not in candidates:
                raise PermissionError("requested child role 会扩大父级权限或违反策略")
            return requested
        if not candidates:
            raise PermissionError("父级角色与 child policy 没有权限交集")
        return max(candidates, key=_ROLE_RANK.__getitem__)

    def _tool_surface(self, task: TeachingSubtask, role: str) -> list[str]:
        required = _TASK_TOOLS[task.kind]
        selected: list[str] = []
        for name in required:
            spec = self.provider.get_spec(name) if hasattr(self.provider, "get_spec") else None
            if spec is None:
                raise DelegationLimitExceeded(f"教学委派消费者缺少工具：{name}")
            if spec.is_mutating(task.arguments):
                raise PermissionError(f"写操作不得委派：{name}")
            if spec.category not in self.policy.allowed_tool_categories:
                raise PermissionError(f"工具类别不允许委派：{name}/{spec.category}")
            if role not in spec.allowed_roles:
                raise PermissionError(f"child role {role} 无权调用 {name}")
            selected.append(name)
        return selected

    def _validated_inputs(
        self,
        parent_context: RunContext,
        task: TeachingSubtask,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        evidence: list[dict[str, Any]] = []
        with self.state_store.connect() as connection:
            for evidence_id in task.input_evidence_ids:
                row = connection.execute(
                    "SELECT * FROM evidence WHERE id=?", (evidence_id,)
                ).fetchone()
                if row is None:
                    raise PermissionError(f"输入 evidence 不存在：{evidence_id}")
                if (
                    row["run_id"] != parent_context.run_id
                    or row["session_id"] != parent_context.session_id
                    or row["actor_id"] != parent_context.actor_id
                    or row["tenant_id"] != parent_context.tenant_id
                    or row["status"] != "accepted"
                ):
                    raise PermissionError(f"输入 evidence 超出父 run scope：{evidence_id}")
                evidence.append(
                    {
                        "id": int(row["id"]),
                        "kind": row["kind"],
                        "tool_name": row["tool_name"],
                        "artifact_id": row["artifact_id"],
                        "citation": row["citation"],
                    }
                )
        citation_verifier = getattr(self.provider, "verify_citation", None)
        citations: list[str] = []
        for citation in task.input_citations:
            if not callable(citation_verifier) or not citation_verifier(citation, parent_context):
                raise PermissionError(f"输入 citation 不存在或超出 scope：{citation}")
            citations.append(citation)
        return evidence, citations

    def _execute_child(self, record: dict[str, Any], task: TeachingSubtask) -> None:
        queue_deadline = time.monotonic() + self.policy.child_timeout_seconds
        claimed = None
        while claimed is None and time.monotonic() < queue_deadline:
            claimed = self.state.claim(
                record["id"],
                worker_owner=self.worker_id,
                max_concurrency=self.policy.max_concurrency,
                lease_seconds=self.policy.worker_lease_seconds,
            )
            if claimed is None:
                time.sleep(0.01)
        if claimed is None:
            self.state.reject_queued(record["id"], "GLOBAL_BACKPRESSURE_TIMEOUT")
            return
        if claimed["status"] in {
            SubtaskStatus.completed.value,
            SubtaskStatus.failed.value,
            SubtaskStatus.timed_out.value,
            SubtaskStatus.cancelled.value,
        }:
            return

        stop_event = threading.Event()
        stop_reason: list[str] = []
        with self._active_lock:
            self._active_stops[record["id"]] = (stop_event, stop_reason)
        deadline = time.monotonic() + self.policy.child_timeout_seconds
        context = None
        input_payload = record["input"]
        connection = None
        heartbeat_stop = threading.Event()
        heartbeat = None
        child_started = time.monotonic()
        try:
            context = RunContext.create(
                session_id=record["session_id"],
                run_id=record["id"],
                actor_id=record["actor_id"],
                tenant_id=record["tenant_id"],
                role=record["role"],
                course_ids=set(record["course_ids"]),
                max_model_calls=int(record["budget"]["max_model_calls"]),
                max_tool_calls=int(record["budget"]["max_tool_calls"]),
            )
            child_input = SubagentInput(
                system_prompt=input_payload["system_prompt"],
                messages=tuple(input_payload["messages"]),
                plan_projection=input_payload["plan_projection"],
                evidence=tuple(input_payload["evidence"]),
                citations=tuple(input_payload["citations"]),
            )
            connection = self.connection_factory()
            executor = PolicyToolExecutor(
                self.provider,
                policy=ExecutionPolicy(
                    require_write_approval=True,
                    allow_local_code_execution=False,
                    enforce_roles=True,
                ),
                state_store=self.state_store,
            )
            execution = ChildExecution(
                claimed,
                task,
                child_input,
                context,
                executor,
                connection,
                self.state,
                self.worker_id,
                stop_event,
                stop_reason,
                deadline,
            )
            context.bind_control_check(execution.checkpoint)
            heartbeat = threading.Thread(
                target=self._heartbeat_loop,
                args=(execution, heartbeat_stop),
                daemon=True,
                name=f"edu-agent-child-heartbeat-{record['id'][:8]}",
            )
            heartbeat.start()
            child_started = time.monotonic()
            payload = self.child_runner(execution) or {}
            execution.checkpoint("child.before_result")
            payload.setdefault(
                "usage",
                {
                    "input_tokens": max(
                        1,
                        len(json.dumps(input_payload, ensure_ascii=False, default=str)) // 4,
                    )
                },
            )
            usage = self._usage(context, payload, child_started)
            self._enforce_actual_budget(record, usage)
            evidence_ids = tuple(
                int(item["id"])
                for item in self.state_store.get_tool_events(
                    run_id=record["id"],
                    session_id=record["session_id"],
                )
                if json.loads(item["outcome_json"]).get("ok")
            )
            summary = str(payload.get("summary", ""))
            citations = tuple(sorted(set(payload.get("citations", ()))))
            artifacts = list(dict.fromkeys(payload.get("artifacts", ())))
            result_payload = {
                "run_id": record["id"],
                "task_key": record["task_key"],
                "status": SubtaskStatus.completed.value,
                "summary": summary,
                "evidence_ids": list(evidence_ids),
                "citations": list(citations),
                "artifacts": artifacts,
                "usage": usage.to_dict(),
                "warnings": list(payload.get("warnings", ())),
            }
            serialized = json.dumps(result_payload, ensure_ascii=False, default=str)
            result_artifact_id = None
            if len(serialized) > self.result_inline_chars:
                artifact = self.artifact_store.write_text(
                    serialized,
                    context=context,
                    kind="subtask-result",
                    metadata={
                        "parent_run_id": record["parent_run_id"],
                        "root_run_id": record["root_run_id"],
                        "task_key": record["task_key"],
                    },
                )
                artifacts.append(artifact.id)
                result_artifact_id = artifact.id
                result_payload["artifacts"] = artifacts
                result_payload["summary"] = summary[: self.result_inline_chars]
                result_payload["warnings"].append("FULL_RESULT_STORED_AS_ARTIFACT")
            self.state.finish(
                record["id"],
                worker_owner=self.worker_id,
                status=SubtaskStatus.completed,
                usage=usage.to_dict(),
                result=result_payload,
                result_artifact_id=result_artifact_id,
            )
        except DelegationTimedOut as error:
            if context is None:
                self.state.fail_running_worker(
                    record["id"], worker_owner=self.worker_id, reason=str(error)
                )
            else:
                usage = self._usage(context, {}, child_started)
                self.state.finish(
                    record["id"],
                    worker_owner=self.worker_id,
                    status=SubtaskStatus.timed_out,
                    usage=usage.to_dict(),
                    result=None,
                    failure_reason=str(error),
                )
        except RunCancelled as error:
            if context is None:
                self.state.fail_running_worker(
                    record["id"], worker_owner=self.worker_id, reason=str(error)
                )
            else:
                usage = self._usage(context, {}, child_started)
                self.state.finish(
                    record["id"],
                    worker_owner=self.worker_id,
                    status=SubtaskStatus.cancelled,
                    usage=usage.to_dict(),
                    result=None,
                    cancel_reason=str(error),
                )
        except Exception as error:
            reason = f"{type(error).__name__}: {error}"
            if context is None:
                self.state.fail_running_worker(
                    record["id"], worker_owner=self.worker_id, reason=reason
                )
            else:
                usage = self._usage(context, {}, child_started)
                self.state.finish(
                    record["id"],
                    worker_owner=self.worker_id,
                    status=SubtaskStatus.failed,
                    usage=usage.to_dict(),
                    result=None,
                    failure_reason=reason,
                )
        finally:
            heartbeat_stop.set()
            if heartbeat is not None:
                heartbeat.join(timeout=1)
            close = getattr(connection, "close", None)
            if callable(close):
                close()
            with self._active_lock:
                self._active_stops.pop(record["id"], None)

    def _heartbeat_loop(self, execution: ChildExecution, stopped: threading.Event) -> None:
        interval = max(0.05, min(1.0, self.policy.worker_lease_seconds / 3))
        while not stopped.wait(interval):
            if time.monotonic() >= execution.deadline:
                execution.stop_reason[:] = ["timeout"]
                execution.stop_event.set()
                stopped.set()
                return
            try:
                self.state.checkpoint(
                    execution.record["id"],
                    worker_owner=execution.worker_owner,
                    boundary="child.heartbeat",
                )
                if not self.state.heartbeat(
                    execution.record["id"],
                    worker_owner=execution.worker_owner,
                    lease_seconds=self.policy.worker_lease_seconds,
                ):
                    execution.stop_reason[:] = ["WORKER_LEASE_LOST"]
                    execution.stop_event.set()
                    return
            except (RunCancelled, FencingTokenRejected) as error:
                execution.stop_reason[:] = [str(error)]
                execution.stop_event.set()
                return

    def _signal_children(self, run_ids: set[str], reason: str) -> None:
        with self._active_lock:
            targets = [self._active_stops[run_id] for run_id in run_ids if run_id in self._active_stops]
        for stop_event, stop_reason in targets:
            stop_reason[:] = [reason]
            stop_event.set()

    @staticmethod
    def _usage(
        context: RunContext,
        payload: dict[str, Any],
        started: float,
    ) -> SubtaskUsage:
        usage = payload.get("usage", {})
        model_calls = max(
            context.budget.model_calls,
            int(usage.get("model_calls", 0)),
        )
        tool_calls = max(
            context.budget.tool_calls,
            int(usage.get("tool_calls", 0)),
        )
        input_tokens = int(usage.get("input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))
        return SubtaskUsage(
            model_calls=model_calls,
            tool_calls=tool_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=float(usage.get("estimated_cost_usd", 0.0)),
            duration_ms=round((time.monotonic() - started) * 1000, 2),
        )

    @staticmethod
    def _enforce_actual_budget(record: dict[str, Any], usage: SubtaskUsage) -> None:
        budget = record["budget"]
        if usage.model_calls > int(budget["max_model_calls"]):
            raise DelegationLimitExceeded("CHILD_MODEL_BUDGET_EXCEEDED")
        if usage.tool_calls > int(budget["max_tool_calls"]):
            raise DelegationLimitExceeded("CHILD_TOOL_BUDGET_EXCEEDED")
        if usage.total_tokens > int(budget["max_tokens"]):
            raise DelegationLimitExceeded("CHILD_TOKEN_BUDGET_EXCEEDED")
        if usage.estimated_cost_usd > float(budget["max_cost_usd"]):
            raise DelegationLimitExceeded("CHILD_COST_BUDGET_EXCEEDED")

    def _run_teaching_task(self, execution: ChildExecution) -> dict[str, Any]:
        arguments = execution.task.arguments
        outputs: list[dict[str, Any]] = []

        def call(name: str, tool_arguments: dict[str, Any]) -> dict[str, Any]:
            outcome = execution.execute_tool(name, tool_arguments)
            data = outcome.data if isinstance(outcome.data, dict) else {"value": outcome.data}
            outputs.append({"tool": name, "data": data})
            return data

        if execution.task.kind in {
            TeachingTaskKind.class_analysis,
            TeachingTaskKind.intervention_grade,
        }:
            course_id = int(arguments["course_id"])
            class_id = int(arguments["class_id"])
            exams = call(
                "list_exams",
                {"class_id": class_id, "course_id": course_id, "page_size": 50},
            ).get("exams", [])
            requested_exam = arguments.get("exam_id")
            allowed_exam_ids = {int(item["id"]) for item in exams}
            if requested_exam is not None and int(requested_exam) not in allowed_exam_ids:
                raise PermissionError("EXAM_OUTSIDE_CHILD_COURSE_SCOPE")
            exam_id = int(requested_exam) if requested_exam is not None else (
                int(exams[0]["id"]) if exams else None
            )
            if exam_id is None:
                raise DelegationError("NO_EXAM_IN_SCOPED_CLASS")
            call(
                "query_student_scores",
                {
                    "exam_id": exam_id,
                    "class_id": class_id,
                    "only_failed": bool(arguments.get("only_failed", False)),
                    "page_size": int(arguments.get("page_size", 100)),
                },
            )
            if execution.task.kind == TeachingTaskKind.class_analysis:
                call(
                    "analyze_class_errors",
                    {"exam_id": exam_id, "top": int(arguments.get("top", 10))},
                )
            call("get_score_distribution", {"exam_id": exam_id})
        elif execution.task.kind == TeachingTaskKind.chapter_retrieval:
            call(
                "retrieve_course_materials",
                {
                    "query": str(arguments["query"]),
                    "course_id": int(arguments["course_id"]),
                    "limit": int(arguments.get("limit", 3)),
                    "mode": str(arguments.get("mode", "hybrid")),
                },
            )
        elif execution.task.kind == TeachingTaskKind.intervention_weakness:
            course_id = int(arguments["course_id"])
            class_id = int(arguments["class_id"])
            call(
                "diagnose_weak_points",
                {
                    "class_id": class_id,
                    "course_id": course_id,
                    "threshold": float(arguments.get("threshold", 0.6)),
                    "top": int(arguments.get("top", 10)),
                },
            )
            exam_id = arguments.get("exam_id")
            if exam_id is not None:
                exams = call(
                    "list_exams",
                    {"class_id": class_id, "course_id": course_id, "page_size": 50},
                ).get("exams", [])
                if int(exam_id) not in {int(item["id"]) for item in exams}:
                    raise PermissionError("EXAM_OUTSIDE_CHILD_COURSE_SCOPE")
                call(
                    "analyze_class_errors",
                    {"exam_id": int(exam_id), "top": int(arguments.get("top", 10))},
                )
        elif execution.task.kind == TeachingTaskKind.intervention_resources:
            course_id = int(arguments["course_id"])
            query = str(arguments["query"])
            call(
                "retrieve_course_materials",
                {
                    "query": query,
                    "course_id": course_id,
                    "limit": int(arguments.get("limit", 3)),
                    "mode": str(arguments.get("mode", "hybrid")),
                },
            )
            call(
                "search_questions",
                {
                    "course_id": course_id,
                    "keyword": arguments.get("keyword", query),
                    "page_size": int(arguments.get("question_limit", 5)),
                },
            )
        else:
            raise DelegationError(f"未注册的教学委派消费者：{execution.task.kind.value}")
        citations = sorted(_find_values(outputs, {"citation", "citation_id", "citations"}))
        artifacts = sorted(_find_values(outputs, {"artifact_id"}))
        return {
            "summary": _result_summary(execution.task, outputs),
            "citations": citations,
            "artifacts": artifacts,
            "warnings": [],
        }

    def _record_result(self, record: dict[str, Any]) -> SubtaskResult:
        if record["result"]:
            payload = dict(record["result"])
            payload["status"] = record["status"]
            payload["failure_reason"] = record["failure_reason"]
            payload["cancel_reason"] = record["cancel_reason"]
            return SubtaskResult.from_dict(payload)
        return SubtaskResult(
            run_id=record["id"],
            task_key=record["task_key"],
            status=SubtaskStatus(record["status"]),
            summary="",
            usage=SubtaskUsage.from_dict(record["usage"]),
            failure_reason=record["failure_reason"],
            cancel_reason=record["cancel_reason"],
        )

    def tree(self, parent_context: RunContext) -> dict[str, Any]:
        parent = self.state.get_run(
            parent_context.run_id,
            actor_id=parent_context.actor_id,
            tenant_id=parent_context.tenant_id,
        )
        root_run_id = parent["root_run_id"] if parent else parent_context.run_id
        return self.state.tree(
            root_run_id,
            actor_id=parent_context.actor_id,
            tenant_id=parent_context.tenant_id,
        )
