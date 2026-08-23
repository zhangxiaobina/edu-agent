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
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import timedelta
import json
import os
import sys
import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ..tools.manifest import (
    ToolManifest,
    ToolManifestEntry,
    ToolEffect,
    ToolRegistrationError,
    canonical_schema_hash,
    enabled_capability_set,
    manifest_entry_matches,
    validate_function_schema,
)
from ..runtime.cancellation import CancellationRequested
from ..runtime.security import redact_sensitive
from ..runtime.tool_arguments import (
    ToolArgumentError,
    normalize_tool_arguments,
    strict_parse_tool_arguments,
    validate_tool_arguments,
)

# edu_agent/mcp/client.py → parents[2] = 仓库根（供子进程 import edu_agent）
_REPO_ROOT = Path(__file__).resolve().parents[2]


class MCPToolProvider:
    """与 registry 鸭子兼容的工具 provider，但工具经 MCP 协议调用。

    暴露 openai_tools() / dispatch(name, args, conn=None) / tool_names()，可直接顶替
    registry 传给 build_agent / run_agent（tools_provider=...）。
    """

    def __init__(
        self,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict | None = None,
        cwd: str | None = None,
        *,
        trusted_manifest: ToolManifest | Iterable[ToolManifestEntry] | None = None,
        max_response_chars: int = 1_000_000,
    ):
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
        self._catalog_lock = threading.RLock()
        self._catalog_generation = 0
        self._connected = False
        self._registration_errors: tuple[str, ...] = ()
        if isinstance(max_response_chars, bool) or not isinstance(max_response_chars, int) or max_response_chars <= 0:
            raise ValueError("MCP max_response_chars must be a positive integer")
        self._max_response_chars = max_response_chars
        if trusted_manifest is None:
            # The bundled stdio server is intentionally a mirror of the local
            # registry.  Pinning its catalog prevents a remote process from
            # squatting on a built-in name or forging a weaker effect.
            from ..tools import registry

            trusted_entries = registry.manifest_entries()
        elif isinstance(trusted_manifest, ToolManifest):
            trusted_entries = trusted_manifest.entries
        else:
            trusted_entries = tuple(trusted_manifest)
        self._trusted_entries = {
            entry.name: entry
            for entry in trusted_entries
            if isinstance(entry, ToolManifestEntry)
        }
        if len(self._trusted_entries) != len(tuple(trusted_entries)):
            raise ToolRegistrationError("MCP trusted_manifest 含非法或重复 entry")
        if not self._trusted_entries:
            raise ToolRegistrationError("MCP trusted_manifest 不能为空")
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._exc: BaseException | None = None

    # ---------------- 生命周期 ----------------
    def start(self, timeout: float = 30.0) -> "MCPToolProvider":
        if self._thread is not None and self._thread.is_alive():
            if self._connected:
                return self
            raise RuntimeError("MCP provider 已有未完成的连接线程")
        self._ready.clear()
        self._exc = None
        self._stop = None
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
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._session = session
                    listed = await session.list_tools()
                    self._install_catalog(listed.tools)
                    self._stop = asyncio.Event()
                    self._ready.set()
                    await self._stop.wait()  # 保持 session 存活直到 close()
        finally:
            with self._catalog_lock:
                self._connected = False
                self._session = None

    def _install_catalog(self, tools: Iterable[object]) -> None:
        """Validate and atomically publish one MCP discovery snapshot."""

        entries: dict[str, ToolManifestEntry] = {}
        cache: list[dict] = []
        errors: list[str] = []
        seen_names: set[str] = set()
        for tool in tools:
            name = getattr(tool, "name", None)
            if not isinstance(name, str) or not name:
                errors.append("tool name missing")
                continue
            if name in seen_names:
                errors.append(f"duplicate tool name: {name}")
                continue
            seen_names.add(name)
            function = {
                "name": name,
                "description": getattr(tool, "description", None) or "",
                "parameters": getattr(tool, "inputSchema", None),
            }
            try:
                function = validate_function_schema(function, name=name)
            except (ToolRegistrationError, TypeError, ValueError) as error:
                errors.append(f"{name}: invalid schema ({type(error).__name__})")
                continue
            raw_meta = getattr(tool, "meta", None)
            metadata = raw_meta.get("edu_agent") if isinstance(raw_meta, Mapping) else None
            required = ("source", "version", "schema_hash", "effect", "capability")
            if not isinstance(metadata, Mapping) or any(
                metadata.get(key) in (None, "") for key in required
            ):
                errors.append(f"{name}: incomplete trust metadata")
                continue
            declared_hash = metadata.get("schema_hash")
            try:
                actual_hash = canonical_schema_hash(function)
                if declared_hash != actual_hash:
                    raise ToolRegistrationError("schema_hash mismatch")
                candidate = ToolManifestEntry(
                    name=name,
                    schema=function,
                    category=metadata.get("category", "unknown"),
                    source=metadata["source"],
                    version=metadata["version"],
                    schema_hash=declared_hash,
                    capability=metadata["capability"],
                    risk=metadata.get("risk", "critical"),
                    effect=metadata["effect"],
                    parallel_safe=metadata.get("parallel_safe", False),
                    resource_keys=tuple(metadata.get("resource_keys") or ()),
                    timeout=metadata.get("timeout", 60.0),
                    allowed_roles=frozenset(metadata.get("allowed_roles") or ()),
                    data_classification=metadata.get("data_classification") or {},
                    mutation_parameters=frozenset(metadata.get("mutation_parameters") or ()),
                )
            except (KeyError, ToolRegistrationError, TypeError, ValueError) as error:
                errors.append(f"{name}: invalid trust metadata ({type(error).__name__})")
                continue
            trusted = self._trusted_entries.get(name)
            if trusted is None:
                errors.append(f"{name}: name is not in the trusted MCP catalog")
                continue
            if candidate.to_dict() != trusted.to_dict():
                errors.append(f"{name}: trusted metadata/schema collision")
                continue
            annotations = getattr(tool, "annotations", None)
            if not self._annotations_match(candidate.effect, annotations):
                errors.append(f"{name}: MCP annotations/effect conflict")
                continue
            entries[name] = candidate
            cache.append({"type": "function", "function": function})
        if not entries and self._trusted_entries:
            errors.append("catalog is empty")
        if errors:
            with self._catalog_lock:
                # Preserve the last good snapshot for diagnostics, but fence
                # all dispatch/build calls until a fresh trusted snapshot is
                # installed.  A failed refresh must not keep using a server
                # whose schema/effect identity just drifted.
                self._connected = False
                self._registration_errors = tuple(errors)
                self._catalog_generation += 1
            raise ToolRegistrationError("MCP catalog rejected: " + "; ".join(errors))
        with self._catalog_lock:
            self._manifest_entries = dict(entries)
            self._tools_cache = list(cache)
            self._catalog_generation += 1
            self._registration_errors = ()
            self._connected = True

    @staticmethod
    def _annotations_match(effect: ToolEffect, annotations) -> bool:
        if annotations is None:
            return False
        read_only = getattr(annotations, "readOnlyHint", None)
        destructive = getattr(annotations, "destructiveHint", None)
        if effect in {ToolEffect.READ, ToolEffect.PURE}:
            return read_only is True and destructive is not True
        if effect in {
            ToolEffect.WRITE,
            ToolEffect.CONDITIONAL_WRITE,
            ToolEffect.CODE_EXECUTION,
            ToolEffect.APPROVAL,
            ToolEffect.INTERACTIVE,
        }:
            return destructive is True and read_only is not True
        return False

    def close(self) -> None:
        with self._catalog_lock:
            self._connected = False
        if self._loop is not None and self._stop is not None:
            try:
                self._loop.call_soon_threadsafe(self._stop.set)
            except RuntimeError:
                # The transport may already have closed the loop after an
                # unexpected disconnect.  The disconnected fence above is the
                # authoritative state; close remains idempotent.
                pass
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
        with self._catalog_lock:
            tools_cache = tuple(self._tools_cache or ())
            manifest_entries = dict(self._manifest_entries)
            connected = self._connected
        if not tools_cache or not connected:
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
            for cached in tools_cache:
                name = cached["function"]["name"]
                entry = manifest_entries.get(name)
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
        with self._catalog_lock:
            return self._manifest_entries.get(name)

    def get_spec(self, name: str):
        return self.get_manifest_entry(name)

    def tool_available(self, name: str, context=None) -> bool:
        with self._catalog_lock:
            return self._connected and name in self._manifest_entries

    def supports_parallel_tool_calls(
        self,
        name: str,
        *,
        context=None,
        entry: ToolManifestEntry | None = None,
    ) -> bool:
        """MCP parallelism is an explicit, pinned read-only capability."""

        with self._catalog_lock:
            current = self._manifest_entries.get(name)
            connected = self._connected
        if not connected or current is None:
            return False
        candidate = entry or current
        return (
            candidate.effect is ToolEffect.READ
            and candidate.parallel_safe is True
            and manifest_entry_matches(candidate, current)
        )

    def refresh(self, timeout: float = 30.0) -> None:
        """Re-discover tools; existing frozen manifests reject any drift."""

        if self._loop is None or not self._connected:
            raise RuntimeError("MCP provider 未启动（先 start()）")
        future = asyncio.run_coroutine_threadsafe(self._refresh(), self._loop)
        try:
            future.result(timeout=timeout)
        except FutureTimeoutError as error:
            future.cancel()
            self.close()
            raise TimeoutError("MCP 工具重连/发现超时") from error
        except BaseException:
            # A rejected discovery snapshot invalidates the whole connection.
            # Stop the old session so a later start() performs a fresh handshake
            # instead of continuing on a catalog that just failed admission.
            self.close()
            raise

    async def _refresh(self) -> None:
        if self._session is None:
            raise RuntimeError("MCP session 已断开")
        listed = await self._session.list_tools()
        self._install_catalog(listed.tools)

    def dispatch(
        self,
        name: str,
        arguments: dict | None = None,
        conn=None,
        *,
        manifest: ToolManifest | None = None,
        context=None,
    ) -> dict:
        """按名经 MCP 协议调用工具，返回 dict（与 registry.dispatch 一致）。

        conn 仅为与 registry.dispatch 签名兼容；MCP server 端自管合成库连接。
        """
        if context is None or manifest is None:
            return {
                "error": "MCP_EXECUTION_CONTEXT_REQUIRED",
                "message": "MCP 远端工具必须经带冻结 manifest 和身份 scope 的执行器调用",
            }
        return self._dispatch_with_context(
            name,
            arguments or {},
            context,
            manifest=manifest,
        )

    def dispatch_with_context(
        self,
        name: str,
        arguments: dict,
        context,
        conn=None,
        *,
        manifest: ToolManifest | None = None,
    ) -> dict:
        return self._dispatch_with_context(name, arguments or {}, context, manifest=manifest)

    def _dispatch_with_context(
        self,
        name: str,
        arguments: dict,
        context,
        *,
        manifest: ToolManifest | None,
    ) -> dict:
        with self._catalog_lock:
            connected = self._connected
            generation = self._catalog_generation
            current_session = self._session
        if self._loop is None or current_session is None or not connected:
            raise RuntimeError("MCP provider 未启动（先 start()）")
        if manifest is None or not manifest.matches_context(context):
            return {"error": "TOOL_MANIFEST_MISMATCH", "message": "MCP manifest 与运行 scope 不匹配"}
        entry = manifest.get(name)
        current = self.get_manifest_entry(name)
        if entry is None:
            return {"error": "工具不在本 run 冻结的 manifest 中"}
        if current is None or entry.to_dict() != current.to_dict():
            return {
                "error": "TOOL_MANIFEST_MISMATCH",
                "message": "MCP 工具 registry 在 run 内发生变化，manifest 身份不匹配",
            }
        validated, validation_error = self._validate_local_call(entry, arguments, context)
        if validation_error is not None:
            return validation_error
        arguments = validated
        if entry.effect is ToolEffect.CODE_EXECUTION:
            return {
                "error": "MCP_CODE_EXECUTION_REQUIRES_APPROVAL",
                "message": "MCP 远端调用不能绕过本地代码执行审批",
            }
        if entry.is_mutating(arguments):
            return {
                "error": "MCP_WRITE_REQUIRES_TRANSACTIONAL_ADAPTER",
                "message": "MCP 远端写调用必须经本地事务执行器",
            }
        token = getattr(context, "cancellation_token", None)
        timeout = max(0.001, float(entry.timeout))
        deadline = time.monotonic() + timeout
        if token is not None and token.remaining_seconds() is not None:
            deadline = min(deadline, time.monotonic() + token.remaining_seconds())
        meta = {
            "edu_agent": {
                "actor_id": getattr(context, "actor_id", None),
                "tenant_id": getattr(context, "tenant_id", None),
                "role": getattr(context, "role", None),
                "course_ids": sorted(getattr(context, "course_ids", ()) or ()),
                "run_id": getattr(context, "run_id", None),
                "manifest_hash": manifest.manifest_hash,
                "manifest_names": list(manifest.names),
                "tool_name": name,
                "schema_hash": entry.canonical_schema_hash,
            }
        }
        fut = asyncio.run_coroutine_threadsafe(
            self._call(name, arguments, meta=meta, timeout_seconds=timeout),
            self._loop,
        )
        unlink = token.register(lambda cancellation: fut.cancel()) if token is not None else None
        try:
            while True:
                if token is not None:
                    token.checkpoint("mcp.before_result")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    fut.cancel()
                    raise TimeoutError(f"MCP 工具 {name} 超时")
                try:
                    result = fut.result(timeout=min(0.05, remaining))
                    if token is not None:
                        token.checkpoint("mcp.after_result")
                    with self._catalog_lock:
                        still_current = self._catalog_state_matches(
                            generation,
                            current_session,
                        )
                    if not still_current:
                        return {
                            "error": "MCP_DISCONNECTED_LATE_RESULT",
                            "message": "MCP 连接或工具 catalog 在结果返回前发生变化",
                        }
                    return result
                except FutureTimeoutError:
                    continue
                except FutureCancelledError as error:
                    if token is not None and token.cancellation is not None:
                        raise CancellationRequested(
                            token.cancellation,
                            boundary="mcp.call",
                        ) from error
                    with self._catalog_lock:
                        still_current = self._catalog_state_matches(
                            generation,
                            current_session,
                        )
                    if not still_current:
                        return {
                            "error": "MCP_DISCONNECTED_LATE_RESULT",
                            "message": "MCP 连接或工具 catalog 在结果返回前发生变化",
                        }
                    raise TimeoutError(f"MCP 工具 {name} 调用被取消") from error
                except Exception as error:
                    if token is not None and token.cancellation is not None:
                        raise CancellationRequested(
                            token.cancellation,
                            boundary="mcp.call",
                        ) from error
                    with self._catalog_lock:
                        still_current = self._catalog_state_matches(
                            generation,
                            current_session,
                        )
                    if not still_current:
                        return {
                            "error": "MCP_DISCONNECTED_LATE_RESULT",
                            "message": "MCP 连接或工具 catalog 在结果返回前发生变化",
                        }
                    raise
        except BaseException:
            fut.cancel()
            raise
        finally:
            if unlink is not None:
                unlink()

    def _catalog_state_matches(self, generation: int, session: object) -> bool:
        return (
            self._connected
            and self._catalog_generation == generation
            and self._session is session
        )

    async def _call(
        self,
        name: str,
        arguments: dict,
        *,
        meta: dict | None = None,
        timeout_seconds: float | None = None,
    ) -> dict:
        if self._session is None:
            raise RuntimeError("MCP session 已断开")
        result = await self._session.call_tool(
            name,
            arguments,
            read_timeout_seconds=(
                timedelta(seconds=max(0.001, timeout_seconds))
                if timeout_seconds is not None
                else None
            ),
            meta=meta,
        )
        chunks: list[str] = []
        total_characters = 0
        captured_characters = 0
        for block in getattr(result, "content", ()) or ():
            if getattr(block, "type", None) != "text":
                continue
            block_text = getattr(block, "text", "")
            if not isinstance(block_text, str):
                continue
            total_characters += len(block_text)
            if captured_characters < self._max_response_chars:
                remaining = self._max_response_chars - captured_characters
                captured = block_text[:remaining]
                chunks.append(captured)
                captured_characters += len(captured)
        if total_characters > self._max_response_chars:
            return {
                "error": "MCP_RESULT_TOO_LARGE",
                "message": "MCP 远端结果超过本地传输预算",
                "original_characters": total_characters,
            }
        text = "".join(chunks)
        is_error = bool(getattr(result, "isError", False))
        if not text:
            return {
                "error": "MCP_REMOTE_ERROR" if is_error else "MCP_EMPTY_RESULT",
                "message": "MCP 远端返回错误但没有错误载荷" if is_error else "MCP 工具无文本返回",
            }
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {
                "error": "MCP_REMOTE_ERROR" if is_error else "MCP_RESULT_NON_JSON",
                "message": "MCP 远端返回错误" if is_error else "MCP 工具返回非 JSON",
                "preview": redact_sensitive(
                    text[: min(1024, self._max_response_chars)]
                ),
            }
        if is_error:
            if isinstance(parsed, dict) and parsed.get("error") not in (None, ""):
                return redact_sensitive(parsed)
            return {
                "error": "MCP_REMOTE_ERROR",
                "message": "MCP 远端标记调用失败但未提供结构化 error",
            }
        if not isinstance(parsed, dict):
            return {
                "error": "MCP_RESULT_INVALID",
                "message": "MCP 工具结果必须是 JSON object",
            }
        return redact_sensitive(parsed)

    @staticmethod
    def _validate_local_call(
        entry: ToolManifestEntry,
        arguments: dict,
        context,
    ) -> tuple[dict, dict | None]:
        """Apply the same bounded argument/scope checks as the local executor."""

        try:
            parsed = strict_parse_tool_arguments(arguments)
            protected = tuple(f"/{key}" for key in entry.mutation_parameters)
            normalized, _repairs = normalize_tool_arguments(
                parsed,
                entry.schema.get("parameters", {}),
                effect=entry.effect,
                data_classification=entry.data_classification,
                protected_pointers=protected,
            )
        except ToolArgumentError as error:
            return {}, {"error": error.code, "message": error.message}
        issues = validate_tool_arguments(normalized, entry.schema.get("parameters", {}))
        if issues:
            return {}, {
                "error": "INVALID_ARGUMENTS",
                "message": "; ".join(item["message"] for item in issues),
                "details": {"issues": [dict(item) for item in issues]},
            }
        role = getattr(context, "role", None)
        if role is not None and role not in entry.allowed_roles:
            return {}, {"error": "FORBIDDEN", "message": f"角色 {role} 无权调用 {entry.name}"}
        course_id = normalized.get("course_id")
        course_ids = frozenset(getattr(context, "course_ids", ()) or ())
        if (
            role not in {"admin", "system"}
            and course_ids
            and course_id is not None
            and int(course_id) not in course_ids
        ):
            return {}, {
                "error": "COURSE_SCOPE_DENIED",
                "message": f"当前身份无权访问课程 {course_id}",
            }
        return normalized, None
