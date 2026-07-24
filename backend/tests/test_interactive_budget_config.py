"""Interactive terminal-publish budget guardrails (R2-8, #489 AC-4).

The interactive answer path promises a typed terminal within a 30s worst-case
ceiling. Round-1 capped the per-turn deadline at ``ceiling − margin`` but only
*reserved* the margin — nothing bounded terminal publication, which awaits a Redis
pipeline that can stall unbounded, so the real worst case stayed unbounded (R2-8).
The runtime now bounds the terminal publish by that margin; config enforces
``interactive_deadline + margin <= ceiling`` at boot so the <=30s promise cannot be
silently overridden into a lie (it was a hopeful comment before).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import (
    _INTERACTIVE_TERMINAL_PUBLISH_MARGIN_SECONDS,
    _INTERACTIVE_WORST_CASE_CEILING_SECONDS,
    _MAX_INTERACTIVE_TIMEOUT_SECONDS,
    Settings,
)

_BASE = {
    "DATABASE_URL": "postgresql+asyncpg://t:t@localhost/t",
    "REDIS_URL": "redis://localhost",
    "CELERY_BROKER_URL": "redis://localhost",
    "CELERY_RESULT_BACKEND": "redis://localhost",
    "S3_ENDPOINT_URL": "http://localhost:9000",
    "S3_ACCESS_KEY": "t",
    "S3_SECRET_KEY": "tt",
    "S3_BUCKET": "b",
}


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **_BASE, **overrides)  # type: ignore[arg-type]


def test_terminal_publish_margin_default() -> None:
    """The margin defaults to the same value the interactive-deadline ceiling reserves,
    and deadline + margin sits comfortably under the 30s worst-case ceiling."""
    s = _settings()
    assert s.llm_terminal_publish_margin_seconds == _INTERACTIVE_TERMINAL_PUBLISH_MARGIN_SECONDS
    assert (
        s.llm_interactive_timeout_seconds + s.llm_terminal_publish_margin_seconds
        <= _INTERACTIVE_WORST_CASE_CEILING_SECONDS
    )


def test_deadline_plus_margin_over_ceiling_fails_fast() -> None:
    """R2-8: a margin that, added to the interactive deadline, would exceed the 30s
    ceiling refuses to boot — the whole producer→terminal path must land a typed
    terminal within the worst-case bound, so an over-budget config is a hard error."""
    with pytest.raises(ValidationError):
        _settings(
            LLM_INTERACTIVE_TIMEOUT_SECONDS=_MAX_INTERACTIVE_TIMEOUT_SECONDS,  # 27s
            LLM_TERMINAL_PUBLISH_MARGIN_SECONDS=4.0,  # 27 + 4 = 31 > 30 → rejected
        )


def test_deadline_at_ceiling_minus_margin_is_accepted() -> None:
    """The exact boundary is valid: the maximum deadline plus the default margin sums
    to precisely the ceiling (27 + 3 = 30) — accepted, not overshooting."""
    s = _settings(
        LLM_INTERACTIVE_TIMEOUT_SECONDS=_MAX_INTERACTIVE_TIMEOUT_SECONDS,
        LLM_TERMINAL_PUBLISH_MARGIN_SECONDS=_INTERACTIVE_TERMINAL_PUBLISH_MARGIN_SECONDS,
    )
    assert (
        s.llm_interactive_timeout_seconds + s.llm_terminal_publish_margin_seconds
        == _INTERACTIVE_WORST_CASE_CEILING_SECONDS
    )


def test_non_positive_margin_fails_fast() -> None:
    """A zero/negative margin would leave no budget to publish the terminal — reject."""
    with pytest.raises(ValidationError):
        _settings(LLM_TERMINAL_PUBLISH_MARGIN_SECONDS=0)
