"""Stable, split-aware provenance for the evaluation task corpus.

The evaluation corpus is intentionally small and synthetic.  That makes it
possible to be stricter than a typical benchmark loader: every sample carries
an immutable identity, source/version metadata, and an intent family.  Split
membership is a property of the family, never of an individual random row.

This module does not store query text in the lineage manifest.  A content hash
is enough to detect duplicates while keeping the manifest safe to publish.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from ..data_classification import contains_sensitive


LINEAGE_SCHEMA_VERSION = "edu-agent.eval-lineage.v1"
DATA_SOURCE = "edu_agent.synthetic"
DATA_VERSION = "seed-42.tasks-v2"
SPLITS = ("train", "dev", "test")

_PRIVATE_PATH = re.compile(r"(?<![A-Za-z0-9:])/(?:Users|home|private|tmp|var/folders)/[^\s\"'<>]*")
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")


class LineageValidationError(ValueError):
    """Raised when a dataset cannot be used as evaluation evidence."""

    def __init__(self, report: Mapping[str, Any]):
        self.report = dict(report)
        errors = self.report.get("errors") or ["lineage_validation_failed"]
        super().__init__("; ".join(str(item) for item in errors))


@dataclass(frozen=True)
class SampleLineage:
    """Public provenance attached to one evaluation sample."""

    sample_id: str
    source: str
    version: str
    split: str
    intent_template_family: str
    semantic_group: str
    task_id: str
    content_hash: str
    query_hash: str
    seed: int = 42
    generator: str = "edu_agent.eval.tasks.build_tasks"
    deterministic: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "source": self.source,
            "version": self.version,
            "split": self.split,
            "intent_template_family": self.intent_template_family,
            "semantic_group": self.semantic_group,
            "task_id": self.task_id,
            "content_hash": self.content_hash,
            "query_hash": self.query_hash,
            "seed": self.seed,
            "generator": self.generator,
            "deterministic": self.deterministic,
        }


def _jsonable(value: Any) -> Any:
    """Convert task declarations to a canonical, JSON-safe representation."""
    # ``ANY`` is an object sentinel.  Avoid importing tasks here to keep the
    # module independent of the task dataclasses.
    if type(value) is object:
        return {"$sentinel": "ANY"}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "value") and isinstance(value.value, (str, int, float, bool)):
        return value.value
    raise TypeError(f"unsupported non-deterministic lineage value: {type(value).__name__}")


def task_payload(task: Any) -> dict[str, Any]:
    """Return the declaration fields that define a sample's identity."""
    expected = []
    for call in getattr(task, "expected_tools", ()):
        expected.append({
            "tool": _jsonable(call.tool),
            "args": _jsonable(call.args),
            "send": _jsonable(call.send),
        })
    success = getattr(task, "success", None)
    success_payload = {
        "required_tools": _jsonable(getattr(success, "required_tools", [])),
        "ordered": bool(getattr(success, "ordered", True)),
        "answer_contains": _jsonable(getattr(success, "answer_contains", ())),
        "forbid_tools": bool(getattr(success, "forbid_tools", False)),
    }
    return {
        "id": str(getattr(task, "id", "")),
        "category": str(getattr(task, "category", "")),
        "query": str(getattr(task, "query", "")),
        "expected_tools": expected,
        "success": success_payload,
        "should_call_tool": bool(getattr(task, "should_call_tool", True)),
        "parallel": bool(getattr(task, "parallel", False)),
        "oracle": str(getattr(task, "oracle", "auto")),
        "notes": str(getattr(task, "notes", "")),
    }


