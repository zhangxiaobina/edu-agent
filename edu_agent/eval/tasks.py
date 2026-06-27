"""agentic 评测任务集：5 类（single / multi_step / parallel / relevance / irrelevance），
全部锚定 seed-42 可复现合成库，对照 BFCL V4 的能力维度自建。

每个任务声明：
- query           ：用户自然语言（中文，交给被测引擎）
- expected_tools  ：期望的工具调用序列（ExpectedCall），用于工具选择 / 参数（AST 式）打分
- success         ：轨迹成功判据（必需工具 + 顺序 + 答案关键词 / 或 forbid_tools）
- should_call_tool：relevance 语义（该不该调用任何工具）

参数匹配用 possible-answer（可接受值集合）或 ANY（仅需存在）——既能精确校验，又不会因
真实模型选了同样合理的别的取值而误判。动态 ID（exam_id / 薄弱生）在 build_tasks 里从库
中解析为具体值，故任务集对 seed-42 自洽、可复现。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

# 参数匹配哨兵：依赖前序工具结果的动态参数，只要存在（任意值）即算命中。
ANY = object()

CATEGORIES = ("single", "multi_step", "parallel", "relevance", "irrelevance")


@dataclass
class ExpectedCall:
    """一次期望的工具调用。args 为匹配器；send 为离线 oracle 实发的具体参数。"""
    tool: object                              # str 或 list[str]（任一可接受）
    args: dict = field(default_factory=dict)  # 匹配器：param -> 标量/列表(possible)/ANY
    send: dict | None = None                  # oracle 实发参数；None 则从 args 推导

    def oracle_call(self) -> tuple[str, dict]:
        name = self.tool[0] if isinstance(self.tool, list) else self.tool
        if self.send is not None:
            return name, dict(self.send)
        concrete = {}
        for k, v in self.args.items():
            if v is ANY:
                continue
            concrete[k] = v[0] if isinstance(v, (list, tuple)) else v
        return name, concrete


@dataclass
class SuccessSpec:
    required_tools: list = field(default_factory=list)  # 每项 str 或 list[str]（任一）
    ordered: bool = True                                # 必需工具是否需按相对顺序出现
    answer_contains: tuple = ()                         # 最终回答须包含的子串
    forbid_tools: bool = False                          # irrelevance：一个工具都不许调


@dataclass
class EvalTask:
    id: str
    category: str
    query: str
    expected_tools: list = field(default_factory=list)  # list[ExpectedCall]
    success: SuccessSpec = field(default_factory=SuccessSpec)
    should_call_tool: bool = True
    parallel: bool = False                              # oracle 是否在一步内并行发起
    oracle: str = "auto"                                # "demo"=复用 demo_policy 动态决策
    notes: str = ""


# --------------------------------------------------------------------------- #
# 从 seed-42 库解析动态锚点
# --------------------------------------------------------------------------- #
def _exam(conn: sqlite3.Connection, class_id: int, course_id: int) -> int | None:
    row = conn.execute("SELECT id FROM exams WHERE class_id=? AND course_id=? LIMIT 1",
                       (class_id, course_id)).fetchone()
    return row["id"] if row else None


def _a_failed_student(conn: sqlite3.Connection, exam_id: int) -> int | None:
    row = conn.execute(
        "SELECT student_id FROM exam_records WHERE exam_id=? AND passed=0 ORDER BY rank DESC LIMIT 1",
        (exam_id,)).fetchone()
    return row["student_id"] if row else None


# 三班 = class 3；Python = course 1；数据结构 = course 2；Python 题库 = bank 1。
CLASS3, PY, DS = 3, 1, 2

# relevance 类「该调哪个工具都行」的查询/分析工具组
_QUERY_OR_ANALYSIS = ["list_exams", "query_student_scores", "get_score_distribution",
                      "analyze_class_errors", "get_class_roster", "diagnose_weak_points"]


def build_tasks(conn: sqlite3.Connection, include_derived: bool = False) -> list[EvalTask]:
    """解析锚点后构造全部评测任务（对 seed-42 库可复现）。

    include_derived=False（默认）：仅返回冻结的 19 题基准集（保 before/after 对照可比）。
    include_derived=True：再追加 tasks_derived 把 6 个 multi_step 模板铺满 8 锚点的派生集，
    供 DPO dump 取更多真实多步轨迹（见 tasks_derived.build_derived_tasks）。"""
    py_exam = _exam(conn, CLASS3, PY)
    ds_exam = _exam(conn, CLASS3, DS)
    sid = _a_failed_student(conn, py_exam)

    tasks: list[EvalTask] = []

    # ===================== single（单工具） ===================== #
    tasks += [
        EvalTask(
            "single-list-exams", "single",
            "列一下三班都安排了哪些考试",
            [ExpectedCall("list_exams", {"class_id": CLASS3})],
            SuccessSpec(["list_exams"]),
        ),
        EvalTask(
            "single-score-dist", "single",
            "看看三班这次 Python 考试的成绩分布情况",
            [ExpectedCall("get_score_distribution", {"exam_id": [py_exam]},
                          send={"exam_id": py_exam})],
            SuccessSpec(["get_score_distribution"]),
        ),
        EvalTask(
            "single-roster", "single",
            "把三班的学生名单拉出来给我",
            [ExpectedCall("get_class_roster", {"class_id": CLASS3})],
            SuccessSpec(["get_class_roster"]),
        ),
        EvalTask(
            "single-search-questions", "single",
            "帮我找几道讲“递归”的题",
            [ExpectedCall("search_questions", {"course_id": [PY], "knowledge_point": ANY},
                          send={"course_id": PY, "knowledge_point": "递归", "page_size": 5})],
            SuccessSpec(["search_questions"]),
        ),
        EvalTask(
            "single-run-code", "single",
            "帮我跑一下这段 Python：print(1+1)，期望输出是 2",
            [ExpectedCall("run_code", {"source_code": ANY, "expected_output": ["2"]},
                          send={"source_code": "print(1+1)", "expected_output": "2"})],
            SuccessSpec(["run_code"]),
        ),
    ]

    # ===================== multi_step（多步依赖） ===================== #
    tasks += [
        EvalTask(
            "multi-flagship", "multi_step",
            "三班这次 Python 考试谁不及格、普遍错在哪个知识点、给薄弱的同学各推 3 道练习题",
            [
                ExpectedCall("list_exams", {"class_id": CLASS3, "course_id": PY}),
                ExpectedCall("query_student_scores", {"exam_id": [py_exam], "only_failed": [True]},
                             send={"exam_id": py_exam, "only_failed": True}),
                ExpectedCall("analyze_class_errors", {"exam_id": [py_exam]},
                             send={"exam_id": py_exam, "top": 5}),
                ExpectedCall("query_knowledge_graph",
                             {"course_id": [PY], "operation": ["prerequisites"], "node": ANY},
                             send={"course_id": PY, "operation": "prerequisites", "node": "递归"}),
                ExpectedCall("recommend_study_path", {"student_id": ANY, "course_id": [PY]},
                             send={"student_id": sid, "course_id": PY, "questions_per_point": 3}),
            ],
            SuccessSpec(["list_exams", "query_student_scores", "analyze_class_errors",
                         "query_knowledge_graph", "recommend_study_path"],
                        answer_contains=("不及格",)),
            oracle="demo",  # 复用 demo_policy：按前序工具真实返回动态决策
            notes="旗舰多步任务；离线用动态 demo_policy 驱动，证明 harness 能评动态轨迹。",
        ),
        EvalTask(
            "multi-fail-then-dist", "multi_step",
            "三班 Python 考试有多少人不及格？再把这场考试的成绩分布给我",
            [
                ExpectedCall("list_exams", {"class_id": CLASS3, "course_id": PY}),
                ExpectedCall("query_student_scores", {"exam_id": [py_exam], "only_failed": [True]},
                             send={"exam_id": py_exam, "only_failed": True}),
                ExpectedCall("get_score_distribution", {"exam_id": [py_exam]},
                             send={"exam_id": py_exam}),
            ],
            SuccessSpec(["list_exams", "query_student_scores", "get_score_distribution"]),
        ),
        EvalTask(
            "multi-classweak-practice", "multi_step",
            "诊断一下三班 Python 全班最薄弱的知识点，并给最弱的那个点找 3 道练习题",
            [
                ExpectedCall("diagnose_weak_points", {"class_id": [CLASS3], "course_id": [PY]},
                             send={"class_id": CLASS3, "course_id": PY}),
                ExpectedCall("search_questions", {"course_id": [PY], "knowledge_point": ANY},
                             send={"course_id": PY, "knowledge_point": "递归", "page_size": 3}),
            ],
            SuccessSpec(["diagnose_weak_points", "search_questions"]),
        ),
        EvalTask(
            "multi-prereq-path", "multi_step",
            "“递归”这个知识点的前置有哪些？再给我从“函数定义”到“递归”的学习路径",
            [
                ExpectedCall("query_knowledge_graph",
                             {"course_id": [PY], "operation": ["prerequisites"], "node": ["递归"]},
                             send={"course_id": PY, "operation": "prerequisites", "node": "递归"}),
                ExpectedCall("query_knowledge_graph",
                             {"course_id": [PY], "operation": ["path"],
                              "node": ["函数定义"], "target": ["递归"]},
                             send={"course_id": PY, "operation": "path",
                                   "node": "函数定义", "target": "递归"}),
            ],
            SuccessSpec(["query_knowledge_graph", "query_knowledge_graph"]),
        ),
        EvalTask(
            "multi-paper-create-exam", "multi_step",
            "用 Python 题库自动组一套 10 道题的卷子，然后给三班建一场 Python 期中考试",
            [
                ExpectedCall("generate_paper", {"question_bank_id": [PY]},
                             send={"question_bank_id": PY, "total_questions": 10}),
                ExpectedCall("create_exam", {"exam_name": ANY, "class_id": [CLASS3],
                                             "course_id": [PY]},
                             send={"exam_name": "Python 期中考试（评测）",
                                   "class_id": CLASS3, "course_id": PY}),
            ],
            SuccessSpec(["generate_paper", "create_exam"]),
            notes="写操作类：组卷预览 → 建草稿考试。",
        ),
        EvalTask(
            "multi-student-diagnose-path", "multi_step",
            f"学生 {sid} 这门 Python 课哪里薄弱？给他推一条学习路径",
            [
                ExpectedCall("diagnose_weak_points", {"student_id": [sid], "course_id": [PY]},
                             send={"student_id": sid, "course_id": PY}),
                ExpectedCall("recommend_study_path", {"student_id": [sid], "course_id": [PY]},
                             send={"student_id": sid, "course_id": PY}),
            ],
            SuccessSpec(["diagnose_weak_points", "recommend_study_path"]),
        ),
    ]

    # ===================== parallel（一轮并行多调用） ===================== #
    tasks.append(EvalTask(
        "parallel-two-distributions", "parallel",
        "把三班 Python 和数据结构这两场考试的成绩分布都给我",
        [
            ExpectedCall("get_score_distribution", {"exam_id": [py_exam]}, send={"exam_id": py_exam}),
            ExpectedCall("get_score_distribution", {"exam_id": [ds_exam]}, send={"exam_id": ds_exam}),
        ],
        SuccessSpec(["get_score_distribution", "get_score_distribution"]),
        parallel=True,
        notes="对照 BFCL parallel：一轮内并行发起两次同名工具调用。",
    ))

    # ===================== relevance（该调工具，调哪个合理都算对） ===================== #
    tasks += [
        EvalTask(
            "rel-implicit-exam", "relevance",
            "三班这次 Python 大概考得怎么样？",
            [ExpectedCall(_QUERY_OR_ANALYSIS, {}, send={"class_id": CLASS3, "course_id": PY})],
            SuccessSpec([_QUERY_OR_ANALYSIS], ordered=False),
            notes="隐含需要查数据 → 该调查询/分析类工具，具体调哪个都算对。",
        ),
        EvalTask(
            "rel-search-tree", "relevance",
            "有没有讲二叉树的练习题？",
            [ExpectedCall("search_questions", {"course_id": [DS], "knowledge_point": ANY},
                          send={"course_id": DS, "knowledge_point": "二叉树", "page_size": 5})],
            SuccessSpec(["search_questions"], ordered=False),
        ),
        EvalTask(
            "rel-class-weak", "relevance",
            "三班整体上哪些知识点掌握得最差？",
            [ExpectedCall("diagnose_weak_points", {"class_id": [CLASS3]},
                          send={"class_id": CLASS3, "course_id": PY})],
            SuccessSpec(["diagnose_weak_points"], ordered=False),
        ),
    ]

    # ===================== irrelevance（不该调任何工具） ===================== #
    tasks += [
        EvalTask("irr-greeting", "irrelevance", "你好呀，你是谁？",
                 success=SuccessSpec(forbid_tools=True), should_call_tool=False),
        EvalTask("irr-thanks", "irrelevance", "好的，谢谢哈，辛苦啦！",
                 success=SuccessSpec(forbid_tools=True), should_call_tool=False),
        EvalTask("irr-concept", "irrelevance", "Python 里 list 和 tuple 有什么区别？",
                 success=SuccessSpec(forbid_tools=True), should_call_tool=False,
                 notes="纯概念解释，直接回答即可，不该为用工具而查库。"),
        EvalTask("irr-out-of-scope", "irrelevance", "帮我订一张明天飞北京的机票",
                 success=SuccessSpec(forbid_tools=True), should_call_tool=False,
                 notes="超出教学教务域，应礼貌说明无法处理，不调工具。"),
    ]

    if include_derived:
        from .tasks_derived import build_derived_tasks
        tasks += build_derived_tasks(conn)

    return tasks
