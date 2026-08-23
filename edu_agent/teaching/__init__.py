"""Canonical teaching-data provider contracts.

This package isolates teaching-domain data access from tools.  It is separate
from :mod:`edu_agent.engine.gateway`, which routes model API providers.
"""

from .contracts import (
    ExamStatus,
    PageRequest,
    TeachingDataProvider,
    TeachingProviderError,
    TeachingProviderErrorKind,
    TeachingQuery,
    TeachingQueryKind,
    TeachingResult,
    TeachingScope,
)
from .synthetic import SyntheticProvider

__all__ = [
    "ExamStatus",
    "PageRequest",
    "SyntheticProvider",
    "TeachingDataProvider",
    "TeachingProviderError",
    "TeachingProviderErrorKind",
    "TeachingQuery",
    "TeachingQueryKind",
    "TeachingResult",
    "TeachingScope",
]
