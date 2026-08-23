from __future__ import annotations

from importlib import import_module
from importlib.metadata import entry_points
import re
from typing import Protocol

from .tools.manifest import (
    ToolEffect,
    ToolRegistrationError,
    canonical_schema_hash,
    normalize_capability,
    validate_function_schema,
)


class ToolProvider(Protocol):
    def openai_tools(self, **kwargs) -> list[dict]: ...

    def dispatch(self, name: str, arguments: dict | None = None, conn=None) -> dict: ...

    def tool_names(self) -> list[str]: ...

    def build_tool_manifest(self, **kwargs): ...

    def supports_parallel_tool_calls(self, name: str, *, context=None) -> bool: ...


class PluginContext:
    def __init__(self, *, register_tool, source: str, version: str):
        self._register_tool = register_tool
        self.source = source
        self.version = version

    def register_tool(self, **kwargs):
        declared_source = kwargs.pop("source", self.source)
        if declared_source != self.source:
            raise ToolRegistrationError(
                f"插件 source 必须为加载器冻结的 {self.source}，不能声明 {declared_source}"
            )
        if kwargs.get("version", self.version) != self.version:
            raise ToolRegistrationError(
                f"插件 version 必须为加载器冻结的 {self.version}"
            )
        kwargs["version"] = self.version
        missing = [
            key
            for key in ("schema_hash", "effect", "capability")
            if kwargs.get(key) in (None, "")
        ]
        if missing:
            raise ToolRegistrationError(
                "插件工具缺少必需的可验证元数据: " + ", ".join(missing)
            )
        schema = kwargs.get("schema")
        try:
            normalized_schema = validate_function_schema(
                schema,
                name=kwargs.get("name"),
            )
            actual_hash = canonical_schema_hash(normalized_schema)
        except (ToolRegistrationError, TypeError, ValueError) as error:
            raise ToolRegistrationError(
                "插件工具 schema 不可验证"
            ) from error
        if kwargs.get("schema_hash") != actual_hash:
            raise ToolRegistrationError(
                "插件工具 schema_hash 与 canonical schema 不一致"
            )
        try:
            effect = ToolEffect.parse(kwargs["effect"])
            capability = normalize_capability(kwargs["capability"])
        except (ToolRegistrationError, TypeError, ValueError) as error:
            raise ToolRegistrationError("插件工具 effect/capability 不可验证") from error
        if effect is ToolEffect.UNKNOWN or capability is None:
            raise ToolRegistrationError(
                "插件工具 effect/capability 不能缺失或为 unknown"
            )
        kwargs["schema"] = normalized_schema
        kwargs["effect"] = effect
        kwargs["capability"] = capability
        return self._register_tool(source=self.source, **kwargs)


class PluginManager:
    """从 entry point 或显式模块加载插件，统一走 registry.register_tool。"""

    ENTRY_POINT_GROUP = "edu_agent.plugins"

    def __init__(self, *, registry_module=None):
        if registry_module is None:
            from .tools import registry as registry_module

        self.registry = registry_module
        self.loaded: list[str] = []

    def load_entry_points(self) -> list[str]:
        discovered = entry_points()
        candidates = (
            discovered.select(group=self.ENTRY_POINT_GROUP)
            if hasattr(discovered, "select")
            else discovered.get(self.ENTRY_POINT_GROUP, [])
        )
        for candidate in candidates:
            distribution_version = getattr(getattr(candidate, "dist", None), "version", None)
            self.load(
                candidate.name,
                candidate.load(),
                distribution_version=distribution_version,
            )
        return list(self.loaded)

    def load_module(self, module_name: str) -> str:
        return self.load(module_name, import_module(module_name))

    def load(self, name: str, plugin, *, distribution_version: str | None = None) -> str:
        register = getattr(plugin, "register", None)
        if not callable(register):
            raise TypeError(f"插件 {name} 必须提供 register(context) 函数")
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", name) is None:
            raise ValueError("插件名必须是稳定标识符")
        if name in self.loaded:
            raise ToolRegistrationError(f"插件 {name} 已加载，拒绝重复/热替换")
        declared_version = getattr(plugin, "__version__", None)
        if declared_version in (None, "") and distribution_version in (None, ""):
            raise ToolRegistrationError(
                f"插件 {name} 缺少可验证 version；必须声明 __version__ 或 entry point distribution version"
            )
        if (
            declared_version not in (None, "")
            and distribution_version not in (None, "")
            and str(declared_version) != str(distribution_version)
        ):
            raise ToolRegistrationError(
                f"插件 {name} 的模块 version 与发行包 version 冲突"
            )
        version = str(declared_version or distribution_version)
        before = set(getattr(self.registry, "TOOL_SPECS", {}))
        generation_reader = getattr(self.registry, "registry_generation", None)
        before_generation = (
            generation_reader() if callable(generation_reader) else None
        )
        try:
            register(
                PluginContext(
                    register_tool=self.registry.register_tool,
                    source=f"plugin:{name}",
                    version=version,
                )
            )
        except BaseException:
            # A plugin is admitted as one unit.  Remove only declarations made
            # during this attempt; pre-existing user registrations are kept.
            specs = getattr(self.registry, "TOOL_SPECS", None)
            functions = getattr(self.registry, "TOOL_FUNCTIONS", None)
            if isinstance(specs, dict):
                for tool_name in set(specs) - before:
                    specs.pop(tool_name, None)
                    if isinstance(functions, dict):
                        functions.pop(tool_name, None)
            if (
                isinstance(before_generation, int)
                and hasattr(self.registry, "_REGISTRY_GENERATION")
            ):
                self.registry._REGISTRY_GENERATION = before_generation
            raise
        if name not in self.loaded:
            self.loaded.append(name)
        return name
