"""Web-search config guardrails (ADR-0014, issue #219).

Pin the ``web_search`` settings: off-by-default (governance, ADR-0014 §5), the
sane defaults, and the fail-fast validators (a non-positive k/window/limit or a
negative fetch-top-n must refuse to boot rather than disable bounding).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings

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


def test_web_search_is_off_by_default() -> None:
    """AC-3 posture: web search is disabled unless a deploy opts in (ADR-0014 §5)."""
    assert _settings().web_search_enabled is False


def test_web_search_defaults() -> None:
    s = _settings()
    assert s.web_search_default_k == 5
    assert s.web_search_max_k == 10
    assert s.web_search_fetch_top_n == 3
    assert s.web_search_rate_max_per_window == 20
    assert s.web_search_rate_window_seconds == 60
    # The endpoint default targets the mapped host port so host-side dev/tests
    # reach the same engine compose runs (mirrors the OpenSearch pattern).
    assert s.web_search_endpoint == "http://localhost:47187"


def test_web_search_enable_flag_from_env() -> None:
    assert _settings(WEB_SEARCH_ENABLED=True).web_search_enabled is True


@pytest.mark.parametrize(
    "field",
    [
        "WEB_SEARCH_DEFAULT_K",
        "WEB_SEARCH_MAX_K",
        "WEB_SEARCH_RATE_MAX_PER_WINDOW",
        "WEB_SEARCH_RATE_WINDOW_SECONDS",
    ],
)
def test_non_positive_counts_fail_fast(field: str) -> None:
    """A zero/negative k or rate window/limit disables bounding — must not boot."""
    with pytest.raises(ValidationError):
        _settings(**{field: 0})


def test_negative_fetch_top_n_fails_fast() -> None:
    with pytest.raises(ValidationError):
        _settings(WEB_SEARCH_FETCH_TOP_N=-1)


def test_zero_fetch_top_n_is_allowed_snippets_only() -> None:
    """``0`` is a valid config: snippets only, no result-page fetch."""
    assert _settings(WEB_SEARCH_FETCH_TOP_N=0).web_search_fetch_top_n == 0
