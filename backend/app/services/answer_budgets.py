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
from decimal import Decimal

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

    ``None`` disables the optional cost, deadline and attempt bounds. Tool,
    round and cumulative token ceilings always apply, including to legacy
    callers that do not supply settings. The tool phase reserves unknown
    provider usage conservatively; billed usage remains a separate fact.
    """

    max_tool_turns: int = 20
    turn_timeout_seconds: float | None = None
    interactive_max_attempts: int | None = None
    answer_max_tokens: int | None = None
    wall_clock_seconds: float | None = None
    max_external_calls: int | None = None
    max_model_calls: int | None = None
    max_tool_calls: int = 60
    max_calls_per_round: int = 8
    max_input_tokens: int = 500_000
    max_output_tokens: int = 16_000
    max_cost_usd: Decimal | None = None

    def __post_init__(self) -> None:
        for name in ("max_tool_calls", "max_input_tokens", "max_output_tokens"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if type(self.max_calls_per_round) is not int or self.max_calls_per_round < 1:
            raise ValueError("max_calls_per_round must be a positive integer")
        if self.max_cost_usd is not None and (
            not isinstance(self.max_cost_usd, Decimal)
            or not self.max_cost_usd.is_finite()
            or self.max_cost_usd < 0
        ):
            raise ValueError("max_cost_usd must be a finite nonnegative Decimal")

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
        max_tool_calls=settings.chat_max_tool_calls_per_answer,
        max_calls_per_round=settings.chat_max_calls_per_round,
        max_input_tokens=settings.chat_max_input_tokens_per_answer,
        max_output_tokens=settings.chat_max_output_tokens_per_answer,
        max_cost_usd=settings.chat_max_cost_usd_per_answer,
    )


__all__ = ["AnswerBudgets", "budgets_from_settings", "is_external_tool"]
