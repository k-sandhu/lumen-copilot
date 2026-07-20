"""The OpenSearch adapter — the single retrieval store's only client (ADR-0010).

This module owns every byte exchanged with OpenSearch (ADR-0004 boundary rule:
one named module per external system; prefer an **HTTP boundary** to a vendor
SDK — so this is plain :mod:`httpx` against the REST API, no ``opensearch-py``).
It exposes **domain types only** (:class:`IndexedChunk` in,
:class:`SearchHit` out); engine response shapes never leak upward.

Three responsibilities, all index-scoped:

* **Schema** — :meth:`OpenSearchStore.ensure_index` creates the chunk index
  (one document per chunk, the citation unit — ADR-0010 §5: ids + ``text`` for
  BM25 + ``embedding`` as a ``knn_vector`` + the char offsets citations need)
  and the hybrid **normalization search pipeline** (min-max normalized BM25 ⊕
  kNN, arithmetic-mean combined). Idempotent — safe on every boot.
* **Writes** — :meth:`upsert_chunks` / :meth:`delete_document` bulk-index and
  delete chunk docs, routed by tenant (ADR-0010 §5). Called from the ingestion
  write path (Celery), never the request path.
* **The hybrid query** — :meth:`hybrid_search` is the ONLY read, and its
  signature **requires** a :class:`~app.search.filters.SearchAllowFilter`; the
  filter is folded into BOTH legs (BM25 ``bool.filter`` and kNN ``filter``), so
  an unfiltered engine query is unrepresentable here — the same structural
  guarantee as ``retrieval/queries`` (INV-1/INV-2, spec 0004 §2.2). Only the
  ``retrieval/`` chokepoint may call it (ADR-0010 §3).

**Fail closed.** The engine is a hard dependency of retrieval (single-store,
ADR-0010): an unreachable/failed engine raises
:class:`~app.core.errors.DependencyError` (→ 503) — never an unfiltered or
partial fallback.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import httpx

from app.core.config import Settings
from app.core.errors import DependencyError
from app.core.logging import get_logger
from app.search.filters import SearchAllowFilter

log = get_logger(__name__)

# Bulk NDJSON content type (the _bulk endpoint rejects application/json).
_NDJSON = "application/x-ndjson"

# Chunks per _bulk request (#258). Bounds the request body no matter how large a
# document is — 1024-dim vectors make each chunk ~20KB of NDJSON, so an
# unbatched 40+-chunk document blew past the request timeout deterministically.
_BULK_BATCH_SIZE = 32


@dataclass(frozen=True, slots=True)
class IndexedChunk:
    """One chunk document to (re)index — the citation unit (ADR-0010 §5).

    ``embedding`` is optional: a chunk whose vector is still pending (or was
    ingested before embeddings existed) indexes WITHOUT the ``knn_vector`` field
    — it stays lexically searchable via BM25 and simply never matches the kNN
    leg, rather than being invisible or blocking the write.

    The four ACL-mirror fields (ADR-0019 §2) are projected from the owning
    document row so the engine-side mode-split filter
    (:class:`~app.search.filters.SearchAllowFilter`) can evaluate them per
    chunk; defaults are the non-enforced upload/``web`` shape.
    """

    chunk_id: UUID
    tenant_id: UUID
    document_id: UUID
    owner_id: UUID
    collection_id: UUID
    ord: int
    text: str
    embedding: tuple[float, ...] | None
    char_start: int
    char_end: int
    acl_enforced: bool = False
    acl_principals: tuple[str, ...] = ()
    acl_synced_at: datetime | None = None
    acl_scope_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One ranked hit from the hybrid query (already permission-filtered)."""

    chunk_id: UUID
    document_id: UUID
    score: float


def _acl_mapping_properties() -> dict[str, Any]:
    """The ADR-0019 §2 mirrored-ACL properties — declared in ONE place.

    Used both by :func:`_index_body` (a fresh index) and by
    :meth:`OpenSearchStore.ensure_index`'s **additive mapping update** for an
    index that already exists. Adding properties to a ``dynamic: strict``
    mapping is a legal, compatible operation (existing documents are untouched;
    they simply lack the fields until the reindex backfill re-writes them), and
    it is what lets a deployment that predates this change accept the new bulk
    writes at all — a strict index rejects an unmapped field outright.

    ``acl_enforced`` is an explicit boolean because an empty keyword array is
    indexed like a missing field and cannot discriminate "not a connector
    document" from "connector document nobody may see".
    """
    return {
        "acl_enforced": {"type": "boolean"},
        "acl_principals": {"type": "keyword"},
        "acl_synced_at": {"type": "date"},
        "acl_scope_ids": {"type": "keyword"},
    }


