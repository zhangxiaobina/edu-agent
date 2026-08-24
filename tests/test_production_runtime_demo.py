from __future__ import annotations

from edu_agent.state import StateStore
from scripts.production_runtime_demo import main


def test_production_runtime_demo_fits_frozen_schema_and_compacts(tmp_path, monkeypatch):
    state_path = tmp_path / "production-demo.db"
    monkeypatch.setenv("EDU_AGENT_PRODUCTION_DEMO_STATE", str(state_path))

    main()

    with StateStore(state_path, read_only=True).connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM context_checkpoints"
        ).fetchone()[0] >= 1
        assert connection.execute(
            "SELECT COUNT(*) FROM runs WHERE status='completed'"
        ).fetchone()[0] == 5
