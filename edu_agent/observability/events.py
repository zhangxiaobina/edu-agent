from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = "edu-agent.runtime-event.v1"


@dataclass(frozen=True)
class RuntimeEvent:
    """Versioned, owner-scoped event projected from persistent runtime state."""

    event_id: str
    timestamp: str
    sequence: int
    run_id: str | None
    root_run_id: str | None
    parent_run_id: str | None
    session_id: str | None
    actor_id: str
    tenant_id: str
    component: str
    event_type: str
    status: str | None = None
    duration_ms: float | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
