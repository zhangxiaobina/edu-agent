"""把 EduAgent 评测任务逐个跑过 Agent，dump 完整轨迹(messages/trace/success)成 jsonl，
供 DPO 偏好对构造用。

放到 edu-agent 仓根的 scripts/ 下运行（与 eval_demo.py 同级，复用同一套建库/引擎/评测）：

  # ① base 档（未微调 fp16，作 chosen 来源）—— 端点指向 base
  export EDU_AGENT_ENGINE=openai
  export EDU_AGENT_BASE_URL=http://127.0.0.1:8000/v1
  export EDU_AGENT_API_KEY=dummy
  export EDU_AGENT_MODEL=Qwen/Qwen3-14B
  uv run python scripts/dump_trajectories.py --tag base

  # ② sft 档（微调 merged 或 W4A16，作 rejected 来源）—— 端点切到 SFT 模型后重跑
  export EDU_AGENT_MODEL=Qwen3-14B-FC-W4A16
  uv run python scripts/dump_trajectories.py --tag sft

关键：--nudges 0（默认）关闭编排兜底，dump 出的是「模型层原生轨迹」——
这正是 DPO 要纠正的对象。若用兜底后的轨迹做偏好对，等于把编排层提示的功劳烤进权重，失真。
"""
import argparse
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from edu_agent.agent import run_agent          # noqa: E402
from edu_agent.data import db, generate         # noqa: E402
from edu_agent.eval import build_tasks, metrics  # noqa: E402
from edu_agent.tools import registry            # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="dump EduAgent 轨迹用于 DPO 偏好对构造")
    ap.add_argument("--tag", required=True,
                    help="档位标识，决定输出文件名，如 base / sft / w4a16")
    ap.add_argument("--cats", default="multi_step",
                    help="逗号分隔的任务类别；默认只跑 multi_step（DPO 目标）。"
                         "可加 single,relevance 看附带损伤")
    ap.add_argument("--nudges", type=int, default=0,
                    help="编排兜底强度；DPO 取原生轨迹应设 0（默认）")
    ap.add_argument("--out-dir", default="dpo_dumps", help="输出目录")
    args = ap.parse_args()

    cats = [c.strip() for c in args.cats.split(",") if c.strip()]
    os.makedirs(args.out_dir, exist_ok=True)

    # 固定 seed-42 合成库，与 eval_demo 完全一致，保证任务锚点逐字节可复现
    db_path = os.path.join(tempfile.gettempdir(), "edu_agent_eval.db")
    generate.build(seed=42, out_path=db_path)
    os.environ["EDU_AGENT_DB"] = db_path
    conn = db.connect(db_path)

    from edu_agent.engine import get_engine  # 延迟导入，避免无端点时报错
    engine = get_engine()
    print(f"档位 {args.tag} · 引擎 model={engine.model} · 类别 {cats} · nudges={args.nudges}\n")

    out_path = os.path.join(args.out_dir, f"traj_{args.tag}.jsonl")
    n_ok = 0
    try:
        tasks = [t for t in build_tasks(conn) if t.category in cats]
        with open(out_path, "w", encoding="utf-8") as f:
            for task in tasks:
                try:
                    result = run_agent(task.query, engine, db_conn=conn,
                                       max_nudges=args.nudges)
                except Exception as e:  # noqa: BLE001  单任务失败不中断整批
                    result = {"final_answer": None, "trace": [], "messages": [],
                              "error": f"{type(e).__name__}: {e}"}
                rec = metrics.score_task(task, result)
                success = bool(rec["success"])
                n_ok += int(success)
                row = {
                    "id": task.id,
                    "category": task.category,
                    "query": task.query,
                    "success": success,
                    "final_answer": result.get("final_answer"),
                    "trace": result.get("trace", []),
                    "messages": result.get("messages", []),
                    "error": result.get("error"),
                }
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                mark = "✓" if success else "✗"
                tools = [t["tool"] for t in result.get("trace", [])]
                print(f"  [{mark}] {task.id:32s} tools={tools}")
    finally:
        conn.close()

    # 工具 schema 一并落盘，build 脚本据此生成训练用 tools 字段（每档相同，覆盖写即可）
    tools_path = os.path.join(args.out_dir, "tools.openai.json")
    with open(tools_path, "w", encoding="utf-8") as f:
        json.dump(registry.openai_tools(), f, ensure_ascii=False, indent=2)

    print(f"\n档位 {args.tag}: {len(tasks)} 任务, {n_ok} 成功 → {out_path}")
    print(f"工具 schema → {tools_path}")
    print("下一步：两档都 dump 完后，python build_dpo_dataset.py 构造偏好对。")


if __name__ == "__main__":
    main()
