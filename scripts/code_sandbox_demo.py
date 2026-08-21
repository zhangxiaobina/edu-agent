"""Stage 6 real-backend code-execution security demonstration.

The script never touches MySQL. It talks only to Jobe or the Docker Engine
socket and uses temporary host paths for filesystem-isolation probes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path

from edu_agent.code_execution import (
    DockerCodeExecutionProvider,
    ExecutionRequest,
    JobeCodeExecutionProvider,
)

DEFAULT_DOCKER_IMAGE = (
    "xiaobiny/jobe-custom@sha256:"
    "173036eb3b5cdc2a2634da0bd70eba56d22efced2b2981568359cc2c6bf63bd4"
)


def _request(source: str, **overrides) -> ExecutionRequest:
    values = {
        "language": "python",
        "source": source,
        "cpu_time_limit_seconds": 2,
        "wall_time_limit_seconds": 5,
        "memory_limit_mb": 512,
        "output_limit_bytes": 64 * 1024,
        "process_limit": 8,
        "file_size_limit_mb": 8,
        "artifact_limit_bytes": 128 * 1024,
        "network_policy": "disabled",
    }
    values.update(overrides)
    return ExecutionRequest(**values)


def _replace_request(request: ExecutionRequest, **changes) -> ExecutionRequest:
    values = dict(request.__dict__)
    values.update(changes)
    return ExecutionRequest(**values)


def _run(provider, name: str, request: ExecutionRequest, check) -> dict:
    started = time.monotonic()
    result = provider.execute(request)
    record = result.to_dict()
    for stream in ("stdout", "stderr"):
        text = str(record.get(stream, ""))
        encoded = text.encode("utf-8")
        record[f"{stream}_bytes"] = len(encoded)
        if len(encoded) > 512:
            record[stream] = encoded[:512].decode("utf-8", errors="ignore") + "...[demo preview]"
    record.update({"name": name, "duration_seconds": round(time.monotonic() - started, 3)})
    try:
        record["passed"] = bool(check(result))
    except Exception as error:
        record["passed"] = False
        record["check_error"] = f"{type(error).__name__}: {error}"
    return record


def _docker_contract_case(provider: DockerCodeExecutionProvider) -> dict:
    inspect = provider.last_container_inspect or {}
    config = inspect.get("Config", {})
    host = inspect.get("HostConfig", {})
    passed = bool(
        config.get("User") == "65534:65534"
        and config.get("NetworkDisabled") is True
        and not config.get("Volumes")
        and host.get("NetworkMode") == "none"
        and host.get("ReadonlyRootfs") is True
        and host.get("Privileged") is False
        and host.get("CapDrop") == ["ALL"]
        and "no-new-privileges:true" in host.get("SecurityOpt", [])
        and not host.get("Binds")
        and int(host.get("PidsLimit", 0)) > 0
        and int(host.get("Memory", 0)) > 0
        and int(host.get("NanoCpus", 0)) > 0
    )
    return {
        "name": "docker_inspect_security_contract",
        "status": "verified" if passed else "failed",
        "passed": passed,
        "observed": {
            "user": config.get("User"),
            "network_disabled": config.get("NetworkDisabled"),
            "network_mode": host.get("NetworkMode"),
            "readonly_rootfs": host.get("ReadonlyRootfs"),
            "privileged": host.get("Privileged"),
            "cap_drop": host.get("CapDrop"),
            "security_opt": host.get("SecurityOpt"),
            "binds": host.get("Binds"),
            "pids_limit": host.get("PidsLimit"),
            "memory": host.get("Memory"),
            "nano_cpus": host.get("NanoCpus"),
        },
    }


def _docker_cancellation_case(provider: DockerCodeExecutionProvider) -> dict:
    cancel_event = threading.Event()
    holder = {}
    previous_container_id = provider.last_container_id

    def execute() -> None:
        holder["result"] = provider.execute(
            _request(
                "while True:\n    pass",
                cpu_time_limit_seconds=10,
                wall_time_limit_seconds=15,
            ),
            cancel_event=cancel_event,
        )

    worker = threading.Thread(target=execute, daemon=True)
    worker.start()
    deadline = time.monotonic() + 5
    while (
        provider.last_container_id in {None, previous_container_id}
        and time.monotonic() < deadline
    ):
        time.sleep(0.02)
    container_id = provider.last_container_id
    cancel_event.set()
    worker.join(timeout=5)
    result = holder.get("result")
    deleted = False
    if container_id and not worker.is_alive():
        try:
            provider._api(
                "GET",
                f"/containers/{container_id}/json",
                expected=(404,),
            )
            deleted = True
        except Exception:
            deleted = False
    return {
        "name": "remote_cancellation_and_cleanup",
        "status": getattr(result, "status", "missing_result"),
        "run_id": container_id,
        "container_deleted": deleted,
        "worker_stopped": not worker.is_alive(),
        "passed": bool(
            result is not None
            and result.status == "cancelled"
            and not worker.is_alive()
            and deleted
        ),
    }


def _provider(args):
    if args.provider == "docker":
        return DockerCodeExecutionProvider(
            args.image,
            socket_path=args.docker_socket,
            security_attested=True,
        )
    return JobeCodeExecutionProvider(args.endpoint, security_attested=True)


def run_demo(provider, *, e2e: bool) -> dict:
    health = provider.health_check(force=True)
    capabilities = health.capabilities
    report = {
        "provider": provider.name,
        "backend": getattr(provider, "endpoint", getattr(provider, "image", None)),
        "health": {
            "healthy": health.healthy,
            "message": health.message,
            "backend_languages": health.backend_languages,
        },
        "capabilities": {
            key: value
            for key, value in capabilities.__dict__.items()
            if key != "languages"
        },
        "languages": sorted(capabilities.languages),
        "limits": {
            "cpu_seconds": provider.max_cpu_time_seconds,
            "wall_seconds": provider.max_wall_time_seconds,
            "memory_mb": provider.max_memory_mb,
            "minimum_memory_mb": provider.min_memory_mb,
            "processes": provider.max_processes,
            "file_size_mb": provider.max_file_size_mb,
            "output_bytes": provider.max_output_bytes,
            "artifact_bytes": provider.max_artifact_bytes,
            "network_policy": "disabled",
        },
        "cases": [],
    }
    if not health.healthy:
        report["all_applicable_passed"] = False
        report["registry_eligible"] = False
        return report

    normal = _run(
        provider,
        "normal",
        _request("print(6 * 7)"),
        lambda result: result.success and result.stdout.strip() == "42",
    )
    report["cases"].append(normal)
    if isinstance(provider, DockerCodeExecutionProvider):
        report["cases"].append(_docker_contract_case(provider))
    report["cases"].extend(
        [
            _run(
                provider,
                "cpu_wall_timeout",
                _request("while True:\n    pass", cpu_time_limit_seconds=1),
                lambda result: result.status == "timeout",
            ),
            _run(
                provider,
                "caller_network_override_denied",
                _replace_request(
                    _request("print('never')"),
                    network_policy="allowlist",
                    network_allowlist=("example.com",),
                ),
                lambda result: result.status == "security_denied",
            ),
            _run(
                provider,
                "language_allowlist",
                _replace_request(_request("echo never"), language="shell"),
                lambda result: result.status == "security_denied",
            ),
        ]
    )
    if isinstance(provider, DockerCodeExecutionProvider):
        report["cases"].append(
            _run(
                provider,
                "args_are_data_not_docker_options",
                _request("import sys; print(sys.argv[1])", args=("--privileged",)),
                lambda result: result.success and result.stdout.strip() == "--privileged",
            )
        )
    else:
        report["cases"].append(
            _run(
                provider,
                "unsupported_args_denied",
                _replace_request(_request("print('never')"), args=("--privileged",)),
                lambda result: result.status == "security_denied",
            )
        )

    if e2e:
        with tempfile.TemporaryDirectory(prefix="edu-agent-sandbox-e2e-") as directory:
            root = Path(directory)
            host_secret = root / "host-secret.txt"
            artifact_secret = root / "other-tenant-artifact.txt"
            host_write = root / "must-not-exist.txt"
            host_secret.write_text("HOST_ONLY_SECRET", encoding="utf-8")
            artifact_secret.write_text("OTHER_TENANT_ARTIFACT", encoding="utf-8")
            passwd_digest = hashlib.sha256(Path("/etc/passwd").read_bytes()).hexdigest()
            protected_paths = [
                str(host_secret),
                str(artifact_secret),
                str(Path.home()),
                str(Path.cwd() / ".env"),
            ]
            file_probe = f"""
