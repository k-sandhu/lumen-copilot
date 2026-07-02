"""``app/search/`` adapter tests — the OpenSearch seam (ADR-0010, slice 1 / #190).

Two layers, honouring the offline-safe pattern of the retrieval tests:

* **Offline (httpx.MockTransport)** — no engine, no network. Proves the
  *structural* guarantees the seam exists for: a :class:`SearchAllowFilter`
  cannot be built without a tenant or with an empty owner set; the hybrid query
  carries the permission filter in **both** legs (BM25 ``bool.filter`` AND the
  kNN ``filter``) plus tenant routing; bulk writes are NDJSON with per-chunk
  tenant routing; an unreachable engine or a partial bulk failure fails
  **closed** as a typed :class:`DependencyError` (503) — never a fallback.
* **Live (compose OpenSearch)** — a real round-trip against the base-stack
  engine: create the index + pipeline (idempotently), bulk-index chunks across
  two tenants/owners, and prove the hybrid query is permission-trimmed end to
  end — INV-1 (other tenant excluded), INV-2 (other owner excluded; a granted
  document admitted), and delete. Skips automatically when the engine is
  unreachable (offline-safe).
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest

from app.core.errors import DependencyError
from app.search import IndexedChunk, OpenSearchStore, SearchAllowFilter

_DIMS = 8


def _chunk(
    *,
    tenant_id: uuid.UUID,
    owner_id: uuid.UUID,
    document_id: uuid.UUID | None = None,
    collection_id: uuid.UUID | None = None,
    text: str = "annual revenue growth strategy",
    hot: int = 0,
) -> IndexedChunk:
    embedding = [0.0] * _DIMS
    embedding[hot % _DIMS] = 1.0
    return IndexedChunk(
        chunk_id=uuid.uuid4(),
        tenant_id=tenant_id,
        document_id=document_id or uuid.uuid4(),
        owner_id=owner_id,
        collection_id=collection_id or uuid.uuid4(),
        ord=0,
        text=text,
        embedding=tuple(embedding),
        char_start=0,
        char_end=len(text),
    )


def _store(handler: httpx.MockTransport) -> OpenSearchStore:
    return OpenSearchStore(
        base_url="http://opensearch.test:9200",
        index="lumen-test",
        dimensions=_DIMS,
        client=httpx.AsyncClient(
            base_url="http://opensearch.test:9200", transport=handler
        ),
    )


# --- SearchAllowFilter: an unfiltered query is unrepresentable ---------------


def test_allow_filter_requires_tenant() -> None:
    """INV-1 structurally: the tenant is a required constructor argument."""
    with pytest.raises(TypeError):
        SearchAllowFilter(owner_ids=frozenset({uuid.uuid4()}))  # type: ignore[call-arg]


def test_allow_filter_rejects_empty_owner_set() -> None:
    """No empty/wildcard owner set — mirrors retrieval's AllowSet invariant."""
    with pytest.raises(ValueError):
        SearchAllowFilter(tenant_id=uuid.uuid4(), owner_ids=frozenset())


def test_engine_filter_shape_owner_only() -> None:
    """Without grants: exactly [tenant term, owner-should] — deny-by-default."""
    tenant, owner = uuid.uuid4(), uuid.uuid4()
    clauses = SearchAllowFilter(
        tenant_id=tenant, owner_ids=frozenset({owner})
    ).to_engine_filter()
    assert clauses[0] == {"term": {"tenant_id": str(tenant)}}
    should = clauses[1]["bool"]["should"]  # type: ignore[index]
    assert should == [{"terms": {"owner_id": [str(owner)]}}]
    assert clauses[1]["bool"]["minimum_should_match"] == 1  # type: ignore[index]


def test_engine_filter_includes_grant_sets_only_when_present() -> None:
    """Grant clauses appear iff their id-set is non-empty (grants only widen)."""
    tenant, owner = uuid.uuid4(), uuid.uuid4()
    doc, coll = uuid.uuid4(), uuid.uuid4()
    clauses = SearchAllowFilter(
        tenant_id=tenant,
        owner_ids=frozenset({owner}),
        granted_document_ids=frozenset({doc}),
        granted_collection_ids=frozenset({coll}),
    ).to_engine_filter()
    should = clauses[1]["bool"]["should"]  # type: ignore[index]
    assert {"terms": {"document_id": [str(doc)]}} in should
    assert {"terms": {"collection_id": [str(coll)]}} in should


# --- hybrid query: the filter is in BOTH legs + tenant routing ---------------


