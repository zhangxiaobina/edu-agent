"""离线演示可靠运行时：记忆、压缩、Artifact、状态和计划任务。"""
from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from edu_agent.engine.base import EngineResponse
from edu_agent.engine.mock import MockEngine
from edu_agent.runtime.config import AppConfig, RuntimeConfig, StorageConfig
from edu_agent.service import EduAgentService


def policy(messages, tools, step):
    memory = "表格" if "表格" in messages[-1]["content"] else "默认格式"
    return EngineResponse(content=f"已按{memory}生成教学分析。")


def main() -> None:
    state_path = Path(
        os.environ.get(
            "EDU_AGENT_PRODUCTION_DEMO_STATE",
            str(Path(tempfile.gettempdir()) / "edu_agent_production_demo.db"),
        )
    )
    state_path.unlink(missing_ok=True)
    service = EduAgentService(
        MockEngine(policy),
        config=AppConfig(
            runtime=RuntimeConfig(
                context_token_budget=512,
                compression_trigger_ratio=0.5,
                compression_keep_recent=2,
            ),
            storage=StorageConfig(
                state_path=str(state_path),
                artifact_path=str(state_path.parent / "edu-agent-demo-artifacts"),
            ),
        ),
    )
    service.remember("教师偏好使用 Markdown 表格", actor_id="teacher-demo", importance=0.9)

    first = service.chat("分析三班本周学情", actor_id="teacher-demo", role="teacher")
    second = service.chat(
        "继续给出干预建议" + "，并解释每项建议的依据" * 80,
        actor_id="teacher-demo",
        role="teacher",
        session_id=first.session_id,
    )
    third = service.chat(
        "最后汇总本次干预方案",
        actor_id="teacher-demo",
        role="teacher",
        session_id=first.session_id,
    )
    job_id = service.schedule(
        name="教学周报",
        prompt="生成三班教学周报",
        actor_id="teacher-demo",
        role="teacher",
        next_run_at=datetime.now(UTC),
    )
    scheduled = service.scheduler(worker_id="demo-worker").tick()

    print(f"state: {state_path}")
    print(f"session: {first.session_id}")
    print(f"turn 1: {first.final_answer}")
    print(f"turn 2: {second.final_answer}")
    print(f"turn 3: {third.final_answer}")
    print(
        "context:",
        {
            "checkpoint_id": third.context["checkpoint_id"],
            "compacted_messages": third.context["compacted_messages"],
        },
    )
    print(f"job: {job_id} -> {scheduled[0]['status']}")
    print(
        "persisted:",
        {
            table: service.state_store.count(table)
            for table in (
                "sessions",
                "messages",
                "runs",
                "memories",
                "context_checkpoints",
                "scheduled_jobs",
            )
        },
    )


if __name__ == "__main__":
    main()
