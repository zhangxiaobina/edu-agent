"""MCP 现场 demo：工具经 MCP server（独立进程）+ MCP 协议被 LangGraph Agent 调用。

    python scripts/mcp_demo.py

与 scripts/agent_demo.py 跑同一个多工具任务、同一个离线 mock 大脑，唯一区别是工具不再
本地直调，而是经 MCP server 往返——证明「16 个教学工具已暴露为 MCP server，并被 Agent
经 MCP 协议调用跑通」。接真模型时把 MockEngine 换成 get_engine() 即可（同一张图、同一 provider）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from edu_agent.agent import run_agent  # noqa: E402
from edu_agent.agent.demo_policy import demo_policy  # noqa: E402
from edu_agent.data import db, generate  # noqa: E402
from edu_agent.engine.mock import MockEngine  # noqa: E402
from edu_agent.mcp import MCPToolProvider  # noqa: E402


def main():
    if not db.db_path().exists():
        generate.build()
    task = "三班这次 Python 考试谁不及格、普遍错在哪个知识点、给薄弱的同学各推 3 道练习题"
    print(f"用户任务：{task}\n" + "=" * 70)

    provider = MCPToolProvider().start()
    print(f"✓ MCP server 已启动；经 MCP 协议发现 {len(provider.tool_names())} 个工具：")
    print("  " + "、".join(provider.tool_names()))
    try:
        result = run_agent(task, MockEngine(demo_policy), tools_provider=provider)
    finally:
        provider.close()

    print("\n【Agent 工具调用轨迹（每一步都经 MCP 协议往返）】")
    for i, step in enumerate(result["trace"], 1):
        print(f"  {i}. {step['tool']}  {step['arguments']}")
    print("\n【最终回答】")
    print("  " + (result["final_answer"] or "(无)"))
    print("\n" + "=" * 70)
    print(f"✓ 工具经 MCP 协议被 LangGraph Agent 调用跑通：{len(result['trace'])} 次调用 → 综合回答。")


if __name__ == "__main__":
    main()
