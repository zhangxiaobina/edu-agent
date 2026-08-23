"""Thin Tool Schema -> canonical teaching provider -> tool JSON mapping."""

from __future__ import annotations

from ..teaching import (
    PageRequest,
    TeachingCommand,
    TeachingCommandKind,
    TeachingOperationContext,
    TeachingProviderRejected,
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


def execute_teaching_command(
    kind: TeachingCommandKind,
    payload: dict,
    *,
    connection=None,
    context=None,
    provider=None,
    operation=None,
) -> dict:
    if provider is None:
        from .registry import teaching_data_provider

        provider = teaching_data_provider()
    operation_context = (
        TeachingOperationContext.from_operation(operation)
        if operation is not None
        else None
    )
    command = TeachingCommand(
        kind=kind,
        payload=payload,
        scope=TeachingScope.from_context(context),
        operation=operation_context,
    )
    result = provider.execute_command(command, connection=connection)
    if result.error is not None and (operation_context is not None or context is not None):
        # The exception preserves canonical error metadata for ToolResult.  On
        # writes it also crosses TransactionalToolRuntime so no partial
        # business/operation/outbox commit is possible.
        raise TeachingProviderRejected(result.error)
    return result.to_tool_result()
