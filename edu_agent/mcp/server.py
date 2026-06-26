"""edu-agent 工具的 MCP server（stdio 传输）。

把 registry 里的 16 个教学教务工具按 **MCP 协议**对外暴露：
  - list_tools：直接复用现成的 OpenAI function schema（parameters → MCP inputSchema），
  - call_tool ：复用 registry.dispatch 执行（**不重写任何工具逻辑**），结果以 JSON 文本返回。

作为独立进程运行（被 MCP client 经 stdio 拉起）：
    python -m edu_agent.mcp.server
"""
from __future__ import annotations

import asyncio
import json

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from ..tools import registry
from ..tools.schemas import SCHEMAS

server: Server = Server("edu-agent-tools")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """对外声明全部工具——schema 与本地 registry 同源（同名、同入参）。"""
    return [
        types.Tool(name=s["name"], description=s["description"], inputSchema=s["parameters"])
        for s in SCHEMAS
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    """执行一次工具调用并返回结果。

    registry.dispatch 永不抛错（未知工具 / 参数错误均返回 {"error": ...}），故无需额外兜底；
    放到线程里跑，避免同步 sqlite 调用阻塞 server 事件循环。
    """
    result = await asyncio.to_thread(registry.dispatch, name, arguments or {})
    text = json.dumps(result, ensure_ascii=False, default=str)
    return [types.TextContent(type="text", text=text)]


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
