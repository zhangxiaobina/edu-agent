from __future__ import annotations

import hashlib
import hmac
import json
import threading
import uuid
from collections.abc import Iterable, Iterator, Mapping
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    TimeoutError as FutureTimeout,
    wait as wait_futures,
)
from dataclasses import asdict, dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

from .observability import (
    RunEvent,
    RunEventBus,
    RunEventTerminalError,
    RunEventType,
    RunEventWriterRejected,
    RunStreamWriter,
    RunStreamWriterRegistry,
    SlowConsumerError,
    SubscriptionClosed,
)
from .observability.redaction import RedactionPolicy
from .runtime.cancellation import CancellationRequested, CancellationToken
from .runtime.context import CurrentUserInputTooLarge
from .runtime.lifecycle import LifecycleAdmission, LifecycleRejected, ShutdownReport
from .state import ContextCheckpointError, StateStorageError


@dataclass(frozen=True)
class Principal:
    actor_id: str
    tenant_id: str
    role: str


class Authenticator(Protocol):
    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        """Resolve immutable identity and scope from trusted credentials."""


class DemoTokenAuth:
    """Explicit local-demo auth; replace this adapter behind a trusted gateway."""

    def __init__(self, tokens: Mapping[str, Principal]):
        if not tokens:
            raise ValueError("at least one demo token is required")
        self._tokens = dict(tokens)

    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        value = headers.get("authorization", "")
        if not value.startswith("Bearer "):
            raise ApiError(401, "UNAUTHENTICATED", "Bearer token is required")
        supplied = value[7:]
        for token, principal in self._tokens.items():
            if hmac.compare_digest(supplied, token):
                return principal
        raise ApiError(401, "UNAUTHENTICATED", "invalid local demo token")


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable

    def payload(self, request_id: str | None) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
                "request_id": request_id,
            }
        }


@dataclass
class ApiResponse:
    status: int
    body: dict[str, Any] | list[Any] | Iterable[bytes]
    content_type: str = "application/json; charset=utf-8"
    headers: dict[str, str] | None = None


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")


def _serialize_result(result) -> dict[str, Any]:
    return asdict(result)


