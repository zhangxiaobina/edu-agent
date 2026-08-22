"""工具注册表：name → (schema, callable)，统一 dispatch，供 Agent 编排层调用。"""
from __future__ import annotations

import copy
import hashlib
import sqlite3
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Callable, Iterable, Mapping

from ..data import db
from ..state import FencingTokenRejected, RunCancelled
from ..runtime.cancellation import CancellationRequested
from . import ai_tools, analysis_tools, kg_tools, ops_tools, query_tools
from .manifest import (
    ToolCapability,
    ToolEffect,
    ToolManifest,
    ToolManifestEntry,
    ToolRegistrationError,
    ToolRisk,
    canonical_json,
    canonical_schema_hash,
    capability_set,
    enabled_capability_set,
    normalize_capability,
    field_data_classification,
    manifest_entry_matches,
    validate_function_schema,
    validate_handler_contract,
    validate_metadata_combination,
    _validate_schema_metadata_fields,
)
from .schemas import SCHEMA_BY_NAME

_code_execution_provider = None
_REGISTRY_GENERATION = 0


def configure_code_execution(provider) -> None:
    global _code_execution_provider
    _code_execution_provider = provider


def code_execution_provider():
    return _code_execution_provider


def code_execution_health():
    if _code_execution_provider is None:
        return None
    try:
        return _code_execution_provider.health_check()
    except Exception:
        # A health probe is an availability gate.  Probe failures must hide
        # the capability rather than make a run expose an unverified tool.
        return None


def code_execution_available() -> bool:
    try:
        health = code_execution_health()
        if health is None or not health.healthy:
            return False
        capabilities = health.capabilities
        return bool(
            capabilities.trusted_isolation
            and capabilities.supports_health_check
            and capabilities.supports_wall_time
            and capabilities.supports_cpu_time
            and capabilities.supports_memory
            and capabilities.supports_process_limit
            and capabilities.supports_file_size_limit
            and capabilities.supports_output_limit
            and capabilities.supports_network_policy
            and capabilities.supports_cancellation
        )
    except Exception:
        return False


