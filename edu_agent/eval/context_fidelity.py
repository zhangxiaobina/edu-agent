"""Deterministic corpus and metrics for context-compaction fidelity.

The corpus deliberately describes *what must survive* rather than prescribing
one summary string.  This keeps offline acceptance independent of a language
model while making scope leakage, trigger churn, and estimator drift measurable
across generated cases.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..data_classification import contains_sensitive


_SCHEMA = "edu-agent.context-fidelity.v1"
_SPLITS = ("train", "dev", "test")
_SOURCE = "synthetic-context-fidelity"
_VERSION = "r4.3.v1"
_GENERATOR = "build_context_fidelity_corpus"
_LINEAGE_FIELDS = (
    "schema",
    "sample_id",
    "source",
    "version",
    "seed",
    "generator",
    "split",
    "intent_template_family",
    "semantic_group",
    "content_hash",
    "query_hash",
    "deterministic",
)


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _stable_id(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True)
class ContextFidelityCase:
    """One scope-isolated fidelity sample with stable lineage."""

    case_id: str
    split: str
    intent_template_family: str
    scope: dict[str, Any]
    key_constraints: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()
    operation_ids: tuple[str, ...] = ()
    approval_states: tuple[str, ...] = ()
    citation_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    lineage_schema: str = _SCHEMA

    def __post_init__(self) -> None:
        if self.split not in _SPLITS:
            raise ValueError(f"unsupported fidelity split: {self.split}")
        if not self.intent_template_family.strip():
            raise ValueError("fidelity intent template family is required")
        expected = _stable_id(
            {
                "split": self.split,
                "family": self.intent_template_family,
                "scope": self.scope,
                "constraints": self.key_constraints,
                "entities": self.entities,
                "operations": self.operation_ids,
                "approvals": self.approval_states,
                "citations": self.citation_ids,
                "artifacts": self.artifact_ids,
            }
        )
        if self.case_id != expected:
            raise ValueError("fidelity case_id is not stable for its lineage payload")

    @property
    def lineage(self) -> dict[str, Any]:
        content_hash = hashlib.sha256(
            _canonical(
                {
                    "scope": self.scope,
                    "constraints": self.key_constraints,
                    "entities": self.entities,
                    "operations": self.operation_ids,
                    "approvals": self.approval_states,
                    "citations": self.citation_ids,
                    "artifacts": self.artifact_ids,
                }
            ).encode("utf-8")
        ).hexdigest()
        return {
            "schema": self.lineage_schema,
            "sample_id": self.case_id,
            "source": _SOURCE,
            "version": _VERSION,
            "seed": 314 if self.split == "test" else 42,
            "generator": _GENERATOR,
            "split": self.split,
            "intent_template_family": self.intent_template_family,
            "semantic_group": f"context:{self.intent_template_family}",
            "content_hash": content_hash,
            "query_hash": hashlib.sha256(
                self.intent_template_family.encode("utf-8")
            ).hexdigest(),
            "deterministic": True,
        }

    def to_dict(self) -> dict[str, Any]:
        record = asdict(self)
        record["lineage"] = self.lineage
        return record


@dataclass(frozen=True)
class ContextFidelityObservation:
    """Production-generated measurements for one fidelity corpus case."""

    summary: str
    before_tokens: int
    after_tokens: int
    trigger_count: int
    duplicate_trigger_count: int
    estimated_input_tokens: int
    actual_input_tokens: int

    def __post_init__(self) -> None:
        numeric = (
            self.before_tokens,
            self.after_tokens,
            self.trigger_count,
            self.duplicate_trigger_count,
            self.estimated_input_tokens,
            self.actual_input_tokens,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in numeric):
            raise ValueError("fidelity observation fields must be non-negative integers")
        if self.duplicate_trigger_count > self.trigger_count:
            raise ValueError("duplicate trigger count cannot exceed trigger count")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ContextFidelityMetrics:
    n_cases: int
    constraint_fidelity: float | None
    entity_fidelity: float | None
    operation_fidelity: float | None
    approval_fidelity: float | None
    citation_fidelity: float | None
    artifact_fidelity: float | None
    scope_leak_rate: float | None
    compression_ratio: float | None
    duplicate_trigger_rate: float | None
    estimate_absolute_error: float | None
    cases: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        # Stable aliases make reports readable while retaining the explicit
        # field names used by the acceptance contract.
        result.update(
            {
                "key_constraint_fidelity": self.constraint_fidelity,
                "scope_leak": self.scope_leak_rate,
                "compression_rate": (
                    1 - self.compression_ratio
                    if self.compression_ratio is not None
                    else None
                ),
                "repeated_trigger_rate": self.duplicate_trigger_rate,
                "estimation_error": self.estimate_absolute_error,
            }
        )
        return result


def _case(
    split: str,
    family: str,
    scope: dict[str, Any],
    *,
    constraints: Iterable[str] = (),
    entities: Iterable[str] = (),
    operations: Iterable[str] = (),
    approvals: Iterable[str] = (),
    citations: Iterable[str] = (),
    artifacts: Iterable[str] = (),
) -> ContextFidelityCase:
    values = {
        "split": split,
        "family": family,
        "scope": scope,
        "constraints": tuple(constraints),
        "entities": tuple(entities),
        "operations": tuple(operations),
        "approvals": tuple(approvals),
        "citations": tuple(citations),
        "artifacts": tuple(artifacts),
    }
    return ContextFidelityCase(
        case_id=_stable_id(values),
        split=split,
        intent_template_family=family,
        scope=scope,
        key_constraints=values["constraints"],
        entities=values["entities"],
        operation_ids=values["operations"],
        approval_states=values["approvals"],
        citation_ids=values["citations"],
        artifact_ids=values["artifacts"],
    )


def build_context_fidelity_corpus() -> list[ContextFidelityCase]:
    """Build a stable, scope-isolated corpus without external data."""

    cases: list[ContextFidelityCase] = []
    for index in range(6):
        cases.append(
            _case(
                "train",
                f"constraint-course-{index}",
                {
                    "tenant_id": f"tenant-train-{index}",
                    "actor_id": f"teacher-train-{index}",
                    "session_id": f"session-train-{index}",
                    "course_ids": [index + 1],
                },
                constraints=(f"必须只使用课程 {index + 1}", "不得跨租户"),
                entities=(f"course_id={index + 1}", f"class_id={10 + index}"),
                operations=(f"op-train-{index}",),
                approvals=(f"approval-train-{index}", "APPROVAL_REQUIRED"),
                citations=(f"citation:train:{index}",),
                artifacts=(f"artifact-train-{index}",),
            )
        )
    for index in range(3):
        cases.append(
            _case(
                "dev",
                f"operation-status-{index}",
                {
                    "tenant_id": f"tenant-dev-{index}",
                    "actor_id": f"teacher-dev-{index}",
                    "session_id": f"session-dev-{index}",
                    "course_ids": [20 + index],
                },
                constraints=("审批状态必须保留",),
                entities=(f"exam_id={30 + index}",),
                operations=(f"op-dev-{index}",),
                approvals=(f"approval-dev-{index}", "APPROVAL_DENIED"),
                citations=(f"citation:dev:{index}",),
            )
        )
    for index in range(3):
        cases.append(
            _case(
                "test",
                f"citation-artifact-{index}",
                {
                    "tenant_id": f"tenant-test-{index}",
                    "actor_id": f"teacher-test-{index}",
                    "session_id": f"session-test-{index}",
                    "course_ids": [40 + index],
                },
                constraints=("只允许读取当前班级",),
                entities=(f"course_id={40 + index}", f"student_id={100 + index}"),
                approvals=(f"approval-test-{index}", "MANUAL_REVIEW_REQUIRED"),
                citations=(f"citation:test:{index}",),
                artifacts=(f"artifact-test-{index}",),
            )
        )
    return cases


def _fidelity_record(case: ContextFidelityCase | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(case, ContextFidelityCase):
        return case.to_dict()
    if isinstance(case, Mapping):
        return dict(case)
    raise TypeError(f"unsupported context fidelity case: {type(case).__name__}")


def _fidelity_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "split": record.get("split"),
        "family": record.get("intent_template_family"),
        "scope": record.get("scope"),
        "constraints": tuple(record.get("key_constraints") or ()),
        "entities": tuple(record.get("entities") or ()),
        "operations": tuple(record.get("operation_ids") or ()),
        "approvals": tuple(record.get("approval_states") or ()),
        "citations": tuple(record.get("citation_ids") or ()),
        "artifacts": tuple(record.get("artifact_ids") or ()),
    }


def _fidelity_content_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    payload = _fidelity_payload(record)
    return {
        "scope": payload["scope"],
        "constraints": payload["constraints"],
        "entities": payload["entities"],
        "operations": payload["operations"],
        "approvals": payload["approvals"],
        "citations": payload["citations"],
        "artifacts": payload["artifacts"],
    }


def _fidelity_records_hash(records: Iterable[Mapping[str, Any]]) -> str:
    ordered = sorted(records, key=lambda item: str(item.get("case_id", "")))
    return hashlib.sha256(_canonical(ordered).encode("utf-8")).hexdigest()


def build_context_fidelity_manifest(
    cases: Iterable[ContextFidelityCase | Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a publishable lineage-only manifest without case free text."""

    samples: list[dict[str, Any]] = []
    for case in cases:
        record = _fidelity_record(case)
        lineage = record.get("lineage")
        if not isinstance(lineage, Mapping):
            continue
        samples.append({key: lineage[key] for key in _LINEAGE_FIELDS if key in lineage})
    samples.sort(key=lambda item: (str(item.get("split", "")), str(item.get("sample_id", ""))))
    split_counts = {
        split: sum(item.get("split") == split for item in samples)
        for split in _SPLITS
        if any(item.get("split") == split for item in samples)
    }
    return {
        "schema": _SCHEMA,
        "sample_count": len(samples),
        "split_counts": split_counts,
        "manifest_hash": hashlib.sha256(_canonical(samples).encode("utf-8")).hexdigest(),
        "samples": samples,
    }


