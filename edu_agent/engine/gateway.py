from __future__ import annotations

import json
import os
import re
from ipaddress import ip_address
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

from .base import Engine, EngineResponse


class ApiMode(str, Enum):
    CHAT_COMPLETIONS = "chat_completions"
    RESPONSES = "responses"

    @classmethod
    def parse(cls, value: ApiMode | str) -> ApiMode:
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value.strip().lower())
            except ValueError:
                pass
        raise ValueError("api_mode 必须是 chat_completions 或 responses")

    def __str__(self) -> str:
        return self.value


class ModeSource(str, Enum):
    EXPLICIT = "explicit"
    REGISTRY = "registry"
    OFFICIAL_HOST = "official_host"
    DEFAULT = "default"


@dataclass(frozen=True)
class ProviderCapabilities:
    tool_calling: bool = True
    structured_output: bool = False
    usage: bool = True
    streaming: bool = False
    context_window_tokens: int | None = None
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        for name in ("tool_calling", "structured_output", "usage", "streaming"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"provider capability {name} 必须是 bool")
        for name in ("context_window_tokens", "max_output_tokens"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise ValueError(f"provider capability {name} 必须大于 0")

    def to_event(self) -> dict[str, bool | int | None]:
        return {
            "tool_calling": self.tool_calling,
            "structured_output": self.structured_output,
            "usage": self.usage,
            "streaming": self.streaming,
            "context_window_tokens": self.context_window_tokens,
            "max_output_tokens": self.max_output_tokens,
        }


@dataclass(frozen=True)
class ProviderRequestRequirements:
    """Capabilities needed to preserve one normalized ``Engine.chat`` request."""

    api_modes: frozenset[ApiMode]
    tool_calling: bool
    structured_output: bool
    usage: bool
    streaming: bool
    context_tokens: int
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.api_modes, frozenset) or not self.api_modes:
            raise ValueError("provider request api_modes 必须是非空 frozenset")
        parsed_modes = frozenset(ApiMode.parse(mode) for mode in self.api_modes)
        object.__setattr__(self, "api_modes", parsed_modes)
        for name in ("tool_calling", "structured_output", "usage", "streaming"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"provider request requirement {name} 必须是 bool")
        if (
            isinstance(self.context_tokens, bool)
            or not isinstance(self.context_tokens, int)
            or self.context_tokens <= 0
        ):
            raise ValueError("provider request context_tokens 必须大于 0")
        if self.max_output_tokens is not None and (
            isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_output_tokens, int)
            or self.max_output_tokens <= 0
        ):
            raise ValueError("provider request max_output_tokens 必须大于 0")

    def to_event(self) -> dict[str, bool | int | None | list[str]]:
        return {
            "api_modes": sorted(mode.value for mode in self.api_modes),
            "tool_calling": self.tool_calling,
            "structured_output": self.structured_output,
            "usage": self.usage,
            "streaming": self.streaming,
            "context_tokens": self.context_tokens,
            "max_output_tokens": self.max_output_tokens,
        }


class ProviderCapabilityError(ValueError):
    """A request cannot be represented by a route without losing capabilities."""

    def __init__(self, gaps: tuple[str, ...]):
        if not gaps:
            raise ValueError("provider capability gaps 不能为空")
        self.gaps = gaps
        labels = {
            "context_window": "context window",
            "context_window_unknown": "context window unknown",
            "structured_output": "structured output",
            "tool_calling": "不支持 tool calling",
        }
        rendered = ", ".join(labels.get(gap, gap) for gap in gaps)
        super().__init__("provider route capability 不兼容: " + rendered)


