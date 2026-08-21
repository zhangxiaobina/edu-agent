from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..runtime.models import RunContext
from .store import StateStore


@dataclass(frozen=True)
class MemorySnapshot:
    items: list[str]
    ids: list[int]


class MemoryProvider(ABC):
    @abstractmethod
    def remember(self, context: RunContext, content: str, **kwargs) -> int:
        raise NotImplementedError

    @abstractmethod
    def snapshot(self, context: RunContext, query: str) -> MemorySnapshot:
        raise NotImplementedError


class MemoryManager(MemoryProvider):
    def __init__(self, store: StateStore, *, max_items: int = 6, max_item_chars: int = 800):
        self.store = store
        self.max_items = max_items
        self.max_item_chars = max_item_chars

    def remember(
        self,
        context: RunContext,
        content: str,
        *,
        kind: str = "fact",
        importance: float = 0.5,
        scope: str = "user",
        scope_id: str = "",
        source: str = "explicit",
        expires_at: str | None = None,
        conflict_key: str | None = None,
    ) -> int:
        return self.store.add_memory(
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
            content=content[: self.max_item_chars],
            scope=scope,
            scope_id=scope_id,
            kind=kind,
            importance=importance,
            source_session_id=context.session_id,
            source=source,
            expires_at=expires_at,
            conflict_key=conflict_key,
        )

    def update(
        self,
        context: RunContext,
        memory_id: int,
        content: str,
        *,
        importance: float | None = None,
        expires_at: str | None = None,
    ) -> bool:
        return self.store.update_memory(
            memory_id,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
            content=content[: self.max_item_chars],
            importance=importance,
            expires_at=expires_at,
        )

    def deactivate(self, context: RunContext, memory_id: int) -> bool:
        return self.store.deactivate_memory(
            memory_id,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
        )

    def snapshot(self, context: RunContext, query: str) -> MemorySnapshot:
        scope_ids = {str(course_id) for course_id in context.course_ids}
        records = self.store.search_memories(
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
            query=query,
            limit=self.max_items,
            scope_ids=scope_ids,
        )
        return MemorySnapshot(
            items=[record["content"][: self.max_item_chars] for record in records],
            ids=[record["id"] for record in records],
        )
