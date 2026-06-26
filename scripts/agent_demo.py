"""Agent 现场 demo：用离线 mock 引擎 + LangGraph 编排跑通多工具任务。

  python scripts/agent_demo.py

接真模型时：设环境变量 EDU_AGENT_ENGINE=openai + EDU_AGENT_BASE_URL/API_KEY/MODEL，
用 edu_agent.engine.get_engine() 取引擎，传给 run_agent 即可（同一张图，无需改代码）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from edu_agent.agent import run_agent  # noqa: E402
from edu_agent.agent.demo_policy import demo_policy  # noqa: E402
from edu_agent.data import db, generate  # noqa: E402
from edu_agent.engine.mock import MockEngine  # noqa: E402


def main():
    if not db.db_path().exists():
        generate.build()
    task = "三班这次 Python 考试谁不及格、普遍错在哪个知识点、给薄弱的同学各推 3 道练习题"
    print(f"用户任务：{task}\n" + "=" * 70)
    result = run_agent(task, MockEngine(demo_policy))

    print("\n【Agent 工具调用轨迹】")
    for i, step in enumerate(result["trace"], 1):
        print(f"  {i}. {step['tool']}  {step['arguments']}")

    print("\n【最终回答】")
    print("  " + (result["final_answer"] or "(无)"))
    print("\n" + "=" * 70)
    print(f"✓ LangGraph 多工具编排跑通：{len(result['trace'])} 次工具调用 → 综合回答。")


if __name__ == "__main__":
    main()
