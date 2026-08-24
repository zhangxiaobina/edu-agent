from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from edu_agent.eval.context_fidelity import (
    assert_context_fidelity_thresholds,
    build_context_fidelity_corpus,
    evaluate_context_fidelity,
    observe_context_fidelity_case,
    validate_context_fidelity_corpus,
)


def test_context_fidelity_corpus_has_stable_split_lineage():
    cases = build_context_fidelity_corpus()
    gate = validate_context_fidelity_corpus(
        cases,
        repeated_cases=build_context_fidelity_corpus(),
    )
    assert gate["passed"] is True
    assert gate["deterministic_generation"]["passed"] is True
    assert gate["split_counts"] == {"dev": 3, "test": 3, "train": 6}
    assert {case.split for case in cases} == {"train", "dev", "test"}
    assert len(cases) >= 12
    assert len({case.case_id for case in cases}) == len(cases)


def test_context_fidelity_lineage_rejects_overlap_missing_provenance_and_sensitive_data():
    records = [case.to_dict() for case in build_context_fidelity_corpus()]
    train = next(record for record in records if record["split"] == "train")
    test = next(record for record in records if record["split"] == "test")

    semantic_overlap = deepcopy(records)
    overlapping_test = next(record for record in semantic_overlap if record["case_id"] == test["case_id"])
    overlapping_test["lineage"]["semantic_group"] = train["lineage"]["semantic_group"]
    overlap_gate = validate_context_fidelity_corpus(semantic_overlap)
    assert any(error.startswith("semantic_group_overlap:") for error in overlap_gate["errors"])

    family_overlap = deepcopy(records)
    overlapping_test = next(record for record in family_overlap if record["case_id"] == test["case_id"])
    overlapping_test["intent_template_family"] = train["intent_template_family"]
    overlapping_test["lineage"]["intent_template_family"] = train["intent_template_family"]
    overlapping_test["lineage"]["query_hash"] = train["lineage"]["query_hash"]
    family_gate = validate_context_fidelity_corpus(family_overlap)
    assert any(error.startswith("template_family_overlap:") for error in family_gate["errors"])

    missing = deepcopy(records)
    del missing[0]["lineage"]["source"]
    missing_gate = validate_context_fidelity_corpus(missing)
    assert any(error.startswith("missing_provenance:") for error in missing_gate["errors"])

    sensitive = deepcopy(records)
    sensitive[0]["api_key"] = "sk-context-lineage-canary-938247"
    sensitive_gate = validate_context_fidelity_corpus(sensitive)
    assert any(error.startswith("sensitive_fidelity_case:") for error in sensitive_gate["errors"])


def test_context_fidelity_lineage_rejects_non_deterministic_repeat():
    first = build_context_fidelity_corpus()
    repeated = [case.to_dict() for case in build_context_fidelity_corpus()]
    repeated[0]["entities"] = [*repeated[0]["entities"], "course_id=999"]
    gate = validate_context_fidelity_corpus(first, repeated_cases=repeated)
    assert gate["passed"] is False
    assert gate["deterministic_generation"]["error"] == "non_deterministic_generation"


def test_context_fidelity_metrics_detect_missing_refs_and_scope_leak():
    cases = build_context_fidelity_corpus()[:2]
    first, second = cases
    summaries = {
        first.case_id: " ".join(first.key_constraints + first.operation_ids),
        second.case_id: f"{second.scope['tenant_id']} only",
    }
    metrics = evaluate_context_fidelity(cases, summaries)
    assert metrics.constraint_fidelity < 1
    assert metrics.operation_fidelity < 1
    assert metrics.scope_leak_rate == 0
    leaked = {
        first.case_id: f"{first.scope['tenant_id']} {second.scope['tenant_id']}",
        second.case_id: "",
    }
    assert evaluate_context_fidelity(cases, leaked).scope_leak_rate > 0
    leaked_artifact = {
        first.case_id: second.artifact_ids[0],
        second.case_id: "",
    }
    assert evaluate_context_fidelity(cases, leaked_artifact).scope_leak_rate > 0


def test_context_fidelity_thresholds_are_caller_configured():
    cases = build_context_fidelity_corpus()
    observations = {case.case_id: observe_context_fidelity_case(case) for case in cases}
    metrics = evaluate_context_fidelity(cases, observations)
    assert all(
        observation.before_tokens > observation.after_tokens
        for observation in observations.values()
    )
    assert all(observation.trigger_count >= 1 for observation in observations.values())
    assert_context_fidelity_thresholds(
        metrics,
        json.loads(
            (Path(__file__).parent / "fixtures/context_fidelity_thresholds.json").read_text(
                encoding="utf-8"
            )
        ),
    )
    with pytest.raises(AssertionError):
        assert_context_fidelity_thresholds(metrics, {"constraint_fidelity": 1.1})
