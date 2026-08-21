"""查询类工具实现（mirror 真实平台只读端点）。

约定：每个工具签名 fn(conn, **params) -> dict，返回 JSON 可序列化结构；
不直接打开/关闭连接（由 registry.dispatch 统一管理）。
"""
from __future__ import annotations

import json
import sqlite3

from ..data.db import rows_to_dicts

STATUS_TEXT = {0: "未开始", 1: "进行中", 2: "已结束"}


def query_student_scores(conn: sqlite3.Connection, exam_id=None, student_id=None,
                         class_id=None, only_failed=False, page=1, page_size=50) -> dict:
    where, params = [], []
    if exam_id is not None:
        where.append("er.exam_id=?")
        params.append(exam_id)
    if student_id is not None:
        where.append("er.student_id=?")
        params.append(student_id)
    if class_id is not None:
        where.append("e.class_id=?")
        params.append(class_id)
    if only_failed:
        where.append("er.passed=0")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    offset = (page - 1) * page_size
    rows = conn.execute(
        f"""SELECT er.student_id, s.name AS student_name, s.username AS student_no,
                   er.exam_id, e.exam_name, er.score, er.total_score,
                   ROUND(er.score * 100.0 / NULLIF(er.total_score,0), 1) AS score_rate,
                   er.correct_count, er.answer_count, er.passed, er.rank, er.status,
                   er.submit_time, er.duration
            FROM exam_records er
            JOIN students s ON s.id = er.student_id
            JOIN exams e ON e.id = er.exam_id
            {clause}
            ORDER BY er.exam_id, er.rank
            LIMIT ? OFFSET ?""",
        params + [page_size, offset],
    ).fetchall()
    total = conn.execute(
        f"SELECT COUNT(*) FROM exam_records er JOIN exams e ON e.id=er.exam_id{clause}", params
    ).fetchone()[0]
    return {"total": total, "page": page, "page_size": page_size, "records": rows_to_dicts(rows)}


