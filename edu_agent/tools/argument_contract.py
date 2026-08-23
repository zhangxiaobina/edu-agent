"""Schema extension shared by tool registration and runtime normalization."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any


NORMALIZATION_KEYWORD = "x-edu-agent-normalize"
STRING_TO_INTEGER = "string_to_integer_v1"
STRING_TO_NUMBER = "string_to_number_v1"
STRING_TO_BOOLEAN = "string_to_boolean_v1"
JSON_STRING_TO_ARRAY = "json_string_to_array_v1"
JSON_STRING_TO_OBJECT = "json_string_to_object_v1"

NORMALIZATION_RULE_TARGETS = {
    STRING_TO_INTEGER: "integer",
    STRING_TO_NUMBER: "number",
    STRING_TO_BOOLEAN: "boolean",
    JSON_STRING_TO_ARRAY: "array",
    JSON_STRING_TO_OBJECT: "object",
}

_IDENTIFIER_FIELD = re.compile(
    r"(?:^id$|_id$|_ids$|^student_(?:no|number|username)$)",
    re.IGNORECASE,
)
_DATE_FIELD = re.compile(r"(?:^|_)(?:date|time|timestamp)(?:$|_)", re.IGNORECASE)
_FREE_TEXT_FIELDS = frozenset(
    {
        "description",
        "exam_name",
        "expected_output",
        "keyword",
        "knowledge_point",
        "name",
        "node",
        "paper_name",
        "query",
        "search",
        "source_code",
        "stdin",
        "target",
        "title",
    }
)

_SINGLE_SCHEMA_KEYS = frozenset(
    {
        "additionalProperties",
        "contains",
        "contentSchema",
        "else",
        "if",
        "items",
        "not",
        "propertyNames",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
)
_SCHEMA_ARRAY_KEYS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_SCHEMA_MAP_KEYS = frozenset(
    {"$defs", "definitions", "dependentSchemas", "patternProperties", "properties"}
)


def _escape_pointer(token: Any) -> str:
    return str(token).replace("~", "~0").replace("/", "~1")


def strictify_object_schemas(schema: dict[str, Any]) -> None:
    """Default every declared object schema to rejecting unknown members."""

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        declared_type = node.get("type")
        types = (
            {declared_type}
            if isinstance(declared_type, str)
            else {item for item in declared_type if isinstance(item, str)}
            if isinstance(declared_type, (list, tuple))
            else set()
        )
        if "object" in types or isinstance(node.get("properties"), Mapping):
            if node.get("additionalProperties") is True:
                raise ValueError("object schema 禁止 additionalProperties: true")
            node.setdefault("additionalProperties", False)
        for key in sorted(_SINGLE_SCHEMA_KEYS):
            child = node.get(key)
            if isinstance(child, dict):
                walk(child)
        for key in sorted(_SCHEMA_ARRAY_KEYS):
            children = node.get(key)
            if isinstance(children, list):
                for child in children:
                    walk(child)
        for key in sorted(_SCHEMA_MAP_KEYS):
            children = node.get(key)
            if isinstance(children, dict):
                for child in children.values():
                    walk(child)

    walk(schema)


def iter_normalization_declarations(
    schema: Mapping[str, Any],
) -> Iterator[tuple[str, Mapping[str, Any], str]]:
    """Yield pointer, field schema and rule for every declared conversion."""

    def walk(node: Any, pointer: str) -> Iterator[tuple[str, Mapping[str, Any], str]]:
        if not isinstance(node, Mapping):
            return
        rule = node.get(NORMALIZATION_KEYWORD)
        if isinstance(rule, str):
            yield pointer, node, rule
        properties = node.get("properties")
        if isinstance(properties, Mapping):
            for key, child in sorted(properties.items()):
                yield from walk(child, f"{pointer}/{_escape_pointer(key)}")
        additional = node.get("additionalProperties")
        if isinstance(additional, Mapping):
            yield from walk(additional, f"{pointer}/*")
        patterns = node.get("patternProperties")
        if isinstance(patterns, Mapping):
            for key, child in sorted(patterns.items()):
                yield from walk(child, f"{pointer}/pattern:{_escape_pointer(key)}")
        items = node.get("items")
        if isinstance(items, Mapping):
            yield from walk(items, f"{pointer}/*")
        prefix_items = node.get("prefixItems")
        if isinstance(prefix_items, (list, tuple)):
            for index, child in enumerate(prefix_items):
                yield from walk(child, f"{pointer}/{index}")
        for key in sorted(_SCHEMA_ARRAY_KEYS - {"prefixItems"}):
            children = node.get(key)
            if isinstance(children, (list, tuple)):
                for index, child in enumerate(children):
                    yield from walk(child, f"{pointer}/{key}/{index}")
        for key in sorted(_SCHEMA_MAP_KEYS - {"properties", "patternProperties"}):
            children = node.get(key)
            if isinstance(children, Mapping):
                for name, child in sorted(children.items()):
                    yield from walk(
                        child,
                        f"{pointer}/{key}/{_escape_pointer(name)}",
                    )
        for key in sorted(_SINGLE_SCHEMA_KEYS - {"additionalProperties", "items"}):
            child = node.get(key)
            if isinstance(child, Mapping):
                yield from walk(child, f"{pointer}/{key}")

    yield from walk(schema, "")


def validate_normalization_declarations(schema: Mapping[str, Any]) -> None:
    """Reject ambiguous or policy-forbidden conversion declarations."""

    def walk(node: Any, pointer: str, *, runtime_path: bool) -> None:
        if not isinstance(node, Mapping):
            return
        if NORMALIZATION_KEYWORD in node:
            if not runtime_path or not pointer:
                raise ValueError(
                    f"{pointer or '/'} normalization 位于运行时无法无歧义遍历的 schema 位置"
                )
            rule = node[NORMALIZATION_KEYWORD]
            if not isinstance(rule, str) or rule not in NORMALIZATION_RULE_TARGETS:
                raise ValueError(f"{pointer or '/'} normalization rule 无效")
            target = NORMALIZATION_RULE_TARGETS[rule]
            declared = node.get("type")
            types = (
                {declared}
                if isinstance(declared, str)
                else {item for item in declared if isinstance(item, str)}
                if isinstance(declared, (list, tuple))
                else set()
            )
            if target not in types:
                raise ValueError(
                    f"{pointer or '/'} normalization rule 与目标 type 不匹配"
                )
            field_name = pointer.rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~")
            if "enum" in node or "const" in node:
                raise ValueError(f"{pointer or '/'} enum/const 字段禁止 normalization")
            if _IDENTIFIER_FIELD.search(field_name):
                raise ValueError(f"{pointer or '/'} ID 字段禁止 normalization")
            if field_name in _FREE_TEXT_FIELDS:
                raise ValueError(f"{pointer or '/'} 自由文本字段禁止 normalization")
            if node.get("format") in {"date", "date-time", "time", "duration"} or _DATE_FIELD.search(
                field_name
            ):
                raise ValueError(f"{pointer or '/'} 日期时间字段禁止 normalization")
        properties = node.get("properties")
        if isinstance(properties, Mapping):
            for key, child in sorted(properties.items()):
                walk(
                    child,
                    f"{pointer}/{_escape_pointer(key)}",
                    runtime_path=runtime_path,
                )
        additional = node.get("additionalProperties")
        if isinstance(additional, Mapping):
            walk(additional, f"{pointer}/*", runtime_path=runtime_path)
        items = node.get("items")
        if isinstance(items, Mapping):
            walk(items, f"{pointer}/*", runtime_path=runtime_path)
        prefix_items = node.get("prefixItems")
        if isinstance(prefix_items, (list, tuple)):
            for index, child in enumerate(prefix_items):
                walk(child, f"{pointer}/{index}", runtime_path=runtime_path)

        # Pattern overlap, conditionals, combinators and references require
        # instance-dependent schema resolution.  The normalization traversal
        # deliberately does not guess which branch should authorize a repair.
        patterns = node.get("patternProperties")
        if isinstance(patterns, Mapping):
            for key, child in sorted(patterns.items()):
                walk(
                    child,
                    f"{pointer}/pattern:{_escape_pointer(key)}",
                    runtime_path=False,
                )
        for key in sorted(_SCHEMA_ARRAY_KEYS - {"prefixItems"}):
            children = node.get(key)
            if isinstance(children, (list, tuple)):
                for index, child in enumerate(children):
                    walk(
                        child,
                        f"{pointer}/{key}/{index}",
                        runtime_path=False,
                    )
        for key in sorted(_SCHEMA_MAP_KEYS - {"properties", "patternProperties"}):
            children = node.get(key)
            if isinstance(children, Mapping):
                for name, child in sorted(children.items()):
                    walk(
                        child,
                        f"{pointer}/{key}/{_escape_pointer(name)}",
                        runtime_path=False,
                    )
        for key in sorted(_SINGLE_SCHEMA_KEYS - {"additionalProperties", "items"}):
            child = node.get(key)
            if isinstance(child, Mapping):
                walk(child, f"{pointer}/{key}", runtime_path=False)

    walk(schema, "", runtime_path=True)


__all__ = [
    "JSON_STRING_TO_ARRAY",
    "JSON_STRING_TO_OBJECT",
    "NORMALIZATION_KEYWORD",
    "NORMALIZATION_RULE_TARGETS",
    "STRING_TO_BOOLEAN",
    "STRING_TO_INTEGER",
    "STRING_TO_NUMBER",
    "iter_normalization_declarations",
    "strictify_object_schemas",
    "validate_normalization_declarations",
]