@dataclass(frozen=True)
class ToolSpec:
    schema: dict
    handler: Callable
    category: str
    risk_level: str = "low"
    risk: ToolRisk | str | None = None
    mutating: bool = False
    mutation_parameters: frozenset[str] = field(default_factory=frozenset)
    allowed_roles: frozenset[str] = field(
        default_factory=lambda: frozenset({"student", "teacher", "admin", "system"})
    )
    # R3.1 metadata.  Defaults preserve the pre-R3 constructor contract for
    # test/fake providers; registry admission still validates every field.
    source: str = "builtin"
    version: str = "1.0.0"
    schema_hash: str | None = None
    capability: str | frozenset[str] | None = None
    effect: ToolEffect | str | None = None
    parallel_safe: bool = False
    resource_keys: tuple[str, ...] = field(default_factory=tuple)
    timeout: float = 30.0
    data_classification: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Keep legacy hand-built ToolSpec instances usable while making their
        # metadata deterministic when they are later admitted to a manifest.
        schema_name = self.schema.get("name") if isinstance(self.schema, Mapping) else None
        normalized_schema = validate_function_schema(self.schema, name=schema_name)
        object.__setattr__(self, "schema", normalized_schema)
        object.__setattr__(self, "allowed_roles", frozenset(self.allowed_roles))
        object.__setattr__(self, "mutation_parameters", frozenset(self.mutation_parameters))
        resolved_risk = ToolRisk.parse(self.risk if self.risk is not None else self.risk_level)
        object.__setattr__(self, "risk_level", resolved_risk.value)
        object.__setattr__(self, "risk", resolved_risk)
        if self.schema_hash is None:
            object.__setattr__(self, "schema_hash", canonical_schema_hash(normalized_schema))
        elif self.schema_hash != canonical_schema_hash(normalized_schema):
            raise ToolRegistrationError(f"tool {self.schema['name']} schema_hash 不匹配")
        inferred_effect = self.effect
        if inferred_effect is None:
            inferred_effect = (
                ToolEffect.WRITE
                if self.mutating
                else ToolEffect.CONDITIONAL_WRITE
                if self.mutation_parameters
                else ToolEffect.READ
            )
        object.__setattr__(self, "effect", ToolEffect.parse(inferred_effect))
        object.__setattr__(self, "resource_keys", tuple(self.resource_keys))
        object.__setattr__(self, "mutation_parameters", frozenset(self.mutation_parameters))
        object.__setattr__(self, "capability", normalize_capability(self.capability))
        classifications = field_data_classification(normalized_schema)
        classifications.update(dict(self.data_classification or {}))
        _validate_schema_metadata_fields(
            normalized_schema,
            mutation_parameters=self.mutation_parameters,
            classifications=classifications,
        )
        object.__setattr__(self, "data_classification", MappingProxyType(classifications))

    def is_mutating(self, arguments: dict) -> bool:
        if self.mutating or self.effect is ToolEffect.WRITE:
            return True
        return any(arguments.get(parameter) for parameter in self.mutation_parameters)

    @property
    def canonical_schema_hash(self) -> str:
        return str(self.schema_hash)

    @property
    def capabilities(self) -> frozenset[str]:
        return capability_set(self.capability)

    @property
    def resource_key_rules(self) -> tuple[str, ...]:
        return self.resource_keys

    @property
    def field_data_classification(self) -> Mapping[str, str]:
        return self.data_classification

    def to_manifest_entry(self) -> ToolManifestEntry:
        kwargs = {
            "name": self.schema["name"],
            "schema": self.schema,
            "category": self.category,
            "source": self.source,
            "version": self.version,
            "schema_hash": self.schema_hash,
            "capability": self.capability,
            "risk": self.risk,
            "effect": self.effect or ToolEffect.UNKNOWN,
            "parallel_safe": self.parallel_safe,
            "resource_keys": self.resource_keys,
            "timeout": self.timeout,
            "allowed_roles": self.allowed_roles,
            "data_classification": self.data_classification,
            "handler": self.handler,
            "mutation_parameters": self.mutation_parameters,
        }
        try:
            return ToolManifestEntry(**kwargs)
        except (ToolRegistrationError, TypeError, ValueError):
            # Legacy/fake providers predate R3 metadata validation.  Keep
            # their callable surface available with conservative identity;
            # registry.register_tool itself remains strict via _validate_spec.
            fallback_source = (
                self.source
                if isinstance(self.source, str) and self.source.startswith("plugin:")
                else "provider"
            )
            return ToolManifestEntry(
                name=self.schema["name"],
                schema=self.schema,
                category="unknown",
                source=fallback_source,
                version="0.0.0",
                capability=(
                    None
                    if fallback_source.startswith("plugin:")
                    else ToolCapability.TOOL_CALLING.value
                ),
                risk=ToolRisk.CRITICAL,
                effect=ToolEffect.UNKNOWN,
                parallel_safe=False,
                resource_keys=(),
                timeout=30.0,
                allowed_roles=_ALL_ROLES,
                handler=None,
            )


def _strict_schema(name: str) -> dict:
    schema = copy.deepcopy(SCHEMA_BY_NAME[name])
    schema["parameters"].setdefault("additionalProperties", False)
    return schema

# 工具名 → 实现函数（签名均为 fn(conn, **params)）
TOOL_FUNCTIONS = {
    # 查询
    "query_student_scores": query_tools.query_student_scores,
    "list_exams": query_tools.list_exams,
    "get_class_roster": query_tools.get_class_roster,
    "search_questions": query_tools.search_questions,
    "get_learning_progress": query_tools.get_learning_progress,
    # 知识图谱
    "query_knowledge_graph": kg_tools.query_knowledge_graph,
    "recommend_study_path": kg_tools.recommend_study_path,
    # 分析
    "analyze_class_errors": analysis_tools.analyze_class_errors,
    "diagnose_weak_points": analysis_tools.diagnose_weak_points,
    "get_score_distribution": analysis_tools.get_score_distribution,
    # 操作
    "create_exam": ops_tools.create_exam,
    "generate_paper": ops_tools.generate_paper,
    "batch_grade": ops_tools.batch_grade,
    "assign_homework": ops_tools.assign_homework,
    # AI / 执行
    "generate_questions": ai_tools.generate_questions,
    "run_code": ai_tools.run_code,
}

_ALL_ROLES = frozenset({"student", "teacher", "admin", "system"})
_STAFF_ROLES = frozenset({"teacher", "admin", "system"})
_ADMIN_ROLES = frozenset({"admin", "system"})

