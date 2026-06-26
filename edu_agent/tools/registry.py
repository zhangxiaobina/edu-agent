"""工具注册表：name → (schema, callable)，统一 dispatch，供 Agent 编排层调用。"""
from __future__ import annotations

import sqlite3

from ..data import db
from . import ai_tools, analysis_tools, kg_tools, ops_tools, query_tools
from .schemas import SCHEMA_BY_NAME, SCHEMAS

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

# 完整性自检：schema 与实现一一对应
_missing_fn = set(SCHEMA_BY_NAME) - set(TOOL_FUNCTIONS)
_missing_schema = set(TOOL_FUNCTIONS) - set(SCHEMA_BY_NAME)
assert not _missing_fn, f"缺少实现的工具: {_missing_fn}"
assert not _missing_schema, f"缺少 schema 的工具: {_missing_schema}"


def openai_tools() -> list[dict]:
    """返回可直接传给 OpenAI 兼容接口的 tools 列表。"""
    return [{"type": "function", "function": s} for s in SCHEMAS]


def dispatch(name: str, arguments: dict | None = None,
             conn: sqlite3.Connection | None = None) -> dict:
    """按名调用工具。conn 为空则自动打开/关闭合成库连接。"""
    if name not in TOOL_FUNCTIONS:
        return {"error": f"未知工具：{name}"}
    arguments = arguments or {}
    own = conn is None
    conn = conn or db.connect()
    try:
        return TOOL_FUNCTIONS[name](conn, **arguments)
    except TypeError as e:
        return {"error": f"参数错误：{e}"}
    finally:
        if own:
            conn.close()


def tool_names() -> list[str]:
    return list(TOOL_FUNCTIONS)
