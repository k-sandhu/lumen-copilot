"""Model-call spans and metrics, recorded at the gateway chokepoint (ADR-0027 §2-3).

:class:`LlmCall` is written for streaming: its span is *not* made current, so
it is safe to hold across the ``yield``s of the gateway's async generators, and
it ends exactly once — on success, failure, or the consumer closing the stream
early. It takes domain types only and is never handed credentials: an API key
or base URL has no parameter to travel through, whatever the content setting.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from openinference.semconv.trace import (
    EmbeddingAttributes,
    MessageAttributes,
    OpenInferenceSpanKindValues,
    SpanAttributes,
    ToolAttributes,
    ToolCallAttributes,
)

from app.domain.llm import ChatMessage, Role, TokenUsage, ToolCall, ToolSpec
from app.observability import telemetry
from app.observability.spans import ObservedSpan, observe, to_json

_TOOLS = SpanAttributes.LLM_TOOLS
_EMBEDDINGS = SpanAttributes.EMBEDDING_EMBEDDINGS
_CACHE_READ = SpanAttributes.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ
_CACHE_WRITE = SpanAttributes.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_WRITE
_OPERATION_CHAT = "chat"
_OPERATION_EMBEDDINGS = "embeddings"
# Embedding texts recorded per span: a query embed is one text, an ingestion
# batch can be hundreds of chunks the trace store does not need verbatim.
_MAX_EMBEDDING_TEXTS = 16


def _provider(model: str) -> str:
    """The routing prefix of a LiteLLM model id (``openrouter/openai/x`` → ``openrouter``)."""
    head, sep, _ = model.partition("/")
    return head if sep else "unknown"


def _tool_call_attributes(
    prefix: str, calls: Sequence[ToolCall]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(structure, content)`` attributes for ``calls`` under ``prefix``."""
    structure: dict[str, Any] = {}
    content: dict[str, Any] = {}
    for index, call in enumerate(calls):
        base = f"{prefix}{MessageAttributes.MESSAGE_TOOL_CALLS}.{index}."
        structure[base + ToolCallAttributes.TOOL_CALL_ID] = call.id
        structure[base + ToolCallAttributes.TOOL_CALL_FUNCTION_NAME] = call.name
        content[base + ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON] = to_json(
            call.arguments
        )
    return structure, content