_TOOL_METADATA = {
    "query_student_scores": {
        "category": "query", "risk": "medium", "effect": ToolEffect.READ,
        "capability": ToolCapability.TEACHING_QUERY.value,
        "parallel_safe": True, "resource_keys": ("/exam_id", "/student_id", "/class_id"),
        "roles": _STAFF_ROLES,
    },
    "list_exams": {
        "category": "query", "risk": "low", "effect": ToolEffect.READ,
        "capability": ToolCapability.TEACHING_QUERY.value,
        "parallel_safe": True, "resource_keys": ("/class_id", "/course_id"),
        "roles": _ALL_ROLES,
    },
    "get_class_roster": {
        "category": "query", "risk": "medium", "effect": ToolEffect.READ,
        "capability": ToolCapability.TEACHING_QUERY.value,
        "parallel_safe": True, "resource_keys": ("/class_id",), "roles": _STAFF_ROLES,
    },
    "search_questions": {
        "category": "query", "risk": "low", "effect": ToolEffect.READ,
        "capability": ToolCapability.TEACHING_QUERY.value,
        "parallel_safe": True, "resource_keys": ("/question_bank_id", "/course_id"),
        "roles": _ALL_ROLES,
    },
    "get_learning_progress": {
        "category": "query", "risk": "medium", "effect": ToolEffect.READ,
        "capability": ToolCapability.TEACHING_QUERY.value,
        "parallel_safe": True, "resource_keys": ("/student_id", "/course_id"),
        "roles": _STAFF_ROLES,
    },
    "query_knowledge_graph": {
        "category": "knowledge", "risk": "low", "effect": ToolEffect.READ,
        "capability": ToolCapability.TEACHING_KNOWLEDGE.value,
        "parallel_safe": True, "resource_keys": ("/course_id",), "roles": _ALL_ROLES,
    },
    "recommend_study_path": {
        "category": "knowledge", "risk": "medium", "effect": ToolEffect.READ,
        "capability": ToolCapability.TEACHING_KNOWLEDGE.value,
        "parallel_safe": True, "resource_keys": ("/student_id", "/course_id"),
        "roles": _STAFF_ROLES,
    },
    "analyze_class_errors": {
        "category": "analysis", "risk": "medium", "effect": ToolEffect.READ,
        "capability": ToolCapability.TEACHING_ANALYSIS.value,
        "parallel_safe": True, "resource_keys": ("/exam_id", "/class_id"),
        "roles": _STAFF_ROLES,
    },
    "diagnose_weak_points": {
        "category": "analysis", "risk": "medium", "effect": ToolEffect.READ,
        "capability": ToolCapability.TEACHING_ANALYSIS.value,
        "parallel_safe": True, "resource_keys": ("/student_id", "/class_id", "/course_id"),
        "roles": _STAFF_ROLES,
    },
    "get_score_distribution": {
        "category": "analysis", "risk": "medium", "effect": ToolEffect.READ,
        "capability": ToolCapability.TEACHING_ANALYSIS.value,
        "parallel_safe": True, "resource_keys": ("/exam_id",), "roles": _STAFF_ROLES,
    },
    "create_exam": {
        "category": "operation", "risk": "high", "effect": ToolEffect.WRITE,
        "capability": ToolCapability.TEACHING_WRITE.value,
        "parallel_safe": False, "resource_keys": ("/class_id", "/course_id", "/question_bank_id"),
        "roles": _STAFF_ROLES, "mutating": True, "timeout": 60.0,
    },
    "generate_paper": {
        "category": "operation", "risk": "medium", "effect": ToolEffect.READ,
        "capability": ToolCapability.TEACHING_QUERY.value,
        "parallel_safe": True, "resource_keys": ("/question_bank_id",),
        "roles": _STAFF_ROLES, "timeout": 60.0,
    },
    "batch_grade": {
        "category": "operation", "risk": "critical", "effect": ToolEffect.WRITE,
        "capability": ToolCapability.TEACHING_WRITE.value,
        "parallel_safe": False, "resource_keys": ("/exam_id",), "roles": _ADMIN_ROLES,
        "mutating": True, "timeout": 120.0,
    },
    "assign_homework": {
        "category": "operation", "risk": "high", "effect": ToolEffect.WRITE,
        "capability": ToolCapability.TEACHING_WRITE.value,
        "parallel_safe": False, "resource_keys": ("/course_id", "/class_ids"),
        "roles": _STAFF_ROLES, "mutating": True, "timeout": 60.0,
    },
    "generate_questions": {
        "category": "content", "risk": "high", "effect": ToolEffect.CONDITIONAL_WRITE,
        "capability": ToolCapability.TEACHING_CONTENT.value,
        "parallel_safe": False, "resource_keys": ("/course_id", "/save_to_bank"),
        "roles": _STAFF_ROLES, "mutation_parameters": ("save_to_bank",), "timeout": 120.0,
    },
    "run_code": {
        "category": "execution", "risk": "critical", "effect": ToolEffect.CODE_EXECUTION,
        "capability": ToolCapability.CODE_EXECUTION.value,
        "parallel_safe": False, "resource_keys": ("sandbox:{tenant_id}:{actor_id}",),
        "roles": _ALL_ROLES, "timeout": 20.0,
    },
}

