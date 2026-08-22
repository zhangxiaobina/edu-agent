#!/bin/zsh
set -euo pipefail

script_dir=${0:A:h}
repo_root=${script_dir:h}
cd "$repo_root"
source scripts/acceptance_common.sh

acceptance_dry_run=0
from_stage8=0
for argument in "$@"; do
  case $argument in
    --dry-run) acceptance_dry_run=1 ;;
    --from-stage8) from_stage8=1 ;;
    *)
      acceptance_die "usage: zsh scripts/accept_r2.sh [--dry-run] [--from-stage8]"
      exit 2
      ;;
  esac
done

r2_root=
r2_parent=
if [[ $from_stage8 == 1 ]]; then
  if [[ ${EDU_AGENT_ACCEPTANCE_PREPARED:-0} != 1 ||
        -z ${EDU_AGENT_ACCEPTANCE_ROOT:-} ||
        ! -d ${EDU_AGENT_ACCEPTANCE_ROOT:-} ]]; then
    acceptance_die "--from-stage8 requires the prepared Stage 8 environment"
    exit 1
  fi
  r2_root=$EDU_AGENT_ACCEPTANCE_ROOT
  if [[ ${r2_root:t} != edu-agent-stage8.* ||
        ${EDU_AGENT_DB:-} != "$r2_root/edu.db" ||
        ${TMPDIR:-} != "$r2_root/runtime" ||
        ${UV_CACHE_DIR:-} != "$r2_root/uv-cache" ||
        ${UV_PROJECT_ENVIRONMENT:-} != "$repo_root/.venv" ]]; then
    acceptance_die "--from-stage8 received unsafe or inconsistent paths"
    exit 1
  fi
else
  r2_parent=${TMPDIR:-/tmp}
  r2_root=$(acceptance_make_temp_dir edu-agent-r2)
  acceptance_r2_cleanup() {
    local exit_code=$?
    trap - ZERR EXIT
    if ! acceptance_cleanup_owned_dir "$r2_root" "$r2_parent" edu-agent-r2; then
      (( exit_code == 0 )) && exit_code=1
    fi
    return $exit_code
  }
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap acceptance_r2_cleanup ZERR EXIT
  acceptance_configure_environment "$r2_root" "$repo_root"
  acceptance_scrub_credentials
  prepare_args=()
  [[ $acceptance_dry_run == 1 ]] && prepare_args=(--dry-run)
  acceptance_run_internal zsh scripts/prepare_acceptance.sh "${prepare_args[@]}"
  export EDU_AGENT_ACCEPTANCE_PREPARED=1
fi

acceptance_uv_run ruff check \
  edu_agent/agent/graph.py edu_agent/agent/loop_journal.py edu_agent/api.py \
  edu_agent/observability/events.py edu_agent/observability/run_stream.py \
  edu_agent/runtime/recovery.py edu_agent/runtime/transactions.py \
  edu_agent/service.py edu_agent/state/journal.py edu_agent/state/store.py \
  edu_agent/state/tool_messages.py edu_agent/state/turn_finalizer.py \
  scripts/r2_recovery_demo.py tests/test_r2_recovery.py

acceptance_uv_run python -m pytest -p no:cacheprovider \
  tests/test_run_events.py \
  tests/test_run_journal.py \
  tests/test_agent_tool_messages.py \
  tests/test_turn_finalizer.py \
  tests/test_provider_streaming.py \
  tests/test_cancellation.py \
  tests/test_api_sse_cancellation.py \
  tests/test_transactional_tools.py \
  tests/test_stage8_boundaries_recovery_trace.py \
  tests/test_r2_recovery.py -q

acceptance_uv_run python scripts/r2_recovery_demo.py

if [[ $acceptance_dry_run == 1 ]]; then
  print -r -- "R2 internal gate dry-run complete; no gate result"
else
  print -r -- "R2 internal gate passed"
fi
