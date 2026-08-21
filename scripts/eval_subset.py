"""快速 Train/Dev 子集评测；独立 Test 只由 eval_demo.py 消费。

  uv run --frozen python scripts/eval_subset.py
  uv run --frozen python scripts/eval_subset.py --split train --cats multi_step
"""
import argparse
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from edu_agent.data import db, generate  # noqa: E402
from edu_agent.eval import build_tasks, run_eval  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["train", "dev"], default="dev")
    ap.add_argument("--cats", default="", help="comma-separated categories; empty selects all")
    args = ap.parse_args()
    cats = {item for item in args.cats.split(",") if item}

    db_path = os.path.join(tempfile.gettempdir(), "edu_agent_eval.db")
    generate.build(seed=42, out_path=db_path)
    os.environ["EDU_AGENT_DB"] = db_path
    conn = db.connect(db_path)
    os.environ.setdefault("EDU_AGENT_ENGINE", "openai")
    from edu_agent.engine import get_engine
    eng = get_engine()

    tasks = [
        task for task in build_tasks(conn)
        if task.split == args.split and (not cats or task.category in cats)
    ]
    if not tasks:
        raise SystemExit(f"no tasks selected for split={args.split} cats={sorted(cats)}")
    print(f"model={eng.model} split={args.split} cats={sorted(cats) or ['all']} n={len(tasks)}")
    report = run_eval(tasks, lambda _t: eng, db_conn=conn)
    conn.close()

    for r in report["records"]:
        mark = "PASS" if r["success"] else "FAIL"
        tools = ",".join(r["tools_called"])
        print(f"  [{mark}] {r['id']:30s} tools=[{tools}]")
    ms = report["by_category"].get("multi_step", {})
    if ms:
        n = ms["n"]
        sr = ms["trajectory_success_rate"]
        print(f"\nmulti_step 轨迹成功: {sr*100:.1f}% ({round(sr*n)}/{n})")
    print(f"总轨迹成功: {report['trajectory_success_rate']*100:.1f}%")


if __name__ == "__main__":
    main()
