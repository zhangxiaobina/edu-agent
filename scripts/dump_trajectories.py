"""把 EduAgent 评测任务逐个跑过 Agent，dump 完整轨迹(messages/trace/success)成 jsonl，
供 DPO 偏好对构造用。

放到 edu-agent 仓根的 scripts/ 下运行（与 eval_demo.py 同级，复用同一套建库/引擎/评测）：

  # ① base 档（未微调 fp16，作 chosen 来源）—— 端点指向 base
  export EDU_AGENT_ENGINE=openai
  export EDU_AGENT_BASE_URL=http://127.0.0.1:8000/v1
  export EDU_AGENT_API_KEY=dummy
  export EDU_AGENT_MODEL=Qwen/Qwen3-14B
  uv run python scripts/dump_trajectories.py --tag base

  # ② sft 档（微调 merged 或 W4A16，作 rejected 来源）—— 端点切到 SFT 模型后重跑
  export EDU_AGENT_MODEL=Qwen3-14B-FC-W4A16
  uv run python scripts/dump_trajectories.py --tag sft

关键：--nudges 0（默认）关闭编排兜底，dump 出的是「模型层原生轨迹」——
这正是 DPO 要纠正的对象。若用兜底后的轨迹做偏好对，等于把编排层提示的功劳烤进权重，失真。
"""
import argparse
import concurrent.futures
import json
import os
import shutil
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from edu_agent.agent import run_agent          # noqa: E402
from edu_agent.data import db, generate         # noqa: E402
from edu_agent.eval import build_tasks, metrics  # noqa: E402
from edu_agent.tools import registry            # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="dump EduAgent 轨迹用于 DPO 偏好对构造")
    ap.add_argument("--tag", required=True,
                    help="档位标识，决定输出文件名，如 base / sft / w4a16")
    ap.add_argument("--cats", default="multi_step",
                    help="逗号分隔的任务类别；默认只跑 multi_step（DPO 目标）。"
                         "可加 single,relevance 看附带损伤")
    ap.add_argument("--nudges", type=int, default=0,
                    help="编排兜底强度；DPO 取原生轨迹应设 0（默认）")
    ap.add_argument("--include-derived", action="store_true",
                    help="并入 tasks_derived 派生集（6 模板 × 8 锚点 = 48 条），"
                         "DPO 想要更多真实多步轨迹时开启")
    ap.add_argument("--ids", default=None,
                    help="逗号分隔的任务 id 子串过滤；任一子串命中即选。"
                         "如 c1-py,c2-ds,c4-net 选这 3 个 (班,课) 锚点下的全部派生模板"
                         "（小样本验证：3 锚点 × 6 模板 = 18 条）")
    ap.add_argument("--out-dir", default="dpo_dumps", help="输出目录")
    ap.add_argument("--n-classes", type=int, default=4,
                    help="合成库班级数（默认 4=冻结基准库；扩库 DPO 时放大，如 16 → 48 组合 × 6 = 288 派生）")
    ap.add_argument("--n-students", type=int, default=None,
                    help="学生数（默认按 35/班 估，至少 140）")
    ap.add_argument("--courses-per-class", type=int, default=2,
                    help="每班选课数（默认 2；扩库设 3=选满全部课程，最大化 班×课 锚点）")
    ap.add_argument("--workers", type=int, default=1,
                    help="并发任务数（默认 1=串行）。>1 时每个 worker 用独立 db 副本 + 独立 engine，"
                         "靠 vLLM 批处理大幅提速——解码受显存带宽限，批处理把『读一次权重』摊给整批，"
                         "GB10 上把 fp16 14B 的串行 ~7tok/s 拉高数倍。建议 8。")
    args = ap.parse_args()

    cats = [c.strip() for c in args.cats.split(",") if c.strip()]
    os.makedirs(args.out_dir, exist_ok=True)

    # 固定 seed-42 合成库，与 eval_demo 完全一致，保证任务锚点逐字节可复现。
    # 默认 n_classes=4 → 冻结基准库；扩库 DPO 时放大 n_classes/courses_per_class（独立临时库，不碰基准）。
    db_path = os.path.join(tempfile.gettempdir(), f"edu_agent_eval_c{args.n_classes}.db")
    generate.build(seed=42, out_path=db_path, n_classes=args.n_classes,
                   n_students=args.n_students, courses_per_class=args.courses_per_class)
    os.environ["EDU_AGENT_DB"] = db_path
    conn = db.connect(db_path)

    from edu_agent.engine import get_engine  # 延迟导入，避免无端点时报错
    engine = get_engine()
    print(f"档位 {args.tag} · 引擎 model={engine.model} · 类别 {cats} · nudges={args.nudges}"
          f" · 派生集={'on' if args.include_derived else 'off'} · workers={args.workers}\n")

    out_path = os.path.join(args.out_dir, f"traj_{args.tag}.jsonl")
    n_ok = 0
    try:
        tasks = [t for t in build_tasks(conn, include_derived=args.include_derived)
                 if t.category in cats]
        if args.ids:
            subs = [s.strip() for s in args.ids.split(",") if s.strip()]
            tasks = [t for t in tasks if any(s in t.id for s in subs)]
            print(f"按 --ids 过滤后保留 {len(tasks)} 条：{[t.id for t in tasks]}\n")

        # 并发上下文：每 worker 一份 db 副本 + 一个 engine（sqlite 连接非线程安全；
        # 独立副本还隔离 create_exam 写操作，避免并发 MAX(id)+1 撞 id）。串行(workers=1)
        # 仍复用主连接与 engine，行为与旧版一致。
        _local = threading.local()

        def run_one(task):
            if args.workers > 1:
                if not hasattr(_local, "ctx"):
                    wpath = os.path.join(tempfile.gettempdir(),
                                         f"edu_dump_w{threading.get_ident()}.db")
                    shutil.copyfile(db_path, wpath)
                    _local.ctx = (db.connect(wpath), get_engine())
                wconn, weng = _local.ctx
            else:
                wconn, weng = conn, engine
            try:
                result = run_agent(task.query, weng, db_conn=wconn, max_nudges=args.nudges)
            except Exception as e:  # noqa: BLE001  单任务失败不中断整批
                result = {"final_answer": None, "trace": [], "messages": [],
                          "error": f"{type(e).__name__}: {e}"}
            return task, result

        def emit(f, task, result, idx):
            nonlocal n_ok
            rec = metrics.score_task(task, result)
            success = bool(rec["success"])
            n_ok += int(success)
            row = {
                "id": task.id,
                "category": task.category,
                "query": task.query,
                "success": success,
                "required_tools": task.success.required_tools,  # 供 build 阶段算覆盖度(B 宽松判据)
                "final_answer": result.get("final_answer"),
                "trace": result.get("trace", []),
                "messages": result.get("messages", []),
                "error": result.get("error"),
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            mark = "✓" if success else "✗"
            tools = [t["tool"] for t in result.get("trace", [])]
            err = result.get("error")
            tail = f"  ERR={err}" if err else ""
            print(f"  [{mark}] {idx:2d}/{len(tasks)} {task.id:30s} tools={tools}{tail}",
                  flush=True)

        # 完成一个写一个（乱序，带计数）→ 实时可见进度；jsonl 顺序无关（下游按 id 配对）。
        with open(out_path, "w", encoding="utf-8") as f:
            if args.workers > 1:
                with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
                    futs = [pool.submit(run_one, t) for t in tasks]
                    for i, fut in enumerate(concurrent.futures.as_completed(futs), 1):
                        task, result = fut.result()
                        emit(f, task, result, i)
            else:
                for i, t in enumerate(tasks, 1):
                    task, result = run_one(t)
                    emit(f, task, result, i)
    finally:
        conn.close()

    # 工具 schema 一并落盘，build 脚本据此生成训练用 tools 字段（每档相同，覆盖写即可）
    tools_path = os.path.join(args.out_dir, "tools.openai.json")
    with open(tools_path, "w", encoding="utf-8") as f:
        json.dump(registry.openai_tools(), f, ensure_ascii=False, indent=2)

    print(f"\n档位 {args.tag}: {len(tasks)} 任务, {n_ok} 成功 → {out_path}")
    print(f"工具 schema → {tools_path}")
    print("下一步：两档都 dump 完后，python build_dpo_dataset.py 构造偏好对。")


if __name__ == "__main__":
    main()
