"""DPO 用「派生」多步任务集：把 tasks.py 的 6 个 multi_step 模板参数化，铺满 seed-42
合成库现有的 8 个 (班,课) 锚点，给 dump_trajectories 提供更多真实多步轨迹来构造偏好对。

与基准 19 题严格隔离：
- 默认评测 ``build_tasks(conn)`` 不含这些（基准集冻结，用于 before/after 对照）。
- 仅 ``build_tasks(conn, include_derived=True)`` 或本模块 ``build_derived_tasks(conn)`` 返回。

每条派生任务对 seed-42 可复现：所有锚点（exam_id / 薄弱学生 / 班级薄弱知识点 / 知识图谱
先修链）在构造时从库实时解析，故任务集对该库自洽。派生任务一律用静态 oracle（``oracle="auto"``，
回放 expected_tools），不依赖只认 三班/Python 的 demo_policy，因此任意锚点离线 oracle 都可
满分——证明 harness 同样能正确评判它们。

注意：8 个锚点里含 (3班, Python) / (3班, 数据结构)，与基准里的 6 个 multi_step 原题同锚点、
query 近似。派生集刻意做成「完整网格」（自含、覆盖全部 8 锚点），dump DPO 数据时取派生集即可
全覆盖；如同时 dump 基准原题，class3 处会有少量重复 query，由下游 build_dpo_dataset 去重即可。
"""
from __future__ import annotations

import sqlite3

from ..tools.analysis_tools import analyze_class_errors
from .tasks import ANY, EvalTask, ExpectedCall, SuccessSpec

# 课程 id → 任务 id 用的短代号
_COURSE_CODE = {1: "py", 2: "ds", 3: "net"}


# --------------------------------------------------------------------------- #
# 锚点解析（对 seed-42 库可复现）
# --------------------------------------------------------------------------- #
def _exam_id(conn: sqlite3.Connection, class_id: int, course_id: int) -> int | None:
    row = conn.execute("SELECT id FROM exams WHERE class_id=? AND course_id=? LIMIT 1",
                       (class_id, course_id)).fetchone()
    return row["id"] if row else None


def _a_failed_student(conn: sqlite3.Connection, exam_id: int) -> int | None:
    row = conn.execute(
        "SELECT student_id FROM exam_records WHERE exam_id=? AND passed=0 ORDER BY rank DESC LIMIT 1",
        (exam_id,)).fetchone()
    return row["student_id"] if row else None


def _names(conn: sqlite3.Connection, class_id: int, course_id: int) -> tuple[str, str]:
    cl = conn.execute("SELECT name FROM classes WHERE id=?", (class_id,)).fetchone()
    co = conn.execute("SELECT name FROM courses WHERE id=?", (course_id,)).fetchone()
    return cl["name"], co["name"]


def _top_error_kp(conn: sqlite3.Connection, exam_id: int) -> str | None:
    """该场考试错得最多的题对应的知识点名——真实「班级薄弱点」，必为可解析的 concept。"""
    err = analyze_class_errors(conn, exam_id=exam_id, top=5)
    eqs = err.get("error_questions") or []
    return eqs[0]["knowledge_point_name"] if eqs else None


def _prereq_pair(conn: sqlite3.Connection, course_id: int) -> tuple[str | None, str | None]:
    """沿课程 concept 先修链选 (start, target)：target 为有前置的难点 concept，start 为链上更
    靠前的 concept。生成器把相邻 concept 串成一条 PREREQUISITE_OF 链，故 start→target 必有有向
    学习路径（已对 3 门课各验证 path_len=3）。"""
    chain = [r["name"] for r in conn.execute(
        "SELECT name FROM kg_nodes WHERE course_id=? AND type='concept' ORDER BY rowid",
        (course_id,))]
    hard = [r["name"] for r in conn.execute(
        "SELECT name FROM kg_nodes WHERE course_id=? AND type='concept' AND difficulty=4 "
        "ORDER BY rowid", (course_id,))]
    for h in hard:                       # 取首个位置 >=2 的难点作 target，留出前置空间
        i = chain.index(h)
        if i >= 2:
            return chain[i - 2], h
    return (chain[0], chain[-1]) if len(chain) >= 2 else (None, None)


# --------------------------------------------------------------------------- #
# 6 个 multi_step 模板的参数化工厂（逐字对齐 tasks.py 的对应原题，仅锚点参数化）
# --------------------------------------------------------------------------- #
def _t_flagship(cl, co, names, eid, sid, weak_kp, _conn):
    cln, con = names
    return EvalTask(
        f"d-flagship-c{cl}-{_COURSE_CODE[co]}", "multi_step",
        f"{cln}这次{con}考试谁不及格、普遍错在哪个知识点、给薄弱的同学各推 3 道练习题",
        [
            ExpectedCall("list_exams", {"class_id": cl, "course_id": co}),
            ExpectedCall("query_student_scores", {"exam_id": [eid], "only_failed": [True]},
                         send={"exam_id": eid, "only_failed": True}),
            ExpectedCall("analyze_class_errors", {"exam_id": [eid]},
                         send={"exam_id": eid, "top": 5}),
            ExpectedCall("query_knowledge_graph",
                         {"course_id": [co], "operation": ["prerequisites"], "node": ANY},
                         send={"course_id": co, "operation": "prerequisites", "node": weak_kp}),
            ExpectedCall("recommend_study_path", {"student_id": ANY, "course_id": [co]},
                         send={"student_id": sid, "course_id": co, "questions_per_point": 3}),
        ],
        SuccessSpec(["list_exams", "query_student_scores", "analyze_class_errors",
                     "query_knowledge_graph", "recommend_study_path"],
                    answer_contains=("不及格",)),
        oracle="auto",  # 静态回放（非 demo_policy），任意锚点离线可评
        notes="派生 flagship：旗舰多步链，覆盖查询→分析→图谱→推荐五工具依赖。",
    )


