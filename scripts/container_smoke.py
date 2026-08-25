"""Report static container checks and conservatively gate Docker smoke.

The repository CI environment intentionally has no Docker dependency. Static
checks therefore always run, while the runtime checks are explicitly marked
``not_verified`` until an operator invokes this command on a host with a
Docker daemon and runs the documented compose smoke procedure.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_CHECKS = (
    "non_root",
    "no_credentials_or_private_files",
    "readonly_rootfs",
    "persistent_volume",
    "restart",
    "sigterm_drain",
    "backup_restore",
    "api_smoke",
)


def static_checks() -> dict[str, bool]:
    dockerfile = (ROOT / "deploy/api/Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    entrypoint = (ROOT / "deploy/api/entrypoint.sh").read_text(encoding="utf-8")
    return {
        "multistage": dockerfile.count("FROM ") >= 2,
        "lockfile_install": "uv sync --frozen --no-dev" in dockerfile,
        "non_root": "USER 10001:10001" in dockerfile and 'user: "10001:10001"' in compose,
        "ignored_private_inputs": all(
            value in ignored
            for value in (".git", ".env", "*.key", "*.db", "dpo_dumps/", "tests/")
        ),
        "readonly_rootfs": "read_only: true" in compose,
        "persistent_mounts": (
            "edu_agent_state:/var/lib/edu-agent/state" in compose
            and "edu_agent_artifacts:/var/lib/edu-agent/artifacts" in compose
            and "/var/lib/edu-agent-backups" in compose
        ),
        "restart_policy": "restart: unless-stopped" in compose,
        "sigterm_grace": "stop_grace_period: 40s" in compose,
        "preflight_before_api": entrypoint.index("container_preflight.py")
        < entrypoint.index("exec python /app/scripts/api_server.py"),
        "no_unrestricted_docker_socket": "docker.sock" not in compose,
        "api_only_compose": compose.count("\n  api:") == 1 and "jobe" not in compose.lower(),
    }


def docker_available() -> bool:
    binary = shutil.which("docker")
    if binary is None:
        return False
    try:
        result = subprocess.run(
            [binary, "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _run(command: list[str], *, env: dict[str, str], timeout: float = 180) -> str:
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"command_failed:{command[0]}:{result.returncode}")
    return result.stdout.strip()


def _compose_args(project: str) -> list[str]:
    return ["docker", "compose", "-p", project, "-f", str(ROOT / "deploy/docker-compose.yml")]


def _wait_ready(port: int, *, timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health/ready", timeout=2
            ) as response:
                body = json.loads(response.read())
                if response.status == 200 and body.get("ready") is True:
                    return body
                last_error = f"status:{response.status}"
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            last_error = type(error).__name__
        time.sleep(1)
    raise RuntimeError(f"health_timeout:{last_error or 'unknown'}")


def _inspect_contract(container_id: str) -> dict[str, bool]:
    raw = _run(["docker", "inspect", container_id], env=os.environ.copy())
    record = json.loads(raw)[0]
    mounts = record.get("Mounts") or []
    destinations = {str(item.get("Destination")) for item in mounts}
    sources = {str(item.get("Source")) for item in mounts}
    return {
        "non_root": record.get("Config", {}).get("User") == "10001:10001",
        "readonly_rootfs": bool(record.get("HostConfig", {}).get("ReadonlyRootfs")),
        "restart": record.get("HostConfig", {}).get("RestartPolicy", {}).get("Name")
        == "unless-stopped",
        "state_mount": "/var/lib/edu-agent/state" in destinations,
        "artifact_mount": "/var/lib/edu-agent/artifacts" in destinations,
        "backup_mount": "/var/lib/edu-agent-backups" in destinations,
        "no_docker_socket": not any("docker.sock" in source for source in sources),
    }


def run_docker_smoke() -> dict[str, str]:
    """Run the destructive actions only inside a generated throw-away project."""

    project = f"edu-agent-r53-smoke-{os.getpid()}"
    port = _free_port()
    with tempfile.TemporaryDirectory(prefix="edu-agent-r53-smoke-") as root:
        root_path = Path(root)
        config = root_path / "config.toml"
        config.write_text(
            (ROOT / "deploy/api/config.container.example.toml").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        demo_token = root_path / "demo-token"
        provider_key = root_path / "provider-key"
        demo_token.write_text("container-smoke-demo\n", encoding="utf-8")
        provider_key.write_text("container-smoke-provider-key\n", encoding="utf-8")
        backup_dir = root_path / "backups"
        backup_dir.mkdir()
        backup_dir.chmod(0o777)
        environment = os.environ.copy()
        environment.update(
            {
                "EDU_AGENT_CONFIG_FILE": str(config),
                "EDU_AGENT_DEMO_TOKEN_FILE": str(demo_token),
                "EDU_AGENT_API_KEY_FILE": str(provider_key),
                "EDU_AGENT_BACKUP_DIR": str(backup_dir),
                "EDU_AGENT_API_PORT": str(port),
            }
        )
        compose = _compose_args(project)
        try:
            _run([*compose, "config", "--quiet"], env=environment)
            _run([*compose, "up", "-d", "--build", "api"], env=environment, timeout=900)
            _wait_ready(port)
            container_id = _run([*compose, "ps", "-q", "api"], env=environment)
            checks = _inspect_contract(container_id)
            _run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--entrypoint",
                    "sh",
                    "edu-agent-api:local",
                    "-c",
                    "for path in /app/tests /app/.git /app/.env /app/dpo_dumps /var/lib/edu-agent/state/state.db; do test ! -e \"$path\"; done",
                ],
                env=environment,
            )
            checks["no_credentials_or_private_files"] = True
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health/live", timeout=3) as response:
                live_ok = response.status == 200
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/openapi.json", timeout=3) as response:
                openapi = json.load(response)
                openapi_ok = response.status == 200 and isinstance(openapi, dict)
            checks["api_smoke"] = live_ok and openapi_ok
            marker = "/var/lib/edu-agent/artifacts/container-smoke-marker"
            _run(["docker", "exec", container_id, "sh", "-c", f"printf marker > {marker}"], env=environment)
            _run([*compose, "restart", "api"], env=environment, timeout=120)
            _wait_ready(port)
            restarted_id = _run([*compose, "ps", "-q", "api"], env=environment)
            checks["persistent_volume"] = _run(
                ["docker", "exec", restarted_id, "sh", "-c", f"test -s {marker}"],
                env=environment,
            ) == ""
            backup_id = "container-smoke-backup"
            _run(
                [
                    *compose,
                    "exec",
                    "-T",
                    "api",
                    "python",
                    "/app/scripts/state_maintenance.py",
                    "backup",
                    "--state",
                    "/var/lib/edu-agent/state/state.db",
                    "--artifacts",
                    "/var/lib/edu-agent/artifacts",
                    "--target",
                    f"/var/lib/edu-agent-backups/{backup_id}",
                ],
                env=environment,
            )
            _run(
                [
                    *compose,
                    "exec",
                    "-T",
                    "api",
                    "python",
                    "/app/scripts/state_maintenance.py",
                    "verify-backup",
                    "--backup",
                    f"/var/lib/edu-agent-backups/{backup_id}",
                ],
                env=environment,
            )
            restore_target = "/var/lib/edu-agent-backups/container-smoke-restore"
            _run(
                [
                    *compose,
                    "exec",
                    "-T",
                    "api",
                    "python",
                    "/app/scripts/state_maintenance.py",
                    "restore",
                    "--backup",
                    f"/var/lib/edu-agent-backups/{backup_id}",
                    "--target-dir",
                    restore_target,
                ],
                env=environment,
            )
            _run(
                [
                    *compose,
                    "exec",
                    "-T",
                    "api",
                    "python",
                    "/app/scripts/state_maintenance.py",
                    "verify-state",
                    "--state",
                    f"{restore_target}/state.db",
                    "--artifacts",
                    f"{restore_target}/artifacts",
                ],
                env=environment,
            )
            checks["backup_restore"] = True
            _run([*compose, "stop", "-t", "40", "api"], env=environment, timeout=60)
            transitions = _run(
                [
                    *compose,
                    "run",
                    "--rm",
                    "--no-deps",
                    "--entrypoint",
                    "python",
                    "api",
                    "-c",
                    "import sqlite3; print(','.join(r[0] for r in sqlite3.connect('/var/lib/edu-agent/state/state.db').execute(\"SELECT decision FROM audit_events WHERE action='process.lifecycle_transition' ORDER BY id\")))",
                ],
                env=environment,
            )
            checks["sigterm_drain"] = all(
                state in transitions.split(",") for state in ("starting", "running", "draining", "stopped")
            )
            checks["readonly_rootfs"] = checks.get("readonly_rootfs", False)
            checks["non_root"] = checks.get("non_root", False)
            checks["restart"] = checks.get("restart", False)
            checks["state_mount"] = checks.get("state_mount", False)
            return {name: "verified" if checks.get(name, False) else "failed" for name in RUNTIME_CHECKS}
        finally:
            subprocess.run(
                [*compose, "down", "--volumes", "--remove-orphans"],
                cwd=ROOT,
                env=environment,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
            )


def build_report(*, run_docker: bool = False) -> dict[str, object]:
    static = static_checks()
    if run_docker and docker_available():
        runtime = run_docker_smoke()
        status = "verified" if all(value == "verified" for value in runtime.values()) else "failed"
        reason = "throw-away compose project smoke completed"
    else:
        runtime = {name: "not_verified" for name in RUNTIME_CHECKS}
        reason = (
            "Docker daemon unavailable in this environment"
            if not docker_available()
            else "Docker smoke requires the explicit --docker flag"
        )
        status = "not_verified"
    return {
        "schema_version": "edu-agent.container-smoke.v1",
        "static": static,
        "static_status": "verified" if all(static.values()) else "failed",
        "runtime_status": status,
        "runtime": runtime,
        "reason": reason,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--docker",
        action="store_true",
        help="build and run a throw-away compose smoke project when Docker is available",
    )
    args = parser.parse_args()
    report = build_report(run_docker=args.docker)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["static_status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