async def test_hybrid_query_carries_permission_filter_in_both_legs() -> None:
    """The INV-1/INV-2 filter appears in the BM25 leg AND inside the kNN leg."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"hits": {"hits": []}})

    store = _store(httpx.MockTransport(handler))
    tenant, owner = uuid.uuid4(), uuid.uuid4()
    allow = SearchAllowFilter(tenant_id=tenant, owner_ids=frozenset({owner}))

    await store.hybrid_search(
        query_text="budget", embedding=[0.0] * _DIMS, allow=allow, k=5
    )

    assert captured["params"]["search_pipeline"] == "lumen-test-hybrid"
    assert captured["params"]["routing"] == str(tenant)
    legs = captured["body"]["query"]["hybrid"]["queries"]
    expected = allow.to_engine_filter()
    assert legs[0]["bool"]["filter"] == expected  # BM25 leg
    assert legs[1]["knn"]["embedding"]["filter"]["bool"]["filter"] == expected  # kNN leg
    assert captured["body"]["size"] == 5


async def test_unreachable_engine_fails_closed() -> None:
    """Single-store: no engine → DependencyError (503), never a fallback."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    store = _store(httpx.MockTransport(handler))
    allow = SearchAllowFilter(tenant_id=uuid.uuid4(), owner_ids=frozenset({uuid.uuid4()}))
    with pytest.raises(DependencyError) as excinfo:
        await store.hybrid_search(
            query_text="q", embedding=[0.0] * _DIMS, allow=allow, k=3
        )
    assert excinfo.value.code == "search_unavailable"


# --- writes: NDJSON bulk with tenant routing; partial failure fails ----------


async def test_upsert_bulk_is_ndjson_with_tenant_routing() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["content_type"] = request.headers["content-type"]
        captured["lines"] = request.content.decode("utf-8").strip().split("\n")
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"errors": False, "items": []})

    store = _store(httpx.MockTransport(handler))
    chunk = _chunk(tenant_id=uuid.uuid4(), owner_id=uuid.uuid4())

    await store.upsert_chunks([chunk], refresh=True)

    assert captured["content_type"] == "application/x-ndjson"
    assert captured["params"] == {"refresh": "true"}
    action = json.loads(captured["lines"][0])["index"]
    assert action["_id"] == str(chunk.chunk_id)
    assert action["routing"] == str(chunk.tenant_id)
    doc = json.loads(captured["lines"][1])
    assert doc["tenant_id"] == str(chunk.tenant_id)
    assert doc["owner_id"] == str(chunk.owner_id)
    assert doc["char_start"] == 0 and doc["char_end"] == len(chunk.text)


async def test_bulk_partial_failure_raises() -> None:
    """A partial bulk failure fails the whole call — never a silent subset."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": True, "items": []})

    store = _store(httpx.MockTransport(handler))
    with pytest.raises(DependencyError) as excinfo:
        await store.upsert_chunks([_chunk(tenant_id=uuid.uuid4(), owner_id=uuid.uuid4())])
    assert excinfo.value.code == "search_index_error"


async def test_upsert_empty_is_noop() -> None:
    """No chunks → no request (the handler would fail the test if called)."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request expected for an empty batch")

    store = _store(httpx.MockTransport(handler))
    await store.upsert_chunks([])


async def test_upsert_batches_large_chunk_sets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression #258: a chunk-heavy document is written in bounded sub-bulks.

    One request per document grows unboundedly (each chunk carries a ~20KB
    embedding) and deterministically outlived the request timeout on a
    42-chunk document. The store must cap actions per ``_bulk`` request,
    preserve order across batches, and keep every batch individually
    fail-closed.
    """
    import app.search.store as store_module

    monkeypatch.setattr(store_module, "_BULK_BATCH_SIZE", 4)
    requests: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        lines = request.content.decode("utf-8").strip().split("\n")
        # Every other NDJSON line is an action header; cap = 4 actions/batch.
        requests.append([json.loads(line)["index"]["_id"] for line in lines[0::2]])
        return httpx.Response(200, json={"errors": False, "items": []})

    store = _store(httpx.MockTransport(handler))
    tenant, owner = uuid.uuid4(), uuid.uuid4()
    chunks = [_chunk(tenant_id=tenant, owner_id=owner) for _ in range(10)]

    await store.upsert_chunks(chunks)

    assert [len(batch) for batch in requests] == [4, 4, 2]
    flattened = [cid for batch in requests for cid in batch]
    assert flattened == [str(c.chunk_id) for c in chunks]  # order preserved


async def test_upsert_failing_later_batch_fails_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression #258: batching must not soften fail-closed — batch 2 fails ⇒ call fails."""
    import app.search.store as store_module

    monkeypatch.setattr(store_module, "_BULK_BATCH_SIZE", 4)
    seen = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["count"] += 1
        if seen["count"] == 2:
            return httpx.Response(200, json={"errors": True, "items": []})
        return httpx.Response(200, json={"errors": False, "items": []})

    store = _store(httpx.MockTransport(handler))
    tenant, owner = uuid.uuid4(), uuid.uuid4()
    chunks = [_chunk(tenant_id=tenant, owner_id=owner) for _ in range(10)]

    with pytest.raises(DependencyError) as excinfo:
        await store.upsert_chunks(chunks)
    assert excinfo.value.code == "search_index_error"
    assert seen["count"] == 2  # stopped at the failing batch, no batch 3