TOOL_SPECS = {
    name: ToolSpec(
        schema=_strict_schema(name),
        handler=handler,
        category=metadata["category"],
        risk_level=metadata["risk"],
        mutating=metadata.get("mutating", False),
        mutation_parameters=frozenset(metadata.get("mutation_parameters", ())),
        allowed_roles=metadata["roles"],
        source="builtin:edu_agent.tools",
        version="1.0.0",
        capability=metadata["capability"],
        effect=metadata["effect"],
        parallel_safe=metadata["parallel_safe"],
        resource_keys=metadata["resource_keys"],
        timeout=metadata.get("timeout", 30.0),
    )
    for name, handler in TOOL_FUNCTIONS.items()
    for metadata in [_TOOL_METADATA[name]]
}


def _validate_spec(spec: ToolSpec) -> None:
    validate_handler_contract(spec.handler, spec.schema)
    validate_metadata_combination(
        schema=spec.schema,
        source=spec.source,
        version=spec.version,
        capability=spec.capability,
        risk=spec.risk_level,
        effect=spec.effect or ToolEffect.UNKNOWN,
        parallel_safe=spec.parallel_safe,
        resource_keys=spec.resource_keys,
        timeout=spec.timeout,
        allowed_roles=spec.allowed_roles,
        mutation_parameters=spec.mutation_parameters,
        category=spec.category,
        mutating=spec.mutating,
    )
    if spec.effect is ToolEffect.CODE_EXECUTION and spec.schema["name"] != "run_code":
        raise ToolRegistrationError(
            "code_execution effect 只能由受控 run_code provider 提供"
        )
    if spec.effect is ToolEffect.CODE_EXECUTION and spec.source.startswith("plugin:"):
        raise ToolRegistrationError(
            "插件不能直接注册 code_execution 工具；必须接入受控执行 provider"
        )
    if spec.effect in {ToolEffect.WRITE, ToolEffect.CONDITIONAL_WRITE} and spec.source.startswith("plugin:"):
        raise ToolRegistrationError(
            "插件写工具必须接入受控事务适配器，不能注册裸连接 handler"
        )
    # Constructing the entry here applies the field-classification and frozen
    # schema/hash checks during registration, before either registry map is
    # published.  ``to_manifest_entry`` has a legacy fallback for test
    # providers; registration deliberately does not use that fallback.
    ToolManifestEntry(
        name=spec.schema["name"],
        schema=spec.schema,
        category=spec.category,
        source=spec.source,
        version=spec.version,
        schema_hash=spec.schema_hash,
        capability=spec.capability,
        risk=spec.risk,
        effect=spec.effect or ToolEffect.UNKNOWN,
        parallel_safe=spec.parallel_safe,
        resource_keys=spec.resource_keys,
        timeout=spec.timeout,
        allowed_roles=spec.allowed_roles,
        data_classification=spec.data_classification,
        handler=spec.handler,
        mutation_parameters=spec.mutation_parameters,
    )


for _builtin_spec in TOOL_SPECS.values():
    _validate_spec(_builtin_spec)

# 完整性自检：schema 与实现一一对应
_missing_fn = set(SCHEMA_BY_NAME) - set(TOOL_FUNCTIONS)
_missing_schema = set(TOOL_FUNCTIONS) - set(SCHEMA_BY_NAME)
assert not _missing_fn, f"缺少实现的工具: {_missing_fn}"
assert not _missing_schema, f"缺少 schema 的工具: {_missing_schema}"


