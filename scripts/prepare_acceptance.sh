#!/bin/zsh
set -euo pipefail

script_dir=${0:A:h}
repo_root=${script_dir:h}
cd "$repo_root"
source scripts/acceptance_common.sh

acceptance_dry_run=0
if (( $# > 1 )) || { (( $# == 1 )) && [[ $1 != --dry-run ]]; }; then
  acceptance_die "usage: zsh scripts/prepare_acceptance.sh [--dry-run]"
  exit 2
fi
if (( $# == 1 )); then
  acceptance_dry_run=1
fi

if ! command -v uv >/dev/null 2>&1; then
  acceptance_die \
    "uv is required; install it from https://docs.astral.sh/uv/getting-started/installation/ and retry"
  exit 1
fi
if [[ ! -f .python-version ]]; then
  acceptance_die ".python-version is missing; restore the tracked file and retry"
  exit 1
fi
if [[ ! -f uv.lock ]]; then
  acceptance_die "uv.lock is missing; restore the tracked lockfile and retry"
  exit 1
fi

required_python=$(<.python-version)
if [[ ! $required_python =~ '^[0-9]+\.[0-9]+$' ]]; then
  acceptance_die ".python-version must contain one major.minor version, found: $required_python"
  exit 1
fi

minimum_python=$(sed -nE 's/^requires-python = ">=([0-9]+\.[0-9]+)"$/\1/p' pyproject.toml)
if [[ -n $minimum_python ]]; then
  required_major=${required_python%%.*}
  required_minor=${required_python#*.}
  minimum_major=${minimum_python%%.*}
  minimum_minor=${minimum_python#*.}
  if (( required_major < minimum_major ||
        (required_major == minimum_major && required_minor < minimum_minor) )); then
    acceptance_die \
      "Python $required_python from .python-version does not satisfy requires-python >=$minimum_python"
    exit 1
  fi
fi

export UV_PROJECT_ENVIRONMENT=${UV_PROJECT_ENVIRONMENT:-"$repo_root/.venv"}
if [[ -z ${UV_CACHE_DIR:-} ]]; then
  export UV_NO_CACHE=1
fi
acceptance_scrub_credentials
if ! acceptance_run uv --version; then
  acceptance_die "uv is present but cannot run; repair the uv installation and retry"
  exit 1
fi
if ! acceptance_run uv lock --check; then
  acceptance_die \
    "uv.lock is out of date; run 'uv lock', review and commit the lockfile, then retry"
  exit 1
fi
if [[ $acceptance_dry_run == 1 ]]; then
  acceptance_run uv python find --managed-python "$required_python"
  acceptance_run uv python install "$required_python"
elif ! acceptance_run uv python find --managed-python "$required_python" >/dev/null; then
  if ! acceptance_run uv python install "$required_python"; then
    acceptance_die \
      "Python $required_python is unavailable; run 'uv python install $required_python' with network access and retry"
    exit 1
  fi
fi
if ! acceptance_run uv sync --frozen --python "$required_python" --managed-python \
  --extra dev --extra mcp; then
  acceptance_die \
    "dependency sync failed for Python $required_python; check interpreter compatibility and uv.lock"
  exit 1
fi

version_command=(uv run --frozen --offline python -c \
  'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
if [[ $acceptance_dry_run == 1 ]]; then
  acceptance_run "${version_command[@]}"
  print -r -- "acceptance preparation dry-run complete (Python $required_python, uv.lock frozen)"
  exit 0
fi

if ! actual_python=$(acceptance_run "${version_command[@]}"); then
  acceptance_die "the synchronized environment cannot start Python $required_python"
  exit 1
fi
if [[ $actual_python != $required_python ]]; then
  acceptance_die \
    "synchronized Python is $actual_python, expected $required_python; remove the incompatible project environment and retry"
  exit 1
fi

print -r -- "acceptance environment ready (Python $actual_python, uv.lock frozen)"
