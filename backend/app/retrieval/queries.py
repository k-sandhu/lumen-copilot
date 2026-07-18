"""The vector + full-text search queries — the only issuer of either (ADR-0004).

This module is the single place in the whole backend that emits a ``pgvector``
ANN query or a Postgres full-text (``to_tsvector``/``plainto_tsquery``) query
(ADR-0004 boundary table: "Vector + lexical retrieval → ``retrieval/``"). It uses
the SQLAlchemy async session and the ``db/`` ORM models for those reads — the
documented ADR-0004 nuance that ``retrieval/`` owns the *search* queries while
general relational CRUD stays in ``db/`` repositories.

**Every** function here takes an :class:`~app.retrieval.permissions.AllowSet` and
folds it into the ``WHERE`` clause: ``tenant_id = :tenant`` (INV-1) **AND**
(``documents.owner_id IN :owners`` **OR** an explicit grant to the requester
covers the document — INV-2, deny-by-default ownership *or* grant). The grant leg
(spec 0004 §2.2, issue #18) is an ``EXISTS`` over the ``grants`` table correlated
to the document: a row whose ``resource`` is **this document** *or* **this
document's collection** (a collection grant **cascades** to its documents),
granted to the requester's ``user`` principal within the same tenant. There is no
function that omits the predicate — the permission filter is structural, not a
caller's responsibility, which is what makes the single chokepoint provable. A
revoked grant (its row deleted) makes the ``EXISTS`` false again, so the document
is excluded once more; deny-by-default is preserved (absence of ownership AND
grant = excluded). Citations inherit the guarantee because they are drawn only
from these retrieved passages.

The functions return **chunk-id rankings** (and small metadata rows), not domain
objects: ranking/fusion is pure and happens in :mod:`app.retrieval.fusion`; the
service hydrates the fused ids into :class:`~app.domain.retrieval.RetrievedPassage`
objects. Keeping the SQL output minimal keeps this module's surface auditable.

These queries require Postgres (pgvector + full-text): they are exercised against
the live compose database (the same offline-skip pattern as the #44 migration
test), not the in-memory SQLite used for the pure unit tests.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, TypeVar
from uuid import UUID

from sqlalchemy import ColumnElement, Select, String, and_, cast, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models
from app.retrieval.permissions import AllowSet
from app.search.filters import acl_freshness_floor

# Preserve the row type of the SELECT through the permission-filter helper so the
# typed columns survive (mypy --strict): a chunk-id select stays a chunk-id select.
_RowT = TypeVar("_RowT", bound=tuple[Any, ...])

# ---------------------------------------------------------------------------
# Plain row carriers — the minimal SQL output the service hydrates into domain
# types. Kept here (not in domain/) because they are this module's wire shape,
# not a domain concept; the service maps them to app.domain.retrieval types.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PassageRow:
    """A hydrated chunk row (the columns a :class:`RetrievedPassage` needs)."""

    chunk_id: UUID
    document_id: UUID
    document_name: str
    ord: int
    text: str
    char_start: int
    char_end: int


@dataclass(frozen=True, slots=True)
class DocumentRow:
    """A document-level match row (the columns a :class:`DocumentMatch` needs)."""

    document_id: UUID
    document_name: str


@dataclass(frozen=True, slots=True)
class DocumentTextRow:
    """A document's reassembled text (the columns a :class:`DocumentText` needs)."""

    document_id: UUID
    document_name: str
    text: str


# ---------------------------------------------------------------------------
# Permission-filtered query builders.
# ---------------------------------------------------------------------------


def _grant_exists(allow_set: AllowSet) -> ColumnElement[bool]:
    """An ``EXISTS`` over ``grants`` admitting a document the requester was granted.

    The grant half of the permission predicate (spec 0004 §2.2, issue #18),
    correlated to ``documents`` (the outer query must have ``documents`` in scope).
    True when, **within the requester's tenant**, there is a grant to the
    requester's ``user`` principal on either:

    * **this document** (``resource_type='document'`` AND ``resource_id =
      documents.id``), or
    * **this document's collection** (``resource_type='collection'`` AND
      ``resource_id = documents.collection_id``) — a collection grant **cascades**
      to its documents.

    Tenant-scoped on the grant row too, so a grant minted in another tenant can
    never widen this filter (a cross-tenant grant is invisible). Deletion of the
    grant row (revoke) makes this ``EXISTS`` false again — the document is excluded
    once more, deny-by-default.
    """
    return (
        select(models.Grant.id)
        .where(
            models.Grant.tenant_id == allow_set.tenant_id,
            models.Grant.principal_type == "user",
            models.Grant.principal_id == allow_set.grant_principal_id,
            or_(
                and_(
                    models.Grant.resource_type == "document",
                    models.Grant.resource_id == models.Document.id,
                ),
                and_(
                    models.Grant.resource_type == "collection",
                    models.Grant.resource_id == models.Document.collection_id,
                ),
            ),
        )
        .exists()
    )


