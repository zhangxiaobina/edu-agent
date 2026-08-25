from __future__ import annotations

from edu_agent.eval.report import (
    OFFLINE_REQUIRED_SECTIONS,
    REPORT_SCHEMA_VERSION,
    report_gate_passed,
    report_section,
    validate_report,
)
from scripts.audit_acceptance_coverage import audit


def _candidate_report() -> dict:
    sections = {
        name: report_section(
            status="passed",
            source=f"test:{name}",
            tests=[f"tests/{name}.py"],
            metrics={"ok": True},
        )
        for name in OFFLINE_REQUIRED_SECTIONS
    }
    sections["sandbox"] = report_section(
        status="not_verified",
        source="test",
        tests=["tests/test_code_execution.py"],
        metrics=None,
        reason="Docker is not provisioned in this fixture",
    )
    return {
        "schema_version": "edu-agent.system-eval.v4",
        "report_schema": {"version": REPORT_SCHEMA_VERSION},
        "evidence_mode": "candidate",
        "commit": "a" * 40,
        "git": {"available": True, "dirty": False},
        "provenance_gate": {"status": "passed"},
        "lineage": {"passed": True},
        "sections": sections,
    }


def test_candidate_report_requires_explicit_status_for_every_section():
    report = _candidate_report()
    assert report_gate_passed(report, evidence_mode="candidate")

    report["sections"]["budget"] = {
        "source": "missing-status",
        "tests": [],
        "metrics": None,
    }
    errors = validate_report(report, evidence_mode="candidate")
    assert "section:budget:invalid_status" in errors
    assert "required_offline_not_passed:budget" in errors


def test_candidate_report_rejects_required_not_run_and_lineage_failure():
    report = _candidate_report()
    report["sections"]["context"] = report_section(
        status="not_run",
        source="test",
        tests=["tests/test_context_fidelity.py"],
        metrics=None,
        reason="not executed",
    )
    report["lineage"] = {"passed": False}
    errors = validate_report(report, evidence_mode="candidate")
    assert "required_offline_not_run:context" in errors
    assert "lineage_leakage_or_invalid" in errors
    assert not report_gate_passed(report, evidence_mode="candidate")


def test_acceptance_coverage_checks_the_stage8_call_graph():
    report = audit()
    assert report["status"] == "passed", report["errors"]
    assert report["public_entrypoint"] == "zsh scripts/accept_stage8.sh"
    assert report["full_suite"] == {
        "command": "python -m pytest -p no:cacheprovider tests -q",
        "count": 1,
        "status": "passed",
    }
    assert report["expensive_steps"] == {
        "data_audit": 2,
        "lineage": 1,
        "system_eval": 1,
        "trace_10k": 1,
    }
    assert all(section["status"] == "passed" for section in report["groups"].values())