def _t_fail_then_dist(cl, co, names, eid, sid, weak_kp, _conn):
    cln, con = names
    return EvalTask(
        f"d-failthendist-c{cl}-{_COURSE_CODE[co]}", "multi_step",
        f"{cln} {con}考试有多少人不及格？再把这场考试的成绩分布给我",
        [
            ExpectedCall("list_exams", {"class_id": cl, "course_id": co}),
            ExpectedCall("query_student_scores", {"exam_id": [eid], "only_failed": [True]},
                         send={"exam_id": eid, "only_failed": True}),
            ExpectedCall("get_score_distribution", {"exam_id": [eid]}, send={"exam_id": eid}),
        ],
        SuccessSpec(["list_exams", "query_student_scores", "get_score_distribution"]),
    )


def _t_classweak_practice(cl, co, names, eid, sid, weak_kp, _conn):
    cln, con = names
    return EvalTask(
        f"d-classweak-c{cl}-{_COURSE_CODE[co]}", "multi_step",
        f"诊断一下{cln} {con}全班最薄弱的知识点，并给最弱的那个点找 3 道练习题",
        [
            ExpectedCall("diagnose_weak_points", {"class_id": [cl], "course_id": [co]},
                         send={"class_id": cl, "course_id": co}),
            ExpectedCall("search_questions", {"course_id": [co], "knowledge_point": ANY},
                         send={"course_id": co, "knowledge_point": weak_kp, "page_size": 3}),
        ],
        SuccessSpec(["diagnose_weak_points", "search_questions"]),
    )


def _t_prereq_path(cl, co, names, eid, sid, weak_kp, conn):
    cln, con = names
    start, target = _prereq_pair(conn, co)
    return EvalTask(
        f"d-prereqpath-c{cl}-{_COURSE_CODE[co]}", "multi_step",
        f"“{target}”这个知识点的前置有哪些？再给我从“{start}”到“{target}”的学习路径",
        [
            ExpectedCall("query_knowledge_graph",
                         {"course_id": [co], "operation": ["prerequisites"], "node": [target]},
                         send={"course_id": co, "operation": "prerequisites", "node": target}),
            ExpectedCall("query_knowledge_graph",
                         {"course_id": [co], "operation": ["path"],
                          "node": [start], "target": [target]},
                         send={"course_id": co, "operation": "path", "node": start, "target": target}),
        ],
        SuccessSpec(["query_knowledge_graph", "query_knowledge_graph"]),
    )


def _t_paper_create_exam(cl, co, names, eid, sid, weak_kp, _conn):
    cln, con = names
    return EvalTask(
        f"d-papercreate-c{cl}-{_COURSE_CODE[co]}", "multi_step",
        f"用{con}题库自动组一套 10 道题的卷子，然后给{cln}建一场{con}期中考试",
        [
            ExpectedCall("generate_paper", {"question_bank_id": [co]},  # 一门课一个主题库，bank_id==course_id
                         send={"question_bank_id": co, "total_questions": 10}),
            ExpectedCall("create_exam", {"exam_name": ANY, "class_id": [cl], "course_id": [co]},
                         send={"exam_name": f"{con}期中考试（评测）", "class_id": cl, "course_id": co}),
        ],
        SuccessSpec(["generate_paper", "create_exam"]),
        notes="写操作类：组卷预览 → 建草稿考试。",
    )


def _t_student_diagnose_path(cl, co, names, eid, sid, weak_kp, _conn):
    cln, con = names
    return EvalTask(
        f"d-studentpath-c{cl}-{_COURSE_CODE[co]}", "multi_step",
        f"学生 {sid} 这门{con}课哪里薄弱？给他推一条学习路径",
        [
            ExpectedCall("diagnose_weak_points", {"student_id": [sid], "course_id": [co]},
                         send={"student_id": sid, "course_id": co}),
            ExpectedCall("recommend_study_path", {"student_id": [sid], "course_id": [co]},
                         send={"student_id": sid, "course_id": co}),
        ],
        SuccessSpec(["diagnose_weak_points", "recommend_study_path"]),
    )


# 工厂与其对锚点的依赖：sid 缺失 → 跳过依赖薄弱学生的两个模板；weak_kp 缺失 → 跳过依赖薄弱点的两个。
_TEMPLATES = [
    (_t_flagship,             ("sid", "kp")),
    (_t_fail_then_dist,       ()),
    (_t_classweak_practice,   ("kp",)),
    (_t_prereq_path,          ()),
    (_t_paper_create_exam,    ()),
    (_t_student_diagnose_path, ("sid",)),
]


def build_derived_tasks(conn: sqlite3.Connection) -> list[EvalTask]:
    """对 seed-42 库的 8 个 (班,课) 锚点逐一参数化 6 个 multi_step 模板。

    完整网格 = 8 × 6 = 48 条（锚点缺薄弱学生/薄弱点时相应模板跳过，对 seed-42 不会发生）。
    """
    combos = conn.execute(
        "SELECT class_id, course_id FROM class_courses ORDER BY class_id, course_id"
    ).fetchall()
    tasks: list[EvalTask] = []
    for row in combos:
        cl, co = row["class_id"], row["course_id"]
        eid = _exam_id(conn, cl, co)
        if eid is None:
            continue
        sid = _a_failed_student(conn, eid)
        weak_kp = _top_error_kp(conn, eid)
        names = _names(conn, cl, co)
        for factory, needs in _TEMPLATES:
            if "sid" in needs and sid is None:
                continue
            if "kp" in needs and weak_kp is None:
                continue
            tasks.append(factory(cl, co, names, eid, sid, weak_kp, conn))
    return tasks
