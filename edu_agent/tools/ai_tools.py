"""AI / 执行类工具：AI 出题（模板化合成，引擎可替换）+ 代码沙箱执行（mirror Jobe）。"""
from __future__ import annotations

import sqlite3

from ..code_execution import ExecutionRequest

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


class _ContextCancelEvent:
    def __init__(self, context):
        self.context = context

    def is_set(self) -> bool:
        self.context.check_control("code_execution.poll")
        return False


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
    return {
        "course_id": course_id, "knowledge_point": kp_name,
        "generation_type": "knowledge_graph" if kp_uid else "manual",
        "status": "completed", "created_questions": len(generated),
        "saved_to_bank": save_to_bank, "saved_question_ids": saved_ids,
        "questions": generated,
        "note": "模板化合成生成；接入工具调用模型(vLLM/API)后可替换为真实 AI 出题。",
    }


def run_code(conn: sqlite3.Connection, source_code, language="python", stdin=None, args=None,
             expected_output=None, timeout=5, cpu_time_limit_seconds=2,
             wall_time_limit_seconds=None, memory_limit_mb=512,
             output_limit_bytes=65536, process_limit=16, file_size_limit_mb=16,
             artifact_limit_bytes=262144,
             network_policy="disabled", network_allowlist=None, _provider=None,
             _context=None) -> dict:
    """Execute only through a configured, healthy remote isolation provider."""
    provider = _provider
    if provider is None:
        return {"error": "代码执行后端不可用：未配置真实隔离 provider"}
    wall = int(wall_time_limit_seconds if wall_time_limit_seconds is not None else timeout)
    request = ExecutionRequest(
        language=str(language), source=str(source_code), stdin=str(stdin or ""),
        args=tuple(str(item) for item in (args or ())),
        cpu_time_limit_seconds=int(cpu_time_limit_seconds),
        wall_time_limit_seconds=wall, memory_limit_mb=int(memory_limit_mb),
        output_limit_bytes=int(output_limit_bytes), process_limit=int(process_limit),
        file_size_limit_mb=int(file_size_limit_mb), network_policy=str(network_policy),
        artifact_limit_bytes=int(artifact_limit_bytes),
        network_allowlist=tuple(network_allowlist or ()),
        tenant_id=getattr(_context, "tenant_id", "default"),
        actor_id=getattr(_context, "actor_id", "unknown"),
    )
    cancel_event = _ContextCancelEvent(_context) if _context is not None else None
    result = provider.execute(request, cancel_event=cancel_event)
    if _context is not None:
        _context.check_control("code_execution.after_call")
    if not result.success:
        return {
            "outcome": {"timeout": "TLE", "memory_limit": "MLE"}.get(result.status, result.status.upper()),
            "status": result.status, "status_description": result.message or result.status,
            "stdout": result.stdout, "stderr": result.stderr, "output_truncated": result.output_truncated,
            "provider": result.provider, "run_id": result.run_id, "success": False,
        }
    if expected_output is not None:
        passed = result.stdout.strip() == str(expected_output).strip()
        return _jobe("AC" if passed else "WA", stdout=result.stdout, stderr=result.stderr,
                     passed=passed, expected_output=expected_output,
                     provider=result.provider, run_id=result.run_id,
                     output_truncated=result.output_truncated)
    return _jobe("AC", stdout=result.stdout, stderr=result.stderr,
                 provider=result.provider, run_id=result.run_id,
                 output_truncated=result.output_truncated)


# ---------- 内部 ----------
def _jobe(outcome, stdout="", stderr="", cmpinfo="", passed=None, expected_output=None,
          provider=None, run_id=None, output_truncated=False) -> dict:
    res = {"outcome": outcome, "status_description": _OUTCOME_TEXT.get(outcome, outcome),
           "stdout": stdout, "stderr": stderr, "cmpinfo": cmpinfo,
           "success": outcome == "AC"}
    if provider:
        res["provider"] = provider
    if run_id:
        res["run_id"] = run_id
    if output_truncated:
        res["output_truncated"] = True
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
