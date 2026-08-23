"""Thin adapters for canonical teaching-data analysis tools."""

from __future__ import annotations

from ..teaching import TeachingQueryKind
from .teaching_adapter import execute_teaching_read


def analyze_class_errors(
    conn,
    exam_id=None,
    class_id=None,
    top=10,
    _provider=None,
    _context=None,
) -> dict:
    return execute_teaching_read(
        TeachingQueryKind.CLASS_ERRORS,
        {"exam_id": exam_id, "class_id": class_id, "top": top},
        connection=conn,
        context=_context,
        provider=_provider,
    )


def diagnose_weak_points(
    conn,
    student_id=None,
    class_id=None,
    course_id=None,
    threshold=0.6,
    top=10,
    _provider=None,
    _context=None,
) -> dict:
    return execute_teaching_read(
        TeachingQueryKind.WEAK_POINTS,
        {
            "student_id": student_id,
            "class_id": class_id,
            "course_id": course_id,
            "threshold": threshold,
            "top": top,
        },
        connection=conn,
        context=_context,
        provider=_provider,
    )


def get_score_distribution(
    conn,
    exam_id,
    _provider=None,
    _context=None,
) -> dict:
    return execute_teaching_read(
        TeachingQueryKind.SCORE_DISTRIBUTION,
        {"exam_id": exam_id},
        connection=conn,
        context=_context,
        provider=_provider,
    )
