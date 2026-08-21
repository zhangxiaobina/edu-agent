from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ExecutionRequest:
    language: str
    source: str
    stdin: str = ""
    args: tuple[str, ...] = ()
    cpu_time_limit_seconds: int = 2
    wall_time_limit_seconds: int = 5
    memory_limit_mb: int = 512
    output_limit_bytes: int = 64 * 1024
    process_limit: int = 16
    file_size_limit_mb: int = 16
    artifact_limit_bytes: int = 256 * 1024
    network_policy: str = "disabled"
    network_allowlist: tuple[str, ...] = ()
    tenant_id: str = "default"
    actor_id: str = "unknown"


@dataclass(frozen=True)
class ProviderCapabilities:
    provider: str
    languages: frozenset[str]
    trusted_isolation: bool
    supports_health_check: bool
    supports_wall_time: bool
    supports_cpu_time: bool
    supports_memory: bool
    supports_process_limit: bool
    supports_file_size_limit: bool
    supports_output_limit: bool
    supports_network_policy: bool
    supports_network_allowlist: bool
    supports_cancellation: bool
    supports_remote_artifacts: bool = False

    def satisfies(self, request: ExecutionRequest) -> tuple[bool, str | None]:
        if not self.trusted_isolation:
            return False, "PROVIDER_NOT_TRUSTED_ISOLATION"
        if request.language not in self.languages:
            return False, "LANGUAGE_NOT_ALLOWED"
        for supported, code in (
            (self.supports_wall_time, "WALL_TIME_UNSUPPORTED"),
            (self.supports_cpu_time, "CPU_LIMIT_UNSUPPORTED"),
            (self.supports_memory, "MEMORY_LIMIT_UNSUPPORTED"),
            (self.supports_process_limit, "PROCESS_LIMIT_UNSUPPORTED"),
            (self.supports_file_size_limit, "FILE_SIZE_LIMIT_UNSUPPORTED"),
            (self.supports_output_limit, "OUTPUT_LIMIT_UNSUPPORTED"),
        ):
            if not supported:
                return False, code
        if request.network_policy != "disabled":
            if not self.supports_network_policy:
                return False, "NETWORK_POLICY_UNSUPPORTED"
            if request.network_policy == "allowlist" and not self.supports_network_allowlist:
                return False, "NETWORK_ALLOWLIST_UNSUPPORTED"
        return True, None


@dataclass(frozen=True)
class ProviderHealth:
    healthy: bool
    checked_at: float
    message: str
    capabilities: ProviderCapabilities
    backend_languages: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    exit_status: int | None = None
    stdout: str = ""
    stderr: str = ""
    output_truncated: bool = False
    provider: str = "unknown"
    run_id: str | None = None
    raw_outcome: str | None = None
    message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.status == "success"

    def to_dict(self) -> dict[str, Any]:
        result = {
            "status": self.status,
            "success": self.success,
            "exit_status": self.exit_status,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "output_truncated": self.output_truncated,
            "provider": self.provider,
            "run_id": self.run_id,
            "raw_outcome": self.raw_outcome,
            "message": self.message,
            "metadata": self.metadata,
        }
        return {key: value for key, value in result.items() if value is not None}


class CodeExecutionProvider(Protocol):
    name: str

    def capabilities(self) -> ProviderCapabilities: ...

    def health_check(self, *, force: bool = False) -> ProviderHealth: ...

    def execute(self, request: ExecutionRequest, *, cancel_event=None) -> ExecutionResult: ...


def _bounded_text(value: Any, limit: int) -> tuple[str, bool]:
    text = value if isinstance(value, str) else str(value or "")
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