class EduAgentApi:
    """Thin owner-scoped API that delegates all runtime work to EduAgentService."""

    def __init__(
        self,
        service,
        *,
        authenticator: Authenticator,
        max_timeout_seconds: float = 300.0,
        redaction: RedactionPolicy | None = None,
        stream_buffer_size: int = 128,
        stream_keepalive_seconds: float = 0.25,
        stream_cleanup_seconds: float = 1.0,
        stream_write_timeout_seconds: float = 1.0,
    ):
        if stream_buffer_size <= 0:
            raise ValueError("stream_buffer_size must be positive")
        if (
            stream_keepalive_seconds <= 0
            or stream_cleanup_seconds <= 0
            or stream_write_timeout_seconds <= 0
        ):
            raise ValueError("stream keepalive/cleanup seconds must be positive")
        self.service = service
        self.authenticator = authenticator
        self.max_timeout_seconds = max_timeout_seconds
        self.redaction = redaction or RedactionPolicy()
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="edu-agent-api")
        self._run_events = RunEventBus(max_buffer_size=stream_buffer_size)
        self._stream_writers = RunStreamWriterRegistry(self._run_events)
        self._stream_buffer_size = int(stream_buffer_size)
        self._stream_keepalive_seconds = float(stream_keepalive_seconds)
        self._stream_cleanup_seconds = float(stream_cleanup_seconds)
        self._stream_write_timeout_seconds = float(stream_write_timeout_seconds)
        self._controls_lock = threading.Lock()
        self._controls: dict[str, CancellationToken] = {}
        self._lifecycle_lock = threading.Lock()
        self._futures: set[Future[Any]] = set()
        self._closing = False
        self._shutdown_report: ShutdownReport | None = None
        register_shutdown_hook = getattr(self.service, "register_shutdown_hook", None)
        if callable(register_shutdown_hook):
            register_shutdown_hook(
                f"api.transport:{id(self)}",
                self._close_transport,
            )

    def _close_transport(self) -> None:
        with self._lifecycle_lock:
            if self._closing:
                return
            self._closing = True
            futures = tuple(self._futures)
        with self._controls_lock:
            controls = tuple(self._controls.values())
            self._controls.clear()
        for token in controls:
            token.cancel("API is closing", source="shutdown")
        self._stream_writers.close()
        self._run_events.close()
        wait_futures(futures, timeout=self._stream_cleanup_seconds)
        self._pool.shutdown(wait=False, cancel_futures=True)

    def shutdown(self, *, deadline_seconds: float | None = None) -> ShutdownReport:
        if self._shutdown_report is None:
            self._shutdown_report = self.service.shutdown(
                deadline_seconds=deadline_seconds,
                reason="api_shutdown",
            )
        self._close_transport()
        return self._shutdown_report

    def close(self) -> ShutdownReport:
        return self.shutdown(deadline_seconds=self._stream_cleanup_seconds)

    def _submit(self, call, /, *args) -> Future[Any]:
        with self._lifecycle_lock:
            if self._closing:
                raise RuntimeError("API is closing")
            future = self._pool.submit(call, *args)
            self._futures.add(future)

        def release(completed: Future[Any]) -> None:
            with self._lifecycle_lock:
                self._futures.discard(completed)

        future.add_done_callback(release)
        return future

    def _register_control(self, run_id: str, token: CancellationToken) -> None:
        with self._controls_lock:
            current = self._controls.get(run_id)
            if current is not None and current is not token:
                current.cancel("superseded API attempt", source="owner_replaced")
            self._controls[run_id] = token

    def _release_control(self, run_id: str, token: CancellationToken) -> None:
        with self._controls_lock:
            if self._controls.get(run_id) is token:
                self._controls.pop(run_id, None)

    def _cancel_control(self, run_id: str, *, reason: str, source: str) -> bool:
        with self._controls_lock:
            token = self._controls.get(run_id)
        return token.cancel(reason, source=source) if token is not None else False

    @staticmethod
    def _request_hash(payload: dict[str, Any]) -> str:
        logical = {key: value for key, value in payload.items() if key not in {"stream", "timeout_seconds"}}
        canonical = json.dumps(logical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _role(principal: Principal, allowed: set[str]) -> None:
        if principal.role not in allowed:
            raise ApiError(403, "ROLE_DENIED", "role is not allowed for this endpoint")

    @staticmethod
    def _query_int(query: dict[str, list[str]], name: str, default: int) -> int:
        try:
            return int(query.get(name, [str(default)])[0])
        except ValueError as error:
            raise ApiError(400, "INVALID_ARGUMENT", f"{name} must be an integer") from error

    def _run_chat(
        self,
        payload: dict[str, Any],
        principal: Principal,
        request_id: str,
        run_id: str,
        owner_id: str,
        attempt: int,
        recovery_action: str = "execute",
        cancellation_token: CancellationToken | None = None,
        stream_writer: RunStreamWriter | None = None,
        lifecycle_admission: LifecycleAdmission | None = None,
    ) -> dict[str, Any]:
        try:
            if cancellation_token is not None:
                cancellation_token.checkpoint("api.run.before_service")
            if recovery_action == "resume":
                result = self.service.resume_api_run(
                    run_id,
                    actor_id=principal.actor_id,
                    tenant_id=principal.tenant_id,
                    cancellation_token=cancellation_token,
                    stream_writer=stream_writer,
                    lifecycle_admission=lifecycle_admission,
                )
            elif recovery_action == "recover_completed":
                terminal = self.service.get_run_status(
                    run_id,
                    actor_id=principal.actor_id,
                    tenant_id=principal.tenant_id,
                )
                if stream_writer is not None and terminal is not None:
                    self.service.bind_terminal_replay_stream(
                        stream_writer,
                        run_id,
                        actor_id=principal.actor_id,
                        tenant_id=principal.tenant_id,
                    )
                    stream_writer.publish(
                        RunEventType.RUN_PHASE,
                        {"phase": "accepted", "status": "replaying"},
                    )
                result = self.service.recover_chat_result(
                    run_id,
                    actor_id=principal.actor_id,
                    tenant_id=principal.tenant_id,
                )
            else:
                result = self.service.chat(
                    str(payload.get("message", "")),
                    actor_id=principal.actor_id,
                    tenant_id=principal.tenant_id,
                    role=principal.role,
                    course_ids={int(item) for item in payload.get("course_ids", [])},
                    session_id=payload.get("session_id"),
                    run_id=run_id,
                    cancellation_token=cancellation_token,
                    stream_writer=stream_writer,
                    lifecycle_admission=lifecycle_admission,
                )
            terminal = self.service.get_run_status(
                run_id,
                actor_id=principal.actor_id,
                tenant_id=principal.tenant_id,
            )
            if terminal is None or terminal.get("status") not in {
                "completed",
                "failed",
                "interrupted",
            }:
                raise RuntimeError("API request cannot complete before the run is terminal")
            response = self.redaction.redact(_serialize_result(result))
            record = self.service.finish_api_request(
                actor_id=principal.actor_id,
                tenant_id=principal.tenant_id,
                request_id=request_id,
                status="completed",
                run_id=run_id,
                response=response,
                owner_id=owner_id,
                attempt=attempt,
                response_status=200,
                response_content_type="application/json; charset=utf-8",
                retention_seconds=self._request_retention_seconds(success=True),
            )
            if stream_writer is not None and not stream_writer.terminal:
                stream_writer.complete(
                    {
                        "stop_reason": response.get("stop_reason") or "completed",
                        "response": record["response"],
                    }
                )
            return record["response"]
        except Exception as error:
            current_user_too_large = isinstance(error, CurrentUserInputTooLarge)
            error_code = (
                "CURRENT_USER_INPUT_TOO_LARGE"
                if current_user_too_large
                else getattr(error, "error_code", type(error).__name__)
            )
            response_status = (
                413 if current_user_too_large else 503 if isinstance(error, StateStorageError) else 500
            )
            if stream_writer is not None and not stream_writer.bound:
                try:
                    failed_run = self.service.get_run_status(
                        run_id,
                        actor_id=principal.actor_id,
                        tenant_id=principal.tenant_id,
                    )
                    if failed_run is not None and failed_run["status"] in {
                        "completed",
                        "failed",
                        "interrupted",
                    }:
                        self.service.bind_terminal_replay_stream(
                            stream_writer,
                            run_id,
                            actor_id=principal.actor_id,
                            tenant_id=principal.tenant_id,
                        )
                except (KeyError, PermissionError, RunEventWriterRejected):
                    pass
            try:
                terminal = self.service.get_run_status(
                    run_id,
                    actor_id=principal.actor_id,
                    tenant_id=principal.tenant_id,
                )
                if terminal and terminal.get("status") == "completed":
                    recovered = self.service.recover_chat_result(
                        run_id,
                        actor_id=principal.actor_id,
                        tenant_id=principal.tenant_id,
                    )
                    response = self.redaction.redact(_serialize_result(recovered))
                    record = self.service.finish_api_request(
                        actor_id=principal.actor_id,
                        tenant_id=principal.tenant_id,
                        request_id=request_id,
                        status="completed",
                        run_id=run_id,
                        response=response,
                        owner_id=owner_id,
                        attempt=attempt,
                        response_status=200,
                        response_content_type="application/json; charset=utf-8",
                        retention_seconds=self._request_retention_seconds(success=True),
                    )
                    if stream_writer is not None and not stream_writer.terminal:
                        stream_writer.complete(
                            {
                                "stop_reason": response.get("stop_reason") or "completed",
                                "response": record["response"],
                            }
                        )
                    return record["response"]
                self.service.finish_api_request(
                    actor_id=principal.actor_id,
                    tenant_id=principal.tenant_id,
                    request_id=request_id,
                    status="failed",
                    run_id=run_id,
                    error={"type": type(error).__name__, "code": error_code, "message": str(error)},
                    owner_id=owner_id,
                    attempt=attempt,
                    response_status=response_status,
                    retention_seconds=self._request_retention_seconds(success=False),
                )
            except RuntimeError:
                pass
            if stream_writer is not None and not stream_writer.terminal:
                try:
                    stream_writer.fail(
                        code=(
                            "CANCELLED"
                            if isinstance(error, CancellationRequested)
                            else error_code
                            if current_user_too_large
                            or str(error_code).startswith("CONTEXT_CHECKPOINT_")
                            else error_code
                            if isinstance(error, StateStorageError)
                            else "INTERNAL"
                        ),
                        message=f"{type(error).__name__}: {error}",
                    )
                except (RunEventTerminalError, RunEventWriterRejected):
                    pass
            raise
        finally:
            if cancellation_token is not None:
                self._release_control(run_id, cancellation_token)
                cancellation_token.close()
            if lifecycle_admission is not None:
                lifecycle_admission.close()

    def _claim_request(
        self, payload: dict[str, Any], principal: Principal, request_id: str
    ) -> tuple[dict, str, str, int]:
        owner_id = uuid.uuid4().hex
        run_id = uuid.uuid4().hex
        record = self.service.begin_api_request(
            actor_id=principal.actor_id,
            tenant_id=principal.tenant_id,
            request_id=request_id,
            request_hash=self._request_hash(payload),
            run_id=run_id,
            owner_id=owner_id,
            lease_seconds=self._request_lease_seconds(),
            retention_seconds=self._request_retention_seconds(success=True),
        )
        if record["status"] == "completed":
            return record, record["run_id"], owner_id, int(record.get("attempt") or 0)
        if record["status"] == "failed":
            raise ApiError(409, "REQUEST_FAILED", "the idempotent request previously failed")
        if record["status"] == "uncertain":
            raise ApiError(
                503,
                "REQUEST_UNCERTAIN",
                "the request has an uncertain write and requires manual review",
                retryable=True,
            )
        if record["status"] == "in_progress":
            raise ApiError(
                409,
                "REQUEST_IN_PROGRESS",
                "the idempotent request is in progress",
                retryable=True,
            )
        if record.get("recovery_action") == "wait":
            raise ApiError(409, "REQUEST_IN_PROGRESS", "the idempotent request is in progress", retryable=True)
        attempt = int(record.get("attempt") or 1)
        if not self.service.start_api_request(
            actor_id=principal.actor_id,
            tenant_id=principal.tenant_id,
            request_id=request_id,
            owner_id=owner_id,
            attempt=attempt,
        ):
            raise ApiError(409, "REQUEST_IN_PROGRESS", "the idempotent request is in progress", retryable=True)
        return record, record["run_id"] or run_id, owner_id, attempt

    def _request_lease_seconds(self) -> float:
        return float(getattr(getattr(self.service, "config", None), "api", None).request_lease_seconds)

    def _request_retention_seconds(self, *, success: bool) -> int:
        config = getattr(getattr(self.service, "config", None), "api", None)
        return int(
            config.request_retention_seconds
            if success
            else config.failed_request_retention_seconds
        )

    @staticmethod
    def _record_response(record: dict[str, Any], *, replay: bool = False) -> ApiResponse:
        headers = {str(key): str(value) for key, value in (record.get("response_headers") or {}).items()}
        if replay:
            headers["Idempotent-Replay"] = "true"
        return ApiResponse(
            int(record.get("response_status") or (200 if record["status"] == "completed" else 500)),
            record.get("response") if record.get("response") is not None else {
                "error": record.get("error") or {"code": "REQUEST_FAILED"}
            },
            content_type=record.get("response_content_type") or "application/json; charset=utf-8",
            headers=headers or None,
        )

    def _chat(self, payload: dict[str, Any], principal: Principal, request_id: str) -> ApiResponse:
        self._role(principal, {"student", "teacher", "admin"})
        if not str(payload.get("message", "")).strip():
            raise ApiError(400, "INVALID_ARGUMENT", "message is required")
        timeout = min(float(payload.get("timeout_seconds", self.max_timeout_seconds)), self.max_timeout_seconds)
        if timeout <= 0:
            raise ApiError(400, "INVALID_ARGUMENT", "timeout_seconds must be positive")
        admission = self.service.lifecycle.admit("api.chat")
        try:
            claim, run_id, owner_id, attempt = self._claim_request(
                payload,
                principal,
                request_id,
            )
            if claim["status"] == "completed":
                admission.close()
                return self._record_response(claim, replay=True)
            if payload.get("stream") is True:
                return ApiResponse(
                    200,
                    self._stream_chat(
                        payload,
                        principal,
                        request_id,
                        run_id,
                        owner_id,
                        attempt,
                        recovery_action=claim.get("recovery_action", "execute"),
                        lifecycle_admission=admission,
                    ),
                    content_type="text/event-stream; charset=utf-8",
                    headers={"Cache-Control": "no-cache"},
                )
            cancellation_token = CancellationToken.with_timeout(timeout)
            admission.add_cancel_callback(
                lambda: cancellation_token.cancel(
                    "process shutdown deadline exceeded",
                    source="process_shutdown",
                )
            )
            self._register_control(run_id, cancellation_token)
            recovery_action = claim.get("recovery_action", "execute")
            try:
                future = self._submit(
                    self._run_chat,
                    payload,
                    principal,
                    request_id,
                    run_id,
                    owner_id,
                    attempt,
                    recovery_action,
                    cancellation_token,
                    None,
                    admission,
                )
            except Exception:
                self._release_control(run_id, cancellation_token)
                cancellation_token.close()
                raise
        except Exception:
            admission.close()
            raise
        try:
            response = future.result(timeout=timeout)
        except FutureTimeout as error:
            cancellation_token.cancel("chat deadline exceeded", source="deadline")
            self.service.cancel_run(
                run_id, actor_id=principal.actor_id, tenant_id=principal.tenant_id
            )
            raise ApiError(
                504,
                "TIMEOUT",
                "chat exceeded its cooperative timeout",
                retryable=True,
            ) from error
        except Exception:
            # ``_run_chat`` is the single request-finalization owner. A second
            # completion attempt here could lose a recovered response body.
            raise
        return ApiResponse(200, response)

    @staticmethod
    def _sse_event(event: RunEvent) -> bytes:
        name = event.event_type.value
        if event.event_type is RunEventType.RUN_PHASE:
            phase = event.payload.get("phase")
            if phase == "accepted":
                name = "accepted"
        return (
            f"id: {event.sequence}\nevent: {name}\ndata: ".encode("utf-8")
            + _json_bytes(event.to_dict())
            + b"\n\n"
        )

    def _stream_chat(
        self,
        payload: dict[str, Any],
        principal: Principal,
        request_id: str,
        run_id: str,
        owner_id: str,
        attempt: int,
        recovery_action: str = "execute",
        lifecycle_admission: LifecycleAdmission | None = None,
    ) -> Iterator[bytes]:
        timeout = min(
            float(payload.get("timeout_seconds", self.max_timeout_seconds)),
            self.max_timeout_seconds,
        )
        cancellation_token = CancellationToken.with_timeout(timeout)
        if lifecycle_admission is not None:
            lifecycle_admission.add_cancel_callback(
                lambda: cancellation_token.cancel(
                    "process shutdown deadline exceeded",
                    source="process_shutdown",
                )
            )
        writer = self._stream_writers.open(
            run_id=run_id,
            attempt=attempt,
            writer_id=f"api:{owner_id}",
            cancellation_token=cancellation_token,
            sequence_reserver=lambda **fields: self.service.reserve_stream_event_sequence(
                actor_id=principal.actor_id,
                tenant_id=principal.tenant_id,
                **fields,
            ),
        )
        subscription = self._run_events.subscribe(
            run_id=run_id,
            attempt=attempt,
            buffer_size=self._stream_buffer_size,
        )
        self._register_control(run_id, cancellation_token)
        future = None
        terminal_seen = False
        try:
            future = self._submit(
                self._run_chat,
                payload,
                principal,
                request_id,
                run_id,
                owner_id,
                attempt,
                recovery_action,
                cancellation_token,
                writer,
                lifecycle_admission,
            )
            while not terminal_seen:
                if cancellation_token.cancelled:
                    cancellation = cancellation_token.cancellation
                    if cancellation is not None and cancellation.source == "deadline":
                        self.service.cancel_run(
                            run_id,
                            actor_id=principal.actor_id,
                            tenant_id=principal.tenant_id,
                        )
                try:
                    event = subscription.get(timeout=self._stream_keepalive_seconds)
                except TimeoutError:
                    if future.done() and not writer.bound:
                        future.result()
                    yield b": keepalive\n\n"
                    continue
                yield self._sse_event(event)
                terminal_seen = event.event_type in {
                    RunEventType.COMPLETED,
                    RunEventType.ERROR,
                }
            if future.done():
                try:
                    future.result()
                except Exception:
                    pass
        except GeneratorExit:
            cancellation_token.cancel(
                "client disconnected from SSE stream",
                source="client_disconnect",
            )
            raise
        except SlowConsumerError:
            cancellation_token.cancel(
                "SSE consumer exceeded the bounded event buffer",
                source="slow_consumer",
            )
        except SubscriptionClosed:
            pass
        except Exception as error:
            api_error = error if isinstance(error, ApiError) else ApiError(
                500, "INTERNAL", f"{type(error).__name__}: {error}"
            )
            if not terminal_seen:
                cancellation_token.cancel(api_error.message, source="writer_error")
        finally:
            subscription.cancel()
            if not terminal_seen:
                cancellation_token.cancel(
                    "SSE stream closed before a terminal event",
                    source="client_disconnect",
                )
                try:
                    self.service.cancel_run(
                        run_id, actor_id=principal.actor_id, tenant_id=principal.tenant_id
                    )
                except (KeyError, PermissionError):
                    pass
            if future is not None and not future.done():
                future.cancel()
                try:
                    future.result(timeout=self._stream_cleanup_seconds)
                except Exception:
                    pass
            self._release_control(run_id, cancellation_token)
            self._stream_writers.release(writer)
            cancellation_token.close()
            if lifecycle_admission is not None and (
                future is None or future.cancelled()
            ):
                lifecycle_admission.close()

    def _openapi(self) -> dict[str, Any]:
        return {
            "openapi": "3.1.0",
            "info": {"title": "EduAgent Local API", "version": "1.0.0"},
            "components": {
                "securitySchemes": {
                    "bearerAuth": {"type": "http", "scheme": "bearer"}
                }
            },
            "security": [{"bearerAuth": []}],
            "paths": {
                "/health/live": {
                    "get": {"summary": "Process liveness", "security": []}
                },
                "/health/ready": {
                    "get": {"summary": "Process readiness", "security": []}
                },
                "/v1/chat": {"post": {"summary": "Run one service chat turn"}},
                "/v1/runs/{run_id}": {"get": {"summary": "Read owner-scoped run status"}},
                "/v1/runs/{run_id}/cancel": {"post": {"summary": "Request cooperative cancellation"}},
                "/v1/runs/{run_id}/resume": {"post": {"summary": "Resume an abandoned run"}},
                "/v1/runs/{run_id}/plan": {"get": {"summary": "Read plan and evidence"}},
                "/v1/sessions/{session_id}": {"get": {"summary": "Read session lease status"}},
                "/v1/sessions/{session_id}/resume": {"post": {"summary": "Resume a run in this session"}},
                "/v1/artifacts/{artifact_id}": {"get": {"summary": "Read artifact metadata"}},
                "/v1/artifacts/{artifact_id}/content": {"get": {"summary": "Read bounded artifact content"}},
                "/v1/schedules": {"post": {"summary": "Create an idempotent schedule"}},
                "/v1/traces": {"get": {"summary": "Read a paginated redacted trace"}},
                "/v1/traces/export": {"get": {"summary": "Stream JSON or JSONL trajectory"}},
            },
        }

    def dispatch(
        self,
        method: str,
        target: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
    ) -> ApiResponse:
        normalized_headers = {str(key).lower(): str(value) for key, value in (headers or {}).items()}
        request_id = normalized_headers.get("x-request-id")
        try:
            parsed = urlsplit(target)
            if method == "GET" and parsed.path == "/openapi.json":
                return ApiResponse(200, self._openapi())
            if method == "GET" and parsed.path in {"/health/live", "/healthz"}:
                health = self.service.liveness_snapshot()
                return ApiResponse(
                    200 if health["live"] else 503,
                    {
                        "status": "ok" if health["live"] else "stopped",
                        "lifecycle": health["lifecycle"],
                        "live": health["live"],
                    },
                )
            if method == "GET" and parsed.path in {"/health/ready", "/readyz"}:
                health = self.service.health_snapshot()
                return ApiResponse(
                    200 if health["ready"] else 503,
                    {
                        "status": "ready" if health["ready"] else "not_ready",
                        **health,
                    },
                )
            principal = self.authenticator.authenticate(normalized_headers)
            query = parse_qs(parsed.query)
            payload = json.loads(body or b"{}")
            if not isinstance(payload, dict):
                raise ApiError(400, "INVALID_JSON", "request body must be a JSON object")
            parts = [part for part in parsed.path.split("/") if part]
            if method == "POST" and parts == ["v1", "chat"]:
                if not request_id:
                    raise ApiError(400, "REQUEST_ID_REQUIRED", "X-Request-ID is required")
                return self._chat(payload, principal, request_id)
            if len(parts) >= 3 and parts[:2] == ["v1", "runs"]:
                run_id = parts[2]
                if method == "GET" and len(parts) == 3:
                    record = self.service.get_run_status(
                        run_id, actor_id=principal.actor_id, tenant_id=principal.tenant_id
                    )
                    if record is None:
                        raise ApiError(404, "NOT_FOUND", "run not found")
                    return ApiResponse(200, record)
                if method == "POST" and parts[3:] == ["cancel"]:
                    requested = self.service.cancel_run(
                        run_id,
                        actor_id=principal.actor_id,
                        tenant_id=principal.tenant_id,
                    )
                    controlled = self._cancel_control(
                        run_id,
                        reason="run cancellation requested",
                        source="explicit",
                    )
                    return ApiResponse(
                        202,
                        {"cancel_requested": requested or controlled},
                    )
                if method == "POST" and parts[3:] == ["resume"]:
                    result = self.service.resume_run(
                        run_id, actor_id=principal.actor_id, tenant_id=principal.tenant_id
                    )
                    return ApiResponse(200, _serialize_result(result))
                if method == "GET" and parts[3:] == ["plan"]:
                    plan = self.service.get_plan(
                        run_id, actor_id=principal.actor_id, tenant_id=principal.tenant_id
                    )
                    if plan is None:
                        raise ApiError(404, "NOT_FOUND", "plan not found")
                    return ApiResponse(200, plan)
            if len(parts) >= 3 and parts[:2] == ["v1", "sessions"]:
                session_id = parts[2]
                if method == "GET" and len(parts) == 3:
                    status = self.service.get_session_status(
                        session_id, actor_id=principal.actor_id, tenant_id=principal.tenant_id
                    )
                    if status is None:
                        raise ApiError(404, "NOT_FOUND", "session not found")
                    return ApiResponse(200, status)
                if method == "POST" and parts[3:] == ["resume"]:
                    run_id = str(payload.get("run_id", ""))
                    if not run_id:
                        raise ApiError(400, "INVALID_ARGUMENT", "run_id is required")
                    result = self.service.resume_run(
                        run_id, actor_id=principal.actor_id, tenant_id=principal.tenant_id
                    )
                    if result.session_id != session_id:
                        raise ApiError(403, "SCOPE_DENIED", "run does not belong to session")
                    return ApiResponse(200, _serialize_result(result))
            if len(parts) >= 3 and parts[:2] == ["v1", "artifacts"]:
                artifact_id = parts[2]
                if method == "GET" and len(parts) == 3:
                    metadata = self.service.get_artifact_metadata(
                        artifact_id, actor_id=principal.actor_id, tenant_id=principal.tenant_id
                    )
                    if metadata is None:
                        raise ApiError(404, "NOT_FOUND", "artifact not found")
                    return ApiResponse(200, metadata)
                if method == "GET" and parts[3:] == ["content"]:
                    limit = self._query_int(query, "limit", 64 * 1024)
                    if limit <= 0 or limit > 1024 * 1024:
                        raise ApiError(400, "INVALID_ARGUMENT", "artifact limit must be 1..1048576")
                    offset = self._query_int(query, "offset", 0)
                    content, truncated = self.service.read_artifact_chunk(
                        artifact_id, actor_id=principal.actor_id,
                        tenant_id=principal.tenant_id, role=principal.role,
                        offset=offset, limit=limit,
                    )
                    return ApiResponse(200, {
                        "artifact_id": artifact_id,
                        "offset": offset,
                        "content": content,
                        "truncated": truncated,
                    })
            if method == "POST" and parts == ["v1", "schedules"]:
                self._role(principal, {"teacher", "admin"})
                job_id = self.service.schedule(
                    name=str(payload["name"]), prompt=str(payload["prompt"]),
                    actor_id=principal.actor_id, tenant_id=principal.tenant_id,
                    role=principal.role, next_run_at=datetime.fromisoformat(payload["next_run_at"]),
                    interval_seconds=payload.get("interval_seconds"),
                    max_attempts=int(payload.get("max_attempts", 3)),
                    retry_backoff_seconds=int(payload.get("retry_backoff_seconds", 60)),
                    idempotency_key=request_id,
                )
                return ApiResponse(201, self.service.get_scheduled_job(
                    job_id, actor_id=principal.actor_id, tenant_id=principal.tenant_id
                ))
            if method == "POST" and len(parts) == 4 and parts[:2] == ["v1", "schedules"] and parts[3] == "cancel":
                job = self.service.get_scheduled_job(
                    parts[2], actor_id=principal.actor_id, tenant_id=principal.tenant_id
                )
                if job is None:
                    raise ApiError(404, "NOT_FOUND", "scheduled job not found")
                return ApiResponse(202, {"cancel_requested": self.service.cancel_scheduled_job(
                    parts[2], actor_id=principal.actor_id, tenant_id=principal.tenant_id
                )})
            if method == "GET" and parts == ["v1", "traces", "export"]:
                export_format = query.get("format", ["jsonl"])[0]
                if export_format not in {"json", "jsonl"}:
                    raise ApiError(400, "INVALID_ARGUMENT", "format must be json or jsonl")
                trace_query = {
                    "actor_id": principal.actor_id,
                    "tenant_id": principal.tenant_id,
                    "run_id": query.get("run_id", [None])[0],
                    "session_id": query.get("session_id", [None])[0],
                    "status": query.get("status", [None])[0],
                    "error": query.get("error", [None])[0],
                    "tool": query.get("tool", [None])[0],
                    "provider": query.get("provider", [None])[0],
                    "component": query.get("component", [None])[0],
                }
                # Force owner-scope validation before returning a lazy stream;
                # otherwise an IDOR would surface only after HTTP headers.
                if trace_query["run_id"] or trace_query["session_id"]:
                    self.service.get_trace(**trace_query, cursor=0, limit=1)
                page_size = self._query_int(query, "limit", 100)
                repository = self.service.trace_repository
                chunks = (
                    chunk.encode("utf-8")
                    for chunk in repository.iter_export(
                        format=export_format, page_size=page_size, **trace_query
                    )
                )
                return ApiResponse(
                    200,
                    chunks,
                    content_type=(
                        "application/json; charset=utf-8"
                        if export_format == "json" else "application/x-ndjson; charset=utf-8"
                    ),
                )
            if method == "GET" and parts == ["v1", "traces"]:
                trace = self.service.get_trace(
                    actor_id=principal.actor_id, tenant_id=principal.tenant_id,
                    run_id=query.get("run_id", [None])[0],
                    session_id=query.get("session_id", [None])[0],
                    status=query.get("status", [None])[0],
                    error=query.get("error", [None])[0],
                    tool=query.get("tool", [None])[0],
                    provider=query.get("provider", [None])[0],
                    component=query.get("component", [None])[0],
                    cursor=query.get("cursor", [None])[0],
                    limit=self._query_int(query, "limit", 100),
                )
                return ApiResponse(200, trace)
            raise ApiError(404, "NOT_FOUND", "endpoint not found")
        except json.JSONDecodeError:
            response = ApiError(400, "INVALID_JSON", "malformed JSON").payload(request_id)
            return ApiResponse(400, response)
        except PermissionError as error:
            return ApiResponse(403, ApiError(403, "SCOPE_DENIED", str(error)).payload(request_id))
        except KeyError as error:
            return ApiResponse(404, ApiError(404, "NOT_FOUND", str(error)).payload(request_id))
        except CurrentUserInputTooLarge as error:
            return ApiResponse(
                413,
                ApiError(
                    413,
                    "CURRENT_USER_INPUT_TOO_LARGE",
                    str(error),
                ).payload(request_id),
            )
        except ContextCheckpointError as error:
            message = self.redaction.redact_text(str(error))
            return ApiResponse(
                500,
                ApiError(500, error.error_code, message).payload(request_id),
            )
        except LifecycleRejected as error:
            return ApiResponse(
                503,
                ApiError(
                    503,
                    error.error_code,
                    "process is not accepting new work",
                    retryable=True,
                ).payload(request_id),
                headers={"Retry-After": "1"},
            )
        except StateStorageError as error:
            return ApiResponse(
                503,
                ApiError(
                    503,
                    error.error_code,
                    str(error),
                    retryable=error.retryable,
                ).payload(request_id),
                headers={"Retry-After": "1"},
            )
        except ValueError as error:
            return ApiResponse(409, ApiError(409, "CONFLICT", str(error)).payload(request_id))
        except ApiError as error:
            return ApiResponse(error.status, error.payload(request_id))
        except Exception as error:
            safe = self.redaction.redact_text(f"{type(error).__name__}: {error}")
            return ApiResponse(500, ApiError(500, "INTERNAL", safe).payload(request_id))


def make_http_server(api: EduAgentApi, host: str = "127.0.0.1", port: int = 8080):
    class LifecycleHTTPServer(ThreadingHTTPServer):
        daemon_threads = True
        block_on_close = False

    class Handler(BaseHTTPRequestHandler):
        server_version = "EduAgentAPI/1.0"

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method: str):
            length = int(self.headers.get("Content-Length", "0"))
            response = api.dispatch(
                method,
                self.path,
                headers=dict(self.headers.items()),
                body=self.rfile.read(length),
            )
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            for name, value in (response.headers or {}).items():
                self.send_header(name, value)
            streaming = not isinstance(response.body, (dict, list))
            payload = None if streaming else _json_bytes(response.body)
            if payload is not None:
                self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if payload is not None:
                self.wfile.write(payload)
                return
            iterator = iter(response.body)
            self.connection.settimeout(api._stream_write_timeout_seconds)
            try:
                for chunk in iterator:
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                pass
            finally:
                close = getattr(iterator, "close", None)
                if callable(close):
                    close()

        def log_message(self, format: str, *args) -> None:
            return

    return LifecycleHTTPServer((host, port), Handler)
