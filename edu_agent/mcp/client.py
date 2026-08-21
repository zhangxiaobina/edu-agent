"""MCP 工具 provider：把 edu-agent MCP server 暴露的工具，按 registry **同样的契约**
（openai_tools() + dispatch()）提供给 LangGraph 图，使工具调用真正经 **MCP 协议**往返。

实现要点：在后台线程跑一个独立 asyncio 事件循环，持有 stdio_client + ClientSession，
provider 的同步方法用 run_coroutine_threadsafe 把协程投递到该循环。图层（同步 invoke）
因此无需改成 async，且与跑本地 registry 时行为一致（dispatch 返回同样的 dict）。

用法：
    provider = MCPToolProvider().start()
    run_agent(task, engine, tools_provider=provider)
    provider.close()
或：
    with MCPToolProvider() as provider:
        ...
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# edu_agent/mcp/client.py → parents[2] = 仓库根（供子进程 import edu_agent）
_REPO_ROOT = Path(__file__).resolve().parents[2]


class MCPToolProvider:
    """与 registry 鸭子兼容的工具 provider，但工具经 MCP 协议调用。

    暴露 openai_tools() / dispatch(name, args, conn=None) / tool_names()，可直接顶替
    registry 传给 build_agent / run_agent（tools_provider=...）。
    """

    def __init__(self, command: str | None = None, args: list[str] | None = None,
                 env: dict | None = None, cwd: str | None = None):
        self._command = command or sys.executable
        self._args = args if args is not None else ["-m", "edu_agent.mcp.server"]
        # 透传当前环境（含 EDU_AGENT_DB，使 server 子进程连同一个合成库）+ 保证能 import edu_agent
        base_env = dict(env) if env is not None else dict(os.environ)
        base_env.setdefault("PYTHONPATH", str(_REPO_ROOT))
        self._env = base_env
        self._cwd = cwd or str(_REPO_ROOT)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: ClientSession | None = None
        self._tools_cache: list[dict] | None = None
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._exc: BaseException | None = None

    # ---------------- 生命周期 ----------------
    def start(self, timeout: float = 30.0) -> "MCPToolProvider":
        self._thread = threading.Thread(target=self._run_loop, name="mcp-tool-provider", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("MCP server 启动 / 握手超时")
        if self._exc is not None:
            raise self._exc
        return self

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except BaseException as e:  # noqa: BLE001 —— 转交给 start() 抛出
            self._exc = e
            self._ready.set()
        finally:
            self._loop.close()

    async def _serve(self) -> None:
        params = StdioServerParameters(command=self._command, args=self._args,
                                       env=self._env, cwd=self._cwd)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                self._session = session
                listed = await session.list_tools()
                self._tools_cache = [
                    {"type": "function",
                     "function": {"name": t.name,
                                  "description": t.description or "",
                                  "parameters": t.inputSchema}}
                    for t in listed.tools
                ]
                self._stop = asyncio.Event()
                self._ready.set()
                await self._stop.wait()  # 保持 session 存活直到 close()

    def close(self) -> None:
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread is not None:
            self._thread.join(timeout=10)

    def __enter__(self) -> "MCPToolProvider":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------- 与 registry 同契约 ----------------
    def openai_tools(self) -> list[dict]:
        if self._tools_cache is None:
            raise RuntimeError("MCP provider 未启动（先 start()）")
        return self._tools_cache

    def tool_names(self) -> list[str]:
        return [t["function"]["name"] for t in self.openai_tools()]

    def dispatch(self, name: str, arguments: dict | None = None, conn=None) -> dict:
        """按名经 MCP 协议调用工具，返回 dict（与 registry.dispatch 一致）。

        conn 仅为与 registry.dispatch 签名兼容；MCP server 端自管合成库连接。
        """
        if self._loop is None or self._session is None:
            raise RuntimeError("MCP provider 未启动（先 start()）")
        fut = asyncio.run_coroutine_threadsafe(self._call(name, arguments or {}), self._loop)
        return fut.result(timeout=60)

    async def _call(self, name: str, arguments: dict) -> dict:
        result = await self._session.call_tool(name, arguments)
        text = "".join(b.text for b in result.content if getattr(b, "type", None) == "text")
        if not text:
            return {"error": "MCP 工具无文本返回", "isError": bool(getattr(result, "isError", False))}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"error": "MCP 工具返回非 JSON", "raw": text}