def validate_context_fidelity_corpus(
    cases: Iterable[ContextFidelityCase | Mapping[str, Any]],
    *,
    repeated_cases: Iterable[ContextFidelityCase | Mapping[str, Any]] | None = None,
    require_all_splits: bool = True,
) -> dict[str, Any]:
    """Audit fidelity lineage, split isolation, sensitivity, and determinism."""

    records = [_fidelity_record(case) for case in cases]
    seen_ids: dict[str, list[str]] = {}
    seen_content: dict[str, set[str]] = {}
    seen_queries: dict[str, set[str]] = {}
    seen_families: dict[str, set[str]] = {}
    seen_semantics: dict[str, set[str]] = {}
    seen_scopes: dict[str, set[str]] = {}
    errors: list[str] = []

    for index, record in enumerate(records):
        case_id = str(record.get("case_id") or f"case[{index}]")
        split = str(record.get("split") or "")
        family = str(record.get("intent_template_family") or "")
        lineage_value = record.get("lineage")
        if not isinstance(lineage_value, Mapping):
            errors.append(f"missing_provenance:{case_id}:lineage")
            lineage: dict[str, Any] = {}
        else:
            lineage = dict(lineage_value)
            missing = [
                key for key in _LINEAGE_FIELDS if key not in lineage or lineage[key] in (None, "")
            ]
            if missing:
                errors.append(f"missing_provenance:{case_id}:{','.join(missing)}")

        if split not in _SPLITS:
            errors.append(f"invalid_split:{case_id}:{split}")
        if lineage.get("schema") != _SCHEMA:
            errors.append(f"invalid_lineage_schema:{case_id}")
        if lineage.get("source") != _SOURCE:
            errors.append(f"unexpected_source:{case_id}")
        if lineage.get("version") != _VERSION:
            errors.append(f"unexpected_version:{case_id}")
        if lineage.get("generator") != _GENERATOR:
            errors.append(f"unexpected_generator:{case_id}")
        if lineage.get("split") != split:
            errors.append(f"lineage_split_mismatch:{case_id}")
        if lineage.get("intent_template_family") != family:
            errors.append(f"lineage_family_mismatch:{case_id}")
        if lineage.get("deterministic") is not True:
            errors.append(f"non_deterministic_generation:{case_id}")

        try:
            expected_id = _stable_id(_fidelity_payload(record))
            expected_content = hashlib.sha256(
                _canonical(_fidelity_content_payload(record)).encode("utf-8")
            ).hexdigest()
            expected_query = hashlib.sha256(family.encode("utf-8")).hexdigest()
            if record.get("case_id") != expected_id or lineage.get("sample_id") != expected_id:
                errors.append(f"sample_id_mismatch:{case_id}")
            if lineage.get("content_hash") != expected_content:
                errors.append(f"content_hash_mismatch:{case_id}")
            if lineage.get("query_hash") != expected_query:
                errors.append(f"query_hash_mismatch:{case_id}")
        except (TypeError, ValueError) as exc:
            errors.append(f"non_deterministic_generation:{case_id}:{type(exc).__name__}")

        scope = record.get("scope")
        if not isinstance(scope, Mapping):
            errors.append(f"missing_scope:{case_id}")
            scope_key = ""
        else:
            required_scope = ("tenant_id", "actor_id", "session_id", "course_ids")
            missing_scope = [key for key in required_scope if scope.get(key) in (None, "", [])]
            if missing_scope:
                errors.append(f"missing_scope:{case_id}:{','.join(missing_scope)}")
            scope_key = _canonical({key: scope.get(key) for key in required_scope})

        auditable_lineage = {
            key: value
            for key, value in lineage.items()
            if key not in {"sample_id", "content_hash", "query_hash"}
        }
        auditable_case = {
            key: value
            for key, value in record.items()
            if key not in {"case_id", "lineage", "lineage_schema"}
        }
        if contains_sensitive(
            {"case": auditable_case, "lineage": auditable_lineage},
            include_pii=True,
        ):
            errors.append(f"sensitive_fidelity_case:{case_id}")

        sample_id = str(lineage.get("sample_id") or record.get("case_id") or "")
        seen_ids.setdefault(sample_id, []).append(split)
        seen_content.setdefault(str(lineage.get("content_hash") or ""), set()).add(split)
        seen_queries.setdefault(str(lineage.get("query_hash") or ""), set()).add(split)
        seen_families.setdefault(family, set()).add(split)
        seen_semantics.setdefault(str(lineage.get("semantic_group") or ""), set()).add(split)
        if scope_key:
            seen_scopes.setdefault(scope_key, set()).add(split)

    for sample_id, splits in seen_ids.items():
        if sample_id and len(splits) > 1:
            code = "cross_split_duplicate" if len(set(splits)) > 1 else "duplicate_sample_id"
            errors.append(f"{code}:{sample_id}")
    for digest, splits in seen_content.items():
        if digest and len(splits) > 1:
            errors.append(f"cross_split_duplicate_content:{digest}")
    for digest, splits in seen_queries.items():
        if digest and len(splits) > 1:
            errors.append(f"cross_split_duplicate_query:{digest}")
    for family_name, splits in seen_families.items():
        if family_name and len(splits) > 1:
            errors.append(f"template_family_overlap:{family_name}")
    for semantic_group, splits in seen_semantics.items():
        if semantic_group and len(splits) > 1:
            errors.append(f"semantic_group_overlap:{semantic_group}")
    for scope_key, splits in seen_scopes.items():
        if len(splits) > 1:
            errors.append(f"scope_overlap:{hashlib.sha256(scope_key.encode()).hexdigest()}")

    present_splits = {str(record.get("split") or "") for record in records}
    if require_all_splits:
        for split in _SPLITS:
            if split not in present_splits:
                errors.append(f"missing_split:{split}")

    first_hash = _fidelity_records_hash(records)
    if repeated_cases is None:
        deterministic = {
            "passed": all(
                isinstance(record.get("lineage"), Mapping)
                and record["lineage"].get("deterministic") is True
                for record in records
            ),
            "first_hash": first_hash,
            "second_hash": None,
            "sample_count": len(records),
            "error": None,
        }
    else:
        repeated_records = [_fidelity_record(case) for case in repeated_cases]
        second_hash = _fidelity_records_hash(repeated_records)
        repeated = first_hash == second_hash
        deterministic = {
            "passed": repeated,
            "first_hash": first_hash,
            "second_hash": second_hash,
            "sample_count": len(repeated_records),
            "error": None if repeated else "non_deterministic_generation",
        }
        if not repeated:
            errors.append("non_deterministic_generation")

    errors = sorted(set(errors))
    manifest = build_context_fidelity_manifest(records)
    checks = {
        "stable_ids": not any(
            marker in error
            for error in errors
            for marker in ("sample_id_mismatch", "content_hash_mismatch", "query_hash_mismatch")
        ),
        "cross_split_duplicates": not any("cross_split_duplicate" in error for error in errors),
        "template_family_isolation": not any("template_family_overlap" in error for error in errors),
        "semantic_group_isolation": not any("semantic_group_overlap" in error for error in errors),
        "scope_isolation": not any("scope_overlap" in error for error in errors),
        "complete_provenance": not any("missing_provenance" in error for error in errors),
        "sensitive_fields": not any("sensitive_fidelity_case" in error for error in errors),
        "deterministic_generation": deterministic["passed"],
        "all_splits_present": not any("missing_split:" in error for error in errors),
    }
    return {
        "schema": _SCHEMA,
        "passed": not errors,
        "n_cases": len(records),
        "sample_count": len(records),
        "split_counts": manifest["split_counts"],
        "manifest_hash": manifest["manifest_hash"],
        "errors": errors,
        "checks": checks,
        "deterministic_generation": deterministic,
        "manifest": manifest,
    }


