#!/bin/sh
set -eu

# Docker secrets are mounted as files. The application intentionally keeps its
# existing CredentialRef environment contract, so values are exported only at
# process start and are never copied into the image or config file.
load_secret_file() {
    file_var="$1"
    target_var="$2"
    required="$3"
    case "$file_var" in
        EDU_AGENT_DEMO_TOKEN_FILE) file_path="${EDU_AGENT_DEMO_TOKEN_FILE-}" ;;
        EDU_AGENT_API_KEY_FILE) file_path="${EDU_AGENT_API_KEY_FILE-}" ;;
        EDU_AGENT_FALLBACK_API_KEY_FILE) file_path="${EDU_AGENT_FALLBACK_API_KEY_FILE-}" ;;
        EDU_AGENT_JOBE_TOKEN_FILE) file_path="${EDU_AGENT_JOBE_TOKEN_FILE-}" ;;
        *) echo "unsupported secret reference" >&2; exit 64 ;;
    esac
    if [ -z "$file_path" ]; then
        if [ "$required" = "1" ]; then
            echo "missing required secret file reference: ${file_var}" >&2
            exit 64
        fi
        return 0
    fi
    if [ ! -f "$file_path" ]; then
        echo "secret file is unavailable: ${file_var}" >&2
        exit 64
    fi
    value=$(cat "$file_path")
    if [ -z "$value" ]; then
        echo "secret file is empty: ${file_var}" >&2
        exit 64
    fi
    export "${target_var}=${value}"
}

load_secret_file EDU_AGENT_DEMO_TOKEN_FILE EDU_AGENT_DEMO_TOKEN 1
load_secret_file EDU_AGENT_API_KEY_FILE EDU_AGENT_API_KEY 1
load_secret_file EDU_AGENT_FALLBACK_API_KEY_FILE EDU_AGENT_FALLBACK_API_KEY 0
load_secret_file EDU_AGENT_JOBE_TOKEN_FILE EDU_AGENT_JOBE_TOKEN 0

python /app/scripts/container_preflight.py
exec python /app/scripts/api_server.py
