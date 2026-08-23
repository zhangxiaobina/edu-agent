"""Thin Tool Schema -> canonical teaching provider -> tool JSON mapping."""

from __future__ import annotations

from ..teaching import (
    PageRequest,
    TeachingQuery,
    TeachingQueryKind,
    TeachingScope,
)


def execute_teaching_read(
    kind: TeachingQueryKind,
    filters: dict,
    *,
    page: PageRequest | None = None,
    connection=None,
    context=None,
    provider=None,
) -> dict:
    if provider is None:
        # Lazy import avoids a module cycle while keeping direct handler calls
        # on the same registry-backed provider as normal Agent dispatch.
        from .registry import teaching_data_provider

        provider = teaching_data_provider()
    query = TeachingQuery(
        kind=kind,
        filters=filters,
        scope=TeachingScope.from_context(context),
        page=page,
    )
    return provider.execute(query, connection=connection).to_tool_result()
