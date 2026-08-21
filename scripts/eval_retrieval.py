"""在固定 seed 合成课件上评测离线稀疏检索与 ACL。"""
from __future__ import annotations

import json
import math
import sys
import tempfile
from pathlib import Path

from edu_agent.knowledge import SQLiteKnowledgeProvider, build_synthetic_corpus


BENCHMARK = (
    (
        "递归函数为什么必须有终止条件",
        "school-1:course-1:python-basics:v1:recursion:chunk-003",
    ),
    (
        "B+ 树为什么适合范围查询",
        "school-1:course-2:database-index:v1:btree:chunk-001",
    ),
    (
        "事务原子性如何保证",
        "school-1:course-2:database-index:v1:acid:chunk-002",
    ),
    (
        "函数局部变量作用域",
        "school-1:course-1:python-basics:v1:function-scope:chunk-002",
    ),
    (
        "range 右边界是否包含",
        "school-1:course-1:python-basics:v1:for-loop:chunk-001",
    ),
)


def evaluate_sparse(knowledge: SQLiteKnowledgeProvider, *, limit: int = 3) -> dict:
    hits = 0
    reciprocal_ranks = []
    discounted_gains = []
    relevant_returned = 0
    total_returned = 0
    verified_queries = 0
    rows = []
    for query, expected in BENCHMARK:
        results = knowledge.search(
            query,
            tenant_id="school-1",
            course_ids=frozenset({1, 2}),
            limit=limit,
            mode="sparse",
        )
        citations = [item["citation_id"] for item in results]
        rank = citations.index(expected) + 1 if expected in citations else None
        hits += int(rank is not None)
        reciprocal_ranks.append(1.0 / rank if rank else 0.0)
        discounted_gains.append(1.0 / math.log2(rank + 1) if rank else 0.0)
        relevant_returned += int(rank is not None)
        total_returned += len(citations)
        verified_queries += int(
            rank is not None
            and knowledge.resolve_citation(
                expected,
                tenant_id="school-1",
                course_ids=frozenset({1, 2}),
            )
            is not None
        )
        rows.append({"query": query, "expected": expected, "rank": rank})
    count = len(BENCHMARK)
    return {
        "status": "verified",
        "queries": count,
        "k": limit,
        "recall_at_k": round(hits / count, 6),
        "mrr_at_k": round(sum(reciprocal_ranks) / count, 6),
        "ndcg_at_k": round(sum(discounted_gains) / count, 6),
        "citation_precision": round(
            relevant_returned / max(1, total_returned), 6
        ),
        "citation_coverage": round(verified_queries / count, 6),
        "details": rows,
    }


def evaluate_acl(knowledge: SQLiteKnowledgeProvider) -> dict:
    probes = (
        ("跨租户机密答案", "school-1", frozenset({1})),
        ("事务原子性", "school-1", frozenset({1})),
        ("递归终止条件", "school-1", frozenset({2})),
    )
    leaked = 0
    returned = 0
    for query, tenant_id, course_ids in probes:
        results = knowledge.search(
            query,
            tenant_id=tenant_id,
            course_ids=course_ids,
            limit=5,
            mode="sparse",
        )
        returned += len(results)
        for item in results:
            if not item["citation_id"].startswith(f"{tenant_id}:"):
                leaked += 1
            if item["course_id"] not in course_ids:
                leaked += 1
    return {
        "probes": len(probes),
        "returned_chunks": returned,
        "leaked_chunks": leaked,
        "leak_rate": round(leaked / max(1, returned), 6),
    }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="edu-agent-retrieval-") as directory:
        path = build_synthetic_corpus(Path(directory) / "knowledge.db", seed=42)
        knowledge = SQLiteKnowledgeProvider(path)
        sparse = evaluate_sparse(knowledge)
        acl = evaluate_acl(knowledge)
    report = {
        "benchmark": "synthetic-course-materials",
        "seed": 42,
        "corpus": "synthetic_only",
        "ablations": {
            "sparse_sqlite_fts5": sparse,
            "semantic": {"status": "not_enabled", "metrics": None},
            "hybrid": {
                "status": "not_verified_without_semantic_provider",
                "metrics": None,
            },
            "hybrid_rerank": {
                "status": "not_verified_without_semantic_provider",
                "metrics": None,
            },
        },
        "acl": acl,
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    print("\nRetrieval ablation", file=sys.stderr)
    print("mode                 status                               Recall@3  MRR@3  nDCG@3", file=sys.stderr)
    print(
        "sparse_sqlite_fts5  verified                             "
        f"{sparse['recall_at_k']:.3f}     {sparse['mrr_at_k']:.3f}  "
        f"{sparse['ndcg_at_k']:.3f}",
        file=sys.stderr,
    )
    print("semantic             not_enabled                          -         -      -", file=sys.stderr)
    print("hybrid               not_verified_without_semantic       -         -      -", file=sys.stderr)
    print("hybrid_rerank        not_verified_without_semantic       -         -      -", file=sys.stderr)
    print(
        "citation precision="
        f"{sparse['citation_precision']:.3f}, coverage={sparse['citation_coverage']:.3f}, "
        f"ACL leak rate={acl['leak_rate']:.3f}",
        file=sys.stderr,
    )
    assert sparse["recall_at_k"] == 1.0
    assert sparse["mrr_at_k"] == 1.0
    assert sparse["ndcg_at_k"] == 1.0
    assert sparse["citation_coverage"] == 1.0
    assert acl["leak_rate"] == 0.0


if __name__ == "__main__":
    main()
