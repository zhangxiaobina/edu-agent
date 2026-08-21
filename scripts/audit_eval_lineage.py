#!/usr/bin/env python3
"""Audit the complete synthetic Train/Dev/Test evaluation lineage."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from edu_agent.data import db, generate
from edu_agent.eval.corpus import build_lineage_corpus
from edu_agent.eval.lineage import audit_lineage, build_lineage_manifest, lineage_gate_passed
from edu_agent.eval.provenance import credential_literals, sanitize_artifact
from edu_agent.eval.tasks_test import (
    TEST_COURSES_PER_CLASS,
    TEST_N_CLASSES,
    TEST_SEED,
)


BASE_SEED = 42


def _build_corpus(root: Path, label: str):
    directory = root / label
    directory.mkdir(parents=True)
    train_dev_path = directory / "train-dev.db"
    test_path = directory / "test.db"
    generate.build(seed=BASE_SEED, out_path=train_dev_path)
    generate.build(
        seed=TEST_SEED,
        out_path=test_path,
        n_classes=TEST_N_CLASSES,
        courses_per_class=TEST_COURSES_PER_CLASS,
    )
    train_dev_conn = db.connect(train_dev_path)
    test_conn = db.connect(test_path)
    try:
        return build_lineage_corpus(train_dev_conn, test_conn)
    finally:
        train_dev_conn.close()
        test_conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit evaluation split lineage and determinism")
    parser.add_argument("--output", help="write the redaction-safe lineage report")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="edu-agent-lineage-audit-") as directory:
        root = Path(directory)
        first = _build_corpus(root, "first")
        second = _build_corpus(root, "second")
        report = audit_lineage(first, repeated_tasks=second)
        report["dataset_generation"] = {
            "train_dev": {"seed": BASE_SEED, "source": "existing_synthetic_generator"},
            "test": {
                "seed": TEST_SEED,
                "n_classes": TEST_N_CLASSES,
                "courses_per_class": TEST_COURSES_PER_CLASS,
                "source": "existing_synthetic_generator",
            },
        }
        report["manifest"] = build_lineage_manifest(first)
        report = sanitize_artifact(report, secrets=credential_literals())

    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
    if not args.quiet:
        print(encoded, end="")
    elif lineage_gate_passed(report):
        print("evaluation lineage audit passed")
    else:
        print("evaluation lineage audit failed")
    return 0 if lineage_gate_passed(report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
