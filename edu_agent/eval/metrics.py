"""评测指标：工具选择 / 参数 / 轨迹成功 / relevance —— 口径对齐 BFCL V4。

对照关系（详见 docs/eval.md）：
- **工具选择 & 参数匹配 ≈ BFCL AST**：只校验「期望调用」里声明的参数，用 possible-answer
  （可接受值集合，或 ANY=仅需存在）匹配，忽略模型多给的、未声明的可选参数。
- **轨迹成功率 ≈ BFCL multi-turn**：以整段多步序列是否达成目标计 1/0——必需工具按依赖
  顺序出现（子序列），且最终回答含关键事实；relevance 类要求「不该调时一个工具都没调」。
- **relevance/irrelevance ≈ BFCL relevance/irrelevance**：该调工具时调了、不该调时没调即对。

本模块仅用标准库，可零依赖单测。输入的 result 结构来自 agent.run_agent：
  {"final_answer": str|None, "trace": [{"tool": str, "arguments": json_str}, ...]}
"""
from __future__ import annotations

import json

from .tasks import ANY, EvalTask

_MISSING = object()


# --------------------------------------------------------------------------- #
# 基础匹配
# --------------------------------------------------------------------------- #
def _loads(arguments) -> dict:
    """trace 里的 arguments 是 JSON 字符串（也容忍已是 dict）。"""
    if isinstance(arguments, dict):
        return arguments
    try:
        return json.loads(arguments or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def _match_param(spec, value) -> bool:
    """单参数匹配：spec 为 ANY（仅需存在）/ 列表（possible answers）/ 标量（等值）。"""
    if value is _MISSING:
        return False
    if spec is ANY:
        return True
    if isinstance(spec, (list, tuple, set)):
        return value in spec
    return value == spec


def _match_group(group, name: str) -> bool:
    """group 为工具名 str 或 list[str]（任一可接受）。"""
    return name in (group if isinstance(group, list) else [group])


def _match_call_params(expected_args: dict, actual_args: dict) -> tuple[int, int]:
    """返回 (matched, total)，仅统计 expected_args 中声明的参数。"""
    total = len(expected_args)
    matched = sum(1 for k, spec in expected_args.items()
                  if _match_param(spec, actual_args.get(k, _MISSING)))
    return matched, total


# --------------------------------------------------------------------------- #
# 工具选择 + 参数（贪心对齐期望调用 → 实际调用）
# --------------------------------------------------------------------------- #
def score_calls(expected_calls, trace) -> dict:
    """把期望调用按工具名贪心对齐到实际调用，给出召回/精确/参数准确率。"""
    actual = [(c["tool"], _loads(c["arguments"])) for c in trace]
    used = [False] * len(actual)
    tool_hits = 0
    p_matched = p_total = 0
    for exp in expected_calls:
        names = exp.tool if isinstance(exp.tool, list) else [exp.tool]
        idx = next((i for i, (n, _) in enumerate(actual) if not used[i] and n in names), None)
        if idx is None:                       # 期望工具没被调用 → 其声明参数全记未匹配
            p_total += len(exp.args)
            continue
        used[idx] = True
        tool_hits += 1
        m, t = _match_call_params(exp.args, actual[idx][1])
        p_matched += m
        p_total += t
    n_exp, n_act = len(expected_calls), len(actual)
    return {
        "tool_recall": tool_hits / n_exp if n_exp else 1.0,
        "tool_precision": tool_hits / n_act if n_act else (1.0 if n_exp == 0 else 0.0),
        "param_accuracy": p_matched / p_total if p_total else 1.0,
        "n_expected": n_exp,
        "n_actual": n_act,
        "extraneous_calls": sum(1 for u in used if not u),
    }


def _f1(recall: float, precision: float) -> float:
    return 2 * recall * precision / (recall + precision) if (recall + precision) else 0.0


# --------------------------------------------------------------------------- #
# 轨迹成功（multi-turn 式整段判定） + relevance
# --------------------------------------------------------------------------- #
def _required_satisfied(required, tools, ordered: bool) -> bool:
    """required 每项是 str 或 list[str]（任一）；以多重集消费 tools 判断是否都出现。"""
    if ordered:                               # 作为子序列按顺序出现
        i = 0
        for t in tools:
            if i < len(required) and _match_group(required[i], t):
                i += 1
        return i == len(required)
    pool = list(tools)                        # 无序：每项消费一个不同的实际调用
    for g in required:
        idx = next((i for i, t in enumerate(pool) if _match_group(g, t)), None)
        if idx is None:
            return False
        pool.pop(idx)
    return True


def trajectory_success(task: EvalTask, result: dict) -> bool:
    tools = [t["tool"] for t in result.get("trace", [])]
    answer = result.get("final_answer") or ""
    spec = task.success
    if spec.forbid_tools:
        if tools:
            return False
    elif not _required_satisfied(spec.required_tools, tools, spec.ordered):
        return False
    if not all(kw in answer for kw in spec.answer_contains):
        return False
    return bool(answer)


def relevance_correct(task: EvalTask, result: dict) -> bool:
    """该调工具时调了、不该调时没调即对。"""
    called = len(result.get("trace", [])) > 0
    return called == task.should_call_tool


# --------------------------------------------------------------------------- #
# 单任务打分 + 汇总
# --------------------------------------------------------------------------- #
def score_task(task: EvalTask, result: dict) -> dict:
    rec = {
        "id": task.id,
        "category": task.category,
        "success": trajectory_success(task, result),
        "tools_called": [t["tool"] for t in result.get("trace", [])],
        "error": result.get("error"),
    }
    if task.expected_tools:                   # single/multi_step/parallel/relevance
        rec.update(score_calls(task.expected_tools, result.get("trace", [])))
        rec["tool_f1"] = _f1(rec["tool_recall"], rec["tool_precision"])
    if task.category in ("relevance", "irrelevance"):
        rec["relevance_correct"] = relevance_correct(task, result)
    return rec


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def aggregate(records: list[dict]) -> dict:
    n = len(records)
    tool_recs = [r for r in records if "tool_f1" in r]
    rel_recs = [r for r in records if "relevance_correct" in r]
    by_category = {}
    for cat in sorted({r["category"] for r in records}):
        crs = [r for r in records if r["category"] == cat]
        by_category[cat] = {
            "n": len(crs),
            "trajectory_success_rate": _mean([r["success"] for r in crs]),
            "tool_f1": _mean([r["tool_f1"] for r in crs if "tool_f1" in r]),
            "param_accuracy": _mean([r["param_accuracy"] for r in crs if "param_accuracy" in r]),
        }
    return {
        "n_tasks": n,
        "trajectory_success_rate": _mean([r["success"] for r in records]),
        "tool_selection_f1": _mean([r["tool_f1"] for r in tool_recs]),
        "tool_recall": _mean([r["tool_recall"] for r in tool_recs]),
        "tool_precision": _mean([r["tool_precision"] for r in tool_recs]),
        "param_accuracy": _mean([r["param_accuracy"] for r in tool_recs]),
        "relevance_accuracy": _mean([r["relevance_correct"] for r in rel_recs]),
        "by_category": by_category,
    }
