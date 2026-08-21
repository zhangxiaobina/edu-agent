from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_r1_fake_provider_acceptance_is_offline_and_complete():
    environment = os.environ.copy()
    for name in tuple(environment):
        if name.endswith("_API_KEY") or name in {
            "OPENAI_API_KEY",
            "DASHSCOPE_API_KEY",
            "VLLM_API_KEY",
        }:
            environment[name] = ""
    result = subprocess.run(
        [sys.executable, "scripts/accept_r1_fake_provider.py"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["gate"] == "passed"
    assert report["api_modes"] == ["chat_completions", "responses"]
    assert report["equivalent_tool_calls"] == 2
    assert report["retry_after_seconds"] == 7
    assert report["route_breaker_isolated"] is True
    assert report["compatible_fallback"] is True
    assert report["incompatible_fallback_gaps"] == {
        "api-mode": "api_mode_request_shape",
        "context": "context_window",
        "structured": "structured_output",
        "tool": "tool_calling",
    }
    assert report["trace_redacted"] is True
    assert report["attempt_events"] >= 10
