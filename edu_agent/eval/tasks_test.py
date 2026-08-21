"""Independent synthetic Test intents for final model evaluation.

These tasks use the existing synthetic teaching-data generator with seed 314,
five classes, and all three courses per class.  They do not reuse the six DPO
multi-step families or the historical 19-task prompt-development set.
"""

from __future__ import annotations

import sqlite3

from .tasks import EvalTask, ExpectedCall, SuccessSpec, attach_lineage


TEST_SEED = 314
TEST_DATA_VERSION = "seed-314.test-v1"
TEST_N_CLASSES = 5
TEST_COURSES_PER_CLASS = 3


def _anchor_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT cc.class_id, cc.course_id, cl.name AS class_name, co.name AS course_name,
                  e.id AS exam_id
             FROM class_courses cc
             JOIN classes cl ON cl.id=cc.class_id
             JOIN courses co ON co.id=cc.course_id
             JOIN exams e ON e.class_id=cc.class_id AND e.course_id=cc.course_id
            ORDER BY cc.class_id, cc.course_id"""
    ).fetchall()


def _attach(task: EvalTask, family: str, semantic_group: str | None = None) -> EvalTask:
    return attach_lineage(
        task,
        split="test",
        family=family,
        semantic_group=semantic_group or family,
        version=TEST_DATA_VERSION,
        seed=TEST_SEED,
        generator="edu_agent.eval.tasks_test.build_test_tasks",
    )


def build_test_tasks(conn: sqlite3.Connection) -> list[EvalTask]:
    """Build the independent Test split from a seed-314 synthetic database."""
    anchors = _anchor_rows(conn)
    if not anchors:
        raise ValueError("seed-314 Test database has no class/course/exam anchors")
    primary = anchors[0]
    comparison = next(
        (
            row for row in anchors
            if row["course_id"] == primary["course_id"]
            and row["class_id"] != primary["class_id"]
        ),
        None,
    )
    if comparison is None:
        raise ValueError("seed-314 Test database needs two classes sharing a course")
    student = conn.execute(
        """SELECT cs.student_id
             FROM class_students cs
             JOIN learning_progress lp ON lp.student_id=cs.student_id
              AND lp.course_id=?
             JOIN exam_records er ON er.student_id=cs.student_id
            WHERE cs.class_id=?
            GROUP BY cs.student_id
           HAVING COUNT(DISTINCT er.exam_id) >= 2
            ORDER BY cs.student_id
            LIMIT 1""",
        (primary["course_id"], primary["class_id"]),
    ).fetchone()
    concept = conn.execute(
        """SELECT name FROM kg_nodes
            WHERE course_id=? AND type='concept'
            ORDER BY rowid LIMIT 1""",
        (primary["course_id"],),
    ).fetchone()
    if student is None or concept is None:
        raise ValueError("seed-314 Test database is missing progress or knowledge-graph anchors")

    class_name = primary["class_name"]
    course_name = primary["course_name"]
    tasks = [
        _attach(
            EvalTask(
                "test-progress-summary",
                "single",
                f"查看学生 {student['student_id']} 在{course_name}课程的课件学习进度",
                [ExpectedCall(
                    "get_learning_progress",
                    {"student_id": [student["student_id"]], "course_id": [primary["course_id"]]},
                    send={"student_id": student["student_id"], "course_id": primary["course_id"]},
                )],
                SuccessSpec(["get_learning_progress"]),
            ),
            "test.student_course_progress",
        ),
        _attach(
            EvalTask(
                "test-knowledge-neighbors",
                "single",
                f"{course_name}里的“{concept['name']}”和哪些知识点直接相邻？",
                [ExpectedCall(
                    "query_knowledge_graph",
                    {
                        "course_id": [primary["course_id"]],
                        "operation": ["neighbors"],
                        "node": [concept["name"]],
                    },
                    send={
                        "course_id": primary["course_id"],
                        "operation": "neighbors",
                        "node": concept["name"],
                    },
                )],
                SuccessSpec(["query_knowledge_graph"]),
            ),
            "test.knowledge_neighbors",
        ),
        _attach(
            EvalTask(
                "test-compare-class-sizes",
                "parallel",
                f"比较{class_name}和{comparison['class_name']}的在班人数",
                [
                    ExpectedCall("get_class_roster", {"class_id": [primary["class_id"]]}),
                    ExpectedCall("get_class_roster", {"class_id": [comparison["class_id"]]}),
                ],
                SuccessSpec(["get_class_roster", "get_class_roster"]),
                parallel=True,
            ),
            "test.compare_class_sizes",
        ),
        _attach(
            EvalTask(
                "test-exam-top-performer",
                "multi_step",
                f"找到{class_name}的{course_name}考试，再告诉我这场考试最高分是谁",
                [
                    ExpectedCall(
                        "list_exams",
                        {"class_id": [primary["class_id"]], "course_id": [primary["course_id"]]},
                        send={"class_id": primary["class_id"], "course_id": primary["course_id"]},
                    ),
                    ExpectedCall(
                        "query_student_scores",
                        {"exam_id": [primary["exam_id"]]},
                        send={"exam_id": primary["exam_id"], "page_size": 50},
                    ),
                ],
                SuccessSpec(["list_exams", "query_student_scores"]),
            ),
            "test.exam_top_performer",
        ),
        _attach(
            EvalTask(
                "test-student-exam-history",
                "single",
                f"汇总学生 {student['student_id']} 参加过的全部考试记录",
                [ExpectedCall(
                    "query_student_scores",
                    {"student_id": [student["student_id"]]},
                    send={"student_id": student["student_id"], "page_size": 50},
                )],
                SuccessSpec(["query_student_scores"]),
            ),
            "test.student_exam_history",
        ),
        _attach(
            EvalTask(
                "test-knowledge-skill-catalog",
                "single",
                f"列出{course_name}知识图谱中的技能节点",
                [ExpectedCall(
                    "query_knowledge_graph",
                    {
                        "course_id": [primary["course_id"]],
                        "operation": ["find"],
                        "node_type": ["skill"],
                    },
                    send={
                        "course_id": primary["course_id"],
                        "operation": "find",
                        "node_type": "skill",
                    },
                )],
                SuccessSpec(["query_knowledge_graph"]),
            ),
            "test.knowledge_skill_catalog",
        ),
    ]
    return tasks


__all__ = [
    "TEST_COURSES_PER_CLASS", "TEST_DATA_VERSION", "TEST_N_CLASSES", "TEST_SEED",
    "build_test_tasks",
]
