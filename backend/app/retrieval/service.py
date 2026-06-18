"""The retrieval adapter — permission-filtered hybrid search + the agent tools.

This is the public surface of ``retrieval/`` (CC-1 #45). It is the permissioned-
by-default chokepoint (spec 0004 INV-2): **there is no method here that returns a
passage or document without first deriving the allow-set from the principal and
folding it into every query**. The permission filter is applied inside this
module (via :mod:`app.retrieval.queries`), keyed off the identity resolved in
``auth/`` — there is no unfiltered path (ADR-0004, mission filter #1).

Composition (the adapter wires three collaborators, none of which it *is*):

* the SQLAlchemy async session + the search queries in
  :mod:`app.retrieval.queries` (the only pgvector/full-text issuer);
* the pure fusion/ranking in :mod:`app.retrieval.fusion` (RRF + a flagged
  cross-encoder re-rank fast-follow);
* the #36 ``llm/`` gateway ``embed()`` to embed the query (bge-m3) — the only
  model caller (ADR-0004); the gateway is injected so the service is testable
  with a fake and never imports LiteLLM.

It returns the domain types in :mod:`app.domain.retrieval`
(:class:`RetrievedPassage`, :class:`DocumentMatch`, :class:`DocumentText`),
never a SQL row or a pgvector value (adapter rule 1). Each carries source
provenance + char offsets so the chat runtime (#24) can cite (CC-11 / INV-3).

The three **agent tools** (:meth:`search_text`, :meth:`search_documents`,
:meth:`get_document`) are the callable functions the chat runtime gives the LLM,
named exactly as the WS ``ChatToolCall`` vocabulary
(``contracts/websocket-envelopes.schema.json``). Each enforces the **same**
permission filter — a tool call as user A can never reach user B's or another
tenant's data (INV-2, asserted by the negative tests).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.principal import Principal
from app.domain.retrieval import DocumentMatch, DocumentText, RetrievedPassage
from app.llm import LLMGateway
from app.retrieval import queries
from app.retrieval.fusion import reciprocal_rank_fusion, rerank, rrf_score
from app.retrieval.permissions import AllowSet

# Per-signal candidate fan-out: each of the semantic/lexical legs pulls a wider
# slate than the final ``k`` so fusion has overlap to work with before the cut.
# A small multiplier keeps the two queries cheap while giving RRF room to reorder.
_CANDIDATE_MULTIPLIER = 4
# A floor so a tiny ``k`` (e.g. 1–2) still fetches enough candidates per leg for
# fusion to be meaningful.
_MIN_CANDIDATES = 20
# Hard ceilings so a hostile/large ``k`` cannot turn into an unbounded scan.
_MAX_K = 100
_MAX_DOCUMENT_HITS = 50


def _candidate_pool(k: int) -> int:
    """How many candidates each leg fetches for a final top-``k`` (bounded)."""
    return min(_MAX_K * _CANDIDATE_MULTIPLIER, max(_MIN_CANDIDATES, k * _CANDIDATE_MULTIPLIER))


class RetrievalService:
    """Permission-filtered hybrid retrieval + the agent search tools.

    Constructed per-request with the async session and the #36 LLM gateway. Every
    public method takes the resolved :class:`Principal`; the allow-set is derived
    from it on each call (never cached across principals) and folded into every
    query, so the permission filter is structural and unskippable (INV-2).
    """

    def __init__(self, session: AsyncSession, *, gateway: LLMGateway) -> None:
        self._session = session
        self._gateway = gateway

    # --- core hybrid search -------------------------------------------------

    async def search(
        self,
        *,
        principal: Principal,
        query: str,
        k: int,
        collection_ids: list[UUID] | None = None,
    ) -> list[RetrievedPassage]:
        """Permission-filtered hybrid passage search (the chokepoint API, AC-1/AC-2).

        Embeds ``query`` via the #36 gateway (bge-m3), runs the pgvector semantic
        leg and the Postgres full-text leg over the **same** allow-set-filtered
        candidate set, fuses the two rankings with reciprocal-rank fusion, applies
        the cross-encoder re-rank (a flagged fast-follow stub today), then hydrates
        the top-``k`` fused ids into :class:`RetrievedPassage` objects carrying
        source + char offsets for citations.

        Args:
            principal: the asking identity (from ``auth/``). The tenant +
                ownership allow-set is derived from it — never from request input.
            query: the natural-language query to ground against.
            k: the number of passages to return (clamped to a safe ceiling).
            collection_ids: optional narrowing to specific collections (still
                permission-filtered — a foreign collection contributes nothing).

        Returns:
            Up to ``k`` ranked passages the principal is permitted to see, best
            first. An empty query, or a principal with no matching permitted
            chunks, yields ``[]`` — never another user's passages (INV-2).
        """
        k = _clamp_k(k)
        if not query.strip():
            return []
        allow_set = AllowSet.for_principal(principal)
        pool = _candidate_pool(k)

        # Embed the query (the only model call; the gateway returns a domain
        # Embedding — no LiteLLM type crosses the boundary).
        embeddings = await self._gateway.embed([query])
        query_vector = embeddings[0].vector

        semantic_ids = await queries.semantic_search(
            self._session,
            allow_set=allow_set,
            query_embedding=query_vector,
            k=pool,
            collection_ids=collection_ids,
        )
        lexical_ids = await queries.lexical_search(
            self._session,
            allow_set=allow_set,
            query=query,
            k=pool,
            collection_ids=collection_ids,
        )

        fused = reciprocal_rank_fusion(semantic_ids, lexical_ids)
        fused = rerank(query, fused)[:k]
        if not fused:
            return []

        rows = await queries.load_passages(self._session, allow_set=allow_set, chunk_ids=fused)
        # Restore the fused order and drop any id the (re-checked) permission
        # filter excluded during hydration (defense in depth — INV-2).
        passages: list[RetrievedPassage] = []
        for position, chunk_id in enumerate(fused):
            row = rows.get(chunk_id)
            if row is None:
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
                    score=rrf_score(position),
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
    ) -> list[RetrievedPassage]:
        """Agent tool ``search_text``: hybrid passage search (delegates to :meth:`search`).

        The LLM-callable form of the core search. Identical permission guarantee
        (INV-2): a call as user A returns only user A's permitted passages, with
        source + offsets for citation. Same signature shape as the ``ChatToolCall``
        ``args`` (``query``, ``k``, optional ``collection_ids``).
        """
        return await self.search(
            principal=principal, query=query, k=k, collection_ids=collection_ids
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
        Permission-filtered (INV-1/INV-2): never another user's or tenant's
        documents. The returned :class:`DocumentMatch` ids feed :meth:`get_document`.
        A blank query yields ``[]``.
        """
        if not name_or_query.strip():
            return []
        allow_set = AllowSet.for_principal(principal)
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

    async def get_document(
        self,
        *,
        principal: Principal,
        document_id: UUID,
    ) -> DocumentText | None:
        """Agent tool ``get_document``: fetch a permitted document's text, or ``None``.

        Returns the reassembled text (chunks in order) of ``document_id`` **iff**
        it is in the principal's allow-set (same tenant + owned by them). A
        document that is missing, in another tenant, or owned by another user
        yields ``None`` — the runtime treats that as "not found" (existence
        non-disclosure, INV-2; never reveals that a foreign document exists).
        """
        allow_set = AllowSet.for_principal(principal)
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