def _contains_all(text: str, values: Iterable[str]) -> tuple[int, int]:
    values = tuple(str(value) for value in values)
    return sum(value in text for value in values), len(values)


def _rate(matched: int, total: int) -> float | None:
    return matched / total if total else 1.0


def _render(summary: Any) -> str:
    if isinstance(summary, str):
        return summary
    return _canonical(summary)


def _case_messages(case: ContextFidelityCase) -> list[dict[str, Any]]:
    facts = " ".join(
        (
            *case.key_constraints,
            *case.entities,
            *case.operation_ids,
            *case.approval_states,
            *case.citation_ids,
            *case.artifact_ids,
        )
    )
    archive_tail = (f" historical-note-{case.case_id} " * 120).strip()
    return [
        {
            "role": "user",
            "content": f"{facts}\n{archive_tail}",
        },
        {
            "role": "assistant",
            "content": _canonical(
                {
                    "operations": list(case.operation_ids),
                    "approvals": list(case.approval_states),
                    "citations": list(case.citation_ids),
                    "artifacts": list(case.artifact_ids),
                    "historical_result": archive_tail,
                }
            ),
        },
    ]


def _production_summary(case: ContextFidelityCase) -> tuple[str, list[dict[str, Any]]]:
    """Run the production deterministic summarizer for one offline case."""

    from ..runtime.context_engine import CheckpointContextEngine

    messages = _case_messages(case)
    engine = CheckpointContextEngine(
        object(),
        token_budget=16_000,
        summary_max_chars=2_000,
    )
    summary = engine._summarize(
        messages,
        scope=case.scope,
        constraints=list(case.key_constraints),
        entities=list(case.entities),
        approvals=[{"state": value} for value in case.approval_states],
        artifact_refs=[{"artifact_id": value} for value in case.artifact_ids],
        citation_refs=list(case.citation_ids),
        operation_refs=[{"operation_id": value} for value in case.operation_ids],
        unfinished_plans=[],
    )
    return summary, messages


