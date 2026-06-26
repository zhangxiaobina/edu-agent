"""离线 demo policy：驱动 mock 引擎，按「上一步工具的真实返回」动态决定下一步，
复现「三班 Python 考试」多步轨迹。用于离线验证 Agent 编排循环（含依赖前序结果的分支）。

这是 LLM 大脑的确定性离线替身：接入真模型后，这些决策改由模型自主完成。
"""
from __future__ import annotations

from ..engine.base import EngineResponse
from ..engine.mock import call, final, last_tool_result

# 「三班」「Python」在 demo 中解析到的 ID（真实模型会用 list/roster 工具解析）
CLASS_ID = 3
COURSE_ID = 1


def _python_exam_id(messages) -> int | None:
    res = last_tool_result(messages, "list_exams") or {}
    for e in res.get("exams", []):
        if e.get("course_id") == COURSE_ID:
            return e["id"]
    return None


def demo_policy(messages, tools, step) -> EngineResponse:
    if step == 0:  # 列出三班的 Python 考试
        return call(step, "list_exams", class_id=CLASS_ID, course_id=COURSE_ID)

    if step == 1:  # 谁不及格
        eid = _python_exam_id(messages)
        return call(step, "query_student_scores", exam_id=eid, only_failed=True)

    if step == 2:  # 普遍错在哪
        eid = _python_exam_id(messages)
        return call(step, "analyze_class_errors", exam_id=eid, top=5)

    if step == 3:  # 定位首要薄弱知识点的前置
        errs = last_tool_result(messages, "analyze_class_errors") or {}
        eqs = errs.get("error_questions", [])
        kp = eqs[0]["knowledge_point_name"] if eqs else "递归"
        return call(step, "query_knowledge_graph", course_id=COURSE_ID,
                    operation="prerequisites", node=kp)

    if step == 4:  # 给最弱的学生推学习路径 + 练习题
        scores = last_tool_result(messages, "query_student_scores") or {}
        recs = scores.get("records", [])
        sid = recs[-1]["student_id"] if recs else 1
        return call(step, "recommend_study_path", student_id=sid,
                    course_id=COURSE_ID, questions_per_point=3)

    # step >= 5：综合所有工具结果，给出最终中文回答
    return final(_compose_answer(messages))


def _compose_answer(messages) -> str:
    scores = last_tool_result(messages, "query_student_scores") or {}
    errs = last_tool_result(messages, "analyze_class_errors") or {}
    path = last_tool_result(messages, "recommend_study_path") or {}
    n_fail = scores.get("total", 0)
    eqs = errs.get("error_questions", [])[:3]
    kp_str = "、".join(
        f"{q['knowledge_point_name']}(错误率{int(q['error_rate']*100)}%)" for q in eqs) or "—"
    steps = path.get("path", [])
    plan = "；".join(
        f"{s['name']}→练习 " + "、".join(f"q{q['id']}" for q in s["practice_questions"])
        for s in steps) or "—"
    return (f"本次三班 Python 考试共 {n_fail} 人不及格。班级普遍薄弱知识点：{kp_str}。"
            f"已为最薄弱的同学生成学习路径：{plan}。建议针对上述知识点安排专项讲解与练习。")
