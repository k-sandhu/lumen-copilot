"""Domain types for model interaction.

These are the types the ``llm/`` adapter returns and accepts — callers of the
gateway see *these*, never a LiteLLM object (ADR-0004 adapter rule 1). Pure
dataclasses: no I/O, no vendor imports. When the provider behind LiteLLM
changes, the gateway maps to/from these and nothing upstream moves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Role(str, Enum):
    """The author of a chat message."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """A single message in a chat exchange."""

    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Token accounting for a completion (cost & observability, CC-8).

    Providers do not always report usage; fields default to ``0`` so callers can
    sum without ``None`` checks. ``total`` falls back to ``prompt + completion``
    when the provider omits it.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


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
class Embedding:
    """An embedding vector for a single input."""

    vector: list[float] = field(default_factory=list)
    model: str = ""