def _acl_principal_overlap(allow_set: AllowSet) -> ColumnElement[bool]:
    """``acl_principals ∩ requester-principals ≠ ∅`` — portably (ADR-0019 §2).

    ``acl_principals`` is a JSON array of principal strings (``user:<uuid>`` /
    ``tenant``) written exclusively by the fail-closed ``map_acl`` normalizer.
    The overlap is expressed as an OR of quoted-substring matches against the
    column's JSON text — portable across Postgres (JSONB) and the offline
    SQLite (TEXT-backed JSON): a JSON string element always renders as
    ``"<value>"`` in both dialects' text form, the vocabulary contains no
    quote/escape characters, and the requester set is tiny (their own ``user:``
    principal + ``tenant``). ``autoescape`` guards the LIKE metacharacters.
    An empty requester set — or a NULL column — matches nothing (fail closed).
    """
    principals = sorted(allow_set.acl_principals)
    if not principals:
        return false()
    rendered = cast(models.Document.acl_principals, String)
    return or_(*[rendered.contains(f'"{p}"', autoescape=True) for p in principals])


def _document_permitted(allow_set: AllowSet) -> ColumnElement[bool]:
    """The document-level **mode-split** permission predicate (INV-2, deny-by-default).

    The reusable boolean every chokepoint ANDs after the ``tenant_id``
    predicate — since ADR-0019 §2 it is split on the exclusive enforcement
    mode (spec 0004 §2.2), and this function plus the engine mirror
    (``search/filters.SearchAllowFilter.to_engine_filter``) are the ONLY two
    places the rule lives:

    * ``acl_enforced = false`` (uploads, ``web``): today's predicate unchanged
      — owned by someone in the allow-set OR an explicit grant covers it
      (directly or via its collection);
    * ``acl_enforced = true`` (managed-connector documents): a **fresh
      mirrored-principal intersection, and nothing else** — the owner and
      grant legs do NOT apply; an empty or stale mirror (``acl_synced_at``
      NULL, or older than ``CONNECTOR_ACL_MAX_AGE_HOURS``) admits no one,
      including the owner.

    Absence of both = excluded. Assumes ``documents`` is in the query's scope.
    """
    return or_(
        and_(
            models.Document.acl_enforced.is_(False),
            or_(
                models.Document.owner_id.in_(allow_set.owner_ids),
                _grant_exists(allow_set),
            ),
        ),
        and_(
            models.Document.acl_enforced.is_(True),
            _acl_principal_overlap(allow_set),
            # NULL acl_synced_at never satisfies >= (stale-stamp ⇒ deny now).
            models.Document.acl_synced_at >= acl_freshness_floor(),
        ),
    )


def permitted_document_names(
    *, allow_set: AllowSet, document_ids: list[UUID]
) -> Select[tuple[UUID, str]]:
    """A bounded batch of permitted ``(document_id, filename)`` pairs (#446 f.4).

    ONE metadata query for the evidence-rehydration path — the exact
    tenant + owner-or-grant predicate every retrieval chokepoint uses,
    without reassembling any document text (this path needs names only).
    Ids outside the allow-set are simply absent (existence non-disclosure).
    """
    stmt = select(models.Document.id, models.Document.filename).where(
        models.Document.tenant_id == allow_set.tenant_id,
        models.Document.id.in_(document_ids),
        _document_permitted(allow_set),
    )
    return stmt


def valid_chunk_pairs(*, tenant_id: object, chunk_ids: list[UUID]) -> Select[tuple[UUID, UUID]]:
    """(chunk_id, document_id) for chunks that really exist in this tenant.

    The defense-in-depth half of evidence rehydration (#446 round-2, finding
    4): a corrupt digest pairing a permitted document with a foreign/fabricated
    chunk id must not smuggle that id into a prompt — only chunks that belong
    to the claimed document survive the join at the caller.
    """
    return select(models.Chunk.id, models.Chunk.document_id).where(
        models.Chunk.tenant_id == tenant_id,
        models.Chunk.id.in_(chunk_ids),
    )


