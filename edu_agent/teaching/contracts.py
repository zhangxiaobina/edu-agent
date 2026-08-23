"""Storage-neutral contracts for canonical teaching queries and commands.

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


class TeachingCommandKind(str, Enum):
    CREATE_EXAM = "create_exam"
    GENERATE_PAPER = "generate_paper"
    BATCH_GRADE = "batch_grade"
    ASSIGN_HOMEWORK = "assign_homework"
    GENERATE_QUESTIONS = "generate_questions"


class TeachingCommandEffect(str, Enum):
    READ = "read"
    PURE = "pure"
    WRITE = "write"


class TeachingProviderErrorKind(str, Enum):
    INVALID_QUERY = "invalid_query"
    INVALID_COMMAND = "invalid_command"
    NOT_FOUND = "not_found"
    BUSINESS_REJECTED = "business_rejected"
    SCOPE_DENIED = "scope_denied"
    APPROVAL_REQUIRED = "approval_required"
    UNSUPPORTED = "unsupported"
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
class TeachingOperationContext:
    """Executor-issued identity for one approved transactional command.

    This is transport-neutral metadata, not an alternate transaction manager.
    Providers may pass the idempotency key to a future platform API, while the
    local provider still joins the caller-owned business transaction.
    """

    operation_id: str
    idempotency_key: str
    payload_hash: str
    approval_scope: str
    status: str
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "operation_id",
            "idempotency_key",
            "payload_hash",
            "approval_scope",
            "status",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} 必须是非空字符串")
        normalized = _canonical_value(dict(self.arguments), path="operation.arguments")
        object.__setattr__(self, "arguments", MappingProxyType(normalized))

    @classmethod
    def from_operation(cls, operation: Mapping[str, Any]) -> TeachingOperationContext:
        if not isinstance(operation, Mapping):
            raise TypeError("operation 必须是 mapping")
        return cls(
            operation_id=str(operation["id"]),
            idempotency_key=str(operation["idempotency_key"]),
            payload_hash=str(operation["payload_hash"]),
            approval_scope=str(operation["approval_scope"]),
            status=str(operation["status"]),
            arguments=operation.get("arguments") or {},
        )


@dataclass(frozen=True)
class TeachingCommand:
    kind: TeachingCommandKind
    payload: Mapping[str, Any] = field(default_factory=dict)
    scope: TeachingScope = field(default_factory=TeachingScope.unrestricted)
    operation: TeachingOperationContext | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TeachingCommandKind):
            raise TypeError("kind 必须是 TeachingCommandKind")
        if not isinstance(self.scope, TeachingScope):
            raise TypeError("scope 必须是 TeachingScope")
        if self.operation is not None and not isinstance(
            self.operation, TeachingOperationContext
        ):
            raise TypeError("operation 必须是 TeachingOperationContext")
        normalized = _canonical_value(dict(self.payload), path="payload")
        object.__setattr__(self, "payload", MappingProxyType(normalized))

    @property
    def effect(self) -> TeachingCommandEffect:
        if self.kind is TeachingCommandKind.GENERATE_PAPER:
            return TeachingCommandEffect.READ
        if self.kind is TeachingCommandKind.GENERATE_QUESTIONS:
            return (
                TeachingCommandEffect.WRITE
                if bool(self.payload.get("save_to_bank"))
                else TeachingCommandEffect.PURE
            )
        return TeachingCommandEffect.WRITE

    @property
    def mutating(self) -> bool:
        return self.effect is TeachingCommandEffect.WRITE


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


@dataclass(frozen=True)
class TeachingReceipt:
    kind: TeachingCommandKind
    effect: TeachingCommandEffect
    data: Mapping[str, Any]
    request_id: str | None = None
    operation_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TeachingCommandKind):
            raise TypeError("receipt kind 必须是 TeachingCommandKind")
        if not isinstance(self.effect, TeachingCommandEffect):
            raise TypeError("receipt effect 必须是 TeachingCommandEffect")
        normalized = _canonical_value(dict(self.data), path="receipt.data")
        object.__setattr__(self, "data", normalized)
        for name in ("request_id", "operation_id"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"receipt {name} 必须为非空字符串或 None")

    def to_tool_result(self) -> dict:
        """Keep the pre-provider tool payload stable."""

        return _canonical_value(dict(self.data), path="tool_result")


@dataclass(frozen=True)
class TeachingCommandResult:
    receipt: TeachingReceipt | None = None
    error: TeachingProviderError | None = None

    def __post_init__(self) -> None:
        if (self.receipt is None) == (self.error is None):
            raise ValueError("TeachingCommandResult 必须且只能包含 receipt 或 error")

    @property
    def ok(self) -> bool:
        return self.error is None

    @classmethod
    def success(
        cls,
        command: TeachingCommand,
        data: Mapping[str, Any],
    ) -> TeachingCommandResult:
        operation = command.operation
        return cls(
            receipt=TeachingReceipt(
                kind=command.kind,
                effect=command.effect,
                data=data,
                request_id=operation.idempotency_key if operation is not None else None,
                operation_id=operation.operation_id if operation is not None else None,
            )
        )

    @classmethod
    def failure(
        cls,
        kind: TeachingProviderErrorKind,
        message: str,
        *,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> TeachingCommandResult:
        return cls(
            error=TeachingProviderError(
                kind=kind,
                message=message,
                retryable=retryable,
                details=details or {},
            )
        )

    def to_tool_result(self) -> dict:
        if self.error is not None:
            return {"error": self.error.message}
        return self.receipt.to_tool_result()


class TeachingProviderRejected(RuntimeError):
    """Preserve a canonical business rejection across a DB rollback."""

    def __init__(self, error: TeachingProviderError):
        super().__init__(error.message)
        self.error = error


class TeachingDataProvider(ABC):
    """Minimal domain provider used by the built-in teaching tools.

    ``connection`` is an adapter-private escape hatch for a caller-owned,
    controlled transaction.  Ordinary calls leave it unset so each invocation
    obtains its own connection or remote request context.
    """

    @property
    def supports_parallel_reads(self) -> bool:
        """Providers opt in only when each read owns an isolated request context."""

        return False

    @abstractmethod
    def execute(self, query: TeachingQuery, *, connection: object | None = None) -> TeachingResult:
        raise NotImplementedError

    @abstractmethod
    def execute_command(
        self,
        command: TeachingCommand,
        *,
        connection: object | None = None,
    ) -> TeachingCommandResult:
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
