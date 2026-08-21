from __future__ import annotations

import base64
import http.client
import json
import re
import socket
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

from .provider import (
    ExecutionRequest,
    ExecutionResult,
    ProviderCapabilities,
    ProviderHealth,
    _bounded_text,
)


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        connection.connect(self.socket_path)
        self.sock = connection


class DockerEngineError(RuntimeError):
    pass


class DockerCodeExecutionProvider:
    """Ephemeral hardened containers through the Docker Engine HTTP API.

    The caller cannot select images, mounts, commands, privileges, users, or
    networking.  This is intentionally not a shell wrapper around ``docker
    run``: every security-relevant container field is constructed here and is
    asserted by the E2E suite against the Engine's inspect response.
    """

    name = "docker"
    _WRAPPER = """import base64, io, json, os, resource, sys, traceback
source = base64.b64decode(sys.argv[1]).decode('utf-8')
stdin_text = base64.b64decode(sys.argv[2]).decode('utf-8')
run_args = json.loads(base64.b64decode(sys.argv[3]).decode('utf-8'))
cpu_seconds = int(sys.argv[4])
file_bytes = int(sys.argv[5])
output_bytes = int(sys.argv[6])
resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
sys.argv = ['Main.py', *run_args]
sys.stdin = io.StringIO(stdin_text)
saved_stdout, saved_stderr = os.dup(1), os.dup(2)
stdout_file = open('/tmp/stdout', 'w+b', buffering=0)
stderr_file = open('/tmp/stderr', 'w+b', buffering=0)
os.dup2(stdout_file.fileno(), 1)
os.dup2(stderr_file.fileno(), 2)
sys.stdout = io.TextIOWrapper(os.fdopen(os.dup(1), 'wb', buffering=0), encoding='utf-8')
sys.stderr = io.TextIOWrapper(os.fdopen(os.dup(2), 'wb', buffering=0), encoding='utf-8')
exit_code = 0
try:
    exec(compile(source, 'Main.py', 'exec'), {'__name__': '__main__'})
except SystemExit as error:
    exit_code = error.code if isinstance(error.code, int) else 1
except BaseException:
    exit_code = 1
    traceback.print_exc()
finally:
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except BaseException:
        exit_code = 1
    os.dup2(saved_stdout, 1)
    os.dup2(saved_stderr, 2)
    for output, destination in ((stdout_file, 1), (stderr_file, 2)):
        output.seek(0)
        data = output.read(output_bytes + 1)
        os.write(destination, data[:output_bytes])
        if len(data) > output_bytes:
            exit_code = 120
sys.exit(exit_code)
"""

    def __init__(
        self,
        image: str,
        *,
        socket_path: str = "~/.docker/run/docker.sock",
        python_path: str = "/usr/bin/python3",
        allowed_languages: tuple[str, ...] = ("python",),
        request_timeout_seconds: float = 10.0,
        health_interval_seconds: float = 30.0,
        max_source_bytes: int = 64 * 1024,
        max_stdin_bytes: int = 64 * 1024,
        max_cpu_time_seconds: int = 10,
        max_wall_time_seconds: int = 15,
        min_memory_mb: int = 128,
        max_memory_mb: int = 1024,
        max_output_bytes: int = 128 * 1024,
        max_processes: int = 32,
        max_file_size_mb: int = 32,
        max_artifact_bytes: int = 256 * 1024,
        max_cpus: float = 1.0,
        security_attested: bool = False,
    ):
        if re.fullmatch(r".+@sha256:[0-9a-fA-F]{64}", image) is None:
            raise ValueError("Docker code_execution.image 必须固定到 sha256 digest")
        if python_path != "/usr/bin/python3":
            raise ValueError("Docker provider 只允许固定容器 Python 路径")
        self.image = image
        self.socket_path = str(Path(socket_path).expanduser())
        self.python_path = python_path
        self.allowed_languages = frozenset(allowed_languages)
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
        self.max_cpus = float(max_cpus)
        self.security_attested = bool(security_attested)
        self._health: ProviderHealth | None = None
        self.last_container_id: str | None = None
        self.last_container_inspect: dict[str, Any] | None = None

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name,
            languages=self.allowed_languages,
            trusted_isolation=self.security_attested,
            supports_health_check=True,
            supports_wall_time=True,
            supports_cpu_time=True,
            supports_memory=True,
            supports_process_limit=True,
            supports_file_size_limit=True,
            supports_output_limit=True,
            supports_network_policy=True,
            supports_network_allowlist=False,
            supports_cancellation=True,
            supports_remote_artifacts=False,
        )

    def _api(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        max_bytes: int = 2 * 1024 * 1024,
        expected: tuple[int, ...] = (200, 201, 204),
    ) -> bytes:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        connection = _UnixHTTPConnection(self.socket_path, self.request_timeout_seconds)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise DockerEngineError("Docker Engine 响应超过预算")
            if response.status not in expected:
                message = raw.decode("utf-8", errors="replace")[:500]
                raise DockerEngineError(f"Docker Engine HTTP {response.status}: {message}")
            return raw
        except (OSError, http.client.HTTPException) as error:
            raise DockerEngineError(f"Docker Engine unavailable: {type(error).__name__}") from error
        finally:
            connection.close()

    def _json(self, method: str, path: str, payload: dict | None = None, **kwargs) -> dict:
        raw = self._api(method, path, payload, **kwargs)
        return json.loads(raw.decode("utf-8")) if raw else {}

    def health_check(self, *, force: bool = False) -> ProviderHealth:
        now = time.monotonic()
        if not force and self._health and now - self._health.checked_at < self.health_interval_seconds:
            return self._health
        try:
            ping = self._api("GET", "/_ping", expected=(200,)).decode("ascii", errors="replace")
            encoded_image = urllib.parse.quote(self.image, safe="")
            image = self._json("GET", f"/images/{encoded_image}/json", expected=(200,))
            healthy = ping.strip() == "OK" and bool(image.get("Id"))
            message = "healthy" if healthy else "Docker Engine/image smoke check failed"
        except (DockerEngineError, json.JSONDecodeError) as error:
            healthy = False
            message = f"Docker health check failed: {type(error).__name__}"
        self._health = ProviderHealth(
            healthy=healthy,
            checked_at=now,
            message=message,
            capabilities=self.capabilities(),
            backend_languages=("python",) if healthy else (),
        )
        return self._health

    def _validate_request(self, request: ExecutionRequest) -> tuple[bool, str | None]:
        if request.language not in self.allowed_languages:
            return False, "LANGUAGE_NOT_ALLOWED"
        if len(request.source.encode("utf-8")) > self.max_source_bytes:
            return False, "SOURCE_LIMIT_EXCEEDED"
        if len(request.stdin.encode("utf-8")) > self.max_stdin_bytes:
            return False, "STDIN_LIMIT_EXCEEDED"
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

    @staticmethod
    def _decode_logs(raw: bytes) -> tuple[bytes, bytes]:
        stdout = bytearray()
        stderr = bytearray()
        position = 0
        while position + 8 <= len(raw):
            stream = raw[position]
            size = int.from_bytes(raw[position + 4:position + 8], "big")
            position += 8
            chunk = raw[position:position + size]
            position += size
            (stderr if stream == 2 else stdout).extend(chunk)
        if position == 0 and raw:
            stdout.extend(raw)
        return bytes(stdout), bytes(stderr)

    def _container_config(self, request: ExecutionRequest) -> dict:
        source = base64.b64encode(request.source.encode("utf-8")).decode("ascii")
        stdin = base64.b64encode(request.stdin.encode("utf-8")).decode("ascii")
        args = base64.b64encode(json.dumps(list(request.args)).encode("utf-8")).decode("ascii")
        memory_bytes = request.memory_limit_mb * 1024 * 1024
        file_bytes = request.file_size_limit_mb * 1024 * 1024
        tmpfs_size_mb = request.file_size_limit_mb + 2
        return {
            "Image": self.image,
            "Cmd": [
                self.python_path,
                "-I",
                "-c",
                self._WRAPPER,
                source,
                stdin,
                args,
                str(request.cpu_time_limit_seconds),
                str(file_bytes),
                str(request.output_limit_bytes),
            ],
            "User": "65534:65534",
            "WorkingDir": "/tmp",
            "Env": ["PYTHONDONTWRITEBYTECODE=1", "PYTHONNOUSERSITE=1"],
            "NetworkDisabled": True,
            "OpenStdin": False,
            "AttachStdout": False,
            "AttachStderr": False,
            "HostConfig": {
                "AutoRemove": False,
                "NetworkMode": "none",
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "PidsLimit": request.process_limit,
                "Memory": memory_bytes,
                "MemorySwap": memory_bytes,
                "NanoCpus": int(self.max_cpus * 1_000_000_000),
                "ReadonlyPaths": ["/proc/sys", "/proc/sysrq-trigger", "/proc/irq", "/proc/bus"],
                "MaskedPaths": ["/proc/kcore", "/proc/keys", "/proc/timer_list", "/sys/firmware"],
                "Tmpfs": {
                    "/tmp": (
                        f"rw,noexec,nosuid,nodev,size={tmpfs_size_mb}m,"
                        "uid=65534,gid=65534,mode=0700"
                    )
                },
                "Ulimits": [
                    {"Name": "nofile", "Soft": 64, "Hard": 64},
                    {
                        "Name": "nproc",
                        "Soft": request.process_limit,
                        "Hard": request.process_limit,
                    },
                ],
            },
        }

    def execute(self, request: ExecutionRequest, *, cancel_event=None) -> ExecutionResult:
        health = self.health_check()
        if not health.healthy:
            return ExecutionResult(status="provider_unavailable", provider=self.name, message=health.message)
        valid, reason = self._validate_request(request)
        if not valid:
            return ExecutionResult(status="security_denied", provider=self.name, message=reason)
        if cancel_event is not None and cancel_event.is_set():
            return ExecutionResult(status="cancelled", provider=self.name, message="执行已取消")

        container_id = None
        cancelled = False
        wall_timeout = False
        inspect = {}
        name = f"edu-agent-run-{uuid.uuid4().hex}"
        try:
            created = self._json(
                "POST",
                f"/containers/create?name={name}",
                self._container_config(request),
                expected=(201,),
            )
            container_id = str(created["Id"])
            self.last_container_id = container_id
            self._api("POST", f"/containers/{container_id}/start", expected=(204,))
            deadline = time.monotonic() + request.wall_time_limit_seconds
            while True:
                inspect = self._json("GET", f"/containers/{container_id}/json", expected=(200,))
                self.last_container_inspect = inspect
                if not inspect.get("State", {}).get("Running", False):
                    break
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    self._api("POST", f"/containers/{container_id}/kill?signal=KILL", expected=(204, 409))
                    break
                if time.monotonic() >= deadline:
                    wall_timeout = True
                    self._api("POST", f"/containers/{container_id}/kill?signal=KILL", expected=(204, 409))
                    break
                time.sleep(0.05)
            inspect = self._json("GET", f"/containers/{container_id}/json", expected=(200,))
            self.last_container_inspect = inspect
            raw_logs = self._api(
                "GET",
                f"/containers/{container_id}/logs?stdout=1&stderr=1",
                max_bytes=request.output_limit_bytes * 2 + 64 * 1024,
                expected=(200,),
            )
            stdout_raw, stderr_raw = self._decode_logs(raw_logs)
            stdout, stdout_truncated = _bounded_text(
                stdout_raw.decode("utf-8", errors="replace"),
                min(request.output_limit_bytes, request.artifact_limit_bytes),
            )
            stdout_bytes = len(stdout.encode("utf-8"))
            stderr, stderr_truncated = _bounded_text(
                stderr_raw.decode("utf-8", errors="replace"),
                min(
                    request.output_limit_bytes,
                    max(0, request.artifact_limit_bytes - stdout_bytes),
                ),
            )
            state = inspect.get("State", {})
            exit_code = int(state.get("ExitCode", -1))
            if cancelled:
                status = "cancelled"
            elif state.get("OOMKilled") or "memoryerror" in stderr.lower():
                status = "memory_limit"
            elif wall_timeout or exit_code in {137, 152}:
                status = "timeout"
            elif exit_code == 120 or stdout_truncated or stderr_truncated:
                status = "output_limit"
            elif exit_code == 0:
                status = "success"
            else:
                status = "runtime_error"
            return ExecutionResult(
                status=status,
                exit_status=exit_code,
                stdout=stdout,
                stderr=stderr,
                output_truncated=(
                    exit_code == 120 or stdout_truncated or stderr_truncated
                ),
                provider=self.name,
                run_id=container_id,
                message=state.get("Error") or None,
                metadata={
                    "oom_killed": bool(state.get("OOMKilled")),
                    "network_mode": "none",
                },
            )
        except BaseException:
            if container_id is not None:
                try:
                    self._api(
                        "POST", f"/containers/{container_id}/kill?signal=KILL",
                        expected=(204, 409),
                    )
                except DockerEngineError:
                    pass
            raise
        finally:
            if container_id is not None:
                try:
                    self._api(
                        "DELETE", f"/containers/{container_id}?force=1&v=1",
                        expected=(204, 404),
                    )
                except DockerEngineError:
                    pass
