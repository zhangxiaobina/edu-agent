"""Fail-closed API container preflight and idempotent state migration.

This command deliberately does not construct the model engine or call a remote
Provider. ``EduAgentService`` performs the same migration checks again during
normal startup; keeping this step separate makes a failed migration/readonly
volume a process-start failure instead of a misleading ready container.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from edu_agent.runtime.config import load_config
from edu_agent.state import StateStore


def _probe_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".preflight-", dir=path, delete=True):
        pass


def run_preflight(config_path: str | None = None) -> dict[str, object]:
    path = config_path or os.environ.get("EDU_AGENT_CONFIG") or "/etc/edu-agent/config.toml"
    if not Path(path).is_file():
        raise FileNotFoundError("container configuration is unavailable")
    config = load_config(path)
    state_path = config.state_path
    artifact_path = config.artifact_path
    _probe_directory(state_path.parent)
    _probe_directory(artifact_path)
    store = StateStore(state_path)
    with store.connect() as connection:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
    migration = store.migration_ready()
    writable = store.writable_ready()
    if integrity != "ok" or foreign_keys or not migration or not writable:
        raise RuntimeError("state preflight failed")
    return {
        "status": "passed",
        "schema": "current",
        "migration": True,
        "state_db_writable": True,
        "artifact_directory_writable": True,
    }


def main() -> int:
    try:
        report = run_preflight()
    except Exception as error:  # keep paths, SQL and provider details out of logs
        print(
            json.dumps(
                {"status": "failed", "error": type(error).__name__},
                sort_keys=True,
            ),
            flush=True,
        )
        return 78
    print(json.dumps(report, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
