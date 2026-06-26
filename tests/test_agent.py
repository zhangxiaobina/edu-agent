"""Agent 编排测试：用离线 mock 引擎跑真实 LangGraph 循环 + 真实工具，验证多步轨迹。

需要 langgraph（在 uv venv 中运行）：python -m pytest tests/test_agent.py -q
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from edu_agent.agent import run_agent  # noqa: E402
from edu_agent.agent.demo_policy import demo_policy  # noqa: E402
from edu_agent.data import generate  # noqa: E402
from edu_agent.engine.base import EngineResponse  # noqa: E402
from edu_agent.engine.mock import MockEngine  # noqa: E402

DB_PATH = os.path.join(tempfile.gettempdir(), "edu_agent_test.db")


def setup_module(module=None):
    generate.build(seed=42, out_path=DB_PATH)
    os.environ["EDU_AGENT_DB"] = DB_PATH


def test_demo_trajectory_through_agent_loop():
    task = "三班这次 Python 考试谁不及格、普遍错在哪、给薄弱同学各推 3 题"
    result = run_agent(task, MockEngine(demo_policy))
    tools_used = [t["tool"] for t in result["trace"]]
    # 期望的多步轨迹（依赖前序工具结果动态决定）
    assert tools_used == [
        "list_exams", "query_student_scores", "analyze_class_errors",
        "query_knowledge_graph", "recommend_study_path",
    ]
    # 最终回答综合了真实工具数据
    assert result["final_answer"]
    assert "不及格" in result["final_answer"]
    assert "q" in result["final_answer"]  # 含练习题号


def test_relevance_no_tool_for_chitchat():
    """relevance：寒暄类问题不该调用工具，直接回答。"""
    def chitchat_policy(messages, tools, step):
        return EngineResponse(content="你好！我是教学教务智能助手，有什么可以帮你？")
    result = run_agent("你好呀", MockEngine(chitchat_policy))
    assert result["trace"] == []
    assert result["final_answer"]


def test_agent_executes_real_tool_results():
    """断言工具节点确实把真实结果回灌给了引擎（最终人数与 DB 一致）。"""
    from edu_agent.tools import registry
    result = run_agent("三班 Python 考试不及格情况", MockEngine(demo_policy))
    # 从 demo_policy 复现的 exam 取真实不及格数
    exams = registry.dispatch("list_exams", {"class_id": 3, "course_id": 1})["exams"]
    eid = next(e["id"] for e in exams if e["course_id"] == 1)
    failed = registry.dispatch("query_student_scores", {"exam_id": eid, "only_failed": True})["total"]
    assert str(failed) in result["final_answer"]


if __name__ == "__main__":
    setup_module()
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    ok = 0
    for n, f in fns:
        try:
            f()
            print(f"  PASS  {n}")
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL  {n}: {type(e).__name__}: {e}")
    print(f"\n{ok}/{len(fns)} passed")
    sys.exit(0 if ok == len(fns) else 1)
