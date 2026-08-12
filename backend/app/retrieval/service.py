"""The retrieval adapter — permission-filtered hybrid search + the agent tools.

This is the public surface of ``retrieval/`` (CC-1 #45). It is the permissioned-
by-default chokepoint (spec 0004 INV-2): **there is no method here that returns a
passage or document without first deriving the allow-set from the principal and
folding it into every query**. The permission filter is applied inside this
module (via :mod:`app.retrieval.queries`), keyed off the identity resolved in
``auth/`` — there is no unfiltered path (ADR-0004, mission filter #1).

Composition (the adapter wires four collaborators, none of which it *is*):

* the ``app/search/`` OpenSearch store — **the single retrieval store**
  (ADR-0010): ranked hybrid candidates (BM25 ⊕ kNN, engine-normalized) come
  from ONE engine query whose
  :class:`~app.search.filters.SearchAllowFilter` this service builds — tenant
  + owner-or-grant, the grant id-sets resolved from the ``grants`` table per
  request — so an unfiltered engine query is unrepresentable;
* the SQLAlchemy async session + :mod:`app.retrieval.queries` for **hydration**
  (every ranked hit is re-read and permission-re-checked against Postgres, the
  source of truth, before becoming a citable passage — defense in depth) and
  for the relational agent tools (``search_documents`` / ``list_documents`` /
  ``get_document``);
* the #36 ``llm/`` gateway ``embed()`` to embed the query — the only
  model caller (ADR-0004); the gateway is injected so the service is testable
  with a fake and never imports LiteLLM.

**Fail closed (single-store, ADR-0010).** An unreachable engine raises
:class:`~app.core.errors.DependencyError` (→ 503); there is no Postgres
fallback for ranked search — never an unfiltered or silently-degraded result.

It returns the domain types in :mod:`app.domain.retrieval`
(:class:`RetrievedPassage`, :class:`DocumentMatch`, :class:`DocumentText`),
never a SQL row or a pgvector value (adapter rule 1). Each carries source
provenance + char offsets so the chat runtime (#24) can cite (CC-11 / INV-3).

The **agent tools** (:meth:`search_text`, :meth:`search_documents`,
:meth:`list_documents`, :meth:`get_document`) are the callable functions the chat
runtime gives the LLM, named per the WS ``ChatToolCall`` vocabulary
(``contracts/websocket-envelopes.schema.json`` — an open-ended set). Each enforces
the **same** permission filter — a tool call as user A can never reach user B's or
another tenant's data (INV-2, asserted by the negative tests). :meth:`list_documents`
is the query-less enumeration ("what can I access"); the others are discovery by a
known handle (text/name/id).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.principal import Principal
from app.db.repositories import GrantRepository, GroupRepository
from app.domain.retrieval import DocumentMatch, DocumentText, RetrievedPassage
from app.llm import LLMGateway
from app.retrieval import queries
from app.retrieval.fusion import rrf_score
from app.retrieval.permissions import AllowSet
from app.search import OpenSearchStore, SearchAllowFilter, get_search_store

# Hard ceilings so a hostile/large ``k`` cannot turn into an unbounded scan.
# ``MAX_K`` is public so callers that paginate over ``search`` (e.g. the search
# service) know the maximum reachable rank and never advertise a page beyond it
# (#270). ``_MAX_K`` stays as the in-module alias the clamps use.
MAX_K = 100
_MAX_K = MAX_K
_MAX_DOCUMENT_HITS = 50


class RetrievalService:
    """Permission-filtered hybrid retrieval + the agent search tools.

    Constructed per-request with the async session and the #36 LLM gateway; the
    OpenSearch store defaults to the app-process client
    (:func:`app.search.get_search_store`) and is injectable so tests target a
    per-test index. Every public method takes the resolved :class:`Principal`;
    the allow-set is derived from it on each call (never cached across
    principals) and folded into every query — engine and SQL alike — so the
    permission filter is structural and unskippable (INV-2).
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        gateway: LLMGateway,
        store: OpenSearchStore | None = None,
    ) -> None:
        self._session = session
        self._gateway = gateway
        self._store = store
        # Group membership resolved once per service instance (ADR-0022 §5).
        # The service is built per request, so this never outlives the request
        # and cannot make a group removal lag — see _resolve_allow_set.
        self._allow_set_cache: dict[UUID, AllowSet] = {}

    def _search_store(self) -> OpenSearchStore:
        """The engine client — injected (tests) or the app-process default.

        Resolved lazily at call time so constructing the service for the
        relational tools never touches engine plumbing.
        """
        return self._store if self._store is not None else get_search_store()

    async def _resolve_allow_set(self, principal: Principal) -> AllowSet:
        """The requester's allow-set, with their group principals resolved (ADR-0022 §5).

        Group membership needs a read, so it cannot come from the pure
        :meth:`AllowSet.for_principal`; it is resolved here — the one place the
        retrieval chokepoint builds an allow-set — and memoized for this request
        only. A user in no groups gets an empty set, which narrows the allow-set
        to ownership plus their own user grants (fail closed).
        """
        cached = self._allow_set_cache.get(principal.user_id)
        if cached is not None:
            return cached
        group_ids = await GroupRepository(self._session, principal.tenant_id).group_ids_for_user(
            principal.user_id
        )
        allow_set = AllowSet.for_principal(principal, group_ids=group_ids)
        self._allow_set_cache[principal.user_id] = allow_set
        return allow_set

    async def _allow_filter(self, allow_set: AllowSet) -> SearchAllowFilter:
        """Build the engine-side INV-1/INV-2 filter for this request.

        The SQL grant ``EXISTS`` becomes resolved id-sets (ADR-0010 §4): the
        requester's ``user``- **and** ``group``-principal grants (ADR-0022 §5)
        are read from the tenant-scoped ``grants`` table **per request**, so a
        revoked grant — or a removal from the group that carried it — vanishes
        from the very next query; deny-by-default is preserved on the engine
        path exactly as it is in SQL.
        """
        granted_docs, granted_colls = await GrantRepository(
            self._session, allow_set.tenant_id
        ).granted_resource_ids(allow_set.grant_principal_id, group_ids=allow_set.group_ids)
        return SearchAllowFilter(
            tenant_id=allow_set.tenant_id,
            owner_ids=allow_set.owner_ids,
            granted_document_ids=granted_docs,
            granted_collection_ids=granted_colls,
            # The mirrored-principal identity (ADR-0019 §2) rides the same
            # allow-set, so the engine's acl_enforced branch and the SQL
            # predicate can never disagree about who the requester is.
            acl_principals=allow_set.acl_principals,
        )

    # --- core hybrid search -------------------------------------------------

    async def search(
        self,
        *,
        principal: Principal,
        query: str,
        k: int,
        collection_ids: list[UUID] | None = None,
        document_ids: list[UUID] | None = None,
    ) -> list[RetrievedPassage]:
        """Permission-filtered hybrid passage search (the chokepoint API, AC-1/AC-2).

        Embeds ``query`` via the #36 gateway, then runs **one**
        OpenSearch hybrid query (BM25 ⊕ kNN, score-normalized by the engine's
        search pipeline — ADR-0010) carrying the INV-1/INV-2
        :class:`SearchAllowFilter` in both legs, and hydrates the ranked hits
        into :class:`RetrievedPassage` objects from Postgres — where the
        permission predicate is applied **again** (defense in depth) — carrying
        source + char offsets for citations.

        Args:
            principal: the asking identity (from ``auth/``). The tenant +
                ownership allow-set is derived from it — never from request input.
            query: the natural-language query to ground against.
            k: the number of passages to return (clamped to a safe ceiling).
            collection_ids: optional narrowing to specific collections (ANDed
                inside the permission filter — narrowing never widens; a foreign
                collection contributes nothing).
            document_ids: optional narrowing to specific pinned documents (spec
                0007 #429) — same narrow-only semantics, ANDed with the
                allow-set and any collection filter; an id the caller cannot
                access contributes nothing and discloses nothing (INV-2).

        Returns:
            Up to ``k`` ranked passages the principal is permitted to see, best
            first. An empty query, or a principal with no matching permitted
            chunks, yields ``[]`` — never another user's passages (INV-2).

        Raises:
            DependencyError: the engine is unreachable (single-store; fails
                closed → 503, never an unfiltered fallback).
        """
        k = _clamp_k(k)
        if not query.strip():
            return []
        allow_set = await self._resolve_allow_set(principal)
        allow = await self._allow_filter(allow_set)

        # Embed the query (the only model call; the gateway returns a domain
        # Embedding — no LiteLLM type crosses the boundary).
        embeddings = await self._gateway.embed(
            [query],
            # #395: opt into the query-embedding cache, namespaced by tenant so
            # entries are never observable across principals.
            cache_namespace=str(principal.tenant_id),
        )
        query_vector = embeddings[0].vector

        store = self._search_store()
        # Latched per store instance (one HEAD on the first search per process):
        # a fresh deployment self-heals the index/pipeline instead of 404ing
        # until the first ingest creates them.
        await store.ensure_index()
        hits = await store.hybrid_search(
            query_text=query,
            embedding=query_vector,
            allow=allow,
            k=k,
            collection_ids=collection_ids,
            document_ids=document_ids,
        )
        if not hits:
            return []

        # Hydrate from Postgres — the source of truth — re-applying the SQL
        # permission predicate (defense in depth — INV-2): a hit the re-check
        # excludes (deleted/revoked between index and read) is dropped, and the
        # citation offsets come from the relational row, never the index.
        rows = await queries.load_passages(
            self._session, allow_set=allow_set, chunk_ids=[h.chunk_id for h in hits]
        )
        passages: list[RetrievedPassage] = []
        for hit in hits:  # engine ranking order preserved
            row = rows.get(hit.chunk_id)
            if row is None:
                continue
            # OpenSearch is derived and can lag a retry/re-delivery. Hydration
            # admits only the exact Ready Postgres generation and coordinate
            # space that produced the hit; stale/failed publications are never
            # citable even if their engine rows still exist (R1-002/R1-006).
            if (
                hit.ingestion_attempt != row.ingestion_attempt
                or hit.embedding_fingerprint != row.embedding_fingerprint
            ):
                continue
            passages.append(
                RetrievedPassage(
                    chunk_id=row.chunk_id,
                    document_id=row.document_id,
                    document_name=row.document_name,
                    ord=row.ord,
                    text=row.text,
                    char_start=row.char_start,
                    char_end=row.char_end,
                    score=hit.score,
                )
            )
        return passages

    # --- agent tools (the WS ChatToolCall vocabulary, AC-3) -----------------

    async def search_text(
        self,
        *,
        principal: Principal,
        query: str,
        k: int,
        collection_ids: list[UUID] | None = None,
        document_ids: list[UUID] | None = None,
    ) -> list[RetrievedPassage]:
        """Agent tool ``search_text``: hybrid passage search (delegates to :meth:`search`).

        The LLM-callable form of the core search. Identical permission guarantee
        (INV-2): a call as user A returns only user A's permitted passages, with
        source + offsets for citation. Same signature shape as the ``ChatToolCall``
        ``args`` (``query``, ``k``, optional ``collection_ids``). ``document_ids``
        is the runtime-threaded pinned-document narrowing (spec 0007 #429) — it
        rides the ``ToolContext``, never model arguments.
        """
        return await self.search(
            principal=principal,
            query=query,
            k=k,
            collection_ids=collection_ids,
            document_ids=document_ids,
        )

    async def search_documents(
        self,
        *,
        principal: Principal,
        name_or_query: str,
        k: int = 10,
    ) -> list[DocumentMatch]:
        """Agent tool ``search_documents``: find permitted documents by filename/metadata.

        Returns documents in the principal's allow-set whose filename matches
        ``name_or_query`` (case-insensitive substring; the MVP metadata signal).
        Permission-filtered (INV-1/INV-2) by the **same** owner-or-grant predicate
        as :meth:`get_document` and the engine-backed :meth:`search_text`
        (``queries._document_permitted``): a document owned by the caller **or**
        explicitly granted to them (directly or via its collection) is returned; a
        document in another tenant, or one the caller neither owns nor was granted,
        never is. The returned :class:`DocumentMatch` ids feed :meth:`get_document`.
        A blank query yields ``[]``.
        """
        if not name_or_query.strip():
            return []
        allow_set = await self._resolve_allow_set(principal)
        hits = await queries.document_search(
            self._session,
            allow_set=allow_set,
            name_or_query=name_or_query,
            k=min(_MAX_DOCUMENT_HITS, max(1, k)),
        )
        # Lexical filename match has no intrinsic relevance score; expose a
        # descending positional score so callers see a stable, ordered ranking.
        return [
            DocumentMatch(
                document_id=hit.document_id,
                document_name=hit.document_name,
                score=rrf_score(position),
            )
            for position, hit in enumerate(hits)
        ]

    async def list_documents(
        self,
        *,
        principal: Principal,
        k: int = _MAX_DOCUMENT_HITS,
    ) -> list[DocumentMatch]:
        """Agent tool ``list_documents``: enumerate the documents the principal can access.

        The enumeration sibling of :meth:`search_documents` — it takes **no query**
        and returns the documents in the principal's allow-set: owned by the caller
        **or** explicitly granted to them (directly or via a grant on the document's
        collection — the cascade). It applies the **same** owner-or-grant predicate
        (``queries._document_permitted``) as :meth:`search_documents`,
        :meth:`get_document`, and the engine-backed :meth:`search_text`, so the four
        agent tools never disagree about what the caller may see (INV-1/INV-2).

        ``k`` caps the count (clamped to ``[1, _MAX_DOCUMENT_HITS]``); the returned
        :class:`DocumentMatch` ids feed :meth:`get_document`. A principal with no
        owned/granted documents yields ``[]`` (never another user's or tenant's).
        """
        allow_set = await self._resolve_allow_set(principal)
        rows = await queries.list_documents(
            self._session,
            allow_set=allow_set,
            k=min(_MAX_DOCUMENT_HITS, max(1, k)),
        )
        # A plain listing has no intrinsic relevance score; expose a descending
        # positional score so callers see a stable, ordered ranking (as
        # ``search_documents`` does for its lexical hits).
        return [
            DocumentMatch(
                document_id=row.document_id,
                document_name=row.document_name,
                score=rrf_score(position),
            )
            for position, row in enumerate(rows)
        ]

    async def permitted_document_names(
        self, *, principal: Principal, document_ids: list[UUID]
    ) -> dict[UUID, str]:
        """The subset of ``document_ids`` the principal may read, with names.

        ONE bounded metadata query (#446 finding 4) under the identical
        owner-or-grant predicate as every other retrieval chokepoint — the
        evidence-rehydration path needs names only, never document text. Ids
        that are missing, foreign, or unpermitted are simply absent (INV-2
        existence non-disclosure).
        """
        if not document_ids:
            return {}
        allow_set = await self._resolve_allow_set(principal)
        stmt = queries.permitted_document_names(
            allow_set=allow_set, document_ids=list(document_ids)
        )
        rows = (await self._session.execute(stmt)).all()
        return {row[0]: row[1] for row in rows}

    async def valid_chunk_pairs(
        self, *, principal: Principal, chunk_ids: list[UUID]
    ) -> dict[UUID, UUID]:
        """{chunk_id: document_id} for chunks that exist in the caller's tenant.

        Pairs evidence-digest validation with :meth:`permitted_document_names`
        (#446 round-2, finding 4): the caller keeps only (doc, chunk) pairs
        whose chunk genuinely belongs to the permitted document — a corrupt row
        cannot inject arbitrary chunk ids into a prompt.
        """
        if not chunk_ids:
            return {}
        allow_set = await self._resolve_allow_set(principal)
        stmt = queries.valid_chunk_pairs(tenant_id=allow_set.tenant_id, chunk_ids=list(chunk_ids))
        rows = (await self._session.execute(stmt)).all()
        return {row[0]: row[1] for row in rows}

    async def get_document(
        self,
        *,
        principal: Principal,
        document_id: UUID,
    ) -> DocumentText | None:
        """Agent tool ``get_document``: fetch a permitted document's text, or ``None``.

        Returns the reassembled text (chunks in order) of ``document_id`` **iff**
        it is in the principal's allow-set: same tenant **and** owned by them
        **or** explicitly granted to them (directly, or via a grant on its
        collection — the cascade). This is the identical owner-or-grant predicate
        the engine-backed :meth:`search`/:meth:`search_text` and the relational
        :meth:`search_documents`/:meth:`list_documents` use
        (``queries._document_permitted``), so the agent tools never disagree about
        what the caller may read (the consistency #181 established, proven across
        all four in the live parity test). A document that is missing, in another
        tenant, or neither owned nor
        granted yields ``None`` — the runtime treats that as "not found"
        (existence non-disclosure, INV-2; never reveals a foreign document
        exists).
        """
        allow_set = await self._resolve_allow_set(principal)
        row = await queries.load_document_text(
            self._session, allow_set=allow_set, document_id=document_id
        )
        if row is None:
            return None
        return DocumentText(
            document_id=row.document_id,
            document_name=row.document_name,
            text=row.text,
        )


def _clamp_k(k: int) -> int:
    """Clamp a requested result count into ``[1, _MAX_K]`` (fail-safe, never huge)."""
    return max(1, min(_MAX_K, k))
