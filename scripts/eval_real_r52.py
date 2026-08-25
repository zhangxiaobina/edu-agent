"""R5.2 fixed-route live evaluation runner.

This runner is intentionally narrow: one DashScope OpenAI-compatible route,
the independent Test split, and repeat-aware, redacted evidence.  It keeps
provider text out of the publishable artifact while retaining numeric usage,
timing, cost status, and failure classification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from statistics import fmean, pvariance
from urllib.parse import urlsplit

from edu_agent.data import db, generate
from edu_agent.engine import (
    ApiMode,
    ProviderSpec,
    capability_gaps,
    estimate_request_tokens,
    get_engine,
    infer_request_requirements,
)
from edu_agent.engine.base import Engine, EngineResponse
from edu_agent.engine.resilient import FailureKind, classify_failure
from edu_agent.engine.streaming import (
    ProviderStreamAggregator,
    ProviderStreamEventType,
)
from edu_agent.eval import (
    audit_lineage,
    build_lineage_corpus,
    build_lineage_manifest,
    format_report,
    lineage_gate_passed,
    run_eval,
    tasks_for_split,
)
from edu_agent.eval.provenance import (
    EVIDENCE_MODES,
    build_provenance,
    credential_literals,
    file_hash,
    provenance_gate_passed,
    sanitize_artifact,
)
from edu_agent.eval.tasks_test import (
    TEST_COURSES_PER_CLASS,
    TEST_N_CLASSES,
    TEST_SEED,
)
from edu_agent.runtime.config import ModelConfig


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEV_SEED = 42
ROUTE_PROVIDER = "dashscope"
ROUTE_API_MODE = ApiMode.CHAT_COMPLETIONS
ROUTE_MODEL = "qwen-plus"
ROUTE_DEPLOYMENT = "dashscope-compatible"
ROUTE_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1"
CREDENTIAL_ENV = "EDU_AGENT_API_KEY"
TEMPERATURE = 0.0
SEED = None
MAX_OUTPUT_TOKENS = 8_192
CONCURRENCY = 4
TIMEOUT_SECONDS = 1_800.0
MAX_RETRIES = 2
MAX_COST_USD = Decimal("30")
# The repository example is retained as an explicit local estimate.  Provider
# billing is still reported as unknown until an independently verified quote is
# available; the guard uses this estimate conservatively for the user cap.
PRICING_VERSION = "example-prices@2026-08-24.v1"
INPUT_PRICE_PER_MILLION = Decimal("0.4")
OUTPUT_PRICE_PER_MILLION = Decimal("1.2")


class R52PreflightError(RuntimeError):
    """A required preflight check failed before any model request."""


class R52SpendGuard:
    """Conservative in-process spend guard for sequential Test requests."""

    def __init__(self, *, max_cost_usd: Decimal):
        self.max_cost_usd = max_cost_usd
        self.reserved_usd = Decimal("0")
        self.estimated_usd = Decimal("0")
        self.unknown_cost_usd = Decimal("0")
        self.potential_cost_usd = Decimal("0")
        self.calls = 0

    @staticmethod
    def quote(input_tokens: int, output_tokens: int) -> Decimal:
        value = (
            Decimal(input_tokens) * INPUT_PRICE_PER_MILLION
            + Decimal(output_tokens) * OUTPUT_PRICE_PER_MILLION
        ) / Decimal(1_000_000)
        return value.quantize(Decimal("0.000001"), rounding=ROUND_CEILING)

    def reserve(self, *, input_tokens: int, max_output_tokens: int) -> Decimal:
        # A request can consume the primary plus MAX_RETRIES retry attempts.
        estimate = self.quote(
            input_tokens * (MAX_RETRIES + 1),
            max_output_tokens * (MAX_RETRIES + 1),
        )
        if self.reserved_usd + estimate > self.max_cost_usd:
            raise R52PreflightError("executable spend cap would be exceeded")
        self.reserved_usd += estimate
        self.potential_cost_usd += estimate
        self.calls += 1
        return estimate

    def settle(
        self,
        *,
        reserved: Decimal,
        input_tokens: int | None,
        output_tokens: int | None,
        request_sent: bool,
    ) -> tuple[Decimal | None, str]:
        self.reserved_usd -= reserved
        if not request_sent:
            return None, "not_sent_environment"
        if input_tokens is None or output_tokens is None:
            # Keep the conservative upper bound charged when provider usage is
            # absent so an unknown response cannot bypass the cap.
            self.unknown_cost_usd += reserved
            self.reserved_usd += reserved
            return None, "unknown_usage_or_price"
        estimated = self.quote(input_tokens, output_tokens)
        self.estimated_usd += estimated
        if self.reserved_usd + estimated > self.max_cost_usd:
            raise R52PreflightError("provider usage would exceed executable spend cap")
        self.reserved_usd += estimated
        return None, "estimated_unverified_price"

    def snapshot(self) -> dict:
        return {
            "max_cost_usd": float(self.max_cost_usd),
            "committed_or_unknown_cost_usd": float(self.reserved_usd),
            "potential_cost_ceiling_usd": float(self.potential_cost_usd),
            "estimated_cost_usd": float(self.estimated_usd),
            "unknown_cost_usd": float(self.unknown_cost_usd),
            "provider_billing_cost_usd": None,
            "cost_status": (
                "unknown_provider_usage"
                if self.unknown_cost_usd
                else "estimated_unverified_price"
            ),
            "guarded_model_requests": self.calls,
        }


def _endpoint_hash() -> str:
    return hashlib.sha256(ROUTE_ENDPOINT.encode("utf-8")).hexdigest()


def _safe_int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _numeric_usage(usage: object) -> dict[str, int]:
    if not isinstance(usage, dict):
        return {}
    names = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "runtime_attempts",
    )
    return {name: _safe_int(usage.get(name)) for name in names if _safe_int(usage.get(name)) is not None}


def _usage_token(usage: dict, primary: str, secondary: str) -> int | None:
    value = _safe_int(usage.get(primary))
    return value if value is not None else _safe_int(usage.get(secondary))


def _failure_class(error: BaseException) -> tuple[str, str]:
    if isinstance(error, ImportError):
        detail = str(error).lower()
        if "socksio" in detail or "proxy" in detail or "dependency" in detail:
            return "environment_not_verified", "missing_transport_dependency"
        return "environment_not_verified", "missing_runtime_dependency"
    if isinstance(error, R52PreflightError):
        return "harness_bug", "runner_preflight_error"
    try:
        decision = classify_failure(error)
    except Exception:
        return "harness_bug", type(error).__name__
    if decision.kind in {
        FailureKind.CONNECTION,
        FailureKind.TIMEOUT,
        FailureKind.RATE_LIMIT,
        FailureKind.SERVER,
        FailureKind.AUTHENTICATION,
        FailureKind.PERMISSION,
        FailureKind.INVALID_REQUEST,
        FailureKind.CONTEXT_OVERFLOW,
        FailureKind.OUTPUT_CAP,
        FailureKind.CIRCUIT_OPEN,
    }:
        return "provider_failure", decision.kind.value
    return "provider_failure", FailureKind.UNKNOWN.value


class MeasuredEngine(Engine):
    """Wrap one frozen engine and collect timing/usage without storing text."""

    name = "r52-measured"

    def __init__(self, underlying: Engine, *, task_id: str, repeat_index: int, guard: R52SpendGuard):
        self.underlying = underlying
        self.task_id = task_id
        self.repeat_index = repeat_index
        self.guard = guard
        self.observations: list[dict] = []

    @property
    def model(self):
        return getattr(self.underlying, "model", ROUTE_MODEL)

    @property
    def base_url(self):
        return getattr(self.underlying, "base_url", ROUTE_ENDPOINT)

    @property
    def temperature(self):
        return getattr(self.underlying, "temperature", TEMPERATURE)

    def begin_turn_routes(self):
        return self.underlying.begin_turn_routes()

    def effective_capabilities(self):
        resolver = getattr(self.underlying, "effective_capabilities", None)
        if callable(resolver):
            return resolver()
        routes = self.begin_turn_routes()
        route_resolver = getattr(self.underlying, "capabilities_for_route", None)
        return (
            route_resolver(routes[0])
            if callable(route_resolver) and routes
            else routes[0].capabilities
        )

    def capabilities_for_route(self, route):
        resolver = getattr(self.underlying, "capabilities_for_route", None)
        return resolver(route) if callable(resolver) else route.capabilities

    def _record(self, observation: dict) -> None:
        self.observations.append(observation)

    def chat(self, messages: list[dict], tools: list[dict], *, cancellation_token=None,
             max_output_tokens: int | None = None, run_budget=None) -> EngineResponse:
        requested_output = MAX_OUTPUT_TOKENS
        if max_output_tokens is not None and max_output_tokens != requested_output:
            raise R52PreflightError("live runner attempted to change frozen max output")
        input_estimate = estimate_request_tokens(messages, tools, model=self.model)
        reserved = self.guard.reserve(
            input_tokens=input_estimate,
            max_output_tokens=requested_output,
        )
        started = time.perf_counter()
        first_token = None
        usage: dict = {}
        attempts: set[int] = set()
        provider_error: tuple[str, str] | None = None
        iterator = None
        aggregator = ProviderStreamAggregator()
        response = None
        try:
            stream = getattr(self.underlying, "stream_chat", None)
            if not callable(stream):
                raise R52PreflightError("frozen live engine lacks provider streaming")
            iterator = iter(
                stream(
                    messages,
                    tools,
                    cancellation_token=cancellation_token,
                    max_output_tokens=requested_output,
                    run_budget=run_budget,
                )
            )
            for event in iterator:
                attempts.add(event.attempt)
                if first_token is None and event.event_type in {
                    ProviderStreamEventType.TEXT_DELTA,
                    ProviderStreamEventType.TOOL_CALL_ID_DELTA,
                    ProviderStreamEventType.TOOL_CALL_NAME_DELTA,
                    ProviderStreamEventType.TOOL_CALL_ARGUMENTS_DELTA,
                }:
                    first_token = (time.perf_counter() - started) * 1000
                if event.event_type is ProviderStreamEventType.USAGE:
                    usage.update(event.usage)
                if event.event_type is ProviderStreamEventType.ERROR and event.error is not None:
                    provider_error = _failure_class(event.error)
                aggregator.feed(event)
            response = aggregator.result()
            usage.update(response.usage)
            input_tokens = _usage_token(usage, "prompt_tokens", "input_tokens")
            output_tokens = _usage_token(usage, "completion_tokens", "output_tokens")
            _, cost_status = self.guard.settle(
                reserved=reserved,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                request_sent=True,
            )
            self._record({
                "task_id": self.task_id,
                "repeat_index": self.repeat_index,
                "status": "completed",
                "failure_class": None,
                "failure_kind": None,
                "first_output_delta_ms": round(first_token, 6) if first_token is not None else None,
                "total_latency_ms": round((time.perf_counter() - started) * 1000, 6),
                "attempt_count": max(attempts, default=0),
                "usage": _numeric_usage(usage),
                "request_estimate": input_estimate,
                "max_output_tokens": requested_output,
                "estimated_cost_usd": float(self.guard.quote(input_tokens, output_tokens)) if input_tokens is not None and output_tokens is not None else None,
                "cost_usd": None,
                "cost_status": cost_status,
                "recovery_safety": {"status": "not_exercised", "late_work_rejected": None},
            })
            return response
        except BaseException as error:
            if provider_error is None:
                provider_error = _failure_class(error)
            usage.update(getattr(error, "usage", {}) if isinstance(getattr(error, "usage", {}), dict) else {})
            input_tokens = _usage_token(usage, "prompt_tokens", "input_tokens")
            output_tokens = _usage_token(usage, "completion_tokens", "output_tokens")
            failure_class, failure_kind = provider_error
            _, failure_cost_status = self.guard.settle(
                reserved=reserved,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                request_sent=failure_class != "environment_not_verified",
            )
            self._record({
                "task_id": self.task_id,
                "repeat_index": self.repeat_index,
                "status": "failed",
                "failure_class": failure_class,
                "failure_kind": failure_kind,
                "first_output_delta_ms": round(first_token, 6) if first_token is not None else None,
                "total_latency_ms": round((time.perf_counter() - started) * 1000, 6),
                "attempt_count": max(attempts, default=0),
                "usage": _numeric_usage(usage),
                "request_estimate": input_estimate,
                "max_output_tokens": requested_output,
                "estimated_cost_usd": None,
                "cost_usd": None,
                "cost_status": failure_cost_status,
                "recovery_safety": {"status": "not_exercised", "late_work_rejected": None},
                "error_type": type(error).__name__,
            })
            raise
        finally:
            close = getattr(iterator, "close", None)
            if callable(close):
                close()


def _make_model_config() -> ModelConfig:
    return ModelConfig(
        provider="openai",
        model=ROUTE_MODEL,
        endpoint=ROUTE_ENDPOINT,
        api_mode=ROUTE_API_MODE,
        vendor=ROUTE_PROVIDER,
        deployment=ROUTE_DEPLOYMENT,
        credential_env=CREDENTIAL_ENV,
        context_window_tokens=131_072,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        timeout_seconds=TIMEOUT_SECONDS,
        temperature=TEMPERATURE,
        max_retries=MAX_RETRIES,
        route_max_concurrency=CONCURRENCY,
    )


def _network_check() -> dict:
    parsed = urlsplit(ROUTE_ENDPOINT)
    host = parsed.hostname
    port = parsed.port or 443
    started = time.perf_counter()
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not addresses:
            raise OSError("DNS returned no addresses")
        with socket.create_connection((host, port), timeout=5):
            pass
    except Exception as error:  # noqa: BLE001 - preflight must classify environment failure
        return {
            "status": "not_verified",
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 6),
            "error_type": type(error).__name__,
        }
    return {
        "status": "passed",
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 6),
        "address_count": len(addresses),
    }


def _build_corpus(root: Path):
    train_dev_path = root / "train-dev.db"
    test_path = root / "test.db"
    repeat_train_dev_path = root / "repeat-train-dev.db"
    repeat_test_path = root / "repeat-test.db"
    generate.build(seed=DEV_SEED, out_path=train_dev_path)
    generate.build(seed=TEST_SEED, out_path=test_path, n_classes=TEST_N_CLASSES,
                   courses_per_class=TEST_COURSES_PER_CLASS)
    generate.build(seed=DEV_SEED, out_path=repeat_train_dev_path)
    generate.build(seed=TEST_SEED, out_path=repeat_test_path, n_classes=TEST_N_CLASSES,
                   courses_per_class=TEST_COURSES_PER_CLASS)
    train_dev = db.connect(train_dev_path)
    test = db.connect(test_path)
    repeated_train_dev = db.connect(repeat_train_dev_path)
    repeated_test = db.connect(repeat_test_path)
    corpus = build_lineage_corpus(train_dev, test)
    repeated = build_lineage_corpus(repeated_train_dev, repeated_test)
    lineage = audit_lineage(corpus, repeated_tasks=repeated)
    repeated_train_dev.close()
    repeated_test.close()
    if not lineage_gate_passed(lineage):
        raise R52PreflightError(f"Test lineage preflight failed: {lineage['errors']}")
    return train_dev, test, corpus, lineage, build_lineage_manifest(corpus)


def _summarize_reports(reports: list[dict], observations: list[dict]) -> dict:
    def values(name: str):
        return [float(item[name]) for item in observations if item.get(name) is not None]

    success = [float(report["trajectory_success_rate"]) for report in reports]
    param_accuracy = [float(report["param_accuracy"]) for report in reports]
    return {
        "repeats": len(reports),
        "trajectory_success_rate": {"mean": fmean(success), "variance": pvariance(success) if len(success) > 1 else 0.0, "values": success},
        "tool_precision": {"mean": fmean([report["tool_precision"] for report in reports]), "variance": pvariance([report["tool_precision"] for report in reports]) if len(reports) > 1 else 0.0},
        "tool_recall": {"mean": fmean([report["tool_recall"] for report in reports]), "variance": pvariance([report["tool_recall"] for report in reports]) if len(reports) > 1 else 0.0},
        "tool_f1": {"mean": fmean([report["tool_selection_f1"] for report in reports]), "variance": pvariance([report["tool_selection_f1"] for report in reports]) if len(reports) > 1 else 0.0},
        "param_accuracy": {"mean": fmean(param_accuracy), "variance": pvariance(param_accuracy) if len(param_accuracy) > 1 else 0.0, "values": param_accuracy},
        "repeat_metrics": [
            {
                "repeat_index": index,
                "trajectory_success_rate": report["trajectory_success_rate"],
                "tool_precision": report["tool_precision"],
                "tool_recall": report["tool_recall"],
                "tool_f1": report["tool_selection_f1"],
                "param_accuracy": report["param_accuracy"],
                "step_completion_rate": report["step_completion_rate"],
                "early_termination_rate": report["early_termination_rate"],
                "avg_model_calls": report["avg_model_calls"],
                "avg_tool_calls": report["avg_tool_calls"],
            }
            for index, report in enumerate(reports, start=1)
        ],
        "plan_evidence": {
            "step_completion_mean": fmean([report["step_completion_rate"] for report in reports]),
            "early_termination_mean": fmean([report["early_termination_rate"] for report in reports]),
            "plan_observed": False,
            "scope": "agent_harness_step_evidence",
        },
        "latency_ms": {
            "first_output_delta_mean": fmean(values("first_output_delta_ms")) if values("first_output_delta_ms") else None,
            "first_output_delta_p95": sorted(values("first_output_delta_ms"))[max(0, int(round(0.95 * (len(values("first_output_delta_ms")) - 1))))] if values("first_output_delta_ms") else None,
            "total_mean": fmean(values("total_latency_ms")) if values("total_latency_ms") else None,
            "total_p95": sorted(values("total_latency_ms"))[max(0, int(round(0.95 * (len(values("total_latency_ms")) - 1))))] if values("total_latency_ms") else None,
        },
        "usage": {
            "input_tokens": sum(item.get("usage", {}).get("prompt_tokens", item.get("usage", {}).get("input_tokens", 0)) for item in observations),
            "output_tokens": sum(item.get("usage", {}).get("completion_tokens", item.get("usage", {}).get("output_tokens", 0)) for item in observations),
            "total_tokens": sum(item.get("usage", {}).get("total_tokens", 0) for item in observations),
            "provider_attempts": sum(item.get("attempt_count", 0) for item in observations),
        },
        "cost": {
            "status": "estimated_unverified_price",
            "provider_billing_cost_usd": None,
            "estimated_cost_usd": sum(item.get("estimated_cost_usd") or 0.0 for item in observations),
        },
        "failure_classification": dict(Counter(item.get("failure_class") for item in observations if item.get("failure_class"))),
        "recovery_safety": {
            "status": "not_exercised",
            "reason": "The fixed Test run does not inject crash/replay faults; recovery remains covered only by offline runtime tests.",
        },
    }


def _resolve_output_path(
    requested: str | None,
    *,
    evidence_mode: str,
) -> Path:
    if requested is None:
        directory = "ci-artifacts" if evidence_mode in {"candidate", "release"} else "artifacts"
        return Path(directory) / "r52-real-model-eval.json"

    output = Path(requested)
    if evidence_mode not in {"candidate", "release"}:
        return output
    resolved = output.expanduser().resolve()
    project_root = PROJECT_ROOT.resolve()
    if project_root == resolved or project_root in resolved.parents:
        relative = resolved.relative_to(project_root)
        if not relative.parts or relative.parts[0] != "ci-artifacts":
            raise R52PreflightError(
                "candidate/release output inside the repository must use ci-artifacts"
            )
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="R5.2 fixed DashScope Test evaluation")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument(
        "--output",
        default=None,
        help="development defaults to artifacts; candidate/release defaults to ci-artifacts",
    )
    parser.add_argument(
        "--evidence-mode",
        choices=EVIDENCE_MODES,
        default="development",
        help="require clean Git provenance for candidate or release evidence",
    )
    args = parser.parse_args()
    if args.repeats < 3:
        parser.error("R5.2 requires at least 3 repeats")
    try:
        output = _resolve_output_path(
            args.output,
            evidence_mode=args.evidence_mode,
        )
    except R52PreflightError as error:
        raise SystemExit(f"R5.2 preflight blocked: {error}") from error

    preflight_provenance = build_provenance(
        repo_root=PROJECT_ROOT,
        config={"stage": "r52-live-preflight", "split": "test"},
        seed=TEST_SEED,
        model_name=ROUTE_MODEL,
        model_mode="real_openai_compatible",
        evidence_mode=args.evidence_mode,
    )
    if not provenance_gate_passed(preflight_provenance):
        reasons = preflight_provenance["provenance_gate"]["reasons"]
        raise SystemExit(
            "R5.2 preflight blocked: Git provenance gate failed: "
            + ",".join(reasons)
        )
    if not os.environ.get(CREDENTIAL_ENV, "").strip():
        raise SystemExit("R5.2 preflight blocked: credential ref is absent")

    network = _network_check()
    if network["status"] != "passed":
        raise SystemExit("R5.2 preflight blocked: network not verified")

    model_config = _make_model_config()
    spec = model_config.provider_spec()
    route_probe = ProviderSpec(
        model=ROUTE_MODEL,
        endpoint=ROUTE_ENDPOINT,
        api_mode=ROUTE_API_MODE,
        provider=ROUTE_PROVIDER,
        deployment=ROUTE_DEPLOYMENT,
        credential=spec.credential,
        capabilities=spec.capabilities,
    )
    from edu_agent.engine.gateway import ProviderGateway

    gateway = ProviderGateway()
    route = gateway.begin_turn(route_probe)
    effective = gateway.capabilities_for(route)
    requirements = infer_request_requirements(
        [{"role": "system", "content": "x"}, {"role": "user", "content": "x"}],
        [{"type": "function", "function": {"name": "x", "parameters": {"type": "object"}}}],
        model=ROUTE_MODEL,
        max_output_tokens=MAX_OUTPUT_TOKENS,
    )
    gaps = capability_gaps(requirements, effective, api_mode=route.api_mode,
                           require_known_context=True, require_known_output=True)
    if gaps:
        raise SystemExit(f"R5.2 preflight blocked: capability gaps {gaps}")

    guard = R52SpendGuard(max_cost_usd=MAX_COST_USD)
    raw_path = output.with_name(f"{output.stem}.raw.jsonl")
    failed_path = output.with_name(f"{output.stem}.failed-traces.jsonl")
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="edu-agent-r52-live-") as directory:
        train_dev, test, corpus, lineage, manifest = _build_corpus(Path(directory))
        tasks = tasks_for_split(corpus, "test")
        shared = get_engine(model_config)
        reports: list[dict] = []
        observations: list[dict] = []
        raw_records: list[dict] = []
        failures: list[dict] = []
        try:
            for repeat_index in range(1, args.repeats + 1):
                per_repeat: list[dict] = []
                for task in tasks:
                    measured = MeasuredEngine(shared, task_id=task.id, repeat_index=repeat_index, guard=guard)
                    report = run_eval([task], lambda _task, measured=measured: measured, db_conn=test)
                    per_repeat.append(report)
                    observations.extend(measured.observations)
                    record = report["records"][0]
                    task_record = {
                        "id": record["id"],
                        "success": record["success"],
                        "category": record["category"],
                        "tools_called": record["tools_called"],
                        "trajectory": record["trajectory"],
                        "stop_reason": record.get("stop_reason"),
                        "lineage": record["lineage"],
                    }
                    raw_records.extend(
                        {**observation, "task_record": task_record}
                        for observation in measured.observations
                    )
                    if not record.get("success"):
                        failures.append({
                            "task_id": task.id,
                            "repeat_index": repeat_index,
                            "sample_id": task.lineage.sample_id,
                            "failure_class": measured.observations[-1].get("failure_class") if measured.observations else "model_failure",
                            "failure_kind": measured.observations[-1].get("failure_kind") if measured.observations else "trajectory_unsuccessful",
                            "trace_reference": f"{failed_path.name}#{len(failures) + 1}",
                        })
                # A one-task report is enough to retain records; aggregate the
                # repeat through the same metrics contract used by run_eval.
                from edu_agent.eval.metrics import aggregate

                records = [record for report in per_repeat for record in report["records"]]
                repeat_report = aggregate(records)
                repeat_report["lineage"] = lineage
                repeat_report["records"] = records
                reports.append(repeat_report)
                print(f"[repeat {repeat_index}/{args.repeats}]")
                print(format_report(repeat_report))
        finally:
            train_dev.close()
            test.close()

    config = {
        "route": {
            "provider": route.provider,
            "api_mode": route.api_mode.value,
            "deployment": route.deployment,
            "model": route.model,
            "endpoint_identity_sha256": _endpoint_hash(),
        },
        "temperature": TEMPERATURE,
        "seed": SEED,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "concurrency": CONCURRENCY,
        "timeout_seconds": TIMEOUT_SECONDS,
        "max_retries": MAX_RETRIES,
        "max_cost_usd": float(MAX_COST_USD),
        "pricing_version": PRICING_VERSION,
        "pricing_status": "unverified_example_only",
        "split": "test",
        "repeats": args.repeats,
        "test_seed": TEST_SEED,
        "lineage_manifest_hash": manifest["manifest_hash"],
        "input_hashes": {
            relative: file_hash(PROJECT_ROOT / relative)
            for relative in (
                "pyproject.toml",
                "uv.lock",
                "scripts/eval_real_r52.py",
                "edu_agent/eval/harness.py",
                "edu_agent/eval/metrics.py",
                "edu_agent/eval/lineage.py",
                "edu_agent/eval/tasks_test.py",
            )
        },
    }
    provenance = build_provenance(
        repo_root=PROJECT_ROOT,
        config=config,
        seed=TEST_SEED,
        model_name=ROUTE_MODEL,
        model_mode="real_openai_compatible",
        evidence_mode=args.evidence_mode,
    )
    raw_path.write_text(
        "".join(json.dumps(sanitize_artifact(item, secrets=credential_literals()), ensure_ascii=False, sort_keys=True) + "\n" for item in raw_records),
        encoding="utf-8",
    )
    if failures:
        failed_path.write_text(
            "".join(json.dumps(sanitize_artifact(item, secrets=credential_literals()), ensure_ascii=False, sort_keys=True) + "\n" for item in failures),
            encoding="utf-8",
        )
    else:
        failed_path.unlink(missing_ok=True)
    summary = _summarize_reports(reports, observations)
    completed_count = sum(item.get("status") == "completed" for item in observations)
    real_status = "verified" if completed_count else "not_verified"
    artifact = {
        "schema_version": "edu-agent.r52-real-model-eval.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        **provenance,
        "route": config["route"],
        "frozen_parameters": {
            "temperature": TEMPERATURE,
            "seed": SEED,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "concurrency": CONCURRENCY,
            "effective_model_concurrency": 1,
            "timeout_seconds": TIMEOUT_SECONDS,
            "max_retries": MAX_RETRIES,
        },
        "lineage": lineage,
        "split": "test",
        "summary": summary,
        "budget": guard.snapshot(),
        "raw_runs": {
            "artifact": raw_path.name,
            "records": len(raw_records),
            "redacted": True,
        },
        "failed_traces": {
            "artifact": failed_path.name if failures else None,
            "records": len(failures),
            "redacted": True,
        },
        "preflight_artifact": "artifacts/r52-real-model-live-preflight.json",
        "prior_attempts": [
            {
                "artifact": "artifacts/r52-real-model-harness-failure-01.json",
                "classification": "harness_bug",
                "counted_as_model_evidence": False,
            },
            {
                "artifact": "artifacts/r52-real-model-env-failure-01.json",
                "classification": "environment_not_verified",
                "counted_as_model_evidence": False,
                "requests_completed": 0,
            },
            {
                "artifact": "artifacts/r52-real-model-eval-attempt-uncounted-budget.json",
                "classification": "harness_bug",
                "counted_as_model_evidence": False,
                "reason": "model-call accounting was incomplete",
            },
            {
                "artifact": "artifacts/r52-real-model-eval-before-raw-enhancement.json",
                "classification": "harness_bug",
                "counted_as_model_evidence": False,
                "reason": "per-task raw trajectory evidence was incomplete",
            },
            {
                "artifact": "artifacts/r52-real-model-eval-before-safe-raw.json",
                "classification": "harness_bug",
                "counted_as_model_evidence": False,
                "reason": "raw latency fields were removed by the central sensitive-key classifier",
            },
            {
                "artifact": "artifacts/r52-real-model-eval-before-full-raw.json",
                "classification": "harness_bug",
                "counted_as_model_evidence": False,
                "reason": "raw JSONL retained only the last observation per task",
            },
        ],
        "real_model": {
            "status": real_status,
            "requests_completed": completed_count,
            "evidence_scope": "independent_test",
            "recovery_safety": summary["recovery_safety"],
        },
        "offline_oracle_comparison": {
            "artifact": "artifacts/r52-oracle-test-eval.json",
            "scope": "harness_only",
        },
        "network_preflight": network,
        "elapsed_seconds": round(time.perf_counter() - started, 6),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(sanitize_artifact(artifact, secrets=credential_literals()), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"脱敏 R5.2 结果已写入 {output}")
    print(json.dumps({"summary": summary, "budget": guard.snapshot()}, ensure_ascii=False, sort_keys=True))
    return 0 if provenance_gate_passed(provenance) else 1


if __name__ == "__main__":
    raise SystemExit(main())
