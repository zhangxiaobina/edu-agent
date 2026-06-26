"""edu-agent 的 MCP 集成层。

- `server.py`：把 16 个教学教务工具暴露为 MCP server（stdio）。
- `client.py`：`MCPToolProvider` —— 与 registry 同契约、但工具经 MCP 协议调用，可直接喂给 LangGraph 图。
- `get_tool_provider()`：按 `EDU_AGENT_TOOLSOURCE` 选择本地 registry 或 MCP provider（与 engine 工厂同套路）。

注：仅在真正构造 MCPToolProvider 时才 import `mcp`，故未装可选依赖 `mcp` 时本模块仍可导入。
"""
from __future__ import annotations

import os

__all__ = ["MCPToolProvider", "get_tool_provider"]


def get_tool_provider(**kwargs):
    """按 EDU_AGENT_TOOLSOURCE 返回工具 provider（默认 local=registry）。

      EDU_AGENT_TOOLSOURCE=local  直接用本地 registry（默认，零额外进程）
      EDU_AGENT_TOOLSOURCE=mcp    起 MCP server 子进程，工具经 MCP 协议往返

    mcp 分支返回已 start() 的 MCPToolProvider，调用方用完需 close()。
    """
    src = os.environ.get("EDU_AGENT_TOOLSOURCE", "local").lower()
    if src in ("local", "registry", "direct"):
        from ..tools import registry
        return registry
    if src == "mcp":
        from .client import MCPToolProvider
        return MCPToolProvider(**kwargs).start()
    raise ValueError(f"未知 EDU_AGENT_TOOLSOURCE：{src}（取 local / mcp）")


def __getattr__(name):  # 惰性导出，避免未装 mcp 时导入本包即失败
    if name == "MCPToolProvider":
        from .client import MCPToolProvider
        return MCPToolProvider
    raise AttributeError(name)
