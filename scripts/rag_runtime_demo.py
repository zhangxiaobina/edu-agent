"""离线展示课程 RAG、引用验真、语义降级与 ACL。"""
from __future__ import annotations

import tempfile
from pathlib import Path

from edu_agent.knowledge import (
    KnowledgeToolProvider,
    SQLiteKnowledgeProvider,
    build_synthetic_corpus,
)
from edu_agent.runtime.models import RunContext
from edu_agent.state import StateStore
from edu_agent.tools import registry


def main() -> None:
    root = Path(tempfile.gettempdir()) / "edu-agent-rag-demo"
    root.mkdir(parents=True, exist_ok=True)
    knowledge_path = build_synthetic_corpus(root / "knowledge.db")
    state_path = root / "state.db"
    state_path.unlink(missing_ok=True)
    store = StateStore(state_path)
    knowledge = SQLiteKnowledgeProvider(
        knowledge_path,
        event_sink=lambda event: store.record_provider_event(**event),
    )
    provider = KnowledgeToolProvider(registry, knowledge, max_results=3)
    context = RunContext.create(
        session_id="rag-demo",
        actor_id="teacher-demo",
        role="teacher",
        tenant_id="school-1",
        course_ids={1},
    )

    question = "递归函数为什么必须有终止条件？"
    result = provider.dispatch_with_context(
        "retrieve_course_materials",
        {
            "query": question,
            "course_id": 1,
            "limit": 3,
            "mode": "hybrid",
        },
        context,
    )
    chunk = result["results"][0]
    citation = chunk["citation_id"]
    answer = f"{chunk['content']} [{citation}]"
    denied = provider.dispatch_with_context(
        "retrieve_course_materials",
        {"query": "事务原子性", "course_id": 2, "mode": "sparse"},
        context,
    )

    with store.connect() as connection:
        fallback_events = connection.execute(
            """
            SELECT COUNT(*) FROM provider_events
            WHERE provider='knowledge.semantic' AND event='retrieval_fallback'
            """
        ).fetchone()[0]

    print(f"question: {question}")
    print(f"retrieval: {chunk['retrieval_method']} score={chunk['score']}")
    print(f"answer: {answer}")
    print(f"citation verified: {knowledge.verify_citation(citation, context)}")
    print(f"claim verified: {knowledge.verify_claim(citation, answer, context)}")
    print(f"fake citation rejected: {not knowledge.verify_citation('fake:chunk-999', context)}")
    print(f"course ACL denied: {denied['results'] == []}")
    print(f"semantic fallback events: {fallback_events}")

    assert chunk["retrieval_method"] == "sparse_fallback"
    assert knowledge.verify_citation(citation, context)
    assert knowledge.verify_claim(citation, answer, context)
    assert denied["results"] == []
    assert fallback_events == 1


if __name__ == "__main__":
    main()
