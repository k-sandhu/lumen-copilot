"""Isolated-stack live acceptance for the #346 embedding contract (R1-012).

This is deliberately not an offline mock. With the explicit opt-in set it
drives the HTTP API, Celery worker, PostgreSQL, MinIO, OpenSearch, public web
connector, embedding provider, and a cheap OpenRouter chat model end to end.
The caller must point it at a disposable compose project and seed the three
declared users there; the normal suite collects-but-skips it safely.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass

import httpx
import pytest

from tests.test_ingestion_parsers import _make_docx, _make_pdf, _make_pptx, _make_xlsx

_RUN = os.environ.get("RUN_EMBEDDING_CONTRACT_LIVE", "").strip().lower() in {
    "1",
    "true",
    "yes",
}
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not _RUN,
        reason="set RUN_EMBEDDING_CONTRACT_LIVE=1 against an isolated stack",
    ),
]

_API = os.environ.get("EMBEDDING_ACCEPTANCE_API", "http://localhost:49381").rstrip("/")
_PASSWORD = os.environ.get("EMBEDDING_ACCEPTANCE_PASSWORD", "r2-acceptance-password")
_OWNER_EMAIL = os.environ.get("EMBEDDING_ACCEPTANCE_OWNER", "r2-owner@lumen.test")
_PEER_EMAIL = os.environ.get("EMBEDDING_ACCEPTANCE_PEER", "r2-peer@lumen.test")
_FOREIGN_EMAIL = os.environ.get("EMBEDDING_ACCEPTANCE_FOREIGN", "r2-foreign@lumen.test")
_PUBLIC_URL = os.environ.get(
    "EMBEDDING_ACCEPTANCE_PUBLIC_URL",
    "https://www.rfc-editor.org/rfc/rfc2606.txt",
)
_TERMINAL_TIMEOUT = 15 * 60.0


@dataclass(frozen=True, slots=True)
class _Fixture:
    suffix: str
    mime_type: str
    marker: str
    data: bytes


def _fixtures(run_id: str) -> tuple[_Fixture, ...]:
    definitions = (
        ("txt", "text/plain", "plain", lambda text: text.encode()),
        ("md", "text/markdown", "markdown", lambda text: f"# Acceptance\n\n{text}".encode()),
        ("pdf", "application/pdf", "pdf", _make_pdf),
        (
            "docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "docx",
            _make_docx,
        ),
        (
            "pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "pptx",
            _make_pptx,
        ),
        (
            "xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "xlsx",
            _make_xlsx,
        ),
    )
    fixtures: list[_Fixture] = []
    for suffix, mime_type, label, builder in definitions:
        marker = f"lumen{label}{run_id}"
        text = f"Lumen R2 {label} acceptance marker {marker}. Verification value {label}-346."
        fixtures.append(_Fixture(suffix, mime_type, marker, builder(text)))
    return tuple(fixtures)


def _client(email: str) -> httpx.Client:
    client = httpx.Client(base_url=_API, timeout=httpx.Timeout(60.0, read=180.0))
    response = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": _PASSWORD},
    )
    response.raise_for_status()
    client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
    return client


def _wait_document(client: httpx.Client, document_id: str) -> dict[str, object]:
    deadline = time.monotonic() + _TERMINAL_TIMEOUT
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/documents/{document_id}")
        response.raise_for_status()
        latest = response.json()
        if latest.get("status") in {"ready", "failed"}:
            return latest
        time.sleep(2.0)
    pytest.fail(f"document {document_id} did not become terminal; latest={latest}")


def _list_document_ids(client: httpx.Client) -> set[str]:
    response = client.get("/api/v1/documents", params={"limit": "100"})
    response.raise_for_status()
    return {str(item["id"]) for item in response.json().get("items", [])}


def _wait_source(client: httpx.Client, source_id: str) -> dict[str, object]:
    deadline = time.monotonic() + _TERMINAL_TIMEOUT
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        response = client.get("/api/v1/sources", params={"limit": "100"})
        response.raise_for_status()
        matches = [
            item for item in response.json().get("items", []) if str(item["id"]) == source_id
        ]
        assert len(matches) == 1
        latest = matches[0]
        if latest.get("status") in {"ready", "error"}:
            return latest
        time.sleep(2.0)
    pytest.fail(f"source {source_id} did not become terminal; latest={latest}")


def _wait_search(
    client: httpx.Client,
    *,
    query: str,
    document_id: str,
    collection_id: str | None = None,
) -> dict[str, object]:
    deadline = time.monotonic() + 90.0
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        params = {"q": query, "limit": "10"}
        if collection_id is not None:
            params["collection_id"] = collection_id
        response = client.get("/api/v1/search", params=params)
        response.raise_for_status()
        latest = response.json()
        if any(str(item.get("document_id")) == document_id for item in latest.get("results", [])):
            return latest
        time.sleep(2.0)
    pytest.fail(f"search never returned document {document_id}; latest={latest}")


def _assert_denied(client: httpx.Client, *, document_id: str, marker: str) -> None:
    direct = client.get(f"/api/v1/documents/{document_id}")
    assert direct.status_code == 404
    search = client.get("/api/v1/search", params={"q": marker, "limit": "10"})
    search.raise_for_status()
    payload = search.json()
    assert all(str(item.get("document_id")) != document_id for item in payload["results"])


def _assert_cited_answer(payload: dict[str, object]) -> None:
    """Every accepted path must synthesize from one returned, citable passage."""

    cited = payload.get("direct_answer")
    assert isinstance(cited, dict) and str(cited.get("text", "")).strip()
    results = payload.get("results")
    assert isinstance(results, list)
    result_ids = {str(item["id"]) for item in results if isinstance(item, dict)}
    citations = cited.get("citations", [])
    assert isinstance(citations, list) and citations
    assert {
        str(item["result_id"]) for item in citations if isinstance(item, dict)
    } <= result_ids
    assert all(
        isinstance(item, dict) and str(item.get("snippet", "")).strip()
        for item in citations
    )


def test_isolated_six_formats_public_source_retrieval_citation_and_tenant_denial() -> None:
    """R1-012: the live, non-skipped all-boundary acceptance and complete cleanup."""

    run_id = uuid.uuid4().hex[:10]
    owner = _client(_OWNER_EMAIL)
    peer = _client(_PEER_EMAIL)
    foreign = _client(_FOREIGN_EMAIL)
    collection_id: str | None = None
    source_id: str | None = None
    uploaded_ids: list[str] = []
    try:
        readiness = owner.get("/health/ready")
        readiness.raise_for_status()
        assert readiness.json()["status"] == "ready"

        created = owner.post("/api/v1/collections", json={"name": f"R2 formats {run_id}"})
        created.raise_for_status()
        collection_id = str(created.json()["id"])

        fixtures = _fixtures(run_id)
        for fixture in fixtures:
            response = owner.post(
                "/api/v1/documents",
                data={"collection_id": collection_id},
                files={
                    "file": (
                        f"acceptance-{run_id}.{fixture.suffix}",
                        fixture.data,
                        fixture.mime_type,
                    )
                },
            )
            assert response.status_code == 201, response.text
            uploaded_ids.append(str(response.json()["id"]))

        for document_id in uploaded_ids:
            terminal = _wait_document(owner, document_id)
            assert terminal["status"] == "ready", terminal
            assert int(terminal["chunk_count"]) > 0

        search_payloads = [
            _wait_search(
                owner,
                query=fixture.marker,
                document_id=document_id,
                collection_id=collection_id,
            )
            for fixture, document_id in zip(fixtures, uploaded_ids, strict=True)
        ]

        # The real cheap model must synthesize/cite EVERY format, not merely
        # prove retrieval for five formats and exercise generation on one.
        for payload in search_payloads:
            _assert_cited_answer(payload)

        # Same-tenant non-owner (INV-2) and foreign tenant (INV-1) both receive
        # existence-nondisclosing 404 and no search passage for the same marker.
        _assert_denied(peer, document_id=uploaded_ids[0], marker=fixtures[0].marker)
        _assert_denied(foreign, document_id=uploaded_ids[0], marker=fixtures[0].marker)

        before_source = _list_document_ids(owner)
        source = owner.post("/api/v1/sources", json={"type": "web", "url": _PUBLIC_URL})
        assert source.status_code == 201, source.text
        source_id = str(source.json()["id"])
        terminal_source = _wait_source(owner, source_id)
        assert terminal_source["status"] == "ready", terminal_source
        assert int(terminal_source["indexed_count"]) > 0
        after_source = _list_document_ids(owner)
        source_documents = after_source - before_source
        assert source_documents
        public_document_id = sorted(source_documents)[0]
        public_search = _wait_search(
            owner,
            query="reserved top level dns names",
            document_id=public_document_id,
        )
        _assert_cited_answer(public_search)
    finally:
        if source_id is not None:
            response = owner.delete(f"/api/v1/sources/{source_id}")
            assert response.status_code in {204, 404}
        for document_id in uploaded_ids:
            response = owner.delete(f"/api/v1/documents/{document_id}")
            assert response.status_code in {204, 404}
        if collection_id is not None:
            response = owner.delete(f"/api/v1/collections/{collection_id}")
            assert response.status_code in {204, 404}
        owner.close()
        peer.close()
        foreign.close()
