"""The ``web_search`` tool — discovery, governance, and fail-closed gate (ADR-0014, #219).

Offline unit tests over the auto-discovered tool:

* it is discovered by the registry (a new file in ``impls/``, no registry edit);
* its governance metadata is **T0 / read-only / no approval** AND
  ``default_offered=False`` — so it is NOT in the ad-hoc chat default allow-list
  (off by default, ADR-0014 §5), the three retrieval tools still are (regression);
* **AC-3 negative:** with web mode disabled the handler returns an ``ok=False``
  *tool result* (``web_search_disabled``) — a governance refusal, never a crash;
* enabled + a stub provider returns web results whose payload is a **web**
  citation shape (URL + snippet, ``sourceType: web``) distinct from a corpus
  document citation (no ``document_id``), and a rate-limited window / unavailable
  provider each map to a distinct ``ok=False`` result.
"""

from __future__ import annotations

import uuid

import pytest

from app.auth.principal import Principal
from app.core.config import Settings
from app.domain.entities import Role
from app.domain.tools import RiskTier
from app.domain.web_search import WebSearchResult
from app.search_web.client import WebSearchRateLimited, WebSearchUnavailable
from app.services.tools import impls as _impls_pkg  # noqa: F401 — ensure package import
from app.services.tools.impls import web_search as web_search_impl
from app.services.tools.registry import default_allowlist, get_tool, registered_names, tool_specs
from app.services.tools.types import ToolContext

_BASE_ENV = {
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
    return Settings(_env_file=None, **_BASE_ENV, **overrides)  # type: ignore[arg-type]


def _ctx() -> ToolContext:
    principal = Principal(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), roles=(Role.MEMBER,))
    return ToolContext(principal=principal, retrieval=object())  # type: ignore[arg-type]


class _StubService:
    """A stand-in web-search service the handler calls once web mode is enabled."""

    def __init__(self, *, results=(), raises: Exception | None = None) -> None:
        self._results = results
        self._raises = raises

    async def search(self, query: str, *, k=None):  # noqa: ANN001, ARG002
        if self._raises is not None:
            raise self._raises
        return self._results


def _patch_service(monkeypatch: pytest.MonkeyPatch, service: _StubService) -> None:
    """Route ``build_web_search_service`` in the impl to a stub (no Redis/HTTP)."""
    monkeypatch.setattr(
        web_search_impl, "build_web_search_service", lambda *a, **k: service  # noqa: ARG005
    )


def _patch_settings(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    monkeypatch.setattr(web_search_impl, "get_settings", lambda: _settings(**overrides))


# --- Discovery + governance metadata ----------------------------------------


def test_web_search_is_discovered() -> None:
    assert "web_search" in registered_names()


def test_web_search_is_t0_read_only_no_approval() -> None:
    defn = get_tool("web_search")
    assert defn.risk_tier is RiskTier.T0
    assert defn.read_only is True
    assert defn.requires_approval is False


def test_web_search_is_off_by_default_allowlist() -> None:
    """Governance (ADR-0014 §5): web_search is NOT in the ad-hoc default set."""
    defn = get_tool("web_search")
    assert defn.default_offered is False
    assert "web_search" not in default_allowlist()
    # The three retrieval tools remain the ad-hoc default (regression).
    assert default_allowlist() == frozenset({"search_text", "search_documents", "get_document"})


def test_web_search_schema_is_query_and_optional_k() -> None:
    schema = get_tool("web_search").json_schema
    assert schema["required"] == ["query"]
    assert set(schema["properties"]) == {"query", "k"}


def test_web_search_can_be_advertised_when_explicitly_allowed() -> None:
    """Offered only when an allow-list explicitly includes it (assistant web scope)."""
    specs = tool_specs(frozenset({"web_search"}))
    assert {s.name for s in specs} == {"web_search"}


# --- AC-3 negative: disabled tenant/deploy -> tool-result error, not a crash --


async def test_disabled_returns_tool_result_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, WEB_SEARCH_ENABLED=False)
    result = await web_search_impl._web_search({"query": "anything"}, _ctx())
    assert result.ok is False
    assert result.error == web_search_impl.ERROR_WEB_DISABLED
    # It never reached (or built) the provider — a governance refusal.


