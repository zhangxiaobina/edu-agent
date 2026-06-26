"""LangGraph 多工具 Agent：ReAct 循环（agent 决策 → tools 执行 → 回灌 → 直到给出回答）。

消息全程用 OpenAI chat-completions 字典格式，故同一张图既能跑离线 mock 引擎，
也能跑真实 OpenAI 兼容端点（通义 / vLLM / 算法仓 W4A16 Qwen3-14B），无需改动。
"""
from __future__ import annotations

import json
import operator
import os
from typing import Annotated, TypedDict

from langgraph.graph import END, StateGraph

from ..engine.base import Engine
from ..tools import registry
from .prompts import SYSTEM_PROMPT


# 编排兜底（B）：模型早停时注入的「继续/自检」提示。措辞**强约束反幻觉**——
# 实测根因不是单纯早停，而是「拿到第一跳数据后，第二跳直接编造结果（分布/路径/题目）
# 而不调用对应工具」。故提示必须逼模型核对每条数据的来源（必须是工具刚返回的），
# 凡需数据的子任务一律改为重新发起工具调用，不许脑补。可用 EDU_AGENT_NUDGE 覆盖以做实验。
_DEFAULT_NUDGE = (
    "停。先别急着回答，按下面三步自检：\n"
    "1）把用户最初请求拆成逐个子任务，逐条列出；\n"
    "2）对每个子任务，检查你打算给出的数据（人数、分数、分布、平均分、通过率、"
    "学习路径、前置/后继知识点、题目列表等）是否来自**上面某次工具调用刚刚返回的原文**——"
    "**凡是你自己算的、估的、或凭印象写的，一律不算数**；\n"
    "3）只要有任何一个子任务，你还没有调用过能直接返回该数据的工具，"
    "就**立即发起那次工具调用**（这一轮只发工具调用、先不要写总结）。"
    "只有当每个子任务都已有对应工具返回时，才给出最终中文回答。"
)
REFLECT_NUDGE = os.environ.get("EDU_AGENT_NUDGE") or _DEFAULT_NUDGE


class AgentState(TypedDict):
    messages: Annotated[list, operator.add]
    nudges: int  # 已注入的「继续」提示次数（编排兜底，防多步早停）


def _called_any_tool(messages: list) -> bool:
    """该轨迹里是否已经真正调用过工具（用于区分『多步早停』与『本就不该用工具』）。"""
    return any(m.get("role") == "assistant" and m.get("tool_calls") for m in messages)


def _count_tool_calls(messages: list) -> int:
    """该轨迹累计发起的工具调用次数（含并行的一轮多次）。"""
    return sum(len(m.get("tool_calls") or [])
               for m in messages if m.get("role") == "assistant")


def build_agent(engine: Engine, db_conn=None, max_nudges: int = 1, max_tool_calls: int = 8,
                tools_provider=None):
    """编译并返回一个多工具 Agent 图。

    db_conn      ：可注入共享连接（否则每次工具调用自开关）。
    max_nudges   ：编排兜底——模型在「已调过工具」后仍早停（输出纯文本不带 tool_calls）时，
                   最多注入几次「继续/自检」提示再给它一次决策机会。0 = 关闭兜底（旧行为）。
                   以「已调过工具」为门槛，使寒暄/纯概念/越域（irrelevance）任务永不触发，
                   只对真正的多步链路补救早停。
    max_tool_calls：反螺旋硬上限——累计工具调用达到该值后不再注入兜底提示，避免兜底把模型
                   推入「反复调同类工具」的死循环（实测 relevance 任务上出现过 8 连查）。
                   取值需高于最长合法链路（旗舰任务 5 步）+ 少量兜底重试。
    tools_provider：工具来源，需提供 openai_tools() 与 dispatch(name, args, conn)。默认本地
                   registry（直调，零额外进程）；传 MCPToolProvider 则工具改经 MCP 协议往返，
                   图本身无需改动（两者同契约）。
    """
    provider = tools_provider if tools_provider is not None else registry
    tools = provider.openai_tools()

    def agent_node(state: AgentState):
        resp = engine.chat(state["messages"], tools)
        return {"messages": [resp.to_assistant_message()]}

    def tools_node(state: AgentState):
        last = state["messages"][-1]
        out = []
        for tc in last.get("tool_calls", []):
            fn = tc["function"]
            name = fn["name"]
            try:
                args = json.loads(fn["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            result = provider.dispatch(name, args, conn=db_conn)
            out.append({"role": "tool", "tool_call_id": tc["id"], "name": name,
                        "content": json.dumps(result, ensure_ascii=False, default=str)})
        return {"messages": out}

    def reflect_node(state: AgentState):
        """注入一次「继续/自检」提示，并记一次 nudge 计数。"""
        return {"messages": [{"role": "user", "content": REFLECT_NUDGE}],
                "nudges": state.get("nudges", 0) + 1}

    def should_continue(state: AgentState):
        if state["messages"][-1].get("tool_calls"):
            return "tools"
        # 模型给了纯文本（候选最终答案）。仅在「已调过工具、未用尽兜底次数、且累计调用未触顶」
        # 时注入一次提示再给一次机会；否则结束。三重门槛：保护 irrelevance（从未调工具）、
        # 限制兜底强度、并硬性防螺旋。
        if (state.get("nudges", 0) < max_nudges
                and _called_any_tool(state["messages"])
                and _count_tool_calls(state["messages"]) < max_tool_calls):
            return "reflect"
        return "end"

    g = StateGraph(AgentState)
    g.add_node("agent", agent_node)
    g.add_node("tools", tools_node)
    g.add_node("reflect", reflect_node)
    g.set_entry_point("agent")
    g.add_conditional_edges("agent", should_continue,
                            {"tools": "tools", "reflect": "reflect", "end": END})
    g.add_edge("tools", "agent")
    g.add_edge("reflect", "agent")
    return g.compile()


def run_agent(task: str, engine: Engine, system_prompt: str = SYSTEM_PROMPT,
              db_conn=None, recursion_limit: int = 30, max_nudges: int = 1,
              tools_provider=None) -> dict:
    """跑一个任务，返回 {final_answer, trace, messages}。trace 记录工具调用序列。

    max_nudges    ：编排兜底强度（见 build_agent）；设 0 可复现「未加兜底」的旧行为做对照。
    tools_provider：工具来源（见 build_agent）；默认本地 registry，传 MCPToolProvider 则经 MCP 协议调用。
    """
    app = build_agent(engine, db_conn=db_conn, max_nudges=max_nudges, tools_provider=tools_provider)
    init = {"messages": [{"role": "system", "content": system_prompt},
                         {"role": "user", "content": task}],
            "nudges": 0}
    state = app.invoke(init, {"recursion_limit": recursion_limit})
    msgs = state["messages"]
    trace = []
    for m in msgs:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                trace.append({"tool": tc["function"]["name"], "arguments": tc["function"]["arguments"]})
    final = next((m["content"] for m in reversed(msgs)
                  if m.get("role") == "assistant" and not m.get("tool_calls")), None)
    return {"final_answer": final, "trace": trace, "messages": msgs}
