from __future__ import annotations

import sys
from pathlib import Path

import pytest

from scripts import eval_real_r52


@pytest.mark.parametrize("evidence_mode", ["candidate", "release"])
def test_r52_publishable_modes_default_to_ignored_artifact_directory(evidence_mode):
    assert eval_real_r52._resolve_output_path(
        None,
        evidence_mode=evidence_mode,
    ) == Path("ci-artifacts/r52-real-model-eval.json")


@pytest.mark.parametrize("evidence_mode", ["candidate", "release"])
def test_r52_publishable_modes_reject_tracked_artifact_output_before_git_or_credentials(
    monkeypatch, evidence_mode
):
    observed: list[str] = []
    monkeypatch.setattr(
        eval_real_r52,
        "build_provenance",
        lambda **kwargs: observed.append(kwargs["evidence_mode"]),
    )
    monkeypatch.delenv(eval_real_r52.CREDENTIAL_ENV, raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eval_real_r52.py",
            "--evidence-mode",
            evidence_mode,
            "--output",
            "artifacts/r52-real-model-eval.json",
        ],
    )

    with pytest.raises(SystemExit, match="must use ci-artifacts"):
        eval_real_r52.main()

    assert observed == []


@pytest.mark.parametrize("evidence_mode", ["candidate", "release"])
def test_r52_publishable_modes_fail_before_credentials_when_git_is_dirty(
    monkeypatch, evidence_mode
):
    observed: list[str] = []

    def fake_provenance(**kwargs):
        observed.append(kwargs["evidence_mode"])
        return {
            "provenance_gate": {
                "status": "failed",
                "reasons": ["git_worktree_dirty"],
            }
        }

    monkeypatch.setattr(eval_real_r52, "build_provenance", fake_provenance)
    monkeypatch.delenv(eval_real_r52.CREDENTIAL_ENV, raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["eval_real_r52.py", "--evidence-mode", evidence_mode],
    )

    with pytest.raises(SystemExit, match="Git provenance gate failed"):
        eval_real_r52.main()

    assert observed == [evidence_mode]