import hashlib, json
paths = {json.dumps(protected_paths)}
result = {{}}
for path in paths:
    try:
        data = open(path, 'rb').read()
        result[path] = hashlib.sha256(data).hexdigest()
    except Exception as error:
        result[path] = type(error).__name__
try:
    result['/etc/passwd'] = hashlib.sha256(open('/etc/passwd', 'rb').read()).hexdigest()
except Exception as error:
    result['/etc/passwd'] = type(error).__name__
print(json.dumps(result, sort_keys=True))
"""
            escape_probe = f"""
import os
targets = [{str(host_write)!r}, '../../../../{str(host_write).lstrip('/')}']
for target in targets:
    try:
        with open(target, 'w') as handle:
            handle.write('ESCAPED')
        print('WROTE', target)
    except Exception as error:
        print('DENIED', type(error).__name__)
try:
    os.symlink({str(host_secret)!r}, '/tmp/host-link')
    print(open('/tmp/host-link').read())
except Exception as error:
    print('SYMLINK_DENIED', type(error).__name__)
"""
            report["cases"].extend(
                [
                    _run(
                        provider,
                        "memory_limit",
                        _request(
                            "chunks=[]\nwhile True:\n    chunks.append(bytearray(8*1024*1024))",
                            memory_limit_mb=max(384, provider.min_memory_mb),
                        ),
                        lambda result: result.status == "memory_limit",
                    ),
                    _run(
                        provider,
                        "process_limit",
                        _request(
                            """import subprocess
