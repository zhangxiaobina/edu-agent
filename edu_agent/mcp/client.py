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
from collections.abc import Mapping
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ..tools.manifest import (
    ToolManifest,
    ToolManifestEntry,
    ToolRegistrationError,
    canonical_schema_hash,
    enabled_capability_set,
    manifest_entry_matches,
)

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
        self._manifest_entries: dict[str, ToolManifestEntry] = {}
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
                entries: dict[str, ToolManifestEntry] = {}
                seen_names: set[str] = set()
                for tool, cached in zip(listed.tools, self._tools_cache, strict=True):
                    if tool.name in seen_names:
                        # A second declaration makes the name ambiguous.  Do
                        # not let a later remote tool silently replace the
                        # already observed identity.
                        entries.pop(tool.name, None)
                        continue
                    seen_names.add(tool.name)
                    raw_meta = getattr(tool, "meta", None)
                    metadata = raw_meta.get("edu_agent") if isinstance(raw_meta, Mapping) else None
                    if not isinstance(metadata, dict):
                        # An unknown remote tool is intentionally not exposed:
                        # it has neither a declared capability nor trustworthy
                        # effect metadata.
                        continue
                    declared_hash = metadata.get("schema_hash")
                    try:
                        actual_hash = canonical_schema_hash(cached["function"])
                    except (TypeError, ValueError):
                        continue
                    if declared_hash != actual_hash:
                        continue
                    try:
                        entry = ToolManifestEntry(
                            name=tool.name,
                            schema=cached["function"],
                            category=metadata.get("category", "unknown"),
                            source=metadata["source"],
                            version=metadata["version"],
                            schema_hash=declared_hash,
                            capability=metadata.get("capability"),
                            risk=metadata.get("risk", "critical"),
                            effect=metadata.get("effect", "unknown"),
                            parallel_safe=metadata.get("parallel_safe", False),
                            resource_keys=tuple(metadata.get("resource_keys") or ()),
                            timeout=metadata.get("timeout", 60.0),
                            allowed_roles=frozenset(metadata.get("allowed_roles") or ()),
                            data_classification=metadata.get("data_classification") or {},
                            mutation_parameters=frozenset(
                                metadata.get("mutation_parameters") or ()
                            ),
                        )
                    except (KeyError, TypeError, ValueError):
                        # Invalid remote declarations are rejected as a unit;
                        # an untrusted tool must never become model-visible.
                        continue
                    entries[tool.name] = entry
                self._manifest_entries = entries
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
    def build_tool_manifest(
        self,
        *,
        context=None,
        role: str | None = None,
        categories: set[str] | None = None,
        allow_local_code_execution: bool = False,
        model_tool_calling: bool = True,
        model_capabilities=None,
        enabled_capabilities=None,
        capabilities=None,
    ) -> ToolManifest:
        if self._tools_cache is None:
            raise RuntimeError("MCP provider 未启动（先 start()）")
        effective_role = role or getattr(context, "role", None)
        if model_capabilities is not None:
            if isinstance(model_capabilities, Mapping):
                capability_mapping = model_capabilities
            else:
                to_event = getattr(model_capabilities, "to_event", None)
                capability_mapping = (
                    to_event()
                    if callable(to_event)
                    else {
                        key: getattr(model_capabilities, key)
                        for key in ("tool_calling", "structured_output", "usage", "streaming")
                        if hasattr(model_capabilities, key)
                    }
                )
            declared = capability_mapping.get("tool_calling", model_tool_calling)
            if not isinstance(declared, bool):
                raise ToolRegistrationError("model capability tool_calling 必须是 bool")
            model_tool_calling = declared
        if enabled_capabilities is not None and capabilities is not None:
            raise ValueError("enabled_capabilities 与 capabilities 不能同时声明")
        enabled_capabilities = (
            enabled_capabilities if enabled_capabilities is not None else capabilities
        )
        available = (
            set(enabled_capability_set(enabled_capabilities) or ())
            if enabled_capabilities is not None
            else None
        )
        entries = []
        if model_tool_calling:
            for cached in self._tools_cache:
                name = cached["function"]["name"]
                entry = self._manifest_entries.get(name)
                if entry is None or not entry.capabilities:
                    continue
                if effective_role is not None and effective_role not in entry.allowed_roles:
                    continue
                if categories is not None and entry.category not in categories:
                    continue
                if available is not None and not (
                    "*" in available
                    or "tool_calling" in available
                    or entry.capabilities <= available
                ):
                    continue
                if entry.effect.value == "code_execution" and not allow_local_code_execution:
                    continue
                entries.append(entry)
        return ToolManifest(
            tuple(entries),
            actor_id=getattr(context, "actor_id", None),
            tenant_id=getattr(context, "tenant_id", None),
            role=effective_role,
            course_ids=getattr(context, "course_ids", ()),
        )

    def openai_tools(self, **kwargs) -> list[dict]:
        return self.build_tool_manifest(**kwargs).to_openai_tools()

    def tool_names(self) -> list[str]:
        return [t["function"]["name"] for t in self.openai_tools()]

    def get_manifest_entry(self, name: str) -> ToolManifestEntry | None:
        return self._manifest_entries.get(name)

    def get_spec(self, name: str):
        return self.get_manifest_entry(name)

    def tool_available(self, name: str, context=None) -> bool:
        return self._session is not None and name in self._manifest_entries

    def dispatch(
        self,
        name: str,
        arguments: dict | None = None,
        conn=None,
        *,
        manifest: ToolManifest | None = None,
    ) -> dict:
        """按名经 MCP 协议调用工具，返回 dict（与 registry.dispatch 一致）。

        conn 仅为与 registry.dispatch 签名兼容；MCP server 端自管合成库连接。
        """
        if self._loop is None or self._session is None:
            raise RuntimeError("MCP provider 未启动（先 start()）")
        if manifest is not None:
            entry = manifest.get(name)
            current = self._manifest_entries.get(name)
            if entry is None:
                return {"error": "工具不在本 run 冻结的 manifest 中"}
            if current is None or not manifest_entry_matches(entry, current):
                return {"error": "MCP 工具 registry 在 run 内发生变化，manifest 身份不匹配"}
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
