"""AI / 执行类工具：AI 出题（模板化合成，引擎可替换）+ 代码沙箱执行（mirror Jobe）。"""
from __future__ import annotations

import subprocess
import sqlite3
import sys

from .ops_tools import _next_id
from .query_tools import _resolve_kp_uid

# Jobe 风格 outcome 码（mirror 真实平台 JobeResponse）
_OUTCOME_TEXT = {
    "AC": "Accepted（正常完成）", "WA": "Wrong Answer（输出不匹配）",
    "CE": "Compile Error（编译/语法错误）", "RE": "Runtime Error（运行时错误）",
    "TLE": "Time Limit Exceeded（超时）",
}
_TYPE_CYCLE = ["single", "judge", "fill"]
_DIFF_CYCLE = ["easy", "medium", "hard"]
_OPTIONS = ["选项A", "选项B", "选项C", "选项D"]


def generate_questions(conn: sqlite3.Connection, course_id, knowledge_point=None, count=5,
                       question_types=None, difficulty_distribution=None, save_to_bank=None) -> dict:
    course = conn.execute("SELECT id,name FROM courses WHERE id=?", (course_id,)).fetchone()
    if not course:
        return {"error": f"课程 {course_id} 不存在"}
    # 目标知识点
    kp_uid, kp_name = None, knowledge_point
    if knowledge_point:
        kp_uid = _resolve_kp_uid(conn, knowledge_point, course_id)
        if kp_uid:
            kp_name = conn.execute("SELECT name FROM kg_nodes WHERE node_uid=?", (kp_uid,)).fetchone()["name"]
    if not kp_name:
        row = conn.execute(
            "SELECT node_uid,name FROM kg_nodes WHERE course_id=? AND type='concept' ORDER BY node_uid LIMIT 1",
            (course_id,)).fetchone()
        if row:
            kp_uid, kp_name = row["node_uid"], row["name"]
        else:
            kp_name = course["name"]

    # 构造 (题型, 难度) 序列（确定性：给定分布则展开，否则轮换）
    pairs = _expand_pairs(count, question_types, difficulty_distribution)
    generated = []
    saved_ids = []
    for i, (qtype, diff) in enumerate(pairs, start=1):
        options, answer = _gen_body(qtype, i)
        q = {"title": f"【AI·{kp_name}】生成题{i}",
             "content": f"围绕知识点「{kp_name}」生成的{diff}难度{qtype}题（合成）。",
             "question_type": qtype, "difficulty": diff,
             "options": options, "correct_answer": answer, "source": "ai"}
        generated.append(q)
        if save_to_bank:
            qid = _save_question(conn, q, course_id, kp_uid, save_to_bank)
            q["id"] = qid
            saved_ids.append(qid)
    if save_to_bank:
        conn.commit()
    return {
        "course_id": course_id, "knowledge_point": kp_name,
        "generation_type": "knowledge_graph" if kp_uid else "manual",
        "status": "completed", "created_questions": len(generated),
        "saved_to_bank": save_to_bank, "saved_question_ids": saved_ids,
        "questions": generated,
        "note": "模板化合成生成；接入工具调用模型(vLLM/API)后可替换为真实 AI 出题。",
    }


def run_code(conn: sqlite3.Connection, source_code, language="python",
             stdin=None, expected_output=None, timeout=5) -> dict:
    """在隔离子进程中运行代码并返回 Jobe 风格结果。

    安全说明：会在本地执行传入代码，仅供可信的教学 demo 使用；
    采用隔离模式(-I)、墙钟超时、不经过 shell。目前仅支持 python。
    """
    if language != "python":
        return {"error": f"暂不支持的语言：{language}（当前仅 python）"}
    if len(source_code) > 20000:
        return {"error": "源代码过长（>20000 字符）"}
    try:
        timeout = min(int(timeout), 15)
    except (TypeError, ValueError):
        timeout = 5
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", source_code],
            input=stdin or "", capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _jobe("TLE", stdout="", stderr=f"执行超过 {timeout}s 被终止")
    if proc.returncode != 0:
        err = proc.stderr or ""
        if "SyntaxError" in err or "IndentationError" in err:
            return _jobe("CE", stdout=proc.stdout, stderr=err, cmpinfo=err)
        return _jobe("RE", stdout=proc.stdout, stderr=err)
    if expected_output is not None:
        ok = proc.stdout.strip() == str(expected_output).strip()
        return _jobe("AC" if ok else "WA", stdout=proc.stdout, stderr=proc.stderr,
                     passed=ok, expected_output=expected_output)
    return _jobe("AC", stdout=proc.stdout, stderr=proc.stderr)


# ---------- 内部 ----------
def _jobe(outcome, stdout="", stderr="", cmpinfo="", passed=None, expected_output=None) -> dict:
    res = {"outcome": outcome, "status_description": _OUTCOME_TEXT.get(outcome, outcome),
           "stdout": stdout, "stderr": stderr, "cmpinfo": cmpinfo,
           "success": outcome == "AC"}
    if passed is not None:
        res["passed"] = passed
        res["expected_output"] = expected_output
    return res


def _expand_pairs(count, question_types, difficulty_distribution):
    types = []
    if question_types:
        for t, k in question_types.items():
            types += [t] * int(k)
    diffs = []
    if difficulty_distribution:
        for d, k in difficulty_distribution.items():
            diffs += [d] * int(k)
    n = max(count or 0, len(types), len(diffs)) or 5
    out = []
    for i in range(n):
        t = types[i] if i < len(types) else _TYPE_CYCLE[i % len(_TYPE_CYCLE)]
        d = diffs[i] if i < len(diffs) else _DIFF_CYCLE[i % len(_DIFF_CYCLE)]
        out.append((t, d))
    return out


def _gen_body(qtype, i):
    if qtype == "single":
        return (list(_OPTIONS), "ABCD"[i % 4])
    if qtype == "multiple":
        return (list(_OPTIONS), ",".join(sorted({"A", "B", "C"})[: 2 + (i % 2)]))
    if qtype == "judge":
        return (["正确", "错误"], "正确" if i % 2 == 0 else "错误")
    if qtype == "fill":
        return (None, "参考答案")
    return (None, "# 参考实现\npass")


def _save_question(conn, q, course_id, kp_uid, bank_id) -> int:
    import json
    qid = _next_id(conn, "questions")
    score = {"easy": 4, "medium": 5, "hard": 8}.get(q["difficulty"], 5)
    conn.execute(
        """INSERT INTO questions(id,title,content,question_type,difficulty,options,correct_answer,
           explanation,score,source,status,creator_id,language,usage_count,course_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,1,NULL,NULL,0,?)""",
        (qid, q["title"], q["content"], q["question_type"], q["difficulty"],
         json.dumps(q["options"], ensure_ascii=False) if q["options"] else None,
         q["correct_answer"], f"考查：{q.get('knowledge_point','')}", score, "ai", course_id),
    )
    if conn.execute("SELECT 1 FROM question_banks WHERE id=?", (bank_id,)).fetchone():
        conn.execute("INSERT INTO question_bank_questions(question_bank_id,question_id) VALUES(?,?)",
                     (bank_id, qid))
    if kp_uid:
        conn.execute(
            """INSERT OR IGNORE INTO kg_resource_link(course_id,node_uid,resource_type,resource_id,
               link_type,weight) VALUES(?,?,?,?,'tests',1.0)""",
            (course_id, kp_uid, "question", qid))
    return qid
