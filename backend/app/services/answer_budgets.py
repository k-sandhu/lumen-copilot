"""Shared per-answer budgets for every ``ChatRuntime`` caller (#574).

One frozen value object carries the spend bounds that used to be wired only
on the live-chat path (per-turn deadline, retry cap, answer-token ceiling):
live chat, assistant Test, and headless runs all build theirs from
:func:`budgets_from_settings`, so a looping or hostile model hits the same
walls everywhere (spec 0028). ``max_model_calls`` counts all attempts,
including retries, fallback routes, synthesis, continuation and suggestions.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings

# Native tool names that leave the deployment (public web). MCP tools are
# namespaced ``mcp:<slug>:<tool>`` (#227) and matched by prefix.
_EXTERNAL_TOOL_NAMES = frozenset({"web_search", "web_fetch"})
_MCP_PREFIX = "mcp:"


def is_external_tool(tool_name: str) -> bool:
    """True iff ``tool_name`` performs an external (web/MCP) call."""
    return tool_name in _EXTERNAL_TOOL_NAMES or tool_name.startswith(_MCP_PREFIX)


@dataclass(frozen=True, slots=True)
class AnswerBudgets:
    """The spend bounds for one answer, shared by all runtime callers.

    ``None`` fields mean "no runtime-level bound" (the underlying gateway or
    rate-limit bound still applies). Defaults mirror the module defaults the
    runtime used before #574 so an un-wired caller keeps its old behaviour
    except for the new wall-clock/external caps, which default to the
    configured live-chat values in :func:`budgets_from_settings`.
    """

    max_tool_turns: int = 20
    turn_timeout_seconds: float | None = None
    interactive_max_attempts: int | None = None
    answer_max_tokens: int | None = None
    wall_clock_seconds: float | None = None
    max_external_calls: int | None = None
    max_model_calls: int | None = None

    def effective_max_tool_turns(self, *, tenant_override: int | None) -> int:
        """The loop bound: the tenant override (#148) beats the shared default."""
        chosen = tenant_override if tenant_override is not None else self.max_tool_turns
        return max(1, chosen)


def budgets_from_settings(settings: Settings) -> AnswerBudgets:
    """Build the shared budgets from the live-chat configured values."""
    return AnswerBudgets(
        max_tool_turns=settings.chat_max_tool_turns,
        turn_timeout_seconds=settings.llm_interactive_timeout_seconds,
        interactive_max_attempts=settings.llm_interactive_max_attempts,
        answer_max_tokens=settings.chat_answer_max_tokens or None,
        wall_clock_seconds=settings.chat_answer_wall_clock_seconds,
        max_external_calls=settings.chat_max_external_calls_per_answer,
        max_model_calls=settings.chat_max_model_calls_per_answer,
    )


__all__ = ["AnswerBudgets", "budgets_from_settings", "is_external_tool"]
