"""Local, read-only Trace Inspector.

Examples:
  uv run --frozen python scripts/trace_inspector.py --state /tmp/state.db \
      --actor teacher-1 --tenant school-1 --run RUN_ID --format summary
  uv run --frozen python scripts/trace_inspector.py --state /tmp/state.db \
      --actor teacher-1 --tenant school-1 --run RUN_ID --format review
  uv run --frozen python scripts/trace_inspector.py --state /tmp/state.db \
      --actor teacher-1 --tenant school-1 --run RUN_ID --format jsonl > trace.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys

from edu_agent.observability import TraceRepository
from edu_agent.state import StateStore


def main() -> int:
    parser = argparse.ArgumentParser(description="只读、脱敏、可分页的 EduAgent Trace Inspector")
    parser.add_argument("--state", required=True, help="StateStore SQLite path")
    parser.add_argument("--actor", required=True)
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--run")
    parser.add_argument("--session")
    parser.add_argument("--status")
    parser.add_argument("--error")
    parser.add_argument("--tool")
    parser.add_argument("--provider")
    parser.add_argument("--component")
    parser.add_argument("--cursor", help="opaque versioned cursor returned by a previous page")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--format",
        choices=("summary", "review", "json", "jsonl"),
        default="summary",
    )
    args = parser.parse_args()
    if args.format == "review" and not args.run:
        parser.error("--format review requires --run")
    repository = TraceRepository(StateStore(args.state, read_only=True))
    query = {
        "actor_id": args.actor,
        "tenant_id": args.tenant,
        "run_id": args.run,
        "session_id": args.session,
        "status": args.status,
        "error": args.error,
        "tool": args.tool,
        "provider": args.provider,
        "component": args.component,
    }
    if args.format == "summary":
        if not args.run:
            cursor = None if args.cursor in {None, "0"} else args.cursor
            page = repository.list_events(cursor=cursor, limit=args.limit, **query)
            payload = page.to_dict()
        else:
            payload = repository.inspect_run(args.run, actor_id=args.actor, tenant_id=args.tenant)
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.format == "review":
        payload = repository.inspect_run(
            args.run,
            actor_id=args.actor,
            tenant_id=args.tenant,
        )["review"]
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.format == "json":
        print("".join(repository.iter_export(format="json", page_size=args.limit, **query)))
        return 0
    for line in repository.iter_export(format="jsonl", page_size=args.limit, **query):
        sys.stdout.write(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
