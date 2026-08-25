"""Shared data-boundary vocabulary used by persistence and exports.

The classifier deliberately separates identity/scope keys from values that may
be redacted.  It is a policy description, not a claim that regular
expressions can detect every possible piece of free-text PII.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from typing import Any


REDACTED = "[REDACTED]"


class DataClass(StrEnum):
    CREDENTIAL = "credential"
    STUDENT_PII = "student_pii"
    PRIVATE_PATH = "private_path"
    OWNER_SCOPE = "owner_scope"
    BUSINESS = "business"
    FREE_TEXT = "free_text"
    METRIC = "runtime_metric"


SCOPE_KEYS = frozenset({
    "actor_id", "tenant_id", "owner_id", "run_id", "root_run_id",
    "parent_run_id", "session_id", "request_id", "operation_id",
    "tool_call_id", "plan_id", "step_id", "artifact_id", "event_id",
})
METRIC_KEYS = frozenset({
    "accepted_prediction_tokens", "audio_tokens", "cached_tokens",
    "actual_input_tokens", "actual_minus_estimate_tokens", "actual_output_tokens",
    "actual_total_tokens", "available_input_tokens", "base_estimated_input_tokens",
    "completion_tokens", "completion_tokens_details", "context_tokens",
    "configured_context_limit_tokens", "current_user_turn_tokens",
    "effective_context_limit_tokens", "estimated_input_tokens",
    "estimated_output_reserve_tokens", "estimated_tokens",
    "estimated_tokens_after", "estimated_tokens_before", "fencing_token",
    "history_message_tokens", "input_tokens", "input_tokens_details",
    "max_output_reserve_tokens", "max_tokens", "memory_checkpoint_tokens",
    "min_reclaim_tokens", "output_tokens", "output_tokens_details",
    "plan_evidence_tokens", "reclaimed_tokens", "release_threshold",
    "prompt_tokens", "protocol_overhead_tokens", "provider_context_limit_tokens",
    "provider_max_output_tokens", "requested_tokenizer",
    "prompt_tokens_details", "reasoning_tokens", "rejected_prediction_tokens",
    "system_prompt_tokens", "token_count", "tokenizer_fallback_reason",
    "trigger_threshold",
    "tool_result_tokens", "tool_schema_tokens", "total_reserved_tokens",
    "total_tokens", "tokens",
    "model_calls", "max_model_calls", "tool_calls", "max_tool_calls",
    "max_input_tokens", "max_output_tokens", "max_total_tokens",
    "cost_microusd", "max_cost_microusd", "cost_usd", "known_cost_usd",
    "max_cost_usd", "cost_status", "wall_time_ms", "max_wall_time_ms",
    "wall_time_seconds", "max_wall_time_seconds", "estimated_operations",
    "unknown_cost_operations", "released_operations",
    "duration_ms", "size_bytes", "attempt", "attempt_count", "sequence",
})
_STRICT_METRIC_KEYS = METRIC_KEYS - {
    "cost_status",
    "requested_tokenizer",
    "tokenizer_fallback_reason",
    "tool_calls",
}
CREDENTIAL_KEYS = frozenset({
    "access_token", "api_key", "apikey", "approval_secret", "auth",
    "authorization", "client_secret", "cookie", "jwt", "key_material",
    "password", "private_key", "raw_secret", "refresh_token", "secret",
    "session_cookie", "token",
})
PII_KEYS = frozenset({
    "email", "id_card", "phone", "student_id", "student_name",
    "student_username", "student_no", "student_number",
})
_CREDENTIAL_PARTS = ("password", "passwd", "secret", "token", "cookie", "authorization")
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
# A phone-like run embedded in a hexadecimal digest is not an identifier.
# Hex boundaries keep SHA/commit provenance stable while standalone values,
# JSON fields, and ``phone=...`` text remain detectable.
_PHONE = re.compile(r"(?<![0-9A-Fa-f])(?:\+?86[- ]?)?1[3-9]\d{9}(?![0-9A-Fa-f])")
_PRIVATE_PATH_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9:])/(?:Users|home|private|tmp|var/folders)/[^\s\"'<>]*"),
    re.compile(r"(?i)\b[A-Z]:\\(?:Users|Documents and Settings|Temp)\\[^\s\"'<>]*"),
)
_VALUE_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\b(?:sk|ghp|github_pat|xox[baprs]|pypi|npm|hf)[-_][A-Za-z0-9_.-]{8,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"(?i)\bcanary[_-]?secret(?:[_:=\-][A-Za-z0-9._~+/=-]+)?\b"),
)


def normalize_key(key: Any) -> str:
    return str(key).strip().lower().replace("-", "_")


def classify_key(key: Any) -> DataClass:
    normalized = normalize_key(key)
    if normalized in SCOPE_KEYS:
        return DataClass.OWNER_SCOPE
    if normalized in METRIC_KEYS:
        return DataClass.METRIC
    if normalized in CREDENTIAL_KEYS or any(part in normalized for part in _CREDENTIAL_PARTS):
        return DataClass.CREDENTIAL
    if normalized in PII_KEYS:
        return DataClass.STUDENT_PII
    return DataClass.BUSINESS


def redact_text(
    value: str,
    *,
    include_pii: bool = False,
    include_private_paths: bool = False,
    literal_secrets: tuple[str, ...] = (),
) -> str:
    redacted = value
    for secret in literal_secrets:
        if secret:
            redacted = redacted.replace(secret, REDACTED)
    for pattern in _VALUE_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    if include_pii:
        redacted = _EMAIL.sub(REDACTED, redacted)
        redacted = _PHONE.sub(REDACTED, redacted)
    if include_private_paths:
        for pattern in _PRIVATE_PATH_PATTERNS:
            redacted = pattern.sub(REDACTED, redacted)
    return redacted


def _redact_metric(
    value: Any,
    *,
    include_pii: bool,
    include_private_paths: bool,
    literal_secrets: tuple[str, ...],
) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        return {
            str(key): _redact_metric(
                item,
                include_pii=include_pii,
                include_private_paths=include_private_paths,
                literal_secrets=literal_secrets,
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _redact_metric(
                item,
                include_pii=include_pii,
                include_private_paths=include_private_paths,
                literal_secrets=literal_secrets,
            )
            for item in value
        ]
    return REDACTED


def redact(
    value: Any,
    *,
    include_pii: bool = False,
    include_private_paths: bool = False,
    literal_secrets: tuple[str, ...] = (),
) -> Any:
    if isinstance(value, str):
        return redact_text(
            value,
            include_pii=include_pii,
            include_private_paths=include_private_paths,
            literal_secrets=literal_secrets,
        )
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            category = classify_key(key)
            if category is DataClass.OWNER_SCOPE:
                result[str(key)] = item
            elif category is DataClass.METRIC:
                result[str(key)] = (
                    _redact_metric(
                        item,
                        include_pii=include_pii,
                        include_private_paths=include_private_paths,
                        literal_secrets=literal_secrets,
                    )
                    if normalize_key(key) in _STRICT_METRIC_KEYS
                    else redact(
                        item,
                        include_pii=include_pii,
                        include_private_paths=include_private_paths,
                        literal_secrets=literal_secrets,
                    )
                )
            elif category is DataClass.CREDENTIAL or (include_pii and category is DataClass.STUDENT_PII):
                result[str(key)] = REDACTED
            else:
                result[str(key)] = redact(
                    item,
                    include_pii=include_pii,
                    include_private_paths=include_private_paths,
                    literal_secrets=literal_secrets,
                )
        return result
    if isinstance(value, list):
        return [
            redact(
                item,
                include_pii=include_pii,
                include_private_paths=include_private_paths,
                literal_secrets=literal_secrets,
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            redact(
                item,
                include_pii=include_pii,
                include_private_paths=include_private_paths,
                literal_secrets=literal_secrets,
            )
            for item in value
        )
    return value


def contains_sensitive(value: Any, *, include_pii: bool = True, secrets: tuple[str, ...] = ()) -> bool:
    serialized = json.dumps(value, ensure_ascii=False, default=str)
    if any(secret and secret in serialized for secret in secrets):
        return True
    if any(pattern.search(serialized) for pattern in _VALUE_PATTERNS):
        return True
    if include_pii and (_EMAIL.search(serialized) or _PHONE.search(serialized)):
        return True
    if any(pattern.search(serialized) for pattern in _PRIVATE_PATH_PATTERNS):
        return True

    def walk(item: Any) -> bool:
        if isinstance(item, dict):
            return any(
                classify_key(key) in {DataClass.CREDENTIAL, DataClass.STUDENT_PII}
                or walk(child)
                for key, child in item.items()
            )
        if isinstance(item, (list, tuple)):
            return any(walk(child) for child in item)
        return False

    return walk(value)


def content_classifications(value: Any) -> tuple[str, ...]:
    """Return deterministic data classes present in a value without exposing values."""

    found: set[DataClass] = set()

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                category = classify_key(key)
                found.add(category)
                walk(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child)
        elif isinstance(item, str):
            if any(pattern.search(item) for pattern in _VALUE_PATTERNS):
                found.add(DataClass.CREDENTIAL)
            if _EMAIL.search(item) or _PHONE.search(item):
                found.add(DataClass.STUDENT_PII)
            if any(pattern.search(item) for pattern in _PRIVATE_PATH_PATTERNS):
                found.add(DataClass.PRIVATE_PATH)

    walk(value)
    if not found:
        found.add(DataClass.BUSINESS)
    priority = {
        DataClass.CREDENTIAL: 0,
        DataClass.STUDENT_PII: 1,
        DataClass.PRIVATE_PATH: 2,
        DataClass.OWNER_SCOPE: 3,
        DataClass.FREE_TEXT: 4,
        DataClass.BUSINESS: 5,
        DataClass.METRIC: 6,
    }
    return tuple(item.value for item in sorted(found, key=priority.__getitem__))


__all__ = [
    "CREDENTIAL_KEYS", "DataClass", "METRIC_KEYS", "PII_KEYS", "REDACTED",
    "SCOPE_KEYS", "classify_key", "contains_sensitive", "content_classifications",
    "normalize_key", "redact", "redact_text",
]