def list_exams(conn: sqlite3.Connection, class_id=None, course_id=None, status=None,
               search=None, page=1, page_size=50) -> dict:
    where, params = [], []
    if class_id is not None:
        where.append("e.class_id=?")
        params.append(class_id)
    if course_id is not None:
        where.append("e.course_id=?")
        params.append(course_id)
    if status is not None:
        where.append("e.status=?")
        params.append(status)
    if search:
        where.append("(e.exam_name LIKE ? OR e.exam_code LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    offset = (page - 1) * page_size
    rows = conn.execute(
        f"""SELECT e.id, e.exam_name, e.exam_code, e.class_id, cl.name AS class_name,
                   e.course_id, co.name AS course_name, e.start_time, e.end_time,
                   e.duration, e.total_score, e.pass_score, e.question_count, e.status,
                   (SELECT COUNT(*) FROM exam_records r WHERE r.exam_id=e.id) AS submit_count,
                   (SELECT ROUND(AVG(r.score),1) FROM exam_records r WHERE r.exam_id=e.id) AS avg_score,
                   (SELECT ROUND(SUM(r.passed)*100.0/COUNT(*),1) FROM exam_records r WHERE r.exam_id=e.id)
                       AS pass_rate
            FROM exams e
            JOIN classes cl ON cl.id = e.class_id
            JOIN courses co ON co.id = e.course_id
            {clause}
            ORDER BY e.start_time DESC
            LIMIT ? OFFSET ?""",
        params + [page_size, offset],
    ).fetchall()
    exams = rows_to_dicts(rows)
    for ex in exams:
        ex["status_text"] = STATUS_TEXT.get(ex["status"], "")
    total = conn.execute(f"SELECT COUNT(*) FROM exams e{clause}", params).fetchone()[0]
    return {"total": total, "page": page, "page_size": page_size, "exams": exams}


def get_class_roster(conn: sqlite3.Connection, class_id, search=None, sort_by=None,
                     sort_order="asc", page=1, page_size=100) -> dict:
    cls = conn.execute("SELECT id,name FROM classes WHERE id=?", (class_id,)).fetchone()
    if not cls:
        return {"error": f"班级 {class_id} 不存在"}
    where = ["cs.class_id=?"]
    params = [class_id]
    if search:
        where.append("(s.name LIKE ? OR s.username LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]
    sort_col = {"student_name": "s.name", "student_username": "s.username",
                "join_time": "cs.join_time", "avg_score": "avg_score"}.get(sort_by, "s.username")
    order = "DESC" if str(sort_order).lower() == "desc" else "ASC"
    offset = (page - 1) * page_size
    rows = conn.execute(
        f"""SELECT s.id AS student_id, s.username AS student_username, s.name AS student_name,
                   s.phone, s.email, cs.join_time, cs.status,
                   (SELECT COUNT(*) FROM exam_records r JOIN exams e ON e.id=r.exam_id
                        WHERE r.student_id=s.id AND e.class_id=cs.class_id) AS exam_count,
                   (SELECT ROUND(AVG(r.score),1) FROM exam_records r JOIN exams e ON e.id=r.exam_id
                        WHERE r.student_id=s.id AND e.class_id=cs.class_id) AS avg_score,
                   (SELECT COUNT(*) FROM homework_classes hc WHERE hc.class_id=cs.class_id) AS homework_count
            FROM class_students cs
            JOIN students s ON s.id = cs.student_id
            WHERE {' AND '.join(where)}
            ORDER BY {sort_col} {order}
            LIMIT ? OFFSET ?""",
        params + [page_size, offset],
    ).fetchall()
    total = conn.execute(
        f"SELECT COUNT(*) FROM class_students cs JOIN students s ON s.id=cs.student_id "
        f"WHERE {' AND '.join(where)}", params
    ).fetchone()[0]
    return {"class_id": class_id, "class_name": cls["name"], "total": total,
            "page": page, "page_size": page_size, "students": rows_to_dicts(rows)}


def search_questions(conn: sqlite3.Connection, question_bank_id=None, course_id=None,
                     question_type=None, difficulty=None, knowledge_point=None,
                     keyword=None, status=1, page=1, page_size=20) -> dict:
    where, params = ["q.status=?"], [status]
    join = ""
    if question_bank_id is not None:
        join += " JOIN question_bank_questions qbq ON qbq.question_id=q.id AND qbq.question_bank_id=?"
        params.insert(0, question_bank_id)  # 占位顺序：JOIN 参数先于 WHERE
    if course_id is not None:
        where.append("q.course_id=?")
        params.append(course_id)
    if question_type:
        where.append("q.question_type=?")
        params.append(question_type)
    if difficulty:
        where.append("q.difficulty=?")
        params.append(difficulty)
    if keyword:
        where.append("(q.title LIKE ? OR q.content LIKE ?)")
        params += [f"%{keyword}%", f"%{keyword}%"]
    if knowledge_point:
        uid = _resolve_kp_uid(conn, knowledge_point, course_id)
        if uid is None:
            return {"total": 0, "questions": [], "note": f"未找到知识点：{knowledge_point}"}
        where.append("q.id IN (SELECT resource_id FROM kg_resource_link "
                     "WHERE resource_type='question' AND node_uid=?)")
        params.append(uid)
    clause = " WHERE " + " AND ".join(where)
    offset = (page - 1) * page_size
    rows = conn.execute(
        f"""SELECT q.id, q.title, q.question_type, q.difficulty, q.content, q.options,
                   q.score, q.source, q.status, q.language, q.usage_count, q.course_id
            FROM questions q{join}{clause}
            ORDER BY q.id LIMIT ? OFFSET ?""",
        params + [page_size, offset],
    ).fetchall()
    questions = rows_to_dicts(rows)
    for qd in questions:
        if qd.get("options"):
            qd["options"] = json.loads(qd["options"])
        qd["knowledge_points"] = _question_kps(conn, qd["id"])
    total = conn.execute(
        f"SELECT COUNT(*) FROM questions q{join}{clause}", params
    ).fetchone()[0]
    return {"total": total, "page": page, "page_size": page_size, "questions": questions}


def get_learning_progress(conn: sqlite3.Connection, student_id, course_id=None) -> dict:
    where = ["lp.student_id=?"]
    params = [student_id]
    if course_id is not None:
        where.append("lp.course_id=?")
        params.append(course_id)
    clause = " WHERE " + " AND ".join(where)
    rows = conn.execute(
        f"""SELECT lp.course_id, co.name AS course_name, lp.courseware_id, cw.name AS courseware_name,
                   lp.progress, lp.completed, lp.watched_time, lp.study_status, lp.last_access_time
            FROM learning_progress lp
            JOIN courseware cw ON cw.id = lp.courseware_id
            JOIN courses co ON co.id = lp.course_id
            {clause}
            ORDER BY lp.course_id, cw.sort_order""",
        params,
    ).fetchall()
    items = rows_to_dicts(rows)
    # 按课程汇总
    courses: dict[int, dict] = {}
    for it in items:
        c = courses.setdefault(it["course_id"], {
            "course_id": it["course_id"], "course_name": it["course_name"],
            "total_courseware": 0, "completed_courseware": 0, "_sum": 0, "coursewares": []})
        c["total_courseware"] += 1
        c["completed_courseware"] += it["completed"]
        c["_sum"] += it["progress"]
        c["coursewares"].append(it)
    summaries = []
    for c in courses.values():
        n = c.pop("_sum")
        c["overall_progress"] = round(n / c["total_courseware"], 1) if c["total_courseware"] else 0
        summaries.append(c)
    return {"student_id": student_id, "courses": summaries}


# ---------- 内部助手 ----------
def _resolve_kp_uid(conn, ref: str, course_id=None) -> str | None:
    """知识点 名称/uid → node_uid。"""
    row = conn.execute("SELECT node_uid FROM kg_nodes WHERE node_uid=?", (ref,)).fetchone()
    if row:
        return row["node_uid"]
    q = "SELECT node_uid FROM kg_nodes WHERE name=?"
    p = [ref]
    if course_id is not None:
        q += " AND course_id=?"
        p.append(course_id)
    row = conn.execute(q, p).fetchone()
    if row:
        return row["node_uid"]
    row = conn.execute(
        "SELECT node_uid FROM kg_nodes WHERE name LIKE ? LIMIT 1", (f"%{ref}%",)
    ).fetchone()
    return row["node_uid"] if row else None


def _question_kps(conn, question_id: int) -> list[str]:
    rows = conn.execute(
        """SELECT kn.name FROM kg_resource_link krl JOIN kg_nodes kn ON kn.node_uid=krl.node_uid
           WHERE krl.resource_type='question' AND krl.resource_id=?""",
        (question_id,),
    ).fetchall()
    return [r["name"] for r in rows]
