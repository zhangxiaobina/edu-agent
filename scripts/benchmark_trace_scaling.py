#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import time
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path

from edu_agent.eval.provenance import (
    EVIDENCE_MODES,
    build_provenance,
    credential_literals,
    file_hash,
    provenance_gate_passed,
    sanitize_artifact,
)
from edu_agent.observability import TraceRepository
from edu_agent.runtime.models import RunContext
from edu_agent.state import StateStore


SEED = 42
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def benchmark(
    *, event_count: int, page_size: int, evidence_mode: str = "development"
) -> dict:
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
    input_hashes = {
        relative: file_hash(PROJECT_ROOT / relative)
        for relative in (
            "pyproject.toml",
            "uv.lock",
            "scripts/benchmark_trace_scaling.py",
            "edu_agent/observability/trace.py",
            "edu_agent/state/trace_index.py",
        )
    }
    config = {
        "event_count": event_count,
        "page_size": page_size,
        "seed": SEED,
        "model": {"name": "none", "mode": "offline_runtime_benchmark"},
        "input_hashes": input_hashes,
    }
    provenance = build_provenance(
        repo_root=PROJECT_ROOT,
        config=config,
        seed=SEED,
        model_name="none",
        model_mode="offline_runtime_benchmark",
        evidence_mode=evidence_mode,
    )
    report = {
        "schema_version": "edu-agent.trace-scaling.v2",
        "generated_at": datetime.now(UTC).isoformat(),
        **provenance,
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
    return sanitize_artifact(report, secrets=credential_literals())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, default=10_000)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--output")
    parser.add_argument("--evidence-mode", choices=EVIDENCE_MODES, default="development")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    report = benchmark(
        event_count=args.events,
        page_size=args.page_size,
        evidence_mode=args.evidence_mode,
    )
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
    if not args.quiet:
        print(encoded, end="")
    elif all(report["assertions"].values()) and provenance_gate_passed(report):
        print("trace scaling evaluation passed")
    else:
        print("trace scaling evaluation failed")
    return 0 if all(report["assertions"].values()) and provenance_gate_passed(report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
