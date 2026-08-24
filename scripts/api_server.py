"""Start the local API demo; credentials are intentionally supplied by env."""
from __future__ import annotations

import os
import signal
import threading

from edu_agent.api import DemoTokenAuth, EduAgentApi, Principal, make_http_server
from edu_agent.service import EduAgentService


def main() -> None:
    token = os.environ.get("EDU_AGENT_DEMO_TOKEN")
    if not token:
        raise SystemExit("set EDU_AGENT_DEMO_TOKEN to a local-only demo token")
    service = EduAgentService.from_config(os.environ.get("EDU_AGENT_CONFIG"))
    auth = DemoTokenAuth(
        {
            token: Principal(
                actor_id=os.environ.get("EDU_AGENT_DEMO_ACTOR", "teacher-demo"),
                tenant_id=os.environ.get("EDU_AGENT_DEMO_TENANT", "school-demo"),
                role=os.environ.get("EDU_AGENT_DEMO_ROLE", "teacher"),
            )
        }
    )
    api = EduAgentApi(service, authenticator=auth)
    server = make_http_server(
        api,
        host=os.environ.get("EDU_AGENT_API_HOST", "127.0.0.1"),
        port=int(os.environ.get("EDU_AGENT_API_PORT", "8080")),
    )
    print(f"EduAgent API listening at http://{server.server_address[0]}:{server.server_address[1]}")
    print("OpenAPI: /openapi.json; local demo auth uses Authorization: Bearer <EDU_AGENT_DEMO_TOKEN>")
    shutdown_started = threading.Event()

    def request_shutdown(signum, frame) -> None:
        del signum, frame
        if shutdown_started.is_set():
            return
        shutdown_started.set()

        def drain_and_stop() -> None:
            try:
                api.shutdown()
            finally:
                server.shutdown()

        threading.Thread(
            target=drain_and_stop,
            name="edu-agent-signal-shutdown",
            daemon=True,
        ).start()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        try:
            api.shutdown()
        finally:
            service.close()


if __name__ == "__main__":
    main()
