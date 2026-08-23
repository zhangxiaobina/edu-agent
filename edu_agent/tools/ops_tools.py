"""Thin adapters for canonical teaching operation and paper commands."""

from __future__ import annotations

from ..teaching import TeachingCommandKind
from .teaching_adapter import execute_teaching_command


def create_exam(
    conn,
    exam_name,
    class_id,
    course_id,
    duration=90,
    question_bank_id=None,
    total_score=100,
    pass_score=60,
    question_count=0,
    description=None,
    _provider=None,
    _context=None,
    _operation=None,
) -> dict:
    return execute_teaching_command(
        TeachingCommandKind.CREATE_EXAM,
        {
            "exam_name": exam_name,
            "class_id": class_id,
            "course_id": course_id,
            "duration": duration,
            "question_bank_id": question_bank_id,
            "total_score": total_score,
            "pass_score": pass_score,
            "question_count": question_count,
            "description": description,
        },
        connection=conn,
        context=_context,
        provider=_provider,
        operation=_operation,
    )


def generate_paper(
    conn,
    question_bank_id,
    paper_name=None,
    total_questions=10,
    difficulty_distribution=None,
    question_counts=None,
    knowledge_points=None,
    _provider=None,
    _context=None,
    _operation=None,
) -> dict:
    return execute_teaching_command(
        TeachingCommandKind.GENERATE_PAPER,
        {
            "question_bank_id": question_bank_id,
            "paper_name": paper_name,
            "total_questions": total_questions,
            "difficulty_distribution": difficulty_distribution,
            "question_counts": question_counts,
            "knowledge_points": knowledge_points,
        },
        connection=conn,
        context=_context,
        provider=_provider,
        operation=_operation,
    )


def batch_grade(
    conn,
    exam_id,
    regrade=False,
    _provider=None,
    _context=None,
    _operation=None,
) -> dict:
    return execute_teaching_command(
        TeachingCommandKind.BATCH_GRADE,
        {"exam_id": exam_id, "regrade": regrade},
        connection=conn,
        context=_context,
        provider=_provider,
        operation=_operation,
    )


def assign_homework(
    conn,
    title,
    course_id,
    class_ids,
    end_time,
    homework_type="open",
    description=None,
    start_time=None,
    total_score=100,
    max_submissions=1,
    _provider=None,
    _context=None,
    _operation=None,
) -> dict:
    return execute_teaching_command(
        TeachingCommandKind.ASSIGN_HOMEWORK,
        {
            "title": title,
            "course_id": course_id,
            "class_ids": class_ids,
            "end_time": end_time,
            "homework_type": homework_type,
            "description": description,
            "start_time": start_time,
            "total_score": total_score,
            "max_submissions": max_submissions,
        },
        connection=conn,
        context=_context,
        provider=_provider,
        operation=_operation,
    )