def estimate_request_tokens(messages: list[dict], tools: list[dict]) -> int:
    """Return the shared conservative byte-based estimate used for route checks."""
    payload = json.dumps(
        {"messages": messages, "tools": tools},
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    ).encode("utf-8")
    return max(1, (len(payload) + 3) // 4)


def infer_request_requirements(
    messages: list[dict],
    tools: list[dict],
) -> ProviderRequestRequirements:
    tool_history = any(
        isinstance(message, Mapping)
        and (
            message.get("role") == "tool"
            or bool(message.get("tool_calls"))
        )
        for message in messages
    )
    structured_output = any(
        isinstance(tool, Mapping)
        and isinstance(tool.get("function"), Mapping)
        and tool["function"].get("strict") is True
        for tool in tools
    )
    return ProviderRequestRequirements(
        api_modes=frozenset(ApiMode),
        tool_calling=bool(tools) or tool_history,
        structured_output=structured_output,
        # Usage is normalized when present, but the current Agent contract accepts
        # providers that omit it. Streaming and output token requests are not in R1.
        usage=False,
        streaming=False,
        context_tokens=estimate_request_tokens(messages, tools),
        max_output_tokens=None,
    )


def effective_capabilities(
    route: ProviderCapabilities,
    adapter: ProviderCapabilities,
) -> ProviderCapabilities:
    def limit(left: int | None, right: int | None) -> int | None:
        known = [value for value in (left, right) if value is not None]
        return min(known) if known else None

    return ProviderCapabilities(
        tool_calling=route.tool_calling and adapter.tool_calling,
        structured_output=route.structured_output and adapter.structured_output,
        usage=route.usage and adapter.usage,
        streaming=route.streaming and adapter.streaming,
        context_window_tokens=limit(
            route.context_window_tokens,
            adapter.context_window_tokens,
        ),
        max_output_tokens=limit(
            route.max_output_tokens,
            adapter.max_output_tokens,
        ),
    )


def capability_gaps(
    requirements: ProviderRequestRequirements,
    capabilities: ProviderCapabilities,
    *,
    api_mode: ApiMode,
    require_known_context: bool = False,
) -> tuple[str, ...]:
    gaps: list[str] = []
    if api_mode not in requirements.api_modes:
        gaps.append("api_mode")
    for name in ("tool_calling", "structured_output", "usage", "streaming"):
        if getattr(requirements, name) and not getattr(capabilities, name):
            gaps.append(name)
    context_limit = capabilities.context_window_tokens
    if context_limit is not None and requirements.context_tokens > context_limit:
        gaps.append("context_window")
    if require_known_context and context_limit is None:
        gaps.append("context_window_unknown")
    requested_output = requirements.max_output_tokens
    output_limit = capabilities.max_output_tokens
    if requested_output is not None and (
        output_limit is None or requested_output > output_limit
    ):
        gaps.append("max_output_tokens")
    return tuple(dict.fromkeys(gaps))


_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


@dataclass(frozen=True, repr=False)
class CredentialRef:
    """A reference to credential material, never the credential value itself."""

    environment_variable: str = "EDU_AGENT_API_KEY"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.environment_variable, str)
            or _ENVIRONMENT_NAME.fullmatch(self.environment_variable) is None
        ):
            raise ValueError("credential environment variable 名称无效")

    def resolve(self, environ: Mapping[str, str] | None = None) -> str | None:
        source = os.environ if environ is None else environ
        return source.get(self.environment_variable)

    def __repr__(self) -> str:
        return "CredentialRef(source='environment')"


@dataclass(frozen=True)
class ProviderSpec:
    model: str
    endpoint: str | None = None
    api_mode: ApiMode | str | None = None
    provider: str | None = None
    deployment: str | None = None
    credential: CredentialRef = field(default_factory=CredentialRef, repr=False, compare=False)
    capabilities: ProviderCapabilities | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.credential, CredentialRef):
            raise ValueError("credential 必须是 CredentialRef")
        if self.capabilities is not None and not isinstance(
            self.capabilities, ProviderCapabilities
        ):
            raise ValueError("capabilities 必须是 ProviderCapabilities")
        _validate_text(self.model, "model")
        if self.provider is not None:
            _validate_identifier(self.provider, "provider")
        if self.deployment is not None:
            _validate_identifier(self.deployment, "deployment")
        if self.endpoint is not None:
            normalize_endpoint(self.endpoint)
        if self.api_mode is not None:
            object.__setattr__(self, "api_mode", ApiMode.parse(self.api_mode))
        _reject_credential_in_route_fields(self)


