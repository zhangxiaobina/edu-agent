"""Strict, schema-guided tool argument preparation.

The pipeline is intentionally one-way: parse a bounded JSON object, apply at
most one explicitly declared conversion per node, then validate the complete
normalized value.  No handler-facing defaults or heuristic repairs live here.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker, validators

from ..data_classification import REDACTED, DataClass
from ..tools.argument_contract import (
    JSON_STRING_TO_ARRAY,
    JSON_STRING_TO_OBJECT,
    NORMALIZATION_KEYWORD,
    NORMALIZATION_RULE_TARGETS,
    STRING_TO_BOOLEAN,
    STRING_TO_INTEGER,
    STRING_TO_NUMBER,
)
from ..tools.manifest import ToolEffect

MAX_ARGUMENT_BYTES = 64 * 1024
MAX_ARGUMENT_DEPTH = 32
MAX_ARGUMENT_NODES = 4096
MAX_CONTAINER_ITEMS = 1024
MAX_SCHEMA_ISSUES = 64

_INTEGER_TEXT = re.compile(r"(?:0|-[1-9][0-9]*|[1-9][0-9]*)")
_NUMBER_TEXT = re.compile(
    r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?"
)
_IDENTIFIER_FIELD = re.compile(
    r"(?:^id$|_id$|_ids$|^student_(?:no|number|username)$)",
    re.IGNORECASE,
)
_DATE_FIELD = re.compile(r"(?:^|_)(?:date|time|timestamp)(?:$|_)", re.IGNORECASE)
_SENSITIVE_CLASSES = frozenset(
    {
        DataClass.CREDENTIAL.value,
        DataClass.STUDENT_PII.value,
        DataClass.PRIVATE_PATH.value,
        DataClass.FREE_TEXT.value,
    }
)
_NORMALIZABLE_EFFECTS = frozenset({ToolEffect.READ, ToolEffect.PURE})
_INTEGRAL_NUMBER_TO_INTEGER = "integral_number_to_integer_v1"


@dataclass(frozen=True)
class RepairAudit:
    pointer: str
    original_type: str
    target_type: str
    rule_id: str
    result: str
    original_sha256: str
    sensitive: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ToolArgumentError(ValueError):
    """A bounded, value-free error suitable for structured model feedback."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        issues: Sequence[Mapping[str, Any]] = (),
        repair_audits: Sequence[RepairAudit] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.issues = tuple(dict(issue) for issue in issues)
        self.repair_audits = tuple(repair_audits)

    def to_error(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.issues:
            error["details"] = {"issues": [dict(issue) for issue in self.issues]}
        return error


class _DuplicateKey(ValueError):
    pass


class _NonFiniteConstant(ValueError):
    pass


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey("duplicate object member")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise _NonFiniteConstant("non-finite number")


def _decode_json(value: str) -> Any:
    return json.loads(
        value,
        object_pairs_hook=_reject_duplicate_pairs,
        parse_constant=_reject_constant,
    )


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _bounded_json_copy(value: Any) -> Any:
    nodes = 0
    scalar_bytes = 0

    def charge_scalar(node: Any) -> None:
        nonlocal scalar_bytes
        try:
            rendered = json.dumps(
                node,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, UnicodeError, ValueError) as error:
            raise ToolArgumentError("INVALID_JSON", "工具参数包含无效 JSON 标量") from error
        scalar_bytes += len(rendered)
        if scalar_bytes > MAX_ARGUMENT_BYTES:
            raise ToolArgumentError(
                "ARGUMENT_LIMIT_EXCEEDED",
                "工具参数大小超过安全上限",
            )

    def copy_node(node: Any, depth: int, ancestors: frozenset[int]) -> Any:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_ARGUMENT_NODES:
            raise ToolArgumentError(
                "ARGUMENT_LIMIT_EXCEEDED",
                "工具参数节点数超过安全上限",
            )
        if depth > MAX_ARGUMENT_DEPTH:
            raise ToolArgumentError(
                "ARGUMENT_LIMIT_EXCEEDED",
                "工具参数嵌套深度超过安全上限",
            )
        if node is None or type(node) in {str, bool, int}:
            charge_scalar(node)
            return node
        if type(node) is float:
            if not math.isfinite(node):
                raise ToolArgumentError(
                    "INVALID_JSON",
                    "工具参数不允许 NaN 或 Infinity",
                )
            charge_scalar(node)
            return node
        if type(node) is dict:
            if len(node) > MAX_CONTAINER_ITEMS:
                raise ToolArgumentError(
                    "ARGUMENT_LIMIT_EXCEEDED",
                    "工具参数 object 成员数超过安全上限",
                )
            identity = id(node)
            if identity in ancestors:
                raise ToolArgumentError("INVALID_JSON", "工具参数包含循环引用")
            next_ancestors = ancestors | {identity}
            if any(not isinstance(key, str) for key in node):
                raise ToolArgumentError(
                    "INVALID_JSON",
                    "工具参数 object 的 key 必须是字符串",
                )
            result: dict[str, Any] = {}
            for key in sorted(node):
                charge_scalar(key)
                result[key] = copy_node(node[key], depth + 1, next_ancestors)
            return result
        if type(node) is list:
            if len(node) > MAX_CONTAINER_ITEMS:
                raise ToolArgumentError(
                    "ARGUMENT_LIMIT_EXCEEDED",
                    "工具参数 array 元素数超过安全上限",
                )
            identity = id(node)
            if identity in ancestors:
                raise ToolArgumentError("INVALID_JSON", "工具参数包含循环引用")
            next_ancestors = ancestors | {identity}
            return [copy_node(item, depth + 1, next_ancestors) for item in node]
        raise ToolArgumentError(
            "INVALID_JSON",
            f"工具参数包含非 JSON 类型：{type(node).__name__}",
        )

    copied = copy_node(value, 0, frozenset())
    try:
        serialized = json.dumps(
            copied,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, UnicodeError, ValueError, RecursionError) as error:
        raise ToolArgumentError("INVALID_JSON", "工具参数不是有限 JSON 值") from error
    if len(serialized.encode("utf-8")) > MAX_ARGUMENT_BYTES:
        raise ToolArgumentError(
            "ARGUMENT_LIMIT_EXCEEDED",
            "工具参数大小超过安全上限",
        )
    return copied


def strict_parse_tool_arguments(raw_arguments: str | dict | None) -> dict[str, Any]:
    """Parse exactly one bounded JSON object without heuristic recovery."""

    if type(raw_arguments) is dict:
        parsed: Any = raw_arguments
    elif raw_arguments is None:
        parsed = {}
    elif isinstance(raw_arguments, str):
        if len(raw_arguments) > MAX_ARGUMENT_BYTES:
            raise ToolArgumentError(
                "ARGUMENT_LIMIT_EXCEEDED",
                "工具参数大小超过安全上限",
            )
        try:
            raw_size = len(raw_arguments.encode("utf-8"))
        except UnicodeError as error:
            raise ToolArgumentError("INVALID_JSON", "工具参数包含无效 Unicode") from error
        if raw_size > MAX_ARGUMENT_BYTES:
            raise ToolArgumentError(
                "ARGUMENT_LIMIT_EXCEEDED",
                "工具参数大小超过安全上限",
            )
        try:
            parsed = _decode_json(raw_arguments)
        except _DuplicateKey as error:
            raise ToolArgumentError(
                "INVALID_JSON",
                "工具参数 JSON object 含重复字段",
            ) from error
        except _NonFiniteConstant as error:
            raise ToolArgumentError(
                "INVALID_JSON",
                "工具参数不允许 NaN 或 Infinity",
            ) from error
        except json.JSONDecodeError as error:
            raise ToolArgumentError(
                "INVALID_JSON",
                f"工具参数不是合法 JSON：{error.msg}",
            ) from error
        except ValueError as error:
            raise ToolArgumentError(
                "INVALID_JSON",
                "工具参数包含无效或超限 JSON 数值",
            ) from error
        except RecursionError as error:
            raise ToolArgumentError(
                "ARGUMENT_LIMIT_EXCEEDED",
                "工具参数嵌套深度超过安全上限",
            ) from error
    else:
        raise ToolArgumentError(
            "INVALID_ARGUMENTS",
            "工具参数必须是 JSON object",
        )
    if type(parsed) is not dict:
        raise ToolArgumentError(
            "INVALID_ARGUMENTS",
            "工具参数必须是 JSON object",
        )
    return _bounded_json_copy(parsed)


def summarize_raw_arguments(raw_arguments: Any) -> dict[str, Any]:
    """Return a persistence-safe summary; raw malformed input is never stored."""

    raw_type = _json_type(raw_arguments)
    if isinstance(raw_arguments, str):
        encoded = raw_arguments.encode("utf-8", errors="surrogatepass")
    else:
        try:
            encoded = json.dumps(
                raw_arguments,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
                default=lambda item: f"<{type(item).__name__}>",
            ).encode("utf-8")
        except (TypeError, UnicodeError, ValueError, RecursionError):
            encoded = f"<{type(raw_arguments).__name__}>".encode()
    return {
        "raw_type": raw_type,
        "raw_bytes": len(encoded),
        "raw_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _escape_pointer_token(token: Any) -> str:
    return str(token).replace("~", "~0").replace("/", "~1")


def _pointer(parts: Sequence[Any]) -> str:
    if not parts:
        return ""
    return "/" + "/".join(_escape_pointer_token(part) for part in parts)


def _schema_types(schema: Mapping[str, Any]) -> frozenset[str]:
    declared = schema.get("type")
    if isinstance(declared, str):
        return frozenset({declared})
    if isinstance(declared, (list, tuple)):
        return frozenset(item for item in declared if isinstance(item, str))
    return frozenset()


def _classification_for(
    pointer: str,
    classifications: Mapping[str, str],
) -> str | None:
    if pointer in classifications:
        return classifications[pointer]
    tokens = pointer.split("/")
    for candidate, classification in sorted(classifications.items()):
        candidate_tokens = candidate.split("/")
        if len(candidate_tokens) == len(tokens) and all(
            expected == "*" or expected == actual
            for expected, actual in zip(candidate_tokens, tokens, strict=True)
        ):
            return classification
    return None


def _is_sensitive(pointer: str, classifications: Mapping[str, str]) -> bool:
    return _classification_for(pointer, classifications) in _SENSITIVE_CLASSES


def _value_digest(value: Any) -> str:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _protected_pointer(pointer: str, protected: frozenset[str]) -> bool:
    return any(
        pointer == candidate
        or pointer.startswith(candidate + "/")
        or candidate.startswith(pointer + "/")
        for candidate in protected
        if candidate
    )


def _repair_policy_result(
    *,
    pointer: str,
    schema: Mapping[str, Any],
    effect: ToolEffect,
    classifications: Mapping[str, str],
    protected_pointers: frozenset[str],
) -> str | None:
    if effect not in _NORMALIZABLE_EFFECTS:
        return "rejected_effect_policy"
    if _protected_pointer(pointer, protected_pointers):
        return "rejected_approval_semantics"
    field_name = pointer.rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~")
    if _IDENTIFIER_FIELD.search(field_name):
        return "rejected_identifier"
    if "enum" in schema or "const" in schema:
        return "rejected_enum"
    if schema.get("format") in {"date", "date-time", "time", "duration"} or _DATE_FIELD.search(
        field_name
    ):
        return "rejected_datetime"
    if _is_sensitive(pointer, classifications):
        return "rejected_sensitive_field"
    return None


def _convert_string(rule_id: str, value: str) -> tuple[bool, Any, str]:
    if rule_id == STRING_TO_INTEGER:
        if not _INTEGER_TEXT.fullmatch(value):
            return False, value, "rejected_lexeme"
        try:
            return True, int(value), "applied"
        except ValueError:
            return False, value, "rejected_limit"
    if rule_id == STRING_TO_NUMBER:
        if not _NUMBER_TEXT.fullmatch(value):
            return False, value, "rejected_lexeme"
        converted: int | float
        try:
            converted = float(value) if "." in value or "e" in value.lower() else int(value)
        except (OverflowError, ValueError):
            return False, value, "rejected_limit"
        if isinstance(converted, float) and not math.isfinite(converted):
            return False, value, "rejected_non_finite"
        return True, converted, "applied"
    if rule_id == STRING_TO_BOOLEAN:
        if value == "true":
            return True, True, "applied"
        if value == "false":
            return True, False, "applied"
        return False, value, "rejected_lexeme"
    if rule_id in {JSON_STRING_TO_ARRAY, JSON_STRING_TO_OBJECT}:
        if len(value.encode("utf-8")) > MAX_ARGUMENT_BYTES:
            return False, value, "rejected_limit"
        try:
            converted = _decode_json(value)
            converted = _bounded_json_copy(converted)
        except (ToolArgumentError, ValueError, RecursionError):
            return False, value, "rejected_json"
        target = list if rule_id == JSON_STRING_TO_ARRAY else dict
        if not isinstance(converted, target):
            return False, value, "rejected_container_type"
        return True, converted, "applied"
    return False, value, "rejected_unknown_rule"


def _child_schema_for_key(schema: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    properties = schema.get("properties")
    if isinstance(properties, Mapping) and isinstance(properties.get(key), Mapping):
        return properties[key]
    matches: list[Mapping[str, Any]] = []
    patterns = schema.get("patternProperties")
    if isinstance(patterns, Mapping):
        for pattern, child in sorted(patterns.items()):
            if isinstance(child, Mapping) and re.search(str(pattern), key):
                matches.append(child)
    if matches:
        # Normalization declarations under patternProperties are rejected at
        # registration.  Do not implicitly reinterpret integer-like values on
        # an instance-selected pattern branch either.
        return None
    additional = schema.get("additionalProperties")
    return additional if not matches and isinstance(additional, Mapping) else None


def normalize_tool_arguments(
    arguments: dict[str, Any],
    schema: Mapping[str, Any],
    *,
    effect: ToolEffect | str,
    data_classification: Mapping[str, str] | None = None,
    protected_pointers: Sequence[str] = (),
) -> tuple[dict[str, Any], tuple[RepairAudit, ...]]:
    """Apply one deterministic, schema-declared normalization traversal."""

    parsed_effect = ToolEffect.parse(effect)
    classifications = dict(data_classification or {})
    protected = frozenset(
        pointer for pointer in protected_pointers if isinstance(pointer, str) and pointer.startswith("/")
    )
    audits: list[RepairAudit] = []
    traversed_nodes = 0

    def walk(value: Any, node_schema: Mapping[str, Any], parts: tuple[Any, ...]) -> Any:
        nonlocal traversed_nodes
        traversed_nodes += 1
        if traversed_nodes > MAX_ARGUMENT_NODES:
            raise ToolArgumentError(
                "ARGUMENT_LIMIT_EXCEEDED",
                "工具参数节点数超过安全上限",
            )
        if len(parts) > MAX_ARGUMENT_DEPTH:
            raise ToolArgumentError(
                "ARGUMENT_LIMIT_EXCEEDED",
                "工具参数嵌套深度超过安全上限",
            )
        pointer = _pointer(parts)
        rule_id = node_schema.get(NORMALIZATION_KEYWORD)
        if (
            isinstance(value, float)
            and math.isfinite(value)
            and value.is_integer()
            and "integer" in _schema_types(node_schema)
            and "number" not in _schema_types(node_schema)
        ):
            denied = _repair_policy_result(
                pointer=pointer,
                schema=node_schema,
                effect=parsed_effect,
                classifications=classifications,
                protected_pointers=protected,
            )
            if denied is not None:
                result = denied
            elif value == 0 and math.copysign(1.0, value) < 0:
                result = "rejected_negative_zero"
            elif abs(value) > 2**53 - 1:
                result = "rejected_unsafe_integer"
            else:
                result = "applied"
            audits.append(
                RepairAudit(
                    pointer=pointer,
                    original_type="number",
                    target_type="integer",
                    rule_id=_INTEGRAL_NUMBER_TO_INTEGER,
                    result=result,
                    original_sha256=_value_digest(value),
                    sensitive=_is_sensitive(pointer, classifications),
                )
            )
            if result == "applied":
                value = int(value)
        elif isinstance(value, str) and isinstance(rule_id, str):
            target_type = NORMALIZATION_RULE_TARGETS.get(rule_id, "unknown")
            denied = _repair_policy_result(
                pointer=pointer,
                schema=node_schema,
                effect=parsed_effect,
                classifications=classifications,
                protected_pointers=protected,
            )
            if denied is None:
                applied, converted, result = _convert_string(rule_id, value)
            else:
                applied, converted, result = False, value, denied
            audits.append(
                RepairAudit(
                    pointer=pointer,
                    original_type="string",
                    target_type=target_type,
                    rule_id=rule_id,
                    result=result,
                    original_sha256=_value_digest(value),
                    sensitive=_is_sensitive(pointer, classifications),
                )
            )
            if applied:
                value = converted
        if isinstance(value, dict):
            normalized: dict[str, Any] = {}
            for key in sorted(value):
                child_schema = _child_schema_for_key(node_schema, key)
                normalized[key] = (
                    walk(value[key], child_schema, (*parts, key))
                    if child_schema is not None
                    else value[key]
                )
            return normalized
        if isinstance(value, list):
            prefix_items = node_schema.get("prefixItems")
            item_schema = node_schema.get("items")
            normalized_items: list[Any] = []
            for index, item in enumerate(value):
                child_schema = None
                if isinstance(prefix_items, (list, tuple)) and index < len(prefix_items):
                    candidate = prefix_items[index]
                    child_schema = candidate if isinstance(candidate, Mapping) else None
                elif isinstance(item_schema, Mapping):
                    child_schema = item_schema
                normalized_items.append(
                    walk(item, child_schema, (*parts, index))
                    if child_schema is not None
                    else item
                )
            return normalized_items
        return value

    try:
        normalized = walk(arguments, schema, ())
        normalized = _bounded_json_copy(normalized)
    except ToolArgumentError as error:
        rejected = tuple(
            replace(audit, result="rejected_aggregate_limit")
            if audit.result == "applied"
            else audit
            for audit in audits
        )
        raise ToolArgumentError(
            error.code,
            error.message,
            issues=error.issues,
            repair_audits=rejected,
        ) from error
    return normalized, tuple(audits)


def _strict_integer(_checker: Any, instance: Any) -> bool:
    return isinstance(instance, int) and not isinstance(instance, bool)


def _strict_number(_checker: Any, instance: Any) -> bool:
    return (
        isinstance(instance, (int, float))
        and not isinstance(instance, bool)
        and (not isinstance(instance, float) or math.isfinite(instance))
    )


@lru_cache(maxsize=None)
def _strict_validator_class(base: type) -> type:
    type_checker = base.TYPE_CHECKER.redefine_many(
        {"integer": _strict_integer, "number": _strict_number}
    )
    return validators.extend(base, type_checker=type_checker)


def _unknown_properties(error: Any) -> list[str]:
    if error.validator != "additionalProperties" or error.validator_value is not False:
        return []
    instance = error.instance
    schema = error.schema
    if not isinstance(instance, dict) or not isinstance(schema, Mapping):
        return []
    properties = schema.get("properties")
    declared = set(properties) if isinstance(properties, Mapping) else set()
    patterns = schema.get("patternProperties")
    pattern_names = tuple(patterns) if isinstance(patterns, Mapping) else ()
    return sorted(
        key
        for key in instance
        if key not in declared and not any(re.search(str(pattern), key) for pattern in pattern_names)
    )


def _issue_message(keyword: str, pointer: str, validator_value: Any) -> str:
    location = pointer or "/"
    if keyword == "type":
        return f"{location} 类型必须是 {validator_value}"
    if keyword == "enum":
        return f"{location} 必须匹配声明的 enum"
    if keyword == "const":
        return f"{location} 必须匹配声明的 const"
    if keyword == "required":
        return f"{location} 缺少必填字段"
    if keyword == "additionalProperties":
        return f"{location} 含未知字段"
    labels = {
        "minimum": "不能小于",
        "maximum": "不能大于",
        "exclusiveMinimum": "必须大于",
        "exclusiveMaximum": "必须小于",
        "multipleOf": "必须是其倍数",
        "minLength": "长度不能小于",
        "maxLength": "长度不能大于",
        "minItems": "元素数不能小于",
        "maxItems": "元素数不能大于",
        "minProperties": "成员数不能小于",
        "maxProperties": "成员数不能大于",
    }
    if keyword in labels:
        return f"{location} {labels[keyword]} {validator_value}"
    if keyword == "uniqueItems":
        return f"{location} 元素必须唯一"
    if keyword == "pattern":
        return f"{location} 不符合声明的 pattern"
    if keyword == "format":
        return f"{location} 不符合 {validator_value} 格式"
    return f"{location} 未通过 JSON Schema 关键字 {keyword}"


def validate_tool_arguments(
    arguments: dict[str, Any],
    schema: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    """Return deterministic, value-free Draft 2020-12 validation issues."""

    strict_cls = _strict_validator_class(Draft202012Validator)
    validator = strict_cls(schema, format_checker=FormatChecker())
    issues: list[dict[str, Any]] = []
    for error in validator.iter_errors(arguments):
        base_parts = tuple(error.absolute_path)
        if error.validator == "required" and isinstance(error.validator_value, (list, tuple)):
            missing = sorted(
                field
                for field in error.validator_value
                if isinstance(field, str)
                and isinstance(error.instance, dict)
                and field not in error.instance
            )
            for field in missing:
                pointer = _pointer((*base_parts, field))
                issues.append(
                    {
                        "pointer": pointer,
                        "keyword": "required",
                        "message": f"{pointer or '/'} 为必填参数",
                    }
                )
            continue
        unknown = _unknown_properties(error)
        if unknown:
            for field in unknown:
                pointer = _pointer((*base_parts, field))
                issues.append(
                    {
                        "pointer": pointer,
                        "keyword": "additionalProperties",
                        "message": f"{pointer or '/'} 是未知参数",
                    }
                )
            continue
        keyword = str(error.validator or "schema")
        pointer = _pointer(base_parts)
        issues.append(
            {
                "pointer": pointer,
                "keyword": keyword,
                "message": _issue_message(keyword, pointer, error.validator_value),
            }
        )
    issues.sort(key=lambda item: (item["pointer"], item["keyword"], item["message"]))
    if len(issues) > MAX_SCHEMA_ISSUES:
        omitted = len(issues) - MAX_SCHEMA_ISSUES
        issues = issues[:MAX_SCHEMA_ISSUES]
        issues.append(
            {
                "pointer": "",
                "keyword": "issueLimit",
                "message": f"其余 {omitted} 个校验错误已省略",
            }
        )
    return tuple(issues)


def redact_classified_arguments(
    arguments: Any,
    data_classification: Mapping[str, str] | None,
) -> Any:
    """Redact schema-classified input values before persistence or Trace."""

    classifications = dict(data_classification or {})

    def walk(value: Any, parts: tuple[Any, ...]) -> Any:
        pointer = _pointer(parts)
        if pointer and _is_sensitive(pointer, classifications):
            return REDACTED
        if isinstance(value, dict):
            return {key: walk(child, (*parts, key)) for key, child in value.items()}
        if isinstance(value, list):
            return [walk(child, (*parts, index)) for index, child in enumerate(value)]
        return value

    return walk(arguments, ())


__all__ = [
    "JSON_STRING_TO_ARRAY",
    "JSON_STRING_TO_OBJECT",
    "MAX_ARGUMENT_BYTES",
    "MAX_ARGUMENT_DEPTH",
    "MAX_ARGUMENT_NODES",
    "NORMALIZATION_KEYWORD",
    "NORMALIZATION_RULE_TARGETS",
    "RepairAudit",
    "STRING_TO_BOOLEAN",
    "STRING_TO_INTEGER",
    "STRING_TO_NUMBER",
    "ToolArgumentError",
    "normalize_tool_arguments",
    "redact_classified_arguments",
    "strict_parse_tool_arguments",
    "summarize_raw_arguments",
    "validate_tool_arguments",
]
