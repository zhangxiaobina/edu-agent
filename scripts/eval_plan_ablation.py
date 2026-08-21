"""PlanGraph 严格 before/after：同模型、温度、提示词、任务顺序，只切换计划层。"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from edu_agent.data import db, generate
from edu_agent.eval import build_tasks, format_report, run_eval
from edu_agent.eval.oracle import make_oracle_engine
from edu_agent.planning.models import PlanSpec, validate_plan_graph
from edu_agent.planning.planner import ModelPlanGenerator
from edu_agent.planning.runtime import PlanningOptions
from edu_agent.state import StateStore


class OracleTaskPlanGenerator:
    def __init__(self, task):
        self.task = task

    def generate(self, task, *, context, available_tools, max_steps):
        context.budget.consume_model_call()
        steps = []
        previous = None
        for index, expected in enumerate(self.task.expected_tools):
            tool, _ = expected.oracle_call()
            step_id = f"step-{index + 1}"
            steps.append(
                {
                    "id": step_id,
                    "goal": f"调用 {tool} 获取第 {index + 1} 步真实数据",
                    "depends_on": [previous] if previous else [],
                    "allowed_tools": [tool],
                    "expected_tools": [tool],
                    "completion_conditions": [{"kind": "tool_success", "tool": tool}],
                }
            )
            previous = step_id
        spec = PlanSpec.model_validate({"goal": task, "steps": steps})
        return validate_plan_graph(
            spec,
            available_tools=available_tools,
            max_steps=max_steps,
        )


def _print_focus(report: dict, label: str) -> None:
    multi = report["by_category"]["multi_step"]
    irrelevant = report["by_category"]["irrelevance"]
    print(f"\n{label} 重点指标")
    print(
        json.dumps(
            {
                "multi_step": multi,
                "irrelevance": irrelevant,
                "overall": {
                    "tool_precision": report["tool_precision"],
                    "tool_recall": report["tool_recall"],
                    "tool_f1": report["tool_selection_f1"],
                    "avg_model_calls": report["avg_model_calls"],
                    "avg_tool_calls": report["avg_tool_calls"],
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=["oracle", "openai"], default="oracle")
    args = parser.parse_args()

    state_paths = {
        "before": Path(tempfile.gettempdir()) / "edu_agent_plan_eval_before.db",
        "after": Path(tempfile.gettempdir()) / "edu_agent_plan_eval_after.db",
    }
    for path in state_paths.values():
        path.unlink(missing_ok=True)

    if args.engine == "oracle":
        make_engine = make_oracle_engine
        model_label = "offline-oracle（只验证 harness，不代表模型能力）"
    else:
        os.environ.setdefault("EDU_AGENT_ENGINE", "openai")
        from edu_agent.engine import get_engine

        shared_engine = get_engine()

        def make_engine(task):
            return shared_engine

        model_label = (
            f"{shared_engine.model} temperature={shared_engine.temperature} "
            f"endpoint={shared_engine.base_url}"
        )

    reports = {}
    print(f"模型：{model_label}")
    for label in ("before", "after"):
        database_path = Path(tempfile.gettempdir()) / f"edu_agent_plan_eval_{label}_data.db"
        generate.build(seed=42, out_path=database_path)
        connection = db.connect(database_path)
        try:
            tasks = build_tasks(connection)
            if label == "before":
                print(f"任务顺序：{', '.join(task.id for task in tasks)}")
            state = StateStore(state_paths[label])

            def options(task, engine, *, current_label=label, current_state=state):
                if current_label == "before":
                    return {
                        "planning": PlanningOptions(enabled=False),
                        "state_store": current_state,
                    }
                generator = (
                    OracleTaskPlanGenerator(task)
                    if args.engine == "oracle"
                    else ModelPlanGenerator(engine)
                )
                return {
                    "planning": PlanningOptions(),
                    "plan_generator": generator,
                    "state_store": current_state,
                    "force_plan": task.category == "multi_step",
                }

            report = run_eval(
                tasks,
                make_engine,
                db_conn=connection,
                run_options=options,
            )
            reports[label] = report
            print(f"\n{'=' * 72}\n{label.upper()}\n{'=' * 72}")
            print(format_report(report))
            _print_focus(report, label)
        finally:
            connection.close()

    before = reports["before"]["by_category"]["multi_step"]
    after = reports["after"]["by_category"]["multi_step"]
    print("\n严格对照（multi_step，before → after）")
    for key in (
        "trajectory_success_rate",
        "step_completion_rate",
        "tool_f1",
        "avg_model_calls",
        "avg_tool_calls",
        "early_termination_rate",
    ):
        print(f"  {key}: {before[key]} → {after[key]}")
    if args.engine == "oracle":
        print("\n结论：以上是离线 oracle 对照，只证明计划路径和指标可运行；真模型数据未运行。")


if __name__ == "__main__":
    main()
