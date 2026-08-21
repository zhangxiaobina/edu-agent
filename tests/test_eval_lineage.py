from __future__ import annotations

import argparse
import json
from dataclasses import replace

import pytest

from edu_agent.data import db, generate
from edu_agent.data_audit import audit_paths
from edu_agent.eval.corpus import build_lineage_corpus, tasks_for_split
from edu_agent.eval.harness import run_eval
from edu_agent.eval.lineage import (
    LineageValidationError,
    audit_lineage,
    build_lineage_manifest,
    make_sample_lineage,
    validate_lineage,
)
from edu_agent.eval.oracle import make_oracle_engine
from edu_agent.eval.tasks_test import (
    TEST_COURSES_PER_CLASS,
    TEST_N_CLASSES,
    TEST_SEED,
    build_test_tasks,
)
from scripts.eval_demo import _evidence_request_error, _write_result


def _build_corpus(root, label):
    directory = root / label
    directory.mkdir()
    train_dev_path = directory / "train-dev.db"
    test_path = directory / "test.db"
    generate.build(seed=42, out_path=train_dev_path)
    generate.build(
        seed=TEST_SEED,
        out_path=test_path,
        n_classes=TEST_N_CLASSES,
        courses_per_class=TEST_COURSES_PER_CLASS,
    )
    train_dev_conn = db.connect(train_dev_path)
    test_conn = db.connect(test_path)
    corpus = build_lineage_corpus(train_dev_conn, test_conn)
    return corpus, train_dev_conn, test_conn


def _relineage(task, *, reference=None, split=None, family=None, semantic_group=None):
    original = (reference or task).lineage
    assert original is not None
    updated = make_sample_lineage(
        task,
        split=split or original.split,
        intent_template_family=family or original.intent_template_family,
        semantic_group=semantic_group or original.semantic_group,
        source=original.source,
        version=original.version,
        seed=original.seed,
        generator=original.generator,
    )
    return replace(task, lineage=updated)


def test_complete_corpus_is_family_isolated_and_deterministic(tmp_path):
    first, first_train, first_test = _build_corpus(tmp_path, "first")
    second, second_train, second_test = _build_corpus(tmp_path, "second")
    try:
        report = audit_lineage(first, repeated_tasks=second)
        assert report["passed"] is True
        assert report["errors"] == []
        assert report["split_counts"] == {"dev": 12, "test": 6, "train": 55}
        assert report["deterministic_generation"]["passed"] is True
        assert len({task.sample_id for task in first}) == len(first) == 73

        manifest = build_lineage_manifest(first)
        assert manifest["sample_count"] == 73
        assert len(manifest["manifest_hash"]) == 64
        assert all("query" not in sample for sample in manifest["samples"])
        assert all(sample["source"] == "edu_agent.synthetic" for sample in manifest["samples"])
    finally:
        first_train.close()
        first_test.close()
        second_train.close()
        second_test.close()


def test_test_split_oracle_only_validates_harness(tmp_path):
    database_path = tmp_path / "test.db"
    generate.build(
        seed=TEST_SEED,
        out_path=database_path,
        n_classes=TEST_N_CLASSES,
        courses_per_class=TEST_COURSES_PER_CLASS,
    )
    connection = db.connect(database_path)
    try:
        tasks = build_test_tasks(connection)
        assert {task.split for task in tasks} == {"test"}
        report = run_eval(tasks, make_oracle_engine, db_conn=connection)
        assert report["trajectory_success_rate"] == 1.0
        assert report["lineage"]["passed"] is True
    finally:
        connection.close()


def test_cross_split_duplicate_query_fails_gate(tmp_path):
    corpus, train_conn, test_conn = _build_corpus(tmp_path, "corpus")
    try:
        original = tasks_for_split(corpus, "test")[0]
        duplicate = replace(original, id="dev-renamed-duplicate", lineage=None)
        duplicate = _relineage(
            duplicate,
            reference=original,
            split="dev",
            family="dev.renamed_duplicate",
            semantic_group="dev.renamed_duplicate",
        )
        report = validate_lineage([original, duplicate], require_all_splits=False)
        assert report["passed"] is False
        assert any(error.startswith("cross_split_duplicate_query:") for error in report["errors"])
    finally:
        train_conn.close()
        test_conn.close()


def test_template_family_and_semantic_group_overlap_fail_gate(tmp_path):
    corpus, train_conn, test_conn = _build_corpus(tmp_path, "corpus")
    try:
        train_task = tasks_for_split(corpus, "train")[0]
        test_task = tasks_for_split(corpus, "test")[0]
        family_overlap = _relineage(
            test_task,
            family=train_task.intent_template_family,
            semantic_group="test.unique_semantics",
        )
        family_report = validate_lineage(
            [train_task, family_overlap], require_all_splits=False
        )
        assert any(
            error.startswith("template_family_overlap:") for error in family_report["errors"]
        )

        semantic_overlap = _relineage(
            test_task,
            family="test.unique_family",
            semantic_group=train_task.semantic_group,
        )
        semantic_report = validate_lineage(
            [train_task, semantic_overlap], require_all_splits=False
        )
        assert any(
            error.startswith("semantic_group_overlap:") for error in semantic_report["errors"]
        )
    finally:
        train_conn.close()
        test_conn.close()


