"""agentic 评测：多工具多步任务集 + 指标（口径对齐 BFCL V4）+ 引擎无关运行器。

  from edu_agent.eval import build_tasks, run_eval, format_report, make_oracle_engine

离线（无 key）用 make_oracle_engine 验证框架；接真引擎后用同一 run_eval 出真数。
"""
from .harness import format_report, run_eval
from .oracle import make_oracle_engine, oracle_policy_for
from .tasks import CATEGORIES, EvalTask, ExpectedCall, SuccessSpec, build_tasks
from .tasks_derived import build_derived_tasks

__all__ = ["build_tasks", "build_derived_tasks", "run_eval", "format_report",
           "make_oracle_engine", "oracle_policy_for", "EvalTask", "ExpectedCall",
           "SuccessSpec", "CATEGORIES"]
