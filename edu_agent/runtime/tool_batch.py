from __future__ import annotations

import contextvars
import inspect
import queue
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from ..state.store import FencingTokenRejected, RunCancelled
from ..tools.manifest import ToolEffect, ToolManifest, ToolManifestEntry
from .cancellation import CancellationRequested, CancellationToken
from .models import RunContext
from .tool_arguments import (
    ToolArgumentError,
    normalize_tool_arguments,
    strict_parse_tool_arguments,
    summarize_raw_arguments,
    validate_tool_arguments,
)
from .tool_executor import PolicyToolExecutor, ToolOutcome


ConnectionFactory = Callable[[], sqlite3.Connection]


@dataclass(frozen=True)
class PlannedToolCall:
    index: int
    call_id: str
    name: str
    raw_arguments: str | dict | None
    arguments: dict[str, Any]
    entry: ToolManifestEntry | None
    validated: bool
    resource_keys: frozenset[str]
    parallel_eligible: bool
    barrier_reason: str | None
    timeout_seconds: float


@dataclass(frozen=True)
class ToolBatchSegment:
    segment_id: str
    mode: str
    calls: tuple[PlannedToolCall, ...]

    @property
    def parallel(self) -> bool:
        return self.mode == "parallel" and len(self.calls) > 1


@dataclass(frozen=True)
class ToolExecutionRecord:
    call: PlannedToolCall
    outcome: ToolOutcome
    started: bool


@dataclass(frozen=True)
class ToolBatchExecution:
    records: tuple[ToolExecutionRecord, ...]
    cancellation: RunCancelled | CancellationRequested | None = None
    terminal_error: BaseException | None = None


@dataclass(frozen=True)
class _WorkerCandidate:
    outcome: ToolOutcome | None
    error: BaseException | None
    started_at: float
    finished_at: float


