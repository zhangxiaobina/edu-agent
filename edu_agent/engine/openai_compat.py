"""Legacy OpenAI-compatible engine constructor.

New code should use ``get_engine`` (or ``GatewayEngine`` with an explicitly
registered ``ChatCompletionsAdapter``). ``OpenAICompatEngine`` remains as a
thin constructor-compatible facade; all request building, SDK calls, response
normalization, and adapter selection now go through the Provider Gateway.
"""
from __future__ import annotations

import os
from typing import Any

from .chat_completions import ChatCompletionsAdapter
from .gateway import ApiMode, GatewayEngine, ProviderGateway, ProviderSpec


class OpenAICompatEngine(GatewayEngine):
    """Deprecated constructor-compatible alias for the Gateway chat engine."""

    name = "openai"

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        temperature: float = 0.0,
        timeout: float | None = None,
        *,
        client: Any | None = None,
        client_factory=None,
    ):
        resolved_base_url = base_url or os.environ.get("EDU_AGENT_BASE_URL") or None
        resolved_api_key = api_key or os.environ.get("EDU_AGENT_API_KEY") or "EMPTY"
        resolved_model = model or os.environ.get("EDU_AGENT_MODEL") or "qwen-plus"
        resolved_timeout = (
            timeout
            if timeout is not None
            else float(os.environ.get("EDU_AGENT_TIMEOUT", "1800"))
        )
        adapter = ChatCompletionsAdapter(
            client,
            client_factory=client_factory,
            api_key=resolved_api_key,
            temperature=temperature,
            timeout=resolved_timeout,
        )
        gateway = ProviderGateway(adapters={ApiMode.CHAT_COMPLETIONS: adapter})
        spec = ProviderSpec(model=resolved_model, endpoint=resolved_base_url)
        super().__init__(gateway, spec, name=self.name)
        self.api_key = resolved_api_key
        self._adapter = adapter

    def configure_provider_route(
        self,
        spec: ProviderSpec,
        gateway: ProviderGateway,
    ) -> None:
        """Keep the R1.1 setup hook while routing through the shared adapter."""
        self._configure_route(gateway.with_adapter(self._adapter), spec)
