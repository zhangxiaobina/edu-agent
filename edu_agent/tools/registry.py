"""工具注册表：name → (schema, callable)，统一 dispatch，供 Agent 编排层调用。"""
from __future__ import annotations

import copy
import sqlite3
from dataclasses import dataclass, field
from typing import Callable

from ..data import db
from ..state import FencingTokenRejected, RunCancelled
from ..runtime.cancellation import CancellationRequested
from . import ai_tools, analysis_tools, kg_tools, ops_tools, query_tools
from .schemas import SCHEMA_BY_NAME

_code_execution_provider = None


def configure_code_execution(provider) -> None:
    global _code_execution_provider
    _code_execution_provider = provider


def code_execution_provider():
    return _code_execution_provider


def code_execution_health():
    if _code_execution_provider is None:
        return None
    return _code_execution_provider.health_check()


def code_execution_available() -> bool:
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


@dataclass(frozen=True)
class ToolSpec:
    schema: dict
    handler: Callable
    category: str
    risk_level: str = "low"
    mutating: bool = False
    mutation_parameters: frozenset[str] = field(default_factory=frozenset)
    allowed_roles: frozenset[str] = field(
        default_factory=lambda: frozenset({"student", "teacher", "admin", "system"})
    )

    def is_mutating(self, arguments: dict) -> bool:
        if self.mutating:
            return True
        return any(arguments.get(parameter) for parameter in self.mutation_parameters)


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
    "query_student_scores": ("query", "medium", False, (), _STAFF_ROLES),
    "list_exams": ("query", "low", False, (), _ALL_ROLES),
    "get_class_roster": ("query", "medium", False, (), _STAFF_ROLES),
    "search_questions": ("query", "low", False, (), _ALL_ROLES),
    "get_learning_progress": ("query", "medium", False, (), _STAFF_ROLES),
    "query_knowledge_graph": ("knowledge", "low", False, (), _ALL_ROLES),
    "recommend_study_path": ("knowledge", "medium", False, (), _STAFF_ROLES),
    "analyze_class_errors": ("analysis", "medium", False, (), _STAFF_ROLES),
    "diagnose_weak_points": ("analysis", "medium", False, (), _STAFF_ROLES),
    "get_score_distribution": ("analysis", "medium", False, (), _STAFF_ROLES),
    "create_exam": ("operation", "high", True, (), _STAFF_ROLES),
    "generate_paper": ("operation", "medium", False, (), _STAFF_ROLES),
    "batch_grade": ("operation", "critical", True, (), _ADMIN_ROLES),
    "assign_homework": ("operation", "high", True, (), _STAFF_ROLES),
    "generate_questions": ("content", "high", False, ("save_to_bank",), _STAFF_ROLES),
    "run_code": ("execution", "critical", False, (), _ALL_ROLES),
}

TOOL_SPECS = {
    name: ToolSpec(
        schema=_strict_schema(name),
        handler=handler,
        category=metadata[0],
        risk_level=metadata[1],
        mutating=metadata[2],
        mutation_parameters=frozenset(metadata[3]),
        allowed_roles=metadata[4],
    )
    for name, handler in TOOL_FUNCTIONS.items()
    for metadata in [_TOOL_METADATA[name]]
}

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
) -> list[dict]:
    """返回当前角色和能力边界内可用的 OpenAI tools。"""
    selected = []
    for spec in TOOL_SPECS.values():
        if role is not None and role not in spec.allowed_roles:
            continue
        if categories is not None and spec.category not in categories:
            continue
        if spec.schema["name"] == "run_code":
            if not allow_local_code_execution or _code_execution_provider is None:
                continue
            if not code_execution_available():
                continue
        selected.append({"type": "function", "function": spec.schema})
    return selected


def dispatch(name: str, arguments: dict | None = None,
             conn: sqlite3.Connection | None = None) -> dict:
    """按名调用工具。conn 为空则自动打开/关闭合成库连接。"""
    if name not in TOOL_FUNCTIONS:
        return {"error": f"未知工具：{name}"}
    arguments = arguments or {}
    own = conn is None
    conn = conn or db.connect()
    try:
        if name == "run_code" and not code_execution_available():
            result = {"error": "代码执行后端未通过健康与安全能力门禁"}
        elif name == "run_code":
            result = ai_tools.run_code(conn, _provider=_code_execution_provider, **arguments)
        else:
            result = TOOL_FUNCTIONS[name](conn, **arguments)
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


def dispatch_with_context(name: str, arguments: dict | None, context, conn=None) -> dict:
    if name != "run_code":
        return dispatch(name, arguments, conn=conn)
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
    risk_level: str = "low",
    mutating: bool = False,
    mutation_parameters: set[str] | frozenset[str] | None = None,
    allowed_roles: set[str] | frozenset[str] | None = None,
) -> None:
    """注册插件工具；插件不需要修改核心工具表。"""
    if name in TOOL_SPECS:
        raise ValueError(f"工具名已存在：{name}")
    if schema.get("name") != name or "parameters" not in schema:
        raise ValueError("插件工具 schema 必须包含同名 name 和 parameters")
    if mutating or mutation_parameters:
        raise ValueError("通用插件不能注册裸连接写工具；请实现受控事务适配器")
    normalized = copy.deepcopy(schema)
    normalized["parameters"].setdefault("additionalProperties", False)
    TOOL_FUNCTIONS[name] = handler
    TOOL_SPECS[name] = ToolSpec(
        schema=normalized,
        handler=handler,
        category=category,
        risk_level=risk_level,
        mutating=mutating,
        mutation_parameters=frozenset(mutation_parameters or ()),
        allowed_roles=frozenset(allowed_roles or _ALL_ROLES),
    )
