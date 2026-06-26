"""分析类工具实现：班级错题 Top、薄弱知识点诊断、成绩分布。"""
from __future__ import annotations

import sqlite3
import statistics

from ..data.db import rows_to_dicts


def analyze_class_errors(conn: sqlite3.Connection, exam_id=None, class_id=None, top=10) -> dict:
    if exam_id is None and class_id is None:
        return {"error": "需提供 exam_id 或 class_id 之一"}
    join, where, params = "", [], []
    if exam_id is not None:
        where.append("ea.exam_id=?")
        params.append(exam_id)
    if class_id is not None:
        join = " JOIN exams e ON e.id = ea.exam_id"
        where.append("e.class_id=?")
        params.append(class_id)
    clause = " WHERE " + " AND ".join(where)
    rows = conn.execute(
        f"""SELECT q.id AS question_id, q.title, q.difficulty, q.score AS full_score,
                   SUM(CASE WHEN ea.is_correct=0 THEN 1 ELSE 0 END) AS error_count,
                   COUNT(*) AS total_count,
                   ROUND(AVG(ea.earned_score),2) AS avg_score
            FROM exam_answers ea
            JOIN questions q ON q.id = ea.question_id{join}
            {clause}
            GROUP BY q.id
            ORDER BY error_count DESC, q.id
            LIMIT ?""",
        params + [top],
    ).fetchall()
    items = rows_to_dicts(rows)
    for it in items:
        it["error_rate"] = round(it["error_count"] / it["total_count"], 3) if it["total_count"] else 0
        kp = conn.execute(
            """SELECT kn.node_uid, kn.name FROM kg_resource_link krl
               JOIN kg_nodes kn ON kn.node_uid=krl.node_uid
               WHERE krl.resource_type='question' AND krl.resource_id=? LIMIT 1""",
            (it["question_id"],),
        ).fetchone()
        it["knowledge_point_name"] = kp["name"] if kp else None
        it["knowledge_point_uid"] = kp["node_uid"] if kp else None
    return {"scope": {"exam_id": exam_id, "class_id": class_id}, "top": top, "error_questions": items}


def diagnose_weak_points(conn: sqlite3.Connection, student_id=None, class_id=None,
                         course_id=None, threshold=0.6, top=10) -> dict:
    if student_id is None and class_id is None:
        return {"error": "需提供 student_id 或 class_id 之一"}
    if student_id is not None:
        where = ["sks.student_id=?", "sks.mastery_rate < ?"]
        params = [student_id, threshold]
        if course_id is not None:
            where.append("sks.course_id=?")
            params.append(course_id)
        rows = conn.execute(
            f"""SELECT sks.node_uid, kn.name AS knowledge_point, kn.type, kn.course_id,
                       sks.mastery_rate, sks.correct_count, sks.total_questions
                FROM student_knowledge_stats sks
                JOIN kg_nodes kn ON kn.node_uid = sks.node_uid
                WHERE {' AND '.join(where)}
                ORDER BY sks.mastery_rate ASC, sks.total_questions DESC
                LIMIT ?""",
            params + [top],
        ).fetchall()
        return {"scope": "student", "student_id": student_id, "threshold": threshold,
                "weak_points": rows_to_dicts(rows)}
    # 全班汇总：按知识点平均掌握度
    where = ["cs.class_id=?"]
    params = [class_id]
    if course_id is not None:
        where.append("sks.course_id=?")
        params.append(course_id)
    rows = conn.execute(
        f"""SELECT sks.node_uid, kn.name AS knowledge_point, kn.type, kn.course_id,
                   ROUND(AVG(sks.mastery_rate),3) AS avg_mastery,
                   COUNT(DISTINCT sks.student_id) AS student_count
            FROM student_knowledge_stats sks
            JOIN class_students cs ON cs.student_id = sks.student_id
            JOIN kg_nodes kn ON kn.node_uid = sks.node_uid
            WHERE {' AND '.join(where)}
            GROUP BY sks.node_uid
            HAVING avg_mastery < ?
            ORDER BY avg_mastery ASC
            LIMIT ?""",
        params + [threshold, top],
    ).fetchall()
    return {"scope": "class", "class_id": class_id, "threshold": threshold,
            "weak_points": rows_to_dicts(rows)}


def get_score_distribution(conn: sqlite3.Connection, exam_id) -> dict:
    exam = conn.execute(
        "SELECT id, exam_name, total_score, pass_score FROM exams WHERE id=?", (exam_id,)
    ).fetchone()
    if not exam:
        return {"error": f"考试 {exam_id} 不存在"}
    rows = conn.execute(
        "SELECT score, passed FROM exam_records WHERE exam_id=? AND score IS NOT NULL", (exam_id,)
    ).fetchall()
    scores = [r["score"] for r in rows]
    if not scores:
        return {"exam_id": exam_id, "total_students": 0, "distribution": []}
    total = exam["total_score"] or 100
    # 按满分百分比分段
    buckets = [("A", 90, 100), ("B", 80, 90), ("C", 70, 80), ("D", 60, 70), ("F", 0, 60)]
    dist = []
    for label, lo, hi in buckets:
        cnt = sum(1 for s in scores if (s * 100.0 / total) >= lo and
                  ((s * 100.0 / total) < hi or (hi == 100 and s * 100.0 / total <= 100)))
        dist.append({"grade": label, "range_pct": f"{lo}-{hi}", "student_count": cnt,
                     "percentage": round(cnt * 100.0 / len(scores), 1)})
    pass_count = sum(r["passed"] for r in rows)
    return {
        "exam_id": exam_id, "exam_name": exam["exam_name"], "total_score": total,
        "pass_score": exam["pass_score"], "total_students": len(scores),
        "average_score": round(statistics.mean(scores), 1),
        "median_score": round(statistics.median(scores), 1),
        "max_score": max(scores), "min_score": min(scores),
        "std_dev": round(statistics.pstdev(scores), 2) if len(scores) > 1 else 0.0,
        "pass_count": pass_count,
        "pass_rate": round(pass_count * 100.0 / len(scores), 1),
        "distribution": dist,
    }
