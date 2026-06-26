"""评测框架测试：

1. 任务集结构合法（id 唯一、类别合法、期望工具都在注册表里）。
2. 用离线 oracle 跑全集 → 轨迹成功率/参数准确率/relevance 判对率 == 100%
   （证明 harness 把「正确行为」判为成功）。
3. 指标能区分对错（喂入残缺/越界轨迹 → 相应指标下降；证明不是恒为满分）。

需 langgraph（在 uv venv 中）：uv run python -m pytest tests/test_eval.py -q
零依赖运行（仅指标判别部分会因缺 langgraph 跳过 oracle 跑批）：见 __main__。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from edu_agent.data import db, generate  # noqa: E402
from edu_agent.eval import build_tasks, format_report, make_oracle_engine, run_eval  # noqa: E402
from edu_agent.eval import metrics  # noqa: E402
from edu_agent.eval.tasks import CATEGORIES, EvalTask, ExpectedCall, SuccessSpec  # noqa: E402
from edu_agent.tools import registry  # noqa: E402

DB_PATH = os.path.join(tempfile.gettempdir(), "edu_agent_eval_test.db")
_CONN = None


def setup_module(module=None):
    global _CONN
    generate.build(seed=42, out_path=DB_PATH)
    os.environ["EDU_AGENT_DB"] = DB_PATH
    _CONN = db.connect(DB_PATH)


def teardown_module(module=None):
    if _CONN is not None:
        _CONN.close()


# ----------------------------- 1. 任务集结构 ----------------------------- #
def test_tasks_wellformed():
    tasks = build_tasks(_CONN)
    assert len(tasks) >= 15
    ids = [t.id for t in tasks]
    assert len(ids) == len(set(ids)), "任务 id 必须唯一"
    valid_tools = set(registry.tool_names())
    for t in tasks:
        assert t.category in CATEGORIES
        assert t.query.strip()
        for ec in t.expected_tools:
            names = ec.tool if isinstance(ec.tool, list) else [ec.tool]
            assert set(names) <= valid_tools, f"{t.id} 引用了未知工具 {names}"
        # oracle 实发参数里的工具名也须合法
        for ec in t.expected_tools:
            name, _ = ec.oracle_call()
            assert name in valid_tools


def test_all_categories_covered():
    cats = {t.category for t in build_tasks(_CONN)}
    assert cats == set(CATEGORIES), f"应覆盖全部类别，缺: {set(CATEGORIES) - cats}"


# ----------------------- 2. oracle 跑全集 → 满分 ----------------------- #
def test_oracle_full_run_is_correct():
    tasks = build_tasks(_CONN)
    report = run_eval(tasks, make_oracle_engine, db_conn=_CONN)
    print("\n" + format_report(report))
    assert report["trajectory_success_rate"] == 1.0, \
        "oracle 回放期望轨迹应 100% 成功；失败说明 harness 判定有误"
    assert report["param_accuracy"] == 1.0
    assert report["tool_recall"] == 1.0
    assert report["relevance_accuracy"] == 1.0
    # 每类都应被评到
    assert set(report["by_category"]) == set(CATEGORIES)


# ----------------------- 3. 指标能区分对错 ----------------------- #
def _task(tid, cat, expected, success, **kw):
    return EvalTask(tid, cat, "q", expected, success, **kw)


def test_metric_penalizes_missing_required_tool():
    task = _task("m", "multi_step",
                 [ExpectedCall("list_exams", {}), ExpectedCall("get_score_distribution", {})],
                 SuccessSpec(["list_exams", "get_score_distribution"]))
    # 只调了第一个必需工具
    result = {"final_answer": "x", "trace": [{"tool": "list_exams", "arguments": "{}"}]}
    rec = metrics.score_task(task, result)
    assert rec["success"] is False
    assert rec["tool_recall"] == 0.5


def test_metric_penalizes_wrong_params():
    task = _task("s", "single",
                 [ExpectedCall("get_score_distribution", {"exam_id": [5]})],
                 SuccessSpec(["get_score_distribution"]))
    result = {"final_answer": "x",
              "trace": [{"tool": "get_score_distribution", "arguments": '{"exam_id": 999}'}]}
    rec = metrics.score_task(task, result)
    assert rec["success"] is True            # 工具对、轨迹达成
    assert rec["param_accuracy"] == 0.0      # 但参数取值不在 possible answers


def test_metric_relevance_discriminates():
    # irrelevance 任务却调了工具 → relevance 判错 + 轨迹失败
    bad = _task("i", "irrelevance", [], SuccessSpec(forbid_tools=True), should_call_tool=False)
    result = {"final_answer": "x", "trace": [{"tool": "list_exams", "arguments": "{}"}]}
    rec = metrics.score_task(bad, result)
    assert rec["relevance_correct"] is False
    assert rec["success"] is False
    # relevance 任务却没调工具 → 判错
    rel = _task("r", "relevance", [ExpectedCall("list_exams", {})],
                SuccessSpec(["list_exams"], ordered=False))
    rec2 = metrics.score_task(rel, {"final_answer": "x", "trace": []})
    assert rec2["relevance_correct"] is False
    assert rec2["success"] is False


def test_metric_penalizes_extraneous_calls():
    task = _task("e", "single", [ExpectedCall("list_exams", {})], SuccessSpec(["list_exams"]))
    result = {"final_answer": "x", "trace": [
        {"tool": "list_exams", "arguments": "{}"},
        {"tool": "get_class_roster", "arguments": '{"class_id": 3}'},  # 多余调用
    ]}
    rec = metrics.score_task(task, result)
    assert rec["success"] is True
    assert rec["tool_precision"] == 0.5      # 2 次里只有 1 次属于期望
    assert rec["extraneous_calls"] == 1


# ----------------------------- 零依赖运行器 ----------------------------- #
if __name__ == "__main__":
    setup_module()
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    ok = 0
    for n, f in fns:
        try:
            f()
            print(f"  PASS  {n}")
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL  {n}: {type(e).__name__}: {e}")
    teardown_module()
    print(f"\n{ok}/{len(fns)} passed")
    sys.exit(0 if ok == len(fns) else 1)
