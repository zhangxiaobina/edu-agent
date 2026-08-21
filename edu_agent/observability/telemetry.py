from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .events import RuntimeEvent
from .redaction import RedactionPolicy


@dataclass(frozen=True)
class TelemetryResult:
    enabled: bool
    exported: int
    error: str | None = None


class OptionalTelemetryExporter:
    """Failure-isolated telemetry gate; disabled means zero exporter calls."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        exporter: Callable[[dict[str, Any]], None] | None = None,
        redaction: RedactionPolicy | None = None,
    ):
        self.enabled = enabled
        self.exporter = exporter
        self.redaction = redaction or RedactionPolicy()

    def export(self, events: Iterable[RuntimeEvent]) -> TelemetryResult:
        if not self.enabled:
            return TelemetryResult(enabled=False, exported=0)
        if self.exporter is None:
            return TelemetryResult(
                enabled=True,
                exported=0,
                error="OpenTelemetry exporter is not configured",
            )
        exported = 0
        try:
            for event in events:
                self.exporter(self.redaction.redact(event.to_dict()))
                exported += 1
        except Exception as error:  # telemetry may never break the agent path
            return TelemetryResult(
                enabled=True,
                exported=exported,
                error=f"{type(error).__name__}: {error}",
            )
        return TelemetryResult(enabled=True, exported=exported)

    def export_payloads(self, payloads: Iterable[dict[str, Any]]) -> TelemetryResult:
        """Export already-redacted payloads through the same safety gate."""
        if not self.enabled:
            return TelemetryResult(enabled=False, exported=0)
        if self.exporter is None:
            return TelemetryResult(True, 0, "OpenTelemetry exporter is not configured")
        exported = 0
        try:
            for payload in payloads:
                self.exporter(self.redaction.redact(payload))
                exported += 1
        except Exception as error:
            return TelemetryResult(True, exported, f"{type(error).__name__}: {error}")
        return TelemetryResult(True, exported)


def build_opentelemetry_exporter(config) -> OptionalTelemetryExporter:
    """Build the optional OTLP span adapter without importing it when off."""
    if not config.otel_enabled:
        return OptionalTelemetryExporter(enabled=False)
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    except ImportError as error:
        reason = str(error)
        def missing_extra(payload):
            raise RuntimeError(f"install the 'otel' extra to enable OTLP: {reason}")
        return OptionalTelemetryExporter(enabled=True, exporter=missing_extra)

    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(OTLPSpanExporter(endpoint=config.otlp_endpoint))
    )
    tracer = provider.get_tracer("edu_agent.runtime", "1.0.0")

    def export(payload: dict[str, Any]) -> None:
        with tracer.start_as_current_span(str(payload.get("event_type", "runtime.event"))) as span:
            for key in (
                "event_id", "schema_version", "run_id", "root_run_id", "parent_run_id",
                "session_id", "actor_id", "tenant_id", "component", "status",
            ):
                value = payload.get(key)
                if value is not None:
                    span.set_attribute(f"edu_agent.{key}", str(value))
            if payload.get("duration_ms") is not None:
                span.set_attribute("edu_agent.duration_ms", float(payload["duration_ms"]))

    return OptionalTelemetryExporter(enabled=True, exporter=export)
