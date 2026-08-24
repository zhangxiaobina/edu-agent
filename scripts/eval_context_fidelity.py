#!/usr/bin/env python3
"""Run the deterministic R4.3 context-fidelity corpus offline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from edu_agent.eval.context_fidelity import (
    assert_context_fidelity_thresholds,
    build_context_fidelity_corpus,
    evaluate_context_fidelity,
    observe_context_fidelity_case,
    validate_context_fidelity_corpus,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--thresholds",
        type=Path,
        help="optional JSON object of metric thresholds; omitted means report-only",
    )
    args = parser.parse_args()
    cases = build_context_fidelity_corpus()
    gate = validate_context_fidelity_corpus(
        cases,
        repeated_cases=build_context_fidelity_corpus(),
    )
    # Observations execute the production deterministic summary, checkpoint
    # hysteresis, and Context Accountant paths.  No external model or tokenizer
    # download participates in the offline baseline.
    observations = {case.case_id: observe_context_fidelity_case(case) for case in cases}
    metrics = evaluate_context_fidelity(cases, observations)
    threshold_report = {"configured": False, "passed": True, "errors": []}
    if args.thresholds is not None:
        thresholds = json.loads(args.thresholds.read_text(encoding="utf-8"))
        try:
            assert_context_fidelity_thresholds(metrics, thresholds)
        except AssertionError as error:
            threshold_report = {
                "configured": True,
                "passed": False,
                "errors": [str(error)],
            }
        else:
            threshold_report["configured"] = True
    report = {
        "schema": "edu-agent.context-fidelity-report.v1",
        "lineage": gate,
        "metrics": metrics.to_dict(),
        "thresholds": threshold_report,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if gate["passed"] and threshold_report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
