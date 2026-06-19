"""Versioned, in-repo chat prompts (CC-6 #24, backend/AGENTS.md).

Prompts are versioned source, not magic strings at the call site: the
grounded-answer system prompt lives here so it is reviewable, testable, and
changes land as code. The chat runtime imports :data:`GROUNDED_SYSTEM_PROMPT`
and :func:`render_citation_marker_guidance`.
"""

from app.services.prompts.grounded_answer import (
    GROUNDED_SYSTEM_PROMPT,
    NO_SOURCES_FALLBACK,
    PROMPT_VERSION,
)

__all__ = [
    "GROUNDED_SYSTEM_PROMPT",
    "NO_SOURCES_FALLBACK",
    "PROMPT_VERSION",
]