def _permission_filter(stmt: Select[_RowT], allow_set: AllowSet) -> Select[_RowT]:
    """Fold the tenant + ownership/grant predicate into a chunk/document ``SELECT``.

    The single helper every chunk query in this module routes through, so the
    permission predicate is applied in exactly one place and cannot be forgotten:
    the chunk's ``tenant_id`` (INV-1) **and** the owning document being permitted —
    owned by someone in the allow-set **or** explicitly granted to the requester
    (INV-2, deny-by-default ownership-or-grant; a collection grant cascades to its
    documents). Assumes the statement already joins ``chunks`` to ``documents``
    (the caller does the join so the document name is available for citations and
    the grant ``EXISTS`` can correlate to ``documents.id`` / ``collection_id``).
    """
    return stmt.where(
        models.Chunk.tenant_id == allow_set.tenant_id,
        _document_permitted(allow_set),
    )


def _base_chunk_select() -> Select[tuple[UUID, UUID, str, int, str, int, int]]:
    """A chunk-joined-document select carrying the columns passages need.

    Joins ``chunks`` to their ``documents`` so a hit carries the document name +
    owner (the latter for the permission predicate, the former for citations).
    """
    return select(
        models.Chunk.id,
        models.Chunk.document_id,
        models.Document.filename,
        models.Chunk.ord,
        models.Chunk.text,
        models.Chunk.char_start,
        models.Chunk.char_end,
    ).join(models.Document, models.Chunk.document_id == models.Document.id)


async def semantic_search(
    session: AsyncSession,
    *,
    allow_set: AllowSet,
    query_embedding: Sequence[float],
    k: int,
    collection_ids: Sequence[UUID] | None = None,
) -> list[UUID]:
    """Return the top-``k`` chunk ids by pgvector cosine distance (permission-filtered).

    Orders the permitted, embedded chunks by ``embedding <=> :query`` (pgvector
    cosine distance, ascending = most similar first) and returns their ids,
    best→worst. ``NULL`` embeddings (a chunk pending embedding) are excluded.
    Optionally narrows to ``collection_ids``. The ANN index added by the #45
    migration accelerates this; correctness does not depend on it.
    """
    distance = models.Chunk.embedding.cosine_distance(list(query_embedding))
    stmt = (
        select(models.Chunk.id)
        .join(models.Document, models.Chunk.document_id == models.Document.id)
        .where(models.Chunk.embedding.is_not(None))
    )
    stmt = _permission_filter(stmt, allow_set)
    if collection_ids:
        stmt = stmt.where(models.Document.collection_id.in_(collection_ids))
    stmt = stmt.order_by(distance.asc()).limit(k)
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)


async def lexical_search(
    session: AsyncSession,
    *,
    allow_set: AllowSet,
    query: str,
    k: int,
    collection_ids: Sequence[UUID] | None = None,
) -> list[UUID]:
    """Return the top-``k`` chunk ids by Postgres full-text rank (permission-filtered).

    Matches ``to_tsvector('english', text)`` against ``plainto_tsquery('english',
    :query)`` and orders by ``ts_rank`` descending (best first). The GIN index
    added by the #45 migration accelerates the match. Permission-filtered exactly
    like the semantic side (INV-1/INV-2) and optionally narrowed to
    ``collection_ids``. A query that matches nothing returns an empty list.
    """
    tsvector = func.to_tsvector("english", models.Chunk.text)
    tsquery = func.plainto_tsquery("english", query)
    rank = func.ts_rank(tsvector, tsquery)
    stmt = (
        select(models.Chunk.id)
        .join(models.Document, models.Chunk.document_id == models.Document.id)
        .where(tsvector.op("@@")(tsquery))
    )
    stmt = _permission_filter(stmt, allow_set)
    if collection_ids:
        stmt = stmt.where(models.Document.collection_id.in_(collection_ids))
    stmt = stmt.order_by(rank.desc()).limit(k)
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)


