from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..data_classification import contains_sensitive, redact, redact_text


@dataclass(frozen=True)
class RedactionPolicy:
    """Central fail-closed trace/export redaction policy.

    ``literal_secrets`` is intended for deployment canaries and known local
    credentials. Literals are never included in the exported event.
    """

    literal_secrets: tuple[str, ...] = field(default_factory=tuple)

    def redact_text(self, value: str) -> str:
        return redact_text(value, include_pii=True, literal_secrets=self.literal_secrets)

    def redact(self, value: Any) -> Any:
        return redact(value, include_pii=True, literal_secrets=self.literal_secrets)


def contains_sensitive_data(value: Any, *, secrets: tuple[str, ...] = ()) -> bool:
    return contains_sensitive(value, include_pii=True, secrets=secrets)