class JobeCodeExecutionProvider:
    """HTTP adapter for a pre-hardened Jobe/runguard deployment."""

    name = "jobe"
    _OUTCOMES = {
        "15": "success", "AC": "success", "11": "compile_error", "CE": "compile_error",
        "12": "runtime_error", "RE": "runtime_error", "13": "timeout", "TLE": "timeout",
        "17": "memory_limit", "MLE": "memory_limit", "19": "security_denied", "SE": "provider_error",
    }

    def __init__(
        self,
        endpoint: str,
        *,
        allowed_languages: tuple[str, ...] = ("python",),
        language_ids: dict[str, str] | None = None,
        request_timeout_seconds: float = 15.0,
        health_interval_seconds: float = 30.0,
        max_source_bytes: int = 64 * 1024,
        max_stdin_bytes: int = 64 * 1024,
        max_cpu_time_seconds: int = 10,
        max_wall_time_seconds: int = 15,
        min_memory_mb: int = 384,
        max_memory_mb: int = 1024,
        max_output_bytes: int = 128 * 1024,
        max_processes: int = 32,
        max_file_size_mb: int = 32,
        max_artifact_bytes: int = 256 * 1024,
        token_env: str = "EDU_AGENT_JOBE_TOKEN",
        security_attested: bool = False,
    ):
        self.endpoint = self._normalize_endpoint(endpoint)
        self.allowed_languages = frozenset(allowed_languages)
        self.language_ids = dict(language_ids or {"python": "python3"})
        self.request_timeout_seconds = float(request_timeout_seconds)
        self.health_interval_seconds = float(health_interval_seconds)
        self.max_source_bytes = int(max_source_bytes)
        self.max_stdin_bytes = int(max_stdin_bytes)
        self.max_cpu_time_seconds = int(max_cpu_time_seconds)
        self.max_wall_time_seconds = int(max_wall_time_seconds)
        self.min_memory_mb = int(min_memory_mb)
        self.max_memory_mb = int(max_memory_mb)
        self.max_output_bytes = int(max_output_bytes)
        self.max_processes = int(max_processes)
        self.max_file_size_mb = int(max_file_size_mb)
        self.max_artifact_bytes = int(max_artifact_bytes)
        self.token_env = token_env
        self.security_attested = bool(security_attested)
        self._health: ProviderHealth | None = None

    @staticmethod
    def _normalize_endpoint(endpoint: str) -> str:
        parsed = urllib.parse.urlsplit(str(endpoint).strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("code_execution.endpoint 必须是 http(s) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("code_execution.endpoint 不得包含凭据、query 或 fragment")
        path = parsed.path.rstrip("/")
        if not path.endswith("/jobe/index.php/restapi"):
            path = f"{path}/jobe/index.php/restapi" if path else "/jobe/index.php/restapi"
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name, languages=self.allowed_languages,
            trusted_isolation=self.security_attested,
            supports_health_check=True, supports_wall_time=True, supports_cpu_time=True,
            supports_memory=True, supports_process_limit=True, supports_file_size_limit=True,
            supports_output_limit=True, supports_network_policy=True,
            supports_network_allowlist=False, supports_cancellation=False,
        )

    def _request(self, method: str, path: str, payload: dict | None = None):
        headers = {"Accept": "application/json"}
        token = os.getenv(self.token_env, "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.endpoint}/{path.lstrip('/')}", data=body, headers=headers, method=method,
        )
        with urllib.request.urlopen(request, timeout=self.request_timeout_seconds) as response:
            raw = response.read(self.max_output_bytes * 4 + 1)
            if len(raw) > self.max_output_bytes * 4:
                raise ValueError("Jobe 响应超过 provider 响应预算")
            return json.loads(raw.decode("utf-8"))

    @staticmethod
    def _language_names(payload: Any) -> tuple[str, ...]:
        if isinstance(payload, dict):
            payload = payload.get("languages", payload.get("data", []))
        names = []
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, (list, tuple)) and item:
                    names.append(str(item[0]))
                elif isinstance(item, dict):
                    value = item.get("id") or item.get("language_id") or item.get("name")
                    if value:
                        names.append(str(value))
                elif isinstance(item, str):
                    names.append(item)
        return tuple(sorted(set(names)))

    def health_check(self, *, force: bool = False) -> ProviderHealth:
        now = time.monotonic()
        if not force and self._health and now - self._health.checked_at < self.health_interval_seconds:
            return self._health
        try:
            languages = self._language_names(self._request("GET", "languages"))
            required = {self.language_ids[name] for name in self.allowed_languages}
            missing = sorted(required - set(languages))
            healthy = not missing
            message = "healthy" if healthy else f"Jobe 缺少允许语言: {missing}"
            if healthy:
                smoke = self._request(
                    "POST",
                    "runs/",
                    {
                        "run_spec": {
                            "language_id": self.language_ids["python"],
                            "sourcecode": "print('EDU_AGENT_JOBE_HEALTHY')",
                            "parameters": {
                                "cputime": 1,
                                "memorylimit": self.min_memory_mb,
                                "numprocs": 2,
                                "disklimit": 2,
                                "streamsize": 2,
                            },
                        }
                    },
                )
                healthy = (
                    str(smoke.get("outcome")) in {"15", "AC"}
                    and str(smoke.get("stdout", "")).strip() == "EDU_AGENT_JOBE_HEALTHY"
                )
                if not healthy:
                    message = "Jobe smoke run failed"
            self._health = ProviderHealth(
                healthy=healthy, checked_at=now, message=message,
                capabilities=self.capabilities(), backend_languages=languages,
            )
        except (OSError, ValueError, json.JSONDecodeError, socket.timeout, urllib.error.URLError) as error:
            self._health = ProviderHealth(
                healthy=False, checked_at=now,
                message=f"Jobe health check failed: {type(error).__name__}",
                capabilities=self.capabilities(),
            )
        return self._health

    def _validate_request(self, request: ExecutionRequest) -> tuple[bool, str | None]:
        if len(request.source.encode("utf-8")) > self.max_source_bytes:
            return False, "SOURCE_LIMIT_EXCEEDED"
        if len(request.stdin.encode("utf-8")) > self.max_stdin_bytes:
            return False, "STDIN_LIMIT_EXCEEDED"
        if request.args:
            return False, "ARGS_UNSUPPORTED"
        if request.network_policy != "disabled" or request.network_allowlist:
            return False, "NETWORK_POLICY_DENIED"
        for value, limit, code in (
            (request.cpu_time_limit_seconds, self.max_cpu_time_seconds, "CPU_LIMIT_EXCEEDED"),
            (request.wall_time_limit_seconds, self.max_wall_time_seconds, "WALL_LIMIT_EXCEEDED"),
            (request.memory_limit_mb, self.max_memory_mb, "MEMORY_LIMIT_EXCEEDED"),
            (request.output_limit_bytes, self.max_output_bytes, "OUTPUT_LIMIT_EXCEEDED"),
            (request.process_limit, self.max_processes, "PROCESS_LIMIT_EXCEEDED"),
            (request.file_size_limit_mb, self.max_file_size_mb, "FILE_SIZE_LIMIT_EXCEEDED"),
            (request.artifact_limit_bytes, self.max_artifact_bytes, "ARTIFACT_LIMIT_EXCEEDED"),
        ):
            if value <= 0 or value > limit:
                return False, code
        if request.memory_limit_mb < self.min_memory_mb:
            return False, "MEMORY_LIMIT_BELOW_BACKEND_MINIMUM"
        return self.capabilities().satisfies(request)

    def execute(self, request: ExecutionRequest, *, cancel_event=None) -> ExecutionResult:
        health = self.health_check()
        if not health.healthy:
            return ExecutionResult(status="provider_unavailable", provider=self.name, message=health.message)
        if cancel_event is not None and cancel_event.is_set():
            return ExecutionResult(status="cancelled", provider=self.name, message="执行已取消")
        valid, reason = self._validate_request(request)
        if not valid:
            return ExecutionResult(status="security_denied", provider=self.name, message=reason)
        # These are the native Jobe RunSpecifier parameters.  The teaching
        # platform wrapper accepts a different DTO, but this provider targets
        # Jobe's documented REST endpoint directly so resource limits cannot
        # silently disappear in an adapter layer.
        stream_size_mb = max(2, (request.output_limit_bytes + 999_999) // 1_000_000)
        payload = {"run_spec": {
            "language_id": self.language_ids[request.language],
            "sourcefilename": "Main.py" if request.language == "python" else "Main.txt",
            "sourcecode": request.source, "input": request.stdin,
            "parameters": {
                "cputime": request.cpu_time_limit_seconds,
                "memorylimit": request.memory_limit_mb,
                "numprocs": request.process_limit,
                "disklimit": request.file_size_limit_mb,
                "streamsize": stream_size_mb,
            },
        }}
        try:
            response = self._request("POST", "runs/", payload)
        except (OSError, ValueError, json.JSONDecodeError, socket.timeout, urllib.error.URLError) as error:
            return ExecutionResult(status="provider_error", provider=self.name,
                                   message=f"Jobe execute failed: {type(error).__name__}")
        if cancel_event is not None and cancel_event.is_set():
            return ExecutionResult(status="cancelled", provider=self.name, message="执行已取消")
        raw_outcome = str(response.get("outcome", "SE")) if isinstance(response, dict) else "SE"
        status = self._OUTCOMES.get(raw_outcome, "provider_error")
        stdout, out_truncated = _bounded_text(response.get("stdout", ""), request.output_limit_bytes)
        stderr, err_truncated = _bounded_text(response.get("stderr", ""), request.output_limit_bytes)
        if not stderr and isinstance(response, dict) and response.get("cmpinfo"):
            stderr = str(response["cmpinfo"])
        if status == "runtime_error" and "memoryerror" in stderr.lower():
            status = "memory_limit"
        if len(stdout.encode("utf-8")) + len(stderr.encode("utf-8")) > request.artifact_limit_bytes:
            status = "output_limit"
            stderr = "执行输出超过总 Artifact 预算"
            out_truncated = True
        return ExecutionResult(
            status=status, exit_status=0 if status == "success" else None,
            stdout=stdout, stderr=stderr, output_truncated=out_truncated or err_truncated,
            provider=self.name, raw_outcome=raw_outcome,
            run_id=str(response.get("run_id")) if isinstance(response, dict) and response.get("run_id") else None,
            metadata={"backend_outcome": raw_outcome, "stderr_truncated": err_truncated},
        )
