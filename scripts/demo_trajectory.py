"""可现场跑的 demo：脚本化复现「三班 Python 考试」多工具轨迹并打印中文结果。

引擎/Agent 接入前，本脚本用固定顺序调用工具，证明工具层能协同产出连贯结论；
接入工具调用模型后，这一串调用将由 LangGraph Agent 自主编排。

运行：python scripts/demo_trajectory.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from edu_agent.data import db, generate  # noqa: E402
from edu_agent.tools import registry  # noqa: E402


def ensure_db():
    if not db.db_path().exists():
        print("· 合成库不存在，正在生成 ...")
        generate.build()


def find_exam():
    with db.connect() as c:
        r = c.execute(
            """SELECT e.id, e.class_id, cl.name FROM exams e JOIN classes cl ON cl.id=e.class_id
               WHERE cl.name LIKE '%3班%' AND e.course_id=1 LIMIT 1""").fetchone()
    return r["id"], r["class_id"], r["name"]


def main():
    ensure_db()
    eid, cid, cname = find_exam()
    print(f"\n任务：「{cname} 这次 Python 考试谁不及格、普遍错在哪个知识点、给薄弱的同学各推 3 道练习题」")
    print(f"（exam_id={eid}, class_id={cid}）\n" + "=" * 70)

    print("\n① list_exams —— 定位这场考试")
    ex = next(e for e in registry.dispatch("list_exams", {"class_id": cid, "course_id": 1})["exams"]
              if e["id"] == eid)
    print(f"   {ex['exam_name']} | 提交{ex['submit_count']}人 均分{ex['avg_score']} 通过率{ex['pass_rate']}%")

    print("\n② query_student_scores(only_failed) —— 谁不及格")
    failed = registry.dispatch("query_student_scores", {"exam_id": eid, "only_failed": True})
    print(f"   不及格 {failed['total']} 人，最低几名：")
    for r in failed["records"][-3:]:
        print(f"     {r['student_name']}(学号{r['student_no']}) {r['score']}/{r['total_score']} 排名{r['rank']}")
    sid = failed["records"][-1]["student_id"]

    print("\n③ analyze_class_errors —— 普遍错在哪些题/知识点")
    errs = registry.dispatch("analyze_class_errors", {"exam_id": eid, "top": 5})
    for q in errs["error_questions"]:
        print(f"     q{q['question_id']} [{q['difficulty']}] {q['knowledge_point_name']} "
              f"错误率{int(q['error_rate']*100)}% ({q['error_count']}/{q['total_count']})")
    weak_kp = errs["error_questions"][0]["knowledge_point_name"]

    print(f"\n④ query_knowledge_graph(prerequisites) —— 「{weak_kp}」的前置知识点")
    kg = registry.dispatch("query_knowledge_graph",
                           {"course_id": 1, "operation": "prerequisites", "node": weak_kp})
    print("     " + " ← ".join(n["name"] for n in kg["prerequisites"][:6]))

    print(f"\n⑤ recommend_study_path —— 给薄弱学生(id={sid})推学习路径 + 每点 3 题")
    path = registry.dispatch("recommend_study_path",
                             {"student_id": sid, "course_id": 1, "questions_per_point": 3})
    print(f"   目标知识点：{path['target']['name']}（该生薄弱点共 {path['weak_point_count']} 个）")
    for step in path["path"]:
        m = step["mastery_rate"]
        qs = "、".join(f"q{q['id']}" for q in step["practice_questions"]) or "（暂无题）"
        print(f"     - {step['name']}({step['type']}) 掌握度{m if m is not None else '—'} → 练习 {qs}")

    print("\n" + "=" * 70 + "\n✓ 多工具轨迹跑通：查询→分析→知识图谱→个性化推荐 闭环。")


if __name__ == "__main__":
    main()
