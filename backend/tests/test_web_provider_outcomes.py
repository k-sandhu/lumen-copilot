"""Malformed provider output must not be reported as an honest empty search."""

import httpx
import pytest

from app.search_web.client import (
    SearxngClient,
    WebSearchRateLimited,
    WebSearchUnavailable,
    map_searxng_results,
)


@pytest.mark.parametrize("payload", [{}, {"results": "wrong"}, [], {"results": [None]}])
def test_unusable_provider_payload_has_parse_error(payload: object) -> None:
    with pytest.raises(WebSearchUnavailable) as error:
        map_searxng_results(payload, k=3)
    assert error.value.reason == "parse_error"


def test_provider_empty_is_distinct_from_parse_error() -> None:
    assert map_searxng_results({"results": []}, k=3) == ()


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1/source", "https://u:p@example.org/", "http://[broken"]
)
def test_unsafe_or_malformed_result_is_blocked(url: str) -> None:
    with pytest.raises(WebSearchUnavailable) as error:
        map_searxng_results({"results": [{"url": url, "content": "unsafe"}]}, k=3)
    assert error.value.reason == "blocked"


async def test_provider_timeout_is_classified() -> None:
    async def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private diagnostics", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        provider = SearxngClient(
            "https://provider.invalid", timeout_seconds=1, user_agent="test", client=client
        )
        with pytest.raises(WebSearchUnavailable) as error:
            await provider.search("query", k=3)
        assert error.value.reason == "timeout"


@pytest.mark.parametrize(
    ("failures", "reason"),
    [
        ([["google", "timeout"], ["bing", "timeout"]], "timeout"),
        ([["google", "Suspended: timeout"]], "timeout"),
        ([["google", "CAPTCHA"], ["bing", "timeout"]], "blocked"),
        ([["google", "access denied"]], "blocked"),
        ([["google", "Suspended: too many requests"], ["bing", "CAPTCHA"]], "rate_limited"),
        ([["google", "rate limit exceeded"]], "rate_limited"),
        ([["google", "unexpected crash"]], "unavailable"),
        ([["google", "private diagnostic"]], "unavailable"),
        ([None], "unavailable"),
        ([["google"]], "unavailable"),
        ([["google", 42]], "unavailable"),
        ("malformed", "unavailable"),
        (None, "unavailable"),
        ([["google", "x" * 256 + "timeout"]], "unavailable"),
        ([["google", "unknown"]] * 64 + [["bing", "timeout"]], "unavailable"),
    ],
)
def test_empty_response_with_engine_faults_is_typed(failures: object, reason: str) -> None:
    expected = WebSearchRateLimited if reason == "rate_limited" else WebSearchUnavailable
    with pytest.raises(expected) as error:
        map_searxng_results({"results": [], "unresponsive_engines": failures}, k=3)
    if reason != "rate_limited":
        assert error.value.reason == reason
    assert "google" not in str(error.value) and "private diagnostic" not in str(error.value)


@pytest.mark.parametrize("failures", [[], [["google", "timeout"]], "malformed"])
def test_partial_results_survive_engine_failures(failures: object) -> None:
    results = map_searxng_results(
        {
            "results": [{"url": "https://example.org/rfc", "title": "RFC", "content": "evidence"}],
            "unresponsive_engines": failures,
        },
        k=3,
    )
    assert len(results) == 1 and results[0].snippet == "evidence"


def test_empty_response_with_no_engine_faults_is_honest() -> None:
    assert map_searxng_results({"results": [], "unresponsive_engines": []}, k=3) == ()
