"""Thin adapters for canonical teaching-data graph and study-path tools."""

from __future__ import annotations

from ..teaching import TeachingQueryKind
from .teaching_adapter import execute_teaching_read


def query_knowledge_graph(
    conn,
    course_id,
    operation,
    node=None,
    target=None,
    node_type=None,
    name=None,
    _provider=None,
    _context=None,
) -> dict:
    return execute_teaching_read(
        TeachingQueryKind.KNOWLEDGE_GRAPH,
        {
            "course_id": course_id,
            "operation": operation,
            "node": node,
            "target": target,
            "node_type": node_type,
            "name": name,
        },
        connection=conn,
        context=_context,
        provider=_provider,
    )


def recommend_study_path(
    conn,
    student_id,
    course_id,
    target=None,
    threshold=0.6,
    max_points=6,
    questions_per_point=3,
    _provider=None,
    _context=None,
) -> dict:
    return execute_teaching_read(
        TeachingQueryKind.STUDY_PATH,
        {
            "student_id": student_id,
            "course_id": course_id,
            "target": target,
            "threshold": threshold,
            "max_points": max_points,
            "questions_per_point": questions_per_point,
        },
        connection=conn,
        context=_context,
        provider=_provider,
    )
