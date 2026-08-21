from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from edu_agent.data_audit import audit_paths
from edu_agent.eval.provenance import (
    UNAVAILABLE_COMMIT,
    build_provenance,
    config_hash,
    read_git_state,
    sanitize_artifact,
)
from scripts.benchmark_trace_scaling import benchmark


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
RUNTIME_DATA_SOURCES = (
    "edu_agent/data/__init__.py",
    "edu_agent/data/db.py",
    "edu_agent/data/generate.py",
    "edu_agent/data/kg.py",
    "edu_agent/data/schema.sql",
)


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _committed_repo(path: Path) -> tuple[Path, str]:
    path.mkdir()
    _git(path, "init")
    (path / "source.txt").write_text("source\n", encoding="utf-8")
    _git(path, "add", "source.txt")
    _git(
        path,
        "-c",
        "commit.gpgsign=false",
        "-c",
        "user.name=CI Provenance Test",
        "-c",
        "user.email=ci-provenance@example.invalid",
        "commit",
        "-m",
        "test fixture",
    )
    return path, _git(path, "rev-parse", "HEAD")


def test_git_provenance_comes_from_repository_and_tracks_dirty_state(tmp_path, monkeypatch):
    repo, commit = _committed_repo(tmp_path / "repo")
    monkeypatch.setenv("GITHUB_SHA", "f" * 40)
    monkeypatch.setenv("CI_COMMIT_SHA", "e" * 40)

    clean = read_git_state(repo)
    assert clean.commit == commit
    assert clean.commit not in {os.environ["GITHUB_SHA"], os.environ["CI_COMMIT_SHA"]}
    assert clean.dirty is False

    candidate = build_provenance(
        repo_root=repo,
        config={"seed": 42, "mode": "offline"},
        seed=42,
        model_name="oracle",
        model_mode="offline_oracle",
        evidence_mode="candidate",
    )
    assert candidate["provenance_gate"] == {
        "required": True,
        "status": "passed",
        "reasons": [],
    }

    (repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    dirty = build_provenance(
        repo_root=repo,
        config={"seed": 42},
        seed=42,
        model_name="oracle",
        model_mode="offline_oracle",
        evidence_mode="release",
    )
    assert dirty["commit"] == commit
    assert dirty["git"]["dirty"] is True
    assert dirty["provenance_gate"]["status"] == "failed"
    assert dirty["provenance_gate"]["reasons"] == ["git_worktree_dirty"]


def test_missing_git_is_unavailable_and_blocks_candidate_modes(tmp_path, monkeypatch):
    source_copy = tmp_path / "source-copy"
    source_copy.mkdir()
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)

    state = read_git_state(source_copy)
    assert state.commit == UNAVAILABLE_COMMIT
    assert state.dirty is None

    development = build_provenance(
        repo_root=source_copy,
        config={"mode": "offline"},
        seed=7,
        model_name="none",
        model_mode="offline_runtime_benchmark",
    )
    assert development["commit"] == UNAVAILABLE_COMMIT
    assert development["provenance_gate"]["status"] == "not_enforced"

    for evidence_mode in ("candidate", "release"):
        gated = build_provenance(
            repo_root=source_copy,
            config={"mode": "offline"},
            seed=7,
            model_name="none",
            model_mode="offline_runtime_benchmark",
            evidence_mode=evidence_mode,
        )
        assert gated["commit"] == UNAVAILABLE_COMMIT
        assert gated["provenance_gate"]["status"] == "failed"
        assert gated["provenance_gate"]["reasons"] == ["git_commit_unavailable"]


