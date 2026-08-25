#!/bin/zsh

# Shared helpers for the acceptance entry points. This file is sourced, not run.

acceptance_die() {
  print -u2 -r -- "acceptance error: $*"
  return 1
}

acceptance_log_command() {
  local rendered="+"
  local argument
  local safe_argument
  for argument in "$@"; do
    safe_argument=$argument
    if [[ -n ${repo_root:-} && $safe_argument == "$repo_root"* ]]; then
      safe_argument="REPOSITORY${safe_argument#$repo_root}"
    elif [[ $safe_argument == /* ]]; then
      safe_argument="PRIVATE_PATH/${safe_argument:t}"
    fi
    rendered+=" ${(q)safe_argument}"
  done
  print -u2 -r -- "$rendered"
}

acceptance_run() {
  acceptance_log_command "$@"
  if [[ ${acceptance_dry_run:-0} == 1 ]]; then
    return 0
  fi
  if command "$@"; then
    return 0
  else
    local exit_code=$?
    return $exit_code
  fi
}

# Internal scripts still run during a dry-run so the complete call graph is exercised.
acceptance_run_internal() {
  acceptance_log_command "$@"
  if command "$@"; then
    return 0
  else
    local exit_code=$?
    return $exit_code
  fi
}

acceptance_uv_run() {
  if acceptance_run uv run --frozen --offline "$@"; then
    return 0
  else
    local exit_code=$?
    return $exit_code
  fi
}

acceptance_make_temp_dir() {
  local prefix=$1
  local base=${TMPDIR:-/tmp}
  if [[ ! -d $base || ! -w $base ]]; then
    acceptance_die "temporary directory base is not writable: $base"
    return 1
  fi
  umask 077
  mktemp -d "${base%/}/${prefix}.XXXXXX" || {
    acceptance_die "could not create a private temporary directory under $base"
    return 1
  }
}

acceptance_cleanup_owned_dir() {
  local directory=$1
  local parent=$2
  local prefix=$3
  [[ -n $directory && -d $directory ]] || return 0

  local resolved_directory=${directory:A}
  local resolved_parent=${parent:A}
  if [[ ${resolved_directory:h} != $resolved_parent || ${resolved_directory:t} != "$prefix".* ]]; then
    print -u2 -r -- "acceptance cleanup refused unexpected path: $resolved_directory"
    return 1
  fi
  command rm -rf -- "$resolved_directory"
}

acceptance_artifact_directory() {
  local repo_root=$1
  local evidence_mode=${2:-development}
  case $evidence_mode in
    development) print -r -- "$repo_root/artifacts" ;;
    candidate|release) print -r -- "$repo_root/ci-artifacts" ;;
    *)
      acceptance_die "unsupported evidence mode: $evidence_mode"
      return 2
      ;;
  esac
}

acceptance_configure_environment() {
  local root=$1
  local repo_root=$2
  local evidence_mode=${acceptance_evidence_mode:-development}
  local artifact_directory
  artifact_directory=$(acceptance_artifact_directory "$repo_root" "$evidence_mode")
  command mkdir -p -- "$root/runtime" "$root/uv-cache" "$root/ruff-cache"

  export EDU_AGENT_ACCEPTANCE_ROOT=$root
  export EDU_AGENT_DB="$root/edu.db"
  export EDU_AGENT_PRODUCTION_DEMO_STATE="$root/production-demo.db"
  export EDU_AGENT_ACCEPTANCE_ARTIFACT_DIR=$artifact_directory
  export TMPDIR="$root/runtime"
  export RUFF_CACHE_DIR="$root/ruff-cache"
  export UV_CACHE_DIR="$root/uv-cache"
  export UV_PROJECT_ENVIRONMENT="$repo_root/.venv"
  export PYTHONDONTWRITEBYTECODE=1
  export PYTHONNOUSERSITE=1
  export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1

  command mkdir -p -- "$EDU_AGENT_ACCEPTANCE_ARTIFACT_DIR"
  unset \
    VIRTUAL_ENV PYTEST_ADDOPTS UV_PYTHON UV_OFFLINE UV_NO_SYNC UV_NO_CACHE \
    UV_PYTHON_DOWNLOADS UV_PYTHON_INSTALL_DIR UV_SYSTEM_PYTHON \
    UV_MANAGED_PYTHON UV_NO_MANAGED_PYTHON
}

acceptance_scrub_credentials() {
  unset \
    EDU_AGENT_API_KEY EDU_AGENT_FALLBACK_API_KEY EDU_AGENT_BASE_URL \
    EDU_AGENT_MODEL EDU_AGENT_JOBE_TOKEN EDU_AGENT_JOBE_ENDPOINT \
    EDU_AGENT_CONFIG EDU_AGENT_DEMO_TOKEN OPENAI_API_KEY OPENAI_BASE_URL \
    DASHSCOPE_API_KEY VLLM_API_KEY ANTHROPIC_API_KEY GOOGLE_API_KEY \
    AZURE_OPENAI_API_KEY AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY HF_TOKEN \
    LANGCHAIN_API_KEY LANGSMITH_API_KEY OTEL_EXPORTER_OTLP_HEADERS \
    GH_TOKEN GITHUB_TOKEN
  export EDU_AGENT_ENGINE=mock
  export EDU_AGENT_TOOLSOURCE=local
}
