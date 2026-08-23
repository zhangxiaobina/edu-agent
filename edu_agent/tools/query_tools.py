"""Thin adapters for canonical teaching-data query tools."""

from __future__ import annotations

from ..teaching import PageRequest, TeachingQueryKind
from .teaching_adapter import execute_teaching_read


def query_student_scores(
    conn,
    exam_id=None,
    student_id=None,
    class_id=None,
    only_failed=False,
    page=1,
    page_size=50,
    _provider=None,
    _context=None,
) -> dict:
    return execute_teaching_read(
        TeachingQueryKind.SCORE_RECORDS,
        {
            "exam_id": exam_id,
            "student_id": student_id,
            "class_id": class_id,
            "only_failed": only_failed,
        },
        page=PageRequest(page, page_size),
        connection=conn,
        context=_context,
        provider=_provider,
    )


def list_exams(
    conn,
    class_id=None,
    course_id=None,
    status=None,
    search=None,
    page=1,
    page_size=50,
    _provider=None,
    _context=None,
) -> dict:
    return execute_teaching_read(
        TeachingQueryKind.EXAMS,
        {
            "class_id": class_id,
            "course_id": course_id,
            "status": status,
            "search": search,
        },
        page=PageRequest(page, page_size),
        connection=conn,
        context=_context,
        provider=_provider,
    )


def get_class_roster(
    conn,
    class_id,
    search=None,
    sort_by=None,
    sort_order="asc",
    page=1,
    page_size=100,
    _provider=None,
    _context=None,
) -> dict:
    return execute_teaching_read(
        TeachingQueryKind.CLASS_ROSTER,
        {
            "class_id": class_id,
            "search": search,
            "sort_by": sort_by,
            "sort_order": sort_order,
        },
        page=PageRequest(page, page_size),
        connection=conn,
        context=_context,
        provider=_provider,
    )


def search_questions(
    conn,
    question_bank_id=None,
    course_id=None,
    question_type=None,
    difficulty=None,
    knowledge_point=None,
    keyword=None,
    status=1,
    page=1,
    page_size=20,
    _provider=None,
    _context=None,
) -> dict:
    return execute_teaching_read(
        TeachingQueryKind.QUESTIONS,
        {
            "question_bank_id": question_bank_id,
            "course_id": course_id,
            "question_type": question_type,
            "difficulty": difficulty,
            "knowledge_point": knowledge_point,
            "keyword": keyword,
            "status": status,
        },
        page=PageRequest(page, page_size),
        connection=conn,
        context=_context,
        provider=_provider,
    )


def get_learning_progress(
    conn,
    student_id,
    course_id=None,
    _provider=None,
    _context=None,
) -> dict:
    return execute_teaching_read(
        TeachingQueryKind.LEARNING_PROGRESS,
        {"student_id": student_id, "course_id": course_id},
        connection=conn,
        context=_context,
        provider=_provider,
    )