def openai_tools(
    *,
    role: str | None = None,
    categories: set[str] | None = None,
    allow_local_code_execution: bool = False,
    model_capabilities: Mapping[str, object] | None = None,
    capabilities: Mapping[str, object] | Iterable[str] | None = None,
) -> list[dict]:
    """返回当前角色和能力边界内可用的 OpenAI tools。"""
    return build_tool_manifest(
        role=role,
        categories=categories,
        allow_local_code_execution=allow_local_code_execution,
        model_capabilities=model_capabilities,
        enabled_capabilities=capabilities,
    ).to_openai_tools()


def registry_generation() -> int:
    return _REGISTRY_GENERATION


def manifest_entries() -> tuple[ToolManifestEntry, ...]:
    """Return a validated snapshot of all declared registry entries."""

    return tuple(spec.to_manifest_entry() for spec in TOOL_SPECS.values())


def build_tool_manifest(
    *,
    context=None,
    role: str | None = None,
    categories: set[str] | None = None,
    allow_local_code_execution: bool = False,
    model_tool_calling: bool = True,
    model_capabilities: Mapping[str, object] | None = None,
    enabled_capabilities: Iterable[str] | None = None,
    capabilities: Iterable[str] | None = None,
) -> ToolManifest:
    """Build the current policy-filtered surface for one run.

    The returned object owns deep-copied schemas.  Later plugin registration or
    health changes therefore cannot mutate the surface already handed to the
    model.  Health and ACL are still checked again by the executor.
    """

    selected: list[ToolManifestEntry] = []
    if model_capabilities is not None:
        capability_mapping = model_capabilities
        if not isinstance(capability_mapping, Mapping):
            to_event = getattr(capability_mapping, "to_event", None)
            capability_mapping = to_event() if callable(to_event) else {
                name: getattr(capability_mapping, name)
                for name in ("tool_calling", "structured_output", "usage", "streaming")
                if hasattr(capability_mapping, name)
            }
        if not isinstance(capability_mapping, Mapping):
            raise ToolRegistrationError("model_capabilities 必须是 mapping 或 capability object")
        declared_tool_calling = capability_mapping.get("tool_calling", model_tool_calling)
        if isinstance(declared_tool_calling, bool):
            model_tool_calling = declared_tool_calling
        elif declared_tool_calling is not None:
            raise ToolRegistrationError("model capability tool_calling 必须是 bool")
    effective_role = role if role is not None else getattr(context, "role", None)
    if enabled_capabilities is not None and capabilities is not None:
        raise ToolRegistrationError("enabled_capabilities 与 capabilities 不能同时声明")
    declared_capabilities = (
        enabled_capabilities if enabled_capabilities is not None else capabilities
    )
    available = (
        set(enabled_capability_set(declared_capabilities) or ())
        if declared_capabilities is not None
        else None
    )
    if model_tool_calling:
        for spec in tuple(TOOL_SPECS.values()):
            if spec.capability is None:
                # Undeclared plugin capabilities are never model-visible.
                continue
            if effective_role is not None and effective_role not in spec.allowed_roles:
                continue
            if categories is not None and spec.category not in categories:
                continue
            if available is not None and not _capability_enabled(spec.capabilities, available):
                continue
            if spec.effect is ToolEffect.CODE_EXECUTION:
                if not allow_local_code_execution or not code_execution_available():
                    continue
            selected.append(spec.to_manifest_entry())
    return ToolManifest(
        tuple(selected),
        actor_id=getattr(context, "actor_id", None),
        tenant_id=getattr(context, "tenant_id", None),
        role=effective_role,
        course_ids=getattr(context, "course_ids", frozenset()),
    )


tool_manifest = build_tool_manifest
freeze_manifest = build_tool_manifest
current_manifest = build_tool_manifest


def manifest_hash(manifest_or_tools) -> str:
    """Hash a frozen manifest or preserve the R2 schema-list hash for callers."""

    if isinstance(manifest_or_tools, ToolManifest):
        return manifest_or_tools.manifest_hash
    try:
        tools = [dict(item) for item in manifest_or_tools]
    except (TypeError, ValueError) as error:
        raise ToolRegistrationError("tools 必须是可迭代的 OpenAI tool 列表") from error
    return hashlib.sha256(canonical_json(tools).encode("utf-8")).hexdigest()


