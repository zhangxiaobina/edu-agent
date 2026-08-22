"""Immutable tool metadata and run-level manifest contracts.

The registry is intentionally kept small: this module owns the data model,
canonical serialization and validation rules used by local tools, plugins,
RAG wrappers and MCP providers.  A manifest contains the *frozen* tool surface
for one run; it is not a second dispatch implementation.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from ..data_classification import DataClass, classify_key


class ToolManifestError(ValueError):
    """Base error for malformed tool metadata or an invalid manifest."""


class ToolRegistrationError(ToolManifestError):
    """Raised when a tool cannot be admitted to the registry."""


class ToolManifestMismatch(ToolManifestError):
    """Raised when a frozen run identity no longer matches the registry."""


class ToolEffect(str, Enum):
    """Effect classes are explicit policy inputs, never inferred from names."""

    READ = "read"
    PURE = "pure"
    WRITE = "write"
    CONDITIONAL_WRITE = "conditional_write"
    CODE_EXECUTION = "code_execution"
    APPROVAL = "approval"
    INTERACTIVE = "interactive"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: "ToolEffect | str") -> "ToolEffect":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower().replace("-", "_")
            aliases = {
                "pure_read": cls.READ.value,
                "readonly": cls.READ.value,
                "read_only": cls.READ.value,
                "conditional-write": cls.CONDITIONAL_WRITE.value,
                "code": cls.CODE_EXECUTION.value,
                "execute": cls.CODE_EXECUTION.value,
            }
            normalized = aliases.get(normalized, normalized)
            try:
                return cls(normalized)
            except ValueError:
                pass
        raise ToolRegistrationError(f"未知工具 effect: {value!r}")


class ToolRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @classmethod
    def parse(cls, value: "ToolRisk | str") -> "ToolRisk":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value.strip().lower())
            except ValueError:
                pass
        raise ToolRegistrationError(f"未知工具 risk: {value!r}")


class ToolCapability(str, Enum):
    """Well-known capability labels; plugins may use stable custom labels."""

    TOOL_CALLING = "tool_calling"
    TEACHING_QUERY = "teaching.query"
    TEACHING_KNOWLEDGE = "teaching.knowledge"
    TEACHING_ANALYSIS = "teaching.analysis"
    TEACHING_WRITE = "teaching.write"
    TEACHING_CONTENT = "teaching.content"
    RAG = "rag"
    CODE_EXECUTION = "code_execution"


class _Unset:
    pass


_UNSET = _Unset()
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_VERSION_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:-]{0,63}$")
_KNOWN_ROLES = frozenset({"student", "teacher", "admin", "system"})
_EFFECTS_WITHOUT_SIDE_EFFECT = frozenset({ToolEffect.READ, ToolEffect.PURE})
_CAPABILITY_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=lambda item: repr(item))
    if hasattr(value, "isoformat") and callable(value.isoformat):
        return value.isoformat()
    raise TypeError(f"value is not canonical JSON: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Serialize JSON-compatible data independent of dictionary insertion order."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        )
    except (TypeError, ValueError) as error:
        raise ToolManifestError(f"无法生成 canonical JSON: {error}") from error


def canonical_schema(schema: Mapping[str, Any]) -> str:
    """Return the canonical function schema representation used for hashing."""

    if not isinstance(schema, Mapping):
        raise ToolRegistrationError("tool schema 必须是 object")
    return canonical_json(dict(schema))


def canonical_schema_hash(schema: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_schema(schema).encode("utf-8")).hexdigest()


# Short aliases are useful to callers that use the roadmap terminology.
schema_hash = canonical_schema_hash


def _escape_pointer(token: str) -> str:
    return str(token).replace("~", "~0").replace("/", "~1")


_FREE_TEXT_FIELDS = frozenset(
    {
        "description",
        "exam_name",
        "paper_name",
        "title",
        "search",
        "keyword",
        "knowledge_point",
        "node",
        "target",
        "name",
        "query",
        "source_code",
        "stdin",
        "expected_output",
        "end_time",
        "start_time",
    }
)


def field_data_classification(schema: Mapping[str, Any]) -> dict[str, str]:
    """Classify every declared input field by JSON Pointer.

    The classifier is conservative for free text and uses the shared project
    vocabulary for identifiers/credentials.  Nested object and array fields
    receive both a container pointer and a wildcard child pointer.
    """

    parameters = schema.get("parameters", schema)
    if not isinstance(parameters, Mapping):
        return {}
    result: dict[str, str] = {}

    def walk(node: Mapping[str, Any], pointer: str) -> None:
        properties = node.get("properties", {})
        if not isinstance(properties, Mapping):
            return
        for key, child in properties.items():
            if not isinstance(key, str) or not isinstance(child, Mapping):
                continue
            child_pointer = f"{pointer}/{_escape_pointer(key)}"
            category = (
                DataClass.FREE_TEXT
                if key in _FREE_TEXT_FIELDS
                else classify_key(key)
            )
            result[child_pointer] = category.value
            if child.get("type") == "object":
                walk(child, child_pointer)
            elif child.get("type") == "array" and isinstance(child.get("items"), Mapping):
                item = child["items"]
                item_pointer = f"{child_pointer}/*"
                item_type = item.get("type")
                result[item_pointer] = (
                    DataClass.FREE_TEXT.value
                    if item_type == "string"
                    else classify_key(key).value
                )
                if item_type == "object":
                    walk(item, item_pointer)

    walk(parameters, "")
    return result


def _schema_field_pointers(schema: Mapping[str, Any]) -> frozenset[str]:
    """Return declared JSON-pointer fields, including nested wildcards."""

    return frozenset(field_data_classification(schema))


def _validate_resource_key_rules(
    schema: Mapping[str, Any], resource_keys: Iterable[str],
) -> None:
    """Ensure resource rules resolve only declared arguments or run scope."""

    pointers = _schema_field_pointers(schema)
    parameters = schema.get("parameters", {})
    properties = parameters.get("properties", {}) if isinstance(parameters, Mapping) else {}
    top_level = set(properties) if isinstance(properties, Mapping) else set()
    allowed_templates = {"tenant_id", "actor_id", "run_id"} | top_level
    for rule in resource_keys:
        if rule.startswith("/"):
            if rule == "/" or (
                rule not in pointers
                and not any(
                    candidate.startswith(rule + "/")
                    or candidate.startswith(rule + "/*")
                    for candidate in pointers
                )
            ):
                raise ToolRegistrationError(
                    f"resource key rule 指向 schema 未声明字段: {rule}"
                )
            continue
        for placeholder in re.findall(r"\{([^{}]+)\}", rule):
            if placeholder not in allowed_templates:
                raise ToolRegistrationError(
                    f"resource key rule 含未知模板字段: {placeholder}"
                )


def _validate_schema_metadata_fields(
    schema: Mapping[str, Any],
    *,
    mutation_parameters: Iterable[str],
    classifications: Mapping[str, Any],
) -> None:
    parameters = schema.get("parameters", {})
    properties = parameters.get("properties", {}) if isinstance(parameters, Mapping) else {}
    parameter_names = set(properties) if isinstance(properties, Mapping) else set()
    unknown_mutations = set(mutation_parameters) - parameter_names
    if unknown_mutations:
        raise ToolRegistrationError(
            "mutation_parameters 未在 schema 声明: "
            + ", ".join(sorted(unknown_mutations))
        )
    pointers = _schema_field_pointers(schema)
    for pointer in classifications:
        normalized = pointer if str(pointer).startswith("/") else f"/{pointer}"
        if normalized == "/":
            continue
        # A caller may classify an object container even when the inferred
        # walker only emitted its leaf fields; accept the exact declared path.
        if normalized not in pointers and not any(
            candidate.startswith(normalized + "/") or candidate.startswith(normalized + "/*")
            for candidate in pointers
        ):
            raise ToolRegistrationError(
                f"字段数据分类指向 schema 未声明字段: {normalized}"
            )


def _deep_copy_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return _thaw_json(value)


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(child) for child in value)
    if isinstance(value, tuple):
        return tuple(_freeze_json(child) for child in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(child) for child in value]
    return value


def _validate_json_values(value: Any, path: str = "schema") -> None:
    """Reject non-JSON values that jsonschema itself may accept indirectly."""

    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ToolRegistrationError(f"{path} 不允许 NaN/Infinity")
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ToolRegistrationError(f"{path} 的 key 必须是字符串")
            _validate_json_values(child, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _validate_json_values(child, f"{path}[{index}]")
        return
    raise ToolRegistrationError(f"{path} 含非 JSON 值 {type(value).__name__}")


def validate_function_schema(schema: Mapping[str, Any], *, name: str | None = None) -> dict[str, Any]:
    """Validate an OpenAI function schema and return a strict deep copy."""

    if not isinstance(schema, Mapping):
        raise ToolRegistrationError("tool schema 必须是 object")
    normalized = _deep_copy_mapping(schema)
    schema_name = normalized.get("name")
    if not isinstance(schema_name, str) or not _SAFE_IDENTIFIER.fullmatch(schema_name):
        raise ToolRegistrationError("tool schema name 必须是稳定标识符")
    if name is not None and schema_name != name:
        raise ToolRegistrationError("tool schema name 与注册名不一致")
    if "description" in normalized and not isinstance(normalized["description"], str):
        raise ToolRegistrationError("tool schema description 必须是字符串")
    if "strict" in normalized and not isinstance(normalized["strict"], bool):
        raise ToolRegistrationError("tool schema strict 必须是 bool")
    parameters = normalized.get("parameters")
    if not isinstance(parameters, dict):
        raise ToolRegistrationError("tool schema 必须包含 parameters object")
    if parameters.get("type") != "object":
        raise ToolRegistrationError("tool parameters 根节点必须是 object")
    properties = parameters.get("properties", {})
    if not isinstance(properties, dict):
        raise ToolRegistrationError("tool parameters.properties 必须是 object")
    required = parameters.get("required", [])
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        raise ToolRegistrationError("tool parameters.required 必须是字符串数组")
    if len(set(required)) != len(required) or not set(required) <= set(properties):
        raise ToolRegistrationError("tool parameters.required 含重复或未知字段")
    parameters.setdefault("additionalProperties", False)
    _validate_json_values(normalized)
    try:
        import jsonschema

        validator_cls = jsonschema.validators.validator_for(parameters)
        validator_cls.check_schema(parameters)
    except ImportError:
        # The project lock currently brings jsonschema transitively.  Keep a
        # structural fallback for minimal standard-library plugin hosts.
        _validate_schema_structure(parameters)
    except Exception as error:
        raise ToolRegistrationError(f"tool JSON Schema 无效: {error}") from error
    return normalized


def _validate_schema_structure(node: Mapping[str, Any], path: str = "parameters") -> None:
    if not isinstance(node, Mapping):
        raise ToolRegistrationError(f"{path} 必须是 object")
    allowed_types = {"object", "array", "string", "integer", "number", "boolean", "null"}
    if "type" in node and node["type"] not in allowed_types and not isinstance(node["type"], list):
        raise ToolRegistrationError(f"{path}.type 无效")
    if isinstance(node.get("properties"), Mapping):
        for key, child in node["properties"].items():
            if not isinstance(key, str):
                raise ToolRegistrationError(f"{path}.properties key 无效")
            _validate_schema_structure(child, f"{path}.properties.{key}")
    if isinstance(node.get("items"), Mapping):
        _validate_schema_structure(node["items"], f"{path}.items")
    if isinstance(node.get("additionalProperties"), Mapping):
        _validate_schema_structure(node["additionalProperties"], f"{path}.additionalProperties")


def validate_handler_contract(handler: Callable[..., Any], schema: Mapping[str, Any]) -> None:
    """Check the synchronous ``handler(conn, **declared_parameters)`` contract."""

    if not callable(handler) or inspect.iscoroutinefunction(handler):
        raise ToolRegistrationError("tool handler 必须是同步 callable")
    try:
        signature = inspect.signature(handler)
    except (TypeError, ValueError) as error:
        raise ToolRegistrationError("无法读取 tool handler signature") from error
    parameters = schema.get("parameters", {})
    properties = parameters.get("properties", {}) if isinstance(parameters, Mapping) else {}
    params = signature.parameters
    has_kwargs = any(item.kind is inspect.Parameter.VAR_KEYWORD for item in params.values())
    try:
        signature.bind_partial(object(), **{str(key): object() for key in properties})
    except TypeError as error:
        raise ToolRegistrationError(
            f"tool handler 无法接受 conn + schema 参数: {error}"
        ) from error
    if not has_kwargs:
        missing = sorted(set(properties) - set(params))
        if missing:
            raise ToolRegistrationError(
                f"tool handler 缺少 schema 参数: {', '.join(missing)}"
            )
    # A connection is part of the local registry contract.  A pure plugin may
    # accept only **kwargs; otherwise require a positional/keyword connection
    # slot so dispatch cannot silently pass the wrong object.
    connection_names = {"conn", "connection", "db", "db_conn"}
    positional = [
        item
        for item in params.values()
        if item.kind
        in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    ]
    if not positional and not any(name in params for name in connection_names):
        raise ToolRegistrationError("tool handler 必须接受 conn/connection 参数")
    declared = set(properties) | connection_names
    unexpected_required = [
        item.name
        for item in params.values()
        if item.default is inspect.Parameter.empty
        and item.kind not in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
        and item.name not in declared
        and (not positional or item is not positional[0])
        and not item.name.startswith("_")
    ]
    if unexpected_required:
        raise ToolRegistrationError(
            "tool handler 含 schema 未声明的必填参数: " + ", ".join(unexpected_required)
        )


def normalize_capability(value: Any) -> str | frozenset[str] | None:
    if value is None:
        return None
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        if not _CAPABILITY_IDENTIFIER.fullmatch(value):
            raise ToolRegistrationError(f"tool capability 不是稳定标识符: {value!r}")
        return value
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, Mapping)):
        values: set[str] = set()
        for item in value:
            if isinstance(item, Enum):
                rendered = str(item.value).strip()
            elif isinstance(item, str):
                rendered = item.strip()
            else:
                raise ToolRegistrationError("tool capability 集合只能包含字符串")
            if not rendered:
                continue
            if not _CAPABILITY_IDENTIFIER.fullmatch(rendered):
                raise ToolRegistrationError(f"tool capability 不是稳定标识符: {rendered!r}")
            values.add(rendered)
        values = frozenset(values)
        return values or None
    raise ToolRegistrationError("tool capability 必须是字符串或字符串集合")


def capability_set(value: Any) -> frozenset[str]:
    normalized = normalize_capability(value)
    if normalized is None:
        return frozenset()
    if isinstance(normalized, str):
        return frozenset({normalized})
    return normalized


def enabled_capability_set(value: Any) -> frozenset[str] | None:
    """Normalize a provider capability declaration into enabled labels."""

    if value is None:
        return None
    if isinstance(value, Mapping):
        labels: list[str] = []
        for key, enabled in value.items():
            if not isinstance(enabled, bool):
                raise ToolRegistrationError(
                    f"capability {key!r} 的开关必须是 bool"
                )
            if enabled:
                labels.append(str(key))
        return capability_set(labels)
    return capability_set(value)


def validate_metadata_combination(
    *,
    schema: Mapping[str, Any] | None = None,
    source: str,
    version: str,
    capability: Any,
    risk: ToolRisk | str,
    effect: ToolEffect | str,
    parallel_safe: bool,
    resource_keys: Iterable[str],
    timeout: float,
    allowed_roles: Iterable[str],
    mutation_parameters: Iterable[str] = (),
    category: str | None = None,
    mutating: bool = False,
) -> tuple[str, str, str | frozenset[str] | None, ToolRisk, ToolEffect, tuple[str, ...], float, frozenset[str]]:
    if category is not None and (
        not isinstance(category, str) or not category.strip() or len(category) > 64
    ):
        raise ToolRegistrationError("tool category 必须是非空字符串")
    if not isinstance(source, str) or not _SAFE_IDENTIFIER.fullmatch(source):
        raise ToolRegistrationError("tool source 必须是稳定标识符")
    if not isinstance(version, str) or not _VERSION_IDENTIFIER.fullmatch(version.strip()):
        raise ToolRegistrationError("tool version 必须是稳定标识符")
    parsed_risk = ToolRisk.parse(risk)
    parsed_effect = ToolEffect.parse(effect)
    caps = normalize_capability(capability)
    if not isinstance(parallel_safe, bool):
        raise ToolRegistrationError("parallel_safe 必须是 bool")
    raw_keys = tuple(resource_keys)
    if any(not isinstance(key, str) for key in raw_keys):
        raise ToolRegistrationError("resource_keys 必须是字符串集合")
    keys = raw_keys
    if len(set(keys)) != len(keys):
        raise ToolRegistrationError("resource_keys 不得重复")
    if any(
        not key
        or len(key) > 256
        or any(character.isspace() or ord(character) < 32 for character in key)
        or key.count("{") != key.count("}")
        for key in keys
    ):
        raise ToolRegistrationError("resource_keys 含无效规则")
    if schema is not None:
        _validate_resource_key_rules(schema, keys)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ToolRegistrationError("timeout 必须是正数")
    timeout = float(timeout)
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 24 * 60 * 60:
        raise ToolRegistrationError("timeout 必须在 (0, 86400] 内")
    raw_roles = tuple(allowed_roles)
    if any(not isinstance(role, str) for role in raw_roles):
        raise ToolRegistrationError("allowed_roles 必须是字符串集合")
    roles = frozenset(raw_roles)
    if not roles or not roles <= _KNOWN_ROLES:
        raise ToolRegistrationError("allowed_roles 为空或包含未知角色")
    raw_mutations = tuple(mutation_parameters)
    if any(not isinstance(item, str) for item in raw_mutations):
        raise ToolRegistrationError("mutation_parameters 必须是字符串集合")
    mutation_parameters = frozenset(raw_mutations)
    if any(not item or not _SAFE_IDENTIFIER.fullmatch(item) for item in mutation_parameters):
        raise ToolRegistrationError("mutation_parameters 含无效字段名")
    if mutating and parsed_effect in _EFFECTS_WITHOUT_SIDE_EFFECT:
        raise ToolRegistrationError("mutating 工具不能声明 read/pure effect")
    if parsed_effect in _EFFECTS_WITHOUT_SIDE_EFFECT and mutation_parameters:
        raise ToolRegistrationError("read/pure 工具不能声明 mutation_parameters")
    if parsed_effect is ToolEffect.CONDITIONAL_WRITE and not mutation_parameters:
        raise ToolRegistrationError("conditional_write 工具必须声明 mutation_parameters")
    if mutation_parameters and parsed_effect not in {
        ToolEffect.CONDITIONAL_WRITE,
        ToolEffect.WRITE,
    }:
        raise ToolRegistrationError("mutation_parameters 只能用于 write/conditional_write 工具")
    if parsed_effect is ToolEffect.WRITE and not mutation_parameters and parallel_safe:
        raise ToolRegistrationError("write 工具不能 parallel_safe")
    if parsed_effect in {
        ToolEffect.WRITE,
        ToolEffect.CONDITIONAL_WRITE,
        ToolEffect.CODE_EXECUTION,
        ToolEffect.APPROVAL,
        ToolEffect.INTERACTIVE,
        ToolEffect.UNKNOWN,
    } and parallel_safe:
        raise ToolRegistrationError("有副作用或未知 effect 的工具不能 parallel_safe")
    if parsed_effect is ToolEffect.CODE_EXECUTION and ToolCapability.CODE_EXECUTION.value not in capability_set(caps):
        raise ToolRegistrationError("code_execution effect 必须声明 code_execution capability")
    if parsed_effect is ToolEffect.UNKNOWN and parsed_risk is not ToolRisk.CRITICAL:
        raise ToolRegistrationError("unknown effect 工具必须使用 critical risk")
    if parsed_effect is ToolEffect.WRITE and parsed_risk is ToolRisk.LOW:
        raise ToolRegistrationError("write 工具风险不能为 low")
    return (
        source,
        version.strip(),
        caps,
        parsed_risk,
        parsed_effect,
        keys,
        timeout,
        roles,
    )


@dataclass(frozen=True)
class ToolManifestEntry:
    """One frozen, serializable tool declaration plus its in-process handler."""

    name: str
    schema: dict[str, Any]
    category: str = "unknown"
    source: str = "builtin"
    version: str = "1.0.0"
    schema_hash: str | None = None
    capability: str | frozenset[str] | None = None
    # A declaration without an explicit risk is treated as untrusted.  The
    # registry may provide a lower risk only when it declares it explicitly.
    risk: ToolRisk | str = ToolRisk.CRITICAL
    effect: ToolEffect | str = ToolEffect.UNKNOWN
    parallel_safe: bool = False
    resource_keys: tuple[str, ...] = field(default_factory=tuple)
    timeout: float = 30.0
    allowed_roles: frozenset[str] = field(default_factory=lambda: _KNOWN_ROLES)
    data_classification: Mapping[str, str] = field(default_factory=dict)
    handler: Callable[..., Any] | None = field(default=None, repr=False, compare=False)
    mutation_parameters: frozenset[str] = field(default_factory=frozenset, repr=False, compare=False)

    def __post_init__(self) -> None:
        normalized_schema = validate_function_schema(self.schema, name=self.name)
        if self.handler is not None:
            validate_handler_contract(self.handler, normalized_schema)
        object.__setattr__(self, "schema", _freeze_json(normalized_schema))
        canonical_hash = canonical_schema_hash(normalized_schema)
        if self.schema_hash is not None and self.schema_hash != canonical_hash:
            raise ToolRegistrationError(
                f"tool {self.name} schema_hash 与 canonical schema 不一致"
            )
        object.__setattr__(self, "schema_hash", canonical_hash)
        # Parse effect before deriving the mutating contract.  Comparing a raw
        # string such as ``"write"`` with the enum would otherwise silently
        # skip the side-effect checks below.
        parsed_effect = ToolEffect.parse(self.effect)
        source, version, capability, risk, effect, keys, timeout, roles = validate_metadata_combination(
            source=self.source,
            schema=normalized_schema,
            version=self.version,
            capability=self.capability,
            risk=self.risk,
            effect=parsed_effect,
            parallel_safe=self.parallel_safe,
            resource_keys=self.resource_keys,
            timeout=self.timeout,
            allowed_roles=self.allowed_roles,
            mutation_parameters=self.mutation_parameters,
            category=self.category,
            mutating=parsed_effect in {ToolEffect.WRITE, ToolEffect.CONDITIONAL_WRITE},
        )
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "capability", capability)
        object.__setattr__(self, "risk", risk)
        object.__setattr__(self, "effect", effect)
        object.__setattr__(self, "resource_keys", keys)
        object.__setattr__(self, "timeout", timeout)
        object.__setattr__(self, "allowed_roles", roles)
        object.__setattr__(self, "mutation_parameters", frozenset(str(item) for item in self.mutation_parameters))
        classifications = dict(self.data_classification or {})
        inferred = field_data_classification(normalized_schema)
        for pointer, category in inferred.items():
            classifications.setdefault(pointer, category)
        if any(not isinstance(key, str) for key in classifications):
            raise ToolRegistrationError("字段数据分类 key 必须是字符串 JSON Pointer")
        classifications = {
            (key if key.startswith("/") else f"/{key}"): (
                value.value if isinstance(value, Enum) else str(value)
            )
            for key, value in classifications.items()
        }
        valid_categories = {item.value for item in DataClass}
        for pointer, category in classifications.items():
            if not isinstance(pointer, str) or not pointer.startswith("/"):
                raise ToolRegistrationError("字段数据分类 key 必须是 JSON Pointer")
            if not isinstance(category, str) or category not in valid_categories:
                raise ToolRegistrationError(f"未知字段数据分类: {category!r}")
        _validate_schema_metadata_fields(
            normalized_schema,
            mutation_parameters=self.mutation_parameters,
            classifications=classifications,
        )
        object.__setattr__(self, "data_classification", MappingProxyType(classifications))

    @property
    def risk_level(self) -> str:
        return self.risk.value

    @property
    def canonical_schema_hash(self) -> str:
        return str(self.schema_hash)

    @property
    def mutating(self) -> bool:
        return self.effect in {ToolEffect.WRITE, ToolEffect.CONDITIONAL_WRITE}

    def is_mutating(self, arguments: Mapping[str, Any] | None = None) -> bool:
        if self.effect is ToolEffect.WRITE:
            return True
        if self.effect is ToolEffect.CONDITIONAL_WRITE:
            arguments = arguments or {}
            return any(arguments.get(parameter) for parameter in self.mutation_parameters)
        return False

    @property
    def capabilities(self) -> frozenset[str]:
        return capability_set(self.capability)

    @property
    def field_data_classification(self) -> Mapping[str, str]:
        return self.data_classification

    @property
    def resource_key_rules(self) -> tuple[str, ...]:
        return self.resource_keys

    def to_dict(self, *, include_schema: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "category": self.category,
            "source": self.source,
            "version": self.version,
            "schema_hash": self.schema_hash,
            "capability": (
                self.capability
                if isinstance(self.capability, str) or self.capability is None
                else sorted(self.capability)
            ),
            "risk": self.risk.value,
            "effect": self.effect.value,
            "parallel_safe": self.parallel_safe,
            "resource_keys": list(self.resource_keys),
            "timeout": self.timeout,
            "allowed_roles": sorted(self.allowed_roles),
            "data_classification": dict(sorted(self.data_classification.items())),
            "mutation_parameters": sorted(self.mutation_parameters),
        }
        if include_schema:
            result["schema"] = _thaw_json(self.schema)
        return result

    def to_openai_tool(self) -> dict[str, Any]:
        return {"type": "function", "function": _thaw_json(self.schema)}

    def resource_keys_for(self, arguments: Mapping[str, Any] | None = None, *, context: Any = None) -> frozenset[str]:
        """Resolve simple JSON-pointer/resource templates deterministically."""

        arguments = arguments or {}
        result: set[str] = set()
        for rule in self.resource_keys:
            if rule.startswith("/"):
                current: Any = arguments
                found = True
                for token in rule[1:].split("/"):
                    token = token.replace("~1", "/").replace("~0", "~")
                    if isinstance(current, Mapping) and token in current:
                        current = current[token]
                    else:
                        found = False
                        break
                if found and current is not None:
                    if isinstance(current, (list, tuple, set, frozenset)):
                        result.update(f"{rule}={item}" for item in current)
                    else:
                        result.add(f"{rule}={current}")
                else:
                    result.add(rule)
            elif "{" in rule and "}" in rule:
                values = {
                    "tenant_id": getattr(context, "tenant_id", "") if context is not None else "",
                    "actor_id": getattr(context, "actor_id", "") if context is not None else "",
                    "run_id": getattr(context, "run_id", "") if context is not None else "",
                }
                try:
                    # Run identity is authoritative even if a tool schema
                    # happens to accept similarly named input fields.
                    format_values = {**dict(arguments), **values}
                    result.add(rule.format(**format_values))
                except (KeyError, ValueError):
                    result.add(rule)
            else:
                result.add(rule)
        return frozenset(result)


def manifest_entry_from_spec(spec: Any) -> ToolManifestEntry | None:
    """Normalize a live provider declaration for frozen-identity checks."""

    if isinstance(spec, ToolManifestEntry):
        return spec
    factory = getattr(spec, "to_manifest_entry", None)
    if not callable(factory):
        return None
    entry = factory()
    return entry if isinstance(entry, ToolManifestEntry) else None


def manifest_entry_matches(entry: ToolManifestEntry, current: Any) -> bool:
    """Compare every frozen metadata field and the in-process handler identity."""

    live = manifest_entry_from_spec(current)
    if live is None or entry.to_dict() != live.to_dict():
        return False
    return entry.handler is None or live.handler is entry.handler


@dataclass(frozen=True)
class ToolManifest:
    """Immutable ordered tool surface frozen at run start."""

    entries: tuple[ToolManifestEntry, ...] = field(default_factory=tuple)
    actor_id: str | None = field(default=None, repr=False, compare=False)
    tenant_id: str | None = field(default=None, repr=False, compare=False)
    role: str | None = field(default=None, repr=False, compare=False)
    course_ids: frozenset[int] = field(default_factory=frozenset, repr=False, compare=False)
    manifest_hash: str = field(init=False)

    def __post_init__(self) -> None:
        entries = tuple(self.entries)
        names: set[str] = set()
        for entry in entries:
            if not isinstance(entry, ToolManifestEntry):
                raise ToolManifestError("manifest entries 必须是 ToolManifestEntry")
            if entry.name in names:
                raise ToolManifestError(f"manifest 含重复工具: {entry.name}")
            names.add(entry.name)
        object.__setattr__(self, "entries", entries)
        object.__setattr__(self, "course_ids", frozenset(int(item) for item in self.course_ids))
        payload = {
            "actor_id": self.actor_id,
            "tenant_id": self.tenant_id,
            "role": self.role,
            "course_ids": sorted(self.course_ids),
            "entries": [entry.to_dict() for entry in sorted(entries, key=lambda item: item.name)],
        }
        object.__setattr__(self, "manifest_hash", hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest())

    @property
    def hash(self) -> str:
        return self.manifest_hash

    @property
    def tool_manifest_hash(self) -> str:
        return self.manifest_hash

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self.entries)

    def __iter__(self):
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def get(self, name: str) -> ToolManifestEntry | None:
        return next((entry for entry in self.entries if entry.name == name), None)

    def contains(self, name: str) -> bool:
        return self.get(name) is not None

    def matches_context(self, context: Any) -> bool:
        """Return whether a live context still has the frozen run identity."""

        if context is None:
            return True
        if self.actor_id is not None and getattr(context, "actor_id", None) != self.actor_id:
            return False
        if self.tenant_id is not None and getattr(context, "tenant_id", None) != self.tenant_id:
            return False
        if self.role is not None and getattr(context, "role", None) != self.role:
            return False
        # A manifest produced for a concrete run binds the course set exactly,
        # including an intentionally empty set.  Standalone legacy manifests
        # (with no actor/tenant/role identity) remain unrestricted.
        context_courses = frozenset(getattr(context, "course_ids", ()))
        scope_bound = any(
            value is not None for value in (self.actor_id, self.tenant_id, self.role)
        )
        return not scope_bound or context_courses == self.course_ids

    def restrict(self, allowed_tools: Iterable[str] | None) -> "ToolManifest":
        if allowed_tools is None:
            return self
        allowed = set(allowed_tools)
        return ToolManifest(
            tuple(entry for entry in self.entries if entry.name in allowed),
            actor_id=self.actor_id,
            tenant_id=self.tenant_id,
            role=self.role,
            course_ids=self.course_ids,
        )

    def to_openai_tools(self) -> list[dict[str, Any]]:
        return [entry.to_openai_tool() for entry in self.entries]

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_hash": self.manifest_hash,
            "actor_id": self.actor_id,
            "tenant_id": self.tenant_id,
            "role": self.role,
            "course_ids": sorted(self.course_ids),
            "entries": [entry.to_dict() for entry in self.entries],
        }


def manifest_from_tools(
    tools: Iterable[Mapping[str, Any]],
    *,
    specs: Mapping[str, Any] | None = None,
    source: str = "provider",
    default_capability: str | None = None,
    actor_id: str | None = None,
    tenant_id: str | None = None,
    role: str | None = None,
    course_ids: Iterable[int] = (),
) -> ToolManifest:
    """Build a manifest from an arbitrary provider's OpenAI tool list.

    Providers that expose richer ``ToolSpec`` objects should pass ``specs``;
    bare MCP/remote schemas receive conservative unknown-plugin metadata.
    """

    entries: list[ToolManifestEntry] = []
    for item in tools:
        function = item.get("function") if isinstance(item, Mapping) else None
        if not isinstance(function, Mapping):
            raise ToolRegistrationError("provider tool 缺少 function schema")
        name = function.get("name")
        spec = specs.get(name) if specs is not None else None
        if spec is not None and hasattr(spec, "to_manifest_entry"):
            try:
                entry = spec.to_manifest_entry()
                normalized_function = validate_function_schema(function, name=str(name))
                if canonical_schema_hash(normalized_function) != entry.canonical_schema_hash:
                    raise ToolRegistrationError(
                        f"provider 工具 {name} 的 schema 与 ToolSpec 不一致"
                    )
                # Legacy in-process providers historically used bare ToolSpec
                # objects (source=builtin) without capabilities.  Preserve
                # that compatibility, while never exposing an explicitly
                # unknown plugin declaration to a model.
                if entry.capability is None and entry.source.startswith("plugin:"):
                    continue
                entries.append(entry)
                continue
            except (ToolRegistrationError, TypeError, ValueError):
                if str(getattr(spec, "source", "")).startswith("plugin:"):
                    # A malformed or incomplete plugin declaration is not
                    # model-visible merely because its provider returned a
                    # callable/schema pair.
                    continue
                # A legacy/fake provider may expose only the pre-R3 fields.
                # Preserve its callable surface with the conservative plugin
                # defaults; real registry/plugin registration is still strict.
                entries.append(
                    ToolManifestEntry(
                        name=str(name),
                        schema=dict(function),
                        category="unknown",
                        source=getattr(spec, "source", source),
                        version=getattr(spec, "version", "0.0.0"),
                        capability=ToolCapability.TOOL_CALLING.value,
                        risk=ToolRisk.CRITICAL,
                        effect=ToolEffect.UNKNOWN,
                        parallel_safe=False,
                        allowed_roles=_KNOWN_ROLES,
                        handler=getattr(spec, "handler", None),
                    )
                )
                continue
        if default_capability is not None:
            entries.append(
                ToolManifestEntry(
                    name=str(name),
                    schema=dict(function),
                    source=source,
                    capability=default_capability,
                    risk=ToolRisk.CRITICAL,
                    effect=ToolEffect.UNKNOWN,
                    parallel_safe=False,
                    allowed_roles=_KNOWN_ROLES,
                )
            )
    return ToolManifest(
        tuple(entries),
        actor_id=actor_id,
        tenant_id=tenant_id,
        role=role,
        course_ids=frozenset(course_ids),
    )


__all__ = [
    "ToolCapability",
    "ToolEffect",
    "ToolManifest",
    "ToolManifestEntry",
    "ToolManifestError",
    "ToolManifestMismatch",
    "ToolRegistrationError",
    "ToolRisk",
    "canonical_json",
    "canonical_schema",
    "canonical_schema_hash",
    "capability_set",
    "enabled_capability_set",
    "field_data_classification",
    "manifest_from_tools",
    "manifest_entry_from_spec",
    "manifest_entry_matches",
    "normalize_capability",
    "schema_hash",
    "validate_function_schema",
    "validate_handler_contract",
    "validate_metadata_combination",
]
