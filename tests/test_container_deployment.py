from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.container_preflight import run_preflight
from scripts.container_smoke import RUNTIME_CHECKS, build_report


ROOT = Path(__file__).resolve().parents[1]


def test_container_build_is_multistage_locked_and_non_root():
    dockerfile = (ROOT / "deploy/api/Dockerfile").read_text(encoding="utf-8")
    assert dockerfile.count("FROM ") >= 2
    assert "uv sync --frozen --no-dev --no-install-project" in dockerfile
    assert "uv sync --frozen --no-dev --no-editable" in dockerfile
    assert "python:3.12.11-slim-bookworm@sha256:" in dockerfile
    assert "ghcr.io/astral-sh/uv:0.11.16@sha256:" in dockerfile
    assert "COPY ." not in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "ENTRYPOINT [\"/usr/local/bin/edu-agent-entrypoint\"]" in dockerfile
    assert "scripts/state_maintenance.py" in dockerfile


def test_dockerignore_covers_credentials_private_state_and_test_inputs():
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for pattern in (".git", ".env", "*.key", "*.pem", "*.db", "dpo_dumps/", "tests/"):
        assert pattern in ignored


def test_compose_is_api_only_hardened_and_has_explicit_persistent_mounts():
    compose = (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
    assert compose.count("\n  api:") == 1
    assert "jobe" not in compose.lower()
    assert "docker.sock" not in compose
    assert 'user: "10001:10001"' in compose
    assert "read_only: true" in compose
    assert "cap_drop:" in compose and "- ALL" in compose
    assert "no-new-privileges:true" in compose
    assert "PYTHON_IMAGE: ${EDU_AGENT_PYTHON_IMAGE:-python:3.12.11-slim-bookworm@sha256:" in compose
    assert "UV_IMAGE: ${EDU_AGENT_UV_IMAGE:-ghcr.io/astral-sh/uv:0.11.16@sha256:" in compose
    assert "edu_agent_state:/var/lib/edu-agent/state" in compose
    assert "edu_agent_artifacts:/var/lib/edu-agent/artifacts" in compose
    assert "/var/lib/edu-agent-backups" in compose
    assert "restart: unless-stopped" in compose
    assert "stop_grace_period: 40s" in compose
    assert "/health/ready" in compose


def test_entrypoint_runs_preflight_before_api_and_reads_file_secrets():
    entrypoint = (ROOT / "deploy/api/entrypoint.sh").read_text(encoding="utf-8")
    subprocess.run(["sh", "-n", str(ROOT / "deploy/api/entrypoint.sh")], check=True)
    assert "EDU_AGENT_DEMO_TOKEN_FILE" in entrypoint
    assert "EDU_AGENT_API_KEY_FILE" in entrypoint
    assert entrypoint.index("container_preflight.py") < entrypoint.index(
        "exec python /app/scripts/api_server.py"
    )


def test_container_preflight_migrates_and_probes_state_and_artifacts(tmp_path):
    config = tmp_path / "config.toml"
    state = tmp_path / "state" / "state.db"
    artifacts = tmp_path / "artifacts"
    config.write_text(
        "\n".join(
            (
                "[storage]",
                f'state_path = "{state}"',
                f'artifact_path = "{artifacts}"',
            )
        ),
        encoding="utf-8",
    )
    report = run_preflight(str(config))
    assert report == {
        "status": "passed",
        "schema": "current",
        "migration": True,
        "state_db_writable": True,
        "artifact_directory_writable": True,
    }
    assert state.is_file()
    assert artifacts.is_dir()


def test_container_preflight_requires_an_explicit_config_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        run_preflight(str(tmp_path / "missing.toml"))


def test_container_smoke_reports_runtime_matrix_without_docker_claims():
    report = build_report()
    assert report["schema_version"] == "edu-agent.container-smoke.v1"
    assert report["static_status"] == "verified"
    assert set(report["runtime"]) == set(RUNTIME_CHECKS)
    assert report["runtime_status"] in {"verified", "not_verified"}
    if report["runtime_status"] == "not_verified":
        pytest.skip("not_verified: Docker daemon is not available for container E2E")


def test_container_smoke_json_is_safe_to_publish():
    report = build_report()
    rendered = json.dumps(report, ensure_ascii=False)
    assert "EDU_AGENT_API_KEY" not in rendered
    assert "docker.sock" not in rendered
