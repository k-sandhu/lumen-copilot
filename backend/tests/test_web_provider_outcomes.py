"""Malformed provider output must not be reported as an honest empty search."""

import httpx
import pytest

from app.search_web.client import SearxngClient, WebSearchUnavailable, map_searxng_results


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
