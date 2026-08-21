"""Split-aware agentic evaluation runner.

  # Offline oracle: validates the Test harness, not model capability.
  uv run --frozen python scripts/eval_demo.py --engine oracle

  # Real model: save a redacted, repeat-aware Test artifact.
  export EDU_AGENT_ENGINE=openai
  uv run --frozen python scripts/eval_demo.py --engine openai --repeats 3 \
    --output artifacts/real-model-eval.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean, pvariance

from edu_agent.data import db, generate
from edu_agent.eval import (
    audit_lineage,
    build_lineage_manifest,
    build_lineage_corpus,
    format_report,
    lineage_gate_passed,
    make_oracle_engine,
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


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEV_SEED = 42


def _endpoint_hash(engine) -> str:
    endpoint = str(getattr(engine, "base_url", "unavailable"))
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


def _evidence_request_error(args) -> str | None:
    if args.evidence_mode in {"candidate", "release"} and args.split != "test":
        return "candidate/release model evidence requires --split test"
    if args.evidence_mode in {"candidate", "release"} and not args.output:
        return "candidate/release model evidence requires --output"
    return None


def _write_result(
    output: Path,
    *,
    args,
    reports: list[dict],
    model_name: str,
    model_mode: str,
    model_config: dict,
    tasks,
    lineage_report: dict,
    lineage_manifest: dict,
    seed: int,
) -> dict:
    config = {
        "seed": seed,
        "split": args.split,
        "repeats": args.repeats,
        "model": {"name": model_name, "mode": model_mode, **model_config},
        "lineage_manifest_hash": lineage_manifest["manifest_hash"],
        "input_hashes": {
            relative: file_hash(PROJECT_ROOT / relative)
            for relative in (
                "pyproject.toml",
                "uv.lock",
                "scripts/eval_demo.py",
                "edu_agent/eval/harness.py",
                "edu_agent/eval/lineage.py",
                "edu_agent/eval/metrics.py",
                "edu_agent/eval/tasks.py",
                "edu_agent/eval/tasks_test.py",
            )
        },
    }
    provenance = build_provenance(
        repo_root=PROJECT_ROOT,
        config=config,
        seed=seed,
        model_name=model_name,
        model_mode=model_mode,
        evidence_mode=args.evidence_mode,
    )
    success_rates = [float(report["trajectory_success_rate"]) for report in reports]
    failed = []
    for repeat_index, report in enumerate(reports, start=1):
        for record in report["records"]:
            if not record.get("success"):
                failed.append(sanitize_artifact({
                    "config_hash": provenance["config_hash"],
                    "repeat_index": repeat_index,
                    "run_id": f"{model_mode}-{repeat_index}",
                    **record,
                }, secrets=credential_literals()))
    failures_artifact = None
    output.parent.mkdir(parents=True, exist_ok=True)
    failures_path = output.with_name(f"{output.stem}.failed-trajectories.jsonl")
    if failed:
        failures_path.write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in failed) + "\n",
            encoding="utf-8",
        )
        failures_artifact = failures_path.name
    else:
        failures_path.unlink(missing_ok=True)

    oracle = args.engine == "oracle"
    artifact = {
        "schema_version": "edu-agent.model-eval.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        **provenance,
        "config": config,
        "lineage": lineage_report,
        "split": args.split,
        "evidence": {
            "harness": {
                "status": "verified" if oracle else "not_run_in_this_artifact",
                "source": "offline_oracle" if oracle else None,
                "scope": "harness_only",
            },
            "real_model": {
                "status": "not_run" if oracle else "completed",
                "source": None if oracle else "openai_compatible_endpoint",
                "metrics": None if oracle else {
                    "trajectory_success_rate_mean": fmean(success_rates),
                    "trajectory_success_rate_variance": (
                        pvariance(success_rates) if len(success_rates) > 1 else 0.0
                    ),
                },
            },
        },
        "repetitions": {
            "requested": args.repeats,
            "completed": len(reports),
            "run_ids": [f"{model_mode}-{index}" for index in range(1, len(reports) + 1)],
            "trajectory_success_rates": success_rates,
            "mean": fmean(success_rates),
            "variance": pvariance(success_rates) if len(success_rates) > 1 else 0.0,
        },
        "runs": [
            {
                "run_id": f"{model_mode}-{index}",
                "metrics": {
                    key: value for key, value in report.items()
                    if key not in {"records", "lineage"}
                },
            }
            for index, report in enumerate(reports, start=1)
        ],
        "failed_trajectories": failures_artifact,
    }
    artifact = sanitize_artifact(artifact, secrets=credential_literals())
    output.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return artifact


def main() -> int:
    ap = argparse.ArgumentParser(description="agentic 评测 demo")
    ap.add_argument("--engine", choices=["oracle", "openai"], default="oracle",
                    help="oracle=离线确定性回放（默认）；openai=接真引擎出真数")
    ap.add_argument("--split", choices=["dev", "test"], default="test")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--output", help="write a redacted JSON result and failed trajectories")
    ap.add_argument("--evidence-mode", choices=EVIDENCE_MODES, default="development")
    args = ap.parse_args()
    if args.repeats < 1:
        ap.error("--repeats must be >= 1")
    if evidence_error := _evidence_request_error(args):
        ap.error(evidence_error)

    with tempfile.TemporaryDirectory(prefix="edu-agent-model-eval-") as directory:
        root = Path(directory)
        train_dev_path = root / "train-dev.db"
        test_path = root / "test.db"
        repeat_train_dev_path = root / "repeat-train-dev.db"
        repeat_test_path = root / "repeat-test.db"
        generate.build(seed=DEV_SEED, out_path=train_dev_path)
        generate.build(
            seed=TEST_SEED,
            out_path=test_path,
            n_classes=TEST_N_CLASSES,
            courses_per_class=TEST_COURSES_PER_CLASS,
        )
        generate.build(seed=DEV_SEED, out_path=repeat_train_dev_path)
        generate.build(
            seed=TEST_SEED,
            out_path=repeat_test_path,
            n_classes=TEST_N_CLASSES,
            courses_per_class=TEST_COURSES_PER_CLASS,
        )
        train_dev_conn = db.connect(train_dev_path)
        test_conn = db.connect(test_path)
        repeat_train_dev_conn = db.connect(repeat_train_dev_path)
        repeat_test_conn = db.connect(repeat_test_path)
        try:
            corpus = build_lineage_corpus(train_dev_conn, test_conn)
            repeated_corpus = build_lineage_corpus(
                repeat_train_dev_conn,
                repeat_test_conn,
            )
            lineage_report = audit_lineage(corpus, repeated_tasks=repeated_corpus)
            if not lineage_gate_passed(lineage_report):
                raise RuntimeError(f"evaluation lineage preflight failed: {lineage_report['errors']}")
            lineage_manifest = build_lineage_manifest(corpus)
            tasks = tasks_for_split(corpus, args.split)
            conn = test_conn if args.split == "test" else train_dev_conn
            seed = TEST_SEED if args.split == "test" else DEV_SEED
        finally:
            repeat_train_dev_conn.close()
            repeat_test_conn.close()
        if args.engine == "oracle":
            make_engine = make_oracle_engine
            model_name = "oracle"
            model_mode = "offline_oracle"
            model_config = {}
        else:
            os.environ.setdefault("EDU_AGENT_ENGINE", "openai")
            from edu_agent.engine import get_engine

            shared = get_engine()
            model_name = str(shared.model)
            model_mode = "real_openai_compatible"
            model_config = {
                "endpoint_hash": _endpoint_hash(shared),
                "temperature": getattr(shared, "temperature", None),
            }

            def make_engine(_task):
                return shared

        try:
            label = (
                "离线 oracle（仅验证 harness，不代表模型能力）"
                if args.engine == "oracle"
                else f"真实 OpenAI-compatible 模型 {model_name}"
            )
            print(f"引擎: {label} · split={args.split} · 任务数 {len(tasks)}\n")
            reports = []
            for index in range(1, args.repeats + 1):
                report = run_eval(tasks, make_engine, db_conn=conn)
                reports.append(report)
                if args.repeats > 1:
                    print(f"[repeat {index}/{args.repeats}]")
                print(format_report(report))
        finally:
            train_dev_conn.close()
            test_conn.close()

    if not args.output:
        return 0
    artifact = _write_result(
        Path(args.output),
        args=args,
        reports=reports,
        model_name=model_name,
        model_mode=model_mode,
        model_config=model_config,
        tasks=tasks,
        lineage_report=lineage_report,
        lineage_manifest=lineage_manifest,
        seed=seed,
    )
    print(f"脱敏结果已写入 {Path(args.output).name}")
    passed = artifact["lineage"]["passed"] and provenance_gate_passed(artifact)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
