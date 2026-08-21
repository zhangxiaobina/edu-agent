"""快速子集评测：默认只跑 multi_step（迭代修复早停/幻觉时用），可选带上 single/relevance 看附带损伤。

  uv run --frozen python scripts/eval_subset.py
  uv run --frozen python scripts/eval_subset.py --cats multi_step,single,relevance
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
    ap.add_argument("--cats", default="multi_step")
    args = ap.parse_args()
    cats = set(args.cats.split(","))

    db_path = os.path.join(tempfile.gettempdir(), "edu_agent_eval.db")
    generate.build(seed=42, out_path=db_path)
    os.environ["EDU_AGENT_DB"] = db_path
    conn = db.connect(db_path)
    os.environ.setdefault("EDU_AGENT_ENGINE", "openai")
    from edu_agent.engine import get_engine
    eng = get_engine()

    tasks = [t for t in build_tasks(conn) if t.category in cats]
    print(f"model={eng.model} cats={sorted(cats)} n={len(tasks)}")
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
