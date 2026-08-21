#!/bin/zsh
set -euo pipefail

script_dir=${0:A:h}
repo_root=${script_dir:h}
cd "$repo_root"

export UV_CACHE_DIR=${UV_CACHE_DIR:-/tmp/edu-agent-uv-cache}
stage8_dir=$(mktemp -d /tmp/edu-agent-stage8.XXXXXX)

test -x .venv/bin/python
uv run --frozen python --version
uv run --frozen python -c \
  "from pathlib import Path; from edu_agent.state import StateStore; StateStore(Path('$stage8_dir') / 'audit.db')"
uv run --frozen python scripts/audit_data_boundaries.py \
  "$stage8_dir/audit.db" "$stage8_dir/audit.db-wal" "$stage8_dir/audit.db-shm"

uv run --frozen ruff check \
  edu_agent/api.py edu_agent/data_audit.py edu_agent/data_classification.py \
  edu_agent/delegation/persistence.py edu_agent/observability \
  edu_agent/runtime/config.py edu_agent/runtime/security.py edu_agent/service.py \
  edu_agent/state/store.py edu_agent/state/trace_index.py \
  scripts/audit_data_boundaries.py \
  scripts/benchmark_trace_scaling.py scripts/eval_system.py \
  tests/test_observability_api.py tests/test_stage8_boundaries_recovery_trace.py

uv run --frozen python -m pytest \
  tests/test_stage8_boundaries_recovery_trace.py tests/test_observability_api.py -q
uv run --frozen python -m pytest tests -q
uv run --frozen python scripts/benchmark_trace_scaling.py \
  --events 10000 --page-size 100 --output artifacts/trace-scaling.json
uv run --frozen python scripts/production_runtime_demo.py
uv run --frozen python scripts/mcp_demo.py

# Stage 7 remains the explicit regression boundary, including the real Docker
# attempt and its honest not_verified fallback when no backend is available.
zsh scripts/accept_stage7.sh

print "stage8 acceptance passed; transient files: $stage8_dir"
