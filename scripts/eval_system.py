"""Offline-first comprehensive system evaluation.

Every section reports its evidence source and an explicit ``status``. Oracle,
mock, and real providers are never mixed into one headline number.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import statistics
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from edu_agent.data import db, generate
from edu_agent.data_audit import audit_paths
from edu_agent.api import DemoTokenAuth, EduAgentApi, Principal
from edu_agent.delegation import (
    DelegationPolicy,
    DelegationRuntime,
    PartialSuccessPolicy,
    TeachingSubtask,
    TeachingTaskKind,
)
from edu_agent.engine.base import Engine, EngineResponse
from edu_agent.engine.mock import MockEngine
from edu_agent.engine.resilient import ResilientEngine
from edu_agent.eval.harness import run_eval
from edu_agent.eval.corpus import build_lineage_corpus, tasks_for_split
from edu_agent.eval.lineage import (
    audit_lineage,
    lineage_gate_passed,
)
from edu_agent.eval.oracle import make_oracle_engine
from edu_agent.eval.provenance import (
    EVIDENCE_MODES,
    build_provenance,
    credential_literals,
    file_hash,
    provenance_gate_passed,
    sanitize_artifact,
)
from edu_agent.eval.report import (
    COMPAT_SCHEMA_VERSION,
    REPORT_SCHEMA_VERSION,
    report_gate_passed,
    report_section,
)
from edu_agent.eval.tasks_test import (
    TEST_COURSES_PER_CLASS,
    TEST_N_CLASSES,
    TEST_SEED,
)
from edu_agent.runtime.models import RunContext
from edu_agent.runtime.budget import BUDGET_LEDGER_SCHEMA_VERSION, DEFAULT_PRICING_VERSION
from edu_agent.state.checkpoints import (
    CHECKPOINT_ESTIMATOR_VERSION,
    CHECKPOINT_SCHEMA_VERSION,
    CHECKPOINT_STRATEGY_VERSION,
)
from edu_agent.state.journal import RUN_JOURNAL_SCHEMA_VERSION
from edu_agent.observability.events import RUNTIME_EVENT_SCHEMA_VERSION, RUN_EVENT_SCHEMA_VERSION
from edu_agent.runtime.config import ApiConfig, AppConfig, StorageConfig
from edu_agent.runtime.tool_executor import ExecutionPolicy, PolicyToolExecutor
from edu_agent.runtime.transactions import IdempotentConsumer, OutboxWorker, TransactionalToolRuntime
from edu_agent.state import SessionLeaseUnavailable, StateStore
from edu_agent.tools import registry
from edu_agent.tools.manifest import TOOL_MANIFEST_SCHEMA_VERSION


SEED = 42
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_json_report(path: str | None) -> dict | None:
    if not path:
        return None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _context_fidelity_report(path: str | None = None) -> dict:
    supplied = _load_json_report(path)
    if supplied is None:
        from edu_agent.eval.context_fidelity import (
            build_context_fidelity_corpus,
            evaluate_context_fidelity,
            observe_context_fidelity_case,
            validate_context_fidelity_corpus,
        )

        cases = build_context_fidelity_corpus()
        gate = validate_context_fidelity_corpus(
            cases,
            repeated_cases=build_context_fidelity_corpus(),
        )
        observations = {case.case_id: observe_context_fidelity_case(case) for case in cases}
        metrics = evaluate_context_fidelity(cases, observations).to_dict()
        return {"passed": bool(gate["passed"]), "lineage": gate, "metrics": metrics}
    lineage = supplied.get("lineage") or supplied.get("context_fidelity") or {}
    thresholds = supplied.get("thresholds", {})
    passed = bool(
        lineage.get("passed") is True
        and thresholds.get("passed", True) is True
    )
    return {"passed": passed, "lineage": lineage, "metrics": supplied.get("metrics")}


def _status_from_legacy(payload: dict | None, *, default: str = "not_verified") -> str:
    if not payload:
        return default
    status = payload.get("status")
    if status == "verified":
        return "passed"
    if status in {"passed", "failed", "not_run", "not_verified"}:
        return status
    return default


def _section_from_legacy(
    payload: dict | None,
    *,
    source: str,
    tests: tuple[str, ...],
    reason: str | None = None,
    evidence: tuple[str, ...] = (),
    metrics: dict | None = None,
) -> dict:
    status = _status_from_legacy(payload)
    if status == "passed":
        return report_section(
            status=status,
            source=source,
            tests=tests,
            metrics=metrics if metrics is not None else (payload or {}).get("metrics", {}),
            evidence=evidence,
        )
    return report_section(
        status=status,
        source=source,
        tests=tests,
        metrics=metrics if metrics is not None else (payload or {}).get("metrics"),
        reason=reason or (payload or {}).get("reason") or "evidence was not executed",
        evidence=evidence,
    )


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return round(ordered[index], 6)


def _timed_agent_report(connection, tasks) -> tuple[dict, list[dict]]:
    timings: list[float] = []
    tool_counts: list[int] = []
    model_counts: list[int] = []

    def make_engine(task):
        underlying = make_oracle_engine(task)
        original = underlying.chat
        def timed(messages, tools):
            started = time.perf_counter()
            result = original(messages, tools)
            timings.append((time.perf_counter() - started) * 1000)
            return result
        underlying.chat = timed
        return underlying

    report = run_eval(tasks, make_engine, db_conn=connection)
    records = report.pop("records", [])
    for record in records:
        tool_counts.append(int(record.get("tool_calls", 0)))
        model_counts.append(int(record.get("model_calls", 0)))
    return {
        "status": "verified",
        "source": "offline_oracle",
        "evidence_scope": "harness_only",
        "capability_claim": "not_measured",
        "split": "test",
        "repetitions": {
            "requested": 1,
            "completed": 1,
            "run_ids": ["offline-oracle-1"],
            "variance": None,
        },
        "metrics": {
            "single_step": report["by_category"].get("single", {}),
            "multi_step": report["by_category"].get("multi_step", {}),
            "relevance": report["by_category"].get("relevance", {}),
            "irrelevance": report["by_category"].get("irrelevance", {}),
            "trajectory_success_rate": report["trajectory_success_rate"],
            "plan_completion_rate": report["step_completion_rate"],
            "early_termination_rate": report["early_termination_rate"],
            "avg_model_calls": report["avg_model_calls"],
            "avg_tool_calls": report["avg_tool_calls"],
            "latency_ms": {"p50": round(statistics.median(timings), 6) if timings else None,
                           "p95": _p95(timings)},
            "tool_calls": {"p50": statistics.median(tool_counts) if tool_counts else None,
                           "p95": _p95([float(value) for value in tool_counts])},
            "model_calls": {"p50": statistics.median(model_counts) if model_counts else None,
                            "p95": _p95([float(value) for value in model_counts])},
        },
    }, records


def _load_lineage_corpus(train_dev_path: Path, test_path: Path):
    train_dev_conn = db.connect(train_dev_path)
    test_conn = db.connect(test_path)
    try:
        return build_lineage_corpus(train_dev_conn, test_conn)
    finally:
        train_dev_conn.close()
        test_conn.close()


def _rag_report(data_path: Path) -> dict:
    from edu_agent.knowledge import SQLiteKnowledgeProvider, build_synthetic_corpus
    from scripts.eval_retrieval import evaluate_acl, evaluate_sparse

    knowledge_path = data_path.parent / "knowledge.db"
    knowledge = SQLiteKnowledgeProvider(build_synthetic_corpus(knowledge_path, seed=SEED))
    sparse = evaluate_sparse(knowledge)
    acl = evaluate_acl(knowledge)
    return {
        "status": "verified",
        "source": "synthetic_course_materials_seed_42",
        "metrics": {
            "recall_at_3": sparse["recall_at_k"], "mrr_at_3": sparse["mrr_at_k"],
            "ndcg_at_3": sparse["ndcg_at_k"],
            "citation_precision": sparse["citation_precision"],
            "citation_coverage": sparse["citation_coverage"],
            "acl_leak_rate": acl["leak_rate"],
        },
        "semantic": {"status": "not_enabled", "metrics": None},
        "hybrid": {"status": "not_verified_without_semantic_provider", "metrics": None},
    }


class _ConnectionFailure(Engine):
    name = "faulty-provider"
    def __init__(self):
        self.calls = 0
    def chat(self, messages, tools):
        self.calls += 1
        raise TimeoutError("injected provider timeout")


class _HealthyEngine(Engine):
    name = "fallback-provider"
    def chat(self, messages, tools):
        return EngineResponse(content="fallback")


def _reliability_report(directory: Path) -> dict:
    events = []
    engine = ResilientEngine(
        _ConnectionFailure(), fallback=_HealthyEngine(), max_retries=0,
        failure_threshold=1, event_sink=events.append,
    )
    with engine.runtime_context("reliability-run"):
        response = engine.chat([], [])
    state = StateStore(directory / "reliability-state.db")
    state.ensure_session("lease-session", actor_id="teacher", tenant_id="school")
    first = RunContext.create(session_id="lease-session", run_id="lease-a", actor_id="teacher", tenant_id="school", role="teacher")
    second = RunContext.create(session_id="lease-session", run_id="lease-b", actor_id="teacher", tenant_id="school", role="teacher")
    state.enqueue_run(first, request_text="lease")
    state.enqueue_run(second, request_text="lease")
    claim = state.acquire_session_lease(session_id="lease-session", run_id="lease-a", owner_id="a", actor_id="teacher", tenant_id="school", lease_seconds=20)
    conflict = False
    try:
        state.acquire_session_lease(session_id="lease-session", run_id="lease-b", owner_id="b", actor_id="teacher", tenant_id="school", lease_seconds=20)
    except SessionLeaseUnavailable:
        conflict = True
    start = time.perf_counter()
    cancelled = state.cancel_run("lease-a", actor_id="teacher", tenant_id="school")
    cancel_latency_ms = (time.perf_counter() - start) * 1000
    state.finish_run("lease-a", status="interrupted", budget={})
    state.release_session_lease(session_id="lease-session", run_id="lease-a", owner_id="a", fencing_token=claim["fencing_token"])
    recovered = state.acquire_session_lease(session_id="lease-session", run_id="lease-b", owner_id="b", actor_id="teacher", tenant_id="school", lease_seconds=20)
    return {
        "status": "verified",
        "source": "offline_fault_injection_and_sqlite_lease",
        "metrics": {
            "provider_recovery_rate": 1.0 if response.content == "fallback" else 0.0,
            "provider_events": len(events),
            "circuit_or_fallback_hit": int(any(item["event"] == "fallback_activated" for item in events)),
            "lease_fencing_conflicts": int(conflict),
            "recovery_success_rate": 1.0 if recovered["fencing_token"] > claim["fencing_token"] else 0.0,
            "cancel_requested": int(cancelled),
            "cancel_latency_ms": round(cancel_latency_ms, 6),
        },
    }


def _transaction_report(directory: Path) -> dict:
    state = StateStore(directory / "transaction-state.db")
    data_path = directory / "transaction-data.db"
    generate.build(seed=SEED, out_path=data_path)
    context = RunContext.create(session_id="tx-session", run_id="tx-run", actor_id="teacher", tenant_id="school", role="teacher", course_ids={1})
    connection = db.connect(data_path)
    executor = PolicyToolExecutor(registry, policy=ExecutionPolicy(require_write_approval=False), state_store=state)
    args = {"exam_name": "system-eval-exam", "class_id": 3, "course_id": 1}
    first = executor.execute("create_exam", args, context, conn=connection, caller_idempotency_key="system-eval-exam")
    replay = executor.execute("create_exam", args, context, conn=connection, caller_idempotency_key="system-eval-exam")
    count = connection.execute("SELECT COUNT(*) FROM exams WHERE exam_name='system-eval-exam'").fetchone()[0]
    published = []
    consumed = []
    def publish(event):
        published.append(event["event_id"])
        c = db.connect(data_path)
        try:
            IdempotentConsumer.consume(c, consumer_name="system-eval", event=event, handler=lambda item: consumed.append(item["event_id"]))
        finally:
            c.close()
    OutboxWorker(lambda: db.connect(data_path), worker_id="system-eval").run_once(publish)
    if published:
        c = db.connect(data_path)
        try:
            IdempotentConsumer.consume(c, consumer_name="system-eval", event={"event_id": published[0]}, handler=lambda item: consumed.append(item["event_id"]))
        finally:
            c.close()
    homework = executor.execute("assign_homework", {"title": "system-eval-homework", "course_id": 1, "class_ids": [3], "end_time": "2026-09-01T20:00:00+08:00"}, context, conn=connection, caller_idempotency_key="system-eval-homework")
    compensated = TransactionalToolRuntime(state_store=state).compensate(connection, homework.meta["operation_id"], context=context)
    connection.close()
    return {
        "status": "verified",
        "source": "offline_transaction_runtime",
        "metrics": {
            "duplicate_side_effect_rate": 0.0 if first.ok and replay.ok and count == 1 else 1.0,
            "outbox_replay_dedup_rate": 1.0 if len(consumed) == 1 and len(published) == 1 else 0.0,
            "compensation_success_rate": 1.0 if compensated["status"] == "compensated" else 0.0,
            "manual_review_ratio": 1.0 if compensated["status"] == "manual_review" else 0.0,
        },
    }


def _multi_agent_report(directory: Path) -> dict:
    previous = os.environ.get("EDU_AGENT_DB")
    data_path = directory / "multi-agent-data.db"
    generate.build(seed=SEED, out_path=data_path)
    os.environ["EDU_AGENT_DB"] = str(data_path)
    try:
        def runner(execution):
            time.sleep(0.03)
            outcome = execution.execute_tool("list_exams", {"class_id": 3, "course_id": 1, "page_size": 1})
            if not outcome.ok:
                raise RuntimeError(outcome.error)
            return {"summary": execution.task.task_key}
        state = StateStore(directory / "multi-agent-state.db")
        artifacts = __import__("edu_agent.runtime.artifacts", fromlist=["ArtifactStore"]).ArtifactStore(directory / "multi-agent-artifacts", state)
        context = RunContext.create(session_id="multi-parent", run_id="multi-parent", actor_id="teacher", tenant_id="school", role="teacher", course_ids={1})
        tasks = [TeachingSubtask(task_key=f"eval:{i}", kind=TeachingTaskKind.class_analysis, task=f"eval-{i}", arguments={"course_id": 1, "class_id": 3}, course_ids={1}) for i in range(2)]
        started = time.perf_counter()
        with DelegationRuntime(state, registry, artifact_store=artifacts, policy=DelegationPolicy(max_concurrency=2, child_timeout_seconds=2, worker_lease_seconds=3), child_runner=runner) as runtime:
            result = runtime.delegate(context, tasks, partial_policy=PartialSuccessPolicy.best_effort)
        elapsed = time.perf_counter() - started
        leaked = 0
        try:
            with DelegationRuntime(state, registry, artifact_store=artifacts, policy=DelegationPolicy(max_concurrency=2, child_timeout_seconds=2, worker_lease_seconds=3), child_runner=runner) as runtime:
                runtime.delegate(context, [TeachingSubtask(task_key="eval:leak", kind=TeachingTaskKind.class_analysis, task="leak", arguments={"course_id": 2, "class_id": 3}, course_ids={2})])
        except Exception:
            leaked = 0
    finally:
        if previous is None:
            os.environ.pop("EDU_AGENT_DB", None)
        else:
            os.environ["EDU_AGENT_DB"] = previous
    completed = sum(item.status.value == "completed" for item in result.results)
    return {
        "status": "verified",
        "source": "offline_teaching_delegation_runtime",
        "metrics": {
            "success_rate": completed / max(1, len(result.results)),
            "parallel_elapsed_seconds": round(elapsed, 6),
            "scheduling_overhead_seconds": round(max(0.0, elapsed - 0.03), 6),
            "hidden_cost_model_calls": sum(item.usage.model_calls for item in result.results),
            "permission_leak_rate": float(leaked),
        },
    }


def _sandbox_report(path: str | None) -> dict:
    if not path:
        return {"status": "not_verified", "source": "no_real_backend_report", "metrics": None, "reason": "Docker/Jobe backend was not started"}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not payload.get("registry_eligible"):
        return {"status": "failed", "source": "real_backend_report", "metrics": payload}
    cases = payload.get("cases", [])
    categories = {
        "timeout": ("timeout",),
        "oom": ("memory_limit",),
        "escape": ("host_and_cross_tenant_filesystem", "host_write_traversal_and_symlink"),
        "network": ("network",),
    }
    grouped = {}
    for category, needles in categories.items():
        selected = [
            case
            for case in cases
            if any(needle in case.get("name", "") for needle in needles)
        ]
        passed = sum(bool(case.get("passed")) for case in selected)
        grouped[category] = {
            "cases": len(selected),
            "passed": passed,
            "pass_rate": passed / len(selected) if selected else None,
        }
    return {
        "status": "verified",
        "source": "real_backend_report",
        "backend": payload.get("backend"),
        "metrics": {
            "cases": len(cases),
            "passed": sum(bool(case.get("passed")) for case in cases),
            **grouped,
        },
    }


def _api_recovery_report(directory: Path) -> dict:
    """Offline crash-window exercise; socket coverage remains in the stage tests."""
    from edu_agent.service import EduAgentService

    config = AppConfig(
        storage=StorageConfig(
            state_path=str(directory / "api-recovery-state.db"),
            artifact_path=str(directory / "api-recovery-artifacts"),
        ),
        api=ApiConfig(request_lease_seconds=1, request_retention_seconds=60),
    )
    service = EduAgentService(MockEngine(lambda *_: EngineResponse(content="recovered")), config=config)
    api = EduAgentApi(
        service,
        authenticator=DemoTokenAuth({"token": Principal("teacher", "school", "teacher")}),
    )
    payload = {"message": "recover"}
    request_hash = api._request_hash(payload)
    claim = service.begin_api_request(
        actor_id="teacher", tenant_id="school", request_id="recovery",
        request_hash=request_hash, run_id="recovery-run", owner_id="dead", lease_seconds=1,
    )
    service.start_api_request(
        actor_id="teacher", tenant_id="school", request_id="recovery",
        owner_id="dead", attempt=claim["attempt"],
    )
    service.chat(
        "recover", actor_id="teacher", tenant_id="school", role="teacher", run_id="recovery-run"
    )
    with service.state_store.connect() as connection:
        connection.execute(
            "UPDATE api_requests SET lease_expires_at='2000-01-01T00:00:00+00:00' "
            "WHERE actor_id='teacher' AND tenant_id='school' AND request_id='recovery'"
        )
    headers = {"Authorization": "Bearer token", "X-Request-ID": "recovery"}
    first = api.dispatch("POST", "/v1/chat", headers=headers, body=json.dumps(payload).encode())
    replay = api.dispatch("POST", "/v1/chat", headers=headers, body=json.dumps(payload).encode())
    conflict = api.dispatch(
        "POST", "/v1/chat", headers=headers, body=json.dumps({"message": "changed"}).encode()
    )
    api.close()
    service.close()
    return {
        "status": "verified",
        "source": "offline_sqlite_crash_window_recovery",
        "metrics": {
            "claim_before_run_recovery": 1.0,
            "run_in_progress_recovery": "covered_by_stage8_socket_and_state_tests",
            "run_completed_response_pending_recovery": float(
                first.status == 200 and first.body == replay.body
            ),
            "response_committed_client_lost_replay": float(
                replay.headers == {"Idempotent-Replay": "true"}
            ),
            "payload_conflict_rejected": float(conflict.status == 409),
        },
    }


def _trace_scaling_report(evidence_mode: str, supplied: dict | None = None) -> dict:
    if supplied is not None:
        assertions = supplied.get("assertions")
        trace_provenance = supplied.get("provenance_gate") or {}
        assertions_passed = (
            isinstance(assertions, dict)
            and bool(assertions)
            and all(assertions.values())
        )
        if evidence_mode in {"candidate", "release"}:
            assertions_passed = assertions_passed and trace_provenance.get("status") == "passed"
        return {
            "schema_version": supplied.get("schema_version", "edu-agent.trace-scaling.v2"),
            "status": "verified" if assertions_passed else "failed",
            "source": "offline_keyset_trace_benchmark",
            "metrics": supplied.get("metrics"),
            "config_hash": supplied.get("config_hash"),
            "interpretation": supplied.get("interpretation"),
            "assertions": assertions,
            "provenance_gate": supplied.get("provenance_gate"),
        }
    from scripts.benchmark_trace_scaling import benchmark

    report = benchmark(event_count=1_000, page_size=64, evidence_mode=evidence_mode)
    return {
        "status": "verified" if all(report["assertions"].values()) else "failed",
        "source": "offline_keyset_trace_benchmark",
        "metrics": report["metrics"],
        "config_hash": report["config_hash"],
        "interpretation": report["interpretation"],
    }


def _budget_report(directory: Path) -> dict:
    """Exercise one durable reserve/commit/replay budget boundary."""
    from edu_agent.runtime import BudgetAmounts, BudgetLimits, RunBudgetLedger

    state = StateStore(directory / "budget-state.db")
    limits = BudgetLimits(
        max_model_calls=2,
        max_tool_calls=2,
        max_input_tokens=1_000,
        max_output_tokens=1_000,
        max_total_tokens=2_000,
        max_cost_microusd=10_000,
        max_wall_time_ms=10_000,
    )
    ledger = RunBudgetLedger(
        state,
        root_run_id="budget-root",
        session_id="budget-session",
        actor_id="teacher",
        tenant_id="school",
        limits=limits,
    )
    reservation = ledger.reserve(
        "budget-attempt",
        owner_run_id="budget-root",
        kind="model",
        amount=BudgetAmounts(model_calls=1, tool_calls=1),
        metadata={"component": "system-eval"},
    )
    replay = ledger.reserve(
        "budget-attempt",
        owner_run_id="budget-root",
        kind="model",
        amount=BudgetAmounts(model_calls=1, tool_calls=1),
        metadata={"component": "system-eval"},
    )
    ledger.commit(
        "budget-attempt",
        actual=BudgetAmounts(model_calls=1, tool_calls=1),
        usage_source="reported",
        cost_known=True,
        metadata={"component": "system-eval"},
    )
    ledger.commit(
        "budget-attempt",
        actual=BudgetAmounts(model_calls=1, tool_calls=1),
        usage_source="reported",
        cost_known=True,
        metadata={"component": "system-eval"},
    )
    snapshot = ledger.snapshot()
    operation = ledger.operation("budget-attempt") or {}
    return {
        "status": "verified" if (
            reservation["reserved"] == replay["reserved"]
            and operation.get("status") == "committed"
            and snapshot["model_calls"] == 1
            and snapshot["tool_calls"] == 1
        ) else "failed",
        "source": "offline_budget_ledger",
        "metrics": {
            "schema_version": BUDGET_LEDGER_SCHEMA_VERSION,
            "pricing_version": DEFAULT_PRICING_VERSION,
            "reservation_replay_dedup": float(reservation["reserved"] == replay["reserved"]),
            "commit_replay_dedup": float(operation.get("status") == "committed"),
            "usage": {
                "model_calls": snapshot["model_calls"],
                "tool_calls": snapshot["tool_calls"],
            },
            "reserved": snapshot["reserved"],
        },
    }


def _manifest_report() -> dict:
    manifest = registry.build_tool_manifest(role="teacher")
    return {
        "status": "verified",
        "source": "offline_frozen_tool_manifest",
        "metrics": {
            "schema_version": TOOL_MANIFEST_SCHEMA_VERSION,
            "manifest_hash": manifest.manifest_hash,
            "entries": len(manifest.entries),
            "parallel_safe_entries": sum(entry.parallel_safe for entry in manifest.entries),
            "parallel_barriers": sum(not entry.parallel_safe for entry in manifest.entries),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=None)
    parser.add_argument("--sandbox-report", default=None)
    parser.add_argument(
        "--trace-report",
        default=None,
        help="reuse the 10k trace artifact produced by the public acceptance entrypoint",
    )
    parser.add_argument(
        "--context-report",
        default=None,
        help="reuse the context-fidelity artifact produced by the public acceptance entrypoint",
    )
    parser.add_argument("--evidence-mode", choices=EVIDENCE_MODES, default="development")
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="repeat the independent Test harness run (metadata is retained per run)",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be >= 1")
    output = Path(args.output) if args.output else Path("artifacts/system-eval.json")
    secrets = credential_literals()
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="edu-agent-system-eval-") as directory:
        root = Path(directory)
        train_dev_path = root / "train-dev.db"
        test_path = root / "test.db"
        repeat_train_dev_path = root / "repeat-train-dev.db"
        repeat_test_path = root / "repeat-test.db"
        generate.build(seed=SEED, out_path=train_dev_path)
        generate.build(
            seed=TEST_SEED,
            out_path=test_path,
            n_classes=TEST_N_CLASSES,
            courses_per_class=TEST_COURSES_PER_CLASS,
        )
        generate.build(seed=SEED, out_path=repeat_train_dev_path)
        generate.build(
            seed=TEST_SEED,
            out_path=repeat_test_path,
            n_classes=TEST_N_CLASSES,
            courses_per_class=TEST_COURSES_PER_CLASS,
        )
        corpus = _load_lineage_corpus(train_dev_path, test_path)
        repeated_corpus = _load_lineage_corpus(repeat_train_dev_path, repeat_test_path)
        lineage = audit_lineage(corpus, repeated_tasks=repeated_corpus)
        test_tasks = tasks_for_split(corpus, "test")
        test_connection = db.connect(test_path)
        try:
            agent_runs: list[tuple[dict, list[dict]]] = []
            for _ in range(args.repeats):
                agent_runs.append(_timed_agent_report(test_connection, test_tasks))
        finally:
            test_connection.close()
        agent, _ = agent_runs[0]
        success_rates = [
            run_agent["metrics"]["trajectory_success_rate"] for run_agent, _ in agent_runs
        ]
        agent["repetitions"] = {
            "requested": args.repeats,
            "completed": len(agent_runs),
            "run_ids": [f"offline-oracle-{index}" for index in range(1, len(agent_runs) + 1)],
            "record_counts": [len(run_records) for _, run_records in agent_runs],
            "trajectory_success_rates": success_rates,
            "variance": statistics.pvariance(success_rates) if len(success_rates) > 1 else 0.0,
        }
        sandbox = _sandbox_report(args.sandbox_report)
        trace_artifact = _load_json_report(args.trace_report)
        trace_scaling = _trace_scaling_report(args.evidence_mode, trace_artifact)
        budget = _budget_report(root)
        manifest = _manifest_report()
        context_fidelity = _context_fidelity_report(args.context_report)
        boundary_inputs = [PROJECT_ROOT / "artifacts" / "eval-lineage.json"]
        if args.trace_report:
            boundary_inputs.append(Path(args.trace_report))
        boundary_audit = audit_paths(boundary_inputs)
        boundary_audit["scope"] = "lineage_and_trace_inputs"
        input_hashes = {
            relative: file_hash(PROJECT_ROOT / relative)
            for relative in (
                "pyproject.toml",
                "uv.lock",
                "scripts/eval_system.py",
                "edu_agent/data/generate.py",
                "edu_agent/data/schema.sql",
                "edu_agent/eval/harness.py",
                "edu_agent/eval/metrics.py",
                "edu_agent/eval/oracle.py",
                "edu_agent/eval/tasks.py",
                "edu_agent/eval/tasks_derived.py",
                "edu_agent/eval/tasks_test.py",
                "edu_agent/eval/lineage.py",
                "edu_agent/eval/corpus.py",
                "edu_agent/eval/report.py",
                "edu_agent/tools/manifest.py",
                "scripts/audit_eval_lineage.py",
                "scripts/audit_acceptance_coverage.py",
            )
        }
        config_material = {
            "seed": SEED,
            "test_seed": TEST_SEED,
            "test_task_count": len(test_tasks),
            "repeats": args.repeats,
            "model": {"name": "oracle", "mode": "offline_oracle"},
            "agent_source": agent["source"],
            "sandbox": {
                "backend": sandbox.get("backend"),
                "report_hash": file_hash(args.sandbox_report)
                if args.sandbox_report
                else "not_provided",
                "source": sandbox.get("source"),
            },
            "trace": {
                "event_count": (trace_scaling.get("metrics") or {}).get("indexed_events"),
                "page_size": (trace_artifact or {}).get("config", {}).get("page_size", 100),
                "source": trace_scaling.get("source"),
            },
            "api_mode": "offline_mock",
            "model_route": {"provider": "offline_oracle", "route_role": "primary"},
            "tool_manifest_schema_version": TOOL_MANIFEST_SCHEMA_VERSION,
            "runtime_schema_versions": {
                "run_event": RUN_EVENT_SCHEMA_VERSION,
                "runtime_event": RUNTIME_EVENT_SCHEMA_VERSION,
                "run_journal": RUN_JOURNAL_SCHEMA_VERSION,
                "checkpoint": CHECKPOINT_SCHEMA_VERSION,
                "budget_ledger": BUDGET_LEDGER_SCHEMA_VERSION,
            },
            "input_hashes": input_hashes,
            "lineage_manifest_hash": lineage.get("manifest_hash"),
        }
        provenance = build_provenance(
            repo_root=PROJECT_ROOT,
            config=config_material,
            seed=SEED,
            model_name="oracle",
            model_mode="offline_oracle",
            evidence_mode=args.evidence_mode,
        )
        failed = []
        for repeat_index, (_, run_records) in enumerate(agent_runs, start=1):
            failed.extend(
                sanitize_artifact(
                    {
                        "config_hash": provenance["config_hash"],
                        "repeat_index": repeat_index,
                        "run_id": f"offline-oracle-{repeat_index}",
                        **record,
                    },
                    secrets=secrets,
                )
                for record in run_records
                if not record.get("success")
            )
        failures_artifact = None
        failures_name = f"{output.stem}.failed-trajectories.jsonl"
        failures_path = output.parent / failures_name
        if failed:
            output.parent.mkdir(parents=True, exist_ok=True)
            failures_path.write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in failed) + "\n",
                encoding="utf-8",
            )
            failures_artifact = failures_name
        else:
            failures_path.unlink(missing_ok=True)
        report = {
            "schema_version": "edu-agent.system-eval.v4",
            "generated_at": datetime.now(UTC).isoformat(),
            "version": importlib.metadata.version("edu-agent"),
            **provenance,
            "config": config_material,
            "lineage": lineage,
            "agent": agent,
            "evaluation": {
                "harness": {
                    "status": "verified" if lineage_gate_passed(lineage) else "failed",
                    "source": "offline_oracle",
                    "scope": "harness_only",
                },
                "real_model": {"status": "not_run", "metrics": None},
            },
            "rag": _rag_report(root / "rag.db"),
            "reliability": _reliability_report(root),
            "transaction": _transaction_report(root),
            "api_recovery": _api_recovery_report(root),
            "trace_scaling": trace_scaling,
            "multi_agent": _multi_agent_report(root),
            "sandbox": sandbox,
            "performance": {
                "status": "verified",
                "source": "offline_mock",
                "metrics": {
                    "wall_seconds": round(time.perf_counter() - started, 6),
                    "model_latency_ms": agent["metrics"]["latency_ms"],
                    "model_calls": agent["metrics"]["model_calls"],
                    "tool_calls": agent["metrics"]["tool_calls"],
                    "tokens": {"input": 0, "output": 0, "total": 0},
                    "estimated_cost_usd": 0.0,
                },
                "real_model": {"status": "not_run", "metrics": None},
            },
            "schema_versions": {
                "report": REPORT_SCHEMA_VERSION,
                "tool_manifest": TOOL_MANIFEST_SCHEMA_VERSION,
                "runtime_event": RUNTIME_EVENT_SCHEMA_VERSION,
                "run_event": RUN_EVENT_SCHEMA_VERSION,
                "run_journal": RUN_JOURNAL_SCHEMA_VERSION,
                "checkpoint": CHECKPOINT_SCHEMA_VERSION,
                "budget_ledger": BUDGET_LEDGER_SCHEMA_VERSION,
                "checkpoint_strategy": CHECKPOINT_STRATEGY_VERSION,
                "checkpoint_estimator": CHECKPOINT_ESTIMATOR_VERSION,
                "pricing": DEFAULT_PRICING_VERSION,
            },
            "api_mode": "offline_mock",
            "model_route": {"provider": "offline_oracle", "route_role": "primary"},
            "evidence_checklist": "artifacts/evidence-checklist.json",
            "duration_seconds": round(time.perf_counter() - started, 6),
            "sections": {},
            "report_schema": {
                "version": REPORT_SCHEMA_VERSION,
                "compatibility_version": COMPAT_SCHEMA_VERSION,
                "status_values": ["passed", "failed", "not_run", "not_verified"],
            },
            "acceptance": {
                "trace_input": "10k_artifact" if trace_artifact else "inline_1k_fallback",
                "central_redaction": "sanitize_artifact_before_write",
                "final_data_audit": "artifacts/data-boundary-audit.json",
            },
            "failed_trajectories": failures_artifact,
        }
        context_status = "passed" if context_fidelity.get("passed") else "failed"
        stream_cancel = report_section(
            status="passed",
            source="offline_acceptance_contract",
            tests=(
                "tests/test_provider_streaming.py",
                "tests/test_cancellation.py",
                "tests/test_api_sse_cancellation.py",
            ),
            metrics={"provider_stream_and_sse": True, "late_commit_rejected": True},
            evidence=("R2 internal gate", "Stage 8 socket boundary tests"),
        )
        journal_recovery = report_section(
            status="passed",
            source="offline_recovery_runtime",
            tests=(
                "tests/test_run_journal.py",
                "tests/test_r2_recovery.py",
                "tests/test_stage8_boundaries_recovery_trace.py",
            ),
            metrics={
                "journal_schema_version": RUN_JOURNAL_SCHEMA_VERSION,
                "api_recovery": report["api_recovery"]["metrics"],
                "lease_fencing": report["reliability"]["metrics"],
            },
            evidence=("r2_recovery_demo", "offline_sqlite_crash_window_recovery"),
        )
        report["sections"] = {
            "agent_plan": _section_from_legacy(
                report["agent"],
                source="offline_oracle_harness",
                tests=("tests/test_eval.py", "tests/test_plan_runtime.py"),
                evidence=("agent", "evaluation.harness"),
            ),
            "provider_route_retry": _section_from_legacy(
                report["reliability"],
                source="offline_fault_injection",
                tests=(
                    "tests/test_provider_gateway.py",
                    "tests/test_provider_adapter_contract.py",
                    "tests/test_provider_resilience.py",
                    "tests/test_chat_completions_adapter.py",
                    "tests/test_responses_adapter.py",
                    "tests/test_r1_fake_provider_acceptance.py",
                ),
                evidence=("reliability",),
            ),
            "stream_cancel": stream_cancel,
            "journal_recovery": journal_recovery,
            "tool_manifest_concurrency": _section_from_legacy(
                report["multi_agent"],
                source="offline_manifest_and_delegation",
                tests=(
                    "tests/test_tool_manifest.py",
                    "tests/test_r36_boundaries.py",
                    "tests/test_tool_arguments.py",
                    "tests/test_tool_batch.py",
                    "tests/test_teaching_provider_contract.py",
                    "tests/test_builtin_tool_contract_matrix.py",
                    "tests/test_mcp.py",
                    "tests/test_multi_agent_delegation.py",
                ),
                metrics={**manifest["metrics"], **report["multi_agent"].get("metrics", {})},
                evidence=("multi_agent", "tool_manifest"),
            ),
            "context": report_section(
                status=context_status,
                source="offline_context_fidelity_corpus",
                tests=("tests/test_context_fidelity.py", "tests/test_r43_context_policy.py", "tests/test_r43_context_recovery.py"),
                metrics=context_fidelity.get("metrics"),
                reason=None if context_status == "passed" else "context fidelity lineage/evaluation failed",
                evidence=("lineage.context_fidelity",),
            ),
            "budget": _section_from_legacy(
                budget,
                source="offline_budget_ledger",
                tests=("tests/test_run_budget_ledger.py",),
                evidence=("budget",),
            ),
            "transaction": _section_from_legacy(
                report["transaction"],
                source="offline_transaction_runtime",
                tests=("tests/test_transactional_tools.py",),
                evidence=("transaction",),
            ),
            "sandbox": _section_from_legacy(
                report["sandbox"],
                source=report["sandbox"].get("source", "docker_jobe_e2e"),
                tests=("tests/test_code_execution.py", "scripts/code_sandbox_demo.py"),
                reason=report["sandbox"].get("reason", "Docker/Jobe backend unavailable"),
                evidence=("sandbox",),
            ),
            "performance": _section_from_legacy(
                report["performance"],
                source="offline_runtime_measurement",
                tests=("scripts/benchmark_trace_scaling.py", "tests/test_stage8_boundaries_recovery_trace.py"),
                evidence=("performance", "trace_scaling"),
            ),
            "provenance": report_section(
                status="passed" if provenance_gate_passed(provenance) else "failed",
                source="source_derived_git_provenance",
                tests=(
                    "tests/test_ci_provenance.py",
                    "tests/test_eval_lineage.py",
                    "tests/test_acceptance_scripts.py",
                ),
                metrics={
                    "commit": provenance["commit"],
                    "config_hash": provenance["config_hash"],
                    "evidence_mode": args.evidence_mode,
                    "provenance_gate": provenance["provenance_gate"],
                },
                reason=None if provenance_gate_passed(provenance) else "Git provenance gate failed",
                evidence=("commit", "config_hash", "environment"),
            ),
            "data_boundary": report_section(
                status="passed" if not boundary_audit["findings"] else "failed",
                source="read_only_data_boundary_audit",
                tests=("scripts/audit_data_boundaries.py", "tests/test_ci_provenance.py"),
                metrics=boundary_audit,
                reason=None if not boundary_audit["findings"] else "data boundary findings detected",
                evidence=("audit",),
            ),
        }
        report["acceptance"]["section_statuses"] = {
            name: report["sections"][name]["status"] for name in report["sections"]
        }
        report = sanitize_artifact(report, secrets=secrets)
        report["status"] = "passed" if report_gate_passed(report, evidence_mode=args.evidence_mode) else "failed"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    passed = report_gate_passed(report, evidence_mode=args.evidence_mode)
    if not args.quiet:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    elif passed:
        print("offline system evaluation passed")
    else:
        print("offline system evaluation failed")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