def _index_body(dimensions: int) -> dict[str, Any]:
    """The chunk-index settings + strict mapping (ADR-0010 §5).

    ``dynamic: strict`` makes an unmapped field an indexing error — the index is
    derived from Postgres, so drift should fail loudly, not accumulate silently.
    ``number_of_replicas: 0`` keeps a single-node local cluster green. The kNN
    field uses Lucene HNSW with cosine similarity — the same metric as the
    pgvector leg it replaces — and Lucene's engine supports **filtered** kNN,
    which the permission filter relies on.
    """
    return {
        "settings": {"index": {"knn": True, "number_of_replicas": 0}},
        "mappings": {
            "dynamic": "strict",
            "properties": {
                "chunk_id": {"type": "keyword"},
                "tenant_id": {"type": "keyword"},
                "document_id": {"type": "keyword"},
                "owner_id": {"type": "keyword"},
                "collection_id": {"type": "keyword"},
                "ord": {"type": "integer"},
                "text": {"type": "text", "analyzer": "english"},
                "embedding": {
                    "type": "knn_vector",
                    "dimension": dimensions,
                    "method": {
                        "name": "hnsw",
                        "space_type": "cosinesimil",
                        "engine": "lucene",
                    },
                },
                "char_start": {"type": "integer"},
                "char_end": {"type": "integer"},
                # Mirrored source ACL (ADR-0019 §2) — the engine half of the
                # mode-split predicate, shared verbatim with the additive
                # mapping update an already-deployed index receives.
                **_acl_mapping_properties(),
            },
        },
    }


def _pipeline_body() -> dict[str, Any]:
    """The hybrid normalization pipeline: min-max per leg, arithmetic mean across."""
    return {
        "description": (
            "Lumen hybrid retrieval (ADR-0010): min-max normalized BM25 + kNN, "
            "arithmetic-mean combined."
        ),
        "phase_results_processors": [
            {
                "normalization-processor": {
                    "normalization": {"technique": "min_max"},
                    "combination": {"technique": "arithmetic_mean"},
                }
            }
        ],
    }


def _hybrid_body(
    *,
    query_text: str,
    embedding: Sequence[float],
    allow: SearchAllowFilter,
    k: int,
    collection_ids: Sequence[UUID] | None = None,
    document_ids: Sequence[UUID] | None = None,
) -> dict[str, Any]:
    """The hybrid query body with the permission filter in BOTH legs.

    The filter appears twice by design: hybrid sub-queries score independently,
    so each leg must be individually incapable of surfacing an out-of-allow-set
    candidate (INV-1/INV-2 hold per leg, not just post-merge). The optional
    ``collection_ids`` narrowing is ANDed alongside the permission clauses in
    both legs — narrowing can only ever shrink the permitted set, never widen
    it. ``document_ids`` (pinned documents, spec 0007 #429) is the same
    narrow-only semantics one level finer: ANDed with everything else, so an id
    outside the allow-set contributes nothing.
    """
    filters: list[dict[str, Any]] = list(allow.to_engine_filter())
    if collection_ids:
        filters.append({"terms": {"collection_id": sorted(str(c) for c in collection_ids)}})
    if document_ids:
        filters.append({"terms": {"document_id": sorted(str(d) for d in document_ids)}})
    return {
        "size": k,
        "_source": ["chunk_id", "document_id"],
        "query": {
            "hybrid": {
                "queries": [
                    {
                        "bool": {
                            "must": [{"match": {"text": {"query": query_text}}}],
                            "filter": filters,
                        }
                    },
                    {
                        "knn": {
                            "embedding": {
                                "vector": list(embedding),
                                "k": k,
                                "filter": {"bool": {"filter": filters}},
                            }
                        }
                    },
                ]
            }
        },
    }


