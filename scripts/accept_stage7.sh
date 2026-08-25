#!/bin/zsh
set -euo pipefail

script_dir=${0:A:h}
repo_root=${script_dir:h}
cd "$repo_root"
source scripts/acceptance_common.sh

acceptance_dry_run=0
from_stage8=0
acceptance_evidence_mode=${EDU_AGENT_ACCEPTANCE_EVIDENCE_MODE:-development}
while (( $# > 0 )); do
  argument=$1
  case $argument in
    --dry-run) acceptance_dry_run=1; shift; continue ;;
    --from-stage8) from_stage8=1; shift; continue ;;
    --evidence-mode)
      (( $# >= 2 )) || { acceptance_die "--evidence-mode requires development, candidate, or release"; exit 2; }
      acceptance_evidence_mode=$2
      [[ $acceptance_evidence_mode == development || $acceptance_evidence_mode == candidate || $acceptance_evidence_mode == release ]] || {
        acceptance_die "unsupported evidence mode: $acceptance_evidence_mode"
        exit 2
      }
      shift 2
      continue
      ;;
    *)
      acceptance_die "usage: zsh scripts/accept_stage7.sh [--dry-run] [--from-stage8] [--evidence-mode development|candidate|release]"
      exit 2
      ;;
  esac
done

stage7_root=
stage7_parent=
if [[ $from_stage8 == 1 ]]; then
  if [[ ${EDU_AGENT_ACCEPTANCE_PREPARED:-0} != 1 ||
        -z ${EDU_AGENT_ACCEPTANCE_ROOT:-} ||
        ! -d ${EDU_AGENT_ACCEPTANCE_ROOT:-} ]]; then
    acceptance_die "--from-stage8 requires the prepared Stage 8 environment"
    exit 1
  fi
  stage7_root=$EDU_AGENT_ACCEPTANCE_ROOT
  if [[ ${stage7_root:t} != edu-agent-stage8.* ||
        ${EDU_AGENT_DB:-} != "$stage7_root/edu.db" ||
        ${EDU_AGENT_PRODUCTION_DEMO_STATE:-} != "$stage7_root/production-demo.db" ||
        ${TMPDIR:-} != "$stage7_root/runtime" ||
        ${UV_CACHE_DIR:-} != "$stage7_root/uv-cache" ||
        ${UV_PROJECT_ENVIRONMENT:-} != "$repo_root/.venv" ||
        ${EDU_AGENT_ACCEPTANCE_ARTIFACT_DIR:-} != "$repo_root/artifacts" ]]; then
    acceptance_die "--from-stage8 received unsafe or inconsistent paths"
    exit 1
  fi
  if [[ ${EDU_AGENT_ACCEPTANCE_EVIDENCE_MODE:-development} != "$acceptance_evidence_mode" ]]; then
    acceptance_die "--from-stage8 evidence mode does not match prepared environment"
    exit 1
  fi
else
  stage7_parent=${TMPDIR:-/tmp}
  stage7_root=$(acceptance_make_temp_dir edu-agent-stage7)
  acceptance_stage7_cleanup() {
    local exit_code=$?
    trap - ZERR EXIT
    if ! acceptance_cleanup_owned_dir "$stage7_root" "$stage7_parent" edu-agent-stage7; then
      (( exit_code == 0 )) && exit_code=1
    fi
    return $exit_code
  }
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap acceptance_stage7_cleanup ZERR EXIT
  acceptance_configure_environment "$stage7_root" "$repo_root"
  acceptance_scrub_credentials
  prepare_args=()
  [[ $acceptance_dry_run == 1 ]] && prepare_args=(--dry-run)
  acceptance_run_internal zsh scripts/prepare_acceptance.sh "${prepare_args[@]}"
  export EDU_AGENT_ACCEPTANCE_PREPARED=1
fi

# Stage 7 is an internal regression boundary. The complete suite runs once in Stage 8.
acceptance_uv_run python -m edu_agent.data.generate --out "$EDU_AGENT_DB"
acceptance_uv_run ruff check \
  edu_agent/api.py edu_agent/observability edu_agent/eval/metrics.py \
  edu_agent/delegation/persistence.py edu_agent/runtime/artifacts.py \
  edu_agent/runtime/config.py edu_agent/runtime/security.py edu_agent/service.py \
  edu_agent/scheduler.py edu_agent/state/store.py scripts/api_server.py \
  scripts/eval_system.py scripts/production_runtime_demo.py \
  scripts/trace_inspector.py tests/test_observability_api.py
acceptance_uv_run python -m pytest -p no:cacheprovider tests/test_observability_api.py -q

acceptance_uv_run python scripts/production_runtime_demo.py
acceptance_uv_run python scripts/plan_runtime_demo.py
acceptance_uv_run python scripts/rag_runtime_demo.py
acceptance_uv_run python scripts/transactional_tools_demo.py
acceptance_uv_run python scripts/runtime_recovery_demo.py
acceptance_uv_run python scripts/multi_agent_demo.py
acceptance_uv_run python scripts/mcp_demo.py

sandbox_report="$stage7_root/sandbox.json"
sandbox_args=()
expected_sandbox_status=not_verified
if [[ $acceptance_dry_run == 1 ]]; then
  acceptance_uv_run python scripts/code_sandbox_demo.py \
    --provider docker --e2e --require-all > "$sandbox_report"
  print -u2 -r -- \
    'Docker was not executed during dry-run; system evaluation remains sandbox=not_verified.'
elif acceptance_uv_run python scripts/code_sandbox_demo.py \
  --provider docker --e2e --require-all > "$sandbox_report"; then
  sandbox_args=(--sandbox-report "$sandbox_report")
  expected_sandbox_status=verified
else
  print -u2 -r -- \
    'real Docker code execution backend unavailable; system evaluation will record sandbox=not_verified.'
fi

system_eval_output="$EDU_AGENT_ACCEPTANCE_ARTIFACT_DIR/system-eval.json"
system_eval_args=()
system_eval_args+=(--evidence-mode "$acceptance_evidence_mode")
if [[ -n ${EDU_AGENT_ACCEPTANCE_TRACE_REPORT:-} ]]; then
  system_eval_args+=(--trace-report "$EDU_AGENT_ACCEPTANCE_TRACE_REPORT")
fi
if [[ -n ${EDU_AGENT_ACCEPTANCE_CONTEXT_REPORT:-} ]]; then
  system_eval_args+=(--context-report "$EDU_AGENT_ACCEPTANCE_CONTEXT_REPORT")
fi
acceptance_uv_run python scripts/trace_inspector.py \
  --state "$EDU_AGENT_PRODUCTION_DEMO_STATE" --actor teacher-demo \
  --tenant default --format summary --limit 20
acceptance_uv_run python scripts/eval_system.py \
  "${sandbox_args[@]}" "${system_eval_args[@]}" --output "$system_eval_output"
acceptance_uv_run python -c \
  'import json, sys; status=json.load(open(sys.argv[1], encoding="utf-8"))["sandbox"]["status"]; assert status == sys.argv[2], (status, sys.argv[2])' \
  "$system_eval_output" "$expected_sandbox_status"

if [[ $acceptance_dry_run == 1 ]]; then
  print -r -- \
    "stage7 internal regression dry-run complete (sandbox=not_verified; no gate result)"
else
  print -r -- "stage7 internal regression passed (sandbox=$expected_sandbox_status)"
fi
