"""调试：把指定任务跑过修复后的 Agent，打印完整消息序列，看模型到底在哪一步停/跑偏。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from edu_agent.agent import run_agent  # noqa: E402
from edu_agent.data import db, generate  # noqa: E402
from edu_agent.engine import get_engine  # noqa: E402
from edu_agent.eval import build_tasks  # noqa: E402


def dump(task_id: str, max_nudges: int):
    db_path = os.path.join(tempfile.gettempdir(), "edu_agent_eval.db")
    generate.build(seed=42, out_path=db_path)
    os.environ["EDU_AGENT_DB"] = db_path
    conn = db.connect(db_path)
    task = next(t for t in build_tasks(conn) if t.id == task_id)
    os.environ.setdefault("EDU_AGENT_ENGINE", "openai")
    eng = get_engine()
    print(f"\n===== {task_id} (max_nudges={max_nudges}) =====")
    print(f"query: {task.query}")
    print(f"expected tools: {[ (c.tool if isinstance(c.tool,str) else c.tool) for c in task.expected_tools]}")
    res = run_agent(task.query, eng, db_conn=conn, max_nudges=max_nudges)
    for i, m in enumerate(res["messages"]):
        role = m.get("role")
        if role == "system":
            continue
        if role == "assistant":
            tcs = m.get("tool_calls") or []
            content = (m.get("content") or "").strip().replace("\n", " ")
            if tcs:
                calls = "; ".join(f"{t['function']['name']}({t['function']['arguments']})" for t in tcs)
                print(f"  [{i}] assistant TOOLCALLS: {calls}")
                if content:
                    print(f"        (text: {content[:120]})")
            else:
                print(f"  [{i}] assistant TEXT: {content[:240]}")
        elif role == "tool":
            c = (m.get("content") or "")[:130].replace("\n", " ")
            print(f"  [{i}] tool[{m.get('name')}] -> {c}")
        elif role == "user":
            print(f"  [{i}] user: {(m.get('content') or '')[:80]}")
    print(f"  >> trace tools: {[t['tool'] for t in res['trace']]}")
    print(f"  >> final: {(res['final_answer'] or '')[:200]}")
    conn.close()


if __name__ == "__main__":
    tid = sys.argv[1] if len(sys.argv) > 1 else "multi-fail-then-dist"
    nud = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    dump(tid, nud)
