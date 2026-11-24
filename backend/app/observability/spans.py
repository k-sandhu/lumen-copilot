"""OpenInference span helpers for turns, runs, tools and retrieval (ADR-0027 §2).

Callers never touch the OpenTelemetry API: they open a span with one of the
context managers below and report onto the :class:`ObservedSpan` it yields.
Content (questions, answers, tool arguments and results, passage text) goes
through :meth:`ObservedSpan.set_content`, which drops it when content capture
is off; ids, names, counts and status are always recorded.

Errors are recorded by type and, for the app's own typed errors, by their
client-safe code and detail. A raw exception message is never recorded: a
vendor message can carry request details.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from openinference.semconv.trace import (
    DocumentAttributes,
    OpenInferenceMimeTypeValues,
    OpenInferenceSpanKindValues,
    SpanAttributes,
)
from opentelemetry.trace import Span, Status, StatusCode, use_span
from opentelemetry.util.types import AttributeValue

from app.core.errors import AppError
from app.observability import telemetry

_KIND = SpanAttributes.OPENINFERENCE_SPAN_KIND
_JSON = OpenInferenceMimeTypeValues.JSON.value
_TEXT = OpenInferenceMimeTypeValues.TEXT.value


def to_json(value: Any) -> str:
    """Serialize a span payload; anything non-JSON (UUIDs, dates) becomes a string."""
    return json.dumps(value, default=str, ensure_ascii=False)


def _clean(attributes: Mapping[str, Any]) -> dict[str, AttributeValue]:
    """Drop ``None`` values and stringify UUIDs so the SDK accepts every attribute."""
    cleaned: dict[str, AttributeValue] = {}
    for key, value in attributes.items():
        if value is None:
            continue
        cleaned[key] = str(value) if isinstance(value, UUID) else value
    return cleaned


class ObservedSpan:
    """A thin, content-aware facade over one OpenTelemetry span."""

    def __init__(self, span: Span) -> None:
        self._span = span

    @property
    def recording(self) -> bool:
        """False when telemetry is off — lets callers skip building payloads."""
        return self._span.is_recording()

    def set(self, attributes: Mapping[str, Any]) -> None:
        """Record non-content attributes (ids, names, counts, flags)."""
        if self.recording:
            self._span.set_attributes(_clean(attributes))

    def set_content(self, attributes: Mapping[str, Any]) -> None:
        """Record content attributes, only when content capture is on."""
        if self.recording and telemetry.capture_content():
            self._span.set_attributes(_clean(attributes))

    def set_input(self, value: Any) -> None:
        """The span's ``input.value`` — text as-is, anything else as JSON."""
        self._set_io(SpanAttributes.INPUT_VALUE, SpanAttributes.INPUT_MIME_TYPE, value)

    def set_output(self, value: Any) -> None:
        """The span's ``output.value`` — text as-is, anything else as JSON."""
        self._set_io(SpanAttributes.OUTPUT_VALUE, SpanAttributes.OUTPUT_MIME_TYPE, value)

    def _set_io(self, value_key: str, mime_key: str, value: Any) -> None:
        if isinstance(value, str):
            self.set_content({value_key: value, mime_key: _TEXT})
        else:
            self.set_content({value_key: to_json(value), mime_key: _JSON})

    def ok(self) -> None:
        self._span.set_status(Status(StatusCode.OK))

    def error(self, code: str, detail: str | None = None) -> None:
        """Mark the span failed with a known, client-safe code."""
        self._span.set_attribute("error.type", code)
        self._span.set_status(Status(StatusCode.ERROR, detail or code))

    def fail(self, exc: BaseException) -> None:
        """Mark the span failed from an exception, without its raw message."""
        if isinstance(exc, asyncio.CancelledError):
            self.error("cancelled")
            return
        if isinstance(exc, AppError):
            self._span.add_event(
                "exception",
                _clean({"exception.type": type(exc).__name__, "exception.message": exc.detail}),
            )
            self.error(exc.code, exc.detail)
            return
        self._span.add_event("exception", {"exception.type": type(exc).__name__})
        self.error(type(exc).__name__)


@contextmanager
def observe(
    name: str,
    kind: OpenInferenceSpanKindValues,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[ObservedSpan]:
    """Open a current span of OpenInference ``kind``; children nest under it.

    Only for straight-line code — never around a ``yield`` in an async
    generator, whose finalization can run in another context (use
    :class:`app.observability.llm.LlmCall` there). An escaping exception,
    cancellation included, marks the span failed and propagates; a span the
    caller did not mark is left unset (Phoenix shows it as completed).
    """
    span = telemetry.tracer().start_span(
        name, attributes={_KIND: kind.value, **_clean(attributes or {})}
    )
    observed = ObservedSpan(span)
    with use_span(span, end_on_exit=True, record_exception=False, set_status_on_exception=False):
        try:
            yield observed
        except BaseException as exc:
            observed.fail(exc)
            raise


def session_attributes(
    *, session_id: UUID | str | None, user_id: UUID | str | None
) -> dict[str, Any]:
    """The ids Phoenix's Sessions view groups turns by."""
    return {SpanAttributes.SESSION_ID: session_id, SpanAttributes.USER_ID: user_id}


def metadata_attribute(metadata: Mapping[str, Any]) -> dict[str, str]:
    """OpenInference ``metadata``: one JSON object of filterable, non-content facts."""
    present = {k: v for k, v in metadata.items() if v is not None}
    return {SpanAttributes.METADATA: to_json(present)} if present else {}


def retrieval_document_attributes(
    documents: Sequence[tuple[str, float | None, str | None, Mapping[str, Any]]],
    *,
    with_content: bool,
) -> dict[str, Any]:
    """Flatten ``(id, score, content, metadata)`` rows into ``retrieval.documents.*``."""
    attributes: dict[str, Any] = {}
    prefix = SpanAttributes.RETRIEVAL_DOCUMENTS
    for index, (doc_id, score, content, metadata) in enumerate(documents):
        base = f"{prefix}.{index}."
        attributes[base + DocumentAttributes.DOCUMENT_ID] = doc_id
        attributes[base + DocumentAttributes.DOCUMENT_SCORE] = score
        attributes[base + DocumentAttributes.DOCUMENT_METADATA] = to_json(metadata)
        if with_content:
            attributes[base + DocumentAttributes.DOCUMENT_CONTENT] = content
    return attributes
