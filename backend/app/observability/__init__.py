"""Ops and LLM observability — the only module that speaks OpenTelemetry.

Single responsibility (ADR-0027; ADR-0004 boundary row): build the trace and
metric providers, export them over OTLP (traces → Arize Phoenix, metrics →
Prometheus), and give the rest of the backend small helpers that record
OpenInference spans and metrics from domain types. **Nobody else may import
``opentelemetry`` or ``openinference``.** Off by default (``TELEMETRY_ENABLED``),
in which case every helper is a no-op.
"""

from app.observability.http import HttpMetricsMiddleware
from app.observability.llm import EmbeddingSpan, LlmCall, observe_embedding
from app.observability.spans import (
    ObservedSpan,
    metadata_attribute,
    observe,
    retrieval_document_attributes,
    session_attributes,
)
from app.observability.telemetry import (
    capture_content,
    configure_telemetry,
    instruments,
    is_configured,
    shutdown_telemetry,
)
from app.observability.turns import (
    observe_headless_run,
    observe_retrieval,
    observe_tool,
    observe_turn,
)

__all__ = [
    "EmbeddingSpan",
    "HttpMetricsMiddleware",
    "LlmCall",
    "ObservedSpan",
    "capture_content",
    "configure_telemetry",
    "instruments",
    "is_configured",
    "metadata_attribute",
    "observe",
    "observe_embedding",
    "observe_headless_run",
    "observe_retrieval",
    "observe_tool",
    "observe_turn",
    "retrieval_document_attributes",
    "session_attributes",
    "shutdown_telemetry",
]
