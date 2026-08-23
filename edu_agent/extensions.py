from __future__ import annotations

from importlib import import_module
from importlib.metadata import entry_points
import re
from typing import Protocol


class ToolProvider(Protocol):
    def openai_tools(self, **kwargs) -> list[dict]: ...

    def dispatch(self, name: str, arguments: dict | None = None, conn=None) -> dict: ...

    def tool_names(self) -> list[str]: ...

    def build_tool_manifest(self, **kwargs): ...

    def supports_parallel_tool_calls(self, name: str, *, context=None) -> bool: ...


class PluginContext:
    def __init__(self, *, register_tool, source: str = "plugin:unknown", version: str = "0.0.0"):
        self._register_tool = register_tool
        self.source = source
        self.version = version

    def register_tool(self, **kwargs):
        declared_source = kwargs.pop("source", self.source)
        if declared_source != self.source:
            raise ValueError(
                f"插件 source 必须为加载器冻结的 {self.source}，不能声明 {declared_source}"
            )
        kwargs.setdefault("version", self.version)
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
            self.load(candidate.name, candidate.load())
        return list(self.loaded)

    def load_module(self, module_name: str) -> str:
        return self.load(module_name, import_module(module_name))

    def load(self, name: str, plugin) -> str:
        register = getattr(plugin, "register", None)
        if not callable(register):
            raise TypeError(f"插件 {name} 必须提供 register(context) 函数")
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", name) is None:
            raise ValueError("插件名必须是稳定标识符")
        version = str(getattr(plugin, "__version__", "0.0.0"))
        register(
            PluginContext(
                register_tool=self.registry.register_tool,
                source=f"plugin:{name}",
                version=version,
            )
        )
        if name not in self.loaded:
            self.loaded.append(name)
        return name
