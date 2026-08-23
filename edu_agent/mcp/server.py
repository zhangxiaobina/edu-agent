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
import re
from collections.abc import Mapping
from types import SimpleNamespace

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from ..tools import registry
from ..tools.manifest import ToolEffect
from ..runtime.security import redact_sensitive
from ..runtime.tool_arguments import (
    ToolArgumentError,
    normalize_tool_arguments,
    strict_parse_tool_arguments,
    validate_tool_arguments,
)


def _configure_code_execution() -> None:
    """MCP subprocess loads the same fail-closed config as the service."""
    from ..code_execution import build_code_execution_provider
    from ..runtime.config import load_config

    config = load_config()
    provider = build_code_execution_provider(config.code_execution)
    if provider is not None:
        provider.health_check(force=True)
    registry.configure_code_execution(provider)


_configure_code_execution()

server: Server = Server("edu-agent-tools")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_ROLES = frozenset({"student", "teacher", "admin", "system"})


def _request_scope():
    """Decode the client-supplied, runtime-generated scope envelope.

    The stdio server is a child of the authenticated local runtime.  It still
    treats the envelope as untrusted input and validates every field before it
    reaches a teaching provider; the parent executor remains the authority.
    """

    try:
        meta = server.request_context.meta
    except LookupError:
        return None, {"error": "MCP_CONTEXT_REQUIRED"}
    raw = getattr(meta, "edu_agent", None)
    if raw is None and isinstance(meta, Mapping):
        raw = meta.get("edu_agent")
    if not isinstance(raw, Mapping):
        return None, {"error": "MCP_CONTEXT_REQUIRED"}
    required = (
        "actor_id",
        "tenant_id",
        "role",
        "course_ids",
        "run_id",
        "manifest_hash",
        "manifest_names",
        "tool_name",
        "schema_hash",
    )
    if any(raw.get(key) in (None, "") for key in required):
        return None, {"error": "MCP_CONTEXT_INVALID", "message": "MCP scope envelope 缺少必需字段"}
    if not isinstance(raw["course_ids"], list) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in raw["course_ids"]
    ):
        return None, {"error": "MCP_CONTEXT_INVALID", "message": "course_ids 必须是整数数组"}
    if not isinstance(raw["manifest_names"], list) or any(
        not isinstance(item, str) or not item for item in raw["manifest_names"]
    ) or len(set(raw["manifest_names"])) != len(raw["manifest_names"]):
        return None, {"error": "MCP_CONTEXT_INVALID", "message": "manifest_names 必须是无重复字符串数组"}
    if any(not isinstance(raw[key], str) for key in ("actor_id", "tenant_id", "role", "run_id", "manifest_hash", "tool_name", "schema_hash")):
        return None, {"error": "MCP_CONTEXT_INVALID", "message": "MCP scope envelope 字段类型无效"}
    if raw["role"] not in _ROLES:
        return None, {"error": "MCP_CONTEXT_INVALID", "message": "MCP scope role 无效"}
    if not _HASH.fullmatch(raw["manifest_hash"]) or not _HASH.fullmatch(raw["schema_hash"]):
        return None, {"error": "MCP_CONTEXT_INVALID", "message": "MCP scope hash 无效"}
    if raw["tool_name"] not in raw["manifest_names"]:
        return None, {"error": "MCP_CONTEXT_INVALID", "message": "MCP scope tool 不在 manifest 中"}
    return (
        SimpleNamespace(
            actor_id=raw["actor_id"],
            tenant_id=raw["tenant_id"],
            role=raw["role"],
            course_ids=frozenset(raw["course_ids"]),
            run_id=raw["run_id"],
            manifest_hash=raw["manifest_hash"],
            manifest_names=tuple(raw["manifest_names"]),
            tool_name=raw["tool_name"],
            schema_hash=raw["schema_hash"],
            cancellation_token=None,
        ),
        None,
    )


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """对外声明全部工具——schema 与本地 registry 同源（同名、同入参）。"""
    return [
        types.Tool(
            name=tool["function"]["name"],
            description=tool["function"].get("description", ""),
            inputSchema=tool["function"]["parameters"],
            annotations=types.ToolAnnotations(
                readOnlyHint=(
                    registry.get_spec(tool["function"]["name"]).effect.value
                    in {"read", "pure"}
                ),
                destructiveHint=(
                    registry.get_spec(tool["function"]["name"]).effect.value
                    in {"write", "conditional_write", "code_execution"}
                ),
                idempotentHint=(
                    registry.get_spec(tool["function"]["name"]).effect.value
                    in {"read", "pure"}
                ),
                openWorldHint=False,
            ),
            _meta={
                "edu_agent": registry.get_spec(
                    tool["function"]["name"]
                ).to_manifest_entry().to_dict(include_schema=False),
            },
        )
        for tool in registry.openai_tools(allow_local_code_execution=False)
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    """执行一次工具调用并返回结果。

    registry.dispatch 永不抛错（未知工具 / 参数错误均返回 {"error": ...}），故无需额外兜底；
    放到线程里跑，避免同步 sqlite 调用阻塞 server 事件循环。
    """
    normalized = arguments or {}
    scope, scope_error = _request_scope()
    if scope_error is not None:
        return [types.TextContent(type="text", text=json.dumps(scope_error, ensure_ascii=False))]
    spec = registry.get_spec(name)
    local_manifest = registry.build_tool_manifest(context=scope)
    request_manifest = local_manifest.restrict(scope.manifest_names)
    if spec is None:
        result = {"error": "UNKNOWN_TOOL"}
    elif name not in scope.manifest_names:
        result = {"error": "TOOL_NOT_IN_MANIFEST", "message": "工具不在 MCP 请求冻结面内"}
    elif any(item not in local_manifest.names for item in scope.manifest_names):
        result = {"error": "MCP_MANIFEST_MISMATCH", "message": "MCP 请求 manifest 含未授权工具"}
    elif request_manifest.manifest_hash != scope.manifest_hash:
        result = {"error": "MCP_MANIFEST_MISMATCH", "message": "MCP 请求 manifest hash 与本地 catalog 不一致"}
    elif scope.tool_name != name:
        result = {"error": "MCP_CONTEXT_INVALID", "message": "scope tool identity mismatch"}
    elif scope.schema_hash != spec.schema_hash:
        result = {"error": "MCP_SCHEMA_MISMATCH", "message": "本地 registry schema hash 与请求不一致"}
    elif scope.role not in spec.allowed_roles:
        result = {"error": "FORBIDDEN", "message": "当前角色无权调用该 MCP 工具"}
    else:
        normalized, argument_error = _validate_arguments(spec, normalized)
        if argument_error is not None:
            result = argument_error
        elif (
            scope.role not in {"admin", "system"}
            and scope.course_ids
            and normalized.get("course_id") is not None
            and int(normalized["course_id"]) not in scope.course_ids
        ):
            result = {"error": "COURSE_SCOPE_DENIED", "message": "当前身份无权访问该课程"}
        elif spec.effect is ToolEffect.CODE_EXECUTION:
            result = {
                "error": "MCP_CODE_EXECUTION_REQUIRES_APPROVAL",
                "message": "MCP 独立 server 不持有 actor/session 审批上下文",
            }
        elif spec.is_mutating(normalized):
            result = {
                "error": "MCP_WRITE_REQUIRES_TRANSACTIONAL_ADAPTER",
                "message": "独立 MCP server 不接受绕过审批、幂等和 outbox 的写调用",
            }
        else:
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        registry.dispatch,
                        name,
                        normalized,
                        manifest=request_manifest,
                        context=scope,
                    ),
                    timeout=max(0.001, float(spec.timeout)),
                )
            except asyncio.TimeoutError:
                result = {"error": "MCP_TOOL_TIMEOUT", "message": "MCP 工具执行超时"}
            except asyncio.CancelledError:
                raise
    result = redact_sensitive(result)
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > 1_000_000:
        text = json.dumps(
            {
                "error": "MCP_RESULT_TOO_LARGE",
                "message": "MCP server result exceeds transport budget",
            },
            ensure_ascii=False,
        )
    return [types.TextContent(type="text", text=text)]


def _validate_arguments(spec, arguments: object) -> tuple[dict, dict | None]:
    """Re-run bounded parsing, normalization and JSON Schema validation locally."""

    try:
        parsed = strict_parse_tool_arguments(arguments)
        protected = tuple(f"/{key}" for key in spec.mutation_parameters)
        normalized, _repairs = normalize_tool_arguments(
            parsed,
            spec.schema.get("parameters", {}),
            effect=spec.effect,
            data_classification=spec.data_classification,
            protected_pointers=protected,
        )
    except ToolArgumentError as error:
        return {}, {"error": error.code, "message": error.message}
    issues = validate_tool_arguments(normalized, spec.schema.get("parameters", {}))
    if issues:
        return {}, {
            "error": "INVALID_ARGUMENTS",
            "message": "; ".join(item["message"] for item in issues),
            "details": {"issues": [dict(item) for item in issues]},
        }
    return normalized, None


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
