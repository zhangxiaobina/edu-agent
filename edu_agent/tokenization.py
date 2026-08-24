from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass
from typing import Callable, Protocol


CONSERVATIVE_ESTIMATOR_NAME = "edu-agent-conservative"
CONSERVATIVE_ESTIMATOR_VERSION = "2026-08-23.v1"


class TextTokenCounter(Protocol):
    def count(self, text: str) -> int: ...


class ConservativeTokenEstimator:
    """Deterministic fallback for models without an available tokenizer."""

    name = CONSERVATIVE_ESTIMATOR_NAME
    version = CONSERVATIVE_ESTIMATOR_VERSION

    def count(self, text: str) -> int:
        if not text:
            return 0
        wordish = 0
        whitespace = 0
        punctuation = 0
        non_ascii_bytes = 0
        for character in text:
            if ord(character) < 128:
                if character.isalnum() or character in {"_", "-"}:
                    wordish += 1
                elif character.isspace():
                    whitespace += 1
                else:
                    punctuation += 1
            else:
                non_ascii_bytes += len(character.encode("utf-8"))
        return max(
            1,
            math.ceil(wordish / 3)
            + math.ceil(whitespace / 4)
            + math.ceil(punctuation / 2)
            + math.ceil(non_ascii_bytes / 3),
        )


@dataclass(frozen=True)
class TokenCounterResolution:
    counter: TextTokenCounter
    method: str
    name: str
    version: str
    requested_tokenizer: str | None = None
    fallback_reason: str | None = None


@dataclass(frozen=True)
class _CallableCounter:
    callback: Callable[[str], int]

    def count(self, text: str) -> int:
        value = self.callback(text)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("tokenizer counter must return a non-negative integer")
        return value


_TOKENIZER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_KNOWN_TIKTOKEN_MODELS = (
    ("gpt-4o", "o200k_base"),
    ("gpt-4", "cl100k_base"),
    ("gpt-3.5", "cl100k_base"),
)


class TokenizerRegistry:
    """A narrow tokenizer registry; model matrices and vocab downloads stay out."""

    def __init__(self, estimator: TextTokenCounter | None = None):
        self.estimator = estimator or ConservativeTokenEstimator()
        self._counters: dict[str, tuple[TextTokenCounter, str]] = {}
        self._lock = threading.RLock()

    def register(
        self,
        name: str,
        counter: TextTokenCounter | Callable[[str], int],
        *,
        version: str,
    ) -> None:
        normalized = self._validate_name(name)
        if not isinstance(version, str) or not version.strip():
            raise ValueError("tokenizer version must be non-empty")
        resolved = counter if hasattr(counter, "count") else _CallableCounter(counter)
        with self._lock:
            self._counters[normalized] = (resolved, version.strip())

    def resolve(
        self,
        *,
        model: str | None,
        tokenizer: str | None = None,
    ) -> TokenCounterResolution:
        requested = tokenizer or self._known_tokenizer(model)
        if requested:
            normalized = self._validate_name(requested)
            with self._lock:
                registered = self._counters.get(normalized)
            if registered is not None:
                counter, version = registered
                return TokenCounterResolution(
                    counter,
                    "model_tokenizer",
                    normalized,
                    version,
                    requested_tokenizer=normalized,
                )
            loaded = self._load_tiktoken(normalized)
            if loaded is not None:
                return loaded
            fallback_reason = "tokenizer_unavailable"
        else:
            fallback_reason = "tokenizer_unknown"
        return TokenCounterResolution(
            self.estimator,
            "versioned_estimator",
            CONSERVATIVE_ESTIMATOR_NAME,
            CONSERVATIVE_ESTIMATOR_VERSION,
            requested_tokenizer=requested,
            fallback_reason=fallback_reason,
        )

    @staticmethod
    def _validate_name(name: str) -> str:
        if not isinstance(name, str) or _TOKENIZER_NAME.fullmatch(name) is None:
            raise ValueError("tokenizer name must be a stable identifier")
        return name

    @staticmethod
    def _known_tokenizer(model: str | None) -> str | None:
        normalized = str(model or "").lower()
        for prefix, encoding in _KNOWN_TIKTOKEN_MODELS:
            if normalized.startswith(prefix):
                return f"tiktoken:{encoding}"
        return None

    @staticmethod
    def _load_tiktoken(name: str) -> TokenCounterResolution | None:
        if not name.startswith("tiktoken:"):
            return None
        encoding_name = name.split(":", 1)[1]
        try:
            import tiktoken  # type: ignore[import-not-found]
        except ImportError:
            return None
        try:
            encoding = tiktoken.get_encoding(encoding_name)
        except (KeyError, ValueError):
            return None
        version = str(getattr(tiktoken, "__version__", "unknown"))
        return TokenCounterResolution(
            _CallableCounter(lambda text: len(encoding.encode(text))),
            "model_tokenizer",
            name,
            version,
            requested_tokenizer=name,
        )


DEFAULT_TOKENIZER_REGISTRY = TokenizerRegistry()


__all__ = [
    "CONSERVATIVE_ESTIMATOR_NAME",
    "CONSERVATIVE_ESTIMATOR_VERSION",
    "ConservativeTokenEstimator",
    "DEFAULT_TOKENIZER_REGISTRY",
    "TextTokenCounter",
    "TokenCounterResolution",
    "TokenizerRegistry",
]
