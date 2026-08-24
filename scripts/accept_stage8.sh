#!/bin/zsh
set -euo pipefail

script_dir=${0:A:h}
repo_root=${script_dir:h}
cd "$repo_root"
source scripts/acceptance_common.sh

acceptance_dry_run=0
if (( $# > 1 )) || { (( $# == 1 )) && [[ $1 != --dry-run ]]; }; then
  acceptance_die "usage: zsh scripts/accept_stage8.sh [--dry-run]"
  exit 2
fi
if (( $# == 1 )); then
  acceptance_dry_run=1
fi

stage8_parent=${TMPDIR:-/tmp}
stage8_root=$(acceptance_make_temp_dir edu-agent-stage8)
acceptance_stage8_cleanup() {
  local exit_code=$?
  trap - ZERR EXIT
  if ! acceptance_cleanup_owned_dir "$stage8_root" "$stage8_parent" edu-agent-stage8; then
    (( exit_code == 0 )) && exit_code=1
  fi
  return $exit_code
}
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
trap acceptance_stage8_cleanup ZERR EXIT

acceptance_configure_environment "$stage8_root" "$repo_root"
acceptance_scrub_credentials
prepare_args=()
[[ $acceptance_dry_run == 1 ]] && prepare_args=(--dry-run)
acceptance_run_internal zsh scripts/prepare_acceptance.sh "${prepare_args[@]}"
export EDU_AGENT_ACCEPTANCE_PREPARED=1

acceptance_uv_run python -c \
  'from pathlib import Path; from edu_agent.state import StateStore; StateStore(Path(__import__("sys").argv[1]))' \
  "$stage8_root/audit.db"
acceptance_uv_run python scripts/audit_data_boundaries.py \
  --fail-on-findings \
  "$stage8_root/audit.db" "$stage8_root/audit.db-wal" "$stage8_root/audit.db-shm"

state_backup="$stage8_root/state-backup"
state_restore="$stage8_root/state-restore"
acceptance_uv_run python scripts/state_maintenance.py backup \
  --state "$stage8_root/audit.db" --artifacts "$stage8_root/state-artifacts" \
  --target "$state_backup"
acceptance_uv_run python scripts/state_maintenance.py verify-backup \
  --backup "$state_backup"
acceptance_uv_run python scripts/state_maintenance.py restore \
  --backup "$state_backup" --target-dir "$state_restore"
acceptance_uv_run python scripts/state_maintenance.py verify-state \
  --state "$state_restore/state.db" --artifacts "$state_restore/artifacts"
acceptance_uv_run python scripts/state_maintenance.py gc \
  --state "$state_restore/state.db" --artifacts "$state_restore/artifacts" \
  --terminal-age-seconds 2592000 --artifact-age-seconds 2592000 --batch-size 100

lineage_output="$EDU_AGENT_ACCEPTANCE_ARTIFACT_DIR/eval-lineage.json"
acceptance_uv_run python scripts/audit_eval_lineage.py \
  --quiet --output "$lineage_output"
acceptance_uv_run python scripts/eval_context_fidelity.py \
  --output "$stage8_root/context-fidelity.json" \
  --thresholds tests/fixtures/context_fidelity_thresholds.json

acceptance_uv_run ruff check \
  edu_agent/api.py edu_agent/data_audit.py edu_agent/data_classification.py \
  edu_agent/delegation/persistence.py edu_agent/eval/corpus.py edu_agent/eval/harness.py \
  edu_agent/eval/lineage.py edu_agent/eval/metrics.py edu_agent/eval/provenance.py \
  edu_agent/eval/tasks.py edu_agent/eval/tasks_derived.py edu_agent/eval/tasks_test.py \
  edu_agent/observability \
  edu_agent/runtime/config.py edu_agent/runtime/security.py edu_agent/service.py \
  edu_agent/state/maintenance.py edu_agent/state/store.py edu_agent/state/trace_index.py \
  scripts/audit_data_boundaries.py scripts/audit_eval_lineage.py \
  scripts/benchmark_trace_scaling.py scripts/eval_context_fidelity.py \
  scripts/eval_system.py scripts/state_maintenance.py \
  tests/test_acceptance_scripts.py tests/test_ci_provenance.py tests/test_eval_lineage.py \
  tests/test_lifecycle.py tests/test_production_runtime_demo.py \
  tests/test_r46_storage_maintenance.py \
  tests/test_stage8_boundaries_recovery_trace.py

acceptance_uv_run python -m pytest -p no:cacheprovider \
  tests/test_eval_lineage.py tests/test_acceptance_scripts.py \
  tests/test_stage8_boundaries_recovery_trace.py -q
acceptance_uv_run python -m pytest -p no:cacheprovider \
  tests/test_context_accounting.py tests/test_context_checkpoint.py \
  tests/test_context_fidelity.py tests/test_r43_context_policy.py \
  tests/test_r43_context_recovery.py tests/test_run_budget_ledger.py \
  tests/test_lifecycle.py tests/test_r46_storage_maintenance.py -q
acceptance_uv_run python scripts/benchmark_trace_scaling.py \
  --events 10000 --page-size 100 \
  --output "$EDU_AGENT_ACCEPTANCE_ARTIFACT_DIR/trace-scaling.json"

# R2 is an internal recovery boundary. The complete suite still runs once below.
r2_args=(--from-stage8)
[[ $acceptance_dry_run == 1 ]] && r2_args+=(--dry-run)
acceptance_run_internal zsh scripts/accept_r2.sh "${r2_args[@]}"

# Highest-stage acceptance explicitly includes the preceding regression boundary.
stage7_args=(--from-stage8)
[[ $acceptance_dry_run == 1 ]] && stage7_args+=(--dry-run)
acceptance_run_internal zsh scripts/accept_stage7.sh "${stage7_args[@]}"

acceptance_uv_run python scripts/audit_data_boundaries.py \
  --fail-on-findings \
  "$EDU_AGENT_ACCEPTANCE_ARTIFACT_DIR"

# Run the complete suite exactly once. Stage-specific tests above provide early boundary failures.
acceptance_uv_run python -m pytest -p no:cacheprovider tests -q

if [[ $acceptance_dry_run == 1 ]]; then
  print -r -- "stage8 acceptance dry-run complete; no gate result; temporary state cleaned on exit"
else
  print -r -- "stage8 acceptance passed; temporary state cleaned on exit"
fi
