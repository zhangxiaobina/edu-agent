"""离线展示 PlanGraph 创建、工具证据绑定、早停拦截和最终完成。"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from edu_agent.agent import run_agent
from edu_agent.data import db, generate
from edu_agent.engine.mock import MockEngine, call, final
from edu_agent.planning.planner import ModelPlanGenerator
from edu_agent.planning.runtime import PlanningOptions
from edu_agent.runtime.models import RunContext
from edu_agent.runtime.tool_executor import ExecutionPolicy, PolicyToolExecutor
from edu_agent.state import StateStore
from edu_agent.tools import registry


TASK = "先查询三班 Python 考试，再基于真实结果给出答复"


def main() -> None:
    state_path = Path(tempfile.gettempdir()) / "edu_agent_plan_demo_state.db"
    data_path = Path(tempfile.gettempdir()) / "edu_agent_plan_demo_data.db"
    state_path.unlink(missing_ok=True)
    generate.build(seed=42, out_path=data_path)
    connection = db.connect(data_path)
    store = StateStore(state_path)
    context = RunContext.create(
        session_id="plan-demo-session",
        actor_id="teacher-demo",
        tenant_id="school-demo",
        role="teacher",
        course_ids={1},
        max_model_calls=8,
        max_tool_calls=4,
    )
    store.ensure_session(
        context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        role=context.role,
        course_ids=context.course_ids,
    )
    plan = {
        "goal": TASK,
        "steps": [
            {
                "id": "find-exam",
                "goal": "查询三班 Python 考试并取得真实列表",
                "depends_on": [],
                "allowed_tools": ["list_exams"],
                "expected_tools": ["list_exams"],
                "completion_conditions": [
                    {"kind": "tool_success", "tool": "list_exams"}
                ],
            }
        ],
    }
    planner = ModelPlanGenerator(
        MockEngine(lambda messages, tools, step: final(json.dumps(plan, ensure_ascii=False)))
    )

    def execution_policy(messages, tools, step):
        if step == 0:
            return final("我已经查完并完成了。")
        if step == 1:
            return call(step, "list_exams", class_id=3, course_id=1)
        return final("已依据 list_exams 的真实返回完成答复。")

    executor = PolicyToolExecutor(
        registry,
        policy=ExecutionPolicy(require_write_approval=False),
        state_store=store,
    )
    try:
        result = run_agent(
            TASK,
            MockEngine(execution_policy),
            db_conn=connection,
            run_context=context,
            tool_executor=executor,
            planning=PlanningOptions(max_steps=4, max_step_retries=2, max_iterations=6),
            plan_generator=planner,
            state_store=store,
            force_plan=True,
        )
    finally:
        connection.close()

    plan_result = result["plan"]
    rejected = [item for item in plan_result["evidence"] if item["status"] == "rejected"]
    accepted = [item for item in plan_result["evidence"] if item["status"] == "accepted"]
    user_turns = [message for message in result["messages"] if message["role"] == "user"]
    print(f"plan: {plan_result['id']} status={plan_result['status']}")
    print(
        "steps:",
        [(step["id"], step["status"], step["retry_count"]) for step in plan_result["steps"]],
    )
    print(f"premature answer blocked: {any(e['kind'] == 'missing' for e in rejected)}")
    print(
        "accepted evidence:",
        [(item["kind"], item["tool_name"], item["tool_event_id"]) for item in accepted],
    )
    print(f"real user turns only: {len(user_turns)} -> {user_turns[0]['content']}")
    print(f"budget: {result['budget']}")
    print(f"final: {result['final_answer']}")
    assert plan_result["status"] == "completed"
    assert any(item["kind"] == "missing" for item in rejected)
    assert any(item["kind"] == "tool_event" for item in accepted)
    assert len(user_turns) == 1


if __name__ == "__main__":
    main()