def _capability_enabled(required: frozenset[str], available: set[str]) -> bool:
    """Match provider/runtime capability labels without conflating model tool calling."""

    if not required:
        return False
    if "*" in available or "tool_calling" in available:
        return True
    return required <= available


def get_manifest_entry(name: str) -> ToolManifestEntry | None:
    spec = get_spec(name)
    return spec.to_manifest_entry() if spec is not None else None


def tool_available(name: str, context=None) -> bool:
    """Live health check used by the executor after manifest admission."""

    if name not in TOOL_SPECS:
        return False
    spec = TOOL_SPECS.get(name)
    if spec is not None and spec.effect is ToolEffect.CODE_EXECUTION:
        return code_execution_available()
    return spec is not None


def dispatch(name: str, arguments: dict | None = None,
             conn: sqlite3.Connection | None = None,
             *, manifest: ToolManifest | None = None) -> dict:
    """按名调用工具。conn 为空则自动打开/关闭合成库连接。"""
    if manifest is not None and not manifest.contains(name):
        return {"error": "工具不在本 run 冻结的 manifest 中"}
    if name not in TOOL_FUNCTIONS:
        return {"error": f"未知工具：{name}"}
    spec = TOOL_SPECS.get(name)
    if spec is None:
        return {"error": "工具 registry 元数据缺失"}
    if manifest is not None:
        entry = manifest.get(name)
        current = TOOL_SPECS.get(name)
        if (
            entry is None
            or current is None
            or not manifest_entry_matches(entry, current)
            or (
                entry.handler is not None
                and TOOL_FUNCTIONS.get(name) is not entry.handler
            )
        ):
            return {"error": "工具 registry 在 run 内发生变化，manifest 身份不匹配"}
    arguments = arguments or {}
    own = conn is None
    conn = conn or db.connect()
    try:
        if spec.effect is ToolEffect.CODE_EXECUTION and not code_execution_available():
            result = {"error": "代码执行后端未通过健康与安全能力门禁"}
        elif spec.effect is ToolEffect.CODE_EXECUTION:
            result = ai_tools.run_code(conn, _provider=_code_execution_provider, **arguments)
        else:
            handler = manifest.get(name).handler if manifest is not None else TOOL_FUNCTIONS[name]
            if handler is None:
                handler = TOOL_FUNCTIONS[name]
            result = handler(conn, **arguments)
        if own:
            conn.commit()
        return result
    except TypeError as e:
        if own:
            conn.rollback()
        return {"error": f"参数错误：{e}"}
    except Exception as e:
        if own:
            conn.rollback()
        return {"error": f"工具执行异常：{type(e).__name__}: {e}"}
    finally:
        if own:
            conn.close()


def dispatch_with_context(
    name: str,
    arguments: dict | None,
    context,
    conn=None,
    *,
    manifest: ToolManifest | None = None,
) -> dict:
    # Context-aware execution (currently only run_code) must apply the same
    # frozen-manifest identity checks as the ordinary dispatch path.
    if manifest is not None:
        if not manifest.contains(name):
            return {"error": "工具不在本 run 冻结的 manifest 中"}
        entry = manifest.get(name)
        current = TOOL_SPECS.get(name)
        if (
            name not in TOOL_FUNCTIONS
            or current is None
            or entry is None
            or not manifest_entry_matches(entry, current)
            or (
                entry is not None
                and entry.handler is not None
                and TOOL_FUNCTIONS.get(name) is not entry.handler
            )
        ):
            return {"error": "工具 registry 在 run 内发生变化，manifest 身份不匹配"}
    elif name not in TOOL_FUNCTIONS:
        return {"error": f"未知工具：{name}"}
    spec = TOOL_SPECS.get(name)
    if spec is None:
        return {"error": "工具 registry 元数据缺失"}
    if spec.effect is not ToolEffect.CODE_EXECUTION:
        return dispatch(name, arguments, conn=conn, manifest=manifest)
    arguments = arguments or {}
    if not code_execution_available():
        return {"error": "代码执行后端未通过健康与安全能力门禁"}
    own = conn is None
    connection = conn or db.connect()
    try:
        result = ai_tools.run_code(
            connection, _provider=_code_execution_provider, _context=context, **arguments,
        )
        if own:
            connection.commit()
        return result
    except (FencingTokenRejected, RunCancelled, CancellationRequested):
        raise
    except Exception as error:
        if own:
            connection.rollback()
        return {"error": f"工具执行异常：{type(error).__name__}: {error}"}
    finally:
        if own:
            connection.close()