class ToolBatchPlanner:
    """Split one assistant envelope into original-order safety segments."""

    def __init__(
        self,
        provider,
        manifest: ToolManifest,
        *,
        max_call_timeout_seconds: float,
    ):
        if max_call_timeout_seconds <= 0:
            raise ValueError("tool call timeout must be positive")
        self.provider = provider
        self.manifest = manifest
        self.max_call_timeout_seconds = float(max_call_timeout_seconds)

    def plan(
        self,
        tool_calls: Sequence[Mapping[str, Any]],
        context: RunContext,
    ) -> tuple[ToolBatchSegment, ...]:
        planned = tuple(
            self._plan_call(index, call, context)
            for index, call in enumerate(tool_calls)
        )
        segments: list[ToolBatchSegment] = []
        parallel: list[PlannedToolCall] = []
        resources: set[str] = set()

        def flush_parallel() -> None:
            if not parallel:
                return
            segments.append(
                ToolBatchSegment(
                    segment_id=f"segment-{len(segments):03d}",
                    mode="parallel",
                    calls=tuple(parallel),
                )
            )
            parallel.clear()
            resources.clear()

        for call in planned:
            if not call.parallel_eligible:
                flush_parallel()
                segments.append(
                    ToolBatchSegment(
                        segment_id=f"segment-{len(segments):03d}",
                        mode="barrier",
                        calls=(call,),
                    )
                )
                continue
            if resources.intersection(call.resource_keys):
                flush_parallel()
            parallel.append(call)
            resources.update(call.resource_keys)
        flush_parallel()
        return tuple(segments)

    def _plan_call(
        self,
        index: int,
        call: Mapping[str, Any],
        context: RunContext,
    ) -> PlannedToolCall:
        function = call.get("function") if isinstance(call, Mapping) else None
        call_id = call.get("id") if isinstance(call, Mapping) else None
        name = function.get("name") if isinstance(function, Mapping) else None
        raw_arguments = function.get("arguments") if isinstance(function, Mapping) else None
        safe_call_id = call_id if isinstance(call_id, str) and call_id else f"invalid-{index}"
        safe_name = name if isinstance(name, str) and name else "unknown"
        entry = self.manifest.get(safe_name)
        timeout = min(
            entry.timeout if entry is not None else self.max_call_timeout_seconds,
            self.max_call_timeout_seconds,
        )
        arguments, validated = self._validated_arguments(entry, raw_arguments)
        resources = (
            entry.resource_keys_for(arguments, context=context)
            if entry is not None and validated
            else frozenset()
        )
        reason = self._barrier_reason(
            entry,
            name=safe_name,
            validated=validated,
            context=context,
        )
        return PlannedToolCall(
            index=index,
            call_id=safe_call_id,
            name=safe_name,
            raw_arguments=raw_arguments,
            arguments=(
                arguments
                if validated
                else {"_raw": summarize_raw_arguments(raw_arguments)}
            ),
            entry=entry,
            validated=validated,
            resource_keys=resources,
            parallel_eligible=reason is None,
            barrier_reason=reason,
            timeout_seconds=timeout,
        )

    @staticmethod
    def _validated_arguments(
        entry: ToolManifestEntry | None,
        raw_arguments: str | dict | None,
    ) -> tuple[dict[str, Any], bool]:
        if entry is None:
            return {}, False
        try:
            parsed = strict_parse_tool_arguments(raw_arguments)
            protected = tuple(f"/{key}" for key in entry.mutation_parameters)
            normalized, _ = normalize_tool_arguments(
                parsed,
                entry.schema.get("parameters", {}),
                effect=entry.effect,
                data_classification=entry.data_classification,
                protected_pointers=protected,
            )
        except ToolArgumentError:
            return {}, False
        issues = validate_tool_arguments(normalized, entry.schema.get("parameters", {}))
        return normalized, not issues

    def _barrier_reason(
        self,
        entry: ToolManifestEntry | None,
        *,
        name: str,
        validated: bool,
        context: RunContext,
    ) -> str | None:
        if entry is None:
            return "unknown_tool"
        if not validated:
            return "invalid_arguments"
        if entry.effect is not ToolEffect.READ:
            return f"effect:{entry.effect.value}"
        if not entry.parallel_safe:
            return "parallel_safe:false"
        if not self._provider_allows_parallel(name, entry, context):
            return "provider_capability"
        return None

    def _provider_allows_parallel(
        self,
        name: str,
        entry: ToolManifestEntry,
        context: RunContext,
    ) -> bool:
        checker = getattr(self.provider, "supports_parallel_tool_calls", None)
        if not callable(checker):
            return False
        try:
            parameters = inspect.signature(checker).parameters.values()
        except (TypeError, ValueError):
            return False
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        names = {parameter.name for parameter in parameters}
        kwargs: dict[str, Any] = {}
        if accepts_kwargs or "context" in names:
            kwargs["context"] = context
        if accepts_kwargs or "entry" in names:
            kwargs["entry"] = entry
        try:
            return checker(name, **kwargs) is True
        except Exception:
            return False


