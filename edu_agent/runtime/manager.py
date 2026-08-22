from __future__ import annotations

import socket
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Iterator

from .models import RunContext


@dataclass(frozen=True)
class LeaseClaim:
    session_id: str
    run_id: str
    owner_id: str
    fencing_token: int
    expires_at: str


@dataclass(frozen=True)
class ActiveRun:
    run_id: str
    session_id: str
    actor_id: str
    tenant_id: str
    started_at: datetime
    owner_id: str | None = None
    fencing_token: int | None = None


def _instance_owner_id() -> str:
    # The random instance identity is the uniqueness boundary; hostname is diagnostic.
    return f"{socket.gethostname()}:{uuid.uuid4().hex}"


class RuntimeManager:
    """Two-layer run manager: process-local single-flight plus SQLite lease."""

    def __init__(
        self,
        state_store=None,
        *,
        owner_id: str | None = None,
        lease_seconds: float = 30.0,
        heartbeat_seconds: float = 10.0,
    ):
        if lease_seconds <= 0 or heartbeat_seconds <= 0:
            raise ValueError("session lease 和 heartbeat 间隔必须大于 0")
        if heartbeat_seconds >= lease_seconds:
            raise ValueError("session heartbeat 间隔必须小于 lease 时长")
        self.state_store = state_store
        self.owner_id = owner_id or _instance_owner_id()
        self.lease_seconds = float(lease_seconds)
        self.heartbeat_seconds = float(heartbeat_seconds)
        self._guard = threading.Lock()
        self._session_locks: dict[str, threading.RLock] = {}
        self._session_refs: dict[str, int] = {}
        self._active: dict[str, ActiveRun] = {}

    @contextmanager
    def session_scope(
        self,
        *,
        run_id: str,
        session_id: str,
        actor_id: str,
        tenant_id: str,
    ) -> Iterator[LeaseClaim | None]:
        with self._guard:
            lock = self._session_locks.setdefault(session_id, threading.RLock())
            self._session_refs[session_id] = self._session_refs.get(session_id, 0) + 1
        lock.acquire()
        claim = None
        stopped = threading.Event()
        heartbeat_thread = None
        try:
            if self.state_store is not None:
                record = self.state_store.acquire_session_lease(
                    session_id=session_id,
                    run_id=run_id,
                    owner_id=self.owner_id,
                    actor_id=actor_id,
                    tenant_id=tenant_id,
                    lease_seconds=self.lease_seconds,
                )
                claim = LeaseClaim(
                    session_id=session_id,
                    run_id=run_id,
                    owner_id=self.owner_id,
                    fencing_token=int(record["fencing_token"]),
                    expires_at=record["expires_at"],
                )
                heartbeat_thread = threading.Thread(
                    target=self._heartbeat_loop,
                    args=(claim, stopped),
                    name=f"edu-agent-session-heartbeat-{session_id[:8]}",
                    daemon=True,
                )
                heartbeat_thread.start()
            with self._guard:
                self._active[run_id] = ActiveRun(
                    run_id=run_id,
                    session_id=session_id,
                    actor_id=actor_id,
                    tenant_id=tenant_id,
                    started_at=datetime.now(UTC),
                    owner_id=claim.owner_id if claim else None,
                    fencing_token=claim.fencing_token if claim else None,
                )
            yield claim
        finally:
            stopped.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=max(1.0, self.heartbeat_seconds * 2))
            if claim is not None:
                # A worker may release its session lease only after the
                # durable run terminal is visible.  If a finalizer crashed
                # before that point, leave the lease to expire so a recovery
                # worker can acquire a higher fencing token without an
                # uncommitted run being falsely declared free.
                terminal = False
                try:
                    status = self.state_store.get_run_status(
                        claim.run_id,
                        actor_id=actor_id,
                        tenant_id=tenant_id,
                    )
                    terminal = bool(status and status.get("status") in {
                        "completed",
                        "failed",
                        "interrupted",
                        "abandoned",
                    })
                    finalizer = self.state_store.get_turn_finalizer(
                        claim.run_id,
                        session_id=claim.session_id,
                        actor_id=actor_id,
                        tenant_id=tenant_id,
                    )
                    if finalizer is not None:
                        terminal = finalizer.terminal
                except Exception:
                    terminal = False
                if terminal:
                    self.state_store.release_session_lease(
                        session_id=claim.session_id,
                        run_id=claim.run_id,
                        owner_id=claim.owner_id,
                        fencing_token=claim.fencing_token,
                    )
            with self._guard:
                self._active.pop(run_id, None)
            lock.release()
            with self._guard:
                refs = self._session_refs[session_id] - 1
                if refs == 0:
                    self._session_refs.pop(session_id, None)
                    self._session_locks.pop(session_id, None)
                else:
                    self._session_refs[session_id] = refs

    def bind_context(self, context: RunContext, claim: LeaseClaim | None) -> None:
        if claim is None:
            return
        context.bind_runtime_control(
            lease_owner=claim.owner_id,
            fencing_token=claim.fencing_token,
            control_check=lambda boundary: self.checkpoint(context, boundary),
        )

    def checkpoint(self, context: RunContext, boundary: str) -> None:
        if self.state_store is None:
            return
        self.state_store.assert_run_writable(context, boundary=boundary)

    def _heartbeat_loop(self, claim: LeaseClaim, stopped: threading.Event) -> None:
        while not stopped.wait(self.heartbeat_seconds):
            if not self.state_store.heartbeat_session_lease(
                session_id=claim.session_id,
                run_id=claim.run_id,
                owner_id=claim.owner_id,
                fencing_token=claim.fencing_token,
                lease_seconds=self.lease_seconds,
            ):
                return

    def active_runs(self) -> list[dict]:
        with self._guard:
            return [asdict(run) for run in self._active.values()]