def tool_names() -> list[str]:
    return list(TOOL_FUNCTIONS)


def get_spec(name: str) -> ToolSpec | None:
    return TOOL_SPECS.get(name)


def register_tool(
    *,
    name: str,
    schema: dict,
    handler: Callable,
    category: str,
    source: str = "plugin:unknown",
    version: str = "0.0.0",
    capability: str | Iterable[str] | None = None,
    risk: ToolRisk | str | None = None,
    risk_level: str | None = None,
    effect: ToolEffect | str = ToolEffect.UNKNOWN,
    parallel_safe: bool = False,
    resource_keys: Iterable[str] | None = None,
    resource_key_rules: Iterable[str] | None = None,
    timeout: float = 30.0,
    timeout_seconds: float | None = None,
    data_classification: Mapping[str, str] | None = None,
    field_data_classification: Mapping[str, str] | None = None,
    mutating: bool = False,
    mutation_parameters: set[str] | frozenset[str] | None = None,
    allowed_roles: set[str] | frozenset[str] | None = None,
) -> None:
    """注册插件工具；插件不需要修改核心工具表。"""
    global _REGISTRY_GENERATION
    if not isinstance(name, str):
        raise ToolRegistrationError("工具名必须是字符串")
    if name in TOOL_SPECS or name in TOOL_FUNCTIONS:
        existing = TOOL_SPECS.get(name)
        existing_source = existing.source if existing is not None else "existing function map"
        raise ToolRegistrationError(
            f"工具名/source 冲突：{name} 已由 {existing_source} 注册，不能由 {source} 覆盖"
        )
    if not isinstance(source, str):
        raise ToolRegistrationError("插件 source 必须是字符串")
    if source == "builtin" or source.startswith("builtin:"):
        raise ToolRegistrationError("插件不能声明保留的 builtin source")
    if mutating or mutation_parameters:
        raise ToolRegistrationError("通用插件不能注册裸连接写工具；请实现受控事务适配器")
    if resource_keys is not None and resource_key_rules is not None:
        raise ToolRegistrationError("resource_keys 与 resource_key_rules 不能同时声明")
    if timeout_seconds is not None:
        try:
            timeout_value = float(timeout)
            timeout_seconds_value = float(timeout_seconds)
        except (TypeError, ValueError) as error:
            raise ToolRegistrationError("timeout 必须是正数") from error
        if timeout != 30.0 and timeout_value != timeout_seconds_value:
            raise ToolRegistrationError("timeout 与 timeout_seconds 不一致")
        timeout = timeout_seconds
    if data_classification is not None and field_data_classification is not None:
        raise ToolRegistrationError("数据分类别名不能同时声明")
    classifications = data_classification or field_data_classification or {}
    if risk is not None and risk_level is not None:
        try:
            if ToolRisk.parse(risk) is not ToolRisk.parse(risk_level):
                raise ToolRegistrationError("risk 与 risk_level 不一致")
        except ToolRegistrationError:
            raise
    resolved_risk = risk if risk is not None else risk_level
    if resolved_risk is None:
        resolved_risk = ToolRisk.CRITICAL
    normalized = validate_function_schema(schema, name=name)
    validate_handler_contract(handler, normalized)
    spec = ToolSpec(
        schema=normalized,
        handler=handler,
        category=category,
        risk_level=ToolRisk.parse(resolved_risk).value,
        mutating=mutating,
        mutation_parameters=frozenset(mutation_parameters or ()),
        allowed_roles=(
            _ALL_ROLES if allowed_roles is None else frozenset(allowed_roles)
        ),
        source=source,
        version=version,
        capability=capability,
        effect=effect,
        parallel_safe=parallel_safe,
        resource_keys=tuple(resource_keys or resource_key_rules or ()),
        timeout=timeout,
        data_classification=classifications,
    )
    _validate_spec(spec)
    # Publish both maps only after the complete contract has passed, so a
    # failed registration cannot leave a half-visible tool behind.
    TOOL_FUNCTIONS[name] = handler
    TOOL_SPECS[name] = spec
    _REGISTRY_GENERATION += 1
