"""Domain types for model interaction.

These are the types the ``llm/`` adapter returns and accepts — callers of the
gateway see *these*, never a LiteLLM object (ADR-0004 adapter rule 1). Pure
dataclasses: no I/O, no vendor imports. When the provider behind LiteLLM
changes, the gateway maps to/from these and nothing upstream moves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Role(str, Enum):
    """The author of a chat message."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A model-requested tool invocation (function/tool-calling).

    The provider-agnostic form of a tool call the model emitted during a
    completion: a stable ``id`` (correlates the call to its result), the tool
    ``name`` (matches a :class:`ToolSpec`), and the JSON-decoded ``arguments``.
    The gateway parses the vendor's tool-call payload into this so callers never
    see a LiteLLM/OpenAI tool object (ADR-0004 adapter rule).
    """

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """A single message in a chat exchange.

    For an ``assistant`` turn that requested tools, ``tool_calls`` carries them
    (and ``content`` may be empty); for a ``tool`` turn, ``tool_call_id`` ties
    the result back to the originating call. Both default empty for the common
    plain text message.
    """

    role: Role
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Token accounting for a completion (cost & observability, CC-8).

    Providers do not always report usage; fields default to ``0`` so callers can
    sum without ``None`` checks. ``total`` falls back to ``prompt + completion``
    when the provider omits it.

    ``cached_prompt_tokens`` / ``cache_write_tokens`` are the provider cache
    accounting (#409, ADR-0016 §2.6): prompt tokens served from the provider's
    prompt cache, and tokens written INTO it (Anthropic-style cache creation).
    Both are subsets/adjuncts of ``prompt_tokens`` bookkeeping — they never count
    toward ``total_tokens`` on their own — and default ``0`` for providers that
    report no cache detail.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_prompt_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True, slots=True)
class Completion:
    """A non-streamed chat completion result."""

    content: str
    model: str
    finish_reason: str | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)


@dataclass(frozen=True, slots=True)
class CompletionChunk:
    """One incremental piece of a streamed completion."""

    delta: str
    index: int


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool the model may call (the function/tool-calling schema).

    Provider-agnostic description of a callable the chat runtime exposes to the
    model: its ``name`` (the LLM references this in a :class:`ToolCall`), a
    human ``description`` the model reads to decide when to use it, and a JSON
    Schema for its ``parameters``. The gateway renders this into the vendor's
    tool format; callers never construct a LiteLLM/OpenAI tool dict.
    """

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One item from a tool-aware streamed completion.

    A streamed tool-calling turn yields a sequence of these. Exactly one of the
    payload fields is set per event:

    * ``text`` — an incremental answer token chunk (most events);
    * ``tool_calls`` — the model finished the turn by requesting tools (the
      runtime runs them, appends results, and streams the next turn);
    * ``finish_reason`` — the terminal marker for this turn (``stop`` /
      ``tool_calls`` / ``length`` / ...), carried on the last event;
    * ``usage`` — token accounting, when the provider reports it at end-of-turn;
    * ``tool_call_started`` — the ADR-0016 §6 classification signal (#414):
      the provider's FIRST tool-call fragment arrived, so this turn is
      provably tool-calling (its buffered text is narration). Provider-neutral
      — no vendor fragment shape leaks; the assembled calls still arrive via
      ``tool_calls`` at end-of-turn.

    Keeping this one flat type (rather than a union) keeps the consumer loop a
    single ``async for`` and the gateway the only place vendor chunks are parsed.
    """

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None
    usage: TokenUsage | None = None
    #: The first-tool-fragment classification signal (ADR-0016 §6, #414).
    tool_call_started: bool = False


@dataclass(frozen=True, slots=True)
class Embedding:
    """An embedding vector for a single input."""

    vector: list[float] = field(default_factory=list)
    model: str = ""


@dataclass(frozen=True, slots=True)
class TranscriptionWord:
    """One provider-neutral, diarized word with citable local timing.

    Times are integer milliseconds on the audio request's zero-based timeline.
    ``speaker_label`` is the provider's request-local diarizer label; media
    ingestion maps it to a stable file-local ``speaker-N`` id before anything is
    persisted or exposed.  It is deliberately not a biometric identity.
    """

    text: str
    start_ms: int
    end_ms: int
    speaker_label: str
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class Transcription:
    """A speech-to-text result whose evidence is safe to put on a time axis."""

    text: str
    words: tuple[TranscriptionWord, ...]
    language: str | None
    model: str
