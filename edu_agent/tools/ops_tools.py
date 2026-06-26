"""操作类工具：建考试 / 组卷 / 批量判分 / 布置作业。均写入合成库。"""
from __future__ import annotations

import sqlite3

from ..data.db import rows_to_dicts
from .query_tools import _resolve_kp_uid


def _next_id(conn, table) -> int:
    return conn.execute(f"SELECT COALESCE(MAX(id),0)+1 FROM {table}").fetchone()[0]


def create_exam(conn: sqlite3.Connection, exam_name, class_id, course_id, duration=90,
                question_bank_id=None, total_score=100, pass_score=60,
                question_count=0, description=None) -> dict:
    if not conn.execute("SELECT 1 FROM classes WHERE id=?", (class_id,)).fetchone():
        return {"error": f"班级 {class_id} 不存在"}
    if not conn.execute("SELECT 1 FROM courses WHERE id=?", (course_id,)).fetchone():
        return {"error": f"课程 {course_id} 不存在"}
    eid = _next_id(conn, "exams")
    code = f"EX{eid:04d}"
    conn.execute(
        """INSERT INTO exams(id,exam_name,exam_code,description,class_id,course_id,
           question_bank_id,creator_id,start_time,end_time,duration,total_score,
           pass_score,question_count,status)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
        (eid, exam_name, code, description, class_id, course_id, question_bank_id,
         _course_teacher(conn, course_id), None, None, duration, total_score,
         pass_score, question_count),
    )
    conn.commit()
    return {"created": True, "exam_id": eid, "exam_code": code, "exam_name": exam_name,
            "class_id": class_id, "course_id": course_id, "status": 0, "status_text": "未开始(草稿)"}


def generate_paper(conn: sqlite3.Connection, question_bank_id, paper_name=None,
                   total_questions=10, difficulty_distribution=None,
                   question_counts=None, knowledge_points=None) -> dict:
    bank = conn.execute("SELECT id,name,course_id FROM question_banks WHERE id=?",
                        (question_bank_id,)).fetchone()
    if not bank:
        return {"error": f"题库 {question_bank_id} 不存在"}
    # 候选池
    rows = conn.execute(
        """SELECT q.id, q.title, q.question_type, q.difficulty, q.score
           FROM questions q JOIN question_bank_questions qbq ON qbq.question_id=q.id
           WHERE qbq.question_bank_id=? AND q.status=1 ORDER BY q.id""",
        (question_bank_id,),
    ).fetchall()
    pool = rows_to_dicts(rows)
    if knowledge_points:
        uids = {_resolve_kp_uid(conn, kp, bank["course_id"]) for kp in knowledge_points}
        uids.discard(None)
        if uids:
            allowed = {r["resource_id"] for r in conn.execute(
                f"SELECT resource_id FROM kg_resource_link WHERE resource_type='question' "
                f"AND node_uid IN ({','.join('?'*len(uids))})", list(uids))}
            pool = [q for q in pool if q["id"] in allowed]

    selected: list[dict] = []
    chosen_ids: set[int] = set()

    def take(items, k):
        for q in items:
            if len(selected) >= 200 or q["id"] in chosen_ids:
                continue
            selected.append(q)
            chosen_ids.add(q["id"])
            k -= 1
            if k <= 0:
                break

    if difficulty_distribution:
        for diff, k in difficulty_distribution.items():
            take([q for q in pool if q["difficulty"] == diff], k)
    elif question_counts:
        for qtype, k in question_counts.items():
            take([q for q in pool if q["question_type"] == qtype], k)
    else:
        take(pool, total_questions)

    total_score = round(sum(q["score"] for q in selected), 1)
    type_dist, diff_dist = {}, {}
    for q in selected:
        type_dist[q["question_type"]] = type_dist.get(q["question_type"], 0) + 1
        diff_dist[q["difficulty"]] = diff_dist.get(q["difficulty"], 0) + 1
    quality, suggestions = _paper_quality(selected, diff_dist, type_dist)
    return {
        "preview_id": f"PV-{question_bank_id}-{len(selected)}",
        "paper_name": paper_name or f"{bank['name']}自动组卷",
        "question_bank_id": question_bank_id, "course_id": bank["course_id"],
        "total_questions": len(selected), "total_score": total_score,
        "difficulty_distribution": diff_dist, "type_distribution": type_dist,
        "quality_score": quality, "suggestions": suggestions,
        "questions": selected,
        "note": "预览阶段；真实平台需 confirm 才落库为正式试卷",
    }


def batch_grade(conn: sqlite3.Connection, exam_id, regrade=False) -> dict:
    if not conn.execute("SELECT 1 FROM exams WHERE id=?", (exam_id,)).fetchone():
        return {"error": f"考试 {exam_id} 不存在"}
    status_filter = "" if regrade else " AND status < 3"
    records = conn.execute(
        f"SELECT id, student_id FROM exam_records WHERE exam_id=?{status_filter}", (exam_id,)
    ).fetchall()
    pass_score = conn.execute("SELECT pass_score FROM exams WHERE id=?", (exam_id,)).fetchone()[0]
    graded, failed = 0, 0
    for rec in records:
        agg = conn.execute(
            """SELECT COALESCE(SUM(earned_score),0) AS sc, COALESCE(SUM(is_correct),0) AS cc,
                      COUNT(*) AS n FROM exam_answers WHERE record_id=?""",
            (rec["id"],),
        ).fetchone()
        if agg["n"] == 0:
            failed += 1
            continue
        passed = 1 if agg["sc"] >= pass_score else 0
        conn.execute(
            "UPDATE exam_records SET score=?, correct_count=?, answer_count=?, status=3, passed=? WHERE id=?",
            (round(agg["sc"], 1), agg["cc"], agg["n"], passed, rec["id"]),
        )
        graded += 1
    # 重算排名
    ranked = conn.execute(
        "SELECT id FROM exam_records WHERE exam_id=? ORDER BY score DESC", (exam_id,)
    ).fetchall()
    for rank, r in enumerate(ranked, start=1):
        conn.execute("UPDATE exam_records SET rank=? WHERE id=?", (rank, r["id"]))
    conn.commit()
    return {"exam_id": exam_id, "total_records": len(records), "graded_count": graded,
            "failed_count": failed, "regrade": regrade}


def assign_homework(conn: sqlite3.Connection, title, course_id, class_ids, end_time,
                    homework_type="open", description=None, start_time=None,
                    total_score=100, max_submissions=1) -> dict:
    if not conn.execute("SELECT 1 FROM courses WHERE id=?", (course_id,)).fetchone():
        return {"error": f"课程 {course_id} 不存在"}
    if isinstance(class_ids, int):
        class_ids = [class_ids]
    hid = _next_id(conn, "homeworks")
    conn.execute(
        """INSERT INTO homeworks(id,title,homework_type,description,course_id,creator_id,
           start_time,end_time,total_score,max_submissions,status)
           VALUES(?,?,?,?,?,?,?,?,?,?, 'PUBLISHED')""",
        (hid, title, homework_type, description, course_id, _course_teacher(conn, course_id),
         start_time, end_time, total_score, max_submissions),
    )
    linked = []
    for cid in class_ids:
        if conn.execute("SELECT 1 FROM classes WHERE id=?", (cid,)).fetchone():
            conn.execute("INSERT INTO homework_classes(homework_id,class_id) VALUES(?,?)", (hid, cid))
            linked.append(cid)
    conn.commit()
    return {"created": True, "homework_id": hid, "title": title, "course_id": course_id,
            "class_ids": linked, "end_time": end_time, "status": "PUBLISHED"}


# ---------- 内部 ----------
def _course_teacher(conn, course_id) -> int | None:
    row = conn.execute("SELECT teacher_id FROM courses WHERE id=?", (course_id,)).fetchone()
    return row["teacher_id"] if row else None


def _paper_quality(selected, diff_dist, type_dist):
    """简单试卷质量启发式：难度/题型越均衡分越高 (0-1)；附改进建议。"""
    suggestions = []
    if not selected:
        return 0.0, ["未选中任何题目，请放宽过滤条件"]
    n = len(selected)
    # 难度均衡：理想 easy:medium:hard ≈ 3:5:2
    ideal = {"easy": 0.3, "medium": 0.5, "hard": 0.2}
    dev = sum(abs(diff_dist.get(d, 0) / n - p) for d, p in ideal.items())
    balance = max(0.0, 1 - dev)
    diversity = min(1.0, len(type_dist) / 3)  # 题型覆盖
    quality = round(0.6 * balance + 0.4 * diversity, 2)
    if diff_dist.get("hard", 0) == 0:
        suggestions.append("缺少难题，建议加入 hard 题以拉开区分度")
    if len(type_dist) < 2:
        suggestions.append("题型单一，建议混合多种题型")
    if not suggestions:
        suggestions.append("难度与题型分布较均衡")
    return quality, suggestions