class OpenSearchStore:
    """The one OpenSearch client (ADR-0010 §3): schema, writes, the hybrid query.

    Constructed from :class:`Settings` (endpoint, index, timeout, optional basic
    auth, embedding dimensions). A caller-supplied ``client`` is honored so the
    offline tests can inject an ``httpx.MockTransport``; otherwise the store
    owns its client — call :meth:`aclose` when done.
    """

    def __init__(
        self,
        *,
        base_url: str,
        index: str,
        dimensions: int,
        timeout_seconds: float = 10.0,
        username: str = "",
        password: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._index = index
        self._pipeline = f"{index}-hybrid"
        self._dimensions = dimensions
        # Instance-level "schema is in place" latch so hot paths can call
        # ensure_index() unconditionally without paying HEAD+PUT per call.
        self._ensured = False
        auth = (username, password) if username else None
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            auth=auth,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> OpenSearchStore:
        """Build the store from the one config source (``core/config.py``)."""
        return cls(
            base_url=settings.opensearch_url,
            index=settings.opensearch_index,
            dimensions=settings.llm_embedding_dimensions,
            timeout_seconds=settings.opensearch_timeout_seconds,
            username=settings.opensearch_username,
            password=settings.opensearch_password,
        )

    async def aclose(self) -> None:
        """Release the owned HTTP client."""
        await self._client.aclose()

    # --- transport (every engine byte flows through here) --------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        content: bytes | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        ok_statuses: frozenset[int] = frozenset({200, 201}),
    ) -> dict[str, Any]:
        """Issue one engine request; anything but an expected status fails closed.

        Transport failures and non-OK statuses raise
        :class:`DependencyError` (503) — retrieval is single-store (ADR-0010),
        so there is no fallback path. The engine's error body is logged (type
        only), never surfaced to callers (one typed error model).
        """
        try:
            response = await self._client.request(
                method,
                path,
                json=json_body,
                content=content,
                params=params,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            log.warning(
                "opensearch.unreachable",
                method=method,
                path=path,
                error=type(exc).__name__,
            )
            raise DependencyError(
                "The search engine is unreachable.", code="search_unavailable"
            ) from exc
        if response.status_code not in ok_statuses:
            log.warning(
                "opensearch.error",
                method=method,
                path=path,
                status=response.status_code,
            )
            raise DependencyError("The search engine rejected the request.", code="search_error")
        payload: dict[str, Any] = response.json()
        return payload

    # --- schema ---------------------------------------------------------------

    async def ensure_index(self) -> None:
        """Create the chunk index + hybrid pipeline if absent (idempotent).

        A concurrent-create race (two workers booting) surfaces as the engine's
        ``resource_already_exists`` 400 — treated as success, matching the
        "safe on every boot" contract. Latched per instance after the first
        success so callers on a hot path may invoke it unconditionally.

        **Mapping migration (ADR-0019 §2).** Creating the index only ever
        covers a *fresh* deployment; an index that already exists keeps the
        mapping it was created with. Because the mapping is ``dynamic:
        strict``, a deployed ``lumen-chunks`` that predates the mirrored-ACL
        fields would reject every new bulk write outright. So the ACL
        properties are **always** applied as an additive ``PUT
        /{index}/_mapping`` — legal and idempotent for a strict mapping (new
        fields only; no existing field is redefined), and a no-op on the index
        this call just created. Existing chunk documents simply lack the fields
        until ``python -m app.search.reindex`` backfills them; until then the
        mode-split filter's ``acl_enforced`` term excludes them from the
        enforced branch and the Postgres hydration re-check is the backstop.
        """
        if self._ensured:
            return
        try:
            head = await self._client.request("HEAD", f"/{self._index}")
        except httpx.HTTPError as exc:
            log.warning("opensearch.unreachable", method="HEAD", error=type(exc).__name__)
            raise DependencyError(
                "The search engine is unreachable.", code="search_unavailable"
            ) from exc
        if head.status_code == 404:
            await self._request(
                "PUT",
                f"/{self._index}",
                json_body=_index_body(self._dimensions),
                # 400 = lost a concurrent-create race → the index exists; fine.
                ok_statuses=frozenset({200, 400}),
            )
        elif head.status_code != 200:
            raise DependencyError("The search engine is unreachable.", code="search_unavailable")
        # Additive, compatible mapping update — also repairs the lost-create
        # race above (that index was created by the winner, possibly older).
        await self._request(
            "PUT",
            f"/{self._index}/_mapping",
            json_body={"properties": _acl_mapping_properties()},
        )
        # PUT of a search pipeline is a full upsert — idempotent by nature.
        await self._request(
            "PUT", f"/_search/pipeline/{self._pipeline}", json_body=_pipeline_body()
        )
        self._ensured = True

    async def health(self) -> bool:
        """True iff the cluster answers its health endpoint (readiness probe)."""
        try:
            await self._request("GET", "/_cluster/health")
        except DependencyError:
            return False
        return True

    # --- writes (ingestion path; never the request path) ----------------------

    async def upsert_chunks(self, chunks: Sequence[IndexedChunk], *, refresh: bool = False) -> None:
        """Bulk-index chunk docs (id = chunk_id, routed by tenant — ADR-0010 §5).

        ``refresh=True`` makes the writes immediately searchable — for tests and
        the backfill command only; the ingestion path leaves the engine's
        refresh cadence alone. A partial bulk failure fails the call (the
        ingestion task retries as a unit; the index must never silently hold a
        subset).

        The write is issued in bounded sub-batches of ``_BULK_BATCH_SIZE``
        chunks (#258): each chunk carries a ~20KB embedding, so one request per
        *document* grows unboundedly with document size and a chunk-heavy
        document deterministically outlives the request timeout. Batches run
        sequentially in order; the first failing batch fails the whole call
        (the caller's retry re-runs the idempotent sync, converging).
        """
        for start in range(0, len(chunks), _BULK_BATCH_SIZE):
            await self._bulk_batch(chunks[start : start + _BULK_BATCH_SIZE], refresh=refresh)

    async def _bulk_batch(self, chunks: Sequence[IndexedChunk], *, refresh: bool) -> None:
        """Issue one bounded ``_bulk`` request for ``chunks`` (fail closed)."""
        if not chunks:
            return
        lines: list[str] = []
        for chunk in chunks:
            lines.append(
                json.dumps(
                    {
                        "index": {
                            "_index": self._index,
                            "_id": str(chunk.chunk_id),
                            "routing": str(chunk.tenant_id),
                        }
                    }
                )
            )
            doc: dict[str, Any] = {
                "chunk_id": str(chunk.chunk_id),
                "tenant_id": str(chunk.tenant_id),
                "document_id": str(chunk.document_id),
                "owner_id": str(chunk.owner_id),
                "collection_id": str(chunk.collection_id),
                "ord": chunk.ord,
                "text": chunk.text,
                "char_start": chunk.char_start,
                "char_end": chunk.char_end,
                # Mirrored source ACL (ADR-0019 §2): always written explicitly
                # so an enforced document's chunks can never fall back to the
                # non-enforced branch by omission. ``acl_synced_at`` is null
                # for never-synced/stale-stamped state — the freshness range
                # then never admits it (fail closed).
                "acl_enforced": chunk.acl_enforced,
                "acl_principals": sorted(chunk.acl_principals),
                "acl_synced_at": (
                    chunk.acl_synced_at.isoformat() if chunk.acl_synced_at is not None else None
                ),
                "acl_scope_ids": sorted(chunk.acl_scope_ids),
            }
            # A pending-embedding chunk indexes without the knn_vector field —
            # BM25-searchable now, kNN-matchable once re-synced with a vector.
            if chunk.embedding is not None:
                doc["embedding"] = list(chunk.embedding)
            lines.append(json.dumps(doc))
        body = ("\n".join(lines) + "\n").encode("utf-8")
        result = await self._request(
            "POST",
            "/_bulk",
            content=body,
            headers={"content-type": _NDJSON},
            params={"refresh": "true"} if refresh else None,
        )
        if result.get("errors"):
            log.warning("opensearch.bulk_partial_failure", index=self._index)
            raise DependencyError(
                "The search engine rejected part of the indexing batch.",
                code="search_index_error",
            )

    async def delete_document(
        self, *, tenant_id: UUID, document_id: UUID, refresh: bool = False
    ) -> None:
        """Delete every chunk doc of one document (tenant-scoped, routed)."""
        params: dict[str, str] = {"routing": str(tenant_id)}
        if refresh:
            params["refresh"] = "true"
        await self._request(
            "POST",
            f"/{self._index}/_delete_by_query",
            json_body={
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"tenant_id": str(tenant_id)}},
                            {"term": {"document_id": str(document_id)}},
                        ]
                    }
                }
            },
            params=params,
        )

    async def stamp_acl_stale(
        self,
        *,
        tenant_id: UUID,
        scope_ids: Sequence[str] | None = None,
        document_ids: Sequence[UUID] | None = None,
        refresh: bool = False,
    ) -> None:
        """Null ``acl_synced_at`` on matching chunk docs (ADR-0019 §3 cascade).

        The engine-side propagation of the framework's Postgres stale-stamp:
        an update-by-query (tenant-scoped, routed) sets ``acl_synced_at`` to
        null via script, so the mode-split filter's freshness range denies the
        stamped documents at the engine too — not just via the hydration
        re-check backstop. Selectors: ``scope_ids`` (terms on the persisted
        ``acl_scope_ids`` chains) and/or ``document_ids`` (the ids the
        framework's transaction already stamped). At least one selector is
        required — a bare tenant-wide stamp is unrepresentable (fail closed).
        Mirrors :meth:`delete_document`'s tenant-term + routing shape.
        """
        selectors: list[dict[str, Any]] = []
        if scope_ids:
            selectors.append({"terms": {"acl_scope_ids": sorted(scope_ids)}})
        if document_ids:
            selectors.append({"terms": {"document_id": sorted(str(d) for d in document_ids)}})
        if not selectors:
            raise ValueError("stamp_acl_stale requires scope_ids and/or document_ids")
        params: dict[str, str] = {"routing": str(tenant_id)}
        if refresh:
            params["refresh"] = "true"
        await self._request(
            "POST",
            f"/{self._index}/_update_by_query",
            json_body={
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"tenant_id": str(tenant_id)}},
                            {"bool": {"should": selectors, "minimum_should_match": 1}},
                        ]
                    }
                },
                "script": {
                    "source": "ctx._source.acl_synced_at = null",
                    "lang": "painless",
                },
            },
            params=params,
        )

    # --- the one read ----------------------------------------------------------

    async def hybrid_search(
        self,
        *,
        query_text: str,
        embedding: Sequence[float],
        allow: SearchAllowFilter,
        k: int,
        collection_ids: Sequence[UUID] | None = None,
        document_ids: Sequence[UUID] | None = None,
    ) -> list[SearchHit]:
        """Run the permission-filtered hybrid query (BM25 ⊕ kNN), best first.

        ``allow`` is required — there is no engine read without the INV-1/INV-2
        filter, and the filter is applied inside **both** legs (see
        :func:`_hybrid_body`). ``collection_ids`` optionally narrows within the
        allow-set (never widens); ``document_ids`` (pinned documents, spec 0007
        #429) narrows one level finer with the same semantics. Only the
        ``retrieval/`` chokepoint calls this (ADR-0010 §3); results are hydrated
        + re-checked against Postgres there (defense in depth).
        """
        result = await self._request(
            "POST",
            f"/{self._index}/_search",
            json_body=_hybrid_body(
                query_text=query_text,
                embedding=embedding,
                allow=allow,
                k=k,
                collection_ids=collection_ids,
                document_ids=document_ids,
            ),
            params={
                "search_pipeline": self._pipeline,
                "routing": str(allow.tenant_id),
            },
        )
        hits: list[SearchHit] = []
        for raw in result.get("hits", {}).get("hits", []):
            source = raw.get("_source", {})
            hits.append(
                SearchHit(
                    chunk_id=UUID(source["chunk_id"]),
                    document_id=UUID(source["document_id"]),
                    score=float(raw.get("_score") or 0.0),
                )
            )
        return hits


# --- app-process store (the API server's one client) -------------------------

_app_store: OpenSearchStore | None = None


def get_search_store() -> OpenSearchStore:
    """The API process's shared store (one HTTP client, created lazily).

    For the request path only (``retrieval/`` — one event loop for the process,
    so the pooled client is loop-safe). Celery tasks build a per-run store via
    :meth:`OpenSearchStore.from_settings` instead — ``run_task`` gives every task
    a fresh loop (#140) and a cached client must never straddle loops. Closed on
    app shutdown via :func:`aclose_search_store`.
    """
    global _app_store
    if _app_store is None:
        from app.core.config import get_settings

        _app_store = OpenSearchStore.from_settings(get_settings())
    return _app_store


async def aclose_search_store() -> None:
    """Release the shared store's HTTP client (app lifespan shutdown)."""
    global _app_store
    if _app_store is not None:
        await _app_store.aclose()
        _app_store = None


__all__ = [
    "IndexedChunk",
    "OpenSearchStore",
    "SearchHit",
    "aclose_search_store",
    "get_search_store",
]
