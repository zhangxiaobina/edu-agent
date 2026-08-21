#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import tempfile
import time
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path

from edu_agent.observability import TraceRepository
from edu_agent.runtime.models import RunContext
from edu_agent.state import StateStore


SEED = 42


def _commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "not_available"


def benchmark(*, event_count: int, page_size: int) -> dict:
    if event_count <= 0 or page_size <= 0 or page_size > 500:
        raise ValueError("event_count must be positive and page_size must be 1..500")
    with tempfile.TemporaryDirectory(prefix="edu-agent-trace-benchmark-") as directory:
        state = StateStore(Path(directory) / "state.db")
        state.ensure_session(
            "benchmark-session", actor_id="benchmark", tenant_id="synthetic", role="teacher"
        )
        context = RunContext.create(
            session_id="benchmark-session",
            run_id="benchmark-run",
            actor_id="benchmark",
            tenant_id="synthetic",
            role="teacher",
        )
        state.enqueue_run(context, request_text="synthetic trace benchmark")
        origin = datetime(2026, 8, 18, tzinfo=UTC)
        rows = [
            (
                "benchmark-run",
                "synthetic-provider",
                "attempt",
                None,
                index,
                json.dumps({"status": "ok", "input_tokens": index % 97}),
                (origin + timedelta(microseconds=index)).isoformat(),
            )
            for index in range(event_count)
        ]
        with state.connect() as connection:
            connection.executemany(
                """
                INSERT INTO provider_events(
                    run_id, provider, event, error_class, attempt, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        repository = TraceRepository(state)
        cursor = None
        pages = 0
        queries = 0
        loaded_rows = 0
        exported = 0
        digest = hashlib.sha256()
        started = time.perf_counter()
        tracemalloc.start()
        while True:
            page = repository.list_events(
                actor_id="benchmark",
                tenant_id="synthetic",
                run_id="benchmark-run",
                cursor=cursor,
                limit=page_size,
            )
            pages += 1
            queries += repository.last_query_stats["sql_queries"]
            loaded_rows += repository.last_query_stats["rows_loaded"]
            for event in page.events:
                digest.update(event.event_id.encode())
                exported += 1
            if page.next_cursor is None:
                total = page.total
                break
            cursor = page.next_cursor
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        elapsed = time.perf_counter() - started
    config = {"event_count": event_count, "page_size": page_size, "seed": SEED}
    return {
        "schema_version": "edu-agent.trace-scaling.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "commit": _commit(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "seed": SEED,
        "config_hash": hashlib.sha256(
            json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "config": config,
        "metrics": {
            "indexed_events": event_count,
            "projected_events": total,
            "exported_events": exported,
            "pages": pages,
            "sql_queries": queries,
            "rows_loaded": loaded_rows,
            "max_rows_loaded_per_page": page_size + 1,
            "peak_memory_bytes": peak_bytes,
            "elapsed_seconds": round(elapsed, 6),
            "events_per_second": round(exported / elapsed, 3),
            "digest": digest.hexdigest(),
        },
        "assertions": {
            "complete": exported == total == event_count + 1,
            "bounded_page_reads": loaded_rows <= pages * (page_size + 1),
            "no_page_times_full_projection": queries <= pages * 3,
        },
        "interpretation": (
            "This deterministic local sample demonstrates keyset page reads bounded by page_size. "
            "It is not a long-term latency or production-capacity claim."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, default=10_000)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--output")
    args = parser.parse_args()
    report = benchmark(event_count=args.events, page_size=args.page_size)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if all(report["assertions"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