def canonical_json(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(task: Any) -> str:
    return hashlib.sha256(canonical_json(task_payload(task)).encode("utf-8")).hexdigest()


def query_hash(task: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(getattr(task, "query", "")))
    normalized = " ".join(normalized.casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def stable_sample_id(
    task: Any,
    *,
    source: str = DATA_SOURCE,
    version: str = DATA_VERSION,
    intent_template_family: str,
) -> str:
    material = {
        "source": source,
        "version": version,
        "intent_template_family": intent_template_family,
        "task": task_payload(task),
    }
    digest = hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()
    return f"sample-{digest}"


def make_sample_lineage(
    task: Any,
    *,
    split: str,
    intent_template_family: str,
    semantic_group: str | None = None,
    source: str = DATA_SOURCE,
    version: str = DATA_VERSION,
    seed: int = 42,
    generator: str = "edu_agent.eval.tasks.build_tasks",
) -> SampleLineage:
    if split not in SPLITS:
        raise ValueError(f"unsupported evaluation split: {split}")
    family = str(intent_template_family).strip()
    if not family:
        raise ValueError("intent_template_family must not be empty")
    group = str(semantic_group or family).strip()
    return SampleLineage(
        sample_id=stable_sample_id(
            task,
            source=source,
            version=version,
            intent_template_family=family,
        ),
        source=source,
        version=version,
        split=split,
        intent_template_family=family,
        semantic_group=group,
        task_id=str(task.id),
        content_hash=content_hash(task),
        query_hash=query_hash(task),
        seed=seed,
        generator=generator,
    )


def _sensitive_text(value: Any) -> bool:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return bool(_PRIVATE_PATH.search(encoded) or _EMAIL.search(encoded) or _PHONE.search(encoded))


def _lineage_dict(task: Any) -> dict[str, Any] | None:
    value = getattr(task, "lineage", None)
    if isinstance(value, SampleLineage):
        return value.to_dict()
    if isinstance(value, Mapping):
        return dict(value)
    return None


def _manifest_samples(tasks: Iterable[Any]) -> list[dict[str, Any]]:
    samples = []
    for task in tasks:
        lineage = _lineage_dict(task)
        if lineage is None:
            continue
        # Keep manifests safe to publish: identity and hashes, not query text.
        samples.append({
            key: lineage[key]
            for key in (
                "sample_id", "source", "version", "split", "intent_template_family",
                "semantic_group", "task_id", "content_hash", "seed", "generator", "deterministic",
                "query_hash",
            )
            if key in lineage
        })
    return sorted(samples, key=lambda item: (str(item.get("split")), str(item.get("sample_id"))))


def manifest_hash(tasks: Iterable[Any]) -> str:
    samples = _manifest_samples(tasks)
    return hashlib.sha256(canonical_json(samples).encode("utf-8")).hexdigest()


def build_lineage_manifest(tasks: Iterable[Any]) -> dict[str, Any]:
    """Build a redaction-safe manifest suitable for an artifact."""
    task_list = list(tasks)
    samples = _manifest_samples(task_list)
    split_counts = Counter(str(item.get("split")) for item in samples)
    family_splits: dict[str, list[str]] = defaultdict(list)
    for item in samples:
        family_splits[str(item.get("intent_template_family"))].append(str(item.get("split")))
    return {
        "schema_version": LINEAGE_SCHEMA_VERSION,
        "sources": sorted({str(item.get("source")) for item in samples}),
        "versions": sorted({str(item.get("version")) for item in samples}),
        "sample_count": len(samples),
        "split_counts": dict(sorted(split_counts.items())),
        "template_families": {
            family: sorted(set(splits)) for family, splits in sorted(family_splits.items())
        },
        "manifest_hash": hashlib.sha256(canonical_json(samples).encode("utf-8")).hexdigest(),
        "samples": samples,
    }


def validate_lineage(
    tasks: Iterable[Any],
    *,
    require_all_splits: bool = True,
    expected_source: str = DATA_SOURCE,
    expected_version: str | None = None,
) -> dict[str, Any]:
    """Run all leakage/provenance/sensitivity checks and return a gate report."""
    task_list = list(tasks)
    errors: list[str] = []
    samples: list[tuple[Any, dict[str, Any]]] = []
    seen_ids: dict[str, list[tuple[str, str]]] = defaultdict(list)
    seen_content: dict[str, list[tuple[str, str]]] = defaultdict(list)
    seen_queries: dict[str, list[tuple[str, str]]] = defaultdict(list)
    family_splits: dict[str, set[str]] = defaultdict(set)
    semantic_splits: dict[str, set[str]] = defaultdict(set)

    for index, task in enumerate(task_list):
        lineage = _lineage_dict(task)
        prefix = f"task[{index}]"
        if lineage is None:
            errors.append(f"missing_provenance:{prefix}")
            continue
        required = (
            "sample_id", "source", "version", "split", "intent_template_family",
            "semantic_group", "task_id", "content_hash", "query_hash", "seed", "generator",
            "deterministic",
        )
        missing = [key for key in required if key not in lineage or lineage[key] in (None, "")]
        if missing:
            errors.append(f"missing_provenance:{lineage.get('task_id', prefix)}:{','.join(missing)}")
        split = str(lineage.get("split", ""))
        if split not in SPLITS:
            errors.append(f"invalid_split:{lineage.get('task_id', prefix)}:{split}")
        if expected_source and lineage.get("source") != expected_source:
            errors.append(f"unexpected_source:{lineage.get('task_id', prefix)}")
        if lineage.get("version") and expected_version and lineage.get("version") != expected_version:
            errors.append(f"unexpected_version:{lineage.get('task_id', prefix)}")
        if lineage.get("task_id") != getattr(task, "id", None):
            errors.append(f"task_id_mismatch:{lineage.get('task_id', prefix)}")
        try:
            expected_content = content_hash(task)
            expected_query = query_hash(task)
            expected_sample = stable_sample_id(
                task,
                source=str(lineage.get("source", expected_source)),
                version=str(lineage.get("version", expected_version or DATA_VERSION)),
                intent_template_family=str(lineage.get("intent_template_family", "")),
            )
            if lineage.get("content_hash") != expected_content:
                errors.append(f"content_hash_mismatch:{lineage.get('task_id', prefix)}")
            if lineage.get("query_hash") != expected_query:
                errors.append(f"query_hash_mismatch:{lineage.get('task_id', prefix)}")
            if lineage.get("sample_id") != expected_sample:
                errors.append(f"sample_id_mismatch:{lineage.get('task_id', prefix)}")
        except (TypeError, ValueError) as exc:
            errors.append(f"non_deterministic_generation:{lineage.get('task_id', prefix)}:{exc}")
        if lineage.get("deterministic") is not True:
            errors.append(f"non_deterministic_generation:{lineage.get('task_id', prefix)}")
        auditable_lineage = {
            key: value
            for key, value in lineage.items()
            if key not in {"content_hash", "query_hash", "sample_id"}
        }
        if (
            contains_sensitive(auditable_lineage, include_pii=True)
            or _sensitive_text(auditable_lineage)
        ):
            errors.append(f"sensitive_provenance:{lineage.get('task_id', prefix)}")
        sample_text = {
            "query": getattr(task, "query", ""),
            "notes": getattr(task, "notes", ""),
        }
        if contains_sensitive(sample_text, include_pii=True) or _sensitive_text(sample_text):
            errors.append(f"sensitive_sample_text:{getattr(task, 'id', prefix)}")

        task_id = str(getattr(task, "id", lineage.get("task_id", prefix)))
        sample_id = str(lineage.get("sample_id", ""))
        content = str(lineage.get("content_hash", ""))
        query = str(lineage.get("query_hash", ""))
        seen_ids[sample_id].append((split, task_id))
        seen_content[content].append((split, task_id))
        seen_queries[query].append((split, task_id))
        family = str(lineage.get("intent_template_family", ""))
        semantic = str(lineage.get("semantic_group", ""))
        family_splits[family].add(split)
        semantic_splits[semantic].add(split)
        samples.append((task, lineage))

    for sample_id, occurrences in seen_ids.items():
        if not sample_id:
            continue
        if len(occurrences) > 1:
            splits = {split for split, _ in occurrences}
            code = "cross_split_duplicate" if len(splits) > 1 else "duplicate_sample_id"
            errors.append(f"{code}:{sample_id}")
    for digest, occurrences in seen_content.items():
        if not digest:
            continue
        splits = {split for split, _ in occurrences}
        if len(splits) > 1:
            errors.append(f"cross_split_duplicate:{digest}")
    for digest, occurrences in seen_queries.items():
        if not digest:
            continue
        splits = {split for split, _ in occurrences}
        if len(splits) > 1:
            errors.append(f"cross_split_duplicate_query:{digest}")
    for family, splits in family_splits.items():
        if len(splits) > 1:
            errors.append(f"template_family_overlap:{family}")
    for semantic, splits in semantic_splits.items():
        if len(semantic) > 0 and len(splits) > 1:
            errors.append(f"semantic_group_overlap:{semantic}")

    present_splits = {str(item.get("split")) for _, item in samples}
    if require_all_splits:
        for split in SPLITS:
            if split not in present_splits:
                errors.append(f"missing_split:{split}")

    # Stable ordering makes the report itself reproducible and easy to diff.
    errors = sorted(set(errors))
    manifest = build_lineage_manifest(task_list)
    checks = {
        "stable_ids": not any(
            marker in error
            for error in errors
            for marker in ("sample_id_mismatch", "content_hash_mismatch", "query_hash_mismatch")
        ),
        "cross_split_duplicates": not any("cross_split_duplicate" in error for error in errors),
        "template_family_isolation": not any("template_family_overlap" in error for error in errors),
        "semantic_group_isolation": not any("semantic_group_overlap" in error for error in errors),
        "complete_provenance": not any("missing_provenance" in error for error in errors),
        "sensitive_fields": not any("sensitive_" in error for error in errors),
        "deterministic_declarations": not any("non_deterministic_generation" in error for error in errors),
        "all_splits_present": not any("missing_split:" in error for error in errors),
    }
    return {
        "schema_version": LINEAGE_SCHEMA_VERSION,
        "passed": not errors,
        "errors": errors,
        "checks": checks,
        "sample_count": len(task_list),
        "split_counts": manifest["split_counts"],
        "template_families": manifest["template_families"],
        "manifest_hash": manifest["manifest_hash"],
    }


def lineage_gate_passed(report: Mapping[str, Any]) -> bool:
    return bool(report.get("passed")) and not report.get("errors")


def deterministic_generation_check(build: Callable[[], Iterable[Any]]) -> dict[str, Any]:
    """Run a task generator twice and compare canonical task declarations."""
    try:
        first = list(build())
        second = list(build())
        first_payload = [
            {"task": task_payload(task), "lineage": _lineage_dict(task)}
            for task in sorted(first, key=lambda item: str(item.id))
        ]
        second_payload = [
            {"task": task_payload(task), "lineage": _lineage_dict(task)}
            for task in sorted(second, key=lambda item: str(item.id))
        ]
        first_hash = hashlib.sha256(canonical_json(first_payload).encode("utf-8")).hexdigest()
        second_hash = hashlib.sha256(canonical_json(second_payload).encode("utf-8")).hexdigest()
        return {
            "passed": first_hash == second_hash,
            "first_hash": first_hash,
            "second_hash": second_hash,
            "sample_count": len(first),
            "error": None if first_hash == second_hash else "non_deterministic_generation",
        }
    except Exception as exc:  # noqa: BLE001 - the audit must report generator failures
        return {
            "passed": False,
            "first_hash": None,
            "second_hash": None,
            "sample_count": 0,
            "error": f"non_deterministic_generation:{type(exc).__name__}",
        }


def audit_lineage(
    tasks: Iterable[Any],
    *,
    repeated_tasks: Iterable[Any] | None = None,
    require_all_splits: bool = True,
) -> dict[str, Any]:
    """Combine declaration, leakage, and optional repeat determinism checks."""
    task_list = list(tasks)
    report = validate_lineage(task_list, require_all_splits=require_all_splits)
    if repeated_tasks is not None:
        first_hash = hashlib.sha256(
            canonical_json([
                {"task": task_payload(task), "lineage": _lineage_dict(task)}
                for task in sorted(task_list, key=lambda item: str(item.id))
            ]).encode("utf-8")
        ).hexdigest()
        repeated = list(repeated_tasks)
        second_hash = hashlib.sha256(
            canonical_json([
                {"task": task_payload(task), "lineage": _lineage_dict(task)}
                for task in sorted(repeated, key=lambda item: str(item.id))
            ]).encode("utf-8")
        ).hexdigest()
        deterministic = {
            "passed": first_hash == second_hash,
            "first_hash": first_hash,
            "second_hash": second_hash,
            "sample_count": len(repeated),
            "error": None if first_hash == second_hash else "non_deterministic_generation",
        }
        report["deterministic_generation"] = deterministic
        if not deterministic["passed"]:
            report["errors"] = sorted(set(report["errors"] + [deterministic["error"]]))
            report["passed"] = False
            report["checks"]["deterministic_generation"] = False
        else:
            report["checks"]["deterministic_generation"] = True
    else:
        report["deterministic_generation"] = {
            "passed": all(item.get("deterministic") is True for item in _manifest_samples(task_list)),
            "first_hash": None,
            "second_hash": None,
            "sample_count": len(task_list),
            "error": None,
        }
    return report


__all__ = [
    "DATA_SOURCE", "DATA_VERSION", "LINEAGE_SCHEMA_VERSION", "LineageValidationError",
    "SPLITS", "SampleLineage", "audit_lineage", "build_lineage_manifest", "canonical_json",
    "content_hash", "deterministic_generation_check", "lineage_gate_passed", "make_sample_lineage",
    "query_hash",
    "manifest_hash", "stable_sample_id", "task_payload", "validate_lineage",
]