def render_context_fidelity_summary(case: ContextFidelityCase) -> str:
    """Return the production deterministic summary for one offline case."""

    return _production_summary(case)[0]


def _observe_trigger_policy(case: ContextFidelityCase) -> tuple[int, int]:
    """Measure restart/jitter behavior through the production checkpoint policy."""

    from ..runtime.context_engine import CheckpointContextEngine
    from ..state import StateStore

    with tempfile.TemporaryDirectory(prefix="edu-agent-context-fidelity-") as temp_dir:
        store = StateStore(Path(temp_dir) / "state.db")
        session_id = f"policy-{case.case_id}"
        store.ensure_session(
            session_id,
            actor_id=str(case.scope.get("actor_id") or "fidelity-actor"),
            tenant_id=str(case.scope.get("tenant_id") or "fidelity-tenant"),
            role="teacher",
            course_ids={
                int(value)
                for value in case.scope.get("course_ids", [])
                if isinstance(value, int) and not isinstance(value, bool)
            },
        )
        store.append_messages(
            session_id,
            [
                {"role": "user", "content": "old question " + "x" * 1_200},
                {"role": "assistant", "content": "old answer " + "y" * 1_200},
                {"role": "user", "content": "recent question " + "z" * 1_200},
                {"role": "assistant", "content": "recent answer " + "q" * 1_200},
            ],
        )
        policy_args = {
            "token_budget": 512,
            "trigger_ratio": 0.5,
            "release_ratio": 0.25,
            "keep_recent": 2,
            "summary_max_chars": 512,
            "cooldown_turns": 1,
        }
        first_engine = CheckpointContextEngine(store, **policy_args)
        first = first_engine.compact_if_needed(
            session_id,
            store.get_messages(session_id),
            context=None,
        )
        if first.decision != "compacted":
            raise AssertionError(
                f"fidelity policy fixture did not compact: {case.case_id}={first.decision}"
            )
        # Chatter grows the request above trigger without completing a new user
        # turn.  A restarted engine must hold instead of micro-compacting again.
        store.append_messages(
            session_id,
            [
                {"role": "assistant", "content": "chatter-a " + "a" * 1_200},
                {"role": "assistant", "content": "chatter-b " + "b" * 1_200},
            ],
        )
        restarted = CheckpointContextEngine(store, **policy_args)
        second = restarted.compact_if_needed(
            session_id,
            store.get_messages(session_id),
            context=None,
        )
        decisions = (first, second)
        trigger_count = sum(
            result.trigger_threshold is not None
            and result.estimated_tokens_before >= result.trigger_threshold
            for result in decisions
        )
        duplicate_count = int(second.decision == "compacted")
        return trigger_count, duplicate_count