def test_missing_sensitive_and_non_deterministic_provenance_fail_gate(tmp_path):
    corpus, train_conn, test_conn = _build_corpus(tmp_path, "corpus")
    try:
        task = corpus[0]
        missing = replace(task, lineage=None)
        missing_report = validate_lineage([missing], require_all_splits=False)
        assert any(error.startswith("missing_provenance:") for error in missing_report["errors"])

        required_fields = (
            "sample_id", "source", "version", "split", "intent_template_family",
            "semantic_group", "task_id", "content_hash", "query_hash", "seed", "generator",
            "deterministic",
        )
        for field in required_fields:
            incomplete_lineage = task.lineage.to_dict()
            del incomplete_lineage[field]
            incomplete = replace(task, lineage=incomplete_lineage)
            incomplete_report = validate_lineage([incomplete], require_all_splits=False)
            assert any(
                error.startswith("missing_provenance:") and field in error
                for error in incomplete_report["errors"]
            ), field

        sensitive_lineage = task.lineage.to_dict()
        sensitive_lineage["api_key"] = "canary-secret-value"
        sensitive = replace(task, lineage=sensitive_lineage)
        sensitive_report = validate_lineage([sensitive], require_all_splits=False)
        assert any(error.startswith("sensitive_provenance:") for error in sensitive_report["errors"])

        sensitive_text_task = replace(
            task,
            query="Use Bearer sk-lineage-query-canary-938247 to run this sample",
            lineage=None,
        )
        sensitive_text_task = _relineage(sensitive_text_task, reference=task)
        sensitive_text_report = validate_lineage(
            [sensitive_text_task], require_all_splits=False
        )
        assert any(
            error.startswith("sensitive_sample_text:")
            for error in sensitive_text_report["errors"]
        )

        unstable_lineage = task.lineage.to_dict()
        unstable_lineage["deterministic"] = False
        unstable = replace(task, lineage=unstable_lineage)
        unstable_report = validate_lineage([unstable], require_all_splits=False)
        assert any(
            error.startswith("non_deterministic_generation:")
            for error in unstable_report["errors"]
        )
    finally:
        train_conn.close()
        test_conn.close()


def test_repeat_mismatch_and_harness_missing_provenance_fail(tmp_path):
    corpus, train_conn, test_conn = _build_corpus(tmp_path, "corpus")
    try:
        changed = list(corpus)
        changed_task = replace(changed[0], query=changed[0].query + "（变体）", lineage=None)
        changed[0] = _relineage(changed_task, reference=changed[0])
        report = audit_lineage(corpus, repeated_tasks=changed)
        assert report["passed"] is False
        assert report["deterministic_generation"]["error"] == "non_deterministic_generation"

        invalid = replace(corpus[0], lineage=None)
        with pytest.raises(LineageValidationError):
            run_eval([invalid], make_oracle_engine)
    finally:
        train_conn.close()
        test_conn.close()


def test_saved_failure_trace_keeps_run_metadata_and_is_redacted(tmp_path, monkeypatch):
    corpus, train_conn, test_conn = _build_corpus(tmp_path, "corpus")
    canary = "sk-lineage-failure-canary-938247"
    monkeypatch.setenv("EDU_AGENT_API_KEY", canary)
    output = tmp_path / "private-output" / "model-eval.json"
    try:
        report = {
            "trajectory_success_rate": 0.0,
            "records": [{
                "id": "test-failure",
                "success": False,
                "error": f"Bearer {canary} at /Users/private-user/eval.log",
                "final_answer": None,
                "trajectory": [{"tool": "list_exams", "arguments": {"api_key": canary}}],
            }],
        }
        lineage_report = audit_lineage(corpus, repeated_tasks=corpus)
        artifact = _write_result(
            output,
            args=argparse.Namespace(
                split="test", repeats=1, engine="oracle", evidence_mode="development"
            ),
            reports=[report],
            model_name="oracle",
            model_mode="offline_oracle",
            model_config={},
            tasks=tasks_for_split(corpus, "test"),
            lineage_report=lineage_report,
            lineage_manifest=build_lineage_manifest(corpus),
            seed=TEST_SEED,
        )
        failure_path = output.with_name("model-eval.failed-trajectories.jsonl")
        failure = json.loads(failure_path.read_text(encoding="utf-8"))
        serialized = output.read_text(encoding="utf-8") + failure_path.read_text(encoding="utf-8")
        assert artifact["failed_trajectories"] == failure_path.name
        assert failure["config_hash"] == artifact["config_hash"]
        assert failure["repeat_index"] == 1
        assert failure["run_id"] == "offline_oracle-1"
        assert canary not in serialized
        assert "/Users/private-user" not in serialized
        assert audit_paths([output, failure_path])["findings"] == []
    finally:
        train_conn.close()
        test_conn.close()


def test_candidate_and_release_evidence_require_test_output():
    assert _evidence_request_error(argparse.Namespace(
        evidence_mode="candidate", split="dev", output="candidate.json"
    )) == "candidate/release model evidence requires --split test"
    assert _evidence_request_error(argparse.Namespace(
        evidence_mode="release", split="test", output=None
    )) == "candidate/release model evidence requires --output"
    assert _evidence_request_error(argparse.Namespace(
        evidence_mode="candidate", split="test", output="candidate.json"
    )) is None
    assert _evidence_request_error(argparse.Namespace(
        evidence_mode="development", split="dev", output=None
    )) is None