def test_config_hash_is_canonical_and_provenance_is_complete(tmp_path):
    first = {"nested": {"b": 2, "a": 1}, "seed": 42}
    second = {"seed": 42, "nested": {"a": 1, "b": 2}}
    assert config_hash(first) == config_hash(second)
    assert config_hash(first) != config_hash({**first, "seed": 43})

    report = build_provenance(
        repo_root=tmp_path,
        config=first,
        seed=42,
        model_name="oracle",
        model_mode="offline_oracle",
    )
    assert report["config_hash"] == config_hash(first)
    assert report["seed"] == 42
    assert report["model"] == {"name": "oracle", "mode": "offline_oracle"}
    assert set(report["environment"]) == {
        "architecture",
        "os",
        "os_release",
        "python",
        "python_implementation",
        "sqlite",
    }
    assert all("/" not in value and "\\" not in value for value in report["environment"].values())


def test_artifact_sanitizer_removes_credentials_pii_and_private_paths():
    canary = "literal-secret-value-9382"
    report = sanitize_artifact(
        {
            "api_key": canary,
            "student_name": "Identifiable Student",
            "path": "/Users/private-user/project/config.toml",
            "message": f"Bearer {canary} from /home/private-user/work/report.json",
            "relative_artifact": "ci-artifacts/report.json",
            "tokens": {"input": 1, "output": 2, "total": 3},
        },
        secrets=(canary,),
    )
    serialized = json.dumps(report, ensure_ascii=False)
    assert canary not in serialized
    assert "Identifiable Student" not in serialized
    assert "/Users/private-user" not in serialized
    assert "/home/private-user" not in serialized
    assert report["path"] == "[REDACTED_PATH]"
    assert "api_key" not in report
    assert "student_name" not in report
    assert report["relative_artifact"] == "ci-artifacts/report.json"
    assert report["tokens"] == {"input": 1, "output": 2, "total": 3}


def test_redacted_evaluation_shape_has_no_boundary_findings(tmp_path):
    artifact = tmp_path / "report.json"
    artifact.write_text(
        json.dumps(
            sanitize_artifact(
                {
                    "model": {"name": "oracle", "mode": "offline_oracle"},
                    "tokens": {"input": 0, "output": 0, "total": 0},
                    "artifact": "ci-artifacts/report.json",
                }
            )
        ),
        encoding="utf-8",
    )
    assert audit_paths([artifact])["findings"] == []


def test_trace_benchmark_always_records_shared_provenance():
    report = benchmark(event_count=5, page_size=2)
    assert report["schema_version"] == "edu-agent.trace-scaling.v2"
    assert report["seed"] == 42
    assert report["model"] == {
        "name": "none",
        "mode": "offline_runtime_benchmark",
    }
    assert len(report["config_hash"]) == 64
    assert report["config"]["input_hashes"]["uv.lock"] != "unavailable"
    assert report["environment"]["python"]
    assert all(report["assertions"].values())


def test_system_eval_cli_writes_redacted_complete_provenance(tmp_path):
    output = tmp_path / "private-output" / "system-eval.json"
    canary = "sk-cli-provenance-canary-938247"
    environment = os.environ.copy()
    environment["EDU_AGENT_API_KEY"] = canary
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "eval_system.py"),
            "--quiet",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "offline system evaluation passed"
    report = json.loads(output.read_text(encoding="utf-8"))
    serialized = json.dumps(report, ensure_ascii=False)
    assert report["schema_version"] == "edu-agent.system-eval.v4"
    assert report["commit"] == _git(ROOT, "rev-parse", "HEAD")
    assert isinstance(report["git"]["dirty"], bool)
    assert report["seed"] == 42
    assert report["model"] == {"name": "oracle", "mode": "offline_oracle"}
    assert len(report["config_hash"]) == 64
    assert report["config"]["input_hashes"]["uv.lock"] != "unavailable"
    assert report["config"]["lineage_manifest_hash"] == report["lineage"]["manifest_hash"]
    assert report["lineage"]["passed"] is True
    assert report["lineage"]["split_counts"] == {"dev": 12, "test": 6, "train": 55}
    assert report["lineage"]["deterministic_generation"]["passed"] is True
    assert report["agent"]["evidence_scope"] == "harness_only"
    assert report["agent"]["capability_claim"] == "not_measured"
    assert report["agent"]["split"] == "test"
    assert report["agent"]["repetitions"]["completed"] == 1
    assert report["evaluation"]["real_model"] == {"status": "not_run", "metrics": None}
    assert report["environment"]["python"]
    assert canary not in serialized + result.stdout + result.stderr
    assert str(tmp_path) not in serialized + result.stdout + result.stderr


