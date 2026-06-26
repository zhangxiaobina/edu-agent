"""多步早停修复的 before/after 消融：同一真实端点上跑两档配置，隔离「修复」这一个变量。

  before = 旧提示 + 无编排兜底（max_nudges=0）   —— 复现塌陷
  after  = 强化提示 + 编排兜底一次（max_nudges=1）—— 本次修复

两档用同一套 19 任务、同一 seed-42 库、同一 W4A16 端点顺序跑，温度 0 确定性，
因此分差可直接归因到「提示强化 + 编排兜底」，而非端点/版本漂移。

  export EDU_AGENT_ENGINE=openai EDU_AGENT_BASE_URL=... EDU_AGENT_API_KEY=... EDU_AGENT_MODEL=...
  uv run python scripts/eval_ablation.py
"""
import argparse
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from edu_agent.data import db, generate  # noqa: E402
from edu_agent.eval import build_tasks, format_report, run_eval  # noqa: E402

# 修复前的原始系统提示（无「多步执行纪律」段），用于 before 档严格对照。
OLD_PROMPT = """你是「全域教学教务智能助手」，服务于一个在线教学平台（教师/教务视角）。
你可以调用一组工具来查询与操作教学数据（成绩、考试、班级、题库、学习进度、知识图谱、
错题分析、组卷判分、布置作业、AI 出题、代码沙箱等）。

工作原则：
1. 用工具获取真实数据再回答，不要凭空编造分数、人数或知识点。
2. 多步任务按需依次调用工具：先用查询/分析类工具拿事实，再据结果决定下一步。
3. 只在确有必要时调用工具；纯常识/寒暄类问题直接回答，不要为了用工具而用工具。
4. 班级名/学生名等需要先解析成对应 ID（可用列表/名单类工具）。
5. 最终用简洁中文回答，给出关键数字与可执行建议。"""


def _multi(report):
    return report["by_category"].get("multi_step", {})


def main():
    ap = argparse.ArgumentParser(description="多步早停修复 before/after 消融")
    ap.add_argument("--only", choices=["before", "after"], default=None,
                    help="只跑其中一档（默认两档都跑）")
    args = ap.parse_args()

    db_path = os.path.join(tempfile.gettempdir(), "edu_agent_eval.db")
    generate.build(seed=42, out_path=db_path)
    os.environ["EDU_AGENT_DB"] = db_path
    conn = db.connect(db_path)

    os.environ.setdefault("EDU_AGENT_ENGINE", "openai")
    from edu_agent.engine import get_engine
    shared = get_engine()

    def make_engine(_task):
        # 每个任务取新引擎实例：openai 引擎本身无状态，但保持与 oracle 路径一致语义。
        return shared

    configs = {
        "before": dict(max_nudges=0, system_prompt=OLD_PROMPT,
                       title="BEFORE（旧提示 + 无编排兜底）"),
        "after": dict(max_nudges=1, system_prompt=None,
                      title="AFTER（强化提示 + 编排兜底×1）"),
    }
    if args.only:
        configs = {args.only: configs[args.only]}

    summary = {}
    try:
        tasks = build_tasks(conn)
        print(f"端点 model={shared.model} · 任务数 {len(tasks)} · 同端点顺序跑 {len(configs)} 档\n")
        for key, cfg in configs.items():
            print("#" * 64)
            print(f"# {cfg['title']}")
            print("#" * 64)
            report = run_eval(tasks, make_engine, db_conn=conn,
                              max_nudges=cfg["max_nudges"], system_prompt=cfg["system_prompt"])
            print(format_report(report))
            print()
            summary[key] = report
    finally:
        conn.close()

    if len(summary) == 2:
        b, a = summary["before"], summary["after"]
        print("=" * 64)
        print("对照（before → after，Δ）")
        print("-" * 64)
        rows = [
            ("轨迹成功率", "trajectory_success_rate"),
            ("multi_step 轨迹", None),
            ("工具选择 F1", "tool_selection_f1"),
            ("工具精确率", "tool_precision"),
            ("参数准确率", "param_accuracy"),
            ("relevance 判对率", "relevance_accuracy"),
        ]
        for label, key in rows:
            if key is None:
                bm, am = _multi(b).get("trajectory_success_rate"), _multi(a).get("trajectory_success_rate")
                bn, an = _multi(b).get("n", 0), _multi(a).get("n", 0)
                print(f"  {label:16s}: {bm*100:5.1f}% ({round(bm*bn)}/{bn}) → "
                      f"{am*100:5.1f}% ({round(am*an)}/{an})   Δ {(am-bm)*100:+.1f}pct")
            else:
                bv, av = b[key], a[key]
                print(f"  {label:16s}: {bv*100:5.1f}% → {av*100:5.1f}%   Δ {(av-bv)*100:+.1f}pct")
        print("=" * 64)


if __name__ == "__main__":
    main()
