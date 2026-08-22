"""Offline R1 Provider Gateway acceptance against a loopback fake HTTP server."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import openai

from edu_agent.engine import (
    ApiMode,
    ChatCompletionsAdapter,
    CredentialRef,
    FailureKind,
    GatewayEngine,
    ProviderCapabilities,
    ProviderGateway,
    ProviderSpec,
    ResilientEngine,
    ResponsesAdapter,
    RouteStateRegistry,
    classify_failure,
)
from edu_agent.observability import TraceRepository
from edu_agent.runtime.models import RunContext
from edu_agent.state import StateStore


ACTOR_ID = "r1-fake-acceptance"
PRIMARY_ENV = "R1_FAKE_PRIMARY_CREDENTIAL"
FALLBACK_ENV = "R1_FAKE_FALLBACK_CREDENTIAL"
PRIMARY_KEY = "primary-fake-canary-492175"
FALLBACK_KEY = "fallback-fake-canary-735804"
CAPABILITIES = ProviderCapabilities(
    streaming=True,
    context_window_tokens=16_384,
)


class _FakeProviderState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counts: Counter[str] = Counter()

    def hit(self, path: str) -> int:
        with self._lock:
            self.counts[path] += 1
            return self.counts[path]


def _chat_tool_response(model: str, *, content: str = "Checking both.") -> dict:
    return {
        "id": "chatcmpl-r1-fake",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "id": "call-a",
                            "type": "function",
                            "function": {"name": "list_exams", "arguments": "{}"},
                        },
                        {
                            "id": "call-b",
                            "type": "function",
                            "function": {
                                "name": "query_student_scores",
                                "arguments": "{\"exam_id\":17}",
                            },
                        },
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13},
    }


def _responses_tool_response(model: str, *, content: str = "Checking both.") -> dict:
    return {
        "id": "resp-r1-fake",
        "object": "response",
        "created_at": 1.0,
        "model": model,
        "output": [
            {
                "id": "msg-r1-fake",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": content, "annotations": []}
                ],
            },
            {
                "id": "fc-a",
                "type": "function_call",
                "call_id": "call-a",
                "name": "list_exams",
                "arguments": "{}",
                "status": "completed",
            },
            {
                "id": "fc-b",
                "type": "function_call",
                "call_id": "call-b",
                "name": "query_student_scores",
                "arguments": "{\"exam_id\":17}",
                "status": "completed",
            },
        ],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "status": "completed",
        "usage": {"input_tokens": 9, "output_tokens": 4, "total_tokens": 13},
    }


def _chat_text_response(
    model: str,
    content: str,
    *,
    finish_reason: str = "stop",
) -> dict:
    return {
        "id": "chatcmpl-r1-text",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }


def _handler_type(state: _FakeProviderState):
    class FakeProviderHandler(BaseHTTPRequestHandler):
        server_version = "EduAgentR1Fake/1"

        def log_message(self, _format: str, *args) -> None:
            return

        def _json(self, status: int, payload: dict, *, headers: dict | None = None) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP handler contract
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            try:
                request = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                self._json(400, {"error": {"message": "invalid json"}})
                return
            count = state.hit(self.path)
            model = str(request.get("model") or "r1-fake-model")

            if self.path == "/equiv-chat/v1/chat/completions":
                self._json(200, _chat_tool_response(model))
            elif self.path == "/equiv-responses/v1/responses":
                self._json(200, _responses_tool_response(model))
            elif self.path == "/retry/v1/chat/completions" and count == 1:
                self._json(
                    429,
                    {
                        "error": {
                            "message": "fake rate limit",
                            "type": "rate_limit_error",
                            "code": "rate_limit_exceeded",
                        }
                    },
                    headers={"Retry-After": "7"},
                )
            elif self.path == "/retry/v1/chat/completions":
                self._json(200, _chat_text_response(model, "retry-ok"))
            elif self.path in {
                "/down/v1/chat/completions",
                "/fallback-primary/v1/chat/completions",
            }:
                self._json(
                    503,
                    {
                        "error": {
                            "message": "fake unavailable",
                            "type": "server_error",
                            "code": "server_error",
                        }
                    },
                )
            elif self.path == "/healthy/v1/chat/completions":
                self._json(200, _chat_text_response(model, "healthy-ok"))
            elif self.path == "/fallback-responses/v1/responses":
                self._json(200, _responses_tool_response(model, content="fallback-ok"))
            elif self.path == "/terminal-401/v1/chat/completions":
                self._json(401, {"error": {"message": "fake auth", "code": "invalid_api_key"}})
            elif self.path == "/terminal-403/v1/chat/completions":
                self._json(403, {"error": {"message": "fake denied", "code": "permission_denied"}})
            elif self.path == "/terminal-400/v1/chat/completions":
                self._json(400, {"error": {"message": "fake invalid", "code": "invalid_request"}})
            elif self.path == "/terminal-context/v1/chat/completions":
                self._json(
                    400,
                    {"error": {"message": "fake context", "code": "context_length_exceeded"}},
                )
            elif self.path == "/terminal-output/v1/chat/completions":
                self._json(
                    200,
                    _chat_text_response(model, "partial", finish_reason="length"),
                )
            elif self.path.startswith("/never-"):
                self._json(200, _chat_text_response(model, "must-not-run"))
            else:
                self._json(404, {"error": {"message": "unknown fake route"}})

    return FakeProviderHandler


def _gateway_engine(
    base_url: str,
    path: str,
    model: str,
    mode: ApiMode,
    credential_env: str,
    *,
    capabilities: ProviderCapabilities = CAPABILITIES,
    adapter_capabilities: ProviderCapabilities | None = None,
) -> GatewayEngine:
    endpoint = f"{base_url}/{path}/v1"
    client = openai.OpenAI(
        base_url=endpoint,
        api_key=os.environ[credential_env],
        max_retries=0,
        timeout=2,
        http_client=httpx.Client(trust_env=False),
    )
    chat_adapter = ChatCompletionsAdapter(client, timeout=2)
    responses_adapter = ResponsesAdapter(client, timeout=2)
    if adapter_capabilities is not None:
        chat_adapter.capabilities = adapter_capabilities
        responses_adapter.capabilities = adapter_capabilities
    gateway = ProviderGateway(
        adapters={
            ApiMode.CHAT_COMPLETIONS: chat_adapter,
            ApiMode.RESPONSES: responses_adapter,
        }
    )
    return GatewayEngine(
        gateway,
        ProviderSpec(
            model=model,
            endpoint=endpoint,
            api_mode=mode,
            credential=CredentialRef(credential_env),
            capabilities=capabilities,
        ),
    )


def _calls(response) -> list[tuple[str, str, str | dict]]:
    return [(call.id, call.name, call.arguments) for call in response.tool_calls]


def _prepare_run(store: StateStore, run_id: str) -> None:
    context = RunContext.create(
        session_id=f"session-{run_id}",
        run_id=run_id,
        actor_id=ACTOR_ID,
        role="teacher",
    )
    store.ensure_session(
        context.session_id,
        actor_id=ACTOR_ID,
        tenant_id="default",
        role="teacher",
        course_ids=set(),
    )
    store.enqueue_run(context, request_text="synthetic R1 fake request")
    store.start_run(
        run_id=run_id,
        session_id=context.session_id,
        model="r1-fake",
        context_tokens=1,
        omitted_messages=0,
    )


def _audited_chat(
    engine: ResilientEngine,
    run_id: str,
    messages: list[dict],
    tools: list[dict],
):
    with engine.runtime_context(run_id):
        return engine.chat(messages, tools)


def run_acceptance() -> dict:
    state = _FakeProviderState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_type(state))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    previous_primary = os.environ.get(PRIMARY_ENV)
    previous_fallback = os.environ.get(FALLBACK_ENV)
    os.environ[PRIMARY_ENV] = PRIMARY_KEY
    os.environ[FALLBACK_ENV] = FALLBACK_KEY
    try:
        with tempfile.TemporaryDirectory(prefix="edu-agent-r1-fake-") as directory:
            store = StateStore(Path(directory) / "state.db")
            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "list_exams",
                        "description": "List exams",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ]
            messages = [{"role": "user", "content": "synthetic request"}]

            chat = _gateway_engine(
                base_url,
                "equiv-chat",
                "equivalent-model",
                ApiMode.CHAT_COMPLETIONS,
                PRIMARY_ENV,
            ).chat(messages, tools)
            responses = _gateway_engine(
                base_url,
                "equiv-responses",
                "equivalent-model",
                ApiMode.RESPONSES,
                PRIMARY_ENV,
            ).chat(messages, tools)
            assert _calls(chat) == _calls(responses)
            assert chat.content == responses.content == "Checking both."

            retry_run = "r1-retry-after"
            _prepare_run(store, retry_run)
            sleeps: list[float] = []
            retry_engine = ResilientEngine(
                _gateway_engine(
                    base_url,
                    "retry",
                    "retry-model",
                    ApiMode.CHAT_COMPLETIONS,
                    PRIMARY_ENV,
                ),
                max_retries=1,
                failure_threshold=3,
                sleeper=sleeps.append,
                random_source=lambda: (_ for _ in ()).throw(
                    AssertionError("Retry-After must bypass jitter")
                ),
                event_sink=lambda event: store.record_provider_event(**event),
            )
            retry_response = _audited_chat(retry_engine, retry_run, messages, [])
            assert retry_response.content == "retry-ok"
            assert sleeps == [7]

            registry = RouteStateRegistry(
                failure_threshold=1,
                cooldown_seconds=30,
                idle_ttl_seconds=60,
            )
            down = ResilientEngine(
                _gateway_engine(
                    base_url,
                    "down",
                    "shared-model",
                    ApiMode.CHAT_COMPLETIONS,
                    PRIMARY_ENV,
                ),
                max_retries=0,
                route_registry=registry,
            )
            healthy = ResilientEngine(
                _gateway_engine(
                    base_url,
                    "healthy",
                    "shared-model",
                    ApiMode.CHAT_COMPLETIONS,
                    PRIMARY_ENV,
                ),
                max_retries=0,
                route_registry=registry,
            )
            try:
                down.chat(messages, [])
            except Exception as error:
                assert classify_failure(error).kind is FailureKind.SERVER
            else:  # pragma: no cover - acceptance assertion
                raise AssertionError("down route unexpectedly succeeded")
            assert down.breaker.state == "open"
            assert healthy.chat(messages, []).content == "healthy-ok"
            assert healthy.breaker.state == "closed"

            fallback_run = "r1-compatible-fallback"
            _prepare_run(store, fallback_run)
            compatible = ResilientEngine(
                _gateway_engine(
                    base_url,
                    "fallback-primary",
                    "primary-model",
                    ApiMode.CHAT_COMPLETIONS,
                    PRIMARY_ENV,
                ),
                fallback=_gateway_engine(
                    base_url,
                    "fallback-responses",
                    "fallback-model",
                    ApiMode.RESPONSES,
                    FALLBACK_ENV,
                ),
                max_retries=0,
                event_sink=lambda event: store.record_provider_event(**event),
            )
            fallback_response = _audited_chat(
                compatible,
                fallback_run,
                messages,
                tools,
            )
            assert fallback_response.content == "fallback-ok"
            assert fallback_response.usage["fallback_used"] is True
            assert fallback_response.usage["primary_failure"] == "server"

            denied: dict[str, str] = {}
            incompatibilities = (
                (
                    "tool",
                    messages,
                    tools,
                    ProviderCapabilities(
                        tool_calling=False,
                        context_window_tokens=16_384,
                    ),
                    ApiMode.CHAT_COMPLETIONS,
                    "tool_calling",
                ),
                (
                    "context",
                    messages,
                    [],
                    ProviderCapabilities(context_window_tokens=1),
                    ApiMode.CHAT_COMPLETIONS,
                    "context_window",
                ),
                (
                    "structured",
                    messages,
                    [
                        {
                            "type": "function",
                            "function": {
                                "name": "list_exams",
                                "strict": True,
                                "parameters": {"type": "object", "properties": {}},
                            },
                        }
                    ],
                    ProviderCapabilities(
                        structured_output=False,
                        context_window_tokens=16_384,
                    ),
                    ApiMode.CHAT_COMPLETIONS,
                    "structured_output",
                ),
                (
                    "api-mode",
                    [{"role": "user", "content": [{"type": "text", "text": "x"}]}],
                    [],
                    CAPABILITIES,
                    ApiMode.RESPONSES,
                    "api_mode_request_shape",
                ),
            )
            for name, case_messages, case_tools, capabilities, mode, expected_gap in incompatibilities:
                run_id = f"r1-incompatible-{name}"
                _prepare_run(store, run_id)
                candidate_path = f"never-{name}"
                primary_capabilities = (
                    ProviderCapabilities(
                        structured_output=True,
                        streaming=True,
                        context_window_tokens=16_384,
                    )
                    if name == "structured"
                    else CAPABILITIES
                )
                engine = ResilientEngine(
                    _gateway_engine(
                        base_url,
                        "fallback-primary",
                        f"primary-{name}",
                        ApiMode.CHAT_COMPLETIONS,
                        PRIMARY_ENV,
                        capabilities=primary_capabilities,
                        adapter_capabilities=primary_capabilities,
                    ),
                    fallback=_gateway_engine(
                        base_url,
                        candidate_path,
                        f"fallback-{name}",
                        mode,
                        FALLBACK_ENV,
                        capabilities=capabilities,
                    ),
                    max_retries=0,
                    event_sink=lambda event: store.record_provider_event(**event),
                )
                try:
                    _audited_chat(engine, run_id, case_messages, case_tools)
                except Exception as error:
                    assert classify_failure(error).kind is FailureKind.SERVER
                else:  # pragma: no cover - acceptance assertion
                    raise AssertionError(f"incompatible fallback {name} unexpectedly ran")
                assert not any(path.startswith(f"/{candidate_path}/") for path in state.counts)
                with store.connect() as connection:
                    row = connection.execute(
                        """
                        SELECT details_json FROM provider_events
                        WHERE run_id=? AND event='fallback_rejected'
                        ORDER BY id DESC LIMIT 1
                        """,
                        (run_id,),
                    ).fetchone()
                details = json.loads(row["details_json"])
                assert expected_gap in details["compatibility"]["gaps"]
                denied[name] = expected_gap

            terminal_kinds = {
                "terminal-401": FailureKind.AUTHENTICATION,
                "terminal-403": FailureKind.PERMISSION,
                "terminal-400": FailureKind.INVALID_REQUEST,
                "terminal-context": FailureKind.CONTEXT_OVERFLOW,
            }
            for path, expected_kind in terminal_kinds.items():
                run_id = f"r1-{path}"
                _prepare_run(store, run_id)
                never_path = f"never-{path}"
                engine = ResilientEngine(
                    _gateway_engine(
                        base_url,
                        path,
                        f"primary-{path}",
                        ApiMode.CHAT_COMPLETIONS,
                        PRIMARY_ENV,
                    ),
                    fallback=_gateway_engine(
                        base_url,
                        never_path,
                        f"fallback-{path}",
                        ApiMode.CHAT_COMPLETIONS,
                        FALLBACK_ENV,
                    ),
                    max_retries=0,
                    event_sink=lambda event: store.record_provider_event(**event),
                )
                try:
                    _audited_chat(engine, run_id, messages, [])
                except Exception as error:
                    assert classify_failure(error).kind is expected_kind
                else:  # pragma: no cover - acceptance assertion
                    raise AssertionError(f"terminal failure {path} unexpectedly succeeded")
                assert not any(path.startswith(f"/{never_path}/") for path in state.counts)

            output_engine = ResilientEngine(
                _gateway_engine(
                    base_url,
                    "terminal-output",
                    "primary-output",
                    ApiMode.CHAT_COMPLETIONS,
                    PRIMARY_ENV,
                ),
                fallback=_gateway_engine(
                    base_url,
                    "never-terminal-output",
                    "fallback-output",
                    ApiMode.CHAT_COMPLETIONS,
                    FALLBACK_ENV,
                ),
                max_retries=0,
            )
            output_response = output_engine.chat(messages, [])
            assert output_response.finish_reason == "length"
            assert state.counts["/never-terminal-output/v1/chat/completions"] == 0

            trace = TraceRepository(store).list_events(
                actor_id=ACTOR_ID,
                limit=500,
            ).to_dict()
            rendered_trace = json.dumps(trace, ensure_ascii=False)
            with store.connect() as connection:
                raw_events = connection.execute(
                    "SELECT details_json FROM provider_events ORDER BY id"
                ).fetchall()
            rendered_raw = "\n".join(row["details_json"] for row in raw_events)
            for secret in (PRIMARY_KEY, FALLBACK_KEY, PRIMARY_ENV, FALLBACK_ENV):
                assert secret not in rendered_trace
                assert secret not in rendered_raw
            attempts = [
                event
                for event in trace["events"]
                if event["component"] == "provider"
                and event["attributes"]["event"] == "provider_attempt"
            ]
            assert attempts
            assert all(
                event["attributes"]["details"].get("route")
                and event["attributes"]["details"].get("attempt_sequence")
                for event in attempts
            )
            switches = [
                event
                for event in trace["events"]
                if event["component"] == "provider"
                and event["attributes"]["event"] == "fallback_activated"
            ]
            assert len(switches) == 1
            assert switches[0]["attributes"]["details"]["compatibility"]["compatible"] is True

            return {
                "gate": "passed",
                "api_modes": [mode.value for mode in ApiMode],
                "equivalent_tool_calls": len(_calls(chat)),
                "retry_after_seconds": sleeps[0],
                "route_breaker_isolated": True,
                "compatible_fallback": True,
                "incompatible_fallback_gaps": denied,
                "terminal_fallback_blocked": sorted(terminal_kinds),
                "attempt_events": len(attempts),
                "trace_redacted": True,
            }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        if previous_primary is None:
            os.environ.pop(PRIMARY_ENV, None)
        else:
            os.environ[PRIMARY_ENV] = previous_primary
        if previous_fallback is None:
            os.environ.pop(FALLBACK_ENV, None)
        else:
            os.environ[FALLBACK_ENV] = previous_fallback


def main() -> int:
    print(json.dumps(run_acceptance(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
