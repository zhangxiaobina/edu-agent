"""离线 oracle：确定性回放每个任务的「期望轨迹」，仅用于验证评测框架本身。

注意：oracle 不做语言理解——它按任务声明的 expected_tools 顺序发起工具调用，再给最终回答。
因此用 oracle 跑出的指标必然接近满分，**只证明 harness（任务加载 / 工具执行回灌 / 指标计算）
正确且能区分对错**，不代表任何模型能力。真实模型能力须接真引擎（DashScope / vLLM /
算法仓 W4A16）后用同一套 harness 跑出。

flagship 任务复用 agent.demo_policy 的动态决策（依据前序工具真实返回选下一步），
以此证明 harness 也能正确评判「动态多步」轨迹，而不只是静态回放。
"""
from __future__ import annotations

from ..agent.demo_policy import demo_policy
from ..engine.base import EngineResponse, ToolCall
from ..engine.mock import MockEngine, call, final
from .tasks import EvalTask


def _oracle_answer(task: EvalTask) -> str:
    kws = "；".join(task.success.answer_contains)
    tail = f" {kws}" if kws else ""
    return f"（离线 oracle）任务 {task.id} 已按期望轨迹完成。{tail}".strip()


def _parallel_response(step: int, calls: list[tuple[str, dict]]) -> EngineResponse:
    return EngineResponse(tool_calls=[
        ToolCall(id=f"call_{step}_{i}", name=name, arguments=args)
        for i, (name, args) in enumerate(calls)
    ])


def oracle_policy_for(task: EvalTask):
    """构造一个回放该任务期望轨迹的确定性 policy。"""
    calls = [ec.oracle_call() for ec in task.expected_tools]
    answer = _oracle_answer(task)

    if task.parallel:
        def policy(messages, tools, step):
            if step == 0 and calls:
                return _parallel_response(step, calls)
            return final(answer)
        return policy

    def policy(messages, tools, step):
        if step < len(calls):
            name, args = calls[step]
            return call(step, name, **args)
        return final(answer)
    return policy


def make_oracle_engine(task: EvalTask) -> MockEngine:
    """评测运行器的 make_engine：为每个任务返回新 MockEngine（mock 引擎按步有状态）。"""
    if task.oracle == "demo":
        return MockEngine(demo_policy)
    return MockEngine(oracle_policy_for(task))
