"""agentic 评测 demo：对全任务集跑评测并打印报告。

  # 离线（无 key）：用确定性 oracle 回放期望轨迹，验证评测框架本身
  uv run python scripts/eval_demo.py

  # 接真引擎出真数（同一套 harness，无需改代码）：
  export EDU_AGENT_ENGINE=openai
  export EDU_AGENT_BASE_URL=...   # DashScope / 本地 vLLM / 算法仓 W4A16 端点
  export EDU_AGENT_API_KEY=...    # 本地 vLLM 可填占位
  export EDU_AGENT_MODEL=...      # 如 qwen-plus / Qwen/Qwen3-14B
  uv run python scripts/eval_demo.py --engine openai

oracle 跑出的指标必然接近满分——它只验证「任务加载 / 工具执行回灌 / 指标计算」正确；
真实模型能力须用 --engine openai 接真引擎后跑出。
"""
import argparse
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from edu_agent.data import db, generate  # noqa: E402
from edu_agent.eval import build_tasks, format_report, make_oracle_engine, run_eval  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="agentic 评测 demo")
    ap.add_argument("--engine", choices=["oracle", "openai"], default="oracle",
                    help="oracle=离线确定性回放（默认）；openai=接真引擎出真数")
    args = ap.parse_args()

    # 固定 seed-42 合成库到临时文件，保证可复现
    db_path = os.path.join(tempfile.gettempdir(), "edu_agent_eval.db")
    generate.build(seed=42, out_path=db_path)
    os.environ["EDU_AGENT_DB"] = db_path
    conn = db.connect(db_path)

    try:
        tasks = build_tasks(conn)
        if args.engine == "oracle":
            print(f"引擎: 离线 oracle（确定性回放，仅验证评测框架本身） · 任务数 {len(tasks)}\n")
            make_engine = make_oracle_engine
        else:
            os.environ.setdefault("EDU_AGENT_ENGINE", "openai")
            from edu_agent.engine import get_engine
            shared = get_engine()
            print(f"引擎: OpenAI 兼容端点 model={shared.model} · 任务数 {len(tasks)}\n")

            def make_engine(_task):
                return shared

        report = run_eval(tasks, make_engine, db_conn=conn)
    finally:
        conn.close()

    print(format_report(report))


if __name__ == "__main__":
    main()