async def test_disabled_does_not_call_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, WEB_SEARCH_ENABLED=False)

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("provider must not be built when web mode is disabled")

    monkeypatch.setattr(web_search_impl, "build_web_search_service", _boom)
    result = await web_search_impl._web_search({"query": "x"}, _ctx())
    assert result.ok is False and result.error == web_search_impl.ERROR_WEB_DISABLED


# --- Enabled happy path + web-citation payload ------------------------------


async def test_enabled_returns_web_results_and_web_citation_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_settings(monkeypatch, WEB_SEARCH_ENABLED=True)
    _patch_service(
        monkeypatch,
        _StubService(
            results=(
                WebSearchResult(
                    title="Python.org",
                    url="https://www.python.org/",
                    snippet="Official home of Python.",
                    fetched_passage="Python is a programming language.",
                ),
                WebSearchResult(
                    title="Wikipedia",
                    url="https://en.wikipedia.org/wiki/Python",
                    snippet="A language.",
                ),
            )
        ),
    )
    result = await web_search_impl._web_search({"query": "python", "k": 2}, _ctx())
    assert result.ok is True
    assert result.hit_count == 2
    # The reply carries the URLs so the model can cite them.
    assert "https://www.python.org/" in result.content
    # Web-citation payload: distinct from a corpus citation (no document_id).
    payload = result.payload
    assert payload["sourceType"] == "web"
    assert payload["resultCount"] == 2
    first = payload["results"][0]
    assert first["url"] == "https://www.python.org/"
    assert first["fetched"] is True  # page was fetched through the chokepoint
    assert payload["results"][1]["fetched"] is False  # snippet only
    assert "document_id" not in first  # never implies corpus membership (INV-3)
    # A corpus tool would populate document_ids/passages; a web tool does not.
    assert result.document_ids == ()
    assert result.passages == ()


async def test_enabled_empty_results(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, WEB_SEARCH_ENABLED=True)
    _patch_service(monkeypatch, _StubService(results=()))
    result = await web_search_impl._web_search({"query": "python"}, _ctx())
    assert result.ok is True
    assert result.summary == "0 results"


async def test_enabled_missing_query(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, WEB_SEARCH_ENABLED=True)
    _patch_service(monkeypatch, _StubService(results=()))
    result = await web_search_impl._web_search({}, _ctx())
    assert result.ok is True
    assert result.summary == "no query"


async def test_rate_limited_maps_to_distinct_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, WEB_SEARCH_ENABLED=True)
    _patch_service(monkeypatch, _StubService(raises=WebSearchRateLimited("throttled")))
    result = await web_search_impl._web_search({"query": "python"}, _ctx())
    assert result.ok is False
    assert result.error == web_search_impl.ERROR_WEB_RATE_LIMITED


async def test_unavailable_maps_to_distinct_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, WEB_SEARCH_ENABLED=True)
    _patch_service(monkeypatch, _StubService(raises=WebSearchUnavailable("down")))
    result = await web_search_impl._web_search({"query": "python"}, _ctx())
    assert result.ok is False
    assert result.error == web_search_impl.ERROR_WEB_UNAVAILABLE


async def test_unexpected_error_is_opaque_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A surprise provider error becomes a generic tool_error, never a leaked type."""
    _patch_settings(monkeypatch, WEB_SEARCH_ENABLED=True)
    _patch_service(monkeypatch, _StubService(raises=RuntimeError("vendor detail")))
    result = await web_search_impl._web_search({"query": "python"}, _ctx())
    assert result.ok is False
    assert result.error == "tool_error"
    assert "vendor detail" not in result.content
