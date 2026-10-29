"""Process-wide telemetry state: providers, exporters, and the instrument set.

The ONLY place that builds OpenTelemetry providers or OTLP exporters (ADR-0027,
ADR-0004 boundary row). Traces go to Arize Phoenix and metrics to Prometheus's
OTLP receiver, both over OTLP/HTTP, so the sink is a configuration choice.

Disabled (the default) means a no-op tracer and meter: nothing is exported and
no socket is opened. The SDK and exporters (~1 s of imports) load only when
telemetry is switched on, so the default path costs every process only the
light OpenTelemetry API. The providers are held here rather than installed as
the OpenTelemetry globals, so a test can configure, assert, and reset them, and
no third-party library starts exporting by accident.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from opentelemetry import metrics as otel_metrics
from opentelemetry import trace as otel_trace
from opentelemetry.metrics import Counter, Histogram, Meter
from opentelemetry.trace import Tracer

from app.core.config import Settings
from app.core.logging import get_logger

if TYPE_CHECKING:
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import MetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SpanExporter

log = get_logger(__name__)

_SCOPE = "lumen-copilot"

# Seconds buckets for request/tool/retrieval latencies (OTel HTTP semconv advice).
_LATENCY_BUCKETS: Sequence[float] = (
    0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0
)  # fmt: skip
# Seconds buckets for model calls, which run far longer (GenAI semconv advice).
_LLM_BUCKETS: Sequence[float] = (
    0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56, 5.12, 10.24, 20.48, 40.96, 81.92
)  # fmt: skip
# A turn spans retrieval, several model calls and tools: extend past two minutes.
_TURN_BUCKETS: Sequence[float] = (
    0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 60.0, 120.0, 240.0
)  # fmt: skip

# An LLM span flattens every message into its own attributes; the SDK default
# (128) would silently drop most of a long transcript.
_MAX_SPAN_ATTRIBUTES = 4096
# Bounded export so a slow collector costs memory, never a request.
_MAX_QUEUE = 2048
_BATCH_SIZE = 128
_EXPORT_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class Instruments:
    """Every metric instrument the backend records (ADR-0027 §3)."""

    http_duration: Histogram
    llm_duration: Histogram
    llm_ttft: Histogram
    llm_tokens: Counter
    tool_calls: Counter
    tool_duration: Histogram
    retrieval_duration: Histogram
    chat_turns: Counter
    chat_turn_duration: Histogram


def _build_instruments(meter: Meter) -> Instruments:
    return Instruments(
        http_duration=meter.create_histogram(
            "http.server.request.duration",
            unit="s",
            description="Duration of HTTP server requests.",
            explicit_bucket_boundaries_advisory=_LATENCY_BUCKETS,
        ),
        llm_duration=meter.create_histogram(
            "gen_ai.client.operation.duration",
            unit="s",
            description="Duration of model provider calls, from request to last chunk.",
            explicit_bucket_boundaries_advisory=_LLM_BUCKETS,
        ),
        llm_ttft=meter.create_histogram(
            "lumen.llm.time_to_first_token",
            unit="s",
            description="Time from a streamed model request to its first content or tool chunk.",
            explicit_bucket_boundaries_advisory=_LLM_BUCKETS,
        ),
        llm_tokens=meter.create_counter(
            "lumen.llm.tokens",
            unit="{token}",
            description="Tokens billed by model providers, by token type.",
        ),
        tool_calls=meter.create_counter(
            "lumen.tool.calls",
            unit="{call}",
            description="Agent tool invocations, by tool and outcome.",
        ),
        tool_duration=meter.create_histogram(
            "lumen.tool.duration",
            unit="s",
            description="Duration of agent tool invocations.",
            explicit_bucket_boundaries_advisory=_LATENCY_BUCKETS,
        ),
        retrieval_duration=meter.create_histogram(
            "lumen.retrieval.duration",
            unit="s",
            description="Duration of permission-filtered passage searches.",
            explicit_bucket_boundaries_advisory=_LATENCY_BUCKETS,
        ),
        chat_turns=meter.create_counter(
            "lumen.chat.turns",
            unit="{turn}",
            description="Answer turns produced by the chat runtime, by outcome.",
        ),
        chat_turn_duration=meter.create_histogram(
            "lumen.chat.turn.duration",
            unit="s",
            description="End-to-end duration of an answer turn.",
            explicit_bucket_boundaries_advisory=_TURN_BUCKETS,
        ),
    )


_NOOP_TRACER: Tracer = otel_trace.NoOpTracer()
_NOOP_INSTRUMENTS = _build_instruments(otel_metrics.NoOpMeter(_SCOPE))


@dataclass
class _State:
    tracer_provider: TracerProvider | None = None
    meter_provider: MeterProvider | None = None
    tracer: Tracer = _NOOP_TRACER
    instruments: Instruments = _NOOP_INSTRUMENTS
    capture_content: bool = False


_state = _State()


def tracer() -> Tracer:
    """The backend tracer — a no-op until :func:`configure_telemetry` enables it."""
    return _state.tracer


def instruments() -> Instruments:
    """The metric instruments — no-ops until :func:`configure_telemetry` enables them."""
    return _state.instruments


def capture_content() -> bool:
    """Whether spans may carry prompts, completions, tool I/O and passages (ADR-0027 §4)."""
    return _state.capture_content


def is_configured() -> bool:
    """Whether this process exports telemetry."""
    return _state.tracer_provider is not None or _state.meter_provider is not None


def _resource(settings: Settings, process: str) -> Resource:
    from opentelemetry.sdk.resources import Resource

    # ``Resource(...)`` rather than ``Resource.create`` so no OTEL_* environment
    # variable is read here: all config comes from Settings (backend/AGENTS.md).
    return Resource(
        {
            "service.name": settings.service_name,
            "service.version": settings.version,
            # Distinct per process so Prometheus keeps each Celery child's
            # cumulative series apart instead of interleaving them.
            "service.instance.id": f"{socket.gethostname()}:{os.getpid()}",
            "deployment.environment.name": settings.environment,
            "lumen.process": process,
            # Phoenix files the traces under this project.
            "openinference.project.name": settings.telemetry_project_name,
        }
    )


def configure_telemetry(
    settings: Settings,
    *,
    process: str,
    span_exporter: SpanExporter | None = None,
    metric_reader: MetricReader | None = None,
) -> bool:
    """Enable trace and metric export for this process, per ``settings``.

    Idempotent: a second call while configured is a no-op. Returns whether
    telemetry is now on. ``process`` names the role (``api``, ``worker``) on the
    resource. ``span_exporter`` / ``metric_reader`` are the test seams: an
    injected exporter is attached with a synchronous processor so a test reads
    its spans the moment they end, and an injected reader replaces OTLP export.
    """
    if not settings.telemetry_enabled:
        return False
    if is_configured():
        return True

    # Deferred: only an enabled process pays for the SDK and exporters.
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.trace import SpanLimits, TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

    resource = _resource(settings, process)
    traces_endpoint = settings.telemetry_traces_endpoint
    if span_exporter is not None or traces_endpoint:
        provider = TracerProvider(
            resource=resource,
            span_limits=SpanLimits(
                max_span_attributes=_MAX_SPAN_ATTRIBUTES,
                max_attribute_length=settings.telemetry_max_attribute_chars,
            ),
        )
        if span_exporter is not None:
            provider.add_span_processor(SimpleSpanProcessor(span_exporter))
        else:
            api_key = settings.telemetry_traces_api_key
            headers = {"authorization": f"Bearer {api_key}"} if api_key else None
            provider.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(
                        endpoint=traces_endpoint,
                        headers=headers,
                        timeout=_EXPORT_TIMEOUT_SECONDS,
                    ),
                    max_queue_size=_MAX_QUEUE,
                    max_export_batch_size=_BATCH_SIZE,
                )
            )
        _state.tracer_provider = provider
        _state.tracer = provider.get_tracer(_SCOPE, settings.version)

    metrics_endpoint = settings.telemetry_metrics_endpoint
    if metric_reader is not None or metrics_endpoint:
        reader = metric_reader or PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=metrics_endpoint, timeout=_EXPORT_TIMEOUT_SECONDS),
            export_interval_millis=int(settings.telemetry_metrics_export_interval_seconds * 1000),
        )
        meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
        _state.meter_provider = meter_provider
        _state.instruments = _build_instruments(meter_provider.get_meter(_SCOPE, settings.version))

    _state.capture_content = settings.telemetry_capture_content
    log.info(
        "telemetry.configured",
        process=process,
        traces=_state.tracer_provider is not None,
        metrics=_state.meter_provider is not None,
        capture_content=_state.capture_content,
    )
    return True


def shutdown_telemetry() -> None:
    """Flush and stop export, then return to the no-op state.

    Called on API lifespan teardown and Celery child shutdown so the last
    batch is not lost; safe to call when telemetry was never configured.
    """
    tracer_provider, meter_provider = _state.tracer_provider, _state.meter_provider
    _state.tracer_provider = None
    _state.meter_provider = None
    _state.tracer = _NOOP_TRACER
    _state.instruments = _NOOP_INSTRUMENTS
    _state.capture_content = False
    # Exporter failures are the collector's problem, never a shutdown failure.
    if tracer_provider is not None:
        try:
            tracer_provider.shutdown()
        except Exception as exc:  # noqa: BLE001
            log.warning("telemetry.trace_shutdown_failed", error_type=type(exc).__name__)
    if meter_provider is not None:
        try:
            meter_provider.shutdown()
        except Exception as exc:  # noqa: BLE001
            log.warning("telemetry.metric_shutdown_failed", error_type=type(exc).__name__)
