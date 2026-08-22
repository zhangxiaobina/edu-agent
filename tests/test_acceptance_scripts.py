from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ZSH = shutil.which("zsh")
SCRIPT_NAMES = (
    "acceptance_common.sh",
    "prepare_acceptance.sh",
    "accept_r2.sh",
    "accept_stage7.sh",
    "accept_stage8.sh",
)
SECRET_NAMES = (
    "EDU_AGENT_API_KEY",
    "EDU_AGENT_FALLBACK_API_KEY",
    "EDU_AGENT_JOBE_TOKEN",
    "EDU_AGENT_CONFIG",
    "EDU_AGENT_DEMO_TOKEN",
    "OPENAI_API_KEY",
    "DASHSCOPE_API_KEY",
    "VLLM_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "HF_TOKEN",
    "LANGCHAIN_API_KEY",
    "LANGSMITH_API_KEY",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "GH_TOKEN",
    "GITHUB_TOKEN",
)


def _isolated_repo(tmp_path: Path, *, python_version: str = "3.12") -> Path:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    for name in SCRIPT_NAMES:
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    (repo / ".python-version").write_text(f"{python_version}\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "acceptance-fixture"\nversion = "0"\n'
        'requires-python = ">=3.10"\n',
        encoding="utf-8",
    )
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    return repo


def _fake_uv(tmp_path: Path) -> tuple[Path, Path]:
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir(parents=True)
    log_path = tmp_path / "uv-log.jsonl"
    fake = binary_dir / "uv"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, os, signal, sys, time\n"
        f"secrets = {SECRET_NAMES!r}\n"
        "entry = {\n"
        "    'args': sys.argv[1:],\n"
        "    'tmpdir': os.environ.get('TMPDIR'),\n"
        "    'db': os.environ.get('EDU_AGENT_DB'),\n"
        "    'cache': os.environ.get('UV_CACHE_DIR'),\n"
        "    'venv': os.environ.get('UV_PROJECT_ENVIRONMENT'),\n"
        "    'present_secrets': [name for name in secrets if name in os.environ],\n"
        "}\n"
        "with open(os.environ['ACCEPTANCE_TEST_LOG'], 'a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps(entry) + '\\n')\n"
        "if entry['present_secrets']:\n"
        "    raise SystemExit(91)\n"
        "joined = ' '.join(sys.argv[1:])\n"
        "if sys.argv[1:3] == ['python', 'find'] and "
        "os.environ.get('ACCEPTANCE_TEST_PYTHON_PRESENT') != '1':\n"
        "    raise SystemExit(1)\n"
        "if os.environ.get('ACCEPTANCE_TEST_FAIL_MATCH', '') in joined and "
        "os.environ.get('ACCEPTANCE_TEST_FAIL_MATCH'):\n"
        "    raise SystemExit(42)\n"
        "if os.environ.get('ACCEPTANCE_TEST_SIGNAL_MATCH', '') in joined and "
        "os.environ.get('ACCEPTANCE_TEST_SIGNAL_MATCH'):\n"
        "    os.kill(os.getppid(), signal.SIGTERM)\n"
        "    time.sleep(0.1)\n"
        "if 'sys.version_info.major' in joined:\n"
        "    print(os.environ.get('ACCEPTANCE_TEST_PYTHON_VERSION', '3.12'))\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return binary_dir, log_path


def _environment(binary_dir: Path, log_path: Path, **updates: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": os.pathsep.join((str(binary_dir), environment.get("PATH", ""))),
            "ACCEPTANCE_TEST_LOG": str(log_path),
            **{name: f"must-not-reach-children-{name}" for name in SECRET_NAMES},
            **updates,
        }
    )
    return environment


