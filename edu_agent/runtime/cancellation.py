from __future__ import annotations

import inspect
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Cancellation:
    reason: str
    source: str
    requested_at: float


class CancellationRequested(RuntimeError):
    def __init__(self, cancellation: Cancellation, *, boundary: str | None = None):
        self.cancellation = cancellation
        self.boundary = boundary
        message = cancellation.reason
        if boundary:
            message = f"{boundary}: {message}"
        super().__init__(message)


CancellationCallback = Callable[[Cancellation], None]


class CancellationToken:
    """Thread-safe cooperative cancellation shared by one run tree."""

    def __init__(
        self,
        *,
        deadline: float | None = None,
        parent: CancellationToken | None = None,
    ):
        if deadline is not None and deadline <= 0:
            raise ValueError("deadline must be a positive monotonic timestamp")
        self.deadline = deadline
        self._lock = threading.RLock()
        self._event = threading.Event()
        self._cancellation: Cancellation | None = None
        self._callbacks: dict[int, CancellationCallback] = {}
        self._next_callback_id = 0
        self._unlink_parent: Callable[[], None] | None = None
        if parent is not None:
            self._unlink_parent = parent.register(
                lambda cancellation: self.cancel(
                    cancellation.reason,
                    source=cancellation.source,
                )
            )

    @classmethod
    def with_timeout(
        cls,
        timeout_seconds: float,
        *,
        parent: CancellationToken | None = None,
    ) -> CancellationToken:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        deadline = time.monotonic() + float(timeout_seconds)
        if parent is not None and parent.deadline is not None:
            deadline = min(deadline, parent.deadline)
        return cls(deadline=deadline, parent=parent)

    @property
    def cancellation(self) -> Cancellation | None:
        self._expire_deadline()
        with self._lock:
            return self._cancellation

    @property
    def cancelled(self) -> bool:
        return self.cancellation is not None

    def is_set(self) -> bool:
        return self.cancelled

    def remaining_seconds(self) -> float | None:
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - time.monotonic())

    def cancel(self, reason: str = "run cancelled", *, source: str = "explicit") -> bool:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("cancellation reason must be a non-empty string")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("cancellation source must be a non-empty string")
        callbacks: tuple[CancellationCallback, ...]
        with self._lock:
            if self._cancellation is not None:
                return False
            cancellation = Cancellation(
                reason=reason,
                source=source,
                requested_at=time.monotonic(),
            )
            self._cancellation = cancellation
            callbacks = tuple(self._callbacks.values())
            self._callbacks.clear()
            self._event.set()
        for callback in callbacks:
            try:
                callback(cancellation)
            except Exception:
                # Cancellation must remain idempotent even if a best-effort
                # transport/provider close callback fails.
                continue
        return True

    def checkpoint(self, boundary: str | None = None) -> None:
        cancellation = self.cancellation
        if cancellation is not None:
            raise CancellationRequested(cancellation, boundary=boundary)

    def wait(self, timeout: float | None = None) -> bool:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative")
        self._expire_deadline()
        if self._event.is_set():
            return True
        wait_for = timeout
        remaining = self.remaining_seconds()
        if remaining is not None:
            wait_for = remaining if wait_for is None else min(wait_for, remaining)
        signalled = self._event.wait(wait_for)
        self._expire_deadline()
        return signalled or self._event.is_set()

    def register(self, callback: CancellationCallback) -> Callable[[], None]:
        if not callable(callback):
            raise TypeError("cancellation callback must be callable")
        cancellation = self.cancellation
        if cancellation is not None:
            callback(cancellation)
            return lambda: None
        with self._lock:
            if self._cancellation is not None:
                cancellation = self._cancellation
            else:
                callback_id = self._next_callback_id
                self._next_callback_id += 1
                self._callbacks[callback_id] = callback

                def unregister() -> None:
                    with self._lock:
                        self._callbacks.pop(callback_id, None)

                return unregister
        assert cancellation is not None
        callback(cancellation)
        return lambda: None

    def close(self) -> None:
        unlink = self._unlink_parent
        self._unlink_parent = None
        if unlink is not None:
            unlink()

    def _expire_deadline(self) -> None:
        if (
            self.deadline is not None
            and not self._event.is_set()
            and time.monotonic() >= self.deadline
        ):
            self.cancel("run deadline exceeded", source="deadline")


def accepts_cancellation_token(call: Callable[..., Any]) -> bool:
    try:
        parameters = inspect.signature(call).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "cancellation_token"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def call_with_cancellation(
    call: Callable[..., Any],
    *args: Any,
    cancellation_token: CancellationToken | None = None,
    **kwargs: Any,
) -> Any:
    if cancellation_token is not None:
        cancellation_token.checkpoint("provider.before_call")
        if accepts_cancellation_token(call):
            kwargs["cancellation_token"] = cancellation_token
    result = call(*args, **kwargs)
    if cancellation_token is not None:
        cancellation_token.checkpoint("provider.after_call")
    return result
