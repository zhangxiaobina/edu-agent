from __future__ import annotations

from typing import Any

from ..data_classification import redact, redact_text


def redact_sensitive_text(value: str) -> str:
    """Persistence-safe redaction: preserve scope keys and runtime metrics."""
    return redact_text(value)


def redact_sensitive(value: Any) -> Any:
    """Persistence-safe redaction driven by the shared data classifier."""
    return redact(value, include_pii=False)