def test_audit_cli_fails_without_printing_secret_or_private_path(tmp_path):
    canary = "CANARY_SECRET_ci-audit-938247"
    source = tmp_path / "private" / "events.jsonl"
    source.parent.mkdir()
    source.write_text(json.dumps({"api_key": canary}) + "\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "audit_data_boundaries.py"),
            "--fail-on-findings",
            str(source),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert '"classification": "credential"' in result.stdout
    assert canary not in result.stdout + result.stderr
    assert str(tmp_path) not in result.stdout + result.stderr


def test_runtime_data_sources_are_tracked_and_not_ignored():
    tracked = set(_git(ROOT, "ls-files", "--", *RUNTIME_DATA_SOURCES).splitlines())
    assert tracked == set(RUNTIME_DATA_SOURCES)

    for source in RUNTIME_DATA_SOURCES:
        ignored = subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore", "--no-index", "-q", source],
            check=False,
        )
        assert ignored.returncode == 1, f"runtime source is ignored: {source}"

    generated_database = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "check-ignore",
            "--no-index",
            "-q",
            "edu_agent/data/edu.db",
        ],
        check=False,
    )
    assert generated_database.returncode == 0


def test_ci_is_single_platform_frozen_secret_free_and_offline():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    lowered = workflow.lower()

    assert "runs-on: ubuntu-24.04" in workflow
    assert "matrix:" not in workflow
    assert "persist-credentials: false" in workflow
    action_refs = re.findall(r"uses: [^@\s]+@([^\s]+)", workflow)
    assert len(action_refs) == 3
    assert all(re.fullmatch(r"[0-9a-f]{40}", reference) for reference in action_refs)
    assert 'version: "0.11.16"' in workflow
    assert "uv lock --check" in workflow
    assert (
        "uv sync --frozen --python 3.12 --managed-python --extra dev --extra mcp"
        in workflow
    )
    assert "uv pip check --python .venv/bin/python" in workflow
    assert "test ! -e .venv" in workflow
    assert "pre-existing database files are forbidden" in workflow
    assert "ruff check ." in workflow
    assert "python -m pytest -p no:cacheprovider tests -q" in workflow
    assert "scripts/eval_system.py" in workflow
    assert "scripts/audit_eval_lineage.py" in workflow
    assert "scripts/benchmark_trace_scaling.py" in workflow
    assert "scripts/audit_data_boundaries.py" in workflow
    assert "--fail-on-findings" in workflow
    assert workflow.count("uv run --frozen --offline") >= 5
    assert "--evidence-mode candidate --quiet" in workflow
    assert "${{ secrets." not in workflow
    assert "docker" not in lowered
    assert workflow.index("uv sync --frozen") < workflow.index("ruff check .")
    assert workflow.index("Sensitive data boundary audit") < workflow.index("upload-artifact")

    for name in (
        "EDU_AGENT_API_KEY",
        "EDU_AGENT_FALLBACK_API_KEY",
        "EDU_AGENT_JOBE_TOKEN",
        "OPENAI_API_KEY",
        "DASHSCOPE_API_KEY",
        "VLLM_API_KEY",
        "LANGCHAIN_API_KEY",
        "LANGSMITH_API_KEY",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "GH_TOKEN",
        "GITHUB_TOKEN",
    ):
        assert f'{name}: ""' in workflow

    assert "ci-artifacts/" in (ROOT / ".gitignore").read_text(encoding="utf-8")
