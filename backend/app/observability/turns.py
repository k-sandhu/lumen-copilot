"""Turn, headless-run, tool and retrieval spans (ADR-0027 §2), with their metrics.

Each helper is a context manager that opens one OpenInference span, records
the matching metric when it closes, and yields a small reporter for the caller
to describe the outcome. Children opened inside (model calls, tools,
retrieval) nest under it through the OpenTelemetry context, including across
``asyncio`` tasks spawned inside, which copy the context when created.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes

from app.domain.retrieval import RetrievedPassage
from app.domain.tools import ToolResult
from app.observability import telemetry
from app.observability.spans import (
    ObservedSpan,
    metadata_attribute,
    observe,
    retrieval_document_attributes,
    session_attributes,
)


class TurnSpan:
    """What the chat runtime reports onto its turn span."""

    def __init__(self, span: ObservedSpan) -> None:
        self._span = span
        self.outcome = "error"

    def answered(
        self,
        *,
        answer: str,
        model_used: str,
        finish_reason: str,
        citation_count: int,
        total_tokens: int,
    ) -> None:
        self.outcome = "ok"
        self._span.set(
            {
                "lumen.chat.model_used": model_used,
                "lumen.chat.finish_reason": finish_reason,
                "lumen.chat.citation_count": citation_count,
                SpanAttributes.LLM_TOKEN_COUNT_TOTAL: total_tokens,
            }
        )
        self._span.set_output(answer)
        self._span.ok()

    def failed(self, code: str, detail: str | None = None) -> None:
        self.outcome = "error"
        self._span.error(code, detail)


@contextmanager
def observe_turn(
    *,
    session_id: UUID,
    user_id: UUID,
    tenant_id: UUID,
    model: str,
    question: str,
    interactive: bool,
    assistant_id: UUID | None = None,
    request_id: str | None = None,
) -> Iterator[TurnSpan]:
    """The AGENT root span of one answer turn (chat, headless run, or preview)."""
    started = time.monotonic()
    mode = "interactive" if interactive else "headless"
    with observe(
        "chat.turn",
        OpenInferenceSpanKindValues.AGENT,
        {
            **session_attributes(session_id=session_id, user_id=user_id),
            **metadata_attribute(
                {
                    "tenant_id": tenant_id,
                    "assistant_id": assistant_id,
                    "model": model,
                    "mode": mode,
                    "request_id": request_id,
                }
            ),
            "lumen.tenant_id": str(tenant_id),
        },
    ) as span:
        span.set_input(question)
        turn = TurnSpan(span)
        try:
            yield turn
        except asyncio.CancelledError:
            turn.outcome = "cancelled"
            raise
        except BaseException:
            turn.outcome = "error"
            raise
        finally:
            attributes = {"outcome": turn.outcome, "mode": mode}
            telemetry.instruments().chat_turns.add(1, attributes)
            telemetry.instruments().chat_turn_duration.record(
                time.monotonic() - started, attributes
            )


@contextmanager
def observe_headless_run(
    *, run_id: UUID, assistant_id: UUID | None, trigger: str | None
) -> Iterator[ObservedSpan]:
    """The CHAIN span of one headless/scheduled run; its turn nests inside."""
    with observe(
        "headless_run",
        OpenInferenceSpanKindValues.CHAIN,
        {
            "lumen.run.id": str(run_id),
            **metadata_attribute(
                {"run_id": run_id, "assistant_id": assistant_id, "trigger": trigger}
            ),
        },
    ) as span:
        yield span


class ToolSpan:
    """What the tool runner reports onto its tool span."""

    def __init__(self, span: ObservedSpan) -> None:
        self._span = span
        self.outcome = "error"

    def finished(self, result: ToolResult) -> None:
        self.outcome = "ok" if result.ok else (result.error or "error")
        self._span.set(
            {
                "lumen.tool.ok": result.ok,
                "lumen.tool.error": result.error,
                "lumen.tool.denied_reason": result.denied_reason,
                "lumen.tool.duration_ms": result.duration_ms,
                "lumen.tool.hit_count": result.hit_count,
                "lumen.tool.summary": result.summary,
            }
        )
        self._span.set_output(result.content)
        if result.ok:
            self._span.ok()
        else:
            self._span.error(result.error or "error", result.summary)


@contextmanager
def observe_tool(
    *,
    name: str,
    call_id: str,
    arguments: Mapping[str, Any],
    ordinal: int | None,
    resolved_name: str | None = None,
) -> Iterator[ToolSpan]:
    """A TOOL span with a resolved name (or ``unknown``) for bounded naming/metrics."""
    started = time.monotonic()
    bounded_name = resolved_name if resolved_name is not None else "unknown"
    with observe(
        f"tool.{bounded_name}",
        OpenInferenceSpanKindValues.TOOL,
        {
            SpanAttributes.TOOL_NAME: name,
            "lumen.tool.call_id": call_id,
            "lumen.tool.ordinal": ordinal,
        },
    ) as span:
        span.set_input(dict(arguments))
        tool = ToolSpan(span)
        try:
            yield tool
        except asyncio.CancelledError:
            tool.outcome = "cancelled"
            raise
        finally:
            attributes = {"lumen.tool.name": bounded_name, "outcome": tool.outcome}
            telemetry.instruments().tool_calls.add(1, attributes)
            telemetry.instruments().tool_duration.record(time.monotonic() - started, attributes)


class RetrievalSpan:
    """What the retrieval service reports onto its retriever span."""

    def __init__(self, span: ObservedSpan) -> None:
        self._span = span

    def retrieved(self, passages: Sequence[RetrievedPassage], *, engine_hits: int) -> None:
        self._span.set(
            {"lumen.retrieval.engine_hits": engine_hits, "lumen.retrieval.passages": len(passages)}
        )
        if self._span.recording:
            documents = [
                (
                    str(passage.chunk_id),
                    passage.score,
                    passage.text,
                    {
                        "document_id": str(passage.document_id),
                        "document_name": passage.document_name,
                        "ord": passage.ord,
                    },
                )
                for passage in passages
            ]
            self._span.set(retrieval_document_attributes(documents, with_content=False))
            self._span.set_content(retrieval_document_attributes(documents, with_content=True))
        self._span.ok()


@contextmanager
def observe_retrieval(
    *,
    query: str,
    k: int,
    collection_ids: Sequence[UUID] | None,
    document_ids: Sequence[UUID] | None,
) -> Iterator[RetrievalSpan]:
    """The RETRIEVER span of one permission-filtered passage search."""
    started = time.monotonic()
    outcome = "error"
    with observe(
        "retrieval.search",
        OpenInferenceSpanKindValues.RETRIEVER,
        {
            "lumen.retrieval.k": k,
            "lumen.retrieval.collection_filter": len(collection_ids or ()),
            "lumen.retrieval.document_filter": len(document_ids or ()),
        },
    ) as span:
        span.set_input(query)
        try:
            yield RetrievalSpan(span)
            outcome = "ok"
        finally:
            telemetry.instruments().retrieval_duration.record(
                time.monotonic() - started, {"outcome": outcome}
            )
