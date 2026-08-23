"""Storage-neutral contracts for canonical teaching-data reads.

These contracts describe authoritative teaching data such as exams, scores,
progress and knowledge paths.  They must not be confused with the R1 model
``ProviderGateway`` in :mod:`edu_agent.engine.gateway`.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from types import MappingProxyType
from typing import Any


class TeachingQueryKind(str, Enum):
    SCORE_RECORDS = "score_records"
    EXAMS = "exams"
    CLASS_ROSTER = "class_roster"
    QUESTIONS = "questions"
    LEARNING_PROGRESS = "learning_progress"
    CLASS_ERRORS = "class_errors"
    WEAK_POINTS = "weak_points"
    SCORE_DISTRIBUTION = "score_distribution"
    KNOWLEDGE_GRAPH = "knowledge_graph"
    STUDY_PATH = "study_path"


class TeachingProviderErrorKind(str, Enum):
    INVALID_QUERY = "invalid_query"
    NOT_FOUND = "not_found"
    SCOPE_DENIED = "scope_denied"
    UNAVAILABLE = "unavailable"
    INTERNAL = "internal"


class ExamStatus(IntEnum):
    NOT_STARTED = 0
    IN_PROGRESS = 1
    FINISHED = 2

    @property
    def label(self) -> str:
        return {
            ExamStatus.NOT_STARTED: "未开始",
            ExamStatus.IN_PROGRESS: "进行中",
            ExamStatus.FINISHED: "已结束",
        }[self]

    @classmethod
    def label_for(cls, value: object) -> str:
        try:
            return cls(int(value)).label
        except (TypeError, ValueError):
            return ""


@dataclass(frozen=True)
class PageRequest:
    number: int = 1
    size: int = 50


@dataclass(frozen=True)
class TeachingScope:
    """Identity and course boundary carried to every authoritative read."""

    tenant_id: str | None = None
    actor_id: str | None = None
    role: str | None = None
    course_ids: frozenset[int] = field(default_factory=frozenset)
    enforce_course_scope: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "course_ids", frozenset(int(item) for item in self.course_ids))

    @classmethod
    def unrestricted(cls) -> TeachingScope:
        return cls()

    @classmethod
    def restricted(
        cls,
        course_ids,
        *,
        tenant_id: str | None = None,
        actor_id: str | None = None,
        role: str | None = None,
    ) -> TeachingScope:
        return cls(
            tenant_id=tenant_id,
            actor_id=actor_id,
            role=role,
            course_ids=frozenset(course_ids),
            enforce_course_scope=True,
        )

    @classmethod
    def from_context(cls, context) -> TeachingScope:
        if context is None:
            return cls.unrestricted()
        role = getattr(context, "role", None)
        course_ids = frozenset(getattr(context, "course_ids", ()) or ())
        return cls(
            tenant_id=getattr(context, "tenant_id", None),
            actor_id=getattr(context, "actor_id", None),
            role=role,
            course_ids=course_ids,
            # Preserve the existing Runtime rule: admin/system and an empty
            # course set are not constrained by a course allow-list.
            enforce_course_scope=bool(course_ids) and role not in {"admin", "system"},
        )

    def allows_course(self, course_id: int) -> bool:
        return not self.enforce_course_scope or int(course_id) in self.course_ids


@dataclass(frozen=True)
class TeachingQuery:
    kind: TeachingQueryKind
    filters: Mapping[str, Any] = field(default_factory=dict)
    scope: TeachingScope = field(default_factory=TeachingScope.unrestricted)
    page: PageRequest | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TeachingQueryKind):
            raise TypeError("kind 必须是 TeachingQueryKind")
        if not isinstance(self.scope, TeachingScope):
            raise TypeError("scope 必须是 TeachingScope")
        normalized = _canonical_value(dict(self.filters), path="filters")
        object.__setattr__(self, "filters", MappingProxyType(normalized))


@dataclass(frozen=True)
class TeachingProviderError:
    kind: TeachingProviderErrorKind
    message: str
    retryable: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TeachingProviderErrorKind):
            raise TypeError("kind 必须是 TeachingProviderErrorKind")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("provider error message 不能为空")
        normalized = _canonical_value(dict(self.details), path="error.details")
        object.__setattr__(self, "details", normalized)


@dataclass(frozen=True)
class TeachingResult:
    data: Mapping[str, Any] | None = None
    error: TeachingProviderError | None = None

    def __post_init__(self) -> None:
        if (self.data is None) == (self.error is None):
            raise ValueError("TeachingResult 必须且只能包含 data 或 error")
        if self.data is not None:
            normalized = _canonical_value(dict(self.data), path="result")
            object.__setattr__(self, "data", normalized)

    @property
    def ok(self) -> bool:
        return self.error is None

    @classmethod
    def success(cls, data: Mapping[str, Any]) -> TeachingResult:
        return cls(data=data)

    @classmethod
    def failure(
        cls,
        kind: TeachingProviderErrorKind,
        message: str,
        *,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> TeachingResult:
        return cls(
            error=TeachingProviderError(
                kind=kind,
                message=message,
                retryable=retryable,
                details=details or {},
            )
        )

    def to_tool_result(self) -> dict:
        """Map the canonical result to the pre-R3 tool JSON shape."""

        if self.error is not None:
            return {"error": self.error.message}
        return _canonical_value(dict(self.data or {}), path="tool_result")


class TeachingDataProvider(ABC):
    """Minimal domain provider used by read-only teaching tools.

    ``connection`` is an adapter-private escape hatch for a caller-owned,
    controlled transaction.  Ordinary calls leave it unset so each invocation
    obtains its own connection or remote request context.
    """

    @abstractmethod
    def execute(self, query: TeachingQuery, *, connection: object | None = None) -> TeachingResult:
        raise NotImplementedError


def _canonical_value(value: Any, *, path: str) -> Any:
    """Copy JSON-compatible values and reject storage-specific objects."""

    if isinstance(value, Enum):
        return _canonical_value(value.value, path=path)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError(f"{path} 包含非有限浮点数")
        return value
    if isinstance(value, Mapping):
        normalized = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} 的字段名必须是字符串")
            normalized[key] = _canonical_value(item, path=f"{path}.{key}")
        return normalized
    if isinstance(value, (list, tuple)):
        return [
            _canonical_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise TypeError(f"{path} 包含非 canonical 类型 {type(value).__name__}")