class ToolBatchExecutor:
    """Execute planned segments while accepting results through one coordinator."""

    def __init__(
        self,
        executor: PolicyToolExecutor,
        context: RunContext,
        manifest: ToolManifest,
        *,
        max_workers: int,
        allowed_tools: set[str] | None = None,
        plan_step_id: str | None = None,
        connection_factory: ConnectionFactory | None = None,
        legacy_connection: sqlite3.Connection | None = None,
    ):
        if isinstance(max_workers, bool) or not isinstance(max_workers, int):
            raise TypeError("tool batch max_workers must be an integer")
        if max_workers < 1 or max_workers > 8:
            raise ValueError("tool batch max_workers must be between 1 and 8")
        if connection_factory is not None and legacy_connection is not None:
            raise ValueError("worker connection factory and legacy connection are exclusive")
        self.executor = executor
        self.worker_executor = executor.deferred_worker()
        self.context = context
        self.manifest = manifest
        self.max_workers = max_workers
        self.allowed_tools = allowed_tools
        self.plan_step_id = plan_step_id
        self.connection_factory = connection_factory
        self.legacy_connection = legacy_connection
        self._active_connection_ids: set[int] = set()
        self._connection_lock = threading.Lock()

    def execute(
        self,
        segments: Sequence[ToolBatchSegment],
        *,
        initial_cancellation: RunCancelled | CancellationRequested | None = None,
    ) -> ToolBatchExecution:
        records: dict[int, ToolExecutionRecord] = {}
        cancellation = initial_cancellation
        terminal_error: BaseException | None = None
        abort_reason: str | None = None

        for segment_index, segment in enumerate(segments):
            if cancellation is not None or terminal_error is not None or abort_reason is not None:
                reason = abort_reason or str(cancellation or terminal_error)
                for later in segments[segment_index:]:
                    for call in later.calls:
                        outcome = self._cancelled_outcome(call, reason, later)
                        records[call.index] = (
                            self._accept_outcome(
                                call,
                                later,
                                outcome,
                                started=False,
                            )
                            if cancellation is None and terminal_error is None
                            else ToolExecutionRecord(call, outcome, False)
                        )
                break
            try:
                self.context.check_control("tools.before_segment")
            except (RunCancelled, CancellationRequested) as error:
                cancellation = error
                for later in segments[segment_index:]:
                    for call in later.calls:
                        records[call.index] = ToolExecutionRecord(
                            call,
                            self._cancelled_outcome(call, str(error), later),
                            False,
                        )
                break

            accepted = self.context.budget.reserve_tool_calls(len(segment.calls))
            selected = segment.calls[:accepted]
            for call in segment.calls[accepted:]:
                outcome = self._synthetic_outcome(
                    call,
                    segment,
                    code="BUDGET_EXCEEDED",
                    message=(
                        "工具调用预算已耗尽"
                        f"（{self.context.budget.tool_calls}/{self.context.budget.max_tool_calls}）"
                    ),
                )
                records[call.index] = self._accept_outcome(
                    call,
                    segment,
                    outcome,
                    started=False,
                )

            if not selected:
                continue
            if self.legacy_connection is not None:
                segment_records, segment_cancel, segment_error, segment_abort = (
                    self._execute_legacy_segment(segment, selected)
                )
            else:
                segment_records, segment_cancel, segment_error, segment_abort = (
                    self._execute_worker_segment(segment, selected)
                )
            records.update({record.call.index: record for record in segment_records})
            cancellation = segment_cancel or cancellation
            terminal_error = segment_error or terminal_error
            abort_reason = segment_abort or abort_reason

        ordered = tuple(records[index] for index in sorted(records))
        return ToolBatchExecution(ordered, cancellation, terminal_error)

    def _execute_legacy_segment(
        self,
        segment: ToolBatchSegment,
        calls: Sequence[PlannedToolCall],
    ) -> tuple[
        tuple[ToolExecutionRecord, ...],
        RunCancelled | CancellationRequested | None,
        BaseException | None,
        str | None,
    ]:
        records: list[ToolExecutionRecord] = []
        cancellation = None
        terminal_error = None
        abort_reason = None
        for offset, call in enumerate(calls):
            if cancellation is not None or terminal_error is not None or abort_reason is not None:
                records.extend(
                    ToolExecutionRecord(
                        later,
                        self._cancelled_outcome(
                            later,
                            abort_reason or str(cancellation or terminal_error),
                            segment,
                        ),
                        False,
                    )
                    for later in calls[offset:]
                )
                break
            token = CancellationToken.with_timeout(
                call.timeout_seconds,
                parent=self.context.cancellation_token,
            )
            worker_context = self.context.for_tool_worker(cancellation_token=token)
            started_at = time.monotonic()
            self._emit_started(worker_context, call, segment)
            candidate = self._invoke(
                call,
                worker_context,
                connection=self.legacy_connection,
                started_at=started_at,
            )
            record, cancellation, terminal_error, abort_reason = self._accept_candidate(
                call,
                segment,
                candidate,
                token=token,
            )
            records.append(record)
            token.close()
        return tuple(records), cancellation, terminal_error, abort_reason

    def _execute_worker_segment(
        self,
        segment: ToolBatchSegment,
        calls: Sequence[PlannedToolCall],
    ) -> tuple[
        tuple[ToolExecutionRecord, ...],
        RunCancelled | CancellationRequested | None,
        BaseException | None,
        str | None,
    ]:
        notifications: queue.Queue[tuple[str, int, Any]] = queue.Queue()
        futures: dict[int, Future[_WorkerCandidate]] = {}
        tokens: dict[int, CancellationToken] = {}
        deadlines: dict[int, float] = {}
        started: set[int] = set()
        resolved: dict[int, ToolExecutionRecord] = {}
        cancellation = None
        terminal_error = None
        abort_reason = None
        pool = ThreadPoolExecutor(
            max_workers=min(self.max_workers, len(calls)),
            thread_name_prefix="edu-tool",
        )

        def signal_cancel(_cancellation) -> None:
            notifications.put(("cancel", -1, _cancellation))

        unlink_cancel = self.context.cancellation_token.register(signal_cancel)
        try:
            for call in calls:
                copied_context = contextvars.copy_context()
                future = pool.submit(
                    copied_context.run,
                    self._worker_entry,
                    call,
                    segment,
                    notifications,
                )
                futures[call.index] = future
                future.add_done_callback(
                    lambda completed, index=call.index: notifications.put(
                        ("done", index, completed)
                    )
                )

            while len(resolved) < len(calls):
                parent_cancellation = self.context.cancellation_token.cancellation
                if parent_cancellation is not None:
                    notifications.put(("cancel", -1, parent_cancellation))
                timeout = self._next_wait_timeout(deadlines, resolved)
                try:
                    kind, index, payload = notifications.get(timeout=timeout)
                except queue.Empty:
                    now = time.monotonic()
                    expired = [
                        index
                        for index, deadline in deadlines.items()
                        if index not in resolved and deadline <= now
                    ]
                    for expired_index in expired:
                        token = tokens.get(expired_index)
                        if token is not None:
                            token.cancel(
                                "tool call timeout exceeded",
                                source="tool_timeout",
                            )
                        call = self._call_by_index(calls, expired_index)
                        outcome = self._synthetic_outcome(
                            call,
                            segment,
                            code="TOOL_TIMEOUT",
                            message=(
                                f"工具 {call.name} 超过 {call.timeout_seconds:g} 秒调用上限"
                            ),
                            duration_ms=call.timeout_seconds * 1000,
                        )
                        resolved[expired_index] = self._accept_outcome(
                            call,
                            segment,
                            outcome,
                            started=True,
                        )
                        abort_reason = "先前工具超时；后续 barrier 未启动"
                    if expired:
                        self._cancel_queued_after_timeout(
                            calls,
                            segment,
                            futures,
                            started,
                            resolved,
                        )
                    continue

                if kind == "started":
                    token, deadline = payload
                    tokens[index] = token
                    deadlines[index] = deadline
                    started.add(index)
                    continue
                if kind == "cancel":
                    if cancellation is None:
                        cancellation = CancellationRequested(
                            payload,
                            boundary="tools.batch",
                        )
                    for token in tokens.values():
                        token.cancel(payload.reason, source=payload.source)
                    for future in futures.values():
                        future.cancel()
                    for call in calls:
                        if call.index not in resolved:
                            resolved[call.index] = ToolExecutionRecord(
                                call,
                                self._cancelled_outcome(call, str(cancellation), segment),
                                call.index in started,
                            )
                    continue
                if kind != "done" or index in resolved:
                    continue
                call = self._call_by_index(calls, index)
                future = payload
                if future.cancelled():
                    resolved[index] = ToolExecutionRecord(
                        call,
                        self._cancelled_outcome(
                            call,
                            abort_reason or "工具 worker 在启动前被取消",
                            segment,
                        ),
                        index in started,
                    )
                    continue
                try:
                    candidate = future.result()
                except BaseException as error:
                    candidate = _WorkerCandidate(
                        None,
                        error,
                        time.monotonic(),
                        time.monotonic(),
                    )
                record, call_cancel, call_error, call_abort = self._accept_candidate(
                    call,
                    segment,
                    candidate,
                    token=tokens.get(index),
                )
                resolved[index] = record
                cancellation = call_cancel or cancellation
                terminal_error = call_error or terminal_error
                abort_reason = call_abort or abort_reason
                if call_abort is not None:
                    self._cancel_queued_after_timeout(
                        calls,
                        segment,
                        futures,
                        started,
                        resolved,
                    )
                if cancellation is not None or terminal_error is not None:
                    for token in tokens.values():
                        token.cancel(str(cancellation or terminal_error), source="batch_abort")
                    for pending_index, future in futures.items():
                        if pending_index not in resolved:
                            future.cancel()
                            pending_call = self._call_by_index(calls, pending_index)
                            resolved[pending_index] = ToolExecutionRecord(
                                pending_call,
                                self._cancelled_outcome(
                                    pending_call,
                                    str(cancellation or terminal_error),
                                    segment,
                                ),
                                pending_index in started,
                            )
        finally:
            unlink_cancel()
            for token in tokens.values():
                token.close()
            pool.shutdown(wait=False, cancel_futures=True)
        return (
            tuple(resolved[call.index] for call in calls),
            cancellation,
            terminal_error,
            abort_reason,
        )

    def _worker_entry(
        self,
        call: PlannedToolCall,
        segment: ToolBatchSegment,
        notifications: queue.Queue[tuple[str, int, Any]],
    ) -> _WorkerCandidate:
        token = CancellationToken.with_timeout(
            call.timeout_seconds,
            parent=self.context.cancellation_token,
        )
        started_at = time.monotonic()
        notifications.put(("started", call.index, (token, started_at + call.timeout_seconds)))
        worker_context = self.context.for_tool_worker(cancellation_token=token)
        try:
            self._emit_started(worker_context, call, segment)
            if self.connection_factory is None:
                return self._invoke(call, worker_context, connection=None, started_at=started_at)
            connection = self.connection_factory()
            if not isinstance(connection, sqlite3.Connection):
                raise TypeError("tool worker connection factory must return sqlite3.Connection")
            connection_id = id(connection)
            with self._connection_lock:
                if connection_id in self._active_connection_ids:
                    connection.close()
                    raise RuntimeError("tool worker connection factory returned a shared connection")
                self._active_connection_ids.add(connection_id)
            try:
                return self._invoke(
                    call,
                    worker_context,
                    connection=connection,
                    started_at=started_at,
                )
            finally:
                with self._connection_lock:
                    self._active_connection_ids.discard(connection_id)
                connection.close()
        except BaseException as error:
            return _WorkerCandidate(None, error, started_at, time.monotonic())
        finally:
            token.close()

    def _invoke(
        self,
        call: PlannedToolCall,
        worker_context: RunContext,
        *,
        connection: sqlite3.Connection | None,
        started_at: float,
    ) -> _WorkerCandidate:
        try:
            outcome = self._operation_guard(call)
            if outcome is None:
                outcome = self.worker_executor.execute_raw(
                    call.name,
                    call.raw_arguments,
                    worker_context,
                    conn=connection,
                    allowed_tools=self.allowed_tools,
                    tool_call_id=call.call_id,
                    plan_step_id=self.plan_step_id,
                    manifest=self.manifest,
                    budget_reserved=True,
                )
            worker_context.check_control("tool.worker.before_return")
            return _WorkerCandidate(
                outcome,
                None,
                started_at,
                time.monotonic(),
            )
        except BaseException as error:
            return _WorkerCandidate(None, error, started_at, time.monotonic())

    def _operation_guard(self, call: PlannedToolCall) -> ToolOutcome | None:
        if self.executor.state_store is None:
            return None
        operation = self.executor.state_store.get_tool_operation_for_call(
            run_id=self.context.run_id,
            tool_call_id=call.call_id,
            actor_id=self.context.actor_id,
            tenant_id=self.context.tenant_id,
        )
        if operation is None or operation["status"] not in {"executing", "manual_review"}:
            return None
        return ToolOutcome(
            False,
            error={
                "code": "OPERATION_UNAVAILABLE",
                "message": (
                    "写操作提交状态不确定，已禁止重新执行；"
                    "需要恢复流程复用回执或人工确认"
                ),
            },
            meta={
                "operation_id": operation["operation_id"],
                "operation_status": operation["status"],
                "run_id": self.context.run_id,
                "tool_call_id": call.call_id,
            },
        )

    def _accept_candidate(
        self,
        call: PlannedToolCall,
        segment: ToolBatchSegment,
        candidate: _WorkerCandidate,
        *,
        token: CancellationToken | None,
    ) -> tuple[
        ToolExecutionRecord,
        RunCancelled | CancellationRequested | None,
        BaseException | None,
        str | None,
    ]:
        error = candidate.error
        if error is None and candidate.outcome is not None:
            parent_cancellation = self.context.cancellation_token.cancellation
            if parent_cancellation is not None:
                cancellation = CancellationRequested(
                    parent_cancellation,
                    boundary="tools.batch",
                )
                return (
                    self._accept_outcome(
                        call,
                        segment,
                        self._cancelled_outcome(call, str(cancellation), segment),
                        started=True,
                    ),
                    cancellation,
                    None,
                    None,
                )
            elapsed = candidate.finished_at - candidate.started_at
            if elapsed > call.timeout_seconds or (token is not None and token.cancelled):
                outcome = self._synthetic_outcome(
                    call,
                    segment,
                    code="TOOL_TIMEOUT",
                    message=f"工具 {call.name} 超过 {call.timeout_seconds:g} 秒调用上限",
                    duration_ms=elapsed * 1000,
                )
                return (
                    self._accept_outcome(call, segment, outcome, started=True),
                    None,
                    None,
                    "先前工具超时；后续 barrier 未启动",
                )
            return (
                self._accept_outcome(
                    call,
                    segment,
                    candidate.outcome,
                    started=True,
                ),
                None,
                None,
                None,
            )
        if isinstance(error, RunCancelled):
            return (
                ToolExecutionRecord(
                    call,
                    self._cancelled_outcome(call, str(error), segment),
                    True,
                ),
                error,
                None,
                None,
            )
        if isinstance(error, CancellationRequested):
            parent = self.context.cancellation_token.cancellation
            if parent is not None:
                return (
                    ToolExecutionRecord(
                        call,
                        self._cancelled_outcome(call, str(error), segment),
                        True,
                    ),
                    CancellationRequested(parent, boundary="tools.batch"),
                    None,
                    None,
                )
            outcome = self._synthetic_outcome(
                call,
                segment,
                code="TOOL_TIMEOUT",
                message=f"工具 {call.name} 超过 {call.timeout_seconds:g} 秒调用上限",
                duration_ms=(candidate.finished_at - candidate.started_at) * 1000,
            )
            return (
                self._accept_outcome(call, segment, outcome, started=True),
                None,
                None,
                "先前工具超时；后续 barrier 未启动",
            )
        if isinstance(error, FencingTokenRejected):
            return (
                ToolExecutionRecord(
                    call,
                    self._cancelled_outcome(call, str(error), segment),
                    True,
                ),
                None,
                error,
                None,
            )
        outcome = self._synthetic_outcome(
            call,
            segment,
            code="TOOL_EXCEPTION",
            message=(
                f"{type(error).__name__}: {error}"
                if error is not None
                else "tool worker returned no result"
            ),
            duration_ms=(candidate.finished_at - candidate.started_at) * 1000,
        )
        return self._accept_outcome(call, segment, outcome, started=True), None, None, None

    def _accept_outcome(
        self,
        call: PlannedToolCall,
        segment: ToolBatchSegment,
        outcome: ToolOutcome,
        *,
        started: bool,
    ) -> ToolExecutionRecord:
        outcome.meta.update(self._batch_meta(call, segment))
        try:
            accepted = self.executor.finalize_deferred(
                call.name,
                call.arguments,
                self.context,
                outcome,
                tool_call_id=call.call_id,
            )
        except (RunCancelled, CancellationRequested) as error:
            accepted = self._cancelled_outcome(call, str(error), segment)
        self._emit_completed(call, segment, accepted)
        return ToolExecutionRecord(call, accepted, started)

    def _cancel_queued_after_timeout(
        self,
        calls: Sequence[PlannedToolCall],
        segment: ToolBatchSegment,
        futures: Mapping[int, Future[_WorkerCandidate]],
        started: set[int],
        resolved: dict[int, ToolExecutionRecord],
    ) -> None:
        for call in calls:
            if call.index in resolved or call.index in started:
                continue
            if futures[call.index].cancel():
                resolved[call.index] = self._accept_outcome(
                    call,
                    segment,
                    self._cancelled_outcome(
                        call,
                        "同 segment 的 worker 超时，未再启动排队调用",
                        segment,
                    ),
                    started=False,
                )

    @staticmethod
    def _next_wait_timeout(
        deadlines: Mapping[int, float],
        resolved: Mapping[int, ToolExecutionRecord],
    ) -> float | None:
        active = [
            deadline
            for index, deadline in deadlines.items()
            if index not in resolved
        ]
        if not active:
            return None
        return max(0.0, min(active) - time.monotonic())

    @staticmethod
    def _call_by_index(
        calls: Sequence[PlannedToolCall],
        index: int,
    ) -> PlannedToolCall:
        return next(call for call in calls if call.index == index)

    def _emit_started(
        self,
        context: RunContext,
        call: PlannedToolCall,
        segment: ToolBatchSegment,
    ) -> None:
        context.emit_run_event(
            "tool.started",
            {
                "tool_call_id": call.call_id,
                "tool_name": call.name,
                **self._batch_meta(call, segment),
            },
        )

    def _emit_completed(
        self,
        call: PlannedToolCall,
        segment: ToolBatchSegment,
        outcome: ToolOutcome,
    ) -> None:
        try:
            self.context.emit_run_event(
                "tool.completed",
                {
                    "tool_call_id": call.call_id,
                    "tool_name": call.name,
                    "ok": outcome.ok,
                    "error": outcome.error,
                    **self._batch_meta(call, segment),
                },
            )
        except (RunCancelled, CancellationRequested):
            return

    def _batch_meta(
        self,
        call: PlannedToolCall,
        segment: ToolBatchSegment,
    ) -> dict[str, Any]:
        return {
            "tool_call_index": call.index,
            "tool_batch_segment": segment.segment_id,
            "tool_batch_mode": segment.mode,
            "tool_batch_parallel": bool(
                segment.parallel
                and self.max_workers > 1
                and self.legacy_connection is None
            ),
            "tool_timeout_seconds": call.timeout_seconds,
        }

    def _synthetic_outcome(
        self,
        call: PlannedToolCall,
        segment: ToolBatchSegment,
        *,
        code: str,
        message: str,
        duration_ms: float = 0.0,
    ) -> ToolOutcome:
        return ToolOutcome(
            False,
            error={"code": code, "message": message},
            meta={
                "tool": call.name,
                "duration_ms": round(max(0.0, duration_ms), 2),
                "run_id": self.context.run_id,
                "tool_call_id": call.call_id,
                **self._batch_meta(call, segment),
            },
        )

    def _cancelled_outcome(
        self,
        call: PlannedToolCall,
        reason: str,
        segment: ToolBatchSegment,
    ) -> ToolOutcome:
        return self._synthetic_outcome(
            call,
            segment,
            code="CANCELLED",
            message=reason or "tool call cancelled",
        )


__all__ = [
    "PlannedToolCall",
    "ToolBatchExecution",
    "ToolBatchExecutor",
    "ToolBatchPlanner",
    "ToolBatchSegment",
    "ToolExecutionRecord",
]
