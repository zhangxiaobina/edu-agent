"""Secret-free, source-derived provenance for evaluation artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from edu_agent.data_classification import DataClass, classify_key, normalize_key
from edu_agent.observability import RedactionPolicy


UNAVAILABLE_COMMIT = "unavailable"
EVIDENCE_MODES = ("development", "candidate", "release")
PROVENANCE_SCHEMA_VERSION = "edu-agent.eval-provenance.v1"

_COMMIT_PATTERN = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
_PRIVATE_PATH_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9:])/(?:Users|home|private|tmp|var/folders)/[^\s\"'<>]*"),
    re.compile(r"(?i)\b[A-Z]:\\(?:Users|Documents and Settings|Temp)\\[^\s\"'<>]*"),
)
_PATH_KEYS = frozenset({"cwd", "directory", "file", "home", "location", "path", "root", "workspace"})
_CREDENTIAL_ENV_NAMES = (
    "EDU_AGENT_API_KEY",
    "EDU_AGENT_FALLBACK_API_KEY",
    "EDU_AGENT_JOBE_TOKEN",
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


@dataclass(frozen=True)
class GitState:
    commit: str = UNAVAILABLE_COMMIT
    dirty: bool | None = None

    @property
    def available(self) -> bool:
        return self.commit != UNAVAILABLE_COMMIT

    def to_dict(self) -> dict[str, Any]:
        return {"available": self.available, "dirty": self.dirty}


def _run_git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )


def read_git_state(repo_root: str | Path) -> GitState:
    """Read commit and worktree state from the actual repository metadata.

    Environment variables such as ``GITHUB_SHA`` are deliberately ignored.
    The supplied root must itself be the Git worktree root, so a parent
    repository cannot lend provenance to a copied source directory.
    """
    root = Path(repo_root).resolve()
    if not (root / ".git").exists():
        return GitState()
    try:
        top_level = _run_git(root, "rev-parse", "--show-toplevel")
        if top_level.returncode != 0 or Path(top_level.stdout.strip()).resolve() != root:
            return GitState()
        head = _run_git(root, "rev-parse", "--verify", "HEAD")
        commit = head.stdout.strip().lower()
        if head.returncode != 0 or _COMMIT_PATTERN.fullmatch(commit) is None:
            return GitState()
        status = _run_git(root, "status", "--porcelain=v1", "--untracked-files=normal")
        dirty = bool(status.stdout) if status.returncode == 0 else None
        return GitState(commit=commit, dirty=dirty)
    except (OSError, subprocess.SubprocessError):
        return GitState()


def config_hash(config: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        config,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_hash(path: str | Path) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as stream:
            while chunk := stream.read(128 * 1024):
                digest.update(chunk)
    except OSError:
        return "unavailable"
    return digest.hexdigest()


def safe_environment() -> dict[str, str]:
    """Return reproducibility facts without environment variables or host paths."""
    return {
        "architecture": platform.machine() or "unknown",
        "os": platform.system() or "unknown",
        "os_release": platform.release() or "unknown",
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "sqlite": sqlite3.sqlite_version,
    }


def _gate(git: GitState, evidence_mode: str) -> dict[str, Any]:
    required = evidence_mode in {"candidate", "release"}
    reasons: list[str] = []
    if not git.available:
        reasons.append("git_commit_unavailable")
    if git.available and git.dirty is None:
        reasons.append("git_status_unavailable")
    elif git.dirty:
        reasons.append("git_worktree_dirty")
    if not required:
        return {"required": False, "status": "not_enforced", "reasons": reasons}
    return {
        "required": True,
        "status": "failed" if reasons else "passed",
        "reasons": reasons,
    }


def build_provenance(
    *,
    repo_root: str | Path,
    config: Mapping[str, Any],
    seed: int,
    model_name: str,
    model_mode: str,
    evidence_mode: str = "development",
) -> dict[str, Any]:
    if evidence_mode not in EVIDENCE_MODES:
        raise ValueError(f"unsupported evidence mode: {evidence_mode}")
    git = read_git_state(repo_root)
    return {
        "provenance_schema_version": PROVENANCE_SCHEMA_VERSION,
        "commit": git.commit,
        "git": git.to_dict(),
        "config_hash": config_hash(config),
        "seed": seed,
        "model": {"name": model_name, "mode": model_mode},
        "environment": safe_environment(),
        "evidence_mode": evidence_mode,
        "provenance_gate": _gate(git, evidence_mode),
    }


def provenance_gate_passed(report: Mapping[str, Any]) -> bool:
    return report.get("provenance_gate", {}).get("status") != "failed"


def credential_literals() -> tuple[str, ...]:
    """Collect known credentials only for redaction; values are never persisted."""
    return tuple(
        value
        for name in _CREDENTIAL_ENV_NAMES
        if (value := os.environ.get(name, "").strip())
    )


def _redact_private_paths(value: Any) -> Any:
    if isinstance(value, str):
        redacted = value
        for pattern in _PRIVATE_PATH_PATTERNS:
            redacted = pattern.sub("[REDACTED_PATH]", redacted)
        return redacted
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            normalized = normalize_key(key)
            is_path_key = normalized in _PATH_KEYS or normalized.endswith(("_path", "_directory"))
            if is_path_key and isinstance(child, str) and Path(child).is_absolute():
                result[str(key)] = "[REDACTED_PATH]"
            else:
                result[str(key)] = _redact_private_paths(child)
        return result
    if isinstance(value, list):
        return [_redact_private_paths(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_private_paths(item) for item in value)
    return value


def _strip_sensitive_fields(value: Any) -> Any:
    """Remove sensitive keys from publishable artifacts after value redaction."""
    if isinstance(value, dict):
        return {
            str(key): _strip_sensitive_fields(child)
            for key, child in value.items()
            if classify_key(key) not in {DataClass.CREDENTIAL, DataClass.STUDENT_PII}
        }
    if isinstance(value, list):
        return [_strip_sensitive_fields(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_strip_sensitive_fields(item) for item in value)
    return value


def sanitize_artifact(value: Any, *, secrets: tuple[str, ...] = ()) -> Any:
    """Redact values, drop sensitive fields, and remove private absolute paths."""
    redacted = RedactionPolicy(literal_secrets=secrets).redact(value)
    return _redact_private_paths(_strip_sensitive_fields(redacted))


__all__ = [
    "EVIDENCE_MODES",
    "PROVENANCE_SCHEMA_VERSION",
    "UNAVAILABLE_COMMIT",
    "GitState",
    "build_provenance",
    "config_hash",
    "credential_literals",
    "file_hash",
    "provenance_gate_passed",
    "read_git_state",
    "safe_environment",
    "sanitize_artifact",
]
