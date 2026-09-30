"""Telemetry setup: one TracerProvider and one MeterProvider per process.

`TELEMETRY_EXPORTER`:

* `none`    (default) spans are still created, so every request has a trace ID
            for logs and audit events; nothing is exported.
* `tree`    keeps finished spans in memory and prints them as a tree
            (`aegisdesk agent --trace`): tracing without any infrastructure.
* `console` prints spans and metrics as JSON (OpenTelemetry console exporters).
* `otlp`    OTLP/HTTP to the collector (`OTEL_EXPORTER_OTLP_ENDPOINT`), which
            forwards traces to Tempo and metrics to Prometheus.

Every exporter sits behind `RedactingSpanProcessor`.

We keep our own provider references instead of relying only on OpenTelemetry's
global ones (which can be set once per process), so tests can swap in-memory
exporters for each test.
"""

from __future__ import annotations

import atexit
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    MetricReader,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

from aegisdesk.observability.redaction import RedactingSpanProcessor

logger = logging.getLogger(__name__)

SERVICE_VERSION = "0.8.0"


class TelemetryExporter(StrEnum):
    NONE = "none"
    TREE = "tree"
    CONSOLE = "console"
    OTLP = "otlp"


class TreeExporter(SpanExporter):
    """Keeps finished spans so they can be printed as a tree (local learning, tests)."""

    def __init__(self) -> None:
        self.spans: list[ReadableSpan] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass


@dataclass
class _State:
    tracer_provider: TracerProvider = field(
        default_factory=lambda: TracerProvider(shutdown_on_exit=False)
    )
    meter_provider: MeterProvider = field(
        default_factory=lambda: MeterProvider(shutdown_on_exit=False)
    )
    tree: TreeExporter | None = None
    configured: bool = False


_state = _State()


def configure_telemetry(
    *,
    service_name: str,
    exporter: TelemetryExporter = TelemetryExporter.NONE,
    environment: str = "development",
    span_exporter: SpanExporter | None = None,
    metric_reader: MetricReader | None = None,
) -> None:
    """(Re)configure telemetry for this process. Tests pass in-memory exporters."""
    shutdown_telemetry()
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": SERVICE_VERSION,
            "deployment.environment.name": environment,
        }
    )
    # We flush and shut down ourselves (atexit below), exactly once.
    tracer_provider = TracerProvider(resource=resource, shutdown_on_exit=False)
    readers: list[MetricReader] = []
    tree = None

    if span_exporter is not None:
        tracer_provider.add_span_processor(
            RedactingSpanProcessor(SimpleSpanProcessor(span_exporter))
        )
    elif exporter is TelemetryExporter.TREE:
        tree = TreeExporter()
        tracer_provider.add_span_processor(RedactingSpanProcessor(SimpleSpanProcessor(tree)))
    elif exporter is TelemetryExporter.CONSOLE:
        tracer_provider.add_span_processor(
            RedactingSpanProcessor(SimpleSpanProcessor(ConsoleSpanExporter()))
        )
        readers.append(PeriodicExportingMetricReader(ConsoleMetricExporter()))
    elif exporter is TelemetryExporter.OTLP:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        # Endpoint and headers come from the standard OTEL_EXPORTER_OTLP_* variables.
        tracer_provider.add_span_processor(
            RedactingSpanProcessor(BatchSpanProcessor(OTLPSpanExporter()))
        )
        readers.append(
            PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=5000)
        )
    if metric_reader is not None:
        readers.append(metric_reader)

    _state.tracer_provider = tracer_provider
    _state.meter_provider = MeterProvider(
        resource=resource, metric_readers=readers, shutdown_on_exit=False
    )
    _state.tree = tree
    _state.configured = True
    # Also register globally, so libraries using the global API join our traces.
    _set_global(tracer_provider, _state.meter_provider)
    from aegisdesk.observability import metrics as instruments

    instruments.reset()


def _set_global(tracer_provider: TracerProvider, meter_provider: MeterProvider) -> None:
    # The global providers can only be set once; later calls are ignored with a
    # warning by the API, so we check first. Our own code uses `_state`.
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(tracer_provider)
    if not isinstance(metrics.get_meter_provider(), MeterProvider):
        metrics.set_meter_provider(meter_provider)


def tracer_provider() -> TracerProvider:
    return _state.tracer_provider


def meter_provider() -> MeterProvider:
    return _state.meter_provider


def tree_exporter() -> TreeExporter | None:
    return _state.tree


def shutdown_telemetry() -> None:
    """Flush and stop exporters (short-lived CLI processes must flush before exiting)."""
    if not _state.configured:
        return
    try:
        _state.tracer_provider.shutdown()
        _state.meter_provider.shutdown()
    except Exception:  # never let telemetry break the application
        logger.warning("telemetry shutdown failed", exc_info=True)
    _state.configured = False


atexit.register(shutdown_telemetry)