def _run(repo: Path, script: str, environment: dict[str, str], *arguments: str):
    assert ZSH is not None
    return subprocess.run(
        [ZSH, str(repo / "scripts" / script), *arguments],
        cwd=repo,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _entries(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


def _has_args(entries: list[dict], expected: list[str]) -> bool:
    return any(entry["args"] == expected for entry in entries)


def test_acceptance_shell_syntax_and_public_entrypoint_contract():
    assert ZSH is not None
    subprocess.run(
        [ZSH, "-n", *(str(ROOT / "scripts" / name) for name in SCRIPT_NAMES)],
        cwd=ROOT,
        check=True,
    )

    for relative in ("README.md", "docs/demo-script.md", "docs/architecture.md"):
        content = (ROOT / relative).read_text(encoding="utf-8")
        assert "zsh scripts/accept_stage8.sh" in content
    for relative in ("README.md", "docs/demo-script.md", "docs/architecture.md"):
        content = (ROOT / relative).read_text(encoding="utf-8")
        assert "accept_stage7.sh" not in content


def test_acceptance_command_log_redacts_absolute_paths(tmp_path: Path):
    assert ZSH is not None
    private_path = str(tmp_path / "private-state.db")
    result = subprocess.run(
        [
            ZSH,
            "-c",
            "repo_root=$PWD; source scripts/acceptance_common.sh; "
            "acceptance_log_command command \"$1\" \"$repo_root/artifacts/report.json\"",
            "acceptance-log-test",
            private_path,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert private_path not in result.stderr
    assert str(ROOT) not in result.stderr
    assert "PRIVATE_PATH/private-state.db" in result.stderr
    assert "REPOSITORY/artifacts/report.json" in result.stderr


def test_stage8_controlled_run_bootstraps_and_calls_stage7_once(tmp_path: Path):
    repo = _isolated_repo(tmp_path)
    binary_dir, log_path = _fake_uv(tmp_path)
    temp_base = tmp_path / "temporary"
    temp_base.mkdir()
    artifact_sentinel = repo / "artifacts" / "keep.txt"
    artifact_sentinel.parent.mkdir()
    artifact_sentinel.write_text("user artifact\n", encoding="utf-8")
    environment = _environment(
        binary_dir,
        log_path,
        TMPDIR=str(temp_base),
        ACCEPTANCE_TEST_FAIL_MATCH="scripts/code_sandbox_demo.py",
    )

    result = _run(repo, "accept_stage8.sh", environment)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "sandbox=not_verified" in result.stdout
    entries = _entries(log_path)
    assert _has_args(entries, ["lock", "--check"])
    assert _has_args(entries, ["python", "install", "3.12"])
    assert _has_args(
        entries,
        [
            "sync",
            "--frozen",
            "--python",
            "3.12",
            "--managed-python",
            "--extra",
            "dev",
            "--extra",
            "mcp",
        ],
    )
    assert sum(entry["args"][-1:] == ["scripts/production_runtime_demo.py"] for entry in entries) == 1
    assert sum(entry["args"][-1:] == ["scripts/mcp_demo.py"] for entry in entries) == 1
    assert sum(entry["args"][-1:] == ["scripts/r2_recovery_demo.py"] for entry in entries) == 1
    assert sum(
        entry["args"][3:5] == ["python", "scripts/audit_eval_lineage.py"]
        for entry in entries
    ) == 1
    assert sum(
        entry["args"][3:5] == ["python", "scripts/audit_data_boundaries.py"]
        for entry in entries
    ) == 2
    assert any("tests/test_eval_lineage.py" in entry["args"] for entry in entries)
    assert sum(
        entry["args"]
        == [
            "run",
            "--frozen",
            "--offline",
            "python",
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "tests",
            "-q",
        ]
        for entry in entries
    ) == 1

    generated = next(entry for entry in entries if "edu_agent.data.generate" in entry["args"])
    acceptance_root = Path(generated["db"]).parent
    assert generated["args"][-2:] == ["--out", generated["db"]]
    assert generated["db"] != str(repo / "edu_agent" / "data" / "edu.db")
    assert all(not entry["present_secrets"] for entry in entries)
    assert all(entry["tmpdir"].startswith(str(acceptance_root)) for entry in entries)
    assert all(entry["cache"].startswith(str(acceptance_root)) for entry in entries)
    assert all(entry["venv"] == str(repo / ".venv") for entry in entries)
    assert not acceptance_root.exists()
    assert not (repo / ".venv").exists()
    assert not (repo / "edu_agent" / "data" / "edu.db").exists()
    assert artifact_sentinel.read_text(encoding="utf-8") == "user artifact\n"

    eval_entry = next(entry for entry in entries if "scripts/eval_system.py" in entry["args"])
    assert "--sandbox-report" not in eval_entry["args"]
    final_audit = [
        entry
        for entry in entries
        if entry["args"][3:5] == ["python", "scripts/audit_data_boundaries.py"]
    ][-1]
    assert final_audit["args"][-1] == str(repo / "artifacts")


def test_stage8_propagates_a_stage7_failure(tmp_path: Path):
    repo = _isolated_repo(tmp_path)
    binary_dir, log_path = _fake_uv(tmp_path)
    temp_base = tmp_path / "temporary"
    temp_base.mkdir()
    environment = _environment(
        binary_dir,
        log_path,
        TMPDIR=str(temp_base),
        ACCEPTANCE_TEST_FAIL_MATCH="scripts/plan_runtime_demo.py",
    )

    result = _run(repo, "accept_stage8.sh", environment)

    assert result.returncode == 42
    assert "stage8 acceptance passed" not in result.stdout
    entries = _entries(log_path)
    assert any("scripts/plan_runtime_demo.py" in entry["args"] for entry in entries)
    assert not any("scripts/rag_runtime_demo.py" in entry["args"] for entry in entries)
    assert not any(entry["args"][-2:] == ["tests", "-q"] for entry in entries)
    roots = {Path(entry["db"]).parent for entry in entries if entry["db"]}
    assert roots and all(not root.exists() for root in roots)


def test_stage8_cleans_private_state_on_termination(tmp_path: Path):
    repo = _isolated_repo(tmp_path)
    binary_dir, log_path = _fake_uv(tmp_path)
    temp_base = tmp_path / "temporary"
    temp_base.mkdir()
    result = _run(
        repo,
        "accept_stage8.sh",
        _environment(
            binary_dir,
            log_path,
            TMPDIR=str(temp_base),
            ACCEPTANCE_TEST_SIGNAL_MATCH="scripts/audit_data_boundaries.py",
        ),
    )

    assert result.returncode == 143
    entries = _entries(log_path)
    roots = {Path(entry["db"]).parent for entry in entries if entry["db"]}
    assert roots and all(not root.exists() for root in roots)


def test_stage7_can_run_independently_and_cleans_failures(tmp_path: Path):
    repo = _isolated_repo(tmp_path / "success")
    binary_dir, log_path = _fake_uv(tmp_path / "success-tools")
    temp_base = tmp_path / "success-temporary"
    temp_base.mkdir()
    success = _run(
        repo,
        "accept_stage7.sh",
        _environment(
            binary_dir,
            log_path,
            TMPDIR=str(temp_base),
            ACCEPTANCE_TEST_FAIL_MATCH="scripts/code_sandbox_demo.py",
        ),
    )

    assert success.returncode == 0, success.stdout + success.stderr
    entries = _entries(log_path)
    assert _has_args(entries, ["lock", "--check"])
    assert not any(entry["args"][-2:] == ["tests", "-q"] for entry in entries)
    roots = {Path(entry["db"]).parent for entry in entries if entry["db"]}
    assert roots and all(not root.exists() for root in roots)

    repo = _isolated_repo(tmp_path / "failure")
    binary_dir, log_path = _fake_uv(tmp_path / "failure-tools")
    temp_base = tmp_path / "failure-temporary"
    temp_base.mkdir()
    failure = _run(
        repo,
        "accept_stage7.sh",
        _environment(
            binary_dir,
            log_path,
            TMPDIR=str(temp_base),
            ACCEPTANCE_TEST_FAIL_MATCH="scripts/plan_runtime_demo.py",
        ),
    )

    assert failure.returncode == 42
    roots = {Path(entry["db"]).parent for entry in _entries(log_path) if entry["db"]}
    assert roots and all(not root.exists() for root in roots)


def test_r2_gate_can_run_independently_and_cleans_failures(tmp_path: Path):
    repo = _isolated_repo(tmp_path / "success")
    binary_dir, log_path = _fake_uv(tmp_path / "success-tools")
    temp_base = tmp_path / "success-temporary"
    temp_base.mkdir()
    success = _run(
        repo,
        "accept_r2.sh",
        _environment(binary_dir, log_path, TMPDIR=str(temp_base)),
    )

    assert success.returncode == 0, success.stdout + success.stderr
    entries = _entries(log_path)
    assert _has_args(entries, ["lock", "--check"])
    assert any("tests/test_r2_recovery.py" in entry["args"] for entry in entries)
    assert any("scripts/r2_recovery_demo.py" in entry["args"] for entry in entries)
    assert not any(entry["args"][-2:] == ["tests", "-q"] for entry in entries)
    roots = {Path(entry["db"]).parent for entry in entries if entry["db"]}
    assert roots and all(not root.exists() for root in roots)

    repo = _isolated_repo(tmp_path / "failure")
    binary_dir, log_path = _fake_uv(tmp_path / "failure-tools")
    temp_base = tmp_path / "failure-temporary"
    temp_base.mkdir()
    failure = _run(
        repo,
        "accept_r2.sh",
        _environment(
            binary_dir,
            log_path,
            TMPDIR=str(temp_base),
            ACCEPTANCE_TEST_FAIL_MATCH="tests/test_r2_recovery.py",
        ),
    )

    assert failure.returncode == 42
    assert not any(
        entry["args"][-2:] == ["python", "scripts/r2_recovery_demo.py"]
        for entry in _entries(log_path)
    )
    roots = {Path(entry["db"]).parent for entry in _entries(log_path) if entry["db"]}
    assert roots and all(not root.exists() for root in roots)

def test_prepare_reports_missing_uv_lock_drift_and_incompatible_python(tmp_path: Path):
    repo = _isolated_repo(tmp_path / "missing")
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    missing = _run(repo, "prepare_acceptance.sh", {"PATH": str(empty_path)})
    assert missing.returncode == 1
    assert "uv is required" in missing.stderr

    repo = _isolated_repo(tmp_path / "lock")
    binary_dir, log_path = _fake_uv(tmp_path / "lock-tools")
    drift = _run(
        repo,
        "prepare_acceptance.sh",
        _environment(binary_dir, log_path, ACCEPTANCE_TEST_FAIL_MATCH="lock --check"),
    )
    assert drift.returncode == 1
    assert "uv.lock is out of date" in drift.stderr
    assert not any(entry["args"][:2] == ["python", "install"] for entry in _entries(log_path))

    repo = _isolated_repo(tmp_path / "missing-python")
    binary_dir, log_path = _fake_uv(tmp_path / "missing-python-tools")
    missing_python = _run(
        repo,
        "prepare_acceptance.sh",
        _environment(binary_dir, log_path, ACCEPTANCE_TEST_FAIL_MATCH="python install"),
    )
    assert missing_python.returncode == 1
    assert "Python 3.12 is unavailable" in missing_python.stderr
    assert not any(entry["args"][:2] == ["sync", "--frozen"] for entry in _entries(log_path))

    repo = _isolated_repo(tmp_path / "python", python_version="3.9")
    binary_dir, log_path = _fake_uv(tmp_path / "python-tools")
    incompatible = _run(
        repo,
        "prepare_acceptance.sh",
        _environment(binary_dir, log_path),
    )
    assert incompatible.returncode == 1
    assert "does not satisfy requires-python >=3.10" in incompatible.stderr
    assert _entries(log_path) == []


def test_prepare_is_idempotent_when_managed_python_exists(tmp_path: Path):
    repo = _isolated_repo(tmp_path)
    binary_dir, log_path = _fake_uv(tmp_path)
    environment = _environment(
        binary_dir,
        log_path,
        ACCEPTANCE_TEST_PYTHON_PRESENT="1",
    )

    first = _run(repo, "prepare_acceptance.sh", environment)
    second = _run(repo, "prepare_acceptance.sh", environment)

    assert first.returncode == second.returncode == 0
    entries = _entries(log_path)
    assert sum(entry["args"] == ["lock", "--check"] for entry in entries) == 2
    assert sum(entry["args"][:2] == ["sync", "--frozen"] for entry in entries) == 2
    assert not any(entry["args"][:2] == ["python", "install"] for entry in entries)


def test_stage8_dry_run_traverses_regression_without_claiming_docker(tmp_path: Path):
    repo = _isolated_repo(tmp_path)
    binary_dir, log_path = _fake_uv(tmp_path)
    environment = _environment(binary_dir, log_path, TMPDIR=str(tmp_path))

    result = _run(repo, "accept_stage8.sh", environment, "--dry-run")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "scripts/accept_r2.sh --from-stage8 --dry-run" in result.stderr
    assert "scripts/accept_stage7.sh --from-stage8 --dry-run" in result.stderr
    assert "Docker was not executed during dry-run" in result.stderr
    assert "sandbox=not_verified" in result.stdout
    assert "sandbox=verified" not in result.stdout
    assert "no gate result" in result.stdout
    assert "dry-run passed" not in result.stdout
    assert _entries(log_path) == []
    assert not list(tmp_path.glob("edu-agent-stage8.*"))