def _message_attributes(
    key: str, messages: Sequence[ChatMessage]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(structure, content)`` attributes flattening ``messages`` under ``key``.

    Roles, tool names and call ids are structure (kept with content capture
    off); message text and tool arguments are content.
    """
    structure: dict[str, Any] = {}
    content: dict[str, Any] = {}
    for index, message in enumerate(messages):
        base = f"{key}.{index}."
        structure[base + MessageAttributes.MESSAGE_ROLE] = message.role.value
        structure[base + MessageAttributes.MESSAGE_NAME] = message.name
        structure[base + MessageAttributes.MESSAGE_TOOL_CALL_ID] = message.tool_call_id
        content[base + MessageAttributes.MESSAGE_CONTENT] = message.content
        calls_structure, calls_content = _tool_call_attributes(base, message.tool_calls)
        structure.update(calls_structure)
        content.update(calls_content)
    return structure, content


class LlmCall:
    """One model call: an OpenInference LLM span plus its duration/token metrics."""

    def __init__(
        self,
        *,
        method: str,
        model: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
        invocation_parameters: Mapping[str, Any] | None = None,
    ) -> None:
        self._model = model
        self._started = time.monotonic()
        self._first_chunk_at: float | None = None
        self._ended = False
        span = telemetry.tracer().start_span(
            f"llm.{method}",
            attributes={
                SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.LLM.value,
                SpanAttributes.LLM_MODEL_NAME: model,
                SpanAttributes.LLM_PROVIDER: _provider(model),
                SpanAttributes.LLM_INVOCATION_PARAMETERS: to_json(
                    {k: v for k, v in (invocation_parameters or {}).items() if v is not None}
                ),
            },
        )
        self._span = ObservedSpan(span)
        self._raw = span
        if not self._span.recording:
            return
        structure, content = _message_attributes(SpanAttributes.LLM_INPUT_MESSAGES, messages)
        self._span.set(structure)
        self._span.set_content(content)
        if tools:
            self._span.set({"lumen.llm.tool_names": [tool.name for tool in tools]})
            self._span.set_content(
                {
                    f"{_TOOLS}.{index}.{ToolAttributes.TOOL_JSON_SCHEMA}": to_json(
                        {
                            "type": "function",
                            "function": {
                                "name": tool.name,
                                "description": tool.description,
                                "parameters": tool.parameters,
                            },
                        }
                    )
                    for index, tool in enumerate(tools)
                }
            )
        if messages:
            self._span.set_input(messages[-1].content)

    @property
    def records_content(self) -> bool:
        """Whether this call records content, so streams can skip retaining text."""
        return self._span.recording and telemetry.capture_content()

    def first_chunk(self) -> None:
        """Mark the first streamed content or tool-call chunk (time to first token)."""
        if self._first_chunk_at is not None or self._ended:
            return
        self._first_chunk_at = time.monotonic()
        elapsed = self._first_chunk_at - self._started
        self._span.set({"lumen.llm.time_to_first_token_ms": round(elapsed * 1000)})
        telemetry.instruments().llm_ttft.record(elapsed, {"gen_ai.request.model": self._model})

    def succeed(
        self,
        *,
        content: str,
        tool_calls: Sequence[ToolCall] = (),
        finish_reason: str | None = None,
        usage: TokenUsage | None = None,
    ) -> None:
        """Record the model's answer and usage, then end the span."""
        if self._ended:
            return
        if self._span.recording:
            prefix = f"{SpanAttributes.LLM_OUTPUT_MESSAGES}.0."
            structure, calls_content = _tool_call_attributes(prefix, tool_calls)
            self._span.set(
                {
                    prefix + MessageAttributes.MESSAGE_ROLE: Role.ASSISTANT.value,
                    "llm.finish_reason": finish_reason,
                    **structure,
                }
            )
            self._span.set_content(
                {prefix + MessageAttributes.MESSAGE_CONTENT: content, **calls_content}
            )
            if tool_calls and not content:
                self._span.set_output(
                    [{"name": call.name, "arguments": call.arguments} for call in tool_calls]
                )
            else:
                self._span.set_output(content)
        if usage is not None:
            self._record_usage(usage)
        self._span.ok()
        self._end(error_type=None)

    def fail(self, exc: BaseException) -> None:
        """Record a provider failure (or cancellation) and end the span."""
        if self._ended:
            return
        self._span.fail(exc)
        error_type = getattr(exc, "code", None) or type(exc).__name__
        self._end(error_type=str(error_type))

    def abandon(self) -> None:
        """End the span when the consumer closed the stream before it finished."""
        if self._ended:
            return
        self._span.set({"lumen.llm.stream_closed_early": True})
        self._end(error_type=None)

    def _record_usage(self, usage: TokenUsage) -> None:
        self._span.set(
            {
                SpanAttributes.LLM_TOKEN_COUNT_PROMPT: usage.prompt_tokens,
                SpanAttributes.LLM_TOKEN_COUNT_COMPLETION: usage.completion_tokens,
                SpanAttributes.LLM_TOKEN_COUNT_TOTAL: usage.total_tokens,
                _CACHE_READ: usage.cached_prompt_tokens,
                _CACHE_WRITE: usage.cache_write_tokens,
            }
        )
        record_tokens(_OPERATION_CHAT, self._model, usage)

    def _end(self, *, error_type: str | None) -> None:
        self._ended = True
        attributes = {"gen_ai.operation.name": _OPERATION_CHAT, "gen_ai.request.model": self._model}
        if error_type is not None:
            attributes["error.type"] = error_type
        telemetry.instruments().llm_duration.record(time.monotonic() - self._started, attributes)
        self._raw.end()


def record_tokens(operation: str, model: str, usage: TokenUsage) -> None:
    """Count billed tokens by type (``input`` / ``output`` / ``cache_read`` / ``cache_write``)."""
    counter = telemetry.instruments().llm_tokens
    for token_type, count in (
        ("input", usage.prompt_tokens),
        ("output", usage.completion_tokens),
        ("cache_read", usage.cached_prompt_tokens),
        ("cache_write", usage.cache_write_tokens),
    ):
        if count > 0:
            counter.add(
                count,
                {
                    "gen_ai.operation.name": operation,
                    "gen_ai.request.model": model,
                    "gen_ai.token.type": token_type,
                },
            )


class EmbeddingSpan:
    """What the gateway reports onto an embedding span."""

    def __init__(self, span: ObservedSpan, model: str) -> None:
        self._span = span
        self._model = model

    def cache_hit(self) -> None:
        self._span.set({"lumen.embedding.cache_hit": True})

    def succeed(self, usage: TokenUsage) -> None:
        self._span.set({SpanAttributes.LLM_TOKEN_COUNT_PROMPT: usage.prompt_tokens})
        record_tokens(_OPERATION_EMBEDDINGS, self._model, usage)
        self._span.ok()


@contextmanager
def observe_embedding(*, model: str, inputs: Sequence[str]) -> Iterator[EmbeddingSpan]:
    """An OpenInference EMBEDDING span (plus the duration metric) around one embed call."""
    started = time.monotonic()
    error_type: str | None = None
    with observe(
        "embedding",
        OpenInferenceSpanKindValues.EMBEDDING,
        {
            SpanAttributes.EMBEDDING_MODEL_NAME: model,
            "lumen.embedding.input_count": len(inputs),
        },
    ) as span:
        span.set_content(
            {
                f"{_EMBEDDINGS}.{index}.{EmbeddingAttributes.EMBEDDING_TEXT}": text
                for index, text in enumerate(inputs[:_MAX_EMBEDDING_TEXTS])
            }
        )
        try:
            yield EmbeddingSpan(span, model)
        except BaseException as exc:
            error_type = str(getattr(exc, "code", None) or type(exc).__name__)
            raise
        finally:
            attributes = {
                "gen_ai.operation.name": _OPERATION_EMBEDDINGS,
                "gen_ai.request.model": model,
            }
            if error_type is not None:
                attributes["error.type"] = error_type
            telemetry.instruments().llm_duration.record(time.monotonic() - started, attributes)
