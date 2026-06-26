"""评测运行器：把任务集逐个跑过 Agent（任意引擎），收集轨迹并汇总指标。

引擎无关——同一套 harness：
- 现在：make_engine = oracle.make_oracle_engine（离线确定性回放，验证框架）。
- 以后：make_engine = lambda t: engine.get_engine()（接 DashScope / vLLM / 算法仓 W4A16，出真数）。
"""
from __future__ import annotations

from typing import Callable

from ..agent import run_agent
from ..engine.base import Engine
from . import metrics
from .tasks import EvalTask


def run_eval(tasks: list[EvalTask], make_engine: Callable[[EvalTask], Engine],
             db_conn=None, recursion_limit: int = 30,
             max_nudges: int = 1, system_prompt: str | None = None) -> dict:
    """对每个任务跑一次 Agent，返回 {汇总指标..., "records": [逐任务...]}。

    max_nudges    ：编排兜底强度（0 = 旧行为，无早停补救；1 = 默认补救一次）。
    system_prompt ：可覆盖系统提示词（None 用默认强化版）；用于做「旧提示 vs 新提示」对照。
    """
    from ..agent.prompts import SYSTEM_PROMPT
    sys_prompt = system_prompt or SYSTEM_PROMPT
    records = []
    for task in tasks:
        engine = make_engine(task)
        try:
            result = run_agent(task.query, engine, system_prompt=sys_prompt,
                               db_conn=db_conn, recursion_limit=recursion_limit,
                               max_nudges=max_nudges)
        except Exception as e:  # noqa: BLE001  单个任务失败不应中断整批
            result = {"final_answer": None, "trace": [], "error": f"{type(e).__name__}: {e}"}
        records.append(metrics.score_task(task, result))
    report = metrics.aggregate(records)
    report["records"] = records
    return report


def _pct(x) -> str:
    return "—" if x is None else f"{x * 100:5.1f}%"


def format_report(report: dict) -> str:
    lines = [
        "=" * 64,
        f"评测任务数: {report['n_tasks']}",
        "-" * 64,
        f"轨迹成功率 trajectory success   : {_pct(report['trajectory_success_rate'])}",
        f"工具选择 F1 (召回/精确)         : {_pct(report['tool_selection_f1'])}"
        f"  ({_pct(report['tool_recall'])}/{_pct(report['tool_precision'])})",
        f"参数准确率 param accuracy       : {_pct(report['param_accuracy'])}",
        f"relevance 判对率               : {_pct(report['relevance_accuracy'])}",
        "-" * 64,
        "分类别（轨迹成功率 | 工具F1 | 参数）:",
    ]
    for cat, d in report["by_category"].items():
        lines.append(f"  {cat:12s} n={d['n']:2d}   "
                     f"{_pct(d['trajectory_success_rate'])} | "
                     f"{_pct(d['tool_f1'])} | {_pct(d['param_accuracy'])}")
    fails = [r["id"] for r in report["records"] if not r["success"]]
    if fails:
        lines += ["-" * 64, "未达成轨迹的任务: " + ", ".join(fails)]
    errs = [(r["id"], r["error"]) for r in report["records"] if r.get("error")]
    if errs:
        lines += ["运行报错: " + "; ".join(f"{i}:{e}" for i, e in errs)]
    lines.append("=" * 64)
    return "\n".join(lines)