async def load_passages(
    session: AsyncSession,
    *,
    allow_set: AllowSet,
    chunk_ids: Sequence[UUID],
) -> dict[UUID, PassageRow]:
    """Hydrate fused chunk ids into citation-bearing rows (permission re-checked).

    Given the fused id ranking, fetch each chunk's text + source span + document
    name in one query. The permission predicate is applied **again** here (defense
    in depth: even the hydration step cannot read a row outside the allow-set), so
    a fused id that somehow referenced a foreign-owner chunk would be dropped
    rather than leaked. Returns a mapping ``chunk_id → row`` (the service restores
    the fused order); ids not present in the result were filtered out.
    """
    if not chunk_ids:
        return {}
    stmt = _base_chunk_select().where(models.Chunk.id.in_(chunk_ids))
    stmt = _permission_filter(stmt, allow_set)
    result = await session.execute(stmt)
    rows: dict[UUID, PassageRow] = {}
    for cid, document_id, filename, ordinal, text, char_start, char_end in result.all():
        rows[cid] = PassageRow(
            chunk_id=cid,
            document_id=document_id,
            document_name=filename,
            ord=ordinal,
            text=text,
            char_start=char_start,
            char_end=char_end,
        )
    return rows


async def document_search(
    session: AsyncSession,
    *,
    allow_set: AllowSet,
    name_or_query: str,
    k: int,
) -> list[DocumentRow]:
    """Return permitted documents matching ``name_or_query`` by filename (lexical).

    The ``search_documents`` tool's query: a case-insensitive substring match on
    the filename within the principal's allow-set (INV-1/INV-2). Permitted =
    owned by the requester **or** explicitly granted to them (directly or via the
    document's collection — the grant cascade). Ordered by filename for a stable
    result; ``k`` caps the count. (Richer metadata search is a fast-follow;
    filename is the MVP signal — matching the #44 repository's own
    ``filename_query`` lexical match.)
    """
    stmt = (
        select(models.Document.id, models.Document.filename)
        .where(
            models.Document.tenant_id == allow_set.tenant_id,
            _document_permitted(allow_set),
            models.Document.filename.ilike(f"%{name_or_query}%"),
        )
        .order_by(models.Document.filename.asc(), models.Document.id.asc())
        .limit(k)
    )
    result = await session.execute(stmt)
    return [DocumentRow(document_id=did, document_name=name) for did, name in result.all()]


async def list_documents(
    session: AsyncSession,
    *,
    allow_set: AllowSet,
    k: int,
) -> list[DocumentRow]:
    """Return the permitted documents for a principal, no query (the ``list_documents`` tool).

    The enumeration counterpart of :func:`document_search`: the identical
    permission predicate (tenant + ownership-or-grant, with the collection-grant
    cascade — INV-1/INV-2) but **without** a filename filter, so it answers "what
    documents can I access" rather than "which match this name". Permitted = owned
    by the requester **or** explicitly granted to them (directly or via the
    document's collection). Ordered by filename for a stable list; ``k`` caps the
    count (the service clamps it to a safe ceiling) so a large corpus can never
    turn into an unbounded scan. A principal with no owned/granted documents yields
    an empty list.
    """
    stmt = (
        select(models.Document.id, models.Document.filename)
        .where(
            models.Document.tenant_id == allow_set.tenant_id,
            _document_permitted(allow_set),
        )
        .order_by(models.Document.filename.asc(), models.Document.id.asc())
        .limit(k)
    )
    result = await session.execute(stmt)
    return [DocumentRow(document_id=did, document_name=name) for did, name in result.all()]


async def load_document_text(
    session: AsyncSession,
    *,
    allow_set: AllowSet,
    document_id: UUID,
) -> DocumentTextRow | None:
    """Return a permitted document's reassembled text, or ``None`` (INV-2 → not found).

    Establishes the document is within the allow-set (tenant + ownership-or-grant),
    then concatenates its chunks in ``ord`` order into the document text. Returns
    ``None`` when the document is missing, in another tenant, or owned by another
    user **without** an explicit grant to the requester (directly or via its
    collection) — the ``get_document`` tool maps that to "not found" (existence
    non-disclosure, never a 403). An explicitly-granted document is returned
    (citable), so a granted document is retrievable by the grantee (INV-2).
    """
    doc_stmt = select(models.Document.id, models.Document.filename).where(
        models.Document.id == document_id,
        models.Document.tenant_id == allow_set.tenant_id,
        _document_permitted(allow_set),
    )
    doc = (await session.execute(doc_stmt)).first()
    if doc is None:
        return None
    _, filename = doc

    chunk_stmt = (
        select(models.Chunk.text)
        .where(
            models.Chunk.tenant_id == allow_set.tenant_id,
            models.Chunk.document_id == document_id,
        )
        .order_by(models.Chunk.ord.asc())
    )
    texts = (await session.execute(chunk_stmt)).scalars().all()
    return DocumentTextRow(
        document_id=document_id,
        document_name=filename,
        text="".join(texts),
    )