def _observe_estimator(
    case: ContextFidelityCase,
    messages: list[dict[str, Any]],
    summary: str,
) -> tuple[int, int]:
    """Compare the production fallback estimator with a stable reference counter."""

    from ..runtime.context import ContextAccountant, ContextRouteLimits
    from ..tokenization import TokenizerRegistry

    current = f"continue scoped work for {case.case_id}"
    injection = f"<context_checkpoint>\n{summary}\n</context_checkpoint>"
    current_wire = f"{injection}\n\n{current}"
    wire_messages = [
        {"role": "system", "content": "context fidelity reference"},
        *messages,
        {"role": "user", "content": current_wire},
    ]
    route = ContextRouteLimits(
        "offline-fidelity",
        "offline-production-estimator",
        context_window_tokens=100_000,
        max_output_tokens=1_000,
    )
    estimated = ContextAccountant().breakdown(
        messages=wire_messages,
        tools=[],
        route=route,
        configured_context_limit_tokens=100_000,
        max_output_reserve_tokens=0,
        current_user_turn=current,
        current_user_wire_content=current_wire,
        memory_checkpoint_injection=injection,
    ).estimated_input_tokens

    registry = TokenizerRegistry()
    registry.register(
        "context-fidelity-reference",
        lambda text: math.ceil(len(text.encode("utf-8")) / 3),
        version="utf8-bytes-div3@r4.3.v1",
    )
    actual = ContextAccountant(
        tokenizer_registry=registry,
        estimator_safety_factor=1,
        tokenizer_safety_factor=1,
        calibration_margin=1,
    ).breakdown(
        messages=wire_messages,
        tools=[],
        route=ContextRouteLimits(
            "offline-fidelity",
            "offline-reference-counter",
            tokenizer="context-fidelity-reference",
            context_window_tokens=100_000,
            max_output_tokens=1_000,
        ),
        configured_context_limit_tokens=100_000,
        max_output_reserve_tokens=0,
        current_user_turn=current,
        current_user_wire_content=current_wire,
        memory_checkpoint_injection=injection,
    ).estimated_input_tokens
    return estimated, actual


