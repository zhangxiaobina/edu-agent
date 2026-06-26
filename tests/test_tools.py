"""工具层冒烟测试：每个工具可跑且返回 mirror 形态数据，并验证 demo 多工具轨迹。

零依赖运行：  python tests/test_tools.py
pytest 运行： python -m pytest tests/ -q
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from edu_agent.data import db, generate  # noqa: E402
from edu_agent.tools import registry  # noqa: E402

DB_PATH = os.path.join(tempfile.gettempdir(), "edu_agent_test.db")


def setup_module(module=None):
    generate.build(seed=42, out_path=DB_PATH)
    os.environ["EDU_AGENT_DB"] = DB_PATH


def _conn():
    return db.connect(DB_PATH)


def _python_exam_3ban():
    """返回 3 班的 Python 考试 (exam_id, class_id)。"""
    with _conn() as c:
        row = c.execute(
            """SELECT e.id, e.class_id FROM exams e JOIN classes cl ON cl.id=e.class_id
               WHERE cl.name LIKE '%3班%' AND e.course_id=1 LIMIT 1""").fetchone()
    return row["id"], row["class_id"]


# ---------------- schema / 注册表 ----------------
def test_registry_parity():
    assert len(registry.tool_names()) == 16
    tools = registry.openai_tools()
    assert all(t["type"] == "function" and "name" in t["function"] for t in tools)


# ---------------- 查询类 ----------------
def test_query_student_scores_only_failed():
    eid, _ = _python_exam_3ban()
    res = registry.dispatch("query_student_scores", {"exam_id": eid, "only_failed": True})
    assert res["total"] > 0
    assert all(r["passed"] == 0 for r in res["records"])
    assert "score_rate" in res["records"][0]


def test_list_exams_for_class():
    _, cid = _python_exam_3ban()
    res = registry.dispatch("list_exams", {"class_id": cid})
    assert res["total"] >= 1
    ex = res["exams"][0]
    assert {"avg_score", "pass_rate", "status_text", "submit_count"} <= set(ex)


def test_get_class_roster():
    _, cid = _python_exam_3ban()
    res = registry.dispatch("get_class_roster", {"class_id": cid})
    assert res["total"] > 0
    stu = res["students"][0]
    assert {"student_name", "student_username", "exam_count", "avg_score"} <= set(stu)


def test_search_questions_by_knowledge_point():
    res = registry.dispatch("search_questions",
                            {"course_id": 1, "knowledge_point": "递归", "page_size": 5})
    assert res["total"] > 0
    assert all("递归" in q["knowledge_points"] for q in res["questions"])


def test_get_learning_progress():
    res = registry.dispatch("get_learning_progress", {"student_id": 1})
    assert res["courses"]
    assert "overall_progress" in res["courses"][0]


# ---------------- 知识图谱 ----------------
def test_query_kg_prerequisites_and_path():
    pre = registry.dispatch("query_knowledge_graph",
                            {"course_id": 1, "operation": "prerequisites", "node": "递归"})
    assert pre["count"] > 0
    path = registry.dispatch("query_knowledge_graph",
                             {"course_id": 1, "operation": "path", "node": "函数定义", "target": "递归"})
    assert path["path"] and path["path"][0]["name"] == "函数定义"
    assert path["path"][-1]["name"] == "递归"
    assert "cypher_hint" in path


def test_query_kg_find():
    res = registry.dispatch("query_knowledge_graph",
                            {"course_id": 1, "operation": "find", "node_type": "skill"})
    assert res["count"] > 0 and all(n["type"] == "skill" for n in res["nodes"])


# ---------------- 分析类 ----------------
def test_analyze_class_errors():
    eid, _ = _python_exam_3ban()
    res = registry.dispatch("analyze_class_errors", {"exam_id": eid, "top": 5})
    assert len(res["error_questions"]) == 5
    top = res["error_questions"][0]
    assert top["error_count"] >= res["error_questions"][-1]["error_count"]  # 降序
    assert "knowledge_point_name" in top


def test_diagnose_weak_points_student_and_class():
    eid, cid = _python_exam_3ban()
    # 取一个不及格学生
    with _conn() as c:
        sid = c.execute("SELECT student_id FROM exam_records WHERE exam_id=? AND passed=0 LIMIT 1",
                        (eid,)).fetchone()["student_id"]
    stu = registry.dispatch("diagnose_weak_points", {"student_id": sid, "course_id": 1})
    assert stu["weak_points"] and all(w["mastery_rate"] < 0.6 for w in stu["weak_points"])
    cls = registry.dispatch("diagnose_weak_points", {"class_id": cid, "course_id": 1})
    assert cls["scope"] == "class"


def test_get_score_distribution():
    eid, _ = _python_exam_3ban()
    res = registry.dispatch("get_score_distribution", {"exam_id": eid})
    assert res["total_students"] > 0
    assert sum(d["student_count"] for d in res["distribution"]) == res["total_students"]
    assert 0 <= res["pass_rate"] <= 100


# ---------------- 操作类（写入） ----------------
def test_create_exam():
    res = registry.dispatch("create_exam",
                            {"exam_name": "单元测验-单测", "class_id": 3, "course_id": 1,
                             "duration": 60, "total_score": 50, "pass_score": 30})
    assert res["created"] and res["status"] == 0
    with _conn() as c:
        assert c.execute("SELECT 1 FROM exams WHERE id=?", (res["exam_id"],)).fetchone()


def test_generate_paper():
    res = registry.dispatch("generate_paper",
                            {"question_bank_id": 1, "total_questions": 8,
                             "difficulty_distribution": {"easy": 3, "medium": 3, "hard": 2}})
    assert res["total_questions"] == 8
    assert res["difficulty_distribution"].get("hard", 0) == 2
    assert 0 <= res["quality_score"] <= 1


def test_batch_grade_idempotent():
    eid, _ = _python_exam_3ban()
    res = registry.dispatch("batch_grade", {"exam_id": eid, "regrade": True})
    assert res["graded_count"] > 0 and res["failed_count"] == 0


def test_assign_homework():
    res = registry.dispatch("assign_homework",
                            {"title": "第一次作业-单测", "course_id": 1, "class_ids": [3],
                             "end_time": "2025-12-31 23:59:59"})
    assert res["created"] and 3 in res["class_ids"]


# ---------------- AI / 执行 ----------------
def test_generate_questions():
    res = registry.dispatch("generate_questions",
                            {"course_id": 1, "knowledge_point": "递归", "count": 4})
    assert res["created_questions"] == 4
    assert res["knowledge_point"] == "递归"
    assert all(q["source"] == "ai" for q in res["questions"])


def test_run_code_paths():
    ok = registry.dispatch("run_code", {"source_code": "print(1+1)", "expected_output": "2"})
    assert ok["outcome"] == "AC" and ok["passed"]
    wa = registry.dispatch("run_code", {"source_code": "print(3)", "expected_output": "2"})
    assert wa["outcome"] == "WA" and not wa["passed"]
    ce = registry.dispatch("run_code", {"source_code": "def f(:"})
    assert ce["outcome"] == "CE"
    re_ = registry.dispatch("run_code", {"source_code": "1/0"})
    assert re_["outcome"] == "RE"
    tle = registry.dispatch("run_code", {"source_code": "while True: pass", "timeout": 2})
    assert tle["outcome"] == "TLE"


# ---------------- demo 多工具轨迹（端到端，单引擎驱动前的工具可达性验证） ----------------
def test_demo_trajectory():
    """三班 Python 考试：谁不及格 → 普遍错在哪 → 知识点 → 给薄弱生推 3 题。"""
    eid, cid = _python_exam_3ban()
    # 1. 列考试
    exams = registry.dispatch("list_exams", {"class_id": cid, "course_id": 1})
    assert any(e["id"] == eid for e in exams["exams"])
    # 2. 谁不及格
    failed = registry.dispatch("query_student_scores", {"exam_id": eid, "only_failed": True})
    assert failed["total"] > 0
    sid = failed["records"][0]["student_id"]
    # 3. 普遍错在哪
    errs = registry.dispatch("analyze_class_errors", {"exam_id": eid, "top": 3})
    weak_kp = errs["error_questions"][0]["knowledge_point_name"]
    assert weak_kp
    # 4. 知识图谱定位该知识点前置
    kg = registry.dispatch("query_knowledge_graph",
                           {"course_id": 1, "operation": "prerequisites", "node": weak_kp})
    assert "prerequisites" in kg
    # 5. 给薄弱学生推学习路径 + 练习题
    path = registry.dispatch("recommend_study_path",
                             {"student_id": sid, "course_id": 1, "questions_per_point": 3})
    assert path["path"]
    assert any(p["practice_questions"] for p in path["path"])


# ---------------- 零依赖运行器 ----------------
def _run_all():
    setup_module()
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} passed")
    return passed == len(tests)


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
