"""Contract tests for grounded-answer tool-use policy v4 (#635)."""

from __future__ import annotations

from app.services.prompts.grounded_answer import GROUNDED_SYSTEM_PROMPT, PROMPT_VERSION


def _prompt() -> str:
    return " ".join(GROUNDED_SYSTEM_PROMPT.lower().split())


def test_grounded_answer_prompt_v4_broadens_short_searches_and_refines_thin_results() -> None:
    prompt = _prompt()

    assert PROMPT_VERSION == "grounded-answer-v4"
    assert "short" in prompt and "broad" in prompt
    assert "refine" in prompt and "search_text" in prompt


def test_grounded_answer_prompt_cites_recalled_passages_and_never_document_labels() -> None:
    prompt = _prompt()

    assert "[s1]" in prompt and "[w1]" in prompt
    assert "[d1]" in prompt
    assert "not a citation" in prompt or "not citations" in prompt
    assert "exact" in prompt and "recall" in prompt


def test_grounded_answer_prompt_stops_when_sufficient_asks_intent_and_avoids_repeats() -> None:
    prompt = _prompt()

    assert "sufficient" in prompt or "enough" in prompt
    assert "stop" in prompt
    assert "ask_user" in prompt
    assert "intent" in prompt or "ambiguous" in prompt
    assert "identical" in prompt or "same query" in prompt


def test_grounded_answer_prompt_does_not_depend_on_a_provider_specific_tool_api() -> None:
    prompt = _prompt()

    provider_specific = (
        "openai",
        "anthropic",
        "openrouter",
        "claude",
        "gemini",
        "tool_choice",
        "function_calling",
        "parallel_tool_calls",
    )
    assert not any(feature in prompt for feature in provider_specific)
    assert "provided tools" in prompt or "available tools" in prompt