def observe_context_fidelity_case(case: ContextFidelityCase) -> ContextFidelityObservation:
    """Collect all offline metrics from production code paths."""

    from ..runtime.context_engine import _legacy_compaction_estimate

    summary, messages = _production_summary(case)
    before_tokens = sum(_legacy_compaction_estimate(message) for message in messages)
    after_tokens = _legacy_compaction_estimate({"role": "system", "content": summary})
    trigger_count, duplicate_count = _observe_trigger_policy(case)
    estimated, actual = _observe_estimator(case, messages, summary)
    return ContextFidelityObservation(
        summary=summary,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        trigger_count=trigger_count,
        duplicate_trigger_count=duplicate_count,
        estimated_input_tokens=estimated,
        actual_input_tokens=actual,
    )


def evaluate_context_fidelity(
    cases: Iterable[ContextFidelityCase],
    summaries: Mapping[str, Any],
    *,
    events: Iterable[Mapping[str, Any]] | None = None,
) -> ContextFidelityMetrics:
    records: list[dict[str, Any]] = []
    totals = {
        name: [0, 0]
        for name in (
            "constraint",
            "entity",
            "operation",
            "approval",
            "citation",
            "artifact",
        )
    }
    leaks = 0
    compression_values: list[float] = []
    duplicate_values: list[float] = []
    estimate_values: list[float] = []
    cases = list(cases)
    for case in cases:
        supplied = summaries.get(case.case_id, "")
        if isinstance(supplied, ContextFidelityObservation):
            observation = supplied.to_dict()
        elif isinstance(supplied, Mapping) and isinstance(supplied.get("summary"), str):
            observation = dict(supplied)
        else:
            observation = {}
        text = _render(observation.get("summary", supplied))
        for label, values in (
            ("constraint", case.key_constraints),
            ("entity", case.entities),
            ("operation", case.operation_ids),
            ("approval", case.approval_states),
            ("citation", case.citation_ids),
            ("artifact", case.artifact_ids),
        ):
            matched, total = _contains_all(text, values)
            totals[label][0] += matched
            totals[label][1] += total
        foreign_values = []
        owned_values = {
            str(value)
            for values in (
                case.key_constraints,
                case.entities,
                case.operation_ids,
                case.approval_states,
                case.citation_ids,
                case.artifact_ids,
            )
            for value in values
        }
        for other in cases:
            if other.case_id == case.case_id:
                continue
            for key in ("tenant_id", "actor_id", "session_id"):
                value = other.scope.get(key)
                if value not in (None, ""):
                    foreign_values.append(str(value))
            for value in other.scope.get("course_ids", []) or []:
                # Match an explicit entity token rather than a bare integer;
                # otherwise course 1 would falsely leak into tenant-1.
                foreign_values.append(f"course_id={value}")
            for values in (
                other.key_constraints,
                other.entities,
                other.operation_ids,
                other.approval_states,
                other.citation_ids,
                other.artifact_ids,
            ):
                foreign_values.extend(
                    str(value) for value in values if str(value) not in owned_values
                )
        leak = any(
            value
            and (
                re.search(re.escape(value) + r"(?![\w.-])", text) is not None
                if value.startswith("course_id=")
                else value in text
            )
            for value in foreign_values
        )
        leaks += int(leak)
        before_tokens = observation.get("before_tokens")
        after_tokens = observation.get("after_tokens")
        if (
            isinstance(before_tokens, int)
            and not isinstance(before_tokens, bool)
            and before_tokens > 0
            and isinstance(after_tokens, int)
            and not isinstance(after_tokens, bool)
            and after_tokens >= 0
        ):
            compression_values.append(after_tokens / before_tokens)
        trigger_count = observation.get("trigger_count")
        duplicate_count = observation.get("duplicate_trigger_count")
        if (
            isinstance(trigger_count, int)
            and not isinstance(trigger_count, bool)
            and trigger_count > 0
            and isinstance(duplicate_count, int)
            and not isinstance(duplicate_count, bool)
            and duplicate_count >= 0
        ):
            duplicate_values.append(duplicate_count / trigger_count)
        estimated_input = observation.get("estimated_input_tokens")
        actual_input = observation.get("actual_input_tokens")
        if (
            isinstance(estimated_input, int)
            and not isinstance(estimated_input, bool)
            and estimated_input >= 0
            and isinstance(actual_input, int)
            and not isinstance(actual_input, bool)
            and actual_input >= 0
        ):
            estimate_values.append(
                abs(actual_input - estimated_input) / actual_input
                if actual_input
                else 0.0
            )
        records.append(
            {
                "case_id": case.case_id,
                "split": case.split,
                "constraint_fidelity": _rate(*_contains_all(text, case.key_constraints)),
                "entity_fidelity": _rate(*_contains_all(text, case.entities)),
                "operation_fidelity": _rate(*_contains_all(text, case.operation_ids)),
                "approval_fidelity": _rate(*_contains_all(text, case.approval_states)),
                "citation_fidelity": _rate(*_contains_all(text, case.citation_ids)),
                "artifact_fidelity": _rate(*_contains_all(text, case.artifact_ids)),
                "scope_leak": leak,
            }
        )
    if events is not None:
        trigger_events = [
            event
            for event in events
            if str(event.get("event", ""))
            in {"context_compacted", "context_overflow_recovery_compacted"}
        ]
        keys = [
            (
                event.get("checkpoint_id")
                or event.get("source_sha256")
                or event.get("source_sequences")
                or (
                    event.get("details", {}).get("checkpoint_id")
                    if isinstance(event.get("details"), Mapping)
                    else None
                )
            )
            for event in trigger_events
        ]
        duplicate_count = len(keys) - len({repr(key) for key in keys if key is not None})
        duplicate_values = [duplicate_count / len(keys)] if keys else []
    return ContextFidelityMetrics(
        n_cases=len(cases),
        constraint_fidelity=_rate(*totals["constraint"]),
        entity_fidelity=_rate(*totals["entity"]),
        operation_fidelity=_rate(*totals["operation"]),
        approval_fidelity=_rate(*totals["approval"]),
        citation_fidelity=_rate(*totals["citation"]),
        artifact_fidelity=_rate(*totals["artifact"]),
        scope_leak_rate=leaks / len(cases) if cases else None,
        compression_ratio=(sum(compression_values) / len(compression_values)) if compression_values else None,
        duplicate_trigger_rate=(sum(duplicate_values) / len(duplicate_values)) if duplicate_values else None,
        estimate_absolute_error=(sum(estimate_values) / len(estimate_values)) if estimate_values else None,
        cases=tuple(records),
    )


