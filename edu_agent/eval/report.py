"""Versioned system-evaluation report contracts.

The runtime evaluation helpers deliberately produce small, independently
auditable sections.  This module owns the publication contract so a missing
probe cannot accidentally become a successful result merely because a key was
omitted from a JSON artifact.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


# ``schema_version`` remains v4 for compatibility with existing artifact
# consumers.  ``report_schema`` is the additive R5 publication contract.
REPORT_SCHEMA_VERSION = "edu-agent.system-eval.v5"
COMPAT_SCHEMA_VERSION = "edu-agent.system-eval.v4"
REPORT_STATUS_VALUES = frozenset({"passed", "failed", "not_run", "not_verified"})

REPORT_SECTIONS = (
    "agent_plan",
    "provider_route_retry",
    "stream_cancel",
    "journal_recovery",
    "tool_manifest_concurrency",
    "context",
    "budget",
    "transaction",
    "sandbox",
    "performance",
    "provenance",
    "data_boundary",
)

# Docker is an independently provisioned capability.  It may be absent on a
# local/CI runner, while every other section is part of the offline gate.
OFFLINE_REQUIRED_SECTIONS = tuple(
    section for section in REPORT_SECTIONS if section != "sandbox"
)


def report_section(
    *,
    status: str,
    source: str,
    tests: Sequence[str] = (),
    metrics: Mapping[str, Any] | None = None,
    reason: str | None = None,
    evidence: Sequence[str] = (),
    duration_seconds: float | None = None,
) -> dict[str, Any]:
    """Build a complete report section with an explicit status.

    Every section always contains ``status``, ``source``, ``tests`` and
    ``metrics``.  ``reason`` is mandatory for non-success states and is kept
    separate from metrics so a null metric cannot be mistaken for a pass.
    """

    if status not in REPORT_STATUS_VALUES:
        raise ValueError(f"unsupported report status: {status!r}")
    normalized_source = str(source).strip()
    if not normalized_source:
        raise ValueError("report section source must not be empty")
    if status in {"failed", "not_run", "not_verified"} and not str(reason or "").strip():
        raise ValueError(f"{status} report sections require a reason")
    if status == "passed" and not tuple(tests):
        raise ValueError("passed report sections require test evidence")
    if status == "passed" and metrics is None:
        raise ValueError("passed report sections require metrics")
    section: dict[str, Any] = {
        "status": status,
        "source": normalized_source,
        "tests": [str(test) for test in tests],
        "metrics": dict(metrics) if metrics is not None else None,
        "evidence": [str(item) for item in evidence],
    }
    if reason is not None:
        section["reason"] = str(reason)
    if duration_seconds is not None:
        section["duration_seconds"] = round(float(duration_seconds), 6)
    return section


def _section_errors(name: str, value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return [f"section:{name}:missing"]
    errors: list[str] = []
    status = value.get("status")
    if status not in REPORT_STATUS_VALUES:
        errors.append(f"section:{name}:invalid_status")
    source = value.get("source")
    if not isinstance(source, str) or not source.strip():
        errors.append(f"section:{name}:missing_source")
    tests = value.get("tests")
    if not isinstance(tests, list):
        errors.append(f"section:{name}:missing_tests")
    elif status == "passed" and not tests:
        errors.append(f"section:{name}:passed_without_tests")
    if "metrics" not in value:
        errors.append(f"section:{name}:missing_metrics")
    elif status == "passed" and value.get("metrics") is None:
        errors.append(f"section:{name}:missing_metrics")
    if status in {"failed", "not_run", "not_verified"} and not str(value.get("reason") or "").strip():
        errors.append(f"section:{name}:missing_reason")
    return errors


def validate_report(
    report: Mapping[str, Any],
    *,
    evidence_mode: str | None = None,
) -> list[str]:
    """Return deterministic publication-gate errors for a system report."""

    errors: list[str] = []
    if report.get("schema_version") != COMPAT_SCHEMA_VERSION:
        errors.append("schema_version_incompatible")
    report_schema = report.get("report_schema")
    if not isinstance(report_schema, Mapping) or report_schema.get("version") != REPORT_SCHEMA_VERSION:
        errors.append("report_schema_missing")
    sections = report.get("sections")
    if not isinstance(sections, Mapping):
        return errors + ["sections_missing"]
    for name in REPORT_SECTIONS:
        errors.extend(_section_errors(name, sections.get(name)))
    unknown = sorted(set(sections) - set(REPORT_SECTIONS))
    errors.extend(f"section:{name}:unknown" for name in unknown)

    lineage = report.get("lineage")
    if not isinstance(lineage, Mapping) or lineage.get("passed") is not True:
        errors.append("lineage_gate_failed")
    provenance = report.get("provenance_gate")
    if not isinstance(provenance, Mapping):
        errors.append("provenance_gate_missing")

    mode = evidence_mode or report.get("evidence_mode", "development")
    if mode in {"candidate", "release"}:
        git = report.get("git")
        git_mapping = git if isinstance(git, Mapping) else {}
        if report.get("commit") in {None, "", "unavailable", "not_available"}:
            errors.append("git_commit_unavailable")
        if not isinstance(git_mapping.get("available"), bool) or not git_mapping["available"]:
            errors.append("git_provenance_unavailable")
        if not isinstance(git_mapping.get("dirty"), bool) or git_mapping["dirty"]:
            errors.append("git_worktree_not_clean")
        if not isinstance(lineage, Mapping) or lineage.get("passed") is not True:
            errors.append("lineage_leakage_or_invalid")
        if not isinstance(provenance, Mapping) or provenance.get("status") != "passed":
            errors.append("provenance_gate_not_passed")
        for name in OFFLINE_REQUIRED_SECTIONS:
            status = sections.get(name, {}).get("status") if isinstance(sections.get(name), Mapping) else None
            if status == "not_run":
                errors.append(f"required_offline_not_run:{name}")
            elif status != "passed":
                errors.append(f"required_offline_not_passed:{name}")
        provenance_section = sections.get("provenance")
        if not isinstance(provenance_section, Mapping) or provenance_section.get("status") != "passed":
            errors.append("required_provenance_not_passed")
        boundary_section = sections.get("data_boundary")
        if not isinstance(boundary_section, Mapping) or boundary_section.get("status") != "passed":
            errors.append("required_data_boundary_not_passed")
    return sorted(set(errors))


def report_gate_passed(report: Mapping[str, Any], *, evidence_mode: str | None = None) -> bool:
    return not validate_report(report, evidence_mode=evidence_mode)


__all__ = [
    "COMPAT_SCHEMA_VERSION",
    "OFFLINE_REQUIRED_SECTIONS",
    "REPORT_SCHEMA_VERSION",
    "REPORT_SECTIONS",
    "REPORT_STATUS_VALUES",
    "report_gate_passed",
    "report_section",
    "validate_report",
]