@dataclass(frozen=True)
class ProviderMetadata:
    api_mode: ApiMode | str
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)
    default_endpoint: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.capabilities, ProviderCapabilities):
            raise ValueError("capabilities 必须是 ProviderCapabilities")
        object.__setattr__(self, "api_mode", ApiMode.parse(self.api_mode))
        if self.default_endpoint is not None:
            normalize_endpoint(self.default_endpoint)


RouteIdentity = tuple[str, str, str, str, str]


@dataclass(frozen=True)
class ResolvedRoute:
    api_mode: ApiMode
    provider: str
    deployment: str | None
    endpoint: str
    normalized_endpoint: str
    model: str
    capabilities: ProviderCapabilities
    mode_source: ModeSource
    credential: CredentialRef = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.credential, CredentialRef):
            raise ValueError("credential 必须是 CredentialRef")
        if not isinstance(self.capabilities, ProviderCapabilities):
            raise ValueError("capabilities 必须是 ProviderCapabilities")
        object.__setattr__(self, "api_mode", ApiMode.parse(self.api_mode))
        if not isinstance(self.mode_source, ModeSource):
            try:
                object.__setattr__(self, "mode_source", ModeSource(self.mode_source))
            except (TypeError, ValueError) as error:
                raise ValueError("mode_source 无效") from error
        provider = _validate_identifier(self.provider, "provider")
        object.__setattr__(self, "provider", provider)
        if self.deployment is not None:
            _validate_identifier(self.deployment, "deployment", lowercase=False)
        _validate_text(self.model, "model")
        normalized = normalize_endpoint(self.endpoint)
        if normalized != self.normalized_endpoint:
            raise ValueError("normalized_endpoint 与 endpoint 不一致")
        _reject_credential_in_values(
            self.credential,
            self.model,
            self.endpoint,
            self.normalized_endpoint,
            self.provider,
            self.deployment,
        )

    @property
    def identity(self) -> RouteIdentity:
        return (
            self.provider,
            self.deployment or "",
            self.api_mode.value,
            self.normalized_endpoint,
            self.model,
        )

    def to_event(self) -> dict:
        """Return the stable route audit shape; credential refs are intentionally omitted."""
        return {
            "route_identity": self.identity,
            "api_mode": self.api_mode.value,
            "mode_source": self.mode_source.value,
            "provider": self.provider,
            "deployment": self.deployment,
            "endpoint": self.normalized_endpoint,
            "model": self.model,
            "capabilities": self.capabilities.to_event(),
        }


@runtime_checkable
class ProviderAdapter(Protocol):
    api_mode: ApiMode
    capabilities: ProviderCapabilities

    def chat(
        self,
        route: ResolvedRoute,
        messages: list[dict],
        tools: list[dict],
    ) -> EngineResponse:
        ...


@dataclass(frozen=True)
class _OfficialHostRule:
    provider: str
    api_mode: ApiMode
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)


_CHAT_CAPABILITIES = ProviderCapabilities()
_DEFAULT_ENDPOINT = "https://api.openai.com/v1"


@dataclass(frozen=True)
class _Endpoint:
    original: str
    normalized: str
    host: str
    trusted_official_host: bool


def _validate_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} 必须是非空且无首尾空白的字符串")
    if len(value) > 512 or any(
        ord(character) < 32 or character.isspace() or ord(character) == 127
        for character in value
    ):
        raise ValueError(f"{label} 包含无效字符或过长")
    return value


def _validate_identifier(value: str, label: str, *, lowercase: bool = True) -> str:
    value = _validate_text(value, label)
    if len(value) > 128 or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) is None:
        raise ValueError(f"{label} 必须是稳定标识符")
    return value.lower() if lowercase else value


def _reject_credential_in_route_fields(spec: ProviderSpec) -> None:
    _reject_credential_in_values(
        spec.credential,
        spec.model,
        spec.endpoint,
        spec.provider,
        spec.deployment,
    )


