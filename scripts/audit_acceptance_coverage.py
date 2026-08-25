#!/usr/bin/env python3
"""Verify that the public Stage 8 entrypoint reaches R1-R4 evidence.

This is a call-graph audit, not a file-existence check.  It requires the
highest-stage full-suite command, the explicit R2/Stage 7 regression calls,
and the expensive lineage/Trace/data-boundary commands to be present exactly
once on the Stage 8 path.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from edu_agent.eval.provenance import credential_literals, sanitize_artifact


SCHEMA_VERSION = "edu-agent.acceptance-coverage.v1"
ROOT = Path(__file__).resolve().parents[1]

GROUPS = {
    "r1_provider": {
        "claim": "Provider routes, adapters, retry and fallback are explicit",
        "report_fields": ["sections.provider_route_retry"],
        "tests": (
            "tests/test_provider_gateway.py",
            "tests/test_provider_adapter_contract.py",
            "tests/test_provider_resilience.py",
            "tests/test_chat_completions_adapter.py",
            "tests/test_responses_adapter.py",
            "tests/test_provider_streaming.py",
            "tests/test_r1_fake_provider_acceptance.py",
        ),
        "anchors": ("accept_r2.sh", "tests/test_provider_streaming.py"),
    },
    "r2_stream_cancel_journal_recovery": {
        "claim": "Stream, cancellation, journal and crash recovery reject unsafe late work",
        "report_fields": ["sections.stream_cancel", "sections.journal_recovery"],
        "tests": (
            "tests/test_run_events.py",
            "tests/test_run_journal.py",
            "tests/test_turn_finalizer.py",
            "tests/test_cancellation.py",
            "tests/test_api_sse_cancellation.py",
            "tests/test_r2_recovery.py",
        ),
        "anchors": ("accept_r2.sh", "scripts/r2_recovery_demo.py"),
    },
    "r3_manifest_provider_arguments_concurrency": {
        "claim": "ToolManifest, provider contracts, argument validation and safe concurrency are frozen",
        "report_fields": ["sections.tool_manifest_concurrency"],
        "tests": (
            "tests/test_tool_manifest.py",
            "tests/test_r36_boundaries.py",
            "tests/test_tool_arguments.py",
            "tests/test_tool_batch.py",
            "tests/test_teaching_provider_contract.py",
            "tests/test_builtin_tool_contract_matrix.py",
            "tests/test_mcp.py",
        ),
        "anchors": ("accept_stage7.sh", "scripts/mcp_demo.py"),
    },
    "r4_context_budget_lifecycle_storage": {
        "claim": "Context, budget, lifecycle and storage recovery are durable and bounded",
        "report_fields": ["sections.context", "sections.budget", "sections.journal_recovery"],
        "tests": (
            "tests/test_context_accounting.py",
            "tests/test_context_checkpoint.py",
            "tests/test_context_fidelity.py",
            "tests/test_r43_context_policy.py",
            "tests/test_r43_context_recovery.py",
            "tests/test_run_budget_ledger.py",
            "tests/test_lifecycle.py",
            "tests/test_r46_storage_maintenance.py",
        ),
        "anchors": ("accept_stage8.sh", "scripts/state_maintenance.py"),
    },
}


def audit(repo_root: Path = ROOT) -> dict:
    scripts = {
        name: (repo_root / "scripts" / name).read_text(encoding="utf-8")
        for name in ("accept_stage8.sh", "accept_stage7.sh", "accept_r2.sh")
    }
    stage8 = scripts["accept_stage8.sh"]
    stage7 = scripts["accept_stage7.sh"]
    stage8_call_graph = stage8 + "\n" + stage7
    errors: list[str] = []
    full_suite_command = "python -m pytest -p no:cacheprovider tests -q"
    if stage8.count(full_suite_command) != 1:
        errors.append("stage8_full_suite_command_not_exactly_once")
    if stage8.count("scripts/accept_r2.sh") != 1:
        errors.append("stage8_r2_boundary_not_exactly_once")
    if stage8.count("scripts/accept_stage7.sh") != 1:
        errors.append("stage8_stage7_boundary_not_exactly_once")
    expensive_counts = {
        "lineage": sum("python scripts/audit_eval_lineage.py" in line for line in stage8.splitlines()),
        "trace_10k": sum("python scripts/benchmark_trace_scaling.py" in line for line in stage8.splitlines()),
        "data_audit": sum("python scripts/audit_data_boundaries.py" in line for line in stage8.splitlines()),
        "system_eval": sum("python scripts/eval_system.py" in line for line in stage8_call_graph.splitlines()),
    }
    if expensive_counts["lineage"] != 1 or expensive_counts["trace_10k"] != 1:
        errors.append("expensive_lineage_or_trace_step_not_exactly_once")
    # Data audit has an early state check and one final artifact check.
    if expensive_counts["data_audit"] != 2:
        errors.append("data_boundary_audit_expected_two_phases")
    if expensive_counts["system_eval"] != 1:
        errors.append("system_eval_not_exactly_once")

    groups: dict[str, dict] = {}
    for name, definition in GROUPS.items():
        missing = [path for path in definition["tests"] if not (repo_root / path).is_file()]
        anchors_missing = [anchor for anchor in definition["anchors"] if anchor not in stage8 and anchor not in scripts["accept_r2.sh"] and anchor not in scripts["accept_stage7.sh"]]
        status = "passed" if not missing and not anchors_missing else "failed"
        groups[name] = {
            "status": status,
            "claim": definition["claim"],
            "report_fields": list(definition["report_fields"]),
            "tests": list(definition["tests"]),
            "execution": "stage8_full_suite_plus_explicit_regression_boundary",
            "missing_tests": missing,
            "missing_anchors": anchors_missing,
        }
        if missing:
            errors.extend(f"{name}:missing_test:{path}" for path in missing)
        if anchors_missing:
            errors.extend(f"{name}:missing_anchor:{anchor}" for anchor in anchors_missing)

    report = {
        "schema_version": SCHEMA_VERSION,
        "status": "passed" if not errors else "failed",
        "public_entrypoint": "zsh scripts/accept_stage8.sh",
        "highest_stage": "stage8",
        "full_suite": {
            "command": full_suite_command,
            "count": stage8.count(full_suite_command),
            "status": "passed" if stage8.count(full_suite_command) == 1 else "failed",
        },
        "expensive_steps": expensive_counts,
        "groups": groups,
        "errors": sorted(set(errors)),
    }
    return sanitize_artifact(report, secrets=credential_literals())


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit the Stage 8 R1-R4 acceptance call graph")
    parser.add_argument("--output")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    report = audit()
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
    if not args.quiet:
        print(encoded, end="")
    elif report["status"] == "passed":
        print("acceptance coverage audit passed")
    else:
        print("acceptance coverage audit failed")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
