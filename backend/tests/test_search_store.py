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
        client=httpx.AsyncClient(base_url="http://opensearch.test:9200", transport=handler),
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
    """Without grants: [tenant term, mode-split bool] — deny-by-default.

    Since ADR-0019 §2 the second clause is the exclusive mode split; the
    non-enforced branch carries exactly the pre-0019 owner-should terms (the
    full split shape is pinned in ``tests/test_acl_mode_split.py``).
    """
    tenant, owner = uuid.uuid4(), uuid.uuid4()
    clauses = SearchAllowFilter(tenant_id=tenant, owner_ids=frozenset({owner})).to_engine_filter()
    assert clauses[0] == {"term": {"tenant_id": str(tenant)}}
    not_enforced, _enforced = clauses[1]["bool"]["should"]  # type: ignore[index]
    assert not_enforced["bool"]["filter"][0] == {"term": {"acl_enforced": False}}
    should = not_enforced["bool"]["filter"][1]["bool"]["should"]
    assert should == [{"terms": {"owner_id": [str(owner)]}}]
    assert clauses[1]["bool"]["minimum_should_match"] == 1  # type: ignore[index]


def test_engine_filter_includes_grant_sets_only_when_present() -> None:
    """Grant clauses appear iff their id-set is non-empty (grants only widen
    the NON-enforced branch — ADR-0019 §2 exclusive modes)."""
    tenant, owner = uuid.uuid4(), uuid.uuid4()
    doc, coll = uuid.uuid4(), uuid.uuid4()
    clauses = SearchAllowFilter(
        tenant_id=tenant,
        owner_ids=frozenset({owner}),
        granted_document_ids=frozenset({doc}),
        granted_collection_ids=frozenset({coll}),
    ).to_engine_filter()
    not_enforced, _enforced = clauses[1]["bool"]["should"]  # type: ignore[index]
    should = not_enforced["bool"]["filter"][1]["bool"]["should"]
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

    await store.hybrid_search(query_text="budget", embedding=[0.0] * _DIMS, allow=allow, k=5)

    assert captured["params"]["search_pipeline"] == "lumen-test-hybrid"
    assert captured["params"]["routing"] == str(tenant)
    legs = captured["body"]["query"]["hybrid"]["queries"]

    def _normalized(clauses: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # The enforced branch's freshness floor is wall-clock derived; pin it
        # to a placeholder so the two independently-built filters compare.
        out = json.loads(json.dumps(clauses))
        split = out[1]["bool"]["should"]
        split[1]["bool"]["filter"][2]["range"]["acl_synced_at"]["gte"] = "<floor>"
        return list(out)

    expected = _normalized(allow.to_engine_filter())
    assert _normalized(legs[0]["bool"]["filter"]) == expected  # BM25 leg
    assert (
        _normalized(legs[1]["knn"]["embedding"]["filter"]["bool"]["filter"]) == expected
    )  # kNN leg
    assert captured["body"]["size"] == 5


async def test_unreachable_engine_fails_closed() -> None:
    """Single-store: no engine → DependencyError (503), never a fallback."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    store = _store(httpx.MockTransport(handler))
    allow = SearchAllowFilter(tenant_id=uuid.uuid4(), owner_ids=frozenset({uuid.uuid4()}))
    with pytest.raises(DependencyError) as excinfo:
        await store.hybrid_search(query_text="q", embedding=[0.0] * _DIMS, allow=allow, k=3)
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


# --- schema: the ACL fields reach an ALREADY-EXISTING strict index -----------


class _StrictEngine:
    """A minimal ``dynamic: strict`` engine double (mapping-aware).

    Models the one behaviour the regression is about: the index already exists
    with a **pre-0040 mapping**, and a strict mapping rejects any document
    carrying a field it does not know. ``PUT /{index}/_mapping`` widens the
    known-field set (the additive, compatible operation ADR-0019 §2 calls for).
    """

    def __init__(self, *, exists: bool) -> None:
        self.exists = exists
        self.known_fields: set[str] = {
            "chunk_id",
            "tenant_id",
            "document_id",
            "owner_id",
            "collection_id",
            "ord",
            "text",
            "embedding",
            "char_start",
            "char_end",
        }
        self.requests: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        if request.method == "HEAD":
            return httpx.Response(200 if self.exists else 404)
        if request.method == "PUT" and path.endswith("/_mapping"):
            body = json.loads(request.content.decode("utf-8"))
            self.known_fields |= set(body["properties"])
            return httpx.Response(200, json={"acknowledged": True})
        if request.method == "PUT":  # create index / pipeline
            if path == "/lumen-test":
                body = json.loads(request.content.decode("utf-8"))
                self.known_fields |= set(body["mappings"]["properties"])
                self.exists = True
            return httpx.Response(200, json={"acknowledged": True})
        if path == "/_bulk":
            lines = request.content.decode("utf-8").strip().split("\n")
            for line in lines[1::2]:
                unknown = set(json.loads(line)) - self.known_fields
                if unknown:  # what a strict mapping does to an unmapped field
                    return httpx.Response(200, json={"errors": True, "items": []})
            return httpx.Response(200, json={"errors": False, "items": []})
        return httpx.Response(200, json={})  # pragma: no cover — unused paths


async def test_existing_strict_index_gets_the_acl_mapping_added() -> None:
    """Regression (ADR-0019 §2): an index created BEFORE the mirrored-ACL
    fields must still accept the new writes.

    ``ensure_index`` only ever *created* the index, so a deployed
    ``dynamic: strict`` index never learned ``acl_enforced`` /
    ``acl_principals`` / ``acl_synced_at`` / ``acl_scope_ids`` and rejected
    every subsequent bulk write. The additive ``PUT /{index}/_mapping`` is the
    compatible migration; the documented reindex backfills existing rows.
    """
    engine = _StrictEngine(exists=True)
    store = _store(httpx.MockTransport(engine))

    await store.ensure_index()

    assert ("PUT", "/lumen-test/_mapping") in engine.requests
    assert ("PUT", "/lumen-test") not in engine.requests  # never re-created
    assert {
        "acl_enforced",
        "acl_principals",
        "acl_synced_at",
        "acl_scope_ids",
    } <= engine.known_fields
    # The upgraded index now accepts a mirrored-ACL chunk write.
    await store.upsert_chunks([_chunk(tenant_id=uuid.uuid4(), owner_id=uuid.uuid4())])


async def test_fresh_index_creation_still_carries_the_acl_mapping() -> None:
    """A brand-new index gets the fields from the create body (mapping PUT is a no-op)."""
    engine = _StrictEngine(exists=False)
    store = _store(httpx.MockTransport(engine))

    await store.ensure_index()

    assert ("PUT", "/lumen-test") in engine.requests
    assert ("PUT", "/lumen-test/_mapping") in engine.requests
    await store.upsert_chunks([_chunk(tenant_id=uuid.uuid4(), owner_id=uuid.uuid4())])


async def test_rejected_mapping_update_fails_closed() -> None:
    """An engine that refuses the mapping update is a hard dependency failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200)
        if request.url.path.endswith("/_mapping"):
            return httpx.Response(400, json={"error": "illegal_argument_exception"})
        return httpx.Response(200, json={})  # pragma: no cover

    store = _store(httpx.MockTransport(handler))
    with pytest.raises(DependencyError) as excinfo:
        await store.ensure_index()
    assert excinfo.value.code == "search_error"


async def test_embedding_mapping_dimension_mismatch_fails_closed() -> None:
    """#346: an existing 1,024 mapping cannot accept configured 2,048 vectors."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "lumen-test": {
                    "mappings": {
                        "properties": {"embedding": {"type": "knn_vector", "dimension": 1024}}
                    }
                }
            },
        )

    store = _store(httpx.MockTransport(handler))
    with pytest.raises(DependencyError) as excinfo:
        await store.check_embedding_dimensions()
    assert excinfo.value.code == "embedding_dimension_mismatch"


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
    store = OpenSearchStore(base_url=_OS_URL, index=index, dimensions=_DIMS, timeout_seconds=15.0)
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
        await store.delete_document(tenant_id=tenant_a, document_id=own.document_id, refresh=True)
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