def _reject_credential_in_values(
    credential_ref: CredentialRef,
    *values: str | None,
) -> None:
    credential = credential_ref.resolve()
    if not credential or len(credential) < 8:
        return
    if any(value is not None and credential in value for value in values):
        raise ValueError("provider route 字段不得包含凭据")


def _normalize_host(host: str) -> str:
    host = host.rstrip(".").lower()
    if not host or "%" in host:
        raise ValueError("endpoint host 无效")
    try:
        return ip_address(host).compressed
    except ValueError:
        pass
    try:
        normalized = host.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise ValueError("endpoint host 无效") from error
    if len(normalized) > 253:
        raise ValueError("endpoint host 无效")
    labels = normalized.split(".")
    if any(
        not label
        or len(label) > 63
        or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) is None
        for label in labels
    ):
        raise ValueError("endpoint host 无效")
    return normalized


def _parse_endpoint(endpoint: str) -> _Endpoint:
    endpoint = _validate_text(endpoint, "endpoint")
    if "\\" in endpoint or "?" in endpoint or "#" in endpoint:
        raise ValueError("endpoint 不允许反斜杠、query 或 fragment")
    if re.search(r"(?i)%0[0-9a-f]|%1[0-9a-f]|%7f", endpoint):
        raise ValueError("endpoint 不允许编码控制字符")
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as error:
        raise ValueError("endpoint URL 无效") from error
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.netloc or parsed.hostname is None:
        raise ValueError("endpoint 必须是带 host 的 http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("endpoint 不允许内嵌凭据")
    normalized_host = _normalize_host(parsed.hostname)
    rendered_host = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    default_port = 80 if scheme == "http" else 443
    netloc = rendered_host if port in {None, default_port} else f"{rendered_host}:{port}"
    normalized = urlunsplit((scheme, netloc, parsed.path, "", ""))
    trusted_official_host = scheme == "https" and port in {None, 443}
    return _Endpoint(endpoint, normalized, normalized_host, trusted_official_host)


def normalize_endpoint(endpoint: str) -> str:
    """Normalize only the authority used for route isolation; preserve the path byte shape."""
    return _parse_endpoint(endpoint).normalized


DEFAULT_PROVIDER_REGISTRY: Mapping[str, ProviderMetadata] = MappingProxyType(
    {
        "dashscope": ProviderMetadata(
            api_mode=ApiMode.CHAT_COMPLETIONS,
            default_endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1",
        ),
        "openai": ProviderMetadata(
            api_mode=ApiMode.CHAT_COMPLETIONS,
            default_endpoint=_DEFAULT_ENDPOINT,
        ),
        "vllm": ProviderMetadata(api_mode=ApiMode.CHAT_COMPLETIONS),
    }
)
_OFFICIAL_HOST_RULES: Mapping[str, _OfficialHostRule] = MappingProxyType(
    {
        "api.openai.com": _OfficialHostRule("openai", ApiMode.CHAT_COMPLETIONS),
        "dashscope.aliyuncs.com": _OfficialHostRule(
            "dashscope", ApiMode.CHAT_COMPLETIONS
        ),
        "dashscope-intl.aliyuncs.com": _OfficialHostRule(
            "dashscope", ApiMode.CHAT_COMPLETIONS
        ),
    }
)


class ProviderGateway:
    """Resolve immutable routes and dispatch them to the matching API-mode adapter."""

    def __init__(
        self,
        registry: Mapping[str, ProviderMetadata] | None = None,
        adapters: Mapping[ApiMode | str, ProviderAdapter] | None = None,
    ):
        source = DEFAULT_PROVIDER_REGISTRY if registry is None else registry
        self._registry = MappingProxyType(
            {_validate_identifier(name, "provider registry key"): metadata for name, metadata in source.items()}
        )
        if adapters is None:
            # Lazy import keeps gateway.py independent from the concrete SDK
            # adapter while making the default R1 route usable on its own.
            from .chat_completions import ChatCompletionsAdapter
            from .responses import ResponsesAdapter

            adapters = {
                ApiMode.CHAT_COMPLETIONS: ChatCompletionsAdapter(),
                ApiMode.RESPONSES: ResponsesAdapter(),
            }
        resolved_adapters: dict[ApiMode, ProviderAdapter] = {}
        for configured_mode, adapter in adapters.items():
            mode = ApiMode.parse(configured_mode)
            try:
                adapter_mode = ApiMode.parse(adapter.api_mode)
            except AttributeError as error:
                raise ValueError("provider adapter 缺少 api_mode") from error
            if adapter_mode is not mode:
                raise ValueError("provider adapter 注册 mode 与声明不一致")
            if not isinstance(getattr(adapter, "capabilities", None), ProviderCapabilities):
                raise ValueError("provider adapter capabilities 无效")
            if not callable(getattr(adapter, "chat", None)):
                raise ValueError("provider adapter 缺少 chat")
            resolved_adapters[mode] = adapter
        self._adapters = MappingProxyType(resolved_adapters)

    def with_adapter(self, adapter: ProviderAdapter) -> ProviderGateway:
        """Return a gateway with one adapter added or replaced, preserving route metadata."""
        mode = ApiMode.parse(adapter.api_mode)
        return ProviderGateway(
            self._registry,
            adapters={**self._adapters, mode: adapter},
        )

    def adapter_for(self, route: ResolvedRoute) -> ProviderAdapter:
        """Select the adapter declared for the route's frozen API mode."""
        adapter = self._adapters.get(route.api_mode)
        if adapter is not None:
            return adapter
        supported = ", ".join(mode.value for mode in self._adapters)
        if supported:
            raise ValueError(
                f"当前 Provider Gateway 仅支持 {supported}；"
                f"未注册 {route.api_mode.value} adapter"
            )
        raise ValueError(f"当前 Provider Gateway 未注册 {route.api_mode.value} adapter")

    def capabilities_for(self, route: ResolvedRoute) -> ProviderCapabilities:
        adapter = self.adapter_for(route)
        return effective_capabilities(route.capabilities, adapter.capabilities)

    def validate_request(
        self,
        route: ResolvedRoute,
        messages: list[dict],
        tools: list[dict],
    ) -> ProviderRequestRequirements:
        """Validate one route without creating a client or issuing provider I/O."""
        requirements = infer_request_requirements(messages, tools)
        gaps = capability_gaps(
            requirements,
            self.capabilities_for(route),
            api_mode=route.api_mode,
        )
        if gaps:
            raise ProviderCapabilityError(gaps)
        adapter = self.adapter_for(route)
        validator = getattr(adapter, "validate_request", None)
        if callable(validator):
            validator(route, messages, tools)
        return requirements

    def chat(
        self,
        route: ResolvedRoute,
        messages: list[dict],
        tools: list[dict],
    ) -> EngineResponse:
        """Dispatch one normalized synchronous request without changing Engine.chat."""
        self.validate_request(route, messages, tools)
        return self.adapter_for(route).chat(route, messages, tools)

    def begin_turn(self, spec: ProviderSpec) -> ResolvedRoute:
        _reject_credential_in_route_fields(spec)
        configured_endpoint = _parse_endpoint(spec.endpoint) if spec.endpoint is not None else None
        requested_provider = (
            _validate_identifier(spec.provider, "provider") if spec.provider is not None else None
        )
        metadata = self._registry.get(requested_provider) if requested_provider else None
        official = None
        if configured_endpoint is not None and configured_endpoint.trusted_official_host:
            official = _OFFICIAL_HOST_RULES.get(configured_endpoint.host)
        if requested_provider and official and requested_provider != official.provider:
            raise ValueError("provider 与受信任官方 endpoint 冲突")

        if spec.api_mode is not None:
            api_mode = ApiMode.parse(spec.api_mode)
            mode_source = ModeSource.EXPLICIT
        elif metadata is not None:
            api_mode = ApiMode.parse(metadata.api_mode)
            mode_source = ModeSource.REGISTRY
        elif official is not None:
            api_mode = official.api_mode
            mode_source = ModeSource.OFFICIAL_HOST
        else:
            api_mode = ApiMode.CHAT_COMPLETIONS
            mode_source = ModeSource.DEFAULT

        endpoint_value = spec.endpoint
        if endpoint_value is None and metadata is not None:
            endpoint_value = metadata.default_endpoint
        if endpoint_value is None:
            if requested_provider:
                raise ValueError("该 provider 必须显式配置 endpoint")
            endpoint_value = _DEFAULT_ENDPOINT
        resolved_endpoint = _parse_endpoint(endpoint_value)

        provider = requested_provider
        if provider is None and official is not None:
            provider = official.provider
        if provider is None:
            provider = "openai" if spec.endpoint is None else "custom"

        if spec.capabilities is not None:
            capabilities = spec.capabilities
        elif metadata is not None:
            capabilities = metadata.capabilities
        elif official is not None:
            capabilities = official.capabilities
        else:
            capabilities = _CHAT_CAPABILITIES

        deployment = (
            _validate_identifier(spec.deployment, "deployment", lowercase=False)
            if spec.deployment is not None
            else None
        )
        return ResolvedRoute(
            api_mode=api_mode,
            provider=provider,
            deployment=deployment,
            endpoint=resolved_endpoint.original,
            normalized_endpoint=resolved_endpoint.normalized,
            model=spec.model,
            credential=spec.credential,
            capabilities=capabilities,
            mode_source=mode_source,
        )


class GatewayEngine(Engine):
    """Synchronous Engine facade over one route selected by ProviderGateway."""

    def __init__(
        self,
        gateway: ProviderGateway,
        spec: ProviderSpec,
        *,
        name: str | None = None,
    ):
        self._configure_route(gateway, spec)
        self.name = name or f"{self.route.provider}:{self.route.api_mode.value}"

    def _configure_route(self, gateway: ProviderGateway, spec: ProviderSpec) -> None:
        route = gateway.begin_turn(spec)
        gateway.adapter_for(route)
        self.gateway = gateway
        self.spec = spec
        self.route = route

    @property
    def model(self) -> str:
        return self.route.model

    @property
    def base_url(self) -> str:
        return self.route.endpoint

    @property
    def temperature(self) -> float:
        return float(getattr(self.gateway.adapter_for(self.route), "temperature", 0.0))

    @property
    def timeout(self) -> float:
        return float(getattr(self.gateway.adapter_for(self.route), "timeout", 0.0))

    def begin_turn_routes(self) -> tuple[ResolvedRoute, ...]:
        return (self.route,)

    def effective_capabilities(self) -> ProviderCapabilities:
        return self.gateway.capabilities_for(self.route)

    def validate_request(
        self,
        messages: list[dict],
        tools: list[dict],
    ) -> ProviderRequestRequirements:
        return self.gateway.validate_request(self.route, messages, tools)

    def validate_request_on_route(
        self,
        route: ResolvedRoute,
        messages: list[dict],
        tools: list[dict],
    ) -> ProviderRequestRequirements:
        return self.gateway.validate_request(route, messages, tools)

    def chat_on_route(
        self,
        route: ResolvedRoute,
        messages: list[dict],
        tools: list[dict],
    ) -> EngineResponse:
        return self.gateway.chat(route, messages, tools)

    def chat(self, messages: list[dict], tools: list[dict]) -> EngineResponse:
        return self.gateway.chat(self.route, messages, tools)


__all__ = [
    "ApiMode",
    "ProviderCapabilityError",
    "CredentialRef",
    "DEFAULT_PROVIDER_REGISTRY",
    "GatewayEngine",
    "ModeSource",
    "ProviderAdapter",
    "ProviderCapabilities",
    "ProviderRequestRequirements",
    "ProviderGateway",
    "ProviderMetadata",
    "ProviderSpec",
    "ResolvedRoute",
    "RouteIdentity",
    "capability_gaps",
    "effective_capabilities",
    "estimate_request_tokens",
    "infer_request_requirements",
    "normalize_endpoint",
]
