"""Canonical teaching-data provider contracts.

This package isolates teaching-domain data access from tools.  It is separate
from :mod:`edu_agent.engine.gateway`, which routes model API providers.
"""

from .contracts import (
    ExamStatus,
    PageRequest,
    TeachingCommand,
    TeachingCommandEffect,
    TeachingCommandKind,
    TeachingCommandResult,
    TeachingDataProvider,
    TeachingOperationContext,
    TeachingProviderError,
    TeachingProviderErrorKind,
    TeachingProviderRejected,
    TeachingQuery,
    TeachingQueryKind,
    TeachingReceipt,
    TeachingResult,
    TeachingScope,
)
from .synthetic import SyntheticProvider

__all__ = [
    "ExamStatus",
    "PageRequest",
    "SyntheticProvider",
    "TeachingCommand",
    "TeachingCommandEffect",
    "TeachingCommandKind",
    "TeachingCommandResult",
    "TeachingDataProvider",
    "TeachingOperationContext",
    "TeachingProviderError",
    "TeachingProviderErrorKind",
    "TeachingProviderRejected",
    "TeachingQuery",
    "TeachingQueryKind",
    "TeachingReceipt",
    "TeachingResult",
    "TeachingScope",
]