def assert_context_fidelity_thresholds(
    metrics: ContextFidelityMetrics | Mapping[str, Any],
    thresholds: Mapping[str, float],
) -> None:
    """Apply caller-supplied gates; thresholds are intentionally not baked in."""

    values = metrics.to_dict() if isinstance(metrics, ContextFidelityMetrics) else dict(metrics)
    aliases = {
        "key_constraint_fidelity": "constraint_fidelity",
        "scope_leak": "scope_leak_rate",
        "repeated_trigger_rate": "duplicate_trigger_rate",
        "estimation_error": "estimate_absolute_error",
    }
    lower_is_better = {
        "scope_leak_rate",
        "duplicate_trigger_rate",
        "estimate_absolute_error",
        "compression_ratio",
    }
    for name, threshold in thresholds.items():
        metric_name = aliases.get(name, name)
        actual = values.get(metric_name, values.get(name))
        if actual is None:
            raise AssertionError(f"fidelity metric is unavailable: {name}")
        if name == "compression_rate":
            actual = values.get("compression_rate")
        passed = actual <= threshold if metric_name in lower_is_better else actual >= threshold
        if not passed:
            raise AssertionError(f"fidelity threshold failed: {name}={actual} threshold={threshold}")


# Short aliases keep the module pleasant to use from evaluation scripts.
build_corpus = build_context_fidelity_corpus
measure_fidelity = evaluate_context_fidelity


__all__ = [
    "ContextFidelityCase",
    "ContextFidelityMetrics",
    "ContextFidelityObservation",
    "assert_context_fidelity_thresholds",
    "build_context_fidelity_corpus",
    "build_context_fidelity_manifest",
    "build_corpus",
    "evaluate_context_fidelity",
    "measure_fidelity",
    "observe_context_fidelity_case",
    "render_context_fidelity_summary",
    "validate_context_fidelity_corpus",
]
