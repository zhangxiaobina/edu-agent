#!/bin/zsh
set -euo pipefail

script_dir=${0:A:h}
repo_root=${script_dir:h}
cd "$repo_root"

uv run --frozen python --version
uv run --frozen python -m edu_agent.data.generate
uv run --frozen ruff check \
  edu_agent/api.py edu_agent/observability edu_agent/eval/metrics.py \
  edu_agent/delegation/persistence.py edu_agent/runtime/artifacts.py \
  edu_agent/runtime/config.py edu_agent/runtime/security.py edu_agent/service.py \
  edu_agent/scheduler.py edu_agent/state/store.py scripts/api_server.py \
  scripts/eval_system.py scripts/production_runtime_demo.py \
  scripts/trace_inspector.py tests/test_observability_api.py
uv run --frozen python -m pytest tests/test_observability_api.py -q
uv run --frozen python -m pytest tests -q

stage7_demo_state=$(mktemp /tmp/edu-agent-stage7-production.XXXXXX)
EDU_AGENT_PRODUCTION_DEMO_STATE=$stage7_demo_state \
  uv run --frozen python scripts/production_runtime_demo.py
uv run --frozen python scripts/plan_runtime_demo.py
uv run --frozen python scripts/rag_runtime_demo.py
uv run --frozen python scripts/transactional_tools_demo.py
uv run --frozen python scripts/runtime_recovery_demo.py
uv run --frozen python scripts/multi_agent_demo.py
uv run --frozen python scripts/mcp_demo.py
sandbox_args=()
if uv run --frozen python scripts/code_sandbox_demo.py \
  --provider docker --e2e --require-all > /tmp/edu-agent-stage7-sandbox.json; then
  sandbox_args=(--sandbox-report /tmp/edu-agent-stage7-sandbox.json)
else
  print '真实 Docker 代码执行后端未通过；综合评测将标记 sandbox=not_verified。' >&2
fi

uv run --frozen python scripts/trace_inspector.py \
  --state "$stage7_demo_state" --actor teacher-demo \
  --tenant default --format summary --limit 20
uv run --frozen python scripts/eval_system.py \
  "${sandbox_args[@]}" --output artifacts/system-eval.json
