from __future__ import annotations

import json
from typing import Any

from ..data_classification import redact, redact_text


def redact_sensitive_text(value: str) -> str:
    """Persistence-safe redaction: preserve scope keys and runtime metrics."""
    return redact_text(value)


def redact_sensitive(value: Any) -> Any:
    """Persistence-safe redaction driven by the shared data classifier."""
    return redact(value, include_pii=False)


def redact_sensitive_preview(value: Any) -> Any:
    """Model-visible previews omit credentials, student PII and private paths."""

    if isinstance(value, str):
        try:
            structured = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        else:
            if isinstance(structured, (dict, list)):
                return json.dumps(
                    redact(
                        structured,
                        include_pii=True,
                        include_private_paths=True,
                    ),
                    ensure_ascii=False,
                    default=str,
                )
    return redact(value, include_pii=True, include_private_paths=True)