children=[]
try:
    for _ in range(64):
        children.append(subprocess.Popen(['sleep', '5']))
    print('UNBOUNDED', len(children))
except Exception as error:
    print('DENIED', type(error).__name__, len(children))
finally:
    for child in children:
        child.terminate()
""",
                            process_limit=8,
                        ),
                        lambda result: (
                            "DENIED" in result.stdout and "UNBOUNDED" not in result.stdout
                        ),
                    ),
                    _run(
                        provider,
                        "file_size_limit",
                        _request(
                            """try:
    open('/tmp/large', 'wb').write(b'x' * (4 * 1024 * 1024))
    print('UNBOUNDED')
except Exception as error:
    print('DENIED', type(error).__name__)
""",
                            file_size_limit_mb=1,
                        ),
                        lambda result: (
                            "DENIED" in result.stdout and "UNBOUNDED" not in result.stdout
                        ),
                    ),
                    _run(
                        provider,
                        "host_and_cross_tenant_filesystem",
                        _request(file_probe),
                        lambda result: (
                            result.success
                            and "HOST_ONLY_SECRET" not in result.stdout
                            and "OTHER_TENANT_ARTIFACT" not in result.stdout
                            and passwd_digest not in result.stdout
                        ),
                    ),
                    _run(
                        provider,
                        "host_write_traversal_and_symlink",
                        _request(escape_probe),
                        lambda result: (
                            not host_write.exists()
                            and "HOST_ONLY_SECRET" not in result.stdout
                            and "SYMLINK_DENIED" in result.stdout
                        ),
                    ),
                    _run(
                        provider,
                        "default_network_denied",
                        _request(
                            """import socket
try:
    socket.create_connection(('1.1.1.1', 53), timeout=2)
    print('NETWORK_ALLOWED')
except Exception as error:
    print('NETWORK_DENIED', type(error).__name__)
"""
                        ),
                        lambda result: (
                            "NETWORK_DENIED" in result.stdout
                            and "NETWORK_ALLOWED" not in result.stdout
                        ),
                    ),
                    _run(
                        provider,
                        "output_budget",
                        _request("print('x' * 200000)", output_limit_bytes=4096),
                        lambda result: (
                            result.status == "output_limit"
                            and result.output_truncated
                            and len(result.stdout.encode("utf-8")) <= 4096
                        ),
                    ),
                    _run(
                        provider,
                        "total_artifact_budget",
                        _request(
                            "import sys; print('o'*4000); print('e'*4000, file=sys.stderr)",
                            output_limit_bytes=8192,
                            artifact_limit_bytes=4096,
                        ),
                        lambda result: (
                            result.status == "output_limit"
                            and result.output_truncated
                            and len(result.stdout.encode("utf-8"))
                            + len(result.stderr.encode("utf-8"))
                            <= 4096
                        ),
                    ),
                ]
            )

        if isinstance(provider, DockerCodeExecutionProvider):
            report["cases"].append(_docker_cancellation_case(provider))
        else:
            report["cases"].append(
                {
                    "name": "remote_cancellation_and_cleanup",
                    "passed": False,
                    "status": "not_supported",
                    "message": "Vanilla Jobe has no per-run cancellation API",
                }
            )
    report["all_applicable_passed"] = all(case["passed"] for case in report["cases"])
    report["registry_eligible"] = bool(
        health.healthy
        and capabilities.trusted_isolation
        and capabilities.supports_cancellation
        and report["all_applicable_passed"]
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("jobe", "docker"), default="docker")
    parser.add_argument(
        "--endpoint",
        default=os.getenv("EDU_AGENT_JOBE_ENDPOINT", "http://127.0.0.1:4010"),
    )
    parser.add_argument(
        "--image",
        default=os.getenv("EDU_AGENT_DOCKER_IMAGE", DEFAULT_DOCKER_IMAGE),
    )
    parser.add_argument(
        "--docker-socket",
        default=os.getenv("EDU_AGENT_DOCKER_SOCKET", "~/.docker/run/docker.sock"),
    )
    parser.add_argument("--e2e", action="store_true")
    parser.add_argument("--require-all", action="store_true")
    args = parser.parse_args()
    report = run_demo(_provider(args), e2e=args.e2e)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 1 if args.require_all and not report["registry_eligible"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
