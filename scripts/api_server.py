"""Start the local API demo; credentials are intentionally supplied by env."""
from __future__ import annotations

import os

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
    try:
        server.serve_forever()
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()
