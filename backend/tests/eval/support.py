"""Offline eval adapters — deterministic embeddings + permission-filtered search.

The faked adapters the **offline** eval injects so the harness runs CI-safe (no
key, no Postgres) while still exercising the genuine grounding/citation/permission
machinery. Two pieces:

* :class:`DeterministicEmbedder` — a hash-based, no-network embedding "gateway"
  that maps text to a stable bag-of-tokens vector. Same text → same vector, and
  texts that share tokens get higher cosine similarity, so semantic ranking is
  meaningful and reproducible without a model. (The live run uses the real
  bge-m3 gateway instead.)
* :class:`OfflineRetrieval` — a retrieval stand-in that reads the **seeded
  chunks from the real DB** and ranks them by cosine(query, chunk) + lexical
  token overlap, applying the **real** :class:`~app.retrieval.permissions.AllowSet`
  predicate (deny-by-default ownership, INV-2). It returns the same
  :class:`~app.domain.retrieval.RetrievedPassage` domain type as the production
  service, so the chat runtime cannot tell the difference — the difference from
  production is only that the ranking is pure-Python (Postgres' pgvector/full-text
  is unavailable on the offline SQLite), which is exactly what the *live* eval
  covers.

This keeps the offline eval honest about the one thing it cannot run (the real
pgvector hybrid) while genuinely exercising everything else: the permission
filter, the chunk rows, the citation persistence, and the metrics.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.principal import Principal
from app.db import models
from app.domain.llm import Embedding
from app.domain.retrieval import DocumentMatch, DocumentText, RetrievedPassage
from app.retrieval.permissions import AllowSet

_EMBED_DIM = 256
_TOKEN_RE = re.compile(r"[a-z0-9$]+")

# Common English function words carry no topical signal. They are kept in the
# embedding bag (stable hashing) but excluded from the *content* overlap signal,
# so a question that shares only stopwords with a passage retrieves nothing — the
# offline analogue of a real model recognising "this passage doesn't answer it".
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "to",
        "of",
        "for",
        "in",
        "on",
        "at",
        "by",
        "and",
        "or",
        "but",
        "if",
        "as",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "what",
        "when",
        "where",
        "who",
        "how",
        "many",
        "much",
        "do",
        "does",
        "did",
        "can",
        "could",
        "will",
        "would",
        "my",
        "your",
        "their",
        "our",
        "per",
        "with",
        "from",
        "after",
        "long",
        "company",
        "s",
    }
)

# A passage must clear this fused-score floor to be retrieved at all. Tuned so a
# topically-matching golden passage clears it while a stopword-only overlap does
# not — the deterministic stand-in for relevance gating.
_RELEVANCE_FLOOR = 0.05


def _tokenize(text: str) -> list[str]:
    """Lowercase word/number/$ tokens — the unit the embedding uses."""
    return _TOKEN_RE.findall(text.lower())


def _content_tokens(text: str) -> list[str]:
    """Topical tokens only (stopwords removed) — the unit lexical overlap uses."""
    return [t for t in _tokenize(text) if t not in _STOPWORDS]


class RetrievalLike(Protocol):
    """The retrieval surface the eval harness depends on (a subset of the service).

    Both the production :class:`~app.retrieval.RetrievalService` and the offline
    :class:`OfflineRetrieval` satisfy it, so the harness is adapter-agnostic.
    """

    async def search_text(
        self,
        *,
        principal: Principal,
        query: str,
        k: int,
        collection_ids: Sequence[UUID] | None = ...,
        document_ids: Sequence[UUID] | None = ...,
    ) -> list[RetrievedPassage]: ...

    async def search_documents(
        self, *, principal: Principal, name_or_query: str, k: int = ...
    ) -> list[DocumentMatch]: ...

    async def get_document(
        self, *, principal: Principal, document_id: UUID
    ) -> DocumentText | None: ...


class DeterministicEmbedder:
    """A reproducible, no-network embedding gateway (bag-of-tokens hashing).

    Each token is hashed into one of ``_EMBED_DIM`` buckets and accumulated, then
    the vector is L2-normalised. So two texts sharing tokens have a positive
    cosine similarity proportional to their token overlap — enough structure for
    deterministic semantic ranking in the offline eval, with zero dependencies.
    """

    async def embed(
        self,
        inputs: Sequence[str],
        *,
        cache_namespace: str | None = None,
    ) -> list[Embedding]:
        return [Embedding(vector=self.vector(t), model="deterministic-eval") for t in inputs]

    @staticmethod
    def vector(text: str) -> list[float]:
        # Hash topical tokens only, so two texts are "similar" because they share
        # *content*, not because both contain "the"/"is" — keeps the offline
        # semantic signal honest and lets the relevance floor gate non-answers.
        vec = [0.0] * _EMBED_DIM
        for token in _content_tokens(text):
            bucket = int(hashlib.sha256(token.encode("utf-8")).hexdigest(), 16) % _EMBED_DIM
            vec[bucket] += 1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            return vec
        return [v / norm for v in vec]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))


def _lexical_overlap(query: str, text: str) -> float:
    """Topical-token overlap of query vs text (stopword-free) — the lexical leg.

    Fraction of the query's *content* tokens present in the passage. A question
    that shares only function words with a passage scores 0 here, so it is gated
    out by the relevance floor (the offline analogue of "irrelevant passage").
    """
    q = set(_content_tokens(query))
    t = set(_content_tokens(text))
    if not q:
        return 0.0
    return len(q & t) / len(q)


class OfflineRetrieval:
    """Permission-filtered, deterministic retrieval over the seeded DB chunks.

    Reads the chunks the harness seeded (real rows), applies the real allow-set
    ownership predicate (INV-2), and ranks by a fused cosine + lexical score —
    a faithful offline analogue of the hybrid service. Returns the production
    :class:`RetrievedPassage` type so the chat runtime is unchanged.
    """

    def __init__(self, session: AsyncSession, *, embedder: DeterministicEmbedder) -> None:
        self._session = session
        self._embedder = embedder

    async def search_text(
        self,
        *,
        principal: Principal,
        query: str,
        k: int,
        collection_ids: Sequence[UUID] | None = None,
        document_ids: Sequence[UUID] | None = None,
    ) -> list[RetrievedPassage]:
        if not query.strip():
            return []
        allow = AllowSet.for_principal(principal)
        query_vec = self._embedder.vector(query)
        rows = await self._permitted_chunks(allow, collection_ids)
        scored: list[tuple[float, RetrievedPassage]] = []
        for chunk_id, document_id, filename, ordinal, text, char_start, char_end, embedding in rows:
            cos = _cosine(query_vec, embedding) if embedding else 0.0
            lex = _lexical_overlap(query, text)
            score = 0.5 * cos + 0.5 * lex
            # Gate out passages that do not topically match the question (the
            # offline analogue of relevance: a stopword-only overlap is dropped,
            # so an unanswerable question retrieves nothing → honest refusal).
            if score < _RELEVANCE_FLOOR:
                continue
            scored.append(
                (
                    score,
                    RetrievedPassage(
                        chunk_id=chunk_id,
                        document_id=document_id,
                        document_name=filename,
                        ord=ordinal,
                        text=text,
                        char_start=char_start,
                        char_end=char_end,
                        score=score,
                    ),
                )
            )
        scored.sort(key=lambda s: s[0], reverse=True)
        return [p for _, p in scored[: max(1, k)]]

    async def search_documents(
        self, *, principal: Principal, name_or_query: str, k: int = 10
    ) -> list[DocumentMatch]:
        if not name_or_query.strip():
            return []
        allow = AllowSet.for_principal(principal)
        stmt = (
            select(models.Document.id, models.Document.filename)
            .where(
                models.Document.tenant_id == allow.tenant_id,
                models.Document.owner_id.in_(allow.owner_ids),
                models.Document.filename.ilike(f"%{name_or_query}%"),
            )
            .order_by(models.Document.filename.asc())
            .limit(k)
        )
        rows = (await self._session.execute(stmt)).all()
        return [DocumentMatch(document_id=did, document_name=name, score=1.0) for did, name in rows]

    async def get_document(self, *, principal: Principal, document_id: UUID) -> DocumentText | None:
        allow = AllowSet.for_principal(principal)
        doc_stmt = select(models.Document.id, models.Document.filename).where(
            models.Document.id == document_id,
            models.Document.tenant_id == allow.tenant_id,
            models.Document.owner_id.in_(allow.owner_ids),
        )
        doc = (await self._session.execute(doc_stmt)).first()
        if doc is None:
            return None
        text_stmt = (
            select(models.Chunk.text)
            .where(
                models.Chunk.tenant_id == allow.tenant_id,
                models.Chunk.document_id == document_id,
            )
            .order_by(models.Chunk.ord.asc())
        )
        texts = (await self._session.execute(text_stmt)).scalars().all()
        return DocumentText(document_id=doc[0], document_name=doc[1], text="".join(texts))

    async def _permitted_chunks(
        self, allow: AllowSet, collection_ids: Sequence[UUID] | None
    ) -> list[tuple[UUID, UUID, str, int, str, int, int, list[float] | None]]:
        """Fetch the allow-set's chunks (the real INV-1/INV-2 predicate, in SQL)."""
        stmt = (
            select(
                models.Chunk.id,
                models.Chunk.document_id,
                models.Document.filename,
                models.Chunk.ord,
                models.Chunk.text,
                models.Chunk.char_start,
                models.Chunk.char_end,
                models.Chunk.embedding,
            )
            .join(models.Document, models.Chunk.document_id == models.Document.id)
            .where(
                models.Chunk.tenant_id == allow.tenant_id,
                models.Document.owner_id.in_(allow.owner_ids),
            )
        )
        if collection_ids:
            stmt = stmt.where(models.Document.collection_id.in_(list(collection_ids)))
        return list((await self._session.execute(stmt)).all())  # type: ignore[arg-type]


__all__ = [
    "DeterministicEmbedder",
    "OfflineRetrieval",
    "RetrievalLike",
]
