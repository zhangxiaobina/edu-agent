"""MCP 集成测试：工具经 MCP server + MCP 协议被 LangGraph Agent 调用，
其工具清单 / 单次结果 / 多步轨迹均与本地 registry 直调一致。

需要可选依赖 mcp（在 uv venv 中运行）：python -m pytest tests/test_mcp.py -q
"""
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("mcp")

from edu_agent.agent import run_agent  # noqa: E402
from edu_agent.agent.demo_policy import demo_policy  # noqa: E402
from edu_agent.data import generate  # noqa: E402
from edu_agent.engine.mock import MockEngine  # noqa: E402
from edu_agent.mcp import MCPToolProvider  # noqa: E402
from edu_agent.tools import registry  # noqa: E402

DB_PATH = os.path.join(tempfile.gettempdir(), "edu_agent_mcp_test.db")


def _norm(d):
    """按 MCP 链路同样的 JSON round-trip 归一化，公平对比本地直调结果。"""
    return json.loads(json.dumps(d, ensure_ascii=False, default=str))


@pytest.fixture(scope="module")
def provider():
    generate.build(seed=42, out_path=DB_PATH)
    os.environ["EDU_AGENT_DB"] = DB_PATH  # MCP server 子进程经 env 继承，连同一个合成库
    p = MCPToolProvider().start()
    yield p
    p.close()


def test_mcp_lists_all_tools(provider):
    """经 MCP 协议发现的工具与本地 registry 完全一致（16 个、同名）。"""
    assert set(provider.tool_names()) == set(registry.tool_names())
    assert len(provider.tool_names()) == 16
    for t in provider.openai_tools():  # schema 结构可直接喂 OpenAI 兼容接口
        assert t["type"] == "function"
        assert "name" in t["function"] and "parameters" in t["function"]


def test_mcp_dispatch_matches_registry(provider):
    """同一工具、同参数，经 MCP 往返的结果与本地直调一致。"""
    args = {"class_id": 3, "course_id": 1}
    assert provider.dispatch("list_exams", args) == _norm(registry.dispatch("list_exams", args))


def test_mcp_unknown_tool_returns_error(provider):
    """未知工具经 MCP 返回结构化错误（与 registry 同口径），不抛异常。"""
    assert "error" in provider.dispatch("no_such_tool", {})


def test_mcp_multistep_trajectory_matches_local(provider):
    """同一 mock 大脑，工具经 MCP 协议时跑出与本地一致的多步轨迹与回答。"""
    task = "三班这次 Python 考试谁不及格、普遍错在哪、给薄弱同学各推 3 题"
    result = run_agent(task, MockEngine(demo_policy), tools_provider=provider)
    assert [t["tool"] for t in result["trace"]] == [
        "list_exams", "query_student_scores", "analyze_class_errors",
        "query_knowledge_graph", "recommend_study_path",
    ]
    assert result["final_answer"] and "不及格" in result["final_answer"]


if __name__ == "__main__":  # 允许脱离 pytest 直接跑
    generate.build(seed=42, out_path=DB_PATH)
    os.environ["EDU_AGENT_DB"] = DB_PATH
    p = MCPToolProvider().start()
    try:
        test_mcp_lists_all_tools(p)
        test_mcp_dispatch_matches_registry(p)
        test_mcp_unknown_tool_returns_error(p)
        test_mcp_multistep_trajectory_matches_local(p)
        print("MCP tests passed")
    finally:
        p.close()