# --- Live round-trip against the base-stack engine (skips when offline) ------

_OS_URL = os.environ.get("OPENSEARCH_URL", "http://localhost:47186")


def _os_reachable(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 9200
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


_live = pytest.mark.skipif(
    not _os_reachable(_OS_URL),
    reason=(
        f"OpenSearch not reachable at {_OS_URL}; live search-store test skipped "
        "(offline-safe). The real engine round-trip runs only here."
    ),
)


@_live
async def test_live_round_trip_hybrid_is_permission_filtered() -> None:
    """End-to-end on the real engine: index → hybrid query → INV-1/INV-2 → delete.

    Three matching chunks: the caller's own (tenant A / owner U1), a same-tenant
    other owner's (U2), and another tenant's (B). The owner-only query returns
    exactly the caller's chunk (other owner INV-2-excluded, other tenant
    INV-1-excluded); widening the filter with a document grant admits the
    granted doc; deleting the caller's document removes it. Runs against a
    per-test index so it never collides with app data; the index + pipeline are
    dropped in teardown.
    """
    index = f"lumen-test-{uuid.uuid4().hex[:8]}"
    store = OpenSearchStore(
        base_url=_OS_URL, index=index, dimensions=_DIMS, timeout_seconds=15.0
    )
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    u1, u2, u3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    own = _chunk(tenant_id=tenant_a, owner_id=u1, hot=1)
    foreign_owner = _chunk(tenant_id=tenant_a, owner_id=u2, hot=1)
    foreign_tenant = _chunk(tenant_id=tenant_b, owner_id=u3, hot=1)
    embedding = [0.0] * _DIMS
    embedding[1] = 1.0
    try:
        await store.ensure_index()
        await store.ensure_index()  # idempotent on a live engine too
        await store.upsert_chunks([own, foreign_owner, foreign_tenant], refresh=True)

        allow_u1 = SearchAllowFilter(tenant_id=tenant_a, owner_ids=frozenset({u1}))
        hits = await store.hybrid_search(
            query_text="annual revenue growth strategy",
            embedding=embedding,
            allow=allow_u1,
            k=10,
        )
        assert [h.chunk_id for h in hits] == [own.chunk_id]  # INV-1 + INV-2
        assert hits[0].document_id == own.document_id
        assert hits[0].score > 0

        # An explicit document grant widens the same query to the granted doc.
        allow_granted = SearchAllowFilter(
            tenant_id=tenant_a,
            owner_ids=frozenset({u1}),
            granted_document_ids=frozenset({foreign_owner.document_id}),
        )
        granted_hits = await store.hybrid_search(
            query_text="annual revenue growth strategy",
            embedding=embedding,
            allow=allow_granted,
            k=10,
        )
        assert {h.chunk_id for h in granted_hits} == {own.chunk_id, foreign_owner.chunk_id}
        # The other tenant's chunk is NEVER admitted, grant or not (INV-1).
        assert foreign_tenant.chunk_id not in {h.chunk_id for h in granted_hits}

        # Deleting the caller's document removes its chunks from the index.
        await store.delete_document(
            tenant_id=tenant_a, document_id=own.document_id, refresh=True
        )
        after_delete = await store.hybrid_search(
            query_text="annual revenue growth strategy",
            embedding=embedding,
            allow=allow_u1,
            k=10,
        )
        assert after_delete == []
    finally:
        async with httpx.AsyncClient(base_url=_OS_URL, timeout=15.0) as cleanup:
            await cleanup.delete(f"/{index}")
            await cleanup.delete(f"/_search/pipeline/{index}-hybrid")
        await store.aclose()
